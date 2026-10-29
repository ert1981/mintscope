"""MintScope AI — FastAPI service.

Run:  uvicorn app:app --reload
Docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import dataclasses
import threading
from types import SimpleNamespace
import secrets
import time
from collections import defaultdict, deque
from typing import Literal

from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

import analyzer
import discovery
import featured
import local_mode
import routes_featured
import pages
import register_page
import deactivate_page
import payments
import tool_manifest
from config import Settings, get_settings
from data_store import DataStore
from providers import (CHAINS, NON_EVM_CHAINS, DemoProvider, NotFound, OpenSeaProvider, ProviderError,
                       classify_identifier, normalize_identifier)

VERSION = "0.7.0"


class AnalyzeRequest(BaseModel):
    identifier: str = Field(..., max_length=100, description="OpenSea collection slug or 0x contract address",
                            examples=["demo-collection"])
    chain: str = Field("ethereum", max_length=32, description="Chain of the contract (used for contract addresses)")
    mode: Literal["auto", "demo", "live"] = Field(
        "auto", description="auto = live when an API key is configured, otherwise demo")
    tier: Literal["auto", "free", "full"] = Field(
        "auto", description="auto = full report when an X-Payment header is sent, otherwise the free summary; "
                            "full = paid report (answers 402 with x402 payment requirements)")

    @field_validator("identifier")
    @classmethod
    def _identifier(cls, v: str) -> str:
        # Slugs are lowercase and EVM addresses case-insensitive: normalize so the same
        # collection always yields the same identifier.
        v = normalize_identifier(v)
        classify_identifier(v)  # raises ValueError -> 422
        return v

    @field_validator("chain")
    @classmethod
    def _chain(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CHAINS:
            raise ValueError(f"unsupported chain; use one of: {', '.join(sorted(CHAINS))}")
        return v

    @model_validator(mode="after")
    def _contract_needs_evm_chain(self):
        if self.chain in NON_EVM_CHAINS and classify_identifier(self.identifier) == "contract":
            raise ValueError(f"0x contract addresses are not valid on {self.chain}; use the collection slug")
        return self


class RateLimiter:
    """In-memory sliding window per client. Good enough for one local process."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            # Prevent unbounded memory growth if many distinct client IPs appear.
            if len(self.hits) > 5000:
                stale = [ip for ip, hits in self.hits.items() if not hits or now - hits[-1] > 60]
                for ip in stale:
                    self.hits.pop(ip, None)
            return True


class DiscoverRequest(BaseModel):
    # Required in the JSON body so an empty probe (`tool-sdk verify` POSTs "{}") gets a fast 422
    # instead of starting a multi-minute scan.
    period: Literal["today", "week", "month"] = Field(..., description="today = last 24 h, week = 7 days, month = 30 days")
    chain: Literal["all", "ethereum", "base"] = Field("all", description="Chain to scan")
    limit: int = Field(10, ge=1, le=50, description="Maximum number of ranked collections to return")
    tier: Literal["auto", "free", "full"] = Field("auto", description="Same meaning as for /analyze")


