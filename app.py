"""MintScope AI — FastAPI service.

Run:  uvicorn app:app --reload
Docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import threading
from types import SimpleNamespace
import secrets
import time
from collections import defaultdict, deque
from typing import Literal

from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator, model_validator

import analyzer
import discovery
import featured
import local_mode
import routes_featured
import pages
import register_page
import deactivate_page
import erc8004
import mcp_server
import icon
import payments
import tool_manifest
from config import Settings, get_settings
from data_store import DataStore
from providers import (CHAINS, NON_EVM_CHAINS, DemoProvider, NotFound, OpenSeaProvider, ProviderError,
                       classify_identifier, normalize_identifier)

VERSION = "0.7.0"
# 402index.io domain-claim verification hash for mintscope-zrvv.onrender.com (public by design).
INDEX402_VERIFY_HASH = "d2f60449e30a87218db49c57d852d5f94f77f2add2adb347e484b2c0d599f045"


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


# x402 v2 / CDP Bazaar discovery metadata per paid route. Descriptive text only (the Bazaar refuses
# metadata that reads like instructions to an agent). Schemas are copies of the ERC-8257 manifest inputs/
# outputs; the manifests themselves are not touched (their onchain hashes must not change).
ICON_PATH = "/static/icon.png"
BAZAAR_ROUTES = {
    "/analyze": {
        "description": "Evidence-based NFT collection risk report: verified OpenSea market data, realized sales "
                       "versus listings, creator evidence, risk flags, missing data and a confidence level. "
                       "Not a profit prediction.",
        "tags": ["nft", "analytics", "risk", "opensea", "base"],
        "example": {"identifier": "boredapeyachtclub", "chain": "ethereum", "tier": "full"},
        "body_schema": tool_manifest.INPUTS,
        "output_schema": tool_manifest.OUTPUTS,
        "output_example": {"tier": "full", "data_mode": "live", "project": {"name": "Bored Ape Yacht Club"},
                           "risk_flags": [], "missing_data": [], "confidence": {"level": "medium"},
                           "payment": {"status": "settled", "amount_usdc": "2.99"},
                           "disclaimer": "Evidence summary, not financial advice or a profit prediction."},
    },
    "/discover": {
        "description": "New NFT collections launched on Ethereum and Base in the last day, week or month, found "
                       "from onchain contract deployments and ranked by evidence-based risk analysis. "
                       "Not a profit prediction.",
        "tags": ["nft", "discovery", "new-collections", "ethereum", "base"],
        "example": {"period": "today", "chain": "all", "limit": 10, "tier": "full"},
        "body_schema": tool_manifest.DISCOVER_INPUTS,
        "output_schema": tool_manifest.DISCOVER_OUTPUTS,
        "output_example": {"tier": "full", "period": "today", "collections": [],
                           "payment": {"status": "settled", "amount_usdc": "2.99"}},
    },
}


def bazaar_extension(path: str) -> dict | None:
    meta = BAZAAR_ROUTES.get(path)
    if not meta:
        return None
    return payments.bazaar_body_extension(copy.deepcopy(meta["example"]), copy.deepcopy(meta["body_schema"]),
                                          copy.deepcopy(meta["output_example"]), copy.deepcopy(meta["output_schema"]))


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
    # CDP Facilitator only when CDP_API_KEY_ID and CDP_API_KEY_SECRET are both set; else PayAI as before.
    facilitator = payments.make_facilitator(settings.facilitator_url)

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

    def public_base(request: Request) -> str:
        return settings.public_base_url or str(request.base_url).rstrip("/")

    def discovery_for(path: str, requirements: dict, base: str) -> tuple[dict, dict | None]:
        """x402 v2 resource info + `extensions.bazaar` for a paid route (402 header, and CDP settle payloads)."""
        meta = BAZAAR_ROUTES.get(path, {})
        resource = {"url": requirements["resource"],
                    "description": meta.get("description", requirements["description"]),
                    "mimeType": "application/json", "serviceName": "MintScope",
                    "iconUrl": f"{base}{ICON_PATH}"}
        if meta.get("tags"):
            resource["tags"] = meta["tags"]
        return resource, bazaar_extension(path)

    def payment_required(requirements: dict, error: str, request: Request) -> JSONResponse:
        """v1 JSON body (unchanged, what OpenSea's tool-sdk reads) + x402 v2 PAYMENT-REQUIRED header."""
        resource, ext = discovery_for(request.url.path, requirements, public_base(request))
        v2 = payments.payment_required_v2(x402, resource, error, ext)
        return JSONResponse(payments.challenge(requirements, error), status_code=402, headers={
            "X-Accept-Payment": "x402",
            payments.PAYMENT_REQUIRED_HEADER: payments.encode_header(v2),
            "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE"})

    def payment_header(request: Request) -> tuple[str | None, int]:
        """(header value, x402 version): v2 clients send PAYMENT-SIGNATURE, v1 clients X-Payment."""
        v2 = request.headers.get(payments.PAYMENT_SIGNATURE_HEADER)
        if v2:
            return v2, 2
        return request.headers.get("X-Payment"), 1

    def price_info() -> dict:
        return {"price_usdc": settings.price_usdc, "network": settings.x402_network, "protocol": "x402",
                "how": "Repeat the request with tier 'full': it answers HTTP 402 with x402 payment requirements; "
                       "sign them and resend with the X-Payment header."} if x402 else {
            "available": False, "note": "The paid tier is not configured on this server."}

    def wants_paid(tier: str, request: Request) -> bool:
        return tier == "full" or (tier == "auto" and bool(payment_header(request)[0]))

    def paid_response(request: Request, description: str, produce) -> JSONResponse:
        """x402 flow mirroring @opensea/tool-sdk's hosted gate with a replay guard and required settlement."""
        if not x402:
            raise HTTPException(503, "The paid tier is not configured (MINTSCOPE_PAYEE_ADDRESS is not set).")
        requirements = x402.requirements(resource_url(request), description)
        header, version = payment_header(request)
        if not header:
            return payment_required(requirements, "X-PAYMENT header is required", request)
        out = charge(header, version, requirements, produce, request.url.path, public_base(request))
        if out["status"] == "challenge":
            return payment_required(requirements, out["error"], request)
        return JSONResponse(out["result"], headers=out["headers"])

    def charge(header, version: int, requirements: dict, produce, path: str, base: str | None = None) -> dict:
        """verify -> replay reservation -> produce -> settle. Shared by HTTP routes and MCP tool calls.
        Returns {"status": "challenge", "error"} or {"status": "ok", "result", "headers", "receipt"}."""
        try:
            payment = payments.decode_payment(header)
            if version == 2 or payment.get("x402Version") == 2:
                # v2: verify against our own v2 requirements; the client's `accepted` must match them.
                version = 2
                facil_requirements = x402.requirements_v2()
                accepted = payment.get("accepted") or {}
                if any(str(accepted.get(k, "")).lower() != str(facil_requirements[k]).lower()
                       for k in ("scheme", "network", "asset", "amount", "payTo")):
                    raise payments.PaymentError(402, "payment_requirements_mismatch")
                if facilitator.kind == "cdp" and base is not None and path in BAZAAR_ROUTES:
                    # CDP Bazaar indexes the route from these on settle. PayAI payloads stay exactly as sent.
                    payment = payments.with_discovery(payment, *discovery_for(path, requirements, base))
            else:
                facil_requirements = requirements
            verified = facilitator.verify(payment, facil_requirements)
        except payments.PaymentError as exc:
            if exc.status == 402:
                return {"status": "challenge", "error": exc.error}
            raise HTTPException(exc.status, exc.error)
        # Same (payer, nonce) key for v1 and v2: one onchain authorization can be used once.
        key = payments.replay_key(payment, x402.network, x402.asset)
        if not key:
            return {"status": "challenge", "error": "invalid_payload"}
        if not store.reserve_authorization(key):
            return {"status": "challenge", "error": "payment_authorization_already_used"}
        # If producing the report fails, nothing is settled (the caller is not charged) and the
        # reservation is kept, so one authorization cannot be used to run the handler repeatedly.
        result = produce()
        settled, retryable = facilitator.settle(payment, facil_requirements)
        if not settled:
            if retryable:
                store.release_authorization(key)
            return {"status": "challenge", "error": "settlement_failed"}
        tx = str(settled["transaction"])[:100]
        payer = str(settled.get("payer") or verified.get("payer") or "")[:42]
        store.mark_settled(key, tx)
        # Revenue is recorded only now, after the facilitator confirmed settlement.
        store.record_cost("x402_payment", float(settings.price_usdc), "x402",
                          f"tx={tx} payer={payer} network={x402.network} path={path}")
        result["payment"] = {"status": "settled", "transaction": tx, "network": settled.get("network", x402.network),
                             "payer": payer or None, "amount_usdc": settings.price_usdc}
        receipt = payments.payment_response_header(settled, x402.network)
        headers = {"X-PAYMENT-RESPONSE": receipt}
        receipt_v2 = {"success": True, "transaction": tx, "network": settled.get("network") or x402.caip2,
                      "payer": payer or None}
        if version == 2:
            headers[payments.PAYMENT_RESPONSE_HEADER] = payments.encode_header(receipt_v2)
        return {"status": "ok", "result": result, "headers": headers, "receipt": receipt_v2}

    def use_free(request: Request) -> int:
        left = store.consume_free(caller_ids(request), month(), settings.free_analyses_per_month)
        if left is None:
            return -1
        return left

    def free_exhausted(request: Request, description: str, payable: bool = True):
        msg = (f"Free tier used up: {settings.free_analyses_per_month} analyses per month. "
               f"Full reports cost {settings.price_usdc} USDC.")
        if x402 and payable:
            return payment_required(x402.requirements(resource_url(request), description), msg, request)
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
    app.state.facilitator_kind = facilitator.kind

    paid_descriptions = {"/analyze": "MintScope full NFT risk report", "/discover": "MintScope new NFT launches, full data"}

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """An empty body (or `{}`) on a paid route without a payment header answers 402 with the payment
        requirements (x402 discovery probes send the example input or an empty body). Any other invalid input,
        or an invalid body sent together with a payment, still gets 422, so nothing is charged for bad input."""
        if (x402 and settings.pricing_enabled and request.method == "POST" and request.url.path in paid_descriptions
                and not payment_header(request)[0]
                and (request.url.path != "/analyze" or (settings.live_available and settings.mode != "demo"))):
            raw = (await request.body()).strip()
            empty = not raw
            if not empty:
                try:
                    empty = json.loads(raw) == {}
                except ValueError:
                    empty = False
            if empty:
                description = f"{paid_descriptions[request.url.path]} ({settings.price_usdc} USDC)"
                return payment_required(x402.requirements(resource_url(request), description),
                                        "X-PAYMENT header is required", request)
        return await request_validation_exception_handler(request, exc)

    @app.get(ICON_PATH, include_in_schema=False)
    def icon_png():
        return Response(icon.png_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

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

    def make_analysis(body: AnalyzeRequest):
        """(mode, build) for one analysis request; shared by POST /analyze and the MCP tool."""
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
        return mode, build

    def get_challenge(request: Request, path: str, description: str):
        """GET/HEAD probe (x402dash, crawlers): the same 402 challenge as POST, instantly, no upstream calls.
        A GET that carries a payment is refused without verifying or charging: invocation is POST."""
        if not (x402 and settings.pricing_enabled) or (
                path == "/analyze" and not (settings.live_available and settings.mode != "demo")):
            raise HTTPException(405, f"Use POST {path} with a JSON body.")
        if payment_header(request)[0]:
            return JSONResponse({"detail": f"Use POST {path} with a JSON body; the payment was not used or charged."},
                                status_code=405, headers={"Allow": "POST"})
        return payment_required(x402.requirements(resource_url(request), description),
                                "X-PAYMENT header is required", request)

    @app.api_route("/analyze", methods=["GET", "HEAD"], include_in_schema=False)
    def analyze_probe(request: Request):
        return get_challenge(request, "/analyze", f"MintScope full NFT risk report ({settings.price_usdc} USDC)")

    @app.post("/analyze")
    def analyze(body: AnalyzeRequest, request: Request, x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        authorize(x_api_token)
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, f"Local usage limit: {settings.rate_limit_per_minute} analyses per minute.")

        mode, build = make_analysis(body)

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

    @app.api_route("/discover", methods=["GET", "HEAD"], include_in_schema=False)
    def discover_get(request: Request, period: str = "today", chain: str = "all", limit: str = "10",
                     tier: str = "auto", x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        """GET without a payment answers the 402 challenge instantly (no scan); `tier=free` runs the free
        scan; GET with a payment runs the paid scan. Parameters are validated only after the 402 check."""
        if (settings.pricing_enabled and x402 and tier != "free" and not payment_header(request)[0]) \
                or request.method == "HEAD":
            if request.method == "HEAD" and not (settings.pricing_enabled and x402):
                return Response(status_code=200)
            return payment_required(x402.requirements(resource_url(request),
                                                      f"MintScope new NFT launches, full data ({settings.price_usdc} USDC)"),
                                    "X-PAYMENT header is required", request)
        try:
            body = DiscoverRequest(period=period, chain=chain, limit=int(limit), tier=tier)
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

    # ---------------------------------------------------------------- discovery documents (x402scan etc.)
    X402_GUIDANCE = ("MintScope sells evidence-based NFT data per call with x402 (USDC on Base). POST /analyze "
                     "with {identifier, chain, tier:'full'} returns a full NFT collection risk report; POST /discover "
                     "with {period, chain, limit, tier:'full'} returns newly launched NFT collections ranked by risk "
                     "evidence. Without payment both answer HTTP 402 with x402 v1 (body) and v2 (PAYMENT-REQUIRED "
                     "header) requirements; sign and resend with PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1). "
                     "Also available as a remote MCP server at /mcp. Not financial advice or a profit prediction.")

    def openapi_schema() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title="MintScope AI", version=VERSION, description=app.description, routes=app.routes)
        schema["info"]["x-guidance"] = X402_GUIDANCE
        if settings.public_base_url:
            schema["servers"] = [{"url": settings.public_base_url}]
            schema["info"]["contact"] = {"name": "MintScope", "url": settings.public_base_url}
        for path, meta in BAZAAR_ROUTES.items():
            op = schema.get("paths", {}).get(path, {}).get("post")
            if not op:
                continue
            op["summary"] = {"/analyze": "NFT collection risk report", "/discover": "New NFT launches"}[path]
            op["description"] = meta["description"]
            op["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": copy.deepcopy(meta["body_schema"]), "example": copy.deepcopy(meta["example"])}}}
            op.setdefault("responses", {})["200"] = {"description": "Report (free summary or paid full report)",
                "content": {"application/json": {"schema": copy.deepcopy(meta["output_schema"]),
                                                  "example": copy.deepcopy(meta["output_example"])}}}
            if x402 and settings.pricing_enabled:
                op["x-payment-info"] = {"price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usdc},
                                        "protocols": [{"x402": {}}]}
                op["responses"]["402"] = {
                    "description": "Payment Required: x402 v1 challenge in the body, v2 in the PAYMENT-REQUIRED header",
                    "headers": {"PAYMENT-REQUIRED": {"schema": {"type": "string"},
                                                     "description": "base64 JSON x402 v2 PaymentRequired"}},
                    "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "x402Version": {"type": "integer"}, "error": {"type": "string"},
                        "accepts": {"type": "array", "items": {"type": "object"}}}}}}}
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema

    def paid_urls(request: Request) -> list[str]:
        return [f"{public_base(request)}{p}" for p in BAZAAR_ROUTES] if (x402 and settings.pricing_enabled) else []

    @app.get("/.well-known/x402", include_in_schema=False)
    def well_known_x402(request: Request):
        return {"version": 1, "resources": paid_urls(request), "instructions": X402_GUIDANCE}

    # ---------------------------------------------------------------- 402 Index domain verification
    @app.get("/.well-known/402index-verify.txt", include_in_schema=False)
    def index402_verify():
        # Public verification hash from 402index.io's domain claim (the claim token itself stays secret, off-repo).
        value = (os.environ.get("MINTSCOPE_402INDEX_VERIFY") or INDEX402_VERIFY_HASH).strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,200}", value):
            raise HTTPException(404, "Not configured.")
        return PlainTextResponse(value)

    # ---------------------------------------------------------------- ERC-8004 identity
    @app.get(erc8004.WELL_KNOWN, include_in_schema=False)
    @app.get("/agent-registration.json", include_in_schema=False)
    def agent_registration(request: Request):
        return JSONResponse(erc8004.registration(public_base(request)),
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})

    @app.get("/register-8004", response_class=HTMLResponse, include_in_schema=False)
    def register_8004(request: Request):
        """Owner registers MintScope in the ERC-8004 Identity Registry on Base with their own wallet."""
        return HTMLResponse(erc8004.page(public_base(request)),
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    # ---------------------------------------------------------------- MCP (streamable HTTP) + x402
    def mcp_paid(request: Request, tool: str, path: str, description: str, payment, produce,
                 error: str = "Payment required") -> dict:
        if not x402:
            return mcp_server.error_result("The paid tier is not configured on this server.")
        requirements = x402.requirements(f"{public_base(request)}{path}", description)
        resource = {"url": f"mcp://tool/{tool}", "description": BAZAAR_ROUTES[path]["description"],
                    "mimeType": "application/json"}
        if not payment:
            return mcp_server.payment_required_result(payments.payment_required_v2(x402, resource, error, None))
        if not isinstance(payment, dict):
            return mcp_server.payment_required_result(payments.payment_required_v2(x402, resource, "invalid_payload", None))
        try:
            out = charge(payment, 2 if payment.get("x402Version") == 2 else 1, requirements, produce, f"/mcp/{tool}")
        except HTTPException as exc:
            return mcp_server.error_result(str(exc.detail))
        if out["status"] == "challenge":
            return mcp_server.payment_required_result(payments.payment_required_v2(x402, resource, out["error"], None))
        return mcp_server.text_result(out["result"], {mcp_server.PAYMENT_RESPONSE_META: out["receipt"]})

    # MINTSCOPE_MCP_BILLING=gateway only inside an MCPize-hosted container (their gateway charges per call).
    # Never set it on the public Render service: /mcp would then return paid results without x402.
    mcp_gateway_billed = (os.environ.get("MINTSCOPE_MCP_BILLING") or "").strip().lower() == "gateway"

    def mcp_guard(request: Request) -> dict | None:
        try:
            authorize(request.headers.get("X-API-Token"))
        except HTTPException as exc:
            return mcp_server.error_result(str(exc.detail))
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return mcp_server.error_result(f"Local usage limit: {settings.rate_limit_per_minute} requests per minute.")
        return None

    def mcp_free_or_paid(request, tool, path, tier, payment, mode, produce_full, produce_free_raw, free_view):
        description = f"{paid_descriptions[path]} ({settings.price_usdc} USDC)"
        try:
            if mcp_gateway_billed:
                # Hosted behind the MCPize gateway, which charges per call itself: no second x402 charge.
                return mcp_server.text_result(produce_full())
            if not settings.pricing_enabled:
                return mcp_server.text_result(produce_free_raw())
            if tier == "full" or (tier == "auto" and payment):
                if mode == "demo":
                    return mcp_server.error_result("Paid reports require live data; this server is in demo mode.")
                return mcp_paid(request, tool, path, description, payment, produce_full)
            msg = (f"Free tier used up: {settings.free_analyses_per_month} per month. "
                   f"Full results cost {settings.price_usdc} USDC.")
            if store.free_used(caller_ids(request), month()) >= settings.free_analyses_per_month:
                return (mcp_paid(request, tool, path, description, None, produce_full, msg)
                        if x402 and mode != "demo" else mcp_server.error_result(msg))
            raw = produce_free_raw()
            left = use_free(request)
            if left < 0:
                return (mcp_paid(request, tool, path, description, None, produce_full, msg)
                        if x402 and mode != "demo" else mcp_server.error_result(msg))
            return mcp_server.text_result(free_view(raw, left))
        except HTTPException as exc:
            return mcp_server.error_result(str(exc.detail))

    def mcp_analyze(args: dict, meta: dict, request: Request) -> dict:
        blocked = mcp_guard(request)
        if blocked:
            return blocked
        try:
            body = AnalyzeRequest(**{k: v for k, v in args.items() if k in ("identifier", "chain", "tier")})
            mode, build = make_analysis(body)
        except ValueError as exc:
            return mcp_server.error_result("Invalid arguments", str(exc)[:500])
        except HTTPException as exc:
            return mcp_server.error_result(str(exc.detail))
        return mcp_free_or_paid(request, "analyze_nft_collection", "/analyze", body.tier,
                                meta.get(mcp_server.PAYMENT_META), mode,
                                lambda: {**analyzer.full_view(build()), "tier": "full"}, build,
                                lambda r, left: analyzer.free_view(r, left, settings.free_analyses_per_month,
                                                                   price_info()))

    def mcp_discover(args: dict, meta: dict, request: Request) -> dict:
        blocked = mcp_guard(request)
        if blocked:
            return blocked
        try:
            body = DiscoverRequest(**{k: v for k, v in args.items() if k in ("period", "chain", "limit", "tier")})
        except ValueError as exc:
            return mcp_server.error_result("Invalid arguments", str(exc)[:500])
        chains = list(discovery.DISCOVERY_CHAINS) if body.chain == "all" else [body.chain]

        def scan() -> dict:
            with discover_lock:
                try:
                    return dict(discoverer.discover(body.period, chains, body.limit))
                except discovery.DiscoveryError as exc:
                    raise HTTPException(502, f"Onchain data source error: {exc}")
        return mcp_free_or_paid(request, "discover_new_nft_collections", "/discover", body.tier,
                                meta.get(mcp_server.PAYMENT_META), "live",
                                lambda: {**scan(), "tier": "full"}, scan,
                                lambda r, left: discovery.free_view(r, left, settings.free_analyses_per_month,
                                                                    price_info()))

    def mcp_schema(schema: dict) -> dict:
        out = copy.deepcopy(schema)
        out["properties"]["tier"]["description"] = (
            "free = overall score + short summary (limited per month); full = paid result via x402 "
            "(USDC on Base, sent in params._meta['x402/payment']); auto = full when a payment is attached")
        return out

    price_meta = ({"price_usdc": settings.price_usdc, "network": x402.caip2, "asset": x402.asset,
                   "payTo": x402.payee,
                   "transport": "x402 MCP (_meta x402/payment)"} if x402 else None)
    mcp = mcp_server.McpServer(
        name="io.github.ert1981/mintscope", title="MintScope", version=VERSION,
        instructions="Evidence-based NFT data. Tools return a free summary (limited per month) or, with tier "
                     "'full', a paid x402 result: the first call answers a PaymentRequired object; retry with the "
                     "signed payment in _meta['x402/payment']. Not financial advice or a profit prediction.",
        tools=[
            mcp_server.Tool("analyze_nft_collection", "NFT collection risk report",
                            BAZAAR_ROUTES["/analyze"]["description"], mcp_schema(tool_manifest.INPUTS),
                            mcp_analyze, price_meta),
            mcp_server.Tool("discover_new_nft_collections", "New NFT launches",
                            BAZAAR_ROUTES["/discover"]["description"] + " A full scan can take a few minutes.",
                            mcp_schema(tool_manifest.DISCOVER_INPUTS), mcp_discover, price_meta),
        ])
    app.state.mcp = mcp

    @app.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request):
        raw = await request.body()
        status, payload = await run_in_threadpool(mcp.handle_body, raw, request)
        if payload is None:
            return Response(status_code=status)
        return JSONResponse(payload, status_code=status)

    @app.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
    def mcp_other():
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32000, "message": "Method not allowed: POST JSON-RPC to /mcp"}},
                            status_code=405, headers={"Allow": "POST"})

    return app


app = create_app()
