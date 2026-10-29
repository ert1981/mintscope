"""Normalization, scoring and risk flags.

Rules this module follows:
- A missing fact is reported as "unknown"; it never earns points and never
  counts as evidence of fraud.
- Floor price is the lowest active listing (an ask), not a realized sale.
- Scoring weights are visible, testable hypotheses, not a profit model.
"""
from __future__ import annotations

from data_store import utc_now

SCHEMA_VERSION = "0.3"

# Initial hypothesis weights (sum = 100). See README "Scoring".
WEIGHTS = {
    "track_record": 25,
    "artwork": 20,
    "market_liquidity": 25,
    "technical_risk": 20,
    "transparency": 10,
}

DISCLAIMER = (
    "This report is an informational analysis of publicly available (or, in demo mode, synthetic) data. "
    "It does not predict, promise or guarantee profit, price, or resale value, and it is not financial, "
    "legal or security-audit advice. Verify every fact against the linked sources before acting."
)

DEMO_NOTICE = (
    "DEMO MODE: every value in this report is SYNTHETIC, generated locally to test the pipeline. "
    "It describes no real project and must not be used or sold as analysis."
)

LIMITATIONS = [
    "Artwork quality and originality are not assessed automatically in this version.",
    "Mint price and contract source-code verification are not available from the current provider unless stated.",
    "Risk flags are heuristics, not a security audit of the smart contract.",
    "Scoring weights are initial hypotheses that have not been validated against outcomes.",
    "Average sale price is derived as all-time volume / all-time sales, because the provider no longer reports it.",
    "Owners-to-supply is skipped for edition (multi-copy, e.g. ERC-1155) collections, where it is not meaningful.",
]

MARKET_FIELDS = {
    "mint_price": "Announced/observed mint price.",
    "floor_price": "Lowest active listing (ask). NOT a realized sale price. Unknown when nothing is listed.",
    "average_sale_price": "Average realized sale price over all recorded sales (volume / sales).",
    "total_sales": "Number of recorded sales.",
    "total_volume": "Total realized sales volume.",
    "sales_7d": "Recorded sales in the last 7 days.",
    "volume_7d": "Realized sales volume in the last 7 days.",
    "num_owners": "Unique holder wallets.",
    "total_supply": "Items in the collection.",
}

EVIDENCE_FIELDS = ["safelist_status", "creator_address", "website", "twitter", "discord", "contract_verified"]

SAFELIST_SCORES = {"verified": 85, "approved": 70, "requested": 40, "not_requested": 35, "disabled_top_trending": 20}


# Which currency each market value is denominated in (OpenSea reports floor and volume
# currencies separately; they can differ).
FLOOR_UNIT_FIELDS = {"floor_price"}
VOLUME_UNIT_FIELDS = {"total_volume", "average_sale_price", "volume_7d"}


def _field(value, mode: str, note: str | None = None, derivation: str | None = None,
           unit: str | None = None) -> dict:
    if value is None:
        status = "unknown"
    elif mode == "demo":
        status = "synthetic"
    else:
        status = "derived_from_provider" if derivation else "provider_reported"
    out = {"value": value, "status": status}
    if note:
        out["meaning"] = note
    if derivation and value is not None:
        out["derivation"] = derivation
    if unit and value is not None:
        out["unit"] = unit
    return out


def is_edition_collection(f: dict) -> bool:
    """True when items exist in several copies (e.g. ERC-1155 editions): supply > distinct items."""
    supply, unique = f.get("total_supply"), f.get("unique_item_count")
    return supply is not None and unique is not None and 0 < unique < supply