def resolve_mode(requested: str, settings: Settings) -> str:
    if settings.mode == "demo" and requested == "live":
        raise HTTPException(409, "Server is configured for demo mode only (MINTSCOPE_MODE=demo).")
    if settings.mode == "live" and requested == "demo":
        # A live (public) deployment must never hand synthetic data to callers or agents.
        raise HTTPException(409, "Server is configured for live data only (MINTSCOPE_MODE=live); demo is disabled.")
    if requested == "demo" or settings.mode == "demo":
        return "demo"
    if settings.live_available:
        return "live"
    if requested == "live" or settings.mode == "live":
        raise HTTPException(503, "Live data is unavailable: OPENSEA_API_KEY is not set. Use mode 'demo'.")
    return "demo"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = DataStore(settings.db_path)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    live_lock = threading.Lock()

    def authorize(token: str | None) -> None:
        # Optional shared bearer-like token: configure it before exposing the API publicly.
        if settings.api_token and not secrets.compare_digest(token or "", settings.api_token):
            raise HTTPException(401, "Missing or invalid X-API-Token.")

    def fetch_live(identifier: str, chain: str, id_type: str) -> dict:
        # Serialize budget checks and provider calls so concurrent requests in one
        # process cannot all pass the same remaining-budget check.
        with live_lock:
            required_calls = 3 if id_type == "contract" else 2
            if store.live_calls_today() + required_calls > settings.max_live_calls_per_day:
                raise HTTPException(429, "Daily live-call limit would be exceeded (MINTSCOPE_MAX_LIVE_CALLS_PER_DAY).")
            worst_case = required_calls * settings.live_call_cost_usd
            if store.total_spent() + worst_case > settings.budget_usd + 1e-9:
                raise HTTPException(402, "Budget exhausted: this analysis could exceed MINTSCOPE_BUDGET_USD.")
            provider = OpenSeaProvider(settings.opensea_api_key, settings.opensea_base_url,
                                       settings.request_timeout_seconds)
            try:
                return provider.fetch(identifier, chain)
            except NotFound:
                raise HTTPException(404, f"Collection '{identifier}' was not found by the provider.")
            except ProviderError as exc:
                raise HTTPException(exc.status_code, f"Data provider error: {exc}")
            finally:
                for _ in range(provider.requests_made):
                    store.record_cost("provider_call", settings.live_call_cost_usd, "opensea", identifier)

    def fetch_public(identifier: str, chain: str) -> dict:
        """OpenSea without any key (local mode, free source first). Raises ProviderError/NotFound."""
        return OpenSeaProvider("", settings.opensea_base_url, settings.request_timeout_seconds).fetch(identifier, chain)

    def opensea_enricher(chain: str, address: str) -> dict | None:
        """Optional OpenSea data for a discovered contract (needs OPENSEA_API_KEY). Never raises."""
        if not settings.live_available or settings.mode == "demo":
            return None
        with live_lock:
            if store.live_calls_today() + 3 > settings.max_live_calls_per_day:
                return None
            if store.total_spent() + 3 * settings.live_call_cost_usd > settings.budget_usd + 1e-9:
                return None
            provider = OpenSeaProvider(settings.opensea_api_key, settings.opensea_base_url,
                                       settings.request_timeout_seconds)
            try:
                raw = provider.fetch(address, chain)
            except (NotFound, ProviderError, ValueError):
                return None
            finally:
                for _ in range(provider.requests_made):
                    store.record_cost("provider_call", settings.live_call_cost_usd, "opensea", f"discover:{address}")
        f = raw["fields"]
        keys = ("name", "safelist_status", "total_sales", "total_volume", "volume_symbol", "num_owners",
                "floor_price", "price_symbol", "sales_7d", "website", "twitter", "discord")
        out = {k: f.get(k) for k in keys}
        out["socials"] = any(f.get(k) for k in ("website", "twitter", "discord"))
        out["status"] = "provider_reported"
        out["sources"] = raw.get("sources", [])
        return out

    discoverer = discovery.Discoverer(timeout=max(settings.request_timeout_seconds, 15.0),
                                      opensea_enricher=opensea_enricher,
                                      max_candidates=settings.discovery_max_candidates)
    discover_lock = threading.Lock()

    def run_discovery(body: DiscoverRequest, request: Request, token: str | None) -> dict:
        authorize(token)
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, f"Local usage limit: {settings.rate_limit_per_minute} requests per minute.")
        chains = list(discovery.DISCOVERY_CHAINS) if body.chain == "all" else [body.chain]

        def scan() -> dict:
            # One scan at a time: public RPC endpoints are shared, free resources.
            with discover_lock:
                try:
                    return dict(discoverer.discover(body.period, chains, body.limit))
                except discovery.DiscoveryError as exc:
                    raise HTTPException(502, f"Onchain data source error: {exc}")

        if not settings.pricing_enabled:
            return scan()
        description = f"MintScope new NFT launches, full data ({settings.price_usdc} USDC)"
        if wants_paid(body.tier, request):
            return paid_response(request, description, lambda: {**scan(), "tier": "full"})
        if store.free_used(caller_ids(request), month()) >= settings.free_analyses_per_month:
            return free_exhausted(request, description)
        result = scan()
        left = use_free(request)
        if left < 0:
            return free_exhausted(request, description)
        return discovery.free_view(result, left, settings.free_analyses_per_month, price_info())

    x402 = payments.X402Config(payee=settings.payee_address, price_usdc=settings.price_usdc,
                               network=settings.x402_network) if settings.paid_enabled else None
    facilitator = payments.Facilitator(settings.facilitator_url)

    def caller_ids(request: Request) -> list[str]:
        """Free-tier identities: always the client IP; also the wallet in X-Payer-Address when given.
        The stricter of the two counts, so changing one identity does not reset the quota."""
        ids = [f"ip:{request.client.host if request.client else 'unknown'}"]
        wallet = (request.headers.get("X-Payer-Address") or "").strip()
        if payments.ADDRESS_RE.match(wallet):
            ids.insert(0, f"wallet:{wallet.lower()}")
        return ids

    def month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def resource_url(request: Request) -> str:
        if settings.public_base_url:
            q = f"?{request.url.query}" if request.url.query else ""
            return f"{settings.public_base_url}{request.url.path}{q}"
        return str(request.url).split("#")[0]

    def payment_required(requirements: dict, error: str) -> JSONResponse:
        return JSONResponse(payments.challenge(requirements, error), status_code=402,
                            headers={"X-Accept-Payment": "x402"})

    def price_info() -> dict:
        return {"price_usdc": settings.price_usdc, "network": settings.x402_network, "protocol": "x402",
                "how": "Repeat the request with tier 'full': it answers HTTP 402 with x402 payment requirements; "
                       "sign them and resend with the X-Payment header."} if x402 else {
            "available": False, "note": "The paid tier is not configured on this server."}

    def wants_paid(tier: str, request: Request) -> bool:
        return tier == "full" or (tier == "auto" and bool(request.headers.get("X-Payment")))

    def paid_response(request: Request, description: str, produce) -> JSONResponse:
        """x402 flow mirroring @opensea/tool-sdk's hosted gate with a replay guard and required settlement."""
        if not x402:
            raise HTTPException(503, "The paid tier is not configured (MINTSCOPE_PAYEE_ADDRESS is not set).")
        requirements = x402.requirements(resource_url(request), description)
        header = request.headers.get("X-Payment")
        if not header:
            return payment_required(requirements, "X-PAYMENT header is required")
        try:
            payment = payments.decode_payment(header)
            verified = facilitator.verify(payment, requirements)
        except payments.PaymentError as exc:
            if exc.status == 402:
                return payment_required(requirements, exc.error)
            raise HTTPException(exc.status, exc.error)
        key = payments.replay_key(payment, x402.network, x402.asset)
        if not key:
            return payment_required(requirements, "invalid_payload")
        if not store.reserve_authorization(key):
            return payment_required(requirements, "payment_authorization_already_used")
        # If producing the report fails, nothing is settled (the caller is not charged) and the
        # reservation is kept, so one authorization cannot be used to run the handler repeatedly.
        result = produce()
        settled, retryable = facilitator.settle(payment, requirements)
        if not settled:
            if retryable:
                store.release_authorization(key)
            return payment_required(requirements, "settlement_failed")
        tx = str(settled["transaction"])[:100]
        payer = str(settled.get("payer") or verified.get("payer") or "")[:42]
        store.mark_settled(key, tx)
        # Revenue is recorded only now, after the facilitator confirmed settlement.
        store.record_cost("x402_payment", float(settings.price_usdc), "x402",
                          f"tx={tx} payer={payer} network={x402.network} path={request.url.path}")
        result["payment"] = {"status": "settled", "transaction": tx, "network": settled.get("network", x402.network),
                             "payer": payer or None, "amount_usdc": settings.price_usdc}
        return JSONResponse(result, headers={"X-PAYMENT-RESPONSE": payments.payment_response_header(settled, x402.network)})

    def use_free(request: Request) -> int:
        left = store.consume_free(caller_ids(request), month(), settings.free_analyses_per_month)
        if left is None:
            return -1
        return left

    def free_exhausted(request: Request, description: str, payable: bool = True):
        msg = (f"Free tier used up: {settings.free_analyses_per_month} analyses per month. "
               f"Full reports cost {settings.price_usdc} USDC.")
        if x402 and payable:
            return payment_required(x402.requirements(resource_url(request), description), msg)
        raise HTTPException(429, msg + (" Paid reports need live data (this request is in demo mode)."
                                        if x402 else " The paid tier is not configured on this server."))

    app = FastAPI(
        title="MintScope AI",
        version=VERSION,
        description="Evidence-based NFT collection analysis. Demo data is synthetic and always labeled. "
                    "Reports never predict or guarantee profit.",
    )
    app.state.settings = settings
    app.state.store = store

    def set_opensea_key(value: str | None) -> None:
        """Local mode: swap in the user's own key from the device keystore (never logged or returned)."""
        nonlocal settings
        settings = dataclasses.replace(settings, opensea_api_key=value or None)
        app.state.settings = settings

    featured_service = featured.FeaturedService(settings.db_path, sponsored_enabled=settings.sponsored_enabled)
    app.state.featured = featured_service

    if settings.local_mode:
        local_mode.register(app, SimpleNamespace(settings=lambda: settings, set_opensea_key=set_opensea_key,
                                                 fetch_live=lambda i, c, t: fetch_live(i, c, t),
                                                 fetch_public=fetch_public,
                                                 featured=featured_service, store=store))
    else:
        app.add_api_route("/", lambda: HTMLResponse(pages.landing_page(settings)), methods=["GET"],
                          include_in_schema=False)

    @app.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
    def landing():
        return pages.landing_page(settings)

    routes_featured.register(app, settings, featured_service)

    @app.get("/register-onchain", response_class=HTMLResponse, include_in_schema=False)
    def register_onchain():
        """Static page: the creator signs the ERC-8257 registrations with their own browser wallet."""
        return HTMLResponse(register_page.page(), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @app.get("/deactivate-onchain", response_class=HTMLResponse, include_in_schema=False)
    def deactivate_onchain():
        """Creator removes duplicate tool registrations (#816, #817) with their own wallet."""
        return HTMLResponse(deactivate_page.page(), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": VERSION,
            "configured_mode": settings.mode,
            "live_data_available": settings.live_available,
            "default_effective_mode": "live" if settings.live_available and settings.mode != "demo" else "demo",
        }

    @app.post("/analyze")
    def analyze(body: AnalyzeRequest, request: Request, x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        authorize(x_api_token)
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, f"Local usage limit: {settings.rate_limit_per_minute} analyses per minute.")

        mode = resolve_mode(body.mode, settings)
        id_type = classify_identifier(body.identifier)

        def build() -> dict:
            if mode == "demo":
                raw = DemoProvider().fetch(body.identifier, body.chain)
            else:
                # Never fall back to demo data on provider errors.
                raw = fetch_live(body.identifier, body.chain, id_type)
            report = analyzer.build_report(raw, body.identifier, id_type, body.chain, mode)
            store.save_report(report)
            return report

        if not settings.pricing_enabled:
            return build()
        description = f"MintScope full NFT risk report ({settings.price_usdc} USDC)"
        if wants_paid(body.tier, request):
            if mode == "demo":
                raise HTTPException(409, "Paid reports require live data; this server or request is in demo mode.")
            return paid_response(request, description, lambda: {**analyzer.full_view(build()), "tier": "full"})
        if store.free_used(caller_ids(request), month()) >= settings.free_analyses_per_month:
            return free_exhausted(request, description, payable=mode != "demo")
        report = build()
        left = use_free(request)
        if left < 0:
            return free_exhausted(request, description, payable=mode != "demo")
        return analyzer.free_view(report, left, settings.free_analyses_per_month, price_info())

    @app.get("/discover")
    def discover_get(request: Request, period: Literal["today", "week", "month"] = "today",
                     chain: Literal["all", "ethereum", "base"] = "all", limit: int = 10,
                     tier: Literal["auto", "free", "full"] = "auto",
                     x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        """Find NEW NFT collections launched in the period, analyze and rank them."""
        try:
            body = DiscoverRequest(period=period, chain=chain, limit=limit, tier=tier)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return run_discovery(body, request, x_api_token)

    @app.post("/discover")
    def discover_post(body: DiscoverRequest, request: Request,
                      x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        """Same as GET /discover with a JSON body (the endpoint registered as an OpenSea tool)."""
        return run_discovery(body, request, x_api_token)

    @app.get("/reports/{report_id}")
    def get_report(report_id: str, x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        authorize(x_api_token)
        if len(report_id) > 64:
            raise HTTPException(422, "Invalid report id.")
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(404, "Report not found.")
        return report

    @app.get("/ledger")
    def ledger(x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        """Operational cost and usage ledger."""
        authorize(x_api_token)
        return store.ledger_summary(settings.budget_usd)

    @app.get(f"/.well-known/ai-tool/{tool_manifest.TOOL_SLUG}.json")
    def manifest():
        """ERC-8257 tool manifest for listing on OpenSea Tools."""
        try:
            return tool_manifest.build_manifest(settings)
        except tool_manifest.ManifestNotConfigured as exc:
            raise HTTPException(404, str(exc))

    @app.get(f"/.well-known/ai-tool/{tool_manifest.DISCOVER_SLUG}.json")
    def discover_manifest():
        """ERC-8257 manifest for the new-collection discovery tool (one tool = one endpoint)."""
        try:
            return tool_manifest.build_discover_manifest(settings)
        except tool_manifest.ManifestNotConfigured as exc:
            raise HTTPException(404, str(exc))

    return app


app = create_app()
