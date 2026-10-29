"""Tests for new-collection discovery (no network: RPC and explorer are faked)."""
import discovery
from discovery import SEADROP_MINT, TRANSFER, ZERO

A, B, C, D = ("0x" + c * 40 for c in "abcd")
NEW, OLD, NEW2 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


def t(addr):
    return "0x" + "0" * 24 + addr[2:]


def word(n):
    return format(n, "064x")


def mint_log(nft, minter, qty=1, price_wei=0, block=100, tx="0xt1"):
    data = "0x" + word(int(minter, 16)) + word(qty) + word(price_wei) + word(1000) + word(0)
    return {"topics": [SEADROP_MINT, t(nft), t(minter), t(ZERO)], "data": data, "blockNumber": hex(block),
            "transactionHash": tx}


def transfer(frm, to, tid, block=100, idx=0):
    return {"topics": [TRANSFER, t(frm), t(to), "0x" + word(tid)], "data": "0x", "blockNumber": hex(block),
            "logIndex": hex(idx)}


def test_decode_seadrop_mint():
    m = discovery.decode_seadrop_mint(mint_log(NEW, A, qty=3, price_wei=2 * 10**15, block=7))
    assert m == {"nft": NEW, "minter": A, "payer": A, "quantity": 3, "unit_price_wei": 2 * 10**15,
                 "block": 7, "tx": "0xt1"}
    assert discovery.decode_seadrop_mint({"topics": [SEADROP_MINT], "data": "0x", "blockNumber": "0x1"}) is None


def test_transfer_metrics_holders_and_wash_patterns():
    logs = [transfer(ZERO, A, 1, idx=0), transfer(ZERO, A, 2, idx=1), transfer(ZERO, B, 3, idx=2),
            transfer(A, C, 1, 101), transfer(C, A, 1, 102),          # round trip on token 1
            transfer(ZERO, D, 4, 103), transfer(D, "0x000000000000000000000000000000000000dead", 4, 104)]
    m = discovery.transfer_metrics(logs)
    assert m["minted_onchain"] == 4 and m["burned"] == 1
    assert m["unique_holders"] == 2 and m["held_items"] == 3
    assert m["top_holder"] == A and abs(m["top_holder_share"] - 2 / 3) < 1e-9
    assert m["round_trips"] == 1
    assert m["secondary_transfers"] == 3


def test_one_way_batch_is_not_wash_but_back_and_forth_is():
    one_way = [transfer(ZERO, A, i, 100, i) for i in range(5)] + [transfer(A, B, i, 101, i) for i in range(5)]
    assert discovery.transfer_metrics(one_way)["repeated_pair_transfers"] == 0
    both = one_way + [transfer(B, A, 0, 102, 0), transfer(B, A, 1, 102, 1)]
    m = discovery.transfer_metrics(both)
    assert m["repeated_pair_transfers"] == 7 and m["round_trips"] == 2


def base_metrics(**kw):
    m = {"native": "ETH", "age_hours": 24.0, "unique_minters": 100, "unique_payers": 100,
         "items_minted_via_drop": 300, "items_minted_last_24h": 300, "mint_txs": 120, "mint_revenue_native": 1.0,
         "mint_price_min": 0.003, "mint_price_max": 0.003, "top_minter": A, "top_minter_share": 0.05,
         "total_supply": 300, "max_supply": 1000, "mint_progress": 0.3, "outside_drop_share": 0.0,
         "minted_onchain": 300, "burned": 0, "held_items": 300, "unique_holders": 95, "top_holder": A,
         "top_holder_share": 0.05, "secondary_transfers": 4, "round_trips": 0, "repeated_pair_transfers": 0,
         "contract_verified": True, "explorer_scam_label": False, "creator": None, "socials": None}
    m.update(kw)
    return m


def scored(**kw):
    m = base_metrics(**kw)
    m["flags"] = discovery.red_flags(m)
    return m["flags"], discovery.score_new_collection(m)


def test_red_flags_for_new_launches():
    flags, _ = scored(top_holder_share=0.6, top_minter_share=0.7, unique_minters=3, round_trips=4,
                      contract_verified=False, outside_drop_share=0.5, socials=False, creator=A)
    codes = {f["code"]: f["severity"] for f in flags}
    assert codes["single_wallet_concentration"] == "high"
    assert codes["single_wallet_minting"] == "high"
    assert codes["very_few_minters"] == "warning"
    assert codes["wash_trading_suspected"] == "high"
    assert codes["contract_source_unverified"] == "warning"
    assert codes["large_mint_outside_drop"] == "warning"
    assert codes["no_socials"] == "warning"
    assert codes["creator_is_top_minter"] == "warning"
    clean, _ = scored()
    assert {f["code"] for f in clean} == {"socials_unknown"}  # unknown socials are info, not a red flag


