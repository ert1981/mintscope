# MintScope AI (v0.7)

Evidence-based NFT collection analysis as one HTTP tool. Give it an OpenSea collection slug or a contract
address; it returns a JSON report with market facts, creator evidence, risk flags, the data it could not find,
a hypothesis-weighted score, and a confidence level. It never predicts or promises profit.

**New in v0.5: discovery of NEW collections** (`GET/POST /discover`): finds NFT collections launched today,
this week or this month, analyzes them onchain and ranks them by early momentum, with red flags typical of new
launches. Works without any API key.

**New in v0.6: pricing.** Free tier: 3 analyses per month per caller (overall score + short summary).
Full report: **$2.99 per report**, paid per request in USDC on Base with **x402** (HTTP 402), the same way
`@opensea/tool-sdk` tools charge. Landing/pricing page at `/` and `/pricing`. See [Pricing](#pricing-v06).

**New in v0.7: installable local app** (bring your own key). Download one file (no Python needed) or
`pipx install`, run `mintscope`, and a local web UI opens at `http://127.0.0.1:8765` with **Analyze**,
**Discover**, **Featured** and **Settings**. It works **without any key** (public onchain data + OpenSea's
public reads); users can add their **own** OpenSea key, stored only on their device. No payment and no quota
locally. Plus: **Pro license** (one-time, offline-verified), **Featured Collections** with an admin-only review
queue, optional **Sponsored** slots (off by default) and per-item analytics. See [Local app](#local-app-v07).

It is built to be listed on **OpenSea Tools** (<https://opensea.io/tools>) as an
[ERC-8257](https://docs.opensea.io/docs/agent-tool-registry) tool: one HTTPS endpoint (`POST /analyze`) plus a
manifest at `/.well-known/ai-tool/mintscope-nft-analysis.json`.

- **Stack:** Python 3.10+, FastAPI, requests, SQLite. No paid services.
- **Data:** official OpenSea API v2 (`OPENSEA_API_KEY`). Without a key the app runs in clearly labeled
  **demo mode** with synthetic data.

## Quick start

```bash
cd mintscope
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q                          # 130 tests, no key or network needed
uvicorn app:app --reload           # http://127.0.0.1:8000/docs
```

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/analyze -H "Content-Type: application/json" -d '{"identifier": "demo-collection"}'
curl http://127.0.0.1:8000/reports/<report_id>
curl http://127.0.0.1:8000/ledger
```

`demo-sparse` simulates a project with almost no public data, to show how unknown fields are reported.

## Live data

```bash
cp .env.example .env      # then set OPENSEA_API_KEY in .env (never commit it)
uvicorn app:app
```

Get a key from OpenSea: a full key at <https://opensea.io/settings/developer>, or the documented instant
free-tier key (600 reads/hour, expires after 7 days): `curl -s -X POST https://api.opensea.io/api/v2/auth/keys`.
An expired key returns `502` "rejected the API key". The key is read only from the environment, is sent only
to the https OpenSea base URL, and never appears in reports, errors, the ledger, or the manifest.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status, configured mode, whether live data is available |
| POST | `/analyze` | `{"identifier": "...", "chain": "ethereum", "mode": "auto"}` → report |
| GET | `/reports/{report_id}` | Retrieve a saved report |
| GET | `/ledger` | Cost and usage ledger |
| GET/POST | `/discover` | `?period=today\|week\|month&chain=all\|ethereum\|base&limit=10` → ranked new collections |
| GET | `/.well-known/ai-tool/mintscope-nft-analysis.json` | ERC-8257 manifest for `/analyze` (only when configured) |
| GET | `/.well-known/ai-tool/mintscope-new-collections.json` | ERC-8257 manifest for `/discover` (only when configured) |

- `identifier`: OpenSea collection slug (`[a-z0-9_-]`, 2–100 chars) or `0x` + 40 hex address. Both are
  normalized to lowercase.
- `chain`: one of the OpenSea API chain slugs (`ethereum`, `base`, `polygon`, `arbitrum`, `optimism`, `zora`,
  `ape_chain`, `abstract`, `ronin`, `shape`, `solana`, …; see `providers.CHAINS`). Used for contract addresses;
  `solana` only accepts slugs.
- `mode`: `auto` (live if a key is configured, else demo), `demo`, or `live` (503 without a key). A server
  started with `MINTSCOPE_MODE=live` refuses `demo` (409), so a public tool never returns synthetic data.

Errors: `422` invalid input, `404` unknown collection/report, `409` mode not allowed, `429` local rate limit,
daily live-call cap or provider rate limit, `402` budget guard, `502` provider error or rejected key,
`504` provider timeout. A provider failure is an error; the app never silently falls back to demo data.

## Discovery of new collections (`/discover`)

```bash
curl "http://127.0.0.1:8000/discover?period=today&chain=all&limit=10"
curl -X POST http://127.0.0.1:8000/discover -H "Content-Type: application/json" -d '{"period": "week", "chain": "base"}'
```

Periods: `today` = last 24 h, `week` = last 7 days, `month` = last 30 days. Measured scan times (both chains,
2026-10-29): today 61 s, week 295 s, month 728 s. Results are cached (10 min / 30 min / 60 min) and one scan runs at
a time; for a public tool, pre-warm `week` and `month` on a schedule. In the POST body `period` is required, so an
empty probe gets a fast 422.

How it works (no key needed):

1. Reads every `SeaDropMint` event in the window from OpenSea's SeaDrop contract
   (`0x00005EA00Ac477B1030CE78506496e8C2dE24bf5`, the contract behind OpenSea Drops, same address on Ethereum
   and Base) through public RPC endpoints. Each event gives the NFT contract, minter, payer, quantity and price.
2. Takes the collections with the most distinct minters (`MINTSCOPE_DISCOVERY_MAX_CANDIDATES`, default 30 per
   chain) and keeps only those whose contract **had no code at the window's first block**, i.e. deployed inside
   the window. The creation block is found by binary search, giving the exact launch time.
3. For each new collection: name, symbol, `totalSupply`, `maxSupply` (eth_call); every ERC-721 Transfer since
   creation (holders, top holder share, secondary transfers, round trips); Blockscout on Ethereum (source
   verification, scam label, deployer); OpenSea collection data and sales stats when `OPENSEA_API_KEY` is set.

New-launch score (0–100, visible components):

| Component | Weight | Basis |
|---|---|---|
| Momentum | 30% | distinct minters (log scale) and minters per day since launch |
| Demand | 20% | paid mint revenue (log scale) and mint progress vs. `maxSupply` |
| Distribution | 25% | 1 − top-holder share, number of holders |
| Trust | 25% | verified source, explorer scam label, share minted outside the public drop, socials (if known) |

Red flags subtract from the score (high −15, warning −5): `single_wallet_concentration`, `single_wallet_minting`,
`very_few_minters`, `wash_trading_suspected` (round trips / repeated wallet pairs), `contract_source_unverified`,
`explorer_scam_label`, `creator_is_top_minter`, `large_mint_outside_drop`, `no_socials`. Info only:
`socials_unknown` (no key), `very_new` (< 2 h).

Limits: only OpenSea SeaDrop ERC-721 drops on Ethereum and Base are seen (not Zora, Manifold or custom mint
contracts); transfers are not sales; Base contract verification is unknown because Base's Blockscout blocks
server requests. Public RPC endpoints are configured in `discovery.CHAIN_CONFIG`.

## Report

Every value is `{"value", "status"}` where status is `provider_reported`, `derived_from_provider` (computed by
MintScope from provider values, with a `derivation`), `synthetic` (demo) or `unknown`. Price/volume values carry
a `unit`; the report also has `price_unit` (floor currency) and `volume_unit` (volume currency), because OpenSea
reports them separately and they can differ.

- Floor price = lowest active **listing**, never a realized sale. OpenSea returns `0` when nothing is listed;
  MintScope reports that as unknown plus a `no_active_listing` flag, not as a price of 0.
- Average sale price = all-time volume / all-time sales (OpenSea v2 no longer returns an average).
- Owners-to-supply is skipped for edition collections (supply > distinct items, e.g. ERC-1155), where it is
  meaningless.
- Mint price and contract source verification are not available from OpenSea, so live reports keep them unknown.
- Artwork quality is not scored.

## Scoring (hypothesis, not a profit model)

| Component | Weight | Basis |
|---|---|---|
| Track record / credibility | 25% | OpenSea safelist status, known owner address |
| Artwork quality | 20% | not assessed (always unknown) |
| Sales & liquidity | 25% | sales/supply, 7-day sales; penalized if average sale < mint |
| Technical & contract risk | 20% | contract verification, owners/supply (non-edition only) |
| Transparency | 10% | description, website, X/Twitter, Discord published |

The score is the weighted average of the components that have data; `coverage` is the weight backed by data.
Confidence: `high` (coverage ≥ 75% and ≤ 3 unknown fields), `medium` (≥ 50%), else `low`.
Known limitation: large, verified, liquid collections all saturate near 98/100; the score separates
weak from strong projects but does not rank blue chips against each other.

## Local app (v0.7)

### Install (users)

| Option | Command |
|---|---|
| Standalone executable (no Python) | download `mintscope-<os>-<arch>` from the GitHub Release and run it (macOS: `chmod +x` then right-click → Open the first time, the binary is not notarized) |
| pipx (one line) | `pipx install git+https://github.com/<owner>/mintscope.git` then `mintscope` |
| uv | `uv tool install git+https://github.com/<owner>/mintscope.git` |

`mintscope` (or `mintscope local --port 8765 --no-browser`) starts the app on **127.0.0.1 only** and opens the
browser. Data (SQLite, settings) lives in the user config dir: Linux `~/.config/mintscope`, macOS
`~/Library/Application Support/MintScope`, Windows `%APPDATA%\MintScope` (override `MINTSCOPE_CONFIG_DIR`).

### What works without a key
- **Analyze** a contract address, OpenSea item link or Etherscan/Basescan link on **Ethereum or Base**:
  contract code, ERC-721/1155 support, name/supply, 7-day transfer activity, wash patterns between wallets
  (escrow/lending contracts excluded), Blockscout verification, scam label and holder count (Ethereum) → a
  **safety score** (higher = fewer risks found) with red flags.
- OpenSea's **public** reads (collection + stats) are tried without a key; when OpenSea answers, market data is
  added and labeled `access: public_no_key`. Collection slugs work this way too while OpenSea allows it.
- **Discover** new launches (today / week / month) from onchain SeaDrop events.
- **Featured** list and submissions.

### Your own OpenSea key (optional)
Settings → paste key → **Save** / **Test key** / **Remove**.
- Stored in the **OS keyring** (macOS Keychain, Windows Credential Manager, Linux Secret Service) when available,
  otherwise `secrets.json` with permission **600** in the config dir (folder 700).
- Sent **only** to `api.opensea.io`. The UI only ever gets `••••abcd`; keys are never in logs (access log off),
  reports, exports or responses. *Test key* calls an OpenSea endpoint that always requires a key.
- There is **no AI-model key** field: the code does not call any AI model.
- Each user pays their own API usage (OpenSea's free key tier costs $0).

### Local security
Bound to 127.0.0.1; requests with any other `Host` are refused (DNS-rebinding guard); state-changing
`/api/local/*` calls need the `X-MintScope-Local: 1` header, which other websites cannot send (no CORS).

### Pro (one-time license): $9 intro, then $19
Pro unlocks **Compare** (2–5 collections side by side, ranked) and **bulk JSON export** (up to 25 at once).
- A license is `MSL1.<payload>.<Ed25519 signature>`, checked **offline** against the public key embedded in
  `license_pubkey.py`. No server, no phone-home.
- Owner tool (not bundled in the executable): `python admin/license_admin.py keygen` creates the keypair,
  writes the **private key only to `~/.mintscope-admin/license_ed25519.pem` (permission 600, outside the
  project)**, never prints it, and writes the public key into `license_pubkey.py`.
  `python admin/license_admin.py sign --lid L-0001 --name "Buyer"` prints a license.
- Licenses will be sold through a store page later (not wired yet). This build ships **without** a public key,
  so Pro shows "Licensing not configured" until the owner runs keygen and rebuilds.

## Featured Collections, submissions, sponsored slots

- **Admin-curated list** with image, mint link, chain, contract, note and an **automatic keyless risk analysis**.
  Admin CLI (works on the local database):
  `mintscope admin featured add --contract 0x... --chain ethereum --image https://... --mint-url https://... --note "..."`,
  `... featured list | edit <id> --note ... --active no | remove <id> | reanalyze <id> | sponsor <id> --plan 7d`,
  `mintscope admin submissions list | approve <id> --image https://... | reject <id>`.
  Admin HTTP API (only when `MINTSCOPE_ADMIN_TOKEN` is set to 24+ random characters; header `X-Admin-Token`):
  `GET/POST /admin/featured`, `PATCH/DELETE /admin/featured/{id}`, `POST /admin/featured/{id}/reanalyze`,
  `POST/DELETE /admin/featured/{id}/sponsor`, `GET /admin/submissions`, `POST /admin/submissions/{id}/approve|reject`,
  `GET /admin/analytics`.
- **Public:** `GET /featured`, `GET /featured/{id}`, `GET /featured/{id}/mint` (redirect, counts a click).
- **Submissions:** `POST /submissions {"input": "<contract or link>", "mint_url": "..."}` (5 per IP per day).
  Each one is analyzed and checked: not a contract, no NFT interface, scam label, unverified source, very new,
  wash patterns, concentration, **name impersonation** of well-known collections (official contracts checked
  onchain), **mint-link checks** (not https, punycode, IP address, URL shortener, look-alike of OpenSea/Zora/...),
  duplicates. Everything waits in a **review queue that only the admin can approve**.
- **Sponsored slots** (7 days $9, 30 days $29) are modeled with a **"Sponsored"** label, start/end dates, price
  and stats during the slot, but are **disabled** unless `MINTSCOPE_SPONSORED_ENABLED=on`. No payment is wired.
- **Hard rule:** featuring or sponsorship **never changes the risk score**. The score is read only from the
  stored analysis (whose function has no featured/sponsored input), admin edits cannot contain a score, and an
  integrity check refuses to publish a mismatch. Sponsorship only pins the item to the top, labeled.
- **Analytics:** views (list and detail) and mint-link clicks per item, per day, plus click rate.
- The local app shows the Featured list from its own database, or from Reza's server when
  `MINTSCOPE_FEATURED_FEED_URL=https://<host>` is set (then submissions are forwarded there; no keys are sent).

## Building executables

- Linux (tested): `pip install -r requirements.txt pyinstaller && python scripts/build_exe.py` →
  `dist/mintscope-linux-x64` (about 24 MB, one file). PyInstaller needs a Python built with a shared library
  (python.org, Homebrew, `uv python install` are fine).
- macOS arm64 + x64, Windows x64 and Linux x64: `.github/workflows/release.yml` runs the tests, builds on
  `macos-15`, `macos-15-intel`, `windows-latest`, `ubuntu-22.04` and attaches the files plus `SHA256SUMS.txt` to a
  GitHub Release when a tag `v*` is pushed (`git tag v0.7.0 && git push origin v0.7.0`).
- Binaries are not code-signed: macOS Gatekeeper and Windows SmartScreen will warn until signing is added.

## Pricing (v0.6)

| Tier | Price | What you get |
|---|---|---|
| Free | $0, 3 per month per caller | Overall score (0-100), short project summary, `free_analyses_left` |
| Full report | $2.99 per report (USDC) | Full report: all metrics with sources, risk flags with evidence, `weaknesses`, missing data; for `/discover` all metrics, red flags and score components |
| Monthly plan | coming soon | Not available yet |

Both `/analyze` and `/discover` accept `"tier": "auto" | "free" | "full"` (default `auto`: full when an
`X-Payment` header is sent, otherwise free).

**Free tier counting.** SQLite table `free_usage` (caller, UTC month). The caller is always the client IP; if the
request has an `X-Payer-Address: 0x...` header that wallet is counted too, and the stricter count applies (so a
new header cannot reset the quota). The counter resets at the start of each UTC month. When it is used up, the
response is HTTP 402 with the payment offer (or 429 if the paid tier is not configured / in demo mode).

**Paid flow (x402 v1, scheme `exact`).** Mirrors `@opensea/tool-sdk`'s hosted x402 gate:
1. Request with `tier: "full"` and no `X-Payment` → `402` + `X-Accept-Payment: x402` and
   `{"x402Version":1,"accepts":[{"scheme":"exact","network":"base","maxAmountRequired":"2990000",
   "payTo":"<MINTSCOPE_PAYEE_ADDRESS>","asset":"<USDC>","resource":...,"extra":{"name":"USD Coin","version":"2"}}]}`.
2. The caller's x402 client signs an EIP-3009 USDC authorization and repeats the request with `X-Payment`.
3. MintScope sends it to the facilitator's `/verify` (default PayAI, `https://facilitator.payai.network`,
   free, no API key). Invalid → 402 with the reason; facilitator down → 502.
4. The (payer, nonce) authorization is reserved in SQLite (`x402_authorizations`) → a replay gets
   402 `payment_authorization_already_used`.
5. The report is built, then `/settle` is called. **Only when settlement succeeds with a transaction hash** is the
   report returned (with `payment` and the `X-PAYMENT-RESPONSE` header) and the revenue written to the ledger
   (category `x402_payment`, shown as `revenue_usd` in `/ledger`, never counted as spending). If settlement
   fails, no report and no revenue; the reservation is released only for a 429/network error (not broadcast).
6. USDC goes directly from the payer to `MINTSCOPE_PAYEE_ADDRESS`. MintScope never holds or asks for a key.

**Manifest.** When the paid tier is on, both manifests carry `pricing` exactly like `x402UsdcPricing()`:
`[{"amount":"2990000","asset":"eip155:8453/erc20:0x8335...2913","recipient":"eip155:8453:<payee>","protocol":"x402"}]`
(`npx @opensea/tool-sdk validate` → "Manifest is valid"). A changed price or payee changes the manifest hash, so
it needs `update-metadata` after registration.

**Testing.** `MINTSCOPE_X402_NETWORK=base-sepolia` switches to test USDC
(`0x036c...cf7e`, EIP-712 name `USDC`). The test suite mocks the facilitator; a real end-to-end payment needs a
payer wallet that signs, which MintScope never creates or holds. A live call to PayAI `/verify` with a fake
signature was checked: it returned `invalid_exact_evm_signature` (format accepted, payment refused).

## Configuration (`.env`, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `OPENSEA_API_KEY` | – | Live data |
| `MINTSCOPE_MODE` | `auto` | `auto`, `demo` or `live` |
| `MINTSCOPE_API_TOKEN` | – | If set, `/analyze`, `/reports/*`, `/ledger` need `X-API-Token` (leave empty for a public tool) |
| `MINTSCOPE_RATE_LIMIT_PER_MINUTE` | 30 | Analyses per minute per client IP (in-process) |
| `MINTSCOPE_MAX_LIVE_CALLS_PER_DAY` | 200 | Cap on OpenSea requests per day (2 per slug, 3 per address) |
| `MINTSCOPE_BUDGET_USD` / `MINTSCOPE_LIVE_CALL_COST_USD` | 2.00 / 0 | Refuse a live analysis before any request if it could exceed the budget |
| `MINTSCOPE_PUBLIC_BASE_URL` | – | Public https origin, e.g. `https://mintscope.example.com` |
| `MINTSCOPE_CREATOR_ADDRESS` | – | Lowercase **public** address of the wallet that registers the tool |
| `MINTSCOPE_IMAGE_URL` / `MINTSCOPE_FEATURED_IMAGE_URL` | – | https icon (1:1) and banner (16:9) for the listing |
| `MINTSCOPE_DISCOVERY_MAX_CANDIDATES` | 30 | Busiest SeaDrop collections per chain checked for newness per scan |
| `MINTSCOPE_PRICING` | `on` | `off` = old behavior (full report free, no counter) |
| `MINTSCOPE_PAYEE_ADDRESS` | – | **Public** address that receives USDC. Without it the paid tier is off (zero/dead addresses rejected) |
| `MINTSCOPE_PRICE_USDC` | 2.99 | Price per full report |
| `MINTSCOPE_FREE_PER_MONTH` | 3 | Free analyses per caller per UTC month |
| `MINTSCOPE_X402_NETWORK` | `base` | `base` or `base-sepolia` (testing) |
| `MINTSCOPE_X402_FACILITATOR_URL` | PayAI | https x402 facilitator |
| `MINTSCOPE_LOCAL` | off | Local app mode (set by `mintscope local`): no payment, no quota, UI at `/` |
| `MINTSCOPE_CONFIG_DIR` | OS config dir | Where the local app keeps its database, key file and license |
| `MINTSCOPE_KEYSTORE` | auto | `file` forces the 600 file instead of the OS keyring |
| `MINTSCOPE_ADMIN_TOKEN` | – | Enables the admin API (24+ random characters) |
| `MINTSCOPE_SPONSORED_ENABLED` | off | Turns on sponsored slots |
| `MINTSCOPE_FEATURED_FEED_URL` | – | Local app reads Featured from (and forwards submissions to) this https server |
| `MINTSCOPE_SUBMISSIONS_PER_DAY` | 5 | Public submissions per IP per day |

## Listing on OpenSea Tools

OpenSea Tools has **no submission form**. The directory indexes tools registered onchain in the permissionless
ERC-8257 Tool Registry (the tools on the page are registered on Base and Ethereum). Steps:

1. **Host** the app on a public HTTPS origin (any host that runs the `Dockerfile` or `uvicorn app:app`) with
   `MINTSCOPE_MODE=live`, `OPENSEA_API_KEY`, `MINTSCOPE_PUBLIC_BASE_URL`, `MINTSCOPE_CREATOR_ADDRESS`, and
   ideally the two image URLs. Manifest and endpoint must be on the exact same origin (subdomains differ).
2. **Check** both manifests: `npx @opensea/tool-sdk verify https://<host>/.well-known/ai-tool/mintscope-nft-analysis.json`
   and the same for `mintscope-new-collections.json`
   (schema, HTTP 200, origin binding, and a `POST {}` probe, which this app answers with 422 = pass).
3. **Register** each tool from the creator wallet (one tool = one endpoint; costs Base gas, needs the owner's wallet):
   `npx @opensea/tool-sdk register --metadata https://<host>/.well-known/ai-tool/mintscope-nft-analysis.json --network base`
   and again with `.../mintscope-new-collections.json`
4. The tool then appears on opensea.io/tools; the manifest `tags` (`ai`, `nft`, `security`, `analytics`,
   `trading`) drive category filters. The manifest hash is stored onchain, so any later manifest change needs
   `update-metadata`.

Pricing: with `MINTSCOPE_PAYEE_ADDRESS` set, the manifests declare `pricing` (x402, USDC on Base) and the
endpoints charge $2.99 per full report; free calls stay available (3/month). The `POST {}` probe still gets 422.

## Security

- Never commit `.env`; the test suite fails if a key-shaped secret is tracked.
- `/health` is public and shows only basic status. Leave `MINTSCOPE_API_TOKEN` empty only for the public tool.
- The rate limiter is per process; behind a proxy run with `--proxy-headers` (the Dockerfile does) so limits
  apply per caller, and use a proxy-level limiter for real traffic.
- The free counter uses the client IP. Behind a proxy, start uvicorn with `--proxy-headers
  --forwarded-allow-ips=<proxy ip>` so the real caller IP is used (otherwise all callers share one counter).
- Disclaimer (also on `/pricing` and in every response): an analysis and risk tool, not financial advice, no
  guarantee of profit and no guarantee that a project is or is not a scam.
- Reports are stored in SQLite (`dataRetention: full` in the manifest).

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI app, endpoints, mode selection, rate limit, cost guard |
| `analyzer.py` | normalization, scoring, risk flags, confidence, report |
| `providers.py` | demo (synthetic) and OpenSea (live) providers, chain list |
| `data_store.py` | SQLite reports, ledger (spend + revenue), free-tier counter, x402 replay guard |
| `payments.py` | x402 requirements, X-Payment decoding, facilitator verify/settle, manifest pricing |
| `pages.py` | public landing / pricing page (`/`, `/pricing`) |
| `config.py` | settings from environment / `.env` |
| `discovery.py` | new-collection discovery: SeaDrop scan, onchain metrics, red flags, new-launch score |
| `tool_manifest.py` | ERC-8257 manifests (analysis and discovery) |
| `Dockerfile` | container for hosting |
| `cli.py` | `mintscope` command: local app, hosted server, admin commands |
| `local_mode.py` / `local_ui.py` | local app API and single-page UI |
| `keystore.py` | user key storage (OS keyring or 600 file), masking |
| `onchain.py` | keyless analysis (public RPC + Blockscout), input/link parsing, safety score |
| `licensing.py` / `license_pubkey.py` | Pro license verification (Ed25519, offline) and embedded public key |
| `admin/license_admin.py` | owner-only keypair generation and license signing (not bundled) |
| `featured.py` / `routes_featured.py` | featured list, submissions queue, sponsored slots, analytics, admin API |
| `scripts/build_exe.py`, `.github/workflows/release.yml`, `pyproject.toml` | executables, release CI, pipx install |
| `tests/` | 130 tests (acceptance, analyzer, regressions, discovery, pricing, local app, license, featured, onchain) |
