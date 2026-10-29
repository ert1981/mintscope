"""Keyless collection analysis from free public sources (no API key needed).

Used first by the local app, by Featured Collections and by user submissions:
- public JSON-RPC nodes (contract code, ERC-165 interfaces, name/symbol/totalSupply, recent Transfer logs),
- Blockscout (Ethereum): source verification, scam label, deployer, holder count.

The score here is a SAFETY score: higher = fewer risks detected in the available evidence. It is computed only
from the contract's onchain/explorer data. It has no input for featured, sponsored or paid status, so being
featured or sponsored can never change it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urlparse

import requests

from discovery import (CHAIN_CONFIG, RPC, TRANSFER, ZERO, DiscoveryError, SEL, _abi_string, _addr, _uint,
                       transfer_metrics)

KEYLESS_CHAINS = tuple(CHAIN_CONFIG)  # ethereum, base
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,99}$")
IFACE = {"erc721": "0x80ac58cd", "erc1155": "0xd9b67a26"}
WINDOW_DAYS = 7
OPENSEA_CHAINS = {"ethereum": "ethereum", "base": "base", "matic": "polygon", "polygon": "polygon",
                  "arbitrum": "arbitrum", "optimism": "optimism", "zora": "zora"}
EXPLORERS = {"etherscan.io": "ethereum", "basescan.org": "base", "eth.blockscout.com": "ethereum"}
LIMITATIONS = [
    "Keyless analysis covers Ethereum and Base contracts; market prices and sales need an OpenSea API key.",
    f"Transfer activity covers the last {WINDOW_DAYS} days only; transfers are not sales.",
    "Source verification, scam labels and holder counts come from Blockscout on Ethereum; on Base they are unknown.",
    "Wash-trading detection is a transfer-pattern heuristic, not proof.",
    "The safety score is a hypothesis-weighted summary of evidence, not financial advice or a scam guarantee.",
]


class InputError(ValueError):
    pass


class NotAContract(InputError):
    pass


def parse_input(text: str, default_chain: str = "ethereum") -> dict:
    """Contract address, OpenSea link, explorer link or OpenSea slug -> {kind, chain, address|slug}."""
    raw = (text or "").strip()
    if not raw or len(raw) > 300:
        raise InputError("Enter a contract address, an OpenSea link or an OpenSea collection slug.")
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", raw):
        return {"kind": "contract", "chain": default_chain, "address": raw.lower()}
    if "://" in raw or raw.startswith(("opensea.io", "www.")):
        url = urlparse(raw if "://" in raw else "https://" + raw)
        host = (url.hostname or "").lower().removeprefix("www.")
        parts = [p for p in url.path.split("/") if p]
        if host in ("opensea.io", "pro.opensea.io"):
            if len(parts) >= 3 and parts[0] in ("item", "assets"):
                chain = OPENSEA_CHAINS.get(parts[1].lower())
                if chain and ADDRESS_RE.fullmatch(parts[2]):
                    return {"kind": "contract", "chain": chain, "address": parts[2].lower()}
            if len(parts) >= 2 and parts[0] == "collection" and SLUG_RE.match(parts[1].lower()):
                return {"kind": "slug", "chain": default_chain, "slug": parts[1].lower()}
        if host in EXPLORERS:
            m = ADDRESS_RE.search(url.path)
            if m:
                return {"kind": "contract", "chain": EXPLORERS[host], "address": m.group(0).lower()}
        raise InputError("Unsupported link. Use an OpenSea collection/item link, an Etherscan/Basescan link "
                         "or a contract address.")
    if SLUG_RE.match(raw.lower()):
        return {"kind": "slug", "chain": default_chain, "slug": raw.lower()}
    raise InputError("Not a contract address, link or slug.")


def blockscout_info(chain: str, address: str, timeout: float = 15.0) -> dict:
    base = CHAIN_CONFIG[chain]["blockscout"]
    if not base:
        return {}
    out = {}
    try:
        r = requests.get(f"{base}/addresses/{address}", timeout=timeout, headers={"accept": "application/json"})
        if r.status_code == 200:
            d = r.json()
            out.update({"contract_verified": d.get("is_verified") if isinstance(d.get("is_verified"), bool) else None,
                        "explorer_scam_label": bool(d.get("is_scam")),
                        "creator": (d.get("creator_address_hash") or "").lower() or None})
        r = requests.get(f"{base}/tokens/{address}", timeout=timeout, headers={"accept": "application/json"})
        if r.status_code == 200:
            d = r.json()
            holders = d.get("holders_count") or d.get("holders")
            out["holders"] = int(holders) if str(holders or "").isdigit() else None
            out["explorer_reputation"] = d.get("reputation")
            out["explorer_token_type"] = d.get("type")
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return out


def _supports(rpc: RPC, address: str, iface: str) -> bool | None:
    res = rpc.eth_call(address, "0x01ffc9a7" + iface[2:].ljust(64, "0"))
    if res is None:
        return None
    return _uint(res) == 1


def wash_patterns(logs: list[dict], is_contract) -> dict:
    """Round trips (A->B then B->A of the same item) and repeated two-way pairs between WALLETS.
    Pairs involving a contract (marketplace escrow, lending, staking, vaults) are excluded, because items
    legitimately go into and come back out of those. Only the most frequent counterparties are checked."""
    from collections import defaultdict
    history = defaultdict(list)
    pair_counts, pair_dirs = Counter(), defaultdict(set)
    logs = sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
    for l in logs:
        t = l.get("topics") or []
        if len(t) != 4:
            continue
        frm, to, tid = _addr(t[1]), _addr(t[2]), int(t[3], 16)
        if frm == ZERO:
            continue
        history[tid].append((frm, to))
        pair = tuple(sorted((frm, to)))
        pair_counts[pair] += 1
        pair_dirs[pair].add((frm, to))
    trips = [(a, b) for moves in history.values() for i, (a, b) in enumerate(moves)
             for (c, d) in moves[i + 1:] if c == b and d == a]
    two_way = {p: n for p, n in pair_counts.items() if n >= 3 and len(pair_dirs[p]) == 2}
    involved = Counter([x for p in trips for x in p] + [x for p in two_way for x in p])
    contracts = {a for a, _ in involved.most_common(15) if is_contract(a)}
    trips = [p for p in trips if not (set(p) & contracts)]
    two_way = {p: n for p, n in two_way.items() if not (set(p) & contracts)}
    return {"round_trips": len(trips), "repeated_pair_transfers": sum(two_way.values()),
            "excluded_contract_counterparties": len(contracts)}


def _log(x: float | None, full: float) -> float | None:
    if x is None:
        return None
    return round(min(1.0, math.log10(1 + max(0.0, x)) / math.log10(1 + full)) * 100, 1)


def wash_rate(m: dict) -> float:
    """Share of secondary transfers that look like wash patterns between wallets."""
    secondary = m.get("secondary_transfers_7d") or 0
    if not secondary:
        return 0.0
    return min(1.0, (m.get("round_trips", 0) * 2 + m.get("repeated_pair_transfers", 0) / 2) / secondary)


def safety_flags(m: dict) -> list[dict]:
    flags = []

    def add(code, sev, msg, ev=None):
        flags.append({"code": code, "severity": sev, "message": msg, "evidence": ev or {}})

    if m.get("explorer_scam_label"):
        add("explorer_scam_label", "high", "The block explorer labels this contract as a scam.")
    if m.get("standard") == "unknown":
        add("not_nft_standard", "high", "The contract does not report ERC-721 or ERC-1155 support (ERC-165).")
    if m.get("contract_verified") is False:
        add("contract_source_unverified", "warning", "Contract source is not verified on the block explorer.")
    if m.get("age_hours") is not None and m["age_hours"] < 24:
        add("very_new_contract", "warning", "Contract deployed less than 24 hours ago.",
            {"age_hours": round(m["age_hours"], 2)})
    if m.get("holders") is not None and m["holders"] < 10:
        add("very_few_holders", "warning", "Fewer than 10 holders.", {"holders": m["holders"]})
    rate = wash_rate(m)
    if rate >= 0.05 and (m.get("round_trips", 0) > 0 or m.get("repeated_pair_transfers", 0) >= 3):
        add("wash_trading_suspected", "high" if rate >= 0.15 and m.get("round_trips", 0) >= 3 else "warning",
            "Items moved back and forth between the same wallets in the last 7 days (heuristic, contracts "
            "such as escrow and lending excluded).",
            {"round_trips": m.get("round_trips"), "repeated_pair_transfers": m.get("repeated_pair_transfers"),
             "share_of_secondary_transfers": round(rate, 3)})
    share = m.get("top_receiver_share_7d")
    if share is not None and m.get("transfers_7d", 0) >= 10 and share >= 0.5:
        add("transfer_concentration", "warning", "One wallet received at least half of the items moved in 7 days.",
            {"wallet": m.get("top_receiver_7d"), "share": round(share, 3)})
    if m.get("transfers_7d") == 0:
        add("no_recent_activity", "info", "No transfers in the last 7 days.")
    return flags


def safety_score(m: dict, flags: list[dict]) -> dict:
    comps = []
    t, tb = 50.0, []
    if m.get("contract_verified") is True:
        t += 30; tb.append("source verified")
    elif m.get("contract_verified") is False:
        t -= 30; tb.append("source NOT verified")
    else:
        tb.append("verification unknown")
    if m.get("standard") in ("erc721", "erc1155"):
        t += 10; tb.append(m["standard"])
    elif m.get("standard") == "unknown":
        t -= 30; tb.append("no NFT interface")
    if m.get("explorer_scam_label"):
        t = 0; tb.append("explorer scam label")
    comps.append(("contract_trust", 35, round(max(0.0, min(100.0, t)), 1), ", ".join(tb)))
    holders, supply = m.get("holders"), m.get("total_supply")
    if holders is not None:
        parts = [_log(holders, 5000)]
        basis = f"holders={holders}"
        if supply and m.get("standard") == "erc721":
            parts.append(round(min(1.0, holders / supply / 0.5) * 100, 1))
            basis += f", holders/supply={holders / supply:.0%}"
        comps.append(("holder_distribution", 25, round(sum(parts) / len(parts), 1), basis))
    else:
        comps.append(("holder_distribution", 25, None, "holder count unknown"))
    if m.get("unique_wallets_7d") is not None:
        comps.append(("activity", 20, _log(m["unique_wallets_7d"], 500), f"wallets_7d={m['unique_wallets_7d']}"))
    else:
        comps.append(("activity", 20, None, "activity unknown"))
    rate = wash_rate(m)
    comps.append(("wash_pattern", 20, round(max(0.0, 100 - rate * 400), 1),
                  f"round_trips={m.get('round_trips', 0)}, repeated_pairs={m.get('repeated_pair_transfers', 0)}, "
                  f"wash_share={rate:.0%}"))
    scored = [c for c in comps if c[2] is not None]
    base = sum(c[2] * c[1] for c in scored) / sum(c[1] for c in scored)
    penalty = sum({"high": 15, "warning": 5}.get(f["severity"], 0) for f in flags)
    return {
        "value": round(max(0.0, base - penalty), 1), "max": 100, "before_penalties": round(base, 1),
        "red_flag_penalty": penalty, "coverage": round(sum(c[1] for c in scored) / 100, 2),
        "components": [{"name": n, "weight": wt, "score": s, "status": "unknown" if s is None else "scored",
                        "basis": b} for n, wt, s, b in comps],
        "meaning": "Safety score: higher = fewer risks detected in the available evidence.",
        "note": "Hypothesis weights over public data. Not financial advice; not a prediction of price or profit; "
                "not a guarantee that a project is or is not a scam.",
    }


def _safe_has_code(rpc: RPC, address: str, block: int) -> bool:
    try:
        return rpc.has_code(address, block)
    except DiscoveryError:
        return False


def analyze_contract(chain: str, address: str, rpc: RPC | None = None, explorer=blockscout_info,
                     timeout: float = 20.0) -> dict:
    """Keyless onchain analysis of one NFT contract. Deterministic for the same chain data."""
    if chain not in KEYLESS_CHAINS:
        raise InputError(f"Keyless analysis supports {', '.join(KEYLESS_CHAINS)}; '{chain}' needs an OpenSea key.")
    if not ADDRESS_RE.fullmatch(address or ""):
        raise InputError("Invalid contract address.")
    address = address.lower()
    cfg = CHAIN_CONFIG[chain]
    rpc = rpc or RPC(chain, timeout)
    head_no, head_ts = rpc.head()
    if not rpc.has_code(address, head_no):
        raise NotAContract("No contract code at this address on " + chain + " (it is a wallet or wrong chain).")
    is721, is1155 = _supports(rpc, address, IFACE["erc721"]), _supports(rpc, address, IFACE["erc1155"])
    standard = "erc721" if is721 else "erc1155" if is1155 else "unknown"
    name = _abi_string(rpc.eth_call(address, SEL["name"]))
    symbol = _abi_string(rpc.eth_call(address, SEL["symbol"]))
    total_supply = _uint(rpc.eth_call(address, SEL["totalSupply"]))
    if total_supply is not None and total_supply >= 2 ** 64:
        total_supply = None
    start = rpc.block_at(head_ts - WINDOW_DAYS * 86400, head_no, head_ts, cfg["block_time"])
    age_hours, created_block = None, None
    try:
        if not rpc.has_code(address, start):
            created_block = rpc.creation_block(address, start, head_no)
            age_hours = max(0.0, (head_ts - rpc.block_ts(created_block)) / 3600)
    except DiscoveryError:
        pass
    logs = rpc.get_logs(address, [TRANSFER], start, head_no)
    tm = transfer_metrics(logs)
    receivers, wallets, n721 = Counter(), set(), 0
    for l in logs:
        t = l.get("topics") or []
        if len(t) == 4:
            n721 += 1
            frm, to = _addr(t[1]), _addr(t[2])
            receivers[to] += 1
            wallets.update(w for w in (frm, to) if w != ZERO)
    top = receivers.most_common(1)[0] if receivers else (None, 0)
    m = {
        "standard": standard, "total_supply": total_supply,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "older_than_window": age_hours is None,
        "transfers_7d": n721, "mints_7d": tm["minted_onchain"], "secondary_transfers_7d": tm["secondary_transfers"], "unique_wallets_7d": len(wallets),
        "top_receiver_7d": top[0], "top_receiver_share_7d": round(top[1] / n721, 4) if n721 else None,
        **wash_patterns(logs, lambda a: _safe_has_code(rpc, a, head_no)),
        "contract_verified": None, "explorer_scam_label": False, "creator": None, "holders": None,
    }
    m.update(explorer(chain, address) or {})
    flags = safety_flags(m)
    score = safety_score(m, flags)
    sources = [cfg["explorer"] + address]
    if cfg["blockscout"]:
        sources.append(f"https://eth.blockscout.com/address/{address}")
    return {
        "source": "onchain_keyless", "chain": chain, "contract_address": address, "name": name, "symbol": symbol,
        "created_block": created_block, "metrics": m, "red_flags": flags, "score": score, "sources": sources,
        "limitations": LIMITATIONS, "rpc_calls": rpc.calls,
    }
