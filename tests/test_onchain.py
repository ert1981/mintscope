"""Keyless onchain analysis helpers: input parsing, wash-pattern filtering, safety score."""
import pytest

import onchain

A = "0x" + "ab" * 20


def topic(addr):
    return "0x" + "0" * 24 + addr[2:]


def tlog(frm, to, tid, block):
    return {"blockNumber": hex(block), "logIndex": "0x0",
            "topics": [onchain.TRANSFER, topic(frm), topic(to), hex(tid)]}


@pytest.mark.parametrize("text,kind,chain", [
    (A, "contract", "ethereum"),
    (f"https://opensea.io/item/base/{A}/7", "contract", "base"),
    (f"https://opensea.io/assets/ethereum/{A}/1", "contract", "ethereum"),
    (f"https://etherscan.io/token/{A}", "contract", "ethereum"),
    (f"https://basescan.org/address/{A}", "contract", "base"),
    ("https://opensea.io/collection/pudgypenguins", "slug", "ethereum"),
    ("pudgypenguins", "slug", "ethereum"),
])
def test_parse_input(text, kind, chain):
    p = onchain.parse_input(text)
    assert p["kind"] == kind and p["chain"] == chain


@pytest.mark.parametrize("bad", ["", "https://evil.example/0xabc", "not a valid input!!"])
def test_parse_input_rejects(bad):
    with pytest.raises(onchain.InputError):
        onchain.parse_input(bad)


def test_wash_patterns_exclude_contract_counterparties():
    w1, w2, escrow = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    logs = [tlog(w1, w2, 1, 10), tlog(w2, w1, 1, 11),          # wallet round trip
            tlog(w1, escrow, 2, 12), tlog(escrow, w1, 2, 13)]  # staking/escrow round trip
    res = onchain.wash_patterns(logs, lambda a: a == escrow)
    assert res["round_trips"] == 1 and res["excluded_contract_counterparties"] == 1


def test_safety_score_penalizes_scam_label_and_rewards_verification():
    base = {"standard": "erc721", "holders": 3000, "total_supply": 5000, "unique_wallets_7d": 200,
            "round_trips": 0, "repeated_pair_transfers": 0, "secondary_transfers_7d": 100}
    good = {**base, "contract_verified": True, "explorer_scam_label": False}
    bad = {**base, "contract_verified": False, "explorer_scam_label": True}
    g = onchain.safety_score(good, onchain.safety_flags(good))["value"]
    b = onchain.safety_score(bad, onchain.safety_flags(bad))["value"]
    assert g > 80 and b < g - 30
    assert {f["code"] for f in onchain.safety_flags(bad)} >= {"explorer_scam_label", "contract_source_unverified"}


def test_unsupported_chain_needs_key():
    with pytest.raises(onchain.InputError):
        onchain.analyze_contract("polygon", A)
