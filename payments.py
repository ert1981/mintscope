"""Pricing: free tier counter and x402 pay-per-report (USDC on Base), the way OpenSea-listed
ERC-8257 tools charge (mirrors @opensea/tool-sdk's hosted x402 gate):

1. A request without `X-Payment` that needs payment gets HTTP 402 with x402 v1 `accepts`
   (scheme "exact", network, maxAmountRequired in USDC base units, payTo, asset, resource ...).
2. The caller signs an EIP-3009 TransferWithAuthorization and retries with `X-Payment: base64(json)`.
3. We POST {x402Version, paymentPayload, paymentRequirements} to the facilitator's /verify.
4. The (payer, nonce) authorization is reserved once in SQLite (replay guard), the report is built,
   then the facilitator's /settle moves the USDC onchain. The paid report is returned and the revenue
   is logged ONLY after settle reports success with a transaction hash.

MintScope never holds a key or signs anything: the facilitator broadcasts the caller's signed
authorization, and USDC goes straight to MINTSCOPE_PAYEE_ADDRESS.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

import requests

PAYAI_FACILITATOR_URL = "https://facilitator.payai.network"
USDC = {
    # network: (chain id, USDC contract, EIP-712 domain name of that USDC contract, checked onchain 2026-10-29)
    "base": (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USD Coin"),
    "base-sepolia": (84532, "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "USDC"),
}
USDC_DECIMALS = 6
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
NONCE_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
REJECTED = {"0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}
FACILITATOR_TIMEOUT = 10.0


class PaymentError(Exception):
    """Payment could not be accepted. `status` is the HTTP status to return."""

    def __init__(self, status: int, error: str, accepts: list | None = None):
        super().__init__(error)
        self.status, self.error, self.accepts = status, error, accepts


def to_base_units(amount: str) -> str:
    if not re.fullmatch(r"\d+(\.\d+)?", amount):
        raise ValueError(f"invalid USDC amount: {amount}")
    if "." not in amount:
        return amount
    whole, frac = amount.split(".")
    if len(frac) > USDC_DECIMALS:
        raise ValueError("too many decimals")
    return (whole + frac.ljust(USDC_DECIMALS, "0")).lstrip("0") or "0"


def valid_payee(address: str | None) -> bool:
    return bool(address) and bool(ADDRESS_RE.match(address)) and address.lower() not in REJECTED


@dataclass
class X402Config:
    payee: str
    price_usdc: str = "2.99"
    network: str = "base"
    facilitator_url: str = PAYAI_FACILITATOR_URL
    max_timeout_seconds: int = 300

    @property
    def amount(self) -> str:
        return to_base_units(self.price_usdc)

    @property
    def asset(self) -> str:
        return USDC[self.network][1]

    def pricing(self) -> list[dict]:
        """Manifest `pricing`, identical to @opensea/tool-sdk x402UsdcPricing()."""
        chain_id, asset, _ = USDC[self.network]
        return [{"amount": self.amount, "asset": f"eip155:{chain_id}/erc20:{asset}",
                 "recipient": f"eip155:{chain_id}:{self.payee}", "protocol": "x402"}]

    def requirements(self, resource: str, description: str) -> dict:
        return {
            "scheme": "exact", "network": self.network, "maxAmountRequired": self.amount,
            "resource": resource, "description": description, "mimeType": "application/json",
            "payTo": self.payee, "maxTimeoutSeconds": self.max_timeout_seconds, "asset": self.asset,
            "extra": {"name": USDC[self.network][2], "version": "2"},
        }


def challenge(requirements: dict, error: str = "X-PAYMENT header is required") -> dict:
    return {"x402Version": 1, "error": error, "accepts": [requirements]}


def decode_payment(header: str) -> dict:
    try:
        payload = json.loads(base64.b64decode(header, validate=False).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PaymentError(402, "invalid_payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
        raise PaymentError(402, "invalid_payload")
    return payload


def replay_key(payment: dict, network: str, asset: str) -> str | None:
    """Same (payer, nonce) identity as the SDK's authorizationReplayKey()."""
    auth = (payment.get("payload") or {}).get("authorization") or {}
    frm, nonce = auth.get("from"), auth.get("nonce")
    if not (isinstance(frm, str) and isinstance(nonce, str) and ADDRESS_RE.match(frm) and NONCE_RE.match(nonce)):
        return None
    return f"x402:{network}:{asset}:{frm.lower()}:{nonce.lower()}"


class Facilitator:
    def __init__(self, url: str, timeout: float = FACILITATOR_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payment: dict, requirements: dict) -> requests.Response:
        return requests.post(f"{self.url}{path}", timeout=self.timeout, json={
            "x402Version": payment.get("x402Version", 1), "paymentPayload": payment,
            "paymentRequirements": requirements})

    def verify(self, payment: dict, requirements: dict) -> dict:
        try:
            res = self._post("/verify", payment, requirements)
        except requests.RequestException:
            raise PaymentError(502, "payment_facilitator_unreachable")
        if res.status_code != 200:
            raise PaymentError(502, "payment_facilitator_unreachable")
        try:
            data = res.json()
        except ValueError:
            raise PaymentError(502, "payment_facilitator_bad_response")
        if not isinstance(data, dict) or data.get("isValid") is not True:
            reason = data.get("invalidReason") if isinstance(data, dict) else None
            raise PaymentError(402, str(reason or "invalid_payment")[:120])
        return data

    def settle(self, payment: dict, requirements: dict) -> tuple[dict | None, bool]:
        """Returns (settle response or None, retryable). retryable=True means the authorization
        was provably not broadcast (network error or 429), so the replay reservation may be released."""
        try:
            res = self._post("/settle", payment, requirements)
        except requests.RequestException:
            return None, True
        if res.status_code != 200:
            return None, res.status_code == 429
        try:
            data = res.json()
        except ValueError:
            return None, False
        if not isinstance(data, dict) or data.get("success") is not True or not data.get("transaction"):
            return None, False
        return data, False


def payment_response_header(settled: dict, network: str) -> str:
    body = {"success": True, "transaction": settled.get("transaction"), "network": settled.get("network", network),
            "payer": settled.get("payer")}
    return base64.b64encode(json.dumps(body).encode()).decode()
