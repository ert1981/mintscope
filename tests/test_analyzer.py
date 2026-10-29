import analyzer


def raw(**fields):
    base = {k: None for k in ["name", "mint_price", "floor_price", "total_sales", "total_supply", "sales_7d",
                              "num_owners", "average_sale_price", "contract_verified", "safelist_status",
                              "website", "twitter", "discord", "description", "creator_address"]}
    base.update(fields)
    return {"provider": "opensea", "fields": base, "sources": []}


def test_weights_sum_to_100():
    assert sum(analyzer.WEIGHTS.values()) == 100


def test_missing_data_does_not_raise_score():
    rich = analyzer.build_report(raw(total_sales=10, total_supply=1000, sales_7d=0), "x1", "slug", "ethereum", "live")
    assert rich["score"]["coverage"] < 0.5
    assert rich["confidence"]["level"] == "low"


def test_floor_is_not_treated_as_sale():
    rep = analyzer.build_report(raw(floor_price=5.0), "x1", "slug", "ethereum", "live")
    liq = next(c for c in rep["score"]["components"] if c["name"] == "market_liquidity")
    assert liq["score"] is None


def test_risk_flags():
    rep = analyzer.build_report(
        raw(mint_price=0.1, floor_price=0.05, average_sale_price=0.08, total_sales=50, sales_7d=1,
            num_owners=100, total_supply=1000, contract_verified=False, safelist_status="not_requested"),
        "x1", "slug", "ethereum", "live")
    codes = {f["code"]: f for f in rep["risk_flags"]}
    assert {"floor_below_mint", "avg_sale_below_mint", "low_recent_liquidity", "owner_concentration",
            "contract_source_unverified", "not_marketplace_verified", "no_official_links"} <= set(codes)
    assert codes["owner_concentration"]["severity"] == "high"
    assert codes["not_marketplace_verified"]["severity"] == "info"


def test_well_documented_project_scores_high_confidence():
    rep = analyzer.build_report(
        raw(name="N", mint_price=0.01, floor_price=0.05, average_sale_price=0.04, total_sales=900, sales_7d=30,
            num_owners=600, total_supply=1000, contract_verified=True, safelist_status="verified",
            website="https://a", twitter="a", discord="https://d", description="d", creator_address="0xabc"),
        "x1", "slug", "ethereum", "live")
    assert rep["score"]["coverage"] == 0.8  # artwork is never auto-scored
    assert rep["confidence"]["level"] == "high"
    assert rep["score"]["value"] > 80
    assert rep["risk_flags"] == []
