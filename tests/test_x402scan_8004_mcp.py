"""x402scan discovery (openapi x-payment-info, /.well-known/x402), instant GET/HEAD 402 probes (x402dash),
ERC-8004 registration file + signing page, the remote MCP server with x402 pay-per-call, and the
ERC-8257 manifests staying byte-identical."""
import base64
import hashlib
import json
import re
import subprocess
import shutil

import pytest

import discovery
import erc8004
from tests.test_agentic_market import MANIFEST_SHA256, PROD, FacilV2
from tests.test_pricing import PAYEE, PAYER, TX, payment_header, priced  # noqa: F401

PAID = ("/analyze", "/discover")


def no_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery.Discoverer, "discover",
                        lambda self, *a, **k: calls.append(a) or {"period": "today", "results": [],
                                                                  "new_collections_found": 0})
    return calls


# ---- openapi + well-known -------------------------------------------------------------
def test_openapi_has_payment_info_402_and_schemas(priced):
    c, _ = priced()
    spec = c.get("/openapi.json").json()
    assert spec["openapi"] and spec["info"]["title"] and spec["info"]["version"]
    assert "x402" in spec["info"]["x-guidance"]
    for path in PAID:
        op = spec["paths"][path]["post"]
        assert op["x-payment-info"] == {"price": {"mode": "fixed", "currency": "USD", "amount": "2.99"},
                                        "protocols": [{"x402": {}}]}
        assert "402" in op["responses"]
        assert op["requestBody"]["content"]["application/json"]["schema"]["properties"]
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    # GET probes are not separate operations
    assert "get" not in spec["paths"]["/analyze"]


def test_well_known_x402_lists_paid_urls(priced):
    c, _ = priced()
    doc = c.get("/.well-known/x402").json()
    assert doc["version"] == 1
    assert doc["resources"] == ["https://mintscope.test/analyze", "https://mintscope.test/discover"]


def test_no_payment_info_when_pricing_off(make_client):
    spec = make_client().get("/openapi.json").json()
    assert "x-payment-info" not in spec["paths"]["/analyze"]["post"]
    assert make_client().get("/.well-known/x402").json()["resources"] == []


# ---- GET / HEAD probes ----------------------------------------------------------------
@pytest.mark.parametrize("path", ["/analyze", "/discover", "/discover?period=week&limit=999&chain=x"])
def test_get_probe_answers_402_instantly_without_scanning(priced, monkeypatch, path):
    c, facil = priced()
    calls = no_scan(monkeypatch)
    r = c.get(path)
    assert r.status_code == 402
    assert r.json()["accepts"][0]["maxAmountRequired"] == "2990000"
    v2 = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
    assert v2["accepts"][0]["amount"] == "2990000"
    assert v2["extensions"]["bazaar"]["info"]["input"]
    assert calls == [] and facil.calls == []


@pytest.mark.parametrize("path", ["/analyze", "/discover"])
def test_head_probe_answers_402(priced, monkeypatch, path):
    c, facil = priced()
    calls = no_scan(monkeypatch)
    r = c.head(path)
    assert r.status_code == 402 and r.headers["PAYMENT-REQUIRED"]
    assert calls == [] and facil.calls == []


def test_get_analyze_with_payment_is_refused_without_charging(priced):
    c, facil = priced()
    r = c.get("/analyze", headers={"X-Payment": payment_header()})
    assert r.status_code == 405 and facil.calls == []


def test_get_discover_free_tier_still_scans(priced, monkeypatch):
    c, _ = priced()
    calls = no_scan(monkeypatch)
    r = c.get("/discover?tier=free")
    assert r.status_code == 200 and len(calls) == 1


def test_get_discover_with_payment_runs_paid_scan(priced, monkeypatch):
    c, facil = priced()
    calls = no_scan(monkeypatch)
    r = c.get("/discover", headers={"X-Payment": payment_header("a1")})
    assert r.status_code == 200 and r.json()["tier"] == "full" and len(calls) == 1
    assert [k for k, _ in facil.calls] == ["verify", "settle"]


def test_get_probe_without_pricing_does_not_402(make_client, monkeypatch):
    c = make_client()
    no_scan(monkeypatch)
    assert c.get("/analyze").status_code == 405
    assert c.get("/discover").status_code == 200


# ---- ERC-8004 ---------------------------------------------------------------------------
def test_agent_registration_file(make_client):
    c = make_client(**PROD)
    a = c.get("/.well-known/agent-registration.json")
    b = c.get("/agent-registration.json")
    assert a.status_code == 200 and a.content == b.content
    doc = a.json()
    assert doc["type"] == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
    assert doc["name"] == "MintScope" and doc["image"].endswith("/static/icon.png")
    names = {s["name"]: s for s in doc["services"]}
    assert names["MCP"]["endpoint"] == "https://mintscope-zrvv.onrender.com/mcp"
    assert names["web"]["endpoint"] == "https://mintscope-zrvv.onrender.com/"
    assert doc["x402Support"] is True and doc["active"] is True and doc["registrations"] == []


def test_agent_registration_lists_agent_id_when_set(make_client):
    c = make_client(**PROD, MINTSCOPE_ERC8004_AGENT_ID="1234")
    regs = c.get("/.well-known/agent-registration.json").json()["registrations"]
    assert regs == [{"agentId": 1234, "agentRegistry": "eip155:8453:0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"}]


def test_register_8004_page_shows_exact_calldata(make_client):
    c = make_client(**PROD)
    r = c.get("/register-8004")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    uri = "https://mintscope-zrvv.onrender.com/.well-known/agent-registration.json"
    data = erc8004.encode_register(uri)
    assert data in r.text and uri in r.text and "0x2105" in r.text
    assert "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432" in r.text
    assert "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4" in r.text
    assert not re.search(r"__[A-Z_]+__", r.text)
    # wallet discovery (late injection / EIP-6963), clear no-wallet guidance, non-blocking log scan with timeout
    assert "eip6963:requestProvider" in r.text and "Open in MetaMask" in r.text and "timeout (" in r.text


