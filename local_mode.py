"""Local app mode: a small web UI on 127.0.0.1 where each user brings their own API key.

- No payment and no quota for the core features (analyze, discover, featured).
- The OpenSea key is optional: analysis works keyless from public onchain sources first; with a key it adds
  OpenSea market data. The key is stored only on this device (keystore.py) and only sent to api.opensea.io.
- The UI never receives the key back after saving (masked only); logs and reports never contain it.
- Requests must come to 127.0.0.1/localhost (Host check against DNS rebinding) and state-changing calls
  need the X-MintScope-Local header (a cross-site page cannot send it without CORS, which is not enabled).
- Pro (one-time license, offline Ed25519 check): comparisons and bulk JSON export.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import analyzer
import discovery
import featured
import keystore
import licensing
import local_ui
import onchain
from providers import NotFound, ProviderError

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
CACHE_TTL = 600


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opensea_api_key: str = Field(..., min_length=16, max_length=128)


class KeyTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opensea_api_key: str | None = Field(None, max_length=128)


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(..., min_length=3, max_length=300)
    chain: str = Field("ethereum", max_length=20)


class MultiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: list[str] = Field(..., min_length=1, max_length=25)
    chain: str = Field("ethereum", max_length=20)


class LicenseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    license: str = Field(..., min_length=10, max_length=2000)


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(..., min_length=3, max_length=300)
    chain: str | None = None
    mint_url: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


def check_opensea_key(key: str, base_url: str, timeout: float = 10.0) -> dict:
    """Checks a key against OpenSea's own API (the only place the key is ever sent)."""
    try:
        # Some OpenSea reads answer without a key, so test an endpoint that always requires one.
        r = requests.get(f"{base_url}/collection/boredapeyachtclub/nfts?limit=1",
                         headers={"x-api-key": key, "accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "status": "network_error", "message": f"Could not reach OpenSea ({exc.__class__.__name__})."}
    if r.status_code == 200:
        return {"ok": True, "status": "valid", "message": "Key works: OpenSea answered."}
    if r.status_code in (401, 403):
        return {"ok": False, "status": "invalid", "message": "OpenSea rejected this key."}
    if r.status_code == 429:
        return {"ok": True, "status": "rate_limited", "message": "Key accepted but rate limited right now."}
    return {"ok": False, "status": f"http_{r.status_code}", "message": "Unexpected answer from OpenSea."}


def register(app: FastAPI, ctx) -> None:
    """ctx: settings() getter, set_opensea_key(value), fetch_live(identifier, chain, id_type), featured, store."""
    keys = keystore.KeyStore()
    license_file = keystore.config_dir() / "license.txt"
    cache: dict = {}
    cache_lock = threading.Lock()

    # Load the stored key into the running settings at startup (env OPENSEA_API_KEY still wins if set).
    if not ctx.settings().opensea_api_key:
        stored = keys.get("opensea_api_key")
        if stored:
            ctx.set_opensea_key(stored)

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Local app only answers on 127.0.0.1 / localhost."}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/local/") \
                and request.headers.get("x-mintscope-local") != "1":
            return JSONResponse({"detail": "Missing X-MintScope-Local header."}, status_code=403)
        return await call_next(request)

    # ---- license ------------------------------------------------------------------------------
    def current_license() -> licensing.License | None:
        try:
            text = license_file.read_text().strip()
        except OSError:
            return None
        try:
            return licensing.verify(text)
        except licensing.LicenseError:
            return None

    def require_pro(feature: str) -> None:
        lic = current_license()
        if not lic or feature not in lic.features:
            raise HTTPException(403, {"error": "pro_required", "feature": feature, "pricing": licensing.PRICING})

    # ---- analysis -----------------------------------------------------------------------------
    def analyze_one(text: str, chain: str) -> dict:
        try:
            parsed = onchain.parse_input(text, chain)
        except onchain.InputError as exc:
            raise HTTPException(422, str(exc))
        ck = json.dumps(parsed, sort_keys=True) + str(bool(ctx.settings().opensea_api_key))
        with cache_lock:
            hit = cache.get(ck)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        settings = ctx.settings()
        result = {"input": text, "parsed": parsed, "onchain": None, "opensea": None, "notes": []}
        if parsed["kind"] == "contract" and parsed["chain"] in onchain.KEYLESS_CHAINS:
            try:
                result["onchain"] = onchain.analyze_contract(parsed["chain"], parsed["address"])
            except onchain.InputError as exc:
                raise HTTPException(422, str(exc))
            except discovery.DiscoveryError as exc:
                result["notes"].append(f"Onchain sources failed: {str(exc)[:150]}")
        elif parsed["kind"] == "contract":
            result["notes"].append(f"Keyless analysis covers {', '.join(onchain.KEYLESS_CHAINS)}; "
                                   f"{parsed['chain']} needs an OpenSea key.")
        if settings.opensea_api_key:
            ident = parsed.get("slug") or parsed.get("address")
            id_type = "slug" if parsed["kind"] == "slug" else "contract"
            try:
                report = ctx.fetch_live(ident, parsed["chain"], id_type)
                result["opensea"] = analyzer.full_view(analyzer.build_report(report, ident, id_type,
                                                                             parsed["chain"], "live"))
            except HTTPException as exc:
                result["notes"].append(f"OpenSea: {exc.detail}")
        else:
            # Free source first: OpenSea's public read endpoints, no key sent.
            ident = parsed.get("slug") or parsed.get("address")
            id_type = "slug" if parsed["kind"] == "slug" else "contract"
            try:
                raw = ctx.fetch_public(ident, parsed["chain"])
                result["opensea"] = analyzer.full_view(analyzer.build_report(raw, ident, id_type, parsed["chain"],
                                                                             "live"))
                result["opensea"]["access"] = "public_no_key"
            except NotFound:
                result["notes"].append("OpenSea (no key): collection not found.")
            except (ProviderError, ValueError) as exc:
                result["notes"].append(f"OpenSea without a key is unavailable ({str(exc)[:80]}); add your own key "
                                       "in Settings for market data.")
            if parsed["kind"] == "slug" and not result["opensea"]:
                raise HTTPException(400, "This collection slug could not be read without an OpenSea API key. Add "
                                         "your key in Settings, or paste the contract address / an item link.")
        if not result["onchain"] and not result["opensea"]:
            raise HTTPException(502, "; ".join(result["notes"]) or "No data source answered.")
        src = result["onchain"] or result["opensea"]
        result.update({
            "name": (result["onchain"] or {}).get("name") or analyzer._value(
                ((result["opensea"] or {}).get("project") or {}).get("collection_name")),
            "risk_score": src["score"]["value"],
            "score_source": "onchain_keyless" if result["onchain"] else "opensea",
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "disclaimer": analyzer.PRICING_DISCLAIMER,
        })
        with cache_lock:
            cache[ck] = (time.time(), result)
        return result

    # ---- routes ---------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui():
        return local_ui.PAGE

    @app.get("/api/local/state")
    def state():
        lic = current_license()
        return {"keys": {"opensea_api_key": keys.status("opensea_api_key")},
                "key_from_env": bool(ctx.settings().opensea_api_key) and not keys.get("opensea_api_key"),
                "license": lic.public() if lic else None, "pro_pricing": licensing.PRICING,
                "licensing_configured": licensing.public_key() is not None,
                "featured_feed": ctx.settings().featured_feed_url,
                "storage_dir": str(keystore.config_dir())}

    @app.post("/api/local/key")
    def save_key(body: KeyIn):
        try:
            keys.set("opensea_api_key", body.opensea_api_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        ctx.set_opensea_key(keys.get("opensea_api_key"))
        with cache_lock:
            cache.clear()
        return {"saved": True, **keys.status("opensea_api_key")}

    @app.delete("/api/local/key")
    def delete_key():
        keys.delete("opensea_api_key")
        ctx.set_opensea_key(None)
        with cache_lock:
            cache.clear()
        return {"deleted": True, **keys.status("opensea_api_key")}

    @app.post("/api/local/key/test")
    def check_key(body: KeyTestIn):
        key = (body.opensea_api_key or "").strip() or keys.get("opensea_api_key") or ctx.settings().opensea_api_key
        if not key:
            raise HTTPException(400, "No key entered or saved.")
        return check_opensea_key(key, ctx.settings().opensea_base_url, ctx.settings().request_timeout_seconds)

    @app.post("/api/local/analyze")
    def local_analyze(body: AnalyzeIn):
        return analyze_one(body.input, body.chain)

    @app.post("/api/local/license")
    def set_license(body: LicenseIn):
        try:
            lic = licensing.verify(body.license)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc))
        license_file.parent.mkdir(parents=True, exist_ok=True)
        license_file.write_text(body.license.strip())
        license_file.chmod(0o600)
        return {"activated": True, "license": lic.public()}

    @app.delete("/api/local/license")
    def remove_license():
        license_file.unlink(missing_ok=True)
        return {"removed": True}

    @app.post("/api/local/compare")
    def compare(body: MultiIn):
        require_pro("compare")
        if not 2 <= len(body.inputs) <= 5:
            raise HTTPException(422, "Compare 2 to 5 collections.")
        rows = []
        for text in body.inputs:
            try:
                r = analyze_one(text, body.chain)
            except HTTPException as exc:
                rows.append({"input": text, "error": exc.detail})
                continue
            m = (r["onchain"] or {}).get("metrics", {})
            flags = (r["onchain"] or r["opensea"] or {}).get("red_flags") or (r["opensea"] or {}).get("risk_flags", [])
            rows.append({"input": text, "name": r["name"], "chain": r["parsed"]["chain"],
                         "contract": r["parsed"].get("address"), "risk_score": r["risk_score"],
                         "score_source": r["score_source"],
                         "high_flags": sum(1 for f in flags if f.get("severity") == "high"),
                         "flags": [f.get("code") for f in flags],
                         "holders": m.get("holders"), "wallets_7d": m.get("unique_wallets_7d"),
                         "verified": m.get("contract_verified"), "standard": m.get("standard")})
        ok = [r for r in rows if "error" not in r]
        ok.sort(key=lambda r: r["risk_score"], reverse=True)
        return {"rows": rows, "ranking": [r["name"] or r["input"] for r in ok],
                "note": "Higher score = fewer risks detected. Not financial advice."}

    @app.post("/api/local/export")
    def bulk_export(body: MultiIn):
        require_pro("bulk_export")
        out = []
        for text in body.inputs:
            try:
                out.append(analyze_one(text, body.chain))
            except HTTPException as exc:
                out.append({"input": text, "error": exc.detail})
        name = f"mintscope-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        return JSONResponse({"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "count": len(out), "analyses": out},
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/local/featured")
    def local_featured():
        feed = ctx.settings().featured_feed_url
        if feed:
            try:
                r = requests.get(f"{feed}/featured", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("items", []):
                        if item.get("mint_redirect"):
                            item["mint_redirect"] = feed + item["mint_redirect"]
                    return {**data, "source": feed}
            except (requests.RequestException, ValueError):
                pass
            return {"items": [], "source": feed, "error": "Featured feed unreachable."}
        return {"items": ctx.featured.public_list(), "source": "local",
                "disclosure": "Items labeled 'Sponsored' are paid promotions; this never changes the risk score."}

    @app.post("/api/local/submit")
    def local_submit(body: SubmitIn):
        feed = ctx.settings().featured_feed_url
        payload = body.model_dump(exclude_none=True)
        if feed:  # only the public submission fields are sent; never any key
            try:
                r = requests.post(f"{feed}/submissions", json=payload, timeout=60)
                return JSONResponse(r.json(), status_code=r.status_code)
            except (requests.RequestException, ValueError):
                raise HTTPException(502, "Featured server unreachable.")
        try:
            return ctx.featured.submit(body.input, body.chain, body.mint_url, body.note, submitter="local")
        except featured.FeaturedError as exc:
            raise HTTPException(exc.status, str(exc))
