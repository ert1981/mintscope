"""ERC-8257 tool manifest, so the /analyze endpoint can be registered on OpenSea Tools.

Spec: https://docs.opensea.io/docs/tool-manifest
The manifest must be served from the same origin (scheme, host, port) as the endpoint,
and its keccak256 hash is what gets registered onchain, so its content must stay stable.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from config import Settings
import payments
from providers import CHAINS

TOOL_SLUG = "mintscope-nft-analysis"
DISCOVER_SLUG = "mintscope-new-collections"
MANIFEST_TYPE = "https://ercs.ethereum.org/ERCS/erc-8257#tool-manifest-v1"
TOOL_VERSION = "0.7.0"
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")

INPUTS = {
    "type": "object",
    "required": ["identifier"],
    "additionalProperties": False,
    "properties": {
        "identifier": {
            "type": "string",
            "maxLength": 100,
            "description": "OpenSea collection slug (e.g. 'boredapeyachtclub') or 0x-prefixed contract address",
        },
        "chain": {"type": "string", "default": "ethereum", "enum": sorted(CHAINS),
                  "description": "Chain of the contract (only used for contract addresses)"},
        "tier": {"type": "string", "enum": ["auto", "free", "full"], "default": "auto",
                 "description": "free = overall score + short summary (limited per month); full = paid report "
                                "(x402, USDC on Base); auto = full when an X-Payment header is sent"},
    },
}

OUTPUTS = {
    "type": "object",
    "description": "Evidence-based report. Unknown values are null with status 'unknown'. Not a profit prediction.",
    "properties": {
        "report_id": {"type": "string"},
        "tier": {"type": "string", "enum": ["free", "full"]},
        "free_analyses_left": {"type": "integer"},
        "payment": {"type": "object", "description": "Settled x402 payment (full tier)"},
        "overall_score": {"type": "object", "description": "Free tier"},
        "summary": {"type": "string", "description": "Free tier"},
        "weaknesses": {"type": "array", "description": "Full tier"},
        "data_mode": {"type": "string", "enum": ["demo", "live"]},
        "project": {"type": "object"},
        "market": {"type": "object"},
        "creator_evidence": {"type": "object"},
        "price_unit": {"type": ["string", "null"]},
        "volume_unit": {"type": ["string", "null"]},
        "risk_flags": {"type": "array"},
        "missing_data": {"type": "array"},
        "score": {"type": "object"},
        "confidence": {"type": "object"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "analyzed_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}


DISCOVER_INPUTS = {
    "type": "object",
    "required": ["period"],
    "additionalProperties": False,
    "properties": {
        "period": {"type": "string", "enum": ["today", "week", "month"],
                   "description": "Launch window: today = last 24 h, week = 7 days, month = 30 days"},
        "chain": {"type": "string", "enum": ["all", "ethereum", "base"], "default": "all"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "tier": {"type": "string", "enum": ["auto", "free", "full"], "default": "auto",
                 "description": "free = overall score + short summary (limited per month); full = paid report "
                                "(x402, USDC on Base); auto = full when an X-Payment header is sent"},
    },
}

DISCOVER_OUTPUTS = {
    "type": "object",
    "description": "Ranked NEW collections (contract deployed inside the window) with onchain metrics, red flags "
                   "and an early-momentum score. Not a profit prediction.",
    "properties": {
        "period": {"type": "string"},
        "tier": {"type": "string", "enum": ["free", "full"]},
        "free_analyses_left": {"type": "integer"},
        "payment": {"type": "object", "description": "Settled x402 payment (full tier)"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "new_collections_found": {"type": "integer"},
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "rank": {"type": "integer"},
            "contract_address": {"type": "string"},
            "chain": {"type": "string"},
            "name": {"type": ["string", "null"]},
            "created_at": {"type": "string"},
            "metrics": {"type": "object"},
            "red_flags": {"type": "array"},
            "score": {"type": "object"},
        }}},
        "scan": {"type": "array"},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "disclaimer": {"type": "string"},
    },
}


class ManifestNotConfigured(Exception):
    pass


def _check(settings: Settings) -> tuple[str, str]:
    base = settings.public_base_url
    creator = settings.creator_address
    if not base or not creator:
        raise ManifestNotConfigured(
            "Tool manifest is not configured: set MINTSCOPE_PUBLIC_BASE_URL and MINTSCOPE_CREATOR_ADDRESS.")
    if settings.mode != "live":
        raise ManifestNotConfigured("Tool manifest requires MINTSCOPE_MODE=live (and an OPENSEA_API_KEY).")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ManifestNotConfigured("MINTSCOPE_PUBLIC_BASE_URL must be a bare https:// origin.")
    if not EVM_ADDRESS_RE.match(creator):
        raise ManifestNotConfigured("MINTSCOPE_CREATOR_ADDRESS must be a 0x + 40 hex lowercase address.")
    return base, creator


def _images(settings: Settings, manifest: dict) -> dict:
    # Price declared the way @opensea/tool-sdk's x402UsdcPricing() does, when the paid tier is on.
    if settings.paid_enabled:
        manifest["pricing"] = payments.X402Config(
            payee=settings.payee_address.lower(), price_usdc=settings.price_usdc,
            network=settings.x402_network).pricing()
    if settings.image_url:
        manifest["image"] = settings.image_url
    if settings.featured_image_url:
        manifest["featuredImage"] = settings.featured_image_url
    return manifest


def build_discover_manifest(settings: Settings) -> dict:
    base, creator = _check(settings)
    return _images(settings, {
        "type": MANIFEST_TYPE,
        "name": "MintScope New NFT Launches",
        "description": (
            "Finds NFT collections launched today, this week or this month (OpenSea Drops on Ethereum and Base), "
            "reads mints, holders and transfers onchain, flags wash trading, wallet concentration and unverified "
            "contracts, and ranks them by early momentum. Never predicts or guarantees profit."
        ),
        "version": TOOL_VERSION,
        "endpoint": f"{base}/discover",
        "inputs": DISCOVER_INPUTS,
        "outputs": DISCOVER_OUTPUTS,
        "creatorAddress": creator,
        "tags": ["ai", "nft", "security", "trading", "analytics", "new-mints", "discovery"],
        "verifiability": {
            "tier": "self-attested",
            "execution": "server",
            "description": "Reads public onchain data (SeaDrop mint events, contract state, Transfer logs) through "
                           "public RPC endpoints, plus Blockscout and optionally the OpenSea API.",
            "dataRetention": "none",
        },
    })


def build_manifest(settings: Settings) -> dict:
    base, creator = _check(settings)
    manifest = {
        "type": MANIFEST_TYPE,
        "name": "MintScope NFT Collection Analysis",
        "description": (
            "Evidence-based report for an NFT collection: verified market data, realized sales vs. listings, "
            "creator evidence, risk flags, missing data and a confidence level. Never predicts or guarantees profit."
        ),
        "version": TOOL_VERSION,
        "endpoint": f"{base}/analyze",
        "inputs": INPUTS,
        "outputs": OUTPUTS,
        "creatorAddress": creator,
        "tags": ["ai", "nft", "security", "analytics", "trading"],
        "verifiability": {
            "tier": "self-attested",
            "execution": "server",
            "description": "Reads public OpenSea API v2 data; every value is labeled provider_reported, "
                           "derived_from_provider or unknown, with source URLs.",
            "dataRetention": "full",
        },
    }
    # Optional display images (square icon, 16:9 banner for the featured section).
    return _images(settings, manifest)