def test_scores_spread_and_order():
    strong = scored(unique_minters=1500, unique_holders=1400, mint_revenue_native=20.0, mint_progress=0.9,
                    socials=True)[1]["value"]
    medium = scored()[1]["value"]
    weak = scored(unique_minters=8, unique_holders=6, mint_revenue_native=0.0, mint_progress=0.02,
                  top_holder_share=0.3, contract_verified=None)[1]["value"]
    scam = scored(unique_minters=2, top_holder_share=0.9, top_minter_share=1.0, round_trips=5,
                  contract_verified=False)[1]["value"]
    assert strong > medium > weak > scam
    assert strong - scam > 50  # no bunching
    assert len({strong, medium, weak, scam}) == 4


def test_penalties_are_visible():
    flags, s = scored(round_trips=5)
    assert s["red_flag_penalty"] >= 15 and s["value"] == round(max(0, s["before_penalties"] - s["red_flag_penalty"]), 1)


class FakeRPC:
    """Chain with two new contracts and one old contract minting through SeaDrop."""

    def __init__(self, fail=None):
        self.calls = 0
        self.fail = fail or set()
        self.created = {NEW: 150, NEW2: 180}
        self.mints = ([mint_log(NEW, x, 2, 10**15, 160, f"0x{i}") for i, x in enumerate((A, B, C, D))] +
                      [mint_log(NEW2, A, 5, 0, 190, "0xn2")] +
                      [mint_log(OLD, x, 1, 10**15, 170, f"0xo{i}") for i, x in enumerate((A, B, C, D, A))])

    def head(self):
        return 1000, 1_000_000

    def block_at(self, ts, head_no, head_ts, bt):
        return 100

    def get_logs(self, address, topics, start, end):
        self.calls += 1
        if address == discovery.SEADROP:
            return self.mints
        if address == NEW:
            return [transfer(ZERO, x, i, 160 + i, i) for i, x in enumerate((A, A, B, B, C, C, D, D))]
        return [transfer(ZERO, A, i, 190, i) for i in range(5)]

    def has_code(self, addr, block):
        self.calls += 1
        if addr in self.fail:
            raise discovery.DiscoveryError("boom")
        return addr == OLD or block >= self.created.get(addr, 0)

    def creation_block(self, addr, lo, hi):
        return self.created[addr]

    def block_ts(self, n):
        return 1_000_000 - (1000 - n) * 2

    def eth_call(self, to, data):
        if data == discovery.SEL["totalSupply"]:
            return "0x" + word(8 if to == NEW else 5)
        if data == discovery.SEL["maxSupply"]:
            return "0x" + word(100)
        return None


def make(fail=None, enricher=None):
    rpc = FakeRPC(fail)
    d = discovery.Discoverer(rpc_factory=lambda chain: rpc, blockscout_fn=lambda base, a, t: {"contract_verified": True},
                             opensea_enricher=enricher)
    return d, rpc


def test_discoverer_finds_only_new_contracts_and_ranks():
    d, _ = make()
    out = d.discover("week", ["base"], 10, now=1_000_000)
    addrs = [r["contract_address"] for r in out["results"]]
    assert OLD not in addrs and set(addrs) == {NEW, NEW2}
    assert out["new_collections_found"] == 2
    assert [r["rank"] for r in out["results"]] == [1, 2]
    top = out["results"][0]
    assert top["contract_address"] == NEW  # 4 distinct minters beats 1
    m = top["metrics"]
    assert m["unique_minters"] == 4 and m["items_minted_via_drop"] == 8
    assert abs(m["mint_revenue_native"] - 0.008) < 1e-12
    assert m["mint_progress"] == 0.08 and m["unique_holders"] == 4
    assert out["results"][1]["score"]["value"] < top["score"]["value"]
    assert {f["code"] for f in out["results"][1]["red_flags"]} >= {"very_few_minters"}
    assert out["scan"][0]["collections_minting_in_window"] == 3
    assert out["limitations"] and "not financial advice" in out["disclaimer"].lower()


def test_discoverer_limit_cache_and_errors():
    d, rpc = make(fail={NEW2})
    out = d.discover("today", ["base"], 1, now=1_000_000)
    assert out["returned"] == 1 and len(out["results"]) == 1
    assert out["scan"][0]["candidate_errors"] == 1
    calls = rpc.calls
    again = d.discover("today", ["base"], 1, now=1_000_000)
    assert again["cached"] is True and rpc.calls == calls


