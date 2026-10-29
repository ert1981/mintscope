"""agentic.market / CDP Bazaar readiness: 402 on empty probes, x402 v2 PAYMENT-REQUIRED header with
extensions.bazaar, PNG icon, v2 payments, facilitator selection (PayAI default, CDP only with both keys),
and the ERC-8257 manifests staying byte-identical (their hashes are registered onchain)."""
import base64
import hashlib
import json
import re

import pytest

import payments
from tests.test_acceptance import FakeResp
from tests.test_pricing import PAYEE, PAYER, TX, payment_header, priced  # noqa: F401  (fixture)

PROD = {"MINTSCOPE_MODE": "live", "MINTSCOPE_PRICING": "on",
        "MINTSCOPE_PAYEE_ADDRESS": "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4",
        "MINTSCOPE_CREATOR_ADDRESS": "0x93714d5970f511ac22f0e5635d3fbf538f717cd4",
        "MINTSCOPE_PUBLIC_BASE_URL": "https://mintscope-zrvv.onrender.com", "OPENSEA_API_KEY": "placeholder"}
# sha256 of the manifests served live on 2026-10-29 (tools 814/815 on the Base ToolRegistry).
MANIFEST_SHA256 = {
    "mintscope-nft-analysis": "fc23a4fabf3bfdef8c1a38509f4804f8082fb5d45c552c51167aaf54145312f9",
    "mintscope-new-collections": "3ae8c44fe7ed25cf1a30fb1e8453a38b77203c6f183b8ecc813f906b62bca814",
}
ANALYZE_EXAMPLE = {"identifier": "boredapeyachtclub", "chain": "ethereum", "tier": "full"}
DISCOVER_EXAMPLE = {"period": "today", "chain": "all", "limit": 10, "tier": "full"}


def v2_header(resp) -> dict:
    return json.loads(base64.b64decode(resp.headers["PAYMENT-REQUIRED"]))


