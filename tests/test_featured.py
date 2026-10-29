"""Featured collections: admin auth + CRUD, public view + analytics, submissions queue, sponsored slots,
and the hard rule that featuring/sponsorship never changes the risk score."""
import inspect

import pytest

import featured
import onchain

ADMIN = "adm-" + "k" * 30
CONTRACT = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


def fake_analysis(chain, contract):
    """Deterministic stand-in for onchain.analyze_contract (no network)."""
    score = 40.0 + int(contract[2:4], 16) % 50
    name = "Bored Ape Yacht Club" if contract == OTHER else "Real Drop"
    flags = [{"code": "contract_source_unverified", "severity": "warning", "message": "unverified"}]
    return {"source": "onchain_keyless", "chain": chain, "contract_address": contract, "name": name,
            "metrics": {}, "red_flags": flags, "score": {"value": score, "max": 100, "meaning": "safety"}}


@pytest.fixture
def fc(make_client):
    def _make(**env):
        c = make_client(MINTSCOPE_ADMIN_TOKEN=ADMIN, **env)
        c.app.state.featured.analyze_fn = fake_analysis
        return c
    return _make


H = {"X-Admin-Token": ADMIN}


def add(c, contract=CONTRACT, **kw):
    r = c.post("/admin/featured", json={"contract": contract, "chain": "ethereum",
                                        "image_url": "https://img.example/a.png",
                                        "mint_url": "https://opensea.io/collection/real-drop", "note": "Nice art",
                                        **kw}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def test_admin_disabled_without_token(make_client):
    c = make_client()
    assert c.get("/admin/featured", headers=H).status_code == 404


def test_admin_requires_correct_token(fc):
    c = fc()
    assert c.get("/admin/featured").status_code == 401
    assert c.get("/admin/featured", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.post("/admin/featured", json={"contract": CONTRACT}).status_code == 401
    assert c.get("/admin/featured", headers=H).status_code == 200


def test_short_admin_token_keeps_admin_disabled(make_client):
    c = make_client(MINTSCOPE_ADMIN_TOKEN="short")
    assert c.get("/admin/featured", headers={"X-Admin-Token": "short"}).status_code == 404


def test_featured_crud(fc):
    c = fc()
    item = add(c)
    assert item["risk"]["score"] == fake_analysis("ethereum", CONTRACT)["score"]["value"]
    assert item["image_url"].startswith("https://") and item["note"] == "Nice art"
    r = c.patch(f"/admin/featured/{item['id']}", json={"note": "Updated", "name": "Renamed"}, headers=H)
    assert r.json()["note"] == "Updated" and r.json()["name"] == "Renamed"
    assert c.patch(f"/admin/featured/{item['id']}", json={"image_url": "http://x"}, headers=H).status_code == 400
    assert [i["id"] for i in c.get("/featured").json()["items"]] == [item["id"]]
    c.patch(f"/admin/featured/{item['id']}", json={"active": False}, headers=H)
    assert c.get("/featured").json()["items"] == []
    assert c.delete(f"/admin/featured/{item['id']}", headers=H).status_code == 200
    assert c.get(f"/featured/{item['id']}").status_code == 404
    assert c.post("/admin/featured", json={"link": "https://opensea.io/collection/some-slug"},
                  headers=H).status_code == 422


def test_admin_cannot_set_score(fc):
    c = fc()
    item = add(c)
    for bad in ({"risk_score": 99}, {"score": 99}, {"analysis": {}}):
        assert c.patch(f"/admin/featured/{item['id']}", json=bad, headers=H).status_code == 422
    assert c.post("/admin/featured", json={"contract": OTHER, "risk_score": 99}, headers=H).status_code == 422
    with pytest.raises(featured.FeaturedError):
        c.app.state.featured.edit(item["id"], analysis="{}")


def test_views_and_mint_clicks_are_counted(fc):
    c = fc()
    item = add(c)
    for _ in range(3):
        c.get("/featured")
    c.get(f"/featured/{item['id']}")
    r = c.get(f"/featured/{item['id']}/mint", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://opensea.io/collection/real-drop"
    c.get(f"/featured/{item['id']}/mint", follow_redirects=False)
    stats = c.get("/admin/analytics", headers=H).json()["items"][0]
    assert stats["views"] == 4 and stats["mint_clicks"] == 2 and stats["click_rate"] == 0.5


def test_sponsored_disabled_by_default(fc):
    c = fc()
    item = add(c)
    r = c.post(f"/admin/featured/{item['id']}/sponsor", json={"plan": "7d"}, headers=H)
    assert r.status_code == 403
    assert c.get("/featured").json()["items"][0]["sponsored"] is None


def test_sponsored_slot_labeled_with_dates_and_stats(fc):
    c = fc(MINTSCOPE_SPONSORED_ENABLED="on")
    a, b = add(c), add(c, contract="0x" + "11" * 20)
    r = c.post(f"/admin/featured/{a['id']}/sponsor", json={"plan": "30d", "start": "2026-10-01T00:00:00+00:00"},
               headers=H).json()["sponsored"]
    assert r["label"] == "Sponsored" and r["plan"] == "30d" and r["price_usd"] == 29
    assert r["start"].startswith("2026-10-01") and r["end"].startswith("2026-10-31") and r["active"] is True
    assert "stats_during_sponsorship" in r and "does not change the risk score" in r["disclosure"]
    items = c.get("/featured").json()["items"]
    assert items[0]["id"] == a["id"] and items[0]["sponsored"]["label"] == "Sponsored"
    assert items[1]["sponsored"] is None
    assert c.post(f"/admin/featured/{b['id']}/sponsor", json={"plan": "1y"}, headers=H).status_code == 422
    seven = c.post(f"/admin/featured/{b['id']}/sponsor", json={"plan": "7d"}, headers=H).json()["sponsored"]
    assert seven["price_usd"] == 9


def test_sponsored_or_featured_never_changes_the_risk_score(fc):
    """Hard rule: the score of a sponsored/featured item equals the standalone analysis score."""
    c = fc(MINTSCOPE_SPONSORED_ENABLED="on")
    standalone = fake_analysis("ethereum", CONTRACT)["score"]["value"]
    item = add(c)
    assert item["risk"]["score"] == standalone
    c.post(f"/admin/featured/{item['id']}/sponsor", json={"plan": "30d"}, headers=H)
    for view in (c.get("/featured").json()["items"][0], c.get(f"/featured/{item['id']}").json(),
                 c.get("/admin/featured", headers=H).json()["items"][0]):
        assert view["risk"]["score"] == standalone
        assert view["risk"]["red_flags"] == fake_analysis("ethereum", CONTRACT)["red_flags"]
    # Re-analysis gives the same score whether or not the item is sponsored.
    assert c.post(f"/admin/featured/{item['id']}/reanalyze", headers=H).json()["risk"]["score"] == standalone
    c.delete(f"/admin/featured/{item['id']}/sponsor", headers=H)
    assert c.get(f"/featured/{item['id']}").json()["risk"]["score"] == standalone
    # The analysis functions cannot even receive featured/sponsored state.
    params = set(inspect.signature(onchain.analyze_contract).parameters)
    assert not params & {"featured", "sponsored", "sponsor", "paid", "promotion"}
    # A tampered stored score is refused rather than published.
    with pytest.raises(RuntimeError):
        featured.FeaturedService._check_integrity({"risk": {"score": standalone + 10}}, fake_analysis("ethereum", CONTRACT))


def test_submission_goes_to_review_queue_and_only_admin_approves(fc):
    c = fc()
    r = c.post("/submissions", json={"input": f"https://etherscan.io/address/{CONTRACT}",
                                      "mint_url": "https://opensea.io/collection/real-drop"})
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["status"] == "pending" and sub["contract_address"] == CONTRACT and sub["risk_score"] is not None
    assert c.get("/featured").json()["items"] == []  # nothing public before review
    assert c.post(f"/admin/submissions/{sub['id']}/approve").status_code == 401
    assert c.get("/admin/submissions").status_code == 401
    queue = c.get("/admin/submissions", headers=H).json()["submissions"]
    assert [s["id"] for s in queue] == [sub["id"]]
    assert c.post("/submissions", json={"input": CONTRACT}).status_code == 409  # duplicate pending
    ok = c.post(f"/admin/submissions/{sub['id']}/approve", json={"image_url": "https://img.example/x.png"},
                headers=H).json()
    assert ok["status"] == "approved" and ok["featured"]["risk"]["score"] == sub["risk_score"]
    assert len(c.get("/featured").json()["items"]) == 1
    assert c.post(f"/admin/submissions/{sub['id']}/approve", headers=H).status_code == 409


def test_suspicious_submissions_are_flagged(fc):
    c = fc()
    r = c.post("/submissions", json={"input": OTHER, "mint_url": "https://opensea-mint-free.xyz/claim"}).json()
    codes = {x["code"] for x in r["checks"]}
    assert {"name_impersonation", "mint_link_lookalike"} <= codes and r["suspicious"] is True
    rej = c.post(f"/admin/submissions/{r['id']}/reject", headers=H).json()
    assert rej["status"] == "rejected"
    assert c.get("/admin/submissions?status=rejected", headers=H).json()["submissions"][0]["id"] == r["id"]


def test_submission_of_wallet_address_is_flagged(fc):
    c = fc()

    def no_contract(chain, contract):
        raise onchain.NotAContract("No contract code at this address")
    c.app.state.featured.analyze_fn = no_contract
    r = c.post("/submissions", json={"input": "0x" + "99" * 20}).json()
    assert r["suspicious"] and r["checks"][0]["code"] == "not_a_contract"


def test_submission_rate_limit(fc):
    c = fc(MINTSCOPE_SUBMISSIONS_PER_DAY="2")
    assert c.post("/submissions", json={"input": "0x" + "01" * 20}).status_code == 201
    assert c.post("/submissions", json={"input": "0x" + "02" * 20}).status_code == 201
    assert c.post("/submissions", json={"input": "0x" + "03" * 20}).status_code == 429


@pytest.mark.parametrize("url,code", [
    ("http://mint.example.com", "mint_link_not_https"),
    ("https://xn--opnsea-0ya.io/x", "mint_link_punycode"),
    ("https://1.2.3.4/mint", "mint_link_ip_address"),
    ("https://bit.ly/abc", "mint_link_shortener"),
    ("https://0pensea.io/collection/x", "mint_link_lookalike"),
])
def test_mint_link_checks(url, code):
    assert code in {c["code"] for c in featured.mint_url_checks(url)}


def test_official_mint_links_pass():
    assert featured.mint_url_checks("https://opensea.io/collection/x") == []
    assert featured.mint_url_checks("https://zora.co/collect/base:0xabc") == []
