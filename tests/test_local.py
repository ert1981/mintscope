"""Local app mode: UI, key storage (device only, masked, never leaked), key test, keyless analysis,
no payment/quota, localhost guard, Pro gating with an offline license."""
import base64
import json
import logging
import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import license_pubkey
import licensing
import local_mode
import onchain
import providers
from tests.test_acceptance import FakeResp

USER_KEY = "userkey" + "Z9" * 12   # fake key shaped like a real one
L = {"X-MintScope-Local": "1"}
CONTRACT = "0x" + "ab" * 20


def fake_onchain(chain, address, **_):
    return {"source": "onchain_keyless", "chain": chain, "contract_address": address, "name": "Real Drop",
            "metrics": {"holders": 120, "unique_wallets_7d": 40, "contract_verified": True, "standard": "erc721"},
            "red_flags": [], "score": {"value": 71.5, "max": 100, "components": [], "meaning": "safety"}}


@pytest.fixture
def local(make_client, monkeypatch, tmp_path):
    monkeypatch.setattr(onchain, "analyze_contract", fake_onchain)

    def _make(**env):
        return make_client(MINTSCOPE_LOCAL="1", **env)
    return _make


def test_ui_has_all_pages(local):
    c = local()
    html = c.get("/").text
    for text in ("Analyze", "Discover", "Featured", "Settings", "Test key", "type=\"password\"", "Sponsored",
                 "Not financial advice"):
        assert text in html, text
    assert "AI model" not in html  # no AI-model key field: the code does not use one


def test_localhost_guard(local):
    c = local()
    assert c.get("/", headers={"host": "evil.example"}).status_code == 403
    assert c.post("/api/local/key", json={"opensea_api_key": USER_KEY}).status_code == 403  # no local header


