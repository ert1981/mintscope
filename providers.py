"""Data providers. Each returns raw facts; missing facts are None, never guessed.

- DemoProvider: deterministic SYNTHETIC data, for testing the pipeline only.
- OpenSeaProvider: live data from the official OpenSea API v2 (needs an API key).
"""
from __future__ import annotations

import hashlib
import random
import math
import re
from urllib.parse import quote

import requests

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,99}$")
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
# Chain slugs accepted by the OpenSea API v2 `ChainIdentifier` enum
# (https://api.opensea.io/api/v2/openapi.json, checked 2026-10-29).
CHAINS = {
    "ethereum", "base", "polygon", "arbitrum", "optimism", "zora", "avalanche", "solana", "sei", "ape_chain",
    "flow", "soneium", "ronin", "bera_chain", "shape", "unichain", "gunzilla", "abstract", "animechain",
    "hyperevm", "somnia", "monad", "hyperliquid", "megaeth", "ink", "robinhood", "stablechain",
}
# Contract lookups use 0x addresses, so they only make sense on EVM chains.
NON_EVM_CHAINS = {"solana"}

FIELDS = [
    "name", "description", "mint_price", "floor_price", "price_symbol", "total_sales",
    "total_volume", "average_sale_price", "sales_7d", "volume_7d", "num_owners", "total_supply",
    "created_date", "safelist_status", "contract_address", "chain", "contract_verified",
    "website", "twitter", "discord", "creator_address", "volume_symbol", "unique_item_count",
    "no_active_listing",
]
# Fields computed by MintScope from provider values (not reported directly by the provider).
DERIVED = "_derived"


class ProviderError(Exception):
    """Upstream provider failed (5xx, bad payload, network)."""
    status_code = 502


class ProviderTimeout(ProviderError):
    status_code = 504


class ProviderAuthError(ProviderError):
    status_code = 502


class ProviderRateLimited(ProviderError):
    status_code = 429


class NotFound(Exception):
    """Identifier is well-formed but the provider does not know it."""


def classify_identifier(identifier: str) -> str:
    if ADDRESS_RE.match(identifier):
        return "contract"
    if SLUG_RE.match(identifier) and not identifier.lower().startswith("0x"):
        return "slug"
    raise ValueError(
        "identifier must be an OpenSea collection slug (lowercase letters, digits, '-' or '_', 2-100 chars) "
        "or a 0x-prefixed 40-hex-character contract address"
    )


def empty_fields() -> dict:
    f = {k: None for k in FIELDS}
    f[DERIVED] = {}
    return f


def normalize_identifier(identifier: str) -> str:
    """Trim and lowercase. OpenSea slugs are lowercase and EVM addresses are case-insensitive,
    so the same collection always maps to the same identifier (and the same cached/demo result)."""
    return identifier.strip().lower()


