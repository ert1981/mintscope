"""GET /x402-pay: owner pays each paid route once with their own wallet (x402 v2), page + flow tests.

The page script is run under node with a fake wallet/fetch (tests/x402_pay_harness.js); the PAYMENT-SIGNATURE
it builds is then replayed against the real app with a mocked facilitator."""
import base64
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

import pytest

import x402_pay_page
from tests.test_agentic_market import ANALYZE_EXAMPLE, DISCOVER_EXAMPLE, FacilV2, PROD, TX, v2_header
from tests.test_pricing import PAYEE, priced  # noqa: F401

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
HARNESS = pathlib.Path(__file__).with_name("x402_pay_harness.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def cfg_of(text):
    return json.loads(text.split('<script type="application/json" id="cfg">')[1].split("</script>")[0])


def run_page(html, challenges, **kw):
    inp = {"html": html, "challenges": challenges, "clicks": [], **kw}
    p = subprocess.run(["node", str(HARNESS)], input=json.dumps(inp), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_page_served_with_strict_csp_and_real_routes(make_client):
    c = make_client(**PROD)
    r = c.get("/x402-pay")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["x-robots-tag"] == "noindex" and r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>\s*</body>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "form-action 'none'", "base-uri 'none'"):
        assert part in csp
    assert "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp
    cfg = cfg_of(r.text)
    assert cfg["usdc"] == USDC and cfg["payee"] == "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4"
    assert [(x["path"], x["example"]) for x in cfg["routes"]] == [("/analyze", ANALYZE_EXAMPLE), ("/discover", DISCOVER_EXAMPLE)]
    assert int(cfg["maxAtomic"]) == 5_000_000
    assert "real payment" in r.text and "USDC" in r.text and not re.search(r"__[A-Z]+__", r.text)
    assert "eip6963:requestProvider" in r.text and "Open in MetaMask" in r.text
    # no key material / no private-key handling anywhere in the page
    assert not re.search(r"privateKey|private_key|mnemonic|seed", r.text, re.I)


def test_config_json_cannot_break_out_of_its_script_tag():
    html = x402_pay_page.page("S", "0xabc", [{"id": "a", "name": "n", "path": "/a", "example": {"x": "</script><b>"}}], 1)
    cfg = html.split('id="cfg">')[1].split("</script>")[0]
    assert "<" not in cfg and json.loads(cfg)["routes"][0]["example"]["x"] == "</script><b>"


@needs_node
@pytest.mark.parametrize("path,example", [("/analyze", ANALYZE_EXAMPLE), ("/discover", DISCOVER_EXAMPLE)])
def test_page_flow_signs_eip3009_and_its_payment_settles(priced, monkeypatch, path, example):
    import discovery
    monkeypatch.setattr(discovery.Discoverer, "discover",
                        lambda self, *a, **k: {"period": "today", "chain": "all", "results": []})
    facil = FacilV2()
    c, _ = priced(facil, MINTSCOPE_PAYEE_ADDRESS=PAYEE)
    html = c.get("/x402-pay").text
    challenges = {p: c.post(p, json=e).headers["PAYMENT-REQUIRED"] for p, e in
                  (("/analyze", ANALYZE_EXAMPLE), ("/discover", DISCOVER_EXAMPLE))}
    rid = path.strip("/")
    receipt = base64.b64encode(json.dumps({"success": True, "transaction": TX}).encode()).decode()
    out = run_page(html, challenges, clicks=[rid], chainId="0x1", receiptHeader=receipt)
    assert out["price-analyze"] == out["price-discover"] == "<b>2.99 USDC</b>"
    assert out["total"] == "5.98 USDC"
    assert "wallet_switchEthereumChain" in out["wallet"]  # was on chain 0x1 -> switched to Base first
    assert len(out["confirms"]) == 1 and "2.99 USDC" in out["confirms"][0] and PAYEE in out["confirms"][0]
    assert len(out["signs"]) == 1 and len(out["paid"]) == 1
    acct, typed = out["signs"][0][0], json.loads(out["signs"][0][1])
    ch = json.loads(base64.b64decode(challenges[path]))
    a = ch["accepts"][0]
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert typed["domain"] == {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": a["asset"]}
    m = typed["message"]
    assert m["from"] == acct and m["to"] == PAYEE and m["value"] == a["amount"] == "2990000"
    assert int(m["validBefore"]) - int(m["validAfter"]) == 600 + a["maxTimeoutSeconds"]
    assert re.fullmatch(r"0x[0-9a-f]{64}", m["nonce"])
    assert TX in out["res-" + rid] and "basescan.org/tx/" in out["res-" + rid]
    req = [q for q in out["requests"] if q["paid"]][0]
    assert req["path"] == path and req["body"] == example and req["credentials"] == "omit"
    # the page's PAYMENT-SIGNATURE is a valid v2 payment for the real server
    sig = out["paid"][0]["signature"]
    sent = json.loads(base64.b64decode(sig))
    assert sent["x402Version"] == 2 and sent["accepted"] == a
    assert sent["resource"] == ch["resource"] and sent["extensions"] == ch["extensions"]
    assert sent["payload"]["authorization"] == m
    r = c.post(path, json=example, headers={"PAYMENT-SIGNATURE": sig})
    assert r.status_code == 200, r.text
    assert [k for k, _ in facil.calls] == ["verify", "settle"]
    assert json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))["transaction"] == TX


def _challenge(**over):
    a = {"scheme": "exact", "network": "eip155:8453", "asset": USDC, "amount": "2990000", "payTo": PAYEE,
         "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}, **over}
    return base64.b64encode(json.dumps({"x402Version": 2, "resource": {"url": "x"}, "accepts": [a]}).encode()).decode()


@needs_node
@pytest.mark.parametrize("over", [{"asset": "0x833589fCD6eDb6E08f4c7C32D2A403d29B4C55Bb"}, {"payTo": "0x" + "77" * 20},
                                  {"network": "eip155:84532"}, {"amount": "5000001"}, {"scheme": "upto"}])
def test_page_refuses_unexpected_requirements_without_signing(make_client, over):
    html = x402_pay_page.page("S", PAYEE, [{"id": "a", "name": "A", "path": "/a", "example": {}}], 5_000_000)
    out = run_page(html, {"/a": _challenge(**over)}, clicks=["a"])
    assert out["signs"] == [] and out["paid"] == [] and out["confirms"] == []


@needs_node
def test_cancelled_confirm_signs_nothing_and_double_click_pays_once():
    routes = [{"id": "a", "name": "A", "path": "/a", "example": {}}, {"id": "b", "name": "B", "path": "/b", "example": {}}]
    html = x402_pay_page.page("S", PAYEE, routes, 5_000_000)
    ch = {"/a": _challenge(), "/b": _challenge(amount="10000")}
    out = run_page(html, ch, clicks=["a"], confirm=False)
    assert out["signs"] == [] and out["paid"] == [] and "Cancelled" in out["res-a"]
    assert out["total"] == "3 USDC"
    out = run_page(html, ch, clicks=["a", "b"], concurrent=True)
    assert len(out["confirms"]) == 1 and len(out["signs"]) == 1 and len(out["paid"]) == 1
    assert out["disabled"] == {"pay-a": False, "pay-b": False}  # re-enabled after the single payment finished
