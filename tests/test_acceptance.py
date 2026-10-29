"""Acceptance tests 1-6 from the MVP spec (7 is checked by running the README commands)."""
import re
import subprocess
from pathlib import Path

import requests

import providers

ROOT = Path(__file__).resolve().parent.parent


# 1. The app starts with no paid credentials.
def test_starts_without_credentials(client):
    assert client.app.state.settings.opensea_api_key is None


# 2. /health returns success.
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["live_data_available"] is False
    assert body["default_effective_mode"] == "demo"


# 3. /analyze validates input and returns a report in demo mode.
def test_analyze_demo_report(client):
    r = client.post("/analyze", json={"identifier": "demo-collection"})
    assert r.status_code == 200
    rep = r.json()
    assert rep["data_mode"] == "demo"
    assert "SYNTHETIC" in rep["demo_notice"]
    assert rep["report_id"].startswith("demo_")
    assert rep["project"]["provider"] == "demo"
    assert rep["project"]["collection_name"]["status"] == "synthetic"
    # no demo value is ever labeled as live/provider data
    for section in ("market", "creator_evidence"):
        for item in rep[section].values():
            assert item["status"] in ("synthetic", "unknown")
    for key in ("risk_flags", "missing_data", "analyzed_at", "confidence", "disclaimer", "score", "sources"):
        assert key in rep
    assert "guarantee" in rep["disclaimer"]
    assert rep["confidence"]["level"] in ("low", "medium", "high")
    assert "not a realized sale" in rep["market"]["floor_price"]["meaning"].lower()


def test_analyze_accepts_contract_address(client):
    r = client.post("/analyze", json={"identifier": "0x" + "a" * 40, "chain": "base"})
    assert r.status_code == 200
    assert r.json()["project"]["identifier_type"] == "contract"


def test_analyze_rejects_invalid_input(client):
    for bad in [{"identifier": ""}, {"identifier": "x"}, {"identifier": "bad slug!"},
                {"identifier": "0x123"}, {"identifier": "a" * 101}, {},
                {"identifier": "ok-slug", "chain": "mars"}, {"identifier": "ok-slug", "mode": "magic"}]:
        assert client.post("/analyze", json=bad).status_code == 422, bad


def test_live_mode_without_key_is_refused_not_faked(client):
    r = client.post("/analyze", json={"identifier": "some-collection", "mode": "live"})
    assert r.status_code == 503
    assert "OPENSEA_API_KEY" in r.json()["detail"]


# 4. The report is saved and can be retrieved.
def test_report_saved_and_retrieved(client):
    rep = client.post("/analyze", json={"identifier": "demo-collection"}).json()
    got = client.get(f"/reports/{rep['report_id']}")
    assert got.status_code == 200
    assert got.json() == rep
    assert client.get("/reports/demo_doesnotexist").status_code == 404


# 5. Missing live data is explicitly marked unknown.
def test_missing_data_marked_unknown_demo(client):
    rep = client.post("/analyze", json={"identifier": "demo-sparse"}).json()
    missing = {m["field"] for m in rep["missing_data"]}
    assert {"mint_price", "floor_price", "total_sales"} <= missing
    assert rep["market"]["mint_price"] == {"value": None, "status": "unknown",
                                           "meaning": rep["market"]["mint_price"]["meaning"]}
    # missing data lowers confidence and earns no points
    assert rep["confidence"]["level"] == "low"
    assert rep["score"]["coverage"] < 0.5
    # missing info is never labeled fraud
    assert not any("fraud" in f["message"].lower() and "not evidence" not in f["message"].lower()
                   for f in rep["risk_flags"])


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


def test_missing_live_data_marked_unknown(make_client, monkeypatch):
    """Live path with a mocked OpenSea response that omits most fields."""
    def fake_get(url, headers, timeout):
        assert headers["x-api-key"] == "test-key"
        if url.endswith("/stats"):
            return FakeResp(200, {"total": {"floor_price": 0.05, "floor_price_symbol": "ETH"}, "intervals": []})
        return FakeResp(200, {"name": "Real Name", "opensea_url": "https://opensea.io/collection/x"})
    monkeypatch.setattr(providers.requests, "get", fake_get)
    c = make_client(OPENSEA_API_KEY="test-key")
    rep = c.post("/analyze", json={"identifier": "some-collection"}).json()
    assert rep["data_mode"] == "live" and "demo_notice" not in rep
    assert rep["report_id"].startswith("live_")
    assert rep["project"]["collection_name"] == {"value": "Real Name", "status": "provider_reported"}
    assert rep["market"]["floor_price"]["status"] == "provider_reported"
    for f in ("mint_price", "total_sales", "average_sale_price", "num_owners"):
        assert rep["market"][f]["status"] == "unknown"
        assert rep["market"][f]["value"] is None
    assert "https://opensea.io/collection/x" in rep["sources"]
    # floor alone must not produce a liquidity score
    liq = next(x for x in rep["score"]["components"] if x["name"] == "market_liquidity")
    assert liq["score"] is None
    # each provider request was recorded in the ledger
    assert c.get("/ledger").json()["live_calls_today"] == 2


