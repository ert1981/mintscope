"""MintScope Pro licenses: one-time purchase, verified offline with an Ed25519 signature.

License format:  MSL1.<base64url(JSON payload)>.<base64url(signature)>
The signature covers the ASCII bytes "MSL1.<payload part>". The payload looks like
{"v":1,"product":"mintscope","tier":"pro","lid":"L-0001","name":"Buyer","issued":"2026-10-29","edition":"intro"}
Only the public key ships with the app (license_pubkey.py); the private key never leaves the owner's machine.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import license_pubkey

PREFIX = "MSL1"
PRO_FEATURES = ("compare", "bulk_export")
TIER_FEATURES = {"pro": PRO_FEATURES, "developer": PRO_FEATURES}
PRICING = {"pro_intro_usd": 9, "pro_usd": 19,
           "note": "One-time purchase. Intro price $9 for the first buyers, then $19. Sold through a store page "
                   "(not live yet). You still pay your own API usage."}


class LicenseError(ValueError):
    pass


@dataclass(frozen=True)
class License:
    tier: str
    lid: str
    name: str | None
    issued: str | None
    edition: str | None

    @property
    def features(self) -> tuple[str, ...]:
        return TIER_FEATURES.get(self.tier, ())

    def public(self) -> dict:
        return {"tier": self.tier, "license_id": self.lid, "name": self.name, "issued": self.issued,
                "edition": self.edition, "features": list(self.features)}


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def public_key(b64: str | None = None) -> Ed25519PublicKey | None:
    value = license_pubkey.PUBLIC_KEY_B64 if b64 is None else b64
    if not value:
        return None
    raw = base64.b64decode(value)
    if len(raw) != 32:
        raise LicenseError("Embedded license public key is malformed.")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify(text: str, key: Ed25519PublicKey | None = None) -> License:
    key = key or public_key()
    if key is None:
        raise LicenseError("Pro licensing is not configured in this build (no public key embedded).")
    parts = (text or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("Not a MintScope license (expected MSL1.<payload>.<signature>).")
    try:
        sig = _b64d(parts[2])
        key.verify(sig, f"{parts[0]}.{parts[1]}".encode())
    except (InvalidSignature, ValueError):
        raise LicenseError("License signature is invalid (tampered or not issued by MintScope).")
    try:
        payload = json.loads(_b64d(parts[1]))
    except ValueError:
        raise LicenseError("License payload is unreadable.")
    if not isinstance(payload, dict) or payload.get("product") != "mintscope" or payload.get("v") != 1:
        raise LicenseError("License is for a different product or version.")
    tier = payload.get("tier")
    if tier not in TIER_FEATURES:
        raise LicenseError("Unknown license tier.")
    return License(tier=tier, lid=str(payload.get("lid") or ""), name=payload.get("name"),
                   issued=payload.get("issued"), edition=payload.get("edition"))


def sign(payload: dict, private_key) -> str:
    """Used only by admin/license_admin.py (and tests with throwaway keys)."""
    body = b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = private_key.sign(f"{PREFIX}.{body}".encode())
    return f"{PREFIX}.{body}.{b64e(sig)}"