def _ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def score_components(f: dict) -> list[dict]:
    comps = []

    # Track record / credibility: only from verifiable evidence.
    safelist = f.get("safelist_status")
    if safelist in SAFELIST_SCORES:
        s = SAFELIST_SCORES[safelist]
        if f.get("creator_address"):
            s = min(100, s + 10)
        comps.append(("track_record", s, f"safelist_status={safelist}, creator_known={bool(f.get('creator_address'))}"))
    else:
        reason = "no verifiable creator/team evidence available" if safelist is None else "unrecognized marketplace status; not scored"
        comps.append(("track_record", None, reason))

    comps.append(("artwork", None, "not assessed automatically in this version"))

    # Market liquidity: realized sales activity, never the floor alone.
    sales_ratio = _ratio(f.get("total_sales"), f.get("total_supply"))
    if sales_ratio is not None or f.get("sales_7d") is not None:
        parts, basis = [], []
        if sales_ratio is not None:
            parts.append(min(sales_ratio, 1.0) * 100)
            basis.append(f"sales/supply={sales_ratio:.2f}")
        if f.get("sales_7d") is not None:
            parts.append(min(f["sales_7d"] / 20, 1.0) * 100)
            basis.append(f"sales_7d={f['sales_7d']}")
        s = sum(parts) / len(parts)
        mint, avg = f.get("mint_price"), f.get("average_sale_price")
        if mint is not None and avg is not None and avg < mint:
            s *= 0.7
            basis.append("avg realized sale below mint")
        comps.append(("market_liquidity", round(s, 1), ", ".join(basis)))
    else:
        comps.append(("market_liquidity", None, "no sales data available"))

    # Technical / contract risk (higher score = fewer detected risks).
    owner_ratio = None if is_edition_collection(f) else _ratio(f.get("num_owners"), f.get("total_supply"))
    if f.get("contract_verified") is not None or owner_ratio is not None:
        s, basis = 100.0, []
        if f.get("contract_verified") is False:
            s -= 40
            basis.append("contract source not verified")
        elif f.get("contract_verified") is True:
            basis.append("contract source verified")
        if owner_ratio is not None:
            basis.append(f"owners/supply={owner_ratio:.2f}")
            if owner_ratio < 0.2:
                s -= 40
            elif owner_ratio < 0.4:
                s -= 20
        comps.append(("technical_risk", max(s, 0.0), ", ".join(basis)))
    else:
        comps.append(("technical_risk", None, "no contract or holder data available"))

    # Transparency: how much the project itself publishes.
    published = [k for k in ("description", "website", "twitter", "discord") if f.get(k)]
    # An all-empty provider response cannot prove the project itself published nothing;
    # avoid converting missing provider fields into an unjustified zero score.
    if published:
        comps.append(("transparency", len(published) * 25.0, f"observed fields: {', '.join(published)}"))
    else:
        comps.append(("transparency", None, "no transparency fields available from provider"))

    return [
        {
            "name": name,
            "weight": WEIGHTS[name],
            "score": s,
            "status": "unknown" if s is None else "scored",
            "basis": basis,
        }
        for name, s, basis in comps
    ]


def risk_flags(f: dict) -> list[dict]:
    flags = []

    def add(code, severity, category, message, evidence):
        flags.append({"code": code, "severity": severity, "category": category, "message": message, "evidence": evidence})

    mint, floor, avg = f.get("mint_price"), f.get("floor_price"), f.get("average_sale_price")
    if mint is not None and floor is not None and floor < mint:
        add("floor_below_mint", "warning", "market",
            "Lowest listing is below mint price (this is an ask, not a realized sale).",
            {"floor_price": floor, "mint_price": mint})
    if mint is not None and avg is not None and avg < mint:
        add("avg_sale_below_mint", "warning", "market",
            "Average realized sale price is below mint price.", {"average_sale_price": avg, "mint_price": mint})
    if f.get("no_active_listing") is True:
        add("no_active_listing", "info", "market",
            "No item is currently listed for sale, so there is no floor price.", {})
    if f.get("total_sales") == 0:
        add("no_sales_recorded", "warning", "market", "No sales recorded; liquidity cannot be demonstrated.",
            {"total_sales": 0})
    elif f.get("sales_7d") is not None and f["sales_7d"] < 5:
        add("low_recent_liquidity", "warning", "market", "Fewer than 5 sales in the last 7 days.",
            {"sales_7d": f["sales_7d"]})

    owner_ratio = None if is_edition_collection(f) else _ratio(f.get("num_owners"), f.get("total_supply"))
    if owner_ratio is not None and owner_ratio < 0.4:
        add("owner_concentration", "high" if owner_ratio < 0.2 else "warning", "technical",
            "The holder-to-supply ratio is low; this is only a rough concentration proxy, not a top-holder analysis.",
            {"num_owners": f["num_owners"], "total_supply": f["total_supply"], "ratio": round(owner_ratio, 3)})
    if f.get("contract_verified") is False:
        add("contract_source_unverified", "warning", "technical",
            "Contract source code is not verified on a block explorer; its permissions cannot be reviewed.", {})
    if f.get("safelist_status") in ("not_requested", "requested"):
        add("not_marketplace_verified", "info", "credibility",
            "Collection is not verified by the marketplace. This alone is not evidence of wrongdoing.",
            {"safelist_status": f["safelist_status"]})
    if not any(f.get(k) for k in ("website", "twitter", "discord")):
        add("no_official_links", "info", "data",
            "No official links were found. Missing information is not evidence of fraud.", {})
    return flags


def confidence(coverage: float, missing_count: int, mode: str) -> dict:
    if coverage >= 0.75 and missing_count <= 3:
        level = "high"
    elif coverage >= 0.5:
        level = "medium"
    else:
        level = "low"
    explanation = (
        f"{round(coverage * 100)}% of the scoring weight is backed by available data; "
        f"{missing_count} field(s) are unknown. Unknown components are excluded from the score and lower confidence."
    )
    if mode == "demo":
        explanation = "DEMO: describes coverage of synthetic inputs only, not of a real project. " + explanation
    return {"level": level, "explanation": explanation}


