"""Pro license: offline Ed25519 verification (valid / invalid / tampered) and the admin signing script."""
import base64
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import licensing

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = {"v": 1, "product": "mintscope", "tier": "pro", "lid": "L-0001", "name": "Test Buyer",
           "issued": "2026-10-29", "edition": "intro"}


def keypair():
    priv = Ed25519PrivateKey.generate()  # throwaway key, lives only in memory during the test
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, base64.b64encode(raw).decode()


def test_valid_license_verifies():
    priv, pub = keypair()
    lic = licensing.verify(licensing.sign(PAYLOAD, priv), licensing.public_key(pub))
    assert lic.tier == "pro" and lic.lid == "L-0001" and set(lic.features) == {"compare", "bulk_export"}


def test_license_from_other_key_is_invalid():
    priv, _ = keypair()
    _, other_pub = keypair()
    with pytest.raises(licensing.LicenseError, match="signature"):
        licensing.verify(licensing.sign(PAYLOAD, priv), licensing.public_key(other_pub))


def test_tampered_license_is_rejected():
    priv, pub = keypair()
    text = licensing.sign(PAYLOAD, priv)
    prefix, body, sig = text.split(".")
    forged = licensing.b64e(json.dumps({**PAYLOAD, "tier": "developer", "lid": "L-9999"}).encode())
    for bad in (f"{prefix}.{forged}.{sig}", f"{prefix}.{body}.{sig[:-4]}AAAA", f"MSL2.{body}.{sig}",
                "garbage", "", f"{prefix}.{body}"):
        with pytest.raises(licensing.LicenseError):
            licensing.verify(bad, licensing.public_key(pub))


def test_wrong_product_or_tier_rejected():
    priv, pub = keypair()
    for payload in ({**PAYLOAD, "product": "other"}, {**PAYLOAD, "tier": "enterprise"}, {**PAYLOAD, "v": 2}):
        with pytest.raises(licensing.LicenseError):
            licensing.verify(licensing.sign(payload, priv), licensing.public_key(pub))


def test_unconfigured_build_has_no_pro(monkeypatch):
    import license_pubkey
    monkeypatch.setattr(license_pubkey, "PUBLIC_KEY_B64", "")
    with pytest.raises(licensing.LicenseError, match="not configured"):
        licensing.verify("MSL1.a.b")


def _admin_module():
    spec = importlib.util.spec_from_file_location("license_admin", ROOT / "admin" / "license_admin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_admin_keygen_and_sign(tmp_path, capsys, monkeypatch):
    admin = _admin_module()
    module = tmp_path / "license_pubkey.py"
    module.write_text('"""x"""\nPUBLIC_KEY_B64 = ""\n')
    key_file = tmp_path / "keys" / "license.pem"
    pub = admin.keygen(key_file, module=module)
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert f'PUBLIC_KEY_B64 = "{pub}"' in module.read_text()
    assert "PRIVATE KEY" not in module.read_text()
    with pytest.raises(SystemExit):
        admin.keygen(key_file, module=module)  # refuses to overwrite
    with pytest.raises(SystemExit):
        admin.keygen(ROOT / "leak.pem", module=module)  # refuses a path inside the project
    assert not (ROOT / "leak.pem").exists()
    capsys.readouterr()
    admin.main(["sign", "--key", str(key_file), "--lid", "L-42", "--name", "Buyer"])
    out = capsys.readouterr().out.strip()
    assert "PRIVATE" not in out and key_file.read_text().split("\n")[1] not in out
    lic = licensing.verify(out, licensing.public_key(pub))
    assert lic.lid == "L-42" and lic.tier == "pro"


def test_admin_cli_never_prints_private_key(tmp_path, capsys, monkeypatch):
    admin = _admin_module()
    module = tmp_path / "license_pubkey.py"
    module.write_text('PUBLIC_KEY_B64 = ""\n')
    monkeypatch.setattr(admin, "ROOT", ROOT)
    key_file = tmp_path / "k.pem"
    monkeypatch.setattr(admin, "write_pubkey_module", lambda pub, module=module: None)
    admin.main(["keygen", "--out", str(key_file)])
    out = capsys.readouterr().out
    body = "".join(key_file.read_text().splitlines()[1:-1])
    assert body not in out and "BEGIN PRIVATE KEY" not in out


def test_no_private_key_in_project():
    for p in ROOT.rglob("*"):
        if {".venv", "build", "dist", "__pycache__"} & set(p.parts) or not p.is_file() or p.suffix in (".db", ".pyc"):
            continue
        assert "BEGIN PRIVATE KEY" not in p.read_text(errors="ignore") or p.name == "test_license.py"
