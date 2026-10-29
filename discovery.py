"""Discovery of NEW NFT collections (launched today / this week / this month).

Keyless data path (works without any API key):
- OpenSea SeaDrop, the contract behind OpenSea Drops (same address on Ethereum and Base), emits a
  `SeaDropMint` event for every public mint: NFT contract, minter, payer, quantity and unit price.
  Every collection minting through an OpenSea Drop in the window is a candidate.
- A candidate is NEW when its contract had no code at the window-start block (eth_getCode on an
  archive-capable public RPC), i.e. it was deployed inside the window. Its creation block is then
  found by binary search, giving an exact launch time.
- For new collections: name/symbol/totalSupply/maxSupply (eth_call), every ERC-721 Transfer since
  creation (holders, concentration, secondary transfers, wash-trading patterns), and on Ethereum the
  Blockscout explorer (source verification, scam label, creator address).

Optional: with OPENSEA_API_KEY each result is enriched with OpenSea collection data (slug, socials,
safelist status) and secondary-sale stats.

Limits (stated in every response): only collections that mint through OpenSea SeaDrop (ERC-721) on
Ethereum or Base are seen; transfers are not sales; mint prices are in the chain's native token.
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
# keccak256("SeaDropMint(address,address,address,address,uint256,uint256,uint256,uint256)")
SEADROP_MINT = "0xe90cf9cc0a552cf52ea6ff74ece0f1c8ae8cc9ad630d3181f55ac43ca076b7d6"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
BURN = {ZERO, "0x000000000000000000000000000000000000dead"}
SEL = {"name": "0x06fdde03", "symbol": "0x95d89b41", "totalSupply": "0x18160ddd", "maxSupply": "0xd5abeb01"}

PERIODS = {"today": 86400, "week": 7 * 86400, "month": 30 * 86400}
# Scans of longer windows are slow (a month took ~12 min in testing), so their results are kept longer.
CACHE_TTL = {"today": 600, "week": 1800, "month": 3600}

CHAIN_CONFIG = {
    "ethereum": {
        "block_time": 12.0,
        "rpcs": ["https://rpc.mevblocker.io", "https://mainnet.gateway.tenderly.co", "https://eth.drpc.org",
                 "https://rpc.flashbots.net", "https://ethereum-rpc.publicnode.com"],
        "max_range": 50000,
        "explorer": "https://etherscan.io/address/",
        "blockscout": "https://eth.blockscout.com/api/v2",
        "native": "ETH",
    },
    "base": {
        "block_time": 2.0,
        "rpcs": ["https://base-rpc.publicnode.com", "https://base.drpc.org", "https://mainnet.base.org"],
        "max_range": 50000,
        "explorer": "https://basescan.org/address/",
        "blockscout": None,  # base.blockscout.com sits behind a browser challenge; not usable from a server
        "native": "ETH",
    },
}
DISCOVERY_CHAINS = tuple(CHAIN_CONFIG)

LIMITATIONS = [
    "Only collections minting through OpenSea SeaDrop (ERC-721 OpenSea Drops) on Ethereum and Base are discovered; "
    "other launchpads (Zora, Manifold, custom mint contracts) are not seen yet.",
    "'New' means the contract was deployed inside the period (no contract code at the period's first block).",
    "Onchain transfers are not sales; secondary sales and volume come only from OpenSea when OPENSEA_API_KEY is set.",
    "Wash-trading detection is a transfer-pattern heuristic (round trips, repeated pairs), not proof.",
    "Mint prices and revenue are in the chain's native token (ETH) and cover SeaDrop public mints only.",
    "Scores are hypothesis weights for early momentum, not a prediction of price or profit.",
]


class DiscoveryError(Exception):
    status_code = 502


class RPC:
    """JSON-RPC client with endpoint fallback and adaptive log-range splitting."""

    def __init__(self, chain: str, timeout: float = 20.0, rpcs: list[str] | None = None):
        cfg = CHAIN_CONFIG[chain]
        self.chain = chain
        self.urls = list(rpcs or cfg["rpcs"])
        self.max_range = cfg["max_range"]
        self.timeout = timeout
        self.calls = 0
        self._lock = threading.Lock()

    def _post(self, url: str, method: str, params: list):
        with self._lock:
            self.calls += 1
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
            return None, f"HTTP {r.status_code} non-JSON"
        if not isinstance(body, dict):
            return None, "bad payload"
        if "error" in body:
            return None, str(body["error"])[:200]
        return body.get("result"), None

    def call(self, method: str, params: list):
        errors = []
        for url in self.urls:
            try:
                result, err = self._post(url, method, params)
            except requests.RequestException as exc:
                errors.append(f"{exc.__class__.__name__}")
                continue
            if err is None:
                return result
            errors.append(err)
        raise DiscoveryError(f"all {self.chain} RPC endpoints failed for {method}: {errors[-1] if errors else '?'}")

    def get_logs(self, address: str, topics: list, start: int, end: int) -> list[dict]:
        """Fetch logs in chunks; halve the chunk when a provider complains about range or size."""
        out: list[dict] = []
        chunk = self.max_range
        cur = start
        while cur <= end:
            hi = min(end, cur + chunk - 1)
            flt = {"fromBlock": hex(cur), "toBlock": hex(hi), "address": address, "topics": topics}
            got, last_err = None, None
            for url in self.urls:
                try:
                    res, err = self._post(url, "eth_getLogs", [flt])
                except requests.RequestException as exc:
                    last_err = exc.__class__.__name__
                    continue
                if err is None and isinstance(res, list):
                    got = res
                    break
                last_err = err
            if got is None:
                if chunk > 500:
                    chunk //= 2
                    continue
                raise DiscoveryError(f"{self.chain} log query failed: {last_err}")
            out.extend(got)
            cur = hi + 1
        return out

    def head(self) -> tuple[int, int]:
        blk = self.call("eth_getBlockByNumber", ["latest", False])
        return int(blk["number"], 16), int(blk["timestamp"], 16)

    def block_ts(self, number: int) -> int:
        return int(self.call("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)

    def block_at(self, ts: int, head_no: int, head_ts: int, block_time: float) -> int:
        """Block at (or just before) a timestamp: estimate from block time, refine twice."""
        guess = max(0, head_no - int((head_ts - ts) / block_time))
        for _ in range(3):
            gts = self.block_ts(guess)
            delta = int((ts - gts) / block_time)
            if delta == 0:
                break
            guess = max(0, min(head_no, guess + delta))
        return guess

    def has_code(self, address: str, block: int) -> bool:
        code = self.call("eth_getCode", [address, hex(block)])
        return isinstance(code, str) and len(code) > 2

    def creation_block(self, address: str, lo: int, hi: int) -> int:
        """First block where the contract has code; requires no code at lo and code at hi."""
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.has_code(address, mid):
                hi = mid
            else:
                lo = mid
        return hi

    def eth_call(self, to: str, data: str) -> str | None:
        try:
            res = self.call("eth_call", [{"to": to, "data": data}, "latest"])
        except DiscoveryError:
            return None
        return res if isinstance(res, str) and len(res) > 2 else None


def _uint(word_hex: str | None) -> int | None:
    if not word_hex:
        return None
    try:
        return int(word_hex[:66], 16)
    except ValueError:
        return None


def _abi_string(res: str | None) -> str | None:
    if not res:
        return None
    try:
        raw = bytes.fromhex(res[2:])
        if len(raw) >= 64:
            off = int.from_bytes(raw[0:32], "big")
            ln = int.from_bytes(raw[off:off + 32], "big")
            s = raw[off + 32:off + 32 + ln].decode("utf-8", "replace")
        else:
            s = raw.rstrip(b"\0").decode("utf-8", "replace")
        s = s.strip().replace("\x00", "")
        return s[:200] or None
    except (ValueError, IndexError):
        return None


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_seadrop_mint(log: dict) -> dict | None:
    topics = log.get("topics") or []
    data = (log.get("data") or "0x")[2:]
    if len(topics) != 4 or topics[0].lower() != SEADROP_MINT or len(data) < 64 * 5:
        return None
    words = [data[i:i + 64] for i in range(0, 64 * 5, 64)]
    return {
        "nft": _addr(topics[1]), "minter": _addr(topics[2]), "payer": "0x" + words[0][-40:].lower(),
        "quantity": int(words[1], 16), "unit_price_wei": int(words[2], 16),
        "block": int(log["blockNumber"], 16), "tx": log.get("transactionHash"),
    }


def transfer_metrics(logs: list[dict]) -> dict:
    """Holders, concentration, secondary transfers and wash patterns from ERC-721 Transfer logs."""
    owner: dict[int, str] = {}
    history: dict[int, list[tuple[str, str]]] = defaultdict(list)
    pair_counts: Counter = Counter()
    pair_dirs: dict[tuple, set] = defaultdict(set)
    minted = burned = secondary = 0
    logs = sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
    for l in logs:
        t = l.get("topics") or []
        if len(t) != 4 or t[0].lower() != TRANSFER:
            continue  # not ERC-721
        frm, to, tid = _addr(t[1]), _addr(t[2]), int(t[3], 16)
        if frm == ZERO:
            minted += 1
        else:
            secondary += 1
            pair = tuple(sorted((frm, to)))
            pair_counts[pair] += 1
            pair_dirs[pair].add((frm, to))
            history[tid].append((frm, to))
        if to in BURN:
            burned += 1
            owner.pop(tid, None)
        else:
            owner[tid] = to
    holders = Counter(owner.values())
    held = sum(holders.values())
    top_wallet, top_count = (holders.most_common(1)[0] if holders else (None, 0))
    round_trips = sum(1 for moves in history.values()
                      for i, (a, b) in enumerate(moves) for (c, d) in moves[i + 1:] if c == b and d == a)
    # Only pairs that send items in BOTH directions count (one-way batches are gifts or consolidation).
    repeated_pair_transfers = sum(n for p, n in pair_counts.items() if n >= 3 and len(pair_dirs[p]) == 2)
    return {
        "minted_onchain": minted, "burned": burned, "held_items": held, "unique_holders": len(holders),
        "top_holder": top_wallet, "top_holder_share": (top_count / held) if held else None,
        "secondary_transfers": secondary, "round_trips": round_trips,
        "repeated_pair_transfers": repeated_pair_transfers,
    }


def _log_score(x: float | None, full: float) -> float | None:
    """0..100 on a log scale; reaches 100 at `full`."""
    if x is None:
        return None
    return round(min(1.0, math.log10(1 + max(0.0, x)) / math.log10(1 + full)) * 100, 1)


def score_new_collection(m: dict) -> dict:
    """Early-momentum score for a NEW collection (0-100) with visible components and penalties."""
    comps = []
    age_days = max(m["age_hours"] / 24, 1 / 24)
    # Momentum: how many distinct wallets minted, and how fast (per day since launch).
    momentum = 0.6 * _log_score(m["unique_minters"], 2000) + 0.4 * _log_score(m["unique_minters"] / age_days, 1000)
    comps.append(("momentum", 30, round(momentum, 1),
                  f"unique_minters={m['unique_minters']}, per_day={m['unique_minters'] / age_days:.1f}"))
    # Demand: paid mint revenue and mint progress toward max supply.
    parts, basis = [], []
    rev = m["mint_revenue_native"]
    parts.append(_log_score(rev, 50))
    basis.append(f"mint_revenue={rev:.4f} {m['native']}")
    if m.get("mint_progress") is not None:
        parts.append(round(min(1.0, m["mint_progress"]) * 100, 1))
        basis.append(f"mint_progress={m['mint_progress']:.0%}")
    comps.append(("demand", 20, round(sum(parts) / len(parts), 1), ", ".join(basis)))
    # Distribution: holders spread across wallets.
    th = m.get("top_holder_share")
    if th is not None and m.get("unique_holders"):
        spread = (1 - th) * 70 + _log_score(m["unique_holders"], 2000) * 0.30
        comps.append(("distribution", 25, round(spread, 1),
                      f"unique_holders={m['unique_holders']}, top_holder_share={th:.0%}"))
    else:
        comps.append(("distribution", 25, None, "no holder data"))
    # Trust: verifiable facts about the contract and team.
    t, tb = 50.0, []
    if m.get("contract_verified") is True:
        t += 30; tb.append("source verified")
    elif m.get("contract_verified") is False:
        t -= 30; tb.append("source NOT verified")
    else:
        tb.append("verification unknown")
    if m.get("explorer_scam_label"):
        t = 0; tb.append("explorer scam label")
    out = m.get("outside_drop_share")
    if out is not None and out > 0.3:
        t -= 20; tb.append(f"{out:.0%} minted outside the public drop")
    if m.get("socials") is not None:
        t += 20 if m["socials"] else -20
        tb.append(f"socials={'yes' if m['socials'] else 'none'}")
    comps.append(("trust", 25, round(max(0.0, min(100.0, t)), 1), ", ".join(tb)))

    scored = [c for c in comps if c[2] is not None]
    base = sum(c[2] * c[1] for c in scored) / sum(c[1] for c in scored)
    penalty = 0
    for f in m["flags"]:
        penalty += {"high": 15, "warning": 5}.get(f["severity"], 0)
    value = round(max(0.0, base - penalty), 1)
    return {
        "value": value, "max": 100, "before_penalties": round(base, 1), "red_flag_penalty": penalty,
        "coverage": round(sum(c[1] for c in scored) / 100, 2),
        "components": [{"name": n, "weight": w, "score": s, "status": "unknown" if s is None else "scored", "basis": b}
                       for n, w, s, b in comps],
        "note": "Early-momentum score for new launches (hypothesis weights). Not a prediction of price or profit.",
    }


def red_flags(m: dict) -> list[dict]:
    flags = []

    def add(code, sev, msg, ev):
        flags.append({"code": code, "severity": sev, "message": msg, "evidence": ev})

    th = m.get("top_holder_share")
    if th is not None and m.get("held_items", 0) >= 10:
        if th >= 0.5:
            add("single_wallet_concentration", "high", "One wallet holds at least half of the items.",
                {"top_holder": m.get("top_holder"), "share": round(th, 3)})
        elif th >= 0.2:
            add("single_wallet_concentration", "warning", "One wallet holds at least 20% of the items.",
                {"top_holder": m.get("top_holder"), "share": round(th, 3)})
    tm = m.get("top_minter_share")
    if tm is not None and m["items_minted_via_drop"] >= 10 and tm >= 0.5:
        add("single_wallet_minting", "high", "One wallet minted at least half of the public-drop items.",
            {"top_minter": m.get("top_minter"), "share": round(tm, 3)})
    if m["unique_minters"] < 5:
        add("very_few_minters", "warning", "Fewer than 5 distinct wallets minted.", {"unique_minters": m["unique_minters"]})
    if m.get("round_trips", 0) > 0 or m.get("repeated_pair_transfers", 0) >= 3:
        add("wash_trading_suspected", "high" if m.get("round_trips", 0) >= 3 else "warning",
            "Items moved back and forth between the same wallets (heuristic; transfers are not proven sales).",
            {"round_trips": m.get("round_trips"), "repeated_pair_transfers": m.get("repeated_pair_transfers")})
    if m.get("contract_verified") is False:
        add("contract_source_unverified", "warning", "Contract source is not verified on the block explorer.", {})
    if m.get("explorer_scam_label"):
        add("explorer_scam_label", "high", "The block explorer labels this contract as a scam.", {})
    if m.get("creator") and m.get("top_minter") and m["creator"] == m["top_minter"]:
        add("creator_is_top_minter", "warning", "The deployer wallet is also the largest minter.", {"creator": m["creator"]})
    out = m.get("outside_drop_share")
    if out is not None and out > 0.3:
        add("large_mint_outside_drop", "warning",
            "A large share of items was minted outside the public drop (team reserve or airdrop).",
            {"share": round(out, 3)})
    if m.get("socials") is False:
        add("no_socials", "warning", "No website, X/Twitter or Discord published on OpenSea.", {})
    elif m.get("socials") is None:
        add("socials_unknown", "info", "Social links not checked (needs OPENSEA_API_KEY).", {})
    if m["age_hours"] < 2:
        add("very_new", "info", "Launched less than 2 hours ago; metrics are volatile.", {"age_hours": round(m["age_hours"], 2)})
    return flags


def _blockscout(base: str | None, address: str, timeout: float) -> dict:
    if not base:
        return {}
    try:
        r = requests.get(f"{base}/addresses/{address}", timeout=timeout, headers={"accept": "application/json"})
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            return {}
        d = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {"contract_verified": d.get("is_verified") if isinstance(d.get("is_verified"), bool) else None,
            "explorer_scam_label": bool(d.get("is_scam")),
            "creator": (d.get("creator_address_hash") or "").lower() or None}


class Discoverer:
    def __init__(self, timeout: float = 20.0, opensea_enricher=None, rpc_factory=None, blockscout_fn=None,
                 max_candidates: int = 25, workers: int = 6):
        self.timeout = timeout
        self.enrich = opensea_enricher
        self.rpc_factory = rpc_factory or (lambda chain: RPC(chain, timeout))
        self.blockscout = blockscout_fn or _blockscout
        self.max_candidates = max_candidates
        self.workers = workers
        self._cache: dict = {}
        self._lock = threading.Lock()

    def discover(self, period: str, chains: list[str], limit: int, now: float | None = None) -> dict:
        key = (period, tuple(chains), limit)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL[period]:
                return {**hit[1], "cached": True}
        started = time.time()
        now = int(now or time.time())
        window_start = now - PERIODS[period]
        results, scan, calls = [], [], 0
        for chain in chains:
            res, info, n = self._discover_chain(chain, window_start, now)
            results.extend(res)
            scan.append(info)
            calls += n
        results.sort(key=lambda r: (r["score"]["value"], r["metrics"]["unique_minters"]), reverse=True)
        for i, r in enumerate(results, 1):
            r["rank"] = i
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
        out = {
            "period": period, "window_start": iso(window_start), "window_end": iso(now),
            "chains": chains, "new_collections_found": len(results), "returned": min(limit, len(results)),
            "results": results[:limit], "scan": scan, "rpc_calls": calls,
            "elapsed_seconds": round(time.time() - started, 1),
            "sources": ["OpenSea SeaDrop mint events (public RPC)", "eth_getCode / eth_call / ERC-721 Transfer logs",
                        "Blockscout (Ethereum)"] + (["OpenSea API v2"] if self.enrich else []),
            "limitations": LIMITATIONS,
            "disclaimer": "Informational analysis of public onchain data. Not financial advice; does not predict "
                          "or guarantee price, profit or resale value.",
            "cached": False,
        }
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    def _discover_chain(self, chain: str, window_start: int, now: int):
        cfg = CHAIN_CONFIG[chain]
        rpc = self.rpc_factory(chain)
        head_no, head_ts = rpc.head()
        start_block = rpc.block_at(window_start, head_no, head_ts, cfg["block_time"])
        logs = rpc.get_logs(SEADROP, [SEADROP_MINT], start_block, head_no)
        mints = [m for m in (decode_seadrop_mint(l) for l in logs) if m]
        by_nft: dict[str, list[dict]] = defaultdict(list)
        for m in mints:
            by_nft[m["nft"]].append(m)
        # Rank candidates by distinct minters, then check the busiest ones for newness.
        ranked = sorted(by_nft, key=lambda a: len({x["minter"] for x in by_nft[a]}), reverse=True)
        candidates = ranked[: self.max_candidates]

        errors = []

        def check(addr):
            try:
                if rpc.has_code(addr, start_block):
                    return None  # deployed before the window: not new
                return self._analyze_new(rpc, chain, addr, by_nft[addr], start_block, head_no, head_ts)
            except (DiscoveryError, requests.RequestException, KeyError, ValueError, TypeError) as exc:
                errors.append(f"{addr}: {str(exc)[:120]}")
                return None

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            res = [r for r in ex.map(check, candidates) if r]
        info = {"chain": chain, "from_block": start_block, "to_block": head_no, "seadrop_mint_events": len(mints),
                "collections_minting_in_window": len(by_nft), "candidates_checked": len(candidates),
                "new_collections": len(res), "candidate_errors": len(errors),
                "note": None if len(by_nft) <= len(candidates) else
                f"only the {len(candidates)} collections with the most distinct minters were checked for newness"}
        return res, info, rpc.calls

    def _analyze_new(self, rpc: RPC, chain: str, addr: str, drop_mints: list[dict], start_block: int,
                     head_no: int, head_ts: int) -> dict:
        cfg = CHAIN_CONFIG[chain]
        created = rpc.creation_block(addr, start_block, head_no)
        created_ts = rpc.block_ts(created)
        transfers = rpc.get_logs(addr, [TRANSFER], created, head_no)
        tm = transfer_metrics(transfers)
        name = _abi_string(rpc.eth_call(addr, SEL["name"]))
        symbol = _abi_string(rpc.eth_call(addr, SEL["symbol"]))
        total_supply = _uint(rpc.eth_call(addr, SEL["totalSupply"]))
        max_supply = _uint(rpc.eth_call(addr, SEL["maxSupply"]))
        if max_supply is not None and (max_supply == 0 or max_supply >= 2 ** 64):
            max_supply = None  # 0 or uint max = no cap
        qty = Counter()
        for m in drop_mints:
            qty[m["minter"]] += m["quantity"]
        drop_items = sum(qty.values())
        top_minter, top_q = qty.most_common(1)[0] if qty else (None, 0)
        revenue = sum(m["quantity"] * m["unit_price_wei"] for m in drop_mints) / 1e18
        prices = sorted({m["unit_price_wei"] / 1e18 for m in drop_mints})
        last24 = sum(m["quantity"] for m in drop_mints if m["block"] >= head_no - int(86400 / cfg["block_time"]))
        minted_total = total_supply if total_supply is not None else tm["minted_onchain"]
        m = {
            "native": cfg["native"], "age_hours": max(0.0, (head_ts - created_ts) / 3600),
            "unique_minters": len(qty), "unique_payers": len({x["payer"] for x in drop_mints}),
            "items_minted_via_drop": drop_items, "items_minted_last_24h": last24,
            "mint_txs": len({x["tx"] for x in drop_mints}), "mint_revenue_native": round(revenue, 6),
            "mint_price_min": prices[0] if prices else None, "mint_price_max": prices[-1] if prices else None,
            "top_minter": top_minter, "top_minter_share": (top_q / drop_items) if drop_items else None,
            "total_supply": total_supply, "max_supply": max_supply,
            "mint_progress": (minted_total / max_supply) if max_supply and minted_total is not None else None,
            "outside_drop_share": (max(0, minted_total - drop_items) / minted_total) if minted_total else None,
            **tm,
        }
        m.update(self.blockscout(cfg["blockscout"], addr, self.timeout))
        m.setdefault("contract_verified", None)
        m.setdefault("explorer_scam_label", False)
        m.setdefault("creator", None)
        opensea = None
        m["socials"] = None
        if self.enrich:
            opensea = self.enrich(chain, addr)
            if opensea:
                m["socials"] = opensea.get("socials")
        flags = red_flags(m)
        m["flags"] = flags
        score = score_new_collection(m)
        m.pop("flags")
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
        return {
            "contract_address": addr, "chain": chain, "name": name, "symbol": symbol,
            "created_block": created, "created_at": iso(created_ts),
            "first_drop_mint_at_block": min(x["block"] for x in drop_mints),
            "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
            "red_flags": flags, "score": score, "opensea": opensea,
            "sources": [cfg["explorer"] + addr] + ([f"https://eth.blockscout.com/address/{addr}"]
                                                     if cfg["blockscout"] else []),
            "status_note": "Onchain values are read directly from the chain; OpenSea values are provider_reported.",
        }


def free_view(result: dict, left: int, per_month: int, price: dict) -> dict:
    """Free tier: ranking with names, contracts and scores only; metrics and red flags are paid."""
    rows = []
    for r in result.get("results", []):
        m = r.get("metrics", {})
        flags = [f for f in r.get("red_flags", []) if f.get("severity") != "info"]
        rows.append({
            "rank": r.get("rank"), "name": r.get("name"), "chain": r.get("chain"),
            "contract_address": r.get("contract_address"), "created_at": r.get("created_at"),
            "score": r.get("score", {}).get("value"),
            "summary": f"{m.get('unique_minters')} unique minters, about {round(m.get('age_hours') or 0)} h old, "
                       f"{len(flags)} red flag(s).",
        })
    return {
        "tier": "free", "period": result.get("period"), "window_start": result.get("window_start"),
        "window_end": result.get("window_end"), "chains": result.get("chains"),
        "new_collections_found": result.get("new_collections_found"), "returned": len(rows), "results": rows,
        "free_analyses_left": left, "free_analyses_per_month": per_month, "full_report": price,
        "note": "Free summary. The full result adds every mint/holder metric, red flags with evidence, "
                "score components and sources.",
        "disclaimer": "MintScope is an analysis and risk-research tool. It is not financial advice and does not "
                      "guarantee profit, price, or that a project is or is not a scam.",
        "cached": result.get("cached"),
    }
