"""The /register-onchain page: correct ERC-8257 calldata, no secrets, creator wallet signs."""
import register_page as rp

# Produced with viem encodeFunctionData for registerTool(uri, hash, address(0)).
VIEM_ANALYSIS = ("0xfe1d0b160000000000000000000000000000000000000000000000000000000000000060"
                 "bbd9da625c419559bb15cdf6af18a2eb8718cf273c2b061dd4a42316cef045f1"
                 + "0" * 64 + "0000000000000000000000000000000000000000000000000000000000000053"
                 "68747470733a2f2f6d696e7473636f70652d7a7276762e6f6e72656e6465722e636f6d2f2e77656c6c2d6b6e6f776e2f"
                 "61692d746f6f6c2f6d696e7473636f70652d6e66742d616e616c797369732e6a736f6e00000000000000000000000000")


def test_calldata_matches_viem():
    assert rp.tx_list()[0]["data"] == VIEM_ANALYSIS


def test_calldata_layout():
    for t in rp.tx_list():
        d = t["data"][2:]
        assert d[:8] == "fe1d0b16"
        assert d[8 + 64:8 + 128] == t["manifest_hash"][2:]
        assert int(d[8 + 128:8 + 192], 16) == 0
        n = int(d[8 + 192:8 + 256], 16)
        assert bytes.fromhex(d[8 + 256:8 + 256 + 2 * n]).decode() == t["metadata_uri"]
        assert len(d) % 64 == 8


def test_page_served(client):
    r = client.get("/register-onchain")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    assert rp.REGISTRY in body and rp.CREATOR in body
    for t in rp.tx_list():
        assert t["data"] in body
    assert "eth_sendTransaction" in body and "wallet_switchEthereumChain" in body
    for secret in ("OPENSEA_API_KEY", "private", "mnemonic", "seed", "x-api-key", "TOKEN_PRIVATE_JWK"):
        assert secret.lower() not in body.lower()


def test_tool_slugs_match_manifest_routes(client):
    import tool_manifest
    uris = [t["metadata_uri"] for t in rp.TOOLS]
    assert uris[0].endswith(f"/.well-known/ai-tool/{tool_manifest.TOOL_SLUG}.json")
    assert uris[1].endswith(f"/.well-known/ai-tool/{tool_manifest.DISCOVER_SLUG}.json")
