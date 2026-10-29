"""HTTP routes for Featured Collections, submissions and admin (token-protected) management."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

import featured


class FeaturedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: str | None = Field(None, max_length=42)
    link: str | None = Field(None, max_length=300)
    chain: Literal["ethereum", "base"] | None = None
    name: str | None = Field(None, max_length=120)
    image_url: str | None = Field(None, max_length=500)
    mint_url: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


class FeaturedEdit(BaseModel):
    # extra="forbid": a score (or anything else) cannot be set by hand; it only comes from the analysis.
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=120)
    image_url: str | None = Field(None, max_length=500)
    mint_url: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)
    active: bool | None = None


class SponsorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["7d", "30d"]
    start: str | None = None


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(..., min_length=3, max_length=300, description="Contract address or collection link")
    chain: Literal["ethereum", "base"] | None = None
    mint_url: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


class DailyLimiter:
    def __init__(self, per_day: int):
        self.per_day = per_day
        self.hits = defaultdict(list)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.time()
        with self.lock:
            q = [x for x in self.hits[key] if t - x < 86400]
            if len(q) >= self.per_day:
                self.hits[key] = q
                return False
            q.append(t)
            self.hits[key] = q
            return True


def _http(exc: featured.FeaturedError):
    return HTTPException(exc.status, str(exc))


def register(app: FastAPI, settings, service: featured.FeaturedService) -> None:
    sub_limiter = DailyLimiter(settings.submissions_per_day)

    def admin(token: str | None) -> None:
        if not settings.admin_token:
            raise HTTPException(404, "Admin API is disabled (set MINTSCOPE_ADMIN_TOKEN, 24+ characters).")
        if not secrets.compare_digest((token or "").encode(), settings.admin_token.encode()):
            raise HTTPException(401, "Missing or invalid X-Admin-Token.")

    def run(fn, *a, **k):
        try:
            return fn(*a, **k)
        except featured.FeaturedError as exc:
            raise _http(exc)

    # ---- public ----------------------------------------------------------------------------
    @app.get("/featured")
    def featured_list():
        return {"items": run(service.public_list), "sponsored_slots_enabled": service.sponsored_enabled,
                "disclosure": "Featured collections are picked by MintScope. Items labeled 'Sponsored' are paid "
                              "promotions. Neither featuring nor sponsorship changes the risk score.",
                "disclaimer": "Risk analysis only. Not financial advice; no guarantee of profit or that a project "
                              "is or is not a scam."}

    @app.get("/featured/{item_id}")
    def featured_get(item_id: str):
        item = run(service.get, item_id[:40])
        service.record(item["id"], "view")
        return item

    @app.get("/featured/{item_id}/mint", include_in_schema=False)
    def featured_mint(item_id: str):
        return RedirectResponse(run(service.click, item_id[:40]), status_code=302)

    @app.post("/submissions", status_code=201)
    def submit(body: SubmissionIn, request: Request):
        ip = request.client.host if request.client else "unknown"
        if not sub_limiter.allow(ip):
            raise HTTPException(429, f"Submission limit: {settings.submissions_per_day} per day.")
        return run(service.submit, body.input, body.chain, body.mint_url, body.note, submitter=f"ip:{ip}")

    # ---- admin -------------------------------------------------------------------------------
    T = Header(default=None, alias="X-Admin-Token")

    @app.get("/admin/featured")
    def admin_list(x_admin_token: str | None = T):
        admin(x_admin_token)
        return {"items": service.list(admin=True)}

    @app.post("/admin/featured", status_code=201)
    def admin_add(body: FeaturedIn, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.add, **body.model_dump())

    @app.patch("/admin/featured/{item_id}")
    def admin_edit(item_id: str, body: FeaturedEdit, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.edit, item_id, **body.model_dump(exclude_unset=True))

    @app.delete("/admin/featured/{item_id}")
    def admin_remove(item_id: str, x_admin_token: str | None = T):
        admin(x_admin_token)
        run(service.remove, item_id)
        return {"removed": item_id}

    @app.post("/admin/featured/{item_id}/reanalyze")
    def admin_reanalyze(item_id: str, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.reanalyze, item_id)

    @app.post("/admin/featured/{item_id}/sponsor")
    def admin_sponsor(item_id: str, body: SponsorIn, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.sponsor, item_id, body.plan, body.start)

    @app.delete("/admin/featured/{item_id}/sponsor")
    def admin_unsponsor(item_id: str, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.unsponsor, item_id)

    @app.get("/admin/submissions")
    def admin_submissions(status: Literal["pending", "approved", "rejected", "all"] = "pending",
                          x_admin_token: str | None = T):
        admin(x_admin_token)
        return {"submissions": service.submissions(None if status == "all" else status)}

    @app.post("/admin/submissions/{sub_id}/approve")
    def admin_approve(sub_id: str, body: ReviewIn | None = None, x_admin_token: str | None = T):
        admin(x_admin_token)
        body = body or ReviewIn()
        return run(service.review, sub_id, True, body.image_url, body.note)

    @app.post("/admin/submissions/{sub_id}/reject")
    def admin_reject(sub_id: str, x_admin_token: str | None = T):
        admin(x_admin_token)
        return run(service.review, sub_id, False)

    @app.get("/admin/analytics")
    def admin_analytics(x_admin_token: str | None = T):
        admin(x_admin_token)
        return {"items": [{"id": i["id"], "name": i["name"], **i["stats"],
                           "sponsored": i["sponsored"]} for i in service.list(admin=True)]}