def test_encode_register_matches_cast():
    uri = "https://mintscope-zrvv.onrender.com/.well-known/agent-registration.json"
    cast = shutil.which("cast") or "/root/.foundry/bin/cast"
    try:
        out = subprocess.run([cast, "calldata", "register(string)", uri], capture_output=True, text=True,
                             timeout=20).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("cast not installed")
    assert erc8004.encode_register(uri) == out


# ---- MCP --------------------------------------------------------------------------------
def rpc(c, method, params=None, id_=1, headers=None):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", json=body, headers=headers or {})


def test_mcp_initialize_list_ping(priced):
    c, _ = priced()
    r = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "io.github.ert1981/mintscope"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert rpc(c, "ping").json()["result"] == {}
    tools = {t["name"]: t for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"analyze_nft_collection", "discover_new_nft_collections"}
    assert tools["analyze_nft_collection"]["inputSchema"]["required"] == ["identifier"]
    assert tools["analyze_nft_collection"]["_meta"]["x402"]["price_usdc"] == "2.99"
    assert rpc(c, "nope").json()["error"]["code"] == -32601
    assert c.get("/mcp").status_code == 405


def test_mcp_paid_call_without_payment_returns_payment_required(priced, monkeypatch):
    c, facil = priced()
    calls = no_scan(monkeypatch)
    for name, args in [("analyze_nft_collection", {"identifier": "real", "tier": "full"}),
                       ("discover_new_nft_collections", {"period": "today", "tier": "full"})]:
        res = rpc(c, "tools/call", {"name": name, "arguments": args}).json()["result"]
        assert res["isError"] is True
        pr = res["structuredContent"]
        assert pr["x402Version"] == 2 and pr["resource"]["url"] == f"mcp://tool/{name}"
        assert pr["accepts"][0]["amount"] == "2990000" and pr["accepts"][0]["payTo"] == PAYEE
        assert json.loads(res["content"][0]["text"]) == pr
    assert calls == [] and facil.calls == []


def test_mcp_paid_call_with_payment_settles_and_blocks_replay(priced):
    facil = FacilV2()
    c, _ = priced(facil=facil)
    payment = json.loads(base64.b64decode(
        __import__("tests.test_agentic_market", fromlist=["v2_payment"]).v2_payment("77")))
    params = {"name": "analyze_nft_collection", "arguments": {"identifier": "real", "tier": "full"},
              "_meta": {"x402/payment": payment}}
    res = rpc(c, "tools/call", params).json()["result"]
    assert res["isError"] is False, res
    assert res["structuredContent"]["tier"] == "full"
    assert res["structuredContent"]["payment"]["status"] == "settled"
    assert res["_meta"]["x402/payment-response"]["transaction"] == TX
    assert [k for k, _ in facil.calls] == ["verify", "settle"]
    # Same authorization again: refused, not settled twice
    again = rpc(c, "tools/call", params).json()["result"]
    assert again["isError"] is True
    assert again["structuredContent"]["error"] == "payment_authorization_already_used"
    assert [k for k, _ in facil.calls] == ["verify", "settle", "verify"]
    # REST replay of the same authorization is refused as well (shared replay store)
    hdr = base64.b64encode(json.dumps(payment).encode()).decode()
    r = c.post("/analyze", json={"identifier": "real", "tier": "full"}, headers={"PAYMENT-SIGNATURE": hdr})
    assert r.status_code == 402


def test_mcp_free_tier_and_bad_args(priced):
    c, _ = priced()
    res = rpc(c, "tools/call", {"name": "analyze_nft_collection", "arguments": {"identifier": "real"}}).json()["result"]
    assert res["isError"] is False and res["structuredContent"]["tier"] == "free"
    bad = rpc(c, "tools/call", {"name": "analyze_nft_collection", "arguments": {"identifier": ""}}).json()["result"]
    assert bad["isError"] is True
    assert rpc(c, "tools/call", {"name": "x", "arguments": {}}).json()["error"]["code"] == -32602


def test_mcp_parse_errors(priced):
    c, _ = priced()
    assert c.post("/mcp", content=b"{bad", headers={"Content-Type": "application/json"}).json()["error"]["code"] == -32700
    assert c.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).status_code == 400


# ---- 402 index + manifests --------------------------------------------------------------
def test_402index_verify_file(make_client):
    r = make_client(**PROD).get("/.well-known/402index-verify.txt")
    assert r.status_code == 200 and r.text == "d2f60449e30a87218db49c57d852d5f94f77f2add2adb347e484b2c0d599f045"
    c = make_client(**PROD, MINTSCOPE_402INDEX_VERIFY="abcdef0123456789")
    r = c.get("/.well-known/402index-verify.txt")
    assert r.status_code == 200 and r.text == "abcdef0123456789"


def test_manifests_still_byte_identical(make_client):
    c = make_client(**PROD)
    for slug, digest in MANIFEST_SHA256.items():
        assert hashlib.sha256(c.get(f"/.well-known/ai-tool/{slug}.json").content).hexdigest() == digest


def test_mcp_gateway_billing_mode_does_not_charge_twice(priced, monkeypatch):
    monkeypatch.setenv("MINTSCOPE_MCP_BILLING", "gateway")
    c, facil = priced()
    res = rpc(c, "tools/call", {"name": "analyze_nft_collection", "arguments": {"identifier": "real"}}).json()["result"]
    assert res["isError"] is False and res["structuredContent"]["tier"] == "full" and facil.calls == []