def test_opensea_enrichment_sets_socials():
    d, _ = make(enricher=lambda chain, a: {"socials": False, "status": "provider_reported"})
    out = d.discover("month", ["base"], 5, now=1_000_000)
    assert all("no_socials" in {f["code"] for f in r["red_flags"]} for r in out["results"])


def test_rpc_get_logs_halves_range_on_provider_limit(monkeypatch):
    rpc = discovery.RPC("base", rpcs=["https://rpc.invalid"])
    seen = []

    def fake_post(url, method, params):
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        seen.append(hi - lo + 1)
        if hi - lo + 1 > 12500:
            return None, "exceed maximum block range"
        return [{"blockNumber": hex(lo)}], None
    monkeypatch.setattr(rpc, "_post", fake_post)
    logs = rpc.get_logs(discovery.SEADROP, [], 0, 49999)
    assert max(s for s in seen[-4:]) <= 12500 and len(logs) == 4


def test_rpc_all_endpoints_failing_raises(monkeypatch):
    rpc = discovery.RPC("ethereum", rpcs=["https://a.invalid", "https://b.invalid"])
    monkeypatch.setattr(rpc, "_post", lambda *a: (None, "down"))
    try:
        rpc.call("eth_blockNumber", [])
        raise AssertionError("expected DiscoveryError")
    except discovery.DiscoveryError as exc:
        assert "down" in str(exc)


# ---- API -------------------------------------------------------------------

def test_discover_endpoint_validation(client):
    assert client.get("/discover?period=year").status_code == 422
    assert client.get("/discover?limit=0").status_code == 422
    assert client.get("/discover?limit=51").status_code == 422
    assert client.get("/discover?chain=mars").status_code == 422
    assert client.post("/discover", json={"period": "decade"}).status_code == 422
    # empty probe body must fail fast (no scan), as OpenSea's `tool-sdk verify` POSTs "{}" with a 10 s timeout
    assert client.post("/discover", json={}).status_code == 422


def test_discover_endpoint_returns_ranked_results(client, monkeypatch):
    d, _ = make()
    called = {}
    original = discovery.Discoverer.discover

    def fake(period, chains, limit):
        called.update(period=period, chains=chains, limit=limit)
        return original(d, period, ["base"], limit, now=1_000_000)
    monkeypatch.setattr(discovery.Discoverer, "discover", lambda self, p, c, l: fake(p, c, l))
    r = client.get("/discover?period=week&chain=base&limit=5")
    assert r.status_code == 200, r.text
    assert called == {"period": "week", "chains": ["base"], "limit": 5}
    assert r.json()["results"][0]["rank"] == 1
    r = client.post("/discover", json={"period": "month"})
    assert r.status_code == 200 and called["chains"] == ["ethereum", "base"]


def test_discover_source_failure_is_502(client, monkeypatch):
    def boom(self, *a):
        raise discovery.DiscoveryError("all base RPC endpoints failed")
    monkeypatch.setattr(discovery.Discoverer, "discover", boom)
    r = client.get("/discover")
    assert r.status_code == 502 and "Onchain data source error" in r.json()["detail"]


def test_discover_respects_token(make_client, monkeypatch):
    monkeypatch.setattr(discovery.Discoverer, "discover", lambda self, *a: {"results": []})
    c = make_client(MINTSCOPE_API_TOKEN="tok-test")
    assert c.get("/discover").status_code == 401
    assert c.get("/discover", headers={"X-API-Token": "tok-test"}).status_code == 200


def test_discover_manifest(make_client):
    path = "/.well-known/ai-tool/mintscope-new-collections.json"
    assert make_client().get(path).status_code == 404
    env = {"MINTSCOPE_MODE": "live", "OPENSEA_API_KEY": "k-test", "MINTSCOPE_PUBLIC_BASE_URL": "https://ms.example.com",
           "MINTSCOPE_CREATOR_ADDRESS": "0x" + "ab" * 20}
    m = make_client(**env).get(path).json()
    assert m["endpoint"] == "https://ms.example.com/discover"
    assert m["inputs"]["properties"]["period"]["enum"] == ["today", "week", "month"]
    assert "ai" in m["tags"] and "nft" in m["tags"]
    assert 1 <= len(m["description"]) <= 500 and len(m["name"]) <= 128
    import re
    assert all(re.fullmatch(r"[a-z0-9-]{1,32}", x) for x in m["tags"]) and len(m["tags"]) <= 16