def test_provider_timeout_and_errors(make_client, monkeypatch):
    c = make_client(OPENSEA_API_KEY="test-key")

    def timeout(*a, **k):
        raise requests.Timeout()
    monkeypatch.setattr(providers.requests, "get", timeout)
    r = c.post("/analyze", json={"identifier": "some-collection"})
    assert r.status_code == 504

    for status, expected in [(404, 404), (401, 502), (429, 429), (500, 502)]:
        monkeypatch.setattr(providers.requests, "get", lambda *a, s=status, **k: FakeResp(s, {}))
        r = c.post("/analyze", json={"identifier": "some-collection"})
        assert r.status_code == expected, status
        assert "demo" not in r.text.lower() or r.status_code == 404


def test_rate_limit(make_client):
    c = make_client(MINTSCOPE_RATE_LIMIT_PER_MINUTE="3")
    codes = [c.post("/analyze", json={"identifier": "demo-collection"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_budget_guard_blocks_paid_calls(make_client, monkeypatch):
    monkeypatch.setattr(providers.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    c = make_client(OPENSEA_API_KEY="test-key", MINTSCOPE_LIVE_CALL_COST_USD="1.0", MINTSCOPE_BUDGET_USD="1.99")
    r = c.post("/analyze", json={"identifier": "some-collection"})
    assert r.status_code == 402


# 6. No secrets are committed to the repository.
SECRET_PATTERNS = [
    re.compile(r"OPENSEA_API_KEY[ \t]*=[ \t]*[^\s#]+"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    re.compile(r"0x[a-fA-F0-9]{64}"),  # private-key shaped
]
PLACEHOLDER = re.compile(r"(?i)your_|<|example|xxx")
# Public keccak256 event topics used by discovery.py (not secrets): SeaDropMint and ERC-721 Transfer.
PUBLIC_TOPICS = {
    "0xe90cf9cc0a552cf52ea6ff74ece0f1c8ae8cc9ad630d3181f55ac43ca076b7d6",
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    # ERC-8257 ToolRegistry events: ToolRegistered and ToolDeregistered (register/deactivate pages).
    "0xe7be7fd3c802f61682f56ba817276b1cc81fbee7cb50705c8ed7952811dac397",
    "0x9add33e854e243f868ff7cacac076d65b1d56fb593e2bb891e76e2e8d5ddd034",
}


def _public_onchain_constants():
    """Public on-chain values (manifest hashes, tx hashes) published on the register/deactivate pages."""
    import deactivate_page
    import register_page
    vals = {t["manifest_hash"].lower() for t in register_page.TOOLS}
    vals |= {d["reg_tx"].lower() for d in deactivate_page.DUPLICATES}
    return vals


PUBLIC_TOPICS |= _public_onchain_constants()
CALLDATA_SELECTORS = ("0xfe1d0b16", "0x9148e6ca")


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = [ROOT / p for p in out.splitlines()]
        if files:
            return files
    except Exception:
        pass
    skip = {".venv", "__pycache__", ".pytest_cache", ".git", "build", "dist"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.parts)
            and p.suffix not in (".db",) and p.name != ".env"]


def test_no_secrets_in_repo():
    gitignore = (ROOT / ".gitignore").read_text()
    for entry in (".env", ".venv/", "*.db"):
        assert entry in gitignore
    for path in _tracked_files():
        if path.name == ".env":
            raise AssertionError(".env must not be committed")
        text = path.read_text(errors="ignore")
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                if path.name == "test_acceptance.py":
                    continue  # this file contains the patterns themselves
                if m.group(0).lower() in PUBLIC_TOPICS:
                    continue
                if m.group(0).lower().startswith(CALLDATA_SELECTORS):
                    continue  # public ABI calldata (registerTool / deregisterTool), not a key
                if PLACEHOLDER.search(m.group(0)):
                    continue  # documented placeholder such as "your_key"
                raise AssertionError(f"possible secret in {path.name}: {m.group(0)[:40]}")


def test_optional_api_token_protects_sensitive_routes(make_client):
    c = make_client(MINTSCOPE_API_TOKEN="test-secret-token")
    assert c.post("/analyze", json={"identifier": "demo-collection"}).status_code == 401
    headers = {"X-API-Token": "test-secret-token"}
    rep = c.post("/analyze", json={"identifier": "demo-collection"}, headers=headers)
    assert rep.status_code == 200
    assert c.get(f"/reports/{rep.json()['report_id']}").status_code == 401
    assert c.get(f"/reports/{rep.json()['report_id']}", headers=headers).status_code == 200
    assert c.get("/ledger").status_code == 401
    assert c.get("/ledger", headers=headers).status_code == 200
    assert c.get("/health").status_code == 200


def test_provider_malformed_numeric_values_become_unknown(make_client, monkeypatch):
    def fake_get(url, headers, timeout):
        if url.endswith("/stats"):
            return FakeResp(200, {"total": {"floor_price": "not-a-number", "sales": -2, "volume": float("inf")},
                                  "intervals": [{"interval": "seven_day", "sales": "oops", "volume": -1}]})
        return FakeResp(200, {"name": "Collection", "total_supply": "bad"})
    monkeypatch.setattr(providers.requests, "get", fake_get)
    c = make_client(OPENSEA_API_KEY="test-key")
    r = c.post("/analyze", json={"identifier": "some-collection"})
    assert r.status_code == 200, r.text
    rep = r.json()
    for field in ("floor_price", "total_sales", "total_volume", "sales_7d", "volume_7d", "total_supply"):
        assert rep["market"][field]["value"] is None
        assert rep["market"][field]["status"] == "unknown"


def test_config_rejects_non_finite_and_out_of_range_values(monkeypatch, tmp_path):
    from config import get_settings
    monkeypatch.setenv("MINTSCOPE_DB_PATH", str(tmp_path / "nested" / "db.sqlite"))
    monkeypatch.setenv("MINTSCOPE_BUDGET_USD", "nan")
    monkeypatch.setenv("MINTSCOPE_LIVE_CALL_COST_USD", "inf")
    monkeypatch.setenv("MINTSCOPE_RATE_LIMIT_PER_MINUTE", "-4")
    settings = get_settings()
    assert settings.budget_usd == 2.0
    assert settings.live_call_cost_usd == 0.0
    assert settings.rate_limit_per_minute == 30


def test_zero_mint_price_is_handled_as_a_real_value():
    import analyzer
    report = analyzer.build_report(
        {"provider": "opensea", "fields": {"name": "free", "mint_price": 0, "floor_price": 0.1,
         "average_sale_price": 0.0, "total_sales": 0, "total_supply": 10, "sales_7d": 0,
         "num_owners": 10, "contract_verified": None, "safelist_status": None, "website": None,
         "twitter": None, "discord": None, "description": None, "creator_address": None}, "sources": []},
        "free-project", "slug", "ethereum", "live")
    codes = {item["code"] for item in report["risk_flags"]}
    assert "avg_sale_below_mint" not in codes
    assert report["market"]["mint_price"]["value"] == 0


def test_tool_manifest_not_served_until_configured(client):
    assert client.get("/.well-known/ai-tool/mintscope-nft-analysis.json").status_code == 404


def test_tool_manifest_same_origin_and_valid(make_client):
    c = make_client(MINTSCOPE_MODE="live", OPENSEA_API_KEY="test-key",
                    MINTSCOPE_PUBLIC_BASE_URL="https://mintscope.example.com/",
                    MINTSCOPE_CREATOR_ADDRESS="0x" + "AB" * 20)
    r = c.get("/.well-known/ai-tool/mintscope-nft-analysis.json")
    assert r.status_code == 200
    m = r.json()
    assert m["type"] == "https://ercs.ethereum.org/ERCS/erc-8257#tool-manifest-v1"
    assert m["endpoint"] == "https://mintscope.example.com/analyze"
    assert m["creatorAddress"] == "0x" + "ab" * 20
    assert 1 <= len(m["name"]) <= 128 and 1 <= len(m["description"]) <= 500
    assert m["inputs"]["required"] == ["identifier"]
    assert "pricing" not in m  # free until x402 is implemented
    assert all(re.fullmatch(r"[a-z0-9-]{1,32}", t) for t in m["tags"])
    # manifest content is stable (its hash is registered onchain)
    assert c.get("/.well-known/ai-tool/mintscope-nft-analysis.json").json() == m


def test_tool_manifest_rejects_bad_config(make_client):
    demo = make_client(MINTSCOPE_PUBLIC_BASE_URL="https://mintscope.example.com",
                       MINTSCOPE_CREATOR_ADDRESS="0x" + "ab" * 20)
    assert demo.get("/.well-known/ai-tool/mintscope-nft-analysis.json").status_code == 404
    c = make_client(MINTSCOPE_MODE="live", OPENSEA_API_KEY="test-key", MINTSCOPE_PUBLIC_BASE_URL="http://insecure.example.com",
                    MINTSCOPE_CREATOR_ADDRESS="0x" + "ab" * 20)
    assert c.get("/.well-known/ai-tool/mintscope-nft-analysis.json").status_code == 404
