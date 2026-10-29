"""Configuration. Everything is optional; the app starts with no credentials.

Values come from environment variables, optionally loaded from a local `.env`
file (never committed). Real environment variables win over `.env`.
"""
from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

VALID_MODES = {"auto", "demo", "live"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines) so we need no extra dependency."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    mode: str
    opensea_api_key: str | None
    opensea_base_url: str
    db_path: str
    rate_limit_per_minute: int
    max_live_calls_per_day: int
    request_timeout_seconds: float
    budget_usd: float
    # Cost per live provider call. OpenSea's free key tier costs $0; if you ever
    # switch to a paid provider, set this so the cost guard can block overspending.
    live_call_cost_usd: float
    api_token: str | None = None
    # ERC-8257 tool manifest (OpenSea Tools). Served only when both are set.
    public_base_url: str | None = None
    creator_address: str | None = None
    # Optional manifest display images (https URLs): 1:1 icon and 16:9 banner.
    image_url: str | None = None
    featured_image_url: str | None = None
    # Pricing (free tier + x402 pay-per-report).
    pricing_enabled: bool = False
    free_analyses_per_month: int = 3
    payee_address: str | None = None
    price_usdc: str = "2.99"
    x402_network: str = "base"
    facilitator_url: str = "https://facilitator.payai.network"
    # How many of the busiest SeaDrop collections per chain are checked for "newness" per discovery scan.
    discovery_max_candidates: int = 30
    # Local app mode: UI at 127.0.0.1, user's own keys from the OS keyring / 600 file, no payment, no quota.
    local_mode: bool = False
    # Featured collections / submissions / sponsored slots.
    admin_token: str | None = None
    sponsored_enabled: bool = False
    featured_feed_url: str | None = None
    submissions_per_day: int = 5

    @property
    def live_available(self) -> bool:
        return bool(self.opensea_api_key)

    @property
    def paid_enabled(self) -> bool:
        return self.pricing_enabled and bool(self.payee_address)


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return default
    return value


DEFAULT_OPENSEA_BASE_URL = "https://api.opensea.io/api/v2"


def _https_or_none(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value if value.startswith("https://") else None


def _flag(name: str, default: bool) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "on", "true", "yes")


def _admin_token() -> str | None:
    """Admin endpoints are enabled only with a long random token (24+ chars) in MINTSCOPE_ADMIN_TOKEN."""
    value = (os.environ.get("MINTSCOPE_ADMIN_TOKEN") or "").strip()
    return value if len(value) >= 24 else None


def _payee() -> str | None:
    """Public receiving address only (never a key). Invalid or burn addresses disable the paid tier."""
    value = (os.environ.get("MINTSCOPE_PAYEE_ADDRESS") or "").strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) and value.lower() not in (
            "0x" + "0" * 40, "0x000000000000000000000000000000000000dead"):
        return value
    return None


def _price() -> str:
    value = (os.environ.get("MINTSCOPE_PRICE_USDC") or "2.99").strip()
    return value if re.fullmatch(r"\d{1,6}(\.\d{1,6})?", value) and float(value) > 0 else "2.99"


def get_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")
    mode = os.environ.get("MINTSCOPE_MODE", "auto").strip().lower()
    if mode not in VALID_MODES:
        mode = "auto"
    db_path = os.environ.get("MINTSCOPE_DB_PATH", "mintscope.db")
    if not os.path.isabs(db_path):
        db_path = str(BASE_DIR / db_path)
    return Settings(
        mode=mode,
        opensea_api_key=(os.environ.get("OPENSEA_API_KEY") or "").strip() or None,
        # The API key is sent to this URL, so only https is accepted.
        opensea_base_url=_https_or_none("OPENSEA_BASE_URL") or DEFAULT_OPENSEA_BASE_URL,
        db_path=db_path,
        rate_limit_per_minute=_int("MINTSCOPE_RATE_LIMIT_PER_MINUTE", 30, 1, 10000),
        max_live_calls_per_day=_int("MINTSCOPE_MAX_LIVE_CALLS_PER_DAY", 200, 0, 1000000),
        request_timeout_seconds=_float("MINTSCOPE_REQUEST_TIMEOUT_SECONDS", 10.0, 0.1, 120.0),
        budget_usd=_float("MINTSCOPE_BUDGET_USD", 2.0, 0.0, 1_000_000.0),
        live_call_cost_usd=_float("MINTSCOPE_LIVE_CALL_COST_USD", 0.0, 0.0, 1_000_000.0),
        api_token=(os.environ.get("MINTSCOPE_API_TOKEN") or "").strip() or None,
        public_base_url=(os.environ.get("MINTSCOPE_PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        creator_address=(os.environ.get("MINTSCOPE_CREATOR_ADDRESS") or "").strip().lower() or None,
        # Local app mode never charges: no x402 and no free-tier quota.
        pricing_enabled=(not _flag("MINTSCOPE_LOCAL", False)
                         and os.environ.get("MINTSCOPE_PRICING", "on").strip().lower() not in ("off", "0", "false", "no")),
        free_analyses_per_month=_int("MINTSCOPE_FREE_PER_MONTH", 3, 0, 1000),
        payee_address=_payee(),
        price_usdc=_price(),
        x402_network=(os.environ.get("MINTSCOPE_X402_NETWORK", "base").strip().lower()
                      if os.environ.get("MINTSCOPE_X402_NETWORK", "base").strip().lower() in ("base", "base-sepolia")
                      else "base"),
        facilitator_url=_https_or_none("MINTSCOPE_X402_FACILITATOR_URL") or "https://facilitator.payai.network",
        discovery_max_candidates=_int("MINTSCOPE_DISCOVERY_MAX_CANDIDATES", 30, 1, 200),
        image_url=_https_or_none("MINTSCOPE_IMAGE_URL"),
        local_mode=_flag("MINTSCOPE_LOCAL", False),
        admin_token=_admin_token(),
        sponsored_enabled=_flag("MINTSCOPE_SPONSORED_ENABLED", False),
        featured_feed_url=(_https_or_none("MINTSCOPE_FEATURED_FEED_URL") or "").rstrip("/") or None,
        submissions_per_day=_int("MINTSCOPE_SUBMISSIONS_PER_DAY", 5, 0, 10000),
        featured_image_url=_https_or_none("MINTSCOPE_FEATURED_IMAGE_URL"),
    )
