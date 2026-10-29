#!/usr/bin/env python3
"""Owner-only tool: create the license signing keypair and sign MintScope Pro licenses.

    python admin/license_admin.py keygen  [--out ~/.mintscope-admin/license_ed25519.pem]
    python admin/license_admin.py sign    --lid L-0001 --name "Buyer" [--tier pro] [--edition intro]
    python admin/license_admin.py pubkey

- The private key is written ONLY to a file outside this project folder (default ~/.mintscope-admin/),
  with permission 600. It is never printed. Back it up offline; if it leaks, anyone can mint licenses.
- keygen writes the matching PUBLIC key into license_pubkey.py (safe to commit and ship).
- Licenses are sold through a store page later; this script only signs them.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import licensing  # noqa: E402

DEFAULT_KEY = Path.home() / ".mintscope-admin" / "license_ed25519.pem"


def _outside_project(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        sys.exit("Refusing: the private key must be stored outside the MintScope project folder.")
    return path


def _pub_b64(priv: Ed25519PrivateKey) -> str:
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _load(path: Path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def write_pubkey_module(pub: str, module: Path = ROOT / "license_pubkey.py") -> None:
    text = module.read_text()
    lines = [f'PUBLIC_KEY_B64 = "{pub}"' if l.startswith("PUBLIC_KEY_B64") else l for l in text.splitlines()]
    module.write_text("\n".join(lines) + "\n")


def keygen(out: Path, force: bool = False, module: Path = ROOT / "license_pubkey.py") -> str:
    out = _outside_project(out)
    if out.exists() and not force:
        sys.exit(f"Refusing to overwrite existing key file {out} (use --force only if you mean it).")
    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    pub = _pub_b64(priv)
    write_pubkey_module(pub, module)
    return pub


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", type=Path, default=DEFAULT_KEY)
    k.add_argument("--force", action="store_true")
    s = sub.add_parser("sign")
    s.add_argument("--key", type=Path, default=DEFAULT_KEY)
    s.add_argument("--lid", required=True)
    s.add_argument("--name")
    s.add_argument("--tier", default="pro", choices=sorted(licensing.TIER_FEATURES))
    s.add_argument("--edition", default="intro", choices=["intro", "standard"])
    p = sub.add_parser("pubkey")
    p.add_argument("--key", type=Path, default=DEFAULT_KEY)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        pub = keygen(a.out, a.force)
        print(f"Private key written to {a.out.expanduser()} (permission 600, not printed).")
        print(f"Public key embedded in license_pubkey.py: {pub}")
    elif a.cmd == "pubkey":
        print(_pub_b64(_load(_outside_project(a.key))))
    else:
        priv = _load(_outside_project(a.key))
        payload = {"v": 1, "product": "mintscope", "tier": a.tier, "lid": a.lid, "name": a.name,
                   "issued": date.today().isoformat(), "edition": a.edition}
        print(licensing.sign(payload, priv))


if __name__ == "__main__":
    main()
