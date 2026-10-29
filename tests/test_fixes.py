"""Regression tests for the v0.4 fixes (checked against the OpenSea API v2 schema and real responses)."""
import providers
from tests.test_acceptance import FakeResp

KEY = "test-key-should-never-leak-1234567890"


LIVE = {"OPENSEA_API_KEY": KEY}


def live_client(make_client, monkeypatch, collection=None, stats=None, contract=None, seen=None, **env):
    collection = collection if collection is not None else {"name": "Real", "total_supply": 100, "unique_item_count": 100}
    stats = stats if stats is not None else {"total": {}, "intervals": []}

    def fake_get(url, headers, timeout):
        if seen is not None:
            seen.append(url)
        if "/contract/" in url:
            return FakeResp(200, contract or {})
        if url.endswith("/stats"):
            return FakeResp(200, stats)
        return FakeResp(200, collection)
    monkeypatch.setattr(providers.requests, "get", fake_get)
    return make_client(**LIVE, **env)


def test_zero_floor_means_no_listing_not_a_price(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch, stats={"total": {"floor_price": 0, "floor_price_symbol": "",
                    "sales": 10, "volume": 1.0, "volume_symbol": "ETH"}, "intervals": []})
    rep = c.post("/analyze", json={"identifier": "some-collection"}).json()
    assert rep["market"]["floor_price"]["value"] is None
    assert rep["market"]["floor_price"]["status"] == "unknown"
    assert rep["price_unit"] is None
    codes = {f["code"] for f in rep["risk_flags"]}
    assert "no_active_listing" in codes and "floor_below_mint" not in codes


def test_average_sale_price_is_derived_and_labeled(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch, stats={"total": {"floor_price": 2.0, "floor_price_symbol": "ETH",
                    "sales": 4, "volume": 10.0, "volume_symbol": "ETH"}, "intervals": []})
    avg = c.post("/analyze", json={"identifier": "some-collection"}).json()["market"]["average_sale_price"]
    assert avg["value"] == 2.5
    assert avg["status"] == "derived_from_provider"
    assert "volume" in avg["derivation"] and avg["unit"] == "ETH"


def test_average_not_derived_without_sales_or_currency(make_client, monkeypatch):
    for total in ({"sales": 0, "volume": 0.0, "volume_symbol": "ETH"}, {"sales": 3, "volume": 9.0, "volume_symbol": ""}):
        c = live_client(make_client, monkeypatch, stats={"total": total, "intervals": []})
        avg = c.post("/analyze", json={"identifier": "some-collection"}).json()["market"]["average_sale_price"]
        assert avg == {"value": None, "status": "unknown", "meaning": avg["meaning"]}, total


def test_floor_and_volume_currencies_are_kept_separate(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch, stats={"total": {"floor_price": 5, "floor_price_symbol": "APE",
                    "sales": 2, "volume": 3000, "volume_symbol": "USDC"},
                    "intervals": [{"interval": "seven_day", "sales": 1, "volume": 1500, "volume_symbol": "USDC"}]})
    rep = c.post("/analyze", json={"identifier": "some-collection"}).json()
    assert rep["price_unit"] == "APE" and rep["volume_unit"] == "USDC"
    assert rep["market"]["floor_price"]["unit"] == "APE"
    assert rep["market"]["total_volume"]["unit"] == "USDC"
    assert rep["market"]["volume_7d"]["unit"] == "USDC"


def test_edition_collections_skip_owner_concentration(make_client, monkeypatch):
    # Shape of a real ERC-1155 edition collection (basepaint: 297k copies of far fewer items).
    c = live_client(make_client, monkeypatch, collection={"name": "Editions", "total_supply": 297323,
                    "unique_item_count": 800}, stats={"total": {"num_owners": 34972, "sales": 10}, "intervals": []})
    rep = c.post("/analyze", json={"identifier": "some-editions"}).json()
    assert rep["edition_collection"] is True
    assert "owner_concentration" not in {f["code"] for f in rep["risk_flags"]}


def test_one_of_one_collection_still_checks_owner_concentration(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch, collection={"name": "PFP", "total_supply": 1000, "unique_item_count": 1000},
                    stats={"total": {"num_owners": 100, "sales": 10}, "intervals": []})
    rep = c.post("/analyze", json={"identifier": "some-pfp"}).json()
    assert rep["edition_collection"] is False
    assert "owner_concentration" in {f["code"] for f in rep["risk_flags"]}