def v2_payment(nonce_byte="ef", **accepted_overrides):
    accepted = {"scheme": "exact", "network": "eip155:8453", "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "amount": "2990000", "payTo": PAYEE, "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"}, **accepted_overrides}
    body = {"x402Version": 2, "accepted": accepted,
            "resource": {"url": "https://mintscope.test/analyze"},
            "payload": {"signature": "0x" + "cd" * 65,
                        "authorization": {"from": PAYER, "to": PAYEE, "value": "2990000", "validAfter": "0",
                                          "validBefore": "9999999999", "nonce": "0x" + nonce_byte * 32}}}
    return base64.b64encode(json.dumps(body).encode()).decode()


class FacilV2:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("/verify"):
            return FakeResp(200, {"isValid": True, "payer": PAYER})
        return FakeResp(200, {"success": True, "transaction": TX, "network": "eip155:8453", "payer": PAYER})


# ---- manifests unchanged -------------------------------------------------------------
def test_manifests_are_byte_identical_to_the_onchain_registered_versions(make_client):
    c = make_client(**PROD)
    for slug, digest in MANIFEST_SHA256.items():
        r = c.get(f"/.well-known/ai-tool/{slug}.json")
        assert r.status_code == 200
        assert hashlib.sha256(r.content).hexdigest() == digest


# ---- 402 for empty probes and the Bazaar example input ----------------------------------
@pytest.mark.parametrize("path,body", [("/analyze", b""), ("/analyze", b"{}"), ("/discover", b""),
                                       ("/discover", b"{}"), ("/discover", b"  ")])
def test_empty_body_on_paid_route_answers_402(priced, path, body):
    c, facil = priced()
    r = c.post(path, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 402
    v1 = r.json()
    assert v1["x402Version"] == 1 and v1["accepts"][0]["payTo"] == PAYEE
    assert v1["accepts"][0]["maxAmountRequired"] == "2990000"
    assert v2_header(r)["x402Version"] == 2
    assert facil.calls == []


@pytest.mark.parametrize("path,example", [("/analyze", ANALYZE_EXAMPLE), ("/discover", DISCOVER_EXAMPLE)])
def test_bazaar_example_input_answers_402_with_v2_header(priced, path, example):
    c, _ = priced()
    r = c.post(path, json=example)
    assert r.status_code == 402
    v2 = v2_header(r)
    assert v2["resource"]["url"] == f"https://mintscope.test{path}"
    assert v2["resource"]["iconUrl"] == "https://mintscope.test/static/icon.png"
    assert v2["resource"]["mimeType"] == "application/json"
    req = v2["accepts"][0]
    assert req == {"scheme": "exact", "network": "eip155:8453",
                   "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "amount": "2990000", "payTo": PAYEE,
                   "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}
    info = v2["extensions"]["bazaar"]["info"]
    assert info["input"] == {"type": "http", "method": "POST", "bodyType": "json", "body": example}
    assert "PAYMENT-REQUIRED" in r.headers["Access-Control-Expose-Headers"]


def test_v1_402_body_is_unchanged(priced):
    c, _ = priced()
    r = c.post("/analyze", json=ANALYZE_EXAMPLE)
    assert r.json() == {"x402Version": 1, "error": "X-PAYMENT header is required", "accepts": [{
        "scheme": "exact", "network": "base", "maxAmountRequired": "2990000",
        "resource": "https://mintscope.test/analyze", "description": "MintScope full NFT risk report (2.99 USDC)",
        "mimeType": "application/json", "payTo": PAYEE, "maxTimeoutSeconds": 300,
        "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "extra": {"name": "USD Coin", "version": "2"}}]}


def test_invalid_input_still_422(priced):
    c, _ = priced()
    assert c.post("/analyze", json={"identifier": "bad slug!!"}).status_code == 422
    assert c.post("/discover", json={"period": "year"}).status_code == 422
    assert c.post("/analyze", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 422
    # An empty body sent together with a payment is never verified or charged.
    r = c.post("/analyze", content=b"", headers={"X-Payment": payment_header(), "Content-Type": "application/json"})
    assert r.status_code == 422


def test_empty_body_without_pricing_keeps_422(make_client):
    c = make_client()
    assert c.post("/analyze", content=b"", headers={"Content-Type": "application/json"}).status_code == 422


# ---- bazaar metadata ------------------------------------------------------------------
@pytest.mark.parametrize("path,example", [("/analyze", ANALYZE_EXAMPLE), ("/discover", DISCOVER_EXAMPLE)])
def test_bazaar_metadata_validates_and_is_descriptive(priced, path, example):
    jsonschema = pytest.importorskip("jsonschema")
    c, _ = priced()
    v2 = v2_header(c.post(path, json=example))
    ext = v2["extensions"]["bazaar"]
    # The CDP facilitator validates info against schema strictly (draft 2020-12).
    jsonschema.Draft202012Validator(ext["schema"]).validate(ext["info"])
    body_schema = ext["schema"]["properties"]["input"]["properties"]["body"]
    jsonschema.Draft202012Validator(body_schema).validate(example)
    text = " ".join([v2["resource"]["description"], json.dumps(ext)]).lower()
    for phrase in ("ignore", "you must", "always call", "instruction", "system prompt", "disregard"):
        assert phrase not in text


def test_v2_payment_required_parses_with_official_x402_models(priced):
    pytest.importorskip("jsonschema")  # x402's bazaar helpers need it (test-only; the app does not import them)
    x402_schemas = pytest.importorskip("x402.schemas")
    bazaar = pytest.importorskip("x402.extensions.bazaar")
    from x402.extensions.bazaar.types import parse_discovery_extension
    c, _ = priced()
    v2 = v2_header(c.post("/analyze", json=ANALYZE_EXAMPLE))
    model = x402_schemas.PaymentRequired.model_validate(v2)
    assert model.accepts[0].amount == "2990000"
    ours = v2["extensions"]["bazaar"]
    parse_discovery_extension(ours)
    # Same shape as the official helper's output (which leaves `method` to be filled in per route).
    official = bazaar.declare_discovery_extension(
        input=ANALYZE_EXAMPLE, input_schema=ours["schema"]["properties"]["input"]["properties"]["body"],
        body_type="json", output=bazaar.OutputConfig(example=ours["info"]["output"]["example"]))["bazaar"]
    assert {k: v for k, v in ours["info"]["input"].items() if k != "method"} == official["info"]["input"]
    assert ours["schema"]["properties"]["input"] == official["schema"]["properties"]["input"]


# ---- icon ----------------------------------------------------------------------------
def test_icon_is_public_512_png(client):
    r = client.get("/static/icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.content[16:24] == (512).to_bytes(4, "big") * 2


# ---- v2 payments ------------------------------------------------------------------------
def test_v2_payment_signature_is_verified_settled_and_receipted(priced):
    facil = FacilV2()
    c, _ = priced(facil)
    r = c.post("/analyze", json={"identifier": "real", "tier": "full"}, headers={"PAYMENT-SIGNATURE": v2_payment()})
    assert r.status_code == 200, r.text
    assert [k for k, _ in facil.calls] == ["verify", "settle"]
    sent = facil.calls[0][1]
    assert sent["x402Version"] == 2 and sent["paymentRequirements"]["amount"] == "2990000"
    assert sent["paymentRequirements"]["network"] == "eip155:8453"
    receipt = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert receipt["success"] is True and receipt["transaction"] == TX
    assert "X-PAYMENT-RESPONSE" in r.headers
    assert r.json()["payment"]["status"] == "settled"


@pytest.mark.parametrize("override", [{"amount": "1"}, {"payTo": "0x" + "77" * 20}, {"network": "eip155:84532"}])
def test_v2_mismatched_accepted_requirements_are_rejected(priced, override):
    facil = FacilV2()
    c, _ = priced(facil)
    r = c.post("/analyze", json={"identifier": "real", "tier": "full"},
               headers={"PAYMENT-SIGNATURE": v2_payment(**override)})
    assert r.status_code == 402 and r.json()["error"] == "payment_requirements_mismatch"
    assert facil.calls == []


def test_v1_and_v2_share_the_replay_guard(priced):
    facil = FacilV2()
    c, _ = priced(facil)
    body = {"identifier": "real", "tier": "full"}
    assert c.post("/analyze", json=body, headers={"PAYMENT-SIGNATURE": v2_payment("ab")}).status_code == 200
    r = c.post("/analyze", json=body, headers={"X-Payment": payment_header("ab")})
    assert r.status_code == 402 and r.json()["error"] == "payment_authorization_already_used"


# ---- facilitator selection ------------------------------------------------------------------
def test_payai_is_the_default_facilitator(priced):
    c, _ = priced()
    assert c.app.state.facilitator_kind == "payai"


def test_one_cdp_key_alone_keeps_payai():
    assert payments.make_facilitator("https://facilitator.payai.network", {"CDP_API_KEY_ID": "x"}).kind == "payai"
    assert payments.make_facilitator("https://facilitator.payai.network",
                                     {"CDP_API_KEY_SECRET": "x"}).kind == "payai"


def test_both_cdp_keys_select_cdp_facilitator(priced, monkeypatch):
    pytest.importorskip("cdp.x402")
    monkeypatch.setenv("CDP_API_KEY_ID", "fake-id-for-tests")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "fake-secret-for-tests")
    c, _ = priced()
    assert c.app.state.facilitator_kind == "cdp"
    f = payments.make_facilitator("https://facilitator.payai.network")
    assert f.url == "https://api.cdp.coinbase.com/platform/v2/x402"
    # Key values are never exposed by the app.
    for path in ("/health", "/pricing", "/openapi.json"):
        assert "fake-secret-for-tests" not in c.get(path).text


def test_cdp_facilitator_maps_verify_and_settle(monkeypatch):
    pytest.importorskip("cdp.x402")
    from x402.schemas import SettleResponse, VerifyResponse
    import httpx
    f = payments.CdpFacilitator("fake-id", "fake-secret")
    monkeypatch.setattr(f.client, "verify_from_bytes",
                        lambda p, r: VerifyResponse(is_valid=True, payer=PAYER))
    monkeypatch.setattr(f.client, "settle_from_bytes",
                        lambda p, r: SettleResponse(success=True, transaction=TX, network="eip155:8453", payer=PAYER))
    assert f.verify({}, {})["payer"] == PAYER
    settled, retry = f.settle({}, {})
    assert settled["transaction"] == TX and retry is False

    monkeypatch.setattr(f.client, "verify_from_bytes",
                        lambda p, r: VerifyResponse(is_valid=False, invalid_reason="insufficient_funds"))
    with pytest.raises(payments.PaymentError) as e:
        f.verify({}, {})
    assert e.value.status == 402 and e.value.error == "insufficient_funds"

    def down(p, r):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(f.client, "settle_from_bytes", down)
    assert f.settle({}, {}) == (None, True)
    monkeypatch.setattr(f.client, "verify_from_bytes", down)
    with pytest.raises(payments.PaymentError) as e:
        f.verify({}, {})
    assert e.value.status == 502


def test_no_secret_strings_in_new_code():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("payments.py", "app.py", "icon.py"):
        text = (root / name).read_text()
        assert not re.search(r"-----BEGIN|organizations/[0-9a-f-]{20,}/apiKeys", text)
