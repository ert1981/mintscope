"""Pricing: free tier counter + monthly reset, x402 402 flow with a mocked facilitator (verify/settle),
replay guard, paid path, ledger revenue and secret hygiene.

No real payment is made in tests: a real Base Sepolia end-to-end payment needs a payer wallet that signs an
EIP-3009 authorization, which MintScope must never hold, so the facilitator is mocked here."""
import base64
import json

import pytest
import requests

import app as app_module
import payments
import providers
from tests.test_acceptance import FakeResp

KEY = "test-key-should-never-leak-1234567890"
PAYEE = "0x" + "12" * 20
PAYER = "0x" + "34" * 20
PRICED = {"MINTSCOPE_PRICING": "on", "MINTSCOPE_PAYEE_ADDRESS": PAYEE, "OPENSEA_API_KEY": KEY,
          "MINTSCOPE_PUBLIC_BASE_URL": "https://mintscope.test", "MINTSCOPE_CREATOR_ADDRESS": "0x" + "56" * 20,
          "MINTSCOPE_MODE": "live"}
TX = "0x" + "9" * 64


def payment_header(nonce_byte="ab", frm=PAYER):
    payload = {"x402Version": 1, "scheme": "exact", "network": "base", "payload": {
        "signature": "0x" + "cd" * 65,
        "authorization": {"from": frm, "to": PAYEE, "value": "2990000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": "0x" + nonce_byte * 32}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class Facil:
    """Mocked x402 facilitator (/verify and /settle)."""

    def __init__(self, valid=True, settle_ok=True, settle_status=200, down=False):
        self.valid, self.settle_ok, self.settle_status, self.down = valid, settle_ok, settle_status, down
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        if self.down:
            raise requests.ConnectionError("down")
        assert json["paymentRequirements"]["payTo"] == PAYEE
        assert json["paymentRequirements"]["maxAmountRequired"] == "2990000"
        if url.endswith("/verify"):
            return FakeResp(200, {"isValid": self.valid, "payer": PAYER,
                                  "invalidReason": None if self.valid else "invalid_exact_evm_payload_signature"})
        if self.settle_status != 200:
            return FakeResp(self.settle_status, {})
        return FakeResp(200, {"success": self.settle_ok, "transaction": TX if self.settle_ok else "",
                              "network": "base", "payer": PAYER})


def fake_opensea(monkeypatch):
    def fake_get(url, headers, timeout):
        if url.endswith("/stats"):
            return FakeResp(200, {"total": {"floor_price": 1.0, "floor_price_symbol": "ETH", "sales": 10,
                                            "volume": 12.0, "volume_symbol": "ETH", "num_owners": 40},
                                  "intervals": []})
        return FakeResp(200, {"name": "Real", "total_supply": 100, "unique_item_count": 100,
                              "safelist_status": "verified"})
    monkeypatch.setattr(providers.requests, "get", fake_get)


@pytest.fixture
def priced(make_client, monkeypatch):
    def _make(facil=None, **env):
        fake_opensea(monkeypatch)
        facil = facil or Facil()
        monkeypatch.setattr(payments.requests, "post", facil.post)
        return make_client(**{**PRICED, **env}), facil
    return _make


# ---- free tier -----------------------------------------------------------------
def test_free_tier_returns_score_and_summary_only_and_counts_down(priced):
    c, _ = priced()
    lefts = []
    for _ in range(3):
        r = c.post("/analyze", json={"identifier": "some-collection"})
        assert r.status_code == 200
        body = r.json()
        assert body["tier"] == "free"
        assert set(body["overall_score"]) >= {"value", "max"} and body["summary"]
        for paid_only in ("market", "risk_flags", "creator_evidence", "weaknesses", "missing_data", "report_id"):
            assert paid_only not in body
        assert "not financial advice" in body["disclaimer"]
        lefts.append(body["free_analyses_left"])
    assert lefts == [2, 1, 0]
    r = c.post("/analyze", json={"identifier": "some-collection"})
    assert r.status_code == 402  # quota used: x402 challenge for the paid report
    assert "Free tier used up" in r.json()["error"] and r.json()["accepts"][0]["payTo"] == PAYEE


def test_free_tier_counts_wallet_and_ip_and_resets_monthly(priced, monkeypatch):
    c, _ = priced()
    month = {"v": "2026-10"}
    monkeypatch.setattr(app_module, "datetime", type("D", (), {"now": staticmethod(
        lambda tz=None: type("T", (), {"strftime": lambda self, f: month["v"]})())}))
    w = {"X-Payer-Address": PAYER}
    for _ in range(3):
        assert c.post("/analyze", json={"identifier": "x-coll"}, headers=w).status_code == 200
    # Same IP without the wallet header, or a new wallet from the same IP, is still exhausted.
    assert c.post("/analyze", json={"identifier": "x-coll"}).status_code == 402
    assert c.post("/analyze", json={"identifier": "x-coll"},
                  headers={"X-Payer-Address": "0x" + "77" * 20}).status_code == 402
    month["v"] = "2026-11"  # new UTC month: counter resets
    r = c.post("/analyze", json={"identifier": "x-coll"}, headers=w)
    assert r.status_code == 200 and r.json()["free_analyses_left"] == 2


def test_free_exhausted_without_payee_is_429(make_client, monkeypatch):
    fake_opensea(monkeypatch)
    c = make_client(**{**PRICED, "MINTSCOPE_PAYEE_ADDRESS": "", "MINTSCOPE_FREE_PER_MONTH": "1"})
    assert c.post("/analyze", json={"identifier": "abc-coll"}).status_code == 200
    r = c.post("/analyze", json={"identifier": "abc-coll"})
    assert r.status_code == 429
    assert c.post("/analyze", json={"identifier": "abc-coll", "tier": "full"}).status_code == 503


# ---- x402 -----------------------------------------------------------------------
def test_full_tier_without_payment_returns_x402_challenge(priced):
    c, facil = priced()
    r = c.post("/analyze", json={"identifier": "some-collection", "tier": "full"})
    assert r.status_code == 402 and r.headers["X-Accept-Payment"] == "x402"
    body = r.json()
    assert body["x402Version"] == 1 and body["error"] == "X-PAYMENT header is required"
    req = body["accepts"][0]
    assert req == {"scheme": "exact", "network": "base", "maxAmountRequired": "2990000",
                   "resource": "https://mintscope.test/analyze", "description": req["description"],
                   "mimeType": "application/json", "payTo": PAYEE, "maxTimeoutSeconds": 300,
                   "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                   "extra": {"name": "USD Coin", "version": "2"}}
    assert facil.calls == []


def test_paid_success_returns_full_report_and_logs_revenue_after_settle(priced):
    c, facil = priced()
    r = c.post("/analyze", json={"identifier": "some-collection"}, headers={"X-Payment": payment_header()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] == "full"
    for key in ("market", "risk_flags", "creator_evidence", "weaknesses", "missing_data", "score", "report_id"):
        assert key in body
    assert body["payment"]["status"] == "settled" and body["payment"]["transaction"] == TX
    assert [k for k, _ in facil.calls] == ["verify", "settle"]
    receipt = json.loads(base64.b64decode(r.headers["X-PAYMENT-RESPONSE"]))
    assert receipt["success"] is True and receipt["transaction"] == TX
    ledger = c.get("/ledger").json()
    assert ledger["revenue_usd"] == 2.99 and ledger["spent_usd"] == 0
    assert any(x["category"] == "x402_payment" for x in ledger["by_category"])
    # The paid call does not use the free quota.
    assert c.post("/analyze", json={"identifier": "some-collection"}).json()["free_analyses_left"] == 2


def test_invalid_payment_is_rejected_without_settle_or_revenue(priced):
    c, facil = priced(Facil(valid=False))
    r = c.post("/analyze", json={"identifier": "some-collection"}, headers={"X-Payment": payment_header()})
    assert r.status_code == 402 and r.json()["error"] == "invalid_exact_evm_payload_signature"
    assert [k for k, _ in facil.calls] == ["verify"]
    assert c.get("/ledger").json()["revenue_usd"] == 0


def test_garbage_payment_header_is_invalid_payload(priced):
    c, facil = priced()
    r = c.post("/analyze", json={"identifier": "abc-coll"}, headers={"X-Payment": "not-base64-json!!"})
    assert r.status_code == 402 and r.json()["error"] == "invalid_payload" and facil.calls == []


def test_replayed_authorization_is_rejected(priced):
    c, facil = priced()
    h = {"X-Payment": payment_header("ee")}
    assert c.post("/analyze", json={"identifier": "abc-coll"}, headers=h).status_code == 200
    r = c.post("/analyze", json={"identifier": "abc-coll"}, headers=h)
    assert r.status_code == 402 and r.json()["error"] == "payment_authorization_already_used"
    assert [k for k, _ in facil.calls].count("settle") == 1
    assert c.get("/ledger").json()["revenue_usd"] == 2.99


def test_settle_failure_returns_no_report_and_no_revenue(priced):
    c, _ = priced(Facil(settle_ok=False))
    r = c.post("/analyze", json={"identifier": "abc-coll"}, headers={"X-Payment": payment_header("11")})
    assert r.status_code == 402 and r.json()["error"] == "settlement_failed"
    assert "market" not in r.json() and c.get("/ledger").json()["revenue_usd"] == 0
    # Not provably unbroadcast: the authorization stays reserved.
    assert c.post("/analyze", json={"identifier": "abc-coll"}, headers={"X-Payment": payment_header("11")}
                  ).json()["error"] == "payment_authorization_already_used"


def test_settle_rate_limited_releases_authorization_for_retry(priced):
    facil = Facil(settle_status=429)
    c, _ = priced(facil)
    h = {"X-Payment": payment_header("22")}
    assert c.post("/analyze", json={"identifier": "abc-coll"}, headers=h).json()["error"] == "settlement_failed"
    facil.settle_status = 200
    assert c.post("/analyze", json={"identifier": "abc-coll"}, headers=h).status_code == 200


def test_facilitator_unreachable_is_502(priced):
    c, _ = priced(Facil(down=True))
    r = c.post("/analyze", json={"identifier": "abc-coll"}, headers={"X-Payment": payment_header("33")})
    assert r.status_code == 502


def test_demo_mode_cannot_be_paid(make_client, monkeypatch):
    c = make_client(**{**PRICED, "MINTSCOPE_MODE": "demo", "OPENSEA_API_KEY": ""})
    assert c.post("/analyze", json={"identifier": "abc-coll", "tier": "full"}).status_code == 409


def test_discover_free_and_paid_views(priced, monkeypatch):
    import discovery
    sample = json.load(open(app_module.__file__.rsplit("/", 1)[0] + "/examples/discover_today_2026-10-29.json"))
    monkeypatch.setattr(discovery.Discoverer, "discover", lambda self, *a, **k: dict(sample))
    c, _ = priced()
    free = c.post("/discover", json={"period": "today"}).json()
    assert free["tier"] == "free" and free["results"]
    assert set(free["results"][0]) == {"rank", "name", "chain", "contract_address", "created_at", "score", "summary"}
    assert c.post("/discover", json={"period": "today", "tier": "full"}).status_code == 402
    paid = c.post("/discover", json={"period": "today"}, headers={"X-Payment": payment_header("44")})
    assert paid.status_code == 200
    assert "metrics" in paid.json()["results"][0] and "red_flags" in paid.json()["results"][0]
    assert paid.json()["payment"]["transaction"] == TX


# ---- manifest, page, config -------------------------------------------------------
def test_manifest_declares_x402_pricing_like_tool_sdk(priced):
    c, _ = priced()
    for slug in ("mintscope-nft-analysis", "mintscope-new-collections"):
        m = c.get(f"/.well-known/ai-tool/{slug}.json").json()
        assert m["pricing"] == [{"amount": "2990000",
                                 "asset": "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                                 "recipient": f"eip155:8453:{PAYEE}", "protocol": "x402"}]


def test_base_sepolia_uses_test_usdc(priced):
    c, _ = priced(MINTSCOPE_X402_NETWORK="base-sepolia")
    req = c.post("/analyze", json={"identifier": "abc-coll", "tier": "full"}).json()["accepts"][0]
    assert req["network"] == "base-sepolia" and req["asset"] == "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
    assert req["extra"]["name"] == "USDC"


@pytest.mark.parametrize("bad", ["0x" + "0" * 40, "0x000000000000000000000000000000000000dEaD", "0x123", "nope"])
def test_bad_payee_disables_paid_tier(make_client, monkeypatch, bad):
    c = make_client(**{**PRICED, "MINTSCOPE_PAYEE_ADDRESS": bad})
    assert c.post("/analyze", json={"identifier": "abc-coll", "tier": "full"}).status_code == 503
    assert "pricing" not in c.get("/.well-known/ai-tool/mintscope-nft-analysis.json").json()


def test_landing_page(priced):
    c, _ = priced()
    for path in ("/", "/pricing"):
        html = c.get(path).text
        for text in ("MintScope AI", "Free", "$2.99", "per report", "not financial advice", "Coming soon",
                     "scam", "x402"):
            assert text in html, text


def test_no_keys_in_any_output(priced):
    c, _ = priced()
    outputs = [c.get(p).text for p in ("/", "/pricing", "/health", "/ledger",
                                        "/.well-known/ai-tool/mintscope-nft-analysis.json")]
    outputs.append(c.post("/analyze", json={"identifier": "abc-coll"}).text)
    outputs.append(c.post("/analyze", json={"identifier": "abc-coll", "tier": "full"}).text)
    r = c.post("/analyze", json={"identifier": "abc-coll"}, headers={"X-Payment": payment_header("55")})
    outputs += [r.text, json.dumps(dict(r.headers))]
    blob = "\n".join(outputs)
    assert KEY not in blob
    assert "cd" * 65 not in blob  # the caller's signature is never echoed back


def test_demo_free_exhausted_is_429_not_a_payment_offer(make_client):
    c = make_client(**{**PRICED, "MINTSCOPE_MODE": "demo", "OPENSEA_API_KEY": "", "MINTSCOPE_FREE_PER_MONTH": "1"})
    assert c.post("/analyze", json={"identifier": "abc-coll"}).status_code == 200
    assert c.post("/analyze", json={"identifier": "abc-coll"}).status_code == 429
