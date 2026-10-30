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

x402 v2 (agentic.market / CDP Bazaar discovery): the same 402 also carries a base64 `PAYMENT-REQUIRED`
header (x402Version 2, CAIP-2 network, `resource` info with iconUrl, `extensions.bazaar` metadata), and a v2
client may pay with the `PAYMENT-SIGNATURE` header. The v1 JSON body is unchanged.

Facilitator: PayAI by default (what the live OpenSea tools use). Only when BOTH CDP_API_KEY_ID and
CDP_API_KEY_SECRET are set in the environment is the CDP Facilitator used (needed for Bazaar indexing).
The CDP key values are read from the environment at startup and never stored in Settings, logged or returned.
"""
from __future__ import annotations

import base64
import json
import logging
import os
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
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


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

    @property
    def caip2(self) -> str:
        return f"eip155:{USDC[self.network][0]}"

    def requirements_v2(self) -> dict:
        """x402 v2 PaymentRequirements (CAIP-2 network, `amount` instead of `maxAmountRequired`)."""
        return {
            "scheme": "exact", "network": self.caip2, "asset": self.asset, "amount": self.amount,
            "payTo": self.payee, "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": {"name": USDC[self.network][2], "version": "2"},
        }


def bazaar_body_extension(example_body: dict, body_schema: dict, output_example: dict,
                          output_schema: dict | None = None) -> dict:
    """`extensions.bazaar` for a JSON-body POST route, in the shape produced by
    x402.extensions.bazaar.declare_discovery_extension(body_type="json") plus the route method."""
    example = {"type": "json", "example": output_example}
    out_schema = {"type": "object", "properties": {"type": {"type": "string"},
                                                   "example": {"type": "object", **(output_schema or {})}},
                  "required": ["type"]}
    return {"bazaar": {
        "info": {"input": {"type": "http", "method": "POST", "bodyType": "json", "body": example_body},
                 "output": example},
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": body_schema,
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": out_schema,
            },
            "required": ["input"],
        },
    }}


def payment_required_v2(x402: "X402Config", resource: dict, error: str, extensions: dict | None) -> dict:
    body = {"x402Version": 2, "error": error, "resource": resource, "accepts": [x402.requirements_v2()]}
    if extensions:
        body["extensions"] = extensions
    return body


def encode_header(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def challenge(requirements: dict, error: str = "X-PAYMENT header is required") -> dict:
    return {"x402Version": 1, "error": error, "accepts": [requirements]}


def decode_payment(header) -> dict:
    """Base64 JSON header value (HTTP) or an already-decoded object (MCP `_meta["x402/payment"]`)."""
    if isinstance(header, dict):
        payload = header
    else:
        try:
            payload = json.loads(base64.b64decode(header, validate=False).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, TypeError):
            raise PaymentError(402, "invalid_payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
        raise PaymentError(402, "invalid_payload")
    if payload.get("x402Version") == 2 and not isinstance(payload.get("accepted"), dict):
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
    """PayAI (or any plain HTTP facilitator without auth). Unchanged behavior for v1 payments."""
    kind = "payai"

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


class CdpFacilitator:
    """CDP Facilitator via the official `x402` package + `cdp-sdk` auth headers (JWT per request).
    Same verify()/settle() contract as Facilitator."""
    kind = "cdp"

    def __init__(self, api_key_id: str, api_key_secret: str, timeout: float = FACILITATOR_TIMEOUT):
        from cdp.x402 import create_facilitator_config  # imported only when CDP keys are configured
        from x402.http import HTTPFacilitatorClientSync
        cfg = create_facilitator_config(api_key_id, api_key_secret)
        try:
            from x402.http import FacilitatorConfig, CreateHeadersAuthProvider
            self.client = HTTPFacilitatorClientSync(FacilitatorConfig(
                url=cfg["url"], auth_provider=CreateHeadersAuthProvider(cfg["create_headers"]), timeout=timeout))
        except ImportError:  # older x402 releases accept the plain dict config
            self.client = HTTPFacilitatorClientSync(cfg)
        self.url = cfg["url"]

    @staticmethod
    def _bytes(obj: dict) -> bytes:
        return json.dumps(obj).encode()

    def verify(self, payment: dict, requirements: dict) -> dict:
        import httpx
        try:
            res = self.client.verify_from_bytes(self._bytes(payment), self._bytes(requirements))
        except (httpx.HTTPError, ValueError):
            raise PaymentError(502, "payment_facilitator_unreachable")
        data = res.model_dump(by_alias=True)
        if data.get("isValid") is not True:
            raise PaymentError(402, str(data.get("invalidReason") or "invalid_payment")[:120])
        return data

    def settle(self, payment: dict, requirements: dict) -> tuple[dict | None, bool]:
        import httpx
        try:
            res = self.client.settle_from_bytes(self._bytes(payment), self._bytes(requirements))
        except httpx.TransportError:
            return None, True
        except (httpx.HTTPError, ValueError):
            return None, False
        data = res.model_dump(by_alias=True)
        if data.get("success") is not True or not data.get("transaction"):
            return None, False
        return data, False


def with_discovery(payment: dict, resource: dict, extensions: dict | None) -> dict:
    """CDP Bazaar indexing needs paymentPayload.resource and paymentPayload.extensions.bazaar on settle.
    Fields the client already sent are kept; the signed authorization is not touched."""
    out = dict(payment)
    if out.get("resource") is None:
        out["resource"] = resource
    bazaar = (extensions or {}).get("bazaar")
    if bazaar is not None:
        ext = dict(out["extensions"]) if isinstance(out.get("extensions"), dict) else {}
        ext.setdefault("bazaar", bazaar)
        out["extensions"] = ext
    return out


def make_facilitator(settings_url: str, env=None):
    """CDP only when BOTH key env vars are set; otherwise the configured (PayAI) facilitator, unchanged."""
    env = os.environ if env is None else env
    key_id = (env.get("CDP_API_KEY_ID") or "").strip()
    key_secret = (env.get("CDP_API_KEY_SECRET") or "").strip()
    if key_id and key_secret:
        try:
            return CdpFacilitator(key_id, key_secret)
        except ImportError:
            # Packages missing: keep serving with the default facilitator rather than failing to start.
            logging.getLogger("mintscope").warning("CDP keys set but x402/cdp-sdk not installed; using %s",
                                                   settings_url)
    return Facilitator(settings_url)
