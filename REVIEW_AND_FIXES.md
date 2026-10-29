# MintScope AI — Review and Fixes

## v0.7 (2026-10-29): installable local app, Pro license, Featured Collections

- Local app (`mintscope`): UI on 127.0.0.1 (Analyze, Discover, Featured, Settings), no payment/quota,
  Host-header and custom-header guards. Keyless first: new `onchain.py` analysis (public RPC + Blockscout) and
  OpenSea public reads without a key; the user's own OpenSea key (OS keyring or 600 file, masked, sent only to
  api.opensea.io) adds keyed data. "Test key" uses an endpoint that rejects bad keys (checked live: a fake key
  gets 401; some OpenSea reads answer without any key).
- Wash-pattern heuristic for established collections excludes contract counterparties (escrow, lending,
  staking) and uses the share of secondary transfers, after BAYC showed 76 raw round trips, mostly via contracts.
- Pro license scaffold: Ed25519 offline verification, owner script (private key outside the project, 600,
  never printed). No production key generated; Reza must run keygen.
- Featured Collections + review queue + sponsored slots (off) + analytics; score invariance enforced and tested.
- Packaging: PyInstaller one-file Linux build tested (24 MB); GitHub Actions release workflow for macOS
  arm64/x64, Windows and Linux; `pyproject.toml` for pipx/uv (install tested in a clean venv).
- Tests: 130 passed.

## v0.6 (2026-10-29): pricing

- Free tier (3 analyses/month per caller, SQLite `free_usage`, IP + optional `X-Payer-Address`, UTC monthly reset)
  returns only the overall score, a short summary and the analyses left.
- Paid full report $2.99 via x402 v1 `exact`, USDC on Base, PayAI facilitator verify → replay reservation →
  report → settle; revenue logged to the ledger only after a successful settlement (`x402_payment`,
  `revenue_usd`, excluded from `spent_usd`). `X-PAYMENT-RESPONSE` receipt header.
- `pricing` in both manifests (tool-sdk `x402UsdcPricing` format); `npx @opensea/tool-sdk validate`: valid.
- Landing/pricing page at `/` and `/pricing` with the disclaimer and "monthly plan coming soon".
- Paid reports refused in demo mode (409) so synthetic data is never sold.
- Tests: 78 passed (22 new pricing tests, facilitator mocked). Live check: PayAI `/verify` rejected a fake
  signature with `invalid_exact_evm_signature`. No real payment was made (needs a payer wallet that signs).

## v0.5 (2026-10-29): discovery of NEW collections

Owner feedback: analyze new launches (today / this week / this month), not old blue chips.

- New `GET/POST /discover?period=today|week|month&chain=all|ethereum|base&limit=N` (`discovery.py`).
- Keyless source: OpenSea SeaDrop `SeaDropMint` events (OpenSea Drops, Ethereum + Base) via public RPC;
  "new" = contract had no code at the window's first block; creation block by binary search.
- Per collection, onchain: distinct minters/payers, items minted, mint revenue and price, mint progress vs
  `maxSupply`, holders and top-holder share (replayed ERC-721 Transfers since creation), secondary transfers,
  back-and-forth transfer patterns; Blockscout (Ethereum) verification, scam label, deployer. OpenSea data and
  socials when `OPENSEA_API_KEY` is set (enrichment is mock-tested only; no key was available).
- New-launch score: momentum 30, demand 20, distribution 25, trust 25, minus red-flag penalties
  (high −15, warning −5). Red flags: single-wallet holding/minting, very few minters, wash-trading pattern,
  unverified source, explorer scam label, deployer is top minter, large mint outside the public drop, no socials.
- Second ERC-8257 manifest `/.well-known/ai-tool/mintscope-new-collections.json` (endpoint `/discover`).
- Sources that did NOT work keylessly (checked 2026-10-29): every OpenSea list endpoint (`/collections`,
  `/drops`, `/collections/trending`, `/collections/top`, `/events`) returns 401 without a key; base.blockscout.com
  answers with a browser challenge; Magic Eden's EVM API is deprecated; topic-only `eth_getLogs` for all
  Transfer mints is refused or too large on free RPCs.
- Tests: 56 passed (16 new in `tests/test_discovery.py`).
- Real runs (both chains, 2026-10-29): today 23 new collections (61 s), week 34 (295 s), month 49 (728 s).

## v0.4 (2026-10-29)

### Verification

- Tests before: **25 passed** (original v0.3 code). After: **40 passed** (15 new regression tests in `tests/test_fixes.py`).
- Server smoke test: `/health`, `/analyze` (demo-collection, demo-sparse, contract address, 10 invalid inputs),
  `/reports/{id}`, `/ledger`, manifest route. Demo latency over 300 sequential requests: p50 3.0 ms, p95 4.7 ms.
- Live check against real OpenSea API v2 responses (`scripts/eval_live.py`, 17 identifiers, run without a key so
  only CDN-cached collections answered): v0.3 analyzed 13, v0.4 analyzed 14; 0 field-mapping mismatches against
  the raw API in both; median latency per analysis 0.13–0.15 s (2 upstream calls). The other identifiers got
  401 "Missing an API Key": they need `OPENSEA_API_KEY`, not a code fix.

### Bugs fixed (by severity)

High
1. **Average sale price was always unknown in live mode.** OpenSea v2 stats no longer return `average_price`
   (current schema: `volume`, `volume_symbol`, `sales`, `num_owners`, `floor_price`, `floor_price_symbol`).
   0/13 real reports had it. Now derived as volume / sales, labeled `derived_from_provider`: 14/14.
