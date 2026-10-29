"""The /deactivate-onchain page: deregisterTool calldata for duplicates 816/817 only, no secrets."""
import pytest
import deactivate_page as dp


def test_selector_and_topic():
    # keccak256 values computed with eth_utils / viem against the @opensea/tool-sdk 0.31.1 ABI
    assert dp.DEREGISTER_SELECTOR == "9148e6ca"  # deregisterTool(uint256)
    assert dp.GET_CONFIG_SELECTOR == "a0178453"  # getToolConfig(uint256)
    assert dp.TOOL_DEREGISTERED_TOPIC == "0x9add33e854e243f868ff7cacac076d65b1d56fb593e2bb891e76e2e8d5ddd034"
    assert dp.REGISTRY == "0x265BB2DBFC0A8165C9A1941Eb1372F349baD2cf1"


def test_calldata_matches_viem():
    # viem encodeFunctionData({functionName: "deregisterTool", args: [816n / 817n]})
    data = {t["id"]: t["data"] for t in dp.tx_list()}
    assert data == {816: "0x9148e6ca" + "0" * 61 + "330", 817: "0x9148e6ca" + "0" * 61 + "331"}


def test_kept_tools_cannot_be_encoded():
    for kid in (814, 815):
        with pytest.raises(ValueError):
            dp.encode_deregister(kid)
    assert {t["id"] for t in dp.tx_list()} == {816, 817}


def test_page_served(client):
    r = client.get("/deactivate-onchain")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    assert dp.REGISTRY in body and dp.CREATOR in body
    for t in dp.tx_list():
        assert t["data"] in body and t["cfg"] in body
    assert "0x9148e6ca" + "0" * 61 + "32e" not in body  # 814
    assert "0x9148e6ca" + "0" * 61 + "32f" not in body  # 815
    assert "eth_sendTransaction" in body and "eth_estimateGas" in body and "0x2105" in body
    for secret in ("OPENSEA_API_KEY", "private", "mnemonic", "seed", "x-api-key", "TOKEN_PRIVATE_JWK"):
        assert secret.lower() not in body.lower()