def test_key_saved_to_600_file_masked_and_never_returned(local, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c = local()
    r = c.post("/api/local/key", json={"opensea_api_key": USER_KEY}, headers=L)
    assert r.status_code == 200
    assert r.json()["masked"] == "••••" + USER_KEY[-4:] and r.json()["storage"] == "file-600"
    f = tmp_path / "cfg" / "secrets.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and json.loads(f.read_text())["opensea_api_key"] == USER_KEY
    outputs = [r.text, c.get("/api/local/state").text, c.get("/").text, c.get("/health").text]
    assert all(USER_KEY not in o for o in outputs)
    assert USER_KEY not in caplog.text
    assert c.delete("/api/local/key", headers=L).json()["set"] is False
    assert "opensea_api_key" not in json.loads(f.read_text())


def test_bad_key_format_rejected(local):
    c = local()
    assert c.post("/api/local/key", json={"opensea_api_key": "short"}, headers=L).status_code == 422
    assert c.post("/api/local/key", json={"opensea_api_key": "bad key with spaces!!"}, headers=L).status_code == 422


def test_key_test_button_talks_only_to_opensea(local, monkeypatch):
    c = local()
    seen = []

    def fake_get(url, headers, timeout):
        seen.append((url, headers))
        return FakeResp(200 if headers["x-api-key"] == USER_KEY else 401, {})
    monkeypatch.setattr(local_mode.requests, "get", fake_get)
    assert c.post("/api/local/key/test", json={"opensea_api_key": USER_KEY}, headers=L).json()["status"] == "valid"
    bad = c.post("/api/local/key/test", json={"opensea_api_key": "x" * 32}, headers=L).json()
    assert bad["ok"] is False and bad["status"] == "invalid"
    assert all(u.startswith("https://api.opensea.io/api/v2/") for u, _ in seen)
    c.post("/api/local/key", json={"opensea_api_key": USER_KEY}, headers=L)
    r = c.post("/api/local/key/test", json={}, headers=L)  # uses the saved key
    assert r.json()["ok"] is True and USER_KEY not in r.text


def test_keyless_analysis_without_any_key(local, monkeypatch):
    monkeypatch.setattr(providers.requests, "get", lambda url, headers, timeout: FakeResp(401, {}))
    c = local()
    for text in (CONTRACT, f"https://opensea.io/item/ethereum/{CONTRACT}/1", f"https://basescan.org/address/{CONTRACT}"):
        r = c.post("/api/local/analyze", json={"input": text}, headers=L)
        assert r.status_code == 200, r.text
        assert r.json()["risk_score"] == 71.5 and r.json()["score_source"] == "onchain_keyless"
    r = c.post("/api/local/analyze", json={"input": "https://opensea.io/collection/some-slug"}, headers=L)
    assert r.status_code == 400 and "OpenSea API key" in r.json()["detail"]


def test_keyless_opensea_public_data_sends_no_key(local, monkeypatch):
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(headers)
        if url.endswith("/stats"):
            return FakeResp(200, {"total": {"floor_price": 2, "floor_price_symbol": "ETH", "sales": 5, "volume": 9,
                                            "volume_symbol": "ETH"}, "intervals": []})
        return FakeResp(200, {"name": "Pudgy", "collection": "pudgypenguins", "total_supply": 8888})
    monkeypatch.setattr(providers.requests, "get", fake_get)
    c = local()
    r = c.post("/api/local/analyze", json={"input": "https://opensea.io/collection/pudgypenguins"}, headers=L)
    assert r.status_code == 200, r.text
    assert r.json()["opensea"]["access"] == "public_no_key" and r.json()["score_source"] == "opensea"
    assert seen and all("x-api-key" not in h for h in seen)


def test_users_key_adds_opensea_data_and_never_appears(local, monkeypatch):
    c = local()
    c.post("/api/local/key", json={"opensea_api_key": USER_KEY}, headers=L)
    seen = []

    def fake_get(url, headers, timeout):
        seen.append((url, headers.get("x-api-key")))
        if url.endswith("/stats"):
            return FakeResp(200, {"total": {"floor_price": 1, "floor_price_symbol": "ETH", "sales": 3, "volume": 3,
                                            "volume_symbol": "ETH"}, "intervals": []})
        return FakeResp(200, {"name": "Real Drop", "collection": "real-drop", "total_supply": 10})
    monkeypatch.setattr(providers.requests, "get", fake_get)
    r = c.post("/api/local/analyze", json={"input": "real-drop"}, headers=L)
    assert r.status_code == 200, r.text
    assert r.json()["opensea"]["market"]["floor_price"]["value"] == 1
    assert seen and all(u.startswith("https://api.opensea.io/") and k == USER_KEY for u, k in seen)
    assert USER_KEY not in r.text


def test_no_payment_or_quota_in_local_mode(local, monkeypatch):
    monkeypatch.setattr(providers.requests, "get", lambda url, headers, timeout: FakeResp(401, {}))
    c = local(MINTSCOPE_PRICING="on", MINTSCOPE_PAYEE_ADDRESS="0x" + "12" * 20)
    for _ in range(6):
        assert c.post("/api/local/analyze", json={"input": CONTRACT}, headers=L).status_code == 200
        r = c.post("/analyze", json={"identifier": "some-collection"})
        assert r.status_code == 200 and "tier" not in r.json()
    assert "pricing" not in c.get("/health").text


def _license(monkeypatch, tier="pro"):
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(license_pubkey, "PUBLIC_KEY_B64", base64.b64encode(raw).decode())
    return licensing.sign({"v": 1, "product": "mintscope", "tier": tier, "lid": "L-1", "name": "T",
                           "issued": "2026-10-29", "edition": "intro"}, priv)


def test_pro_features_are_gated_by_offline_license(local, monkeypatch):
    monkeypatch.setattr(providers.requests, "get", lambda url, headers, timeout: FakeResp(401, {}))
    lic = _license(monkeypatch)
    c = local()
    body = {"inputs": [CONTRACT, "0x" + "cd" * 20]}
    r = c.post("/api/local/compare", json=body, headers=L)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "pro_required"
    assert c.post("/api/local/export", json=body, headers=L).status_code == 403
    assert c.post("/api/local/license", json={"license": lic[:-6] + "AAAAAA"}, headers=L).status_code == 422
    assert c.post("/api/local/license", json={"license": lic}, headers=L).json()["activated"] is True
    assert c.get("/api/local/state").json()["license"]["tier"] == "pro"
    cmp = c.post("/api/local/compare", json=body, headers=L).json()
    assert len(cmp["rows"]) == 2 and cmp["rows"][0]["risk_score"] == 71.5
    exp = c.post("/api/local/export", json=body, headers=L)
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    assert exp.json()["count"] == 2
    c.delete("/api/local/license", headers=L)
    assert c.post("/api/local/compare", json=body, headers=L).status_code == 403


def test_local_featured_and_submit_use_local_db_without_keys(local):
    c = local()
    c.post("/api/local/key", json={"opensea_api_key": USER_KEY}, headers=L)
    r = c.post("/api/local/submit", json={"input": CONTRACT}, headers=L)
    assert r.status_code == 200 and r.json()["status"] == "pending" and USER_KEY not in r.text
    assert c.get("/api/local/featured").json()["items"] == []


def test_keystore_uses_os_keyring_when_available(tmp_path, monkeypatch):
    import keystore

    class FakeKR:
        priority = 5
        store = {}

        def get_password(self, s, n):
            return self.store.get((s, n))

        def set_password(self, s, n, v):
            self.store[(s, n)] = v

        def delete_password(self, s, n):
            self.store.pop((s, n), None)
    monkeypatch.setattr(keystore, "_usable_keyring", lambda: FakeKR())
    ks = keystore.KeyStore(tmp_path)
    ks.set("opensea_api_key", USER_KEY)
    assert ks.backend == "os-keyring" and ks.get("opensea_api_key") == USER_KEY
    assert not (tmp_path / "secrets.json").exists()
    assert ks.status("opensea_api_key")["masked"].endswith(USER_KEY[-4:])