2. **Zero floor treated as a real price.** OpenSea returns `floor_price: 0` when nothing is listed; v0.3 reported
   a floor of 0 (and could raise `floor_below_mint`). Now unknown + `no_active_listing` info flag.
3. **Mixed currencies.** One `price_unit` (the floor currency) was applied to volume and averages, although
   OpenSea reports `volume_symbol` separately and documents that the two can differ. Each price/volume value now
   carries its own `unit`; reports have `price_unit` and `volume_unit`.

Medium
4. **False "high" owner-concentration flag on edition (ERC-1155) collections.** Real example: basepaint
   (297,323 copies, 34,972 owners → ratio 0.12 → "high"). Owners/supply is now skipped when supply > distinct
   items (`unique_item_count`). Its score moved 85.3 → 93.8.
5. **Outdated chain list.** `klaytn` and `blast` are no longer valid OpenSea chains (calls would fail), while
   `ape_chain`, `abstract`, `ronin`, `shape`, `unichain`, `sei`, `flow` and others were rejected. Synced with the
   API's `ChainIdentifier` enum; `0x` addresses on `solana` are rejected with 422.
6. **A live (public) server could return synthetic data**: callers could send `mode: "demo"`. Refused (409)
   when `MINTSCOPE_MODE=live`.
7. **Contract address case not normalized**: the same contract in different case gave different identifiers
   and different demo data. Normalized to lowercase.
8. **Provider-supplied slug inserted into the URL unescaped** (from the contract lookup). Now URL-encoded.
9. **Malformed `contracts` entries** (non-objects) could crash with 500. Now skipped.

Low
10. `OPENSEA_BASE_URL` accepted `http://` (the API key would be sent in clear). Only https is accepted.
11. Manifest: added `chain` enum, `verifiability` (self-attested), the `ai` and `trading` category tags, and
    optional https `image` / `featuredImage` (a 16:9 banner is required to be featured on opensea.io/tools).
12. Added `Dockerfile` (with `--proxy-headers` so the per-IP rate limit works behind a proxy) and `.dockerignore`.

### Known limitations (not fixed)

- Scoring saturates: 6 of 13 distinct real collections (BAYC, Pudgy Penguins, Azuki, Doodles, Milady, CloneX) scored exactly 98.4. Weights are unvalidated hypotheses.
- Mint price, contract source verification and artwork quality stay unknown with OpenSea as the only source.
- OpenSea `total_supply` can differ from onchain `totalSupply()` (BAYC: 9,996 vs 10,000 onchain).
- Rate limiter is in-process; no accounts, billing, or x402 payments.

## v0.2 (previous review)

## Verification performed

- Existing tests before changes: **18 passed**.
- Tests after changes: **22 passed**.
- `python -m compileall -q .`: passed.
- Local smoke test: `/health`, demo `/analyze`, and saved-report retrieval passed.
- Live OpenSea was **not** tested against the external network because no real API key/live-network verification was performed; live-provider behavior is covered by mocked tests.

## Issues found and addressed

1. **Non-finite or invalid cost configuration** — `NaN`, infinity, negative limits, or unreasonable timeout values could produce unsafe or confusing guard behavior. Numeric settings are now range-checked and non-finite values fall back to safe defaults.
2. **Concurrent budget checks** — simultaneous live requests could pass the same remaining-budget check. Live calls are serialized within one process, and the budget guard estimates the maximum number of calls needed for the identifier type.
3. **Daily call cap** — the old check tested whether the current count had reached the cap, not whether the complete next analysis could exceed it. The new check accounts for the expected two calls for a slug or three for a contract address.
4. **Malformed upstream numeric fields** — invalid strings, negative values, booleans, or infinities could contaminate calculations or trigger a server error. Provider numeric values are now normalized; invalid values become unknown.
5. **Transparency score from absent fields** — an entirely empty set of provider fields was treated as a confirmed zero transparency score. It is now marked unknown unless at least one transparency field is observed.
6. **Unknown marketplace status** — unrecognized statuses previously received an arbitrary score. They are now excluded from scoring and identified as unknown/unrecognized.
7. **Zero-price mints** — truthiness checks incorrectly ignored a legitimate mint price of zero. Comparisons now check explicitly for `None`.
8. **Rate-limiter memory growth** — the per-process client map now prunes stale entries when it grows large.
9. **Public API exposure** — added optional `MINTSCOPE_API_TOKEN` protection for analysis, report retrieval, and ledger routes. `/health` remains public. The README explains that this is shared-token protection, not full production authentication.
10. **SQLite path robustness** — parent directories are created before opening a database file.
11. **Report ID entropy** — report identifiers now use the full UUID hex value rather than a truncated 64-bit value.
12. **Holder metric wording** — the risk message now explains that owners-to-supply is only a rough proxy and not a top-holder concentration analysis.
13. **Input bounds** — identifier and chain strings now have explicit maximum lengths.

## Known limitations not solved by this MVP

- OpenSea API responses are provider-reported, not independently verified; live external API behavior still needs a real-key integration test.
- The application does not provide per-user accounts, customer-specific quotas, payments, subscription enforcement, or distributed rate limiting.
- `MINTSCOPE_API_TOKEN` is a shared optional token. Set it before public deployment, use HTTPS, and do not expose the service directly without additional controls.
- The in-memory rate limiter is per process and does not coordinate across multiple workers or machines.
- The cost ledger is an estimate based on configured per-call costs. A crash between an upstream call and local ledger persistence could cause under-recording.
- Artwork quality, mint price, and contract source verification are not comprehensively assessed by the current provider implementation.
- Scoring weights and risk thresholds remain unvalidated hypotheses and are not a financial prediction or smart-contract audit.
- The product does not yet include autonomous customer discovery, outreach, payment collection, or always-on hosting.