def test_contract_address_case_is_normalized(client):
    lower = "0x" + "ab" * 20
    a = client.post("/analyze", json={"identifier": lower.upper().replace("0X", "0x")}).json()
    b = client.post("/analyze", json={"identifier": lower}).json()
    assert a["project"]["identifier"] == b["project"]["identifier"] == lower
    assert a["market"] == b["market"]


def test_chain_list_matches_opensea_api(client):
    assert client.post("/analyze", json={"identifier": "ok-slug", "chain": "klaytn"}).status_code == 422
    assert client.post("/analyze", json={"identifier": "ok-slug", "chain": "blast"}).status_code == 422
    assert client.post("/analyze", json={"identifier": "ok-slug", "chain": "ape_chain"}).status_code == 200
    assert client.post("/analyze", json={"identifier": "0x" + "a" * 40, "chain": "solana"}).status_code == 422
    assert client.post("/analyze", json={"identifier": "some-sol-slug", "chain": "solana"}).status_code == 200


def test_live_only_server_refuses_demo(make_client):
    c = make_client(MINTSCOPE_MODE="live", **LIVE)
    r = c.post("/analyze", json={"identifier": "demo-collection", "mode": "demo"})
    assert r.status_code == 409


def test_provider_slug_is_url_encoded(make_client, monkeypatch):
    seen = []
    c = live_client(make_client, monkeypatch, contract={"collection": "../../admin?x=1"}, seen=seen)
    r = c.post("/analyze", json={"identifier": "0x" + "c" * 40})
    assert r.status_code == 200
    assert all("/../" not in u and "?x=" not in u for u in seen), seen


def test_malformed_contract_entries_do_not_crash(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch, collection={"name": "X", "contracts": ["bad", None, {"address": "0xABC"}]})
    r = c.post("/analyze", json={"identifier": "some-collection"})
    assert r.status_code == 200
    assert r.json()["project"]["contract_address"]["value"] == "0xabc"


def test_api_key_never_leaks(make_client, monkeypatch):
    c = live_client(make_client, monkeypatch)
    ok = c.post("/analyze", json={"identifier": "some-collection"})
    assert KEY not in ok.text
    assert KEY not in c.get("/ledger").text and KEY not in c.get("/health").text
    for status in (401, 404, 429, 500):
        monkeypatch.setattr(providers.requests, "get", lambda *a, s=status, **k: FakeResp(s, {}))
        assert KEY not in c.post("/analyze", json={"identifier": "some-collection"}).text


def test_insecure_base_url_is_ignored(monkeypatch, tmp_path):
    from config import get_settings, DEFAULT_OPENSEA_BASE_URL
    monkeypatch.setenv("MINTSCOPE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("OPENSEA_BASE_URL", "http://evil.example.com/api")
    assert get_settings().opensea_base_url == DEFAULT_OPENSEA_BASE_URL


def test_manifest_v04_fields(make_client):
    env = dict(LIVE, MINTSCOPE_MODE="live", MINTSCOPE_PUBLIC_BASE_URL="https://mintscope.example.com",
               MINTSCOPE_CREATOR_ADDRESS="0x" + "ab" * 20)
    m = make_client(**env).get("/.well-known/ai-tool/mintscope-nft-analysis.json").json()
    assert "ai" in m["tags"] and "nft" in m["tags"] and len(m["tags"]) <= 16
    assert "klaytn" not in m["inputs"]["properties"]["chain"]["enum"]
    assert m["verifiability"]["tier"] == "self-attested"
    assert "image" not in m and "featuredImage" not in m
    m2 = make_client(MINTSCOPE_IMAGE_URL="https://mintscope.example.com/icon.png",
                     MINTSCOPE_FEATURED_IMAGE_URL="http://insecure.example.com/b.png", **env) \
        .get("/.well-known/ai-tool/mintscope-nft-analysis.json").json()
    assert m2["image"] == "https://mintscope.example.com/icon.png"
    assert "featuredImage" not in m2
    assert KEY not in str(m2)


def test_manifest_endpoint_probe_with_empty_body_is_4xx(client):
    # OpenSea's `tool-sdk verify` POSTs "{}" and fails on 404/405/5xx; 4xx means reachable.
    assert client.post("/analyze", json={}).status_code == 422