class DemoProvider:
    name = "demo"

    def fetch(self, identifier: str, chain: str) -> dict:
        seed = int(hashlib.sha256(f"{identifier}:{chain}".encode()).hexdigest(), 16)
        rng = random.Random(seed)
        f = empty_fields()
        f["name"] = f"[DEMO] Synthetic collection '{identifier}'"
        f["chain"] = chain
        if classify_identifier(identifier) == "contract":
            f["contract_address"] = identifier.lower()
        else:
            f["contract_address"] = "0x" + hashlib.sha256(identifier.encode()).hexdigest()[:40]

        # "demo-sparse*" identifiers simulate a collection with very little public data.
        if not identifier.startswith("demo-sparse"):
            supply = rng.choice([500, 1000, 3333, 5000, 10000])
            mint = rng.choice([0.0, 0.01, 0.03, 0.05, 0.08])
            floor = round(rng.uniform(0.002, 0.15), 4)
            sales = rng.randint(0, 4000)
            avg = round(rng.uniform(0.005, 0.12), 4)
            f.update(
                description="Synthetic demo data generated locally. Not a real project.",
                mint_price=mint,
                floor_price=floor,
                price_symbol="ETH",
                volume_symbol="ETH",
                total_sales=sales,
                average_sale_price=avg if sales else None,
                total_volume=round(sales * avg, 4),
                sales_7d=rng.randint(0, 60),
                volume_7d=round(rng.uniform(0, 3), 4),
                total_supply=supply,
                num_owners=rng.randint(max(1, supply // 10), supply),
                created_date="2026-01-01",
                safelist_status=rng.choice(["verified", "approved", "not_requested"]),
                contract_verified=rng.choice([True, False]),
                website=rng.choice([None, "https://example.invalid/demo"]),
                twitter=rng.choice([None, "demo_project"]),
            )
        return {"provider": self.name, "fields": f, "sources": [], "requests_made": 0}


class OpenSeaProvider:
    name = "opensea"

    def __init__(self, api_key: str, base_url: str, timeout: float):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_made = 0

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        self.requests_made += 1
        try:
            resp = requests.get(
                url, timeout=self.timeout,
                # No key = OpenSea's public (keyless) access, which some read endpoints currently allow.
                headers={"x-api-key": self.api_key, "accept": "application/json"} if self.api_key
                else {"accept": "application/json"}
            )
        except requests.Timeout as exc:
            raise ProviderTimeout(f"OpenSea did not respond within {self.timeout}s") from exc
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach OpenSea: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            raise NotFound(path)
        if resp.status_code in (401, 403):
            raise ProviderAuthError("OpenSea rejected the API key (expired or invalid?)")
        if resp.status_code == 429:
            raise ProviderRateLimited("OpenSea rate limit reached; try again later")
        if resp.status_code >= 400:
            raise ProviderError(f"OpenSea returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError("OpenSea returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise ProviderError("OpenSea returned an unexpected JSON structure")
        return payload

    def fetch(self, identifier: str, chain: str) -> dict:
        self.requests_made = 0
        identifier = normalize_identifier(identifier)
        f = empty_fields()
        sources: list[str] = []
        slug = identifier
        if classify_identifier(identifier) == "contract":
            if chain in NON_EVM_CHAINS:
                raise ValueError(f"contract-address lookups are not supported on {chain}; use the collection slug")
            contract = self._get(f"/chain/{chain}/contract/{identifier}")
            sources.append(f"{self.base_url}/chain/{chain}/contract/{identifier}")
            slug = contract.get("collection")
            f["contract_address"] = identifier
            f["chain"] = chain
            if not slug or not isinstance(slug, str):
                raise NotFound(identifier)

        # The slug can come from the provider (contract lookup): always URL-encode it.
        enc = quote(slug, safe="")
        col = self._get(f"/collections/{enc}")
        sources.append(f"{self.base_url}/collections/{enc}")
        stats = self._get(f"/collections/{enc}/stats")
        sources.append(f"{self.base_url}/collections/{enc}/stats")
        total = stats.get("total")
        if total is None:
            total = {}
        if not isinstance(total, dict):
            raise ProviderError("OpenSea returned malformed collection statistics")
        if isinstance(col.get("opensea_url"), str) and col["opensea_url"].startswith("https://"):
            sources.append(col["opensea_url"])

        def text(key):
            v = col.get(key)
            return v.strip() if isinstance(v, str) and v.strip() else None

        f["name"] = text("name")
        f["description"] = text("description")
        f["safelist_status"] = text("safelist_status")
        f["creator_address"] = text("owner")
        f["website"] = text("project_url")
        f["twitter"] = text("twitter_username")
        f["discord"] = text("discord_url")
        f["total_supply"] = col.get("total_supply")
        f["unique_item_count"] = col.get("unique_item_count")
        f["created_date"] = text("created_date")
        contracts = [c for c in (col.get("contracts") or []) if isinstance(c, dict)]
        if contracts and not f["contract_address"]:
            addr = contracts[0].get("address")
            f["contract_address"] = addr.lower() if isinstance(addr, str) else None
            f["chain"] = contracts[0].get("chain") if isinstance(contracts[0].get("chain"), str) else None
        # OpenSea does not expose mint price or source-code verification: they stay unknown.

        # Per OpenSea's schema, floor_price is 0 (and floor_price_symbol empty) when there is
        # no active listing. A zero floor is therefore "no listing", never a real price of 0.
        f["floor_price"] = total.get("floor_price")
        f["price_symbol"] = total.get("floor_price_symbol") or None
        f["volume_symbol"] = total.get("volume_symbol") or None
        f["total_sales"] = total.get("sales")
        f["total_volume"] = total.get("volume")
        f["num_owners"] = total.get("num_owners")
        for interval in stats.get("intervals") or []:
            if isinstance(interval, dict) and interval.get("interval") == "seven_day":
                f["sales_7d"] = interval.get("sales")
                f["volume_7d"] = interval.get("volume")

        # Normalize provider numeric values. Bad, negative, infinite, or nonnumeric
        # upstream values become unknown instead of crashing scoring or contaminating it.
        integer_fields = {"total_sales", "sales_7d", "num_owners", "total_supply", "unique_item_count"}
        numeric_fields = {"mint_price", "floor_price", "total_volume", "average_sale_price", "volume_7d"}
        for key in integer_fields | numeric_fields:
            value = f.get(key)
            if value is None or isinstance(value, bool):
                f[key] = None
                continue
            try:
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    f[key] = None
                elif key in integer_fields and not number.is_integer():
                    f[key] = None
                else:
                    f[key] = int(number) if key in integer_fields else number
            except (TypeError, ValueError, OverflowError):
                f[key] = None

        if f["floor_price"] == 0:
            f["floor_price"] = None
            f["price_symbol"] = None
            f["no_active_listing"] = True
        elif f["floor_price"] is not None:
            f["no_active_listing"] = False

        # OpenSea v2 stats no longer return an average sale price. Derive it from all-time
        # volume / sales, only when both exist and the volume currency is known.
        if f["total_sales"] and f["total_volume"] is not None and f["volume_symbol"]:
            f["average_sale_price"] = f["total_volume"] / f["total_sales"]
            f[DERIVED]["average_sale_price"] = "total_volume / total_sales (currency: volume_symbol)"
        return {"provider": self.name, "fields": f, "sources": sources, "requests_made": self.requests_made}