def build_report(raw: dict, identifier: str, identifier_type: str, chain: str, mode: str) -> dict:
    if mode not in ("demo", "live"):
        raise ValueError("mode must be 'demo' or 'live'")
    f = raw["fields"]
    derived = f.get("_derived") or {}
    floor_unit, volume_unit = f.get("price_symbol"), f.get("volume_symbol")

    def unit_for(k):
        return floor_unit if k in FLOOR_UNIT_FIELDS else volume_unit if k in VOLUME_UNIT_FIELDS else None

    comps = score_components(f)
    scored = [c for c in comps if c["score"] is not None]
    coverage = sum(c["weight"] for c in scored) / 100
    value = round(sum(c["score"] * c["weight"] for c in scored) / sum(c["weight"] for c in scored), 1) if scored else None

    tracked = list(MARKET_FIELDS) + EVIDENCE_FIELDS + ["name", "contract_address"]
    missing = [{"field": k, "status": "unknown"} for k in tracked if f.get(k) is None]
    now = utc_now()

    report = {
        "report_id": None,
        "schema_version": SCHEMA_VERSION,
        "data_mode": mode,
        "project": {
            "identifier": identifier,
            "identifier_type": identifier_type,
            "chain": f.get("chain") or chain,
            "provider": raw["provider"],
            "collection_name": _field(f.get("name"), mode),
            "contract_address": _field(f.get("contract_address"), mode),
        },
        "market": {k: _field(f.get(k), mode, note, derived.get(k), unit_for(k)) for k, note in MARKET_FIELDS.items()},
        "price_unit": floor_unit,
        "volume_unit": volume_unit,
        "edition_collection": is_edition_collection(f),
        "creator_evidence": {k: _field(f.get(k), mode) for k in EVIDENCE_FIELDS},
        "risk_flags": risk_flags(f),
        "missing_data": missing,
        "score": {
            "value": value,
            "max": 100,
            "coverage": round(coverage, 2),
            "components": comps,
            "note": "Analytical score from hypothesis weights; not a prediction of price or profit.",
        },
        "confidence": confidence(coverage, len(missing), mode),
        "sources": raw.get("sources", []),
        "analyzed_at": now,
        "data_as_of": now,
        "limitations": LIMITATIONS,
        "disclaimer": DISCLAIMER,
    }
    if mode == "demo":
        report["demo_notice"] = DEMO_NOTICE
    return report


# ---- pricing views -----------------------------------------------------------
FREE_NOTE = ("Free summary: overall score and a short project summary only. The full report adds every "
             "metric with its source, all risk flags with evidence, weaknesses and missing data.")
PRICING_DISCLAIMER = ("MintScope is an analysis and risk-research tool. It is not financial advice and does not "
                      "guarantee profit, price, or that a project is or is not a scam.")


def _value(field):
    return field.get("value") if isinstance(field, dict) else field


def weaknesses(report: dict) -> list[dict]:
    """Plain list of what is weak or unknown in the report (paid tier)."""
    out = []
    for c in report.get("score", {}).get("components", []):
        if c.get("status") != "scored":
            out.append({"area": c["name"], "issue": "not scored: data unavailable", "basis": c.get("basis")})
        elif isinstance(c.get("score"), (int, float)) and c["score"] < 50:
            out.append({"area": c["name"], "issue": f"weak ({c['score']}/100)", "basis": c.get("basis")})
    for f in report.get("risk_flags", []):
        if f.get("severity") in ("high", "medium", "warning"):
            out.append({"area": f.get("category") or "risk", "issue": f.get("message"), "flag": f.get("code")})
    for m in report.get("missing_data", []):
        out.append({"area": "data", "issue": f"{m.get('field')} is {m.get('status', 'unknown')}"})
    return out


def full_view(report: dict) -> dict:
    return {**report, "weaknesses": weaknesses(report)}


def free_view(report: dict, left: int, per_month: int, price: dict) -> dict:
    project = report["project"]
    name = _value(project.get("collection_name")) or project["identifier"]
    flags = report.get("risk_flags", [])
    high = sum(1 for f in flags if f.get("severity") == "high")
    summary = (f"{name} on {project['chain']}: overall score {report['score']['value']}/100 "
               f"({report['confidence']['level']} confidence). {len(flags)} risk flag(s) found"
               f"{f', {high} high severity' if high else ''}; {len(report.get('missing_data', []))} data field(s) unknown.")
    out = {
        "tier": "free",
        "data_mode": report["data_mode"],
        "project": {"identifier": project["identifier"], "chain": project["chain"], "name": name},
        "overall_score": {"value": report["score"]["value"], "max": 100, "note": report["score"]["note"]},
        "summary": summary,
        "free_analyses_left": left,
        "free_analyses_per_month": per_month,
        "full_report": price,
        "note": FREE_NOTE,
        "analyzed_at": report.get("analyzed_at"),
        "disclaimer": PRICING_DISCLAIMER,
    }
    if report.get("demo_notice"):
        out["demo_notice"] = report["demo_notice"]
    return out
