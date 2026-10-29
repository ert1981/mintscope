"""Featured Collections, user submissions (review queue), sponsored slots and basic analytics.

Hard rule (enforced here and covered by tests): being featured or sponsored NEVER changes the risk score.
- The score comes only from `analysis` (keyless onchain analysis, onchain.analyze_contract), whose inputs are
  the chain and contract address. Nothing about featuring, sponsorship or payment is passed to it.
- The public score is re-read from the stored analysis on every view (`score_of`), never from an editable
  column; admin edit payloads cannot contain a score (extra fields are rejected).
- `_check_integrity` refuses to publish an item whose score differs from its analysis.
Sponsored slots are modeled but DISABLED unless MINTSCOPE_SPONSORED_ENABLED=on; sponsored items always carry
a visible "Sponsored" label with their start and end dates.
Featured analyses never use an end user's API key (keyless sources only).
"""
from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import onchain

SPONSOR_PLANS = {"7d": {"days": 7, "price_usd": 9}, "30d": {"days": 30, "price_usd": 29}}
SPONSORED_LABEL = "Sponsored"
# Well-known collections often impersonated by copycat contracts (verified mainnet contract addresses).
KNOWN_COLLECTIONS = {
    "boredapeyachtclub": "0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d",
    "mutantapeyachtclub": "0x60e4d786628fea6478f785a6d7e704777c86a7c6",
    "cryptopunks": "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb",
    "azuki": "0xed5af388653567af2f388e6224dc7c4b3241c544",
    "pudgypenguins": "0xbd3531da5cf5857e7cfaa92426877b022e612cf8",
    "doodles": "0x8a90cab2b38dba80c64b7734e58ee1db38b8992e",
    "milady": "0x5af0d9827e0c53e4799bb226655a1de152a425a5",
    "moonbirds": "0x23581767a106ae21c074b2276d25e5c3e136a68b",
}
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "rb.gy", "cutt.ly", "is.gd", "ow.ly", "shorturl.at"}
TRUSTED_MINT_HOSTS = {"opensea.io", "zora.co", "manifold.xyz", "app.manifold.xyz", "foundation.app",
                      "magiceden.io", "highlight.xyz", "mint.fun"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS featured (
    id TEXT PRIMARY KEY, name TEXT, image_url TEXT, mint_url TEXT, chain TEXT NOT NULL, contract TEXT NOT NULL,
    note TEXT, analysis TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, active INTEGER DEFAULT 1,
    sponsor_plan TEXT, sponsor_start TEXT, sponsor_end TEXT, source TEXT DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS featured_events (
    item_id TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, day, kind)
);
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY, input TEXT NOT NULL, chain TEXT, contract TEXT, mint_url TEXT, note TEXT,
    analysis TEXT, checks TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, reviewed_at TEXT,
    submitter TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class FeaturedError(ValueError):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def https_url(value: str | None, field: str) -> str | None:
    if value in (None, ""):
        return None
    u = urlparse(value)
    if u.scheme != "https" or not u.hostname or len(value) > 500:
        raise FeaturedError(f"{field} must be an https:// URL.")
    return value


def score_of(analysis: dict) -> float | None:
    """The ONLY source of a featured item's risk score."""
    return (analysis or {}).get("score", {}).get("value")


def mint_url_checks(url: str | None) -> list[dict]:
    out = []
    if not url:
        return out
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")

    def add(code, sev, msg):
        out.append({"code": code, "severity": sev, "message": msg})
    if u.scheme != "https":
        add("mint_link_not_https", "high", "Mint link is not https.")
    if host.startswith("xn--") or ".xn--" in host:
        add("mint_link_punycode", "high", "Mint link uses a punycode (look-alike) domain.")
    try:
        ipaddress.ip_address(host)
        add("mint_link_ip_address", "high", "Mint link points to a bare IP address.")
    except ValueError:
        pass
    if host in SHORTENERS:
        add("mint_link_shortener", "warning", "Mint link uses a URL shortener that hides the real site.")
    bare = re.sub(r"[^a-z0-9]", "", host)
    for brand in ("opensea", "0pensea", "openseа", "blur", "magiceden", "zora", "manifold", "foundation"):
        b = re.sub(r"[^a-z0-9]", "", brand)
        if b in bare and not any(host == t or host.endswith("." + t) for t in TRUSTED_MINT_HOSTS | {"blur.io"}):
            add("mint_link_lookalike", "high", f"Mint link domain imitates '{brand}' but is not the official site.")
            break
    return out


def impersonation_check(name: str | None, contract: str) -> list[dict]:
    if not name:
        return []
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for brand, real in KNOWN_COLLECTIONS.items():
        if brand in key and contract.lower() != real:
            exact = key == brand
            return [{"code": "name_impersonation", "severity": "high" if exact else "warning",
                     "message": f"Name {'is' if exact else 'contains'} '{brand}' but the contract is not the official "
                                f"one ({real}). Spin-offs by the same team exist; check before approving."}]
    return []


class FeaturedService:
    def __init__(self, db_path: str, analyze_fn=None, sponsored_enabled: bool = False):
        self.db_path = db_path
        self.analyze_fn = analyze_fn or (lambda chain, contract: onchain.analyze_contract(chain, contract))
        self.sponsored_enabled = sponsored_enabled
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- analysis (never receives featured/sponsored state) -----------------------------
    def _analyze(self, chain: str, contract: str) -> dict:
        try:
            return self.analyze_fn(chain, contract)
        except onchain.InputError as exc:
            raise FeaturedError(str(exc), 422)
        except Exception as exc:  # network / RPC errors
            raise FeaturedError(f"Analysis failed: {str(exc)[:150]}", 502)

    @staticmethod
    def _target(contract: str | None, chain: str | None, link: str | None) -> tuple[str, str]:
        text = contract or link
        parsed = onchain.parse_input(text or "", chain or "ethereum")
        if parsed["kind"] != "contract":
            raise FeaturedError("A contract address (or an OpenSea item/explorer link) is required; "
                                "collection slugs need an API key and are not accepted here.", 422)
        if chain and contract:
            parsed["chain"] = chain
        if parsed["chain"] not in onchain.KEYLESS_CHAINS:
            raise FeaturedError(f"Supported chains: {', '.join(onchain.KEYLESS_CHAINS)}.", 422)
        return parsed["chain"], parsed["address"]

    # ---- admin CRUD -------------------------------------------------------------------------
    def add(self, contract: str | None = None, chain: str | None = None, link: str | None = None,
            name: str | None = None, image_url: str | None = None, mint_url: str | None = None,
            note: str | None = None, source: str = "admin", analysis: dict | None = None) -> dict:
        chain, contract = self._target(contract, chain, link)
        image_url, mint_url = https_url(image_url, "image_url"), https_url(mint_url, "mint_url")
        analysis = analysis or self._analyze(chain, contract)
        item_id = "f_" + uuid.uuid4().hex[:12]
        t = iso(now())
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO featured (id, name, image_url, mint_url, chain, contract, note, analysis, "
                      "created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (item_id, (name or analysis.get("name") or contract)[:120], image_url, mint_url, chain,
                       contract, (note or "")[:1000], json.dumps(analysis), t, t, source))
        return self.get(item_id, admin=True)

    def edit(self, item_id: str, **fields) -> dict:
        allowed = {"name", "image_url", "mint_url", "note", "active"}
        bad = set(fields) - allowed
        if bad:
            raise FeaturedError(f"Cannot edit {sorted(bad)} (the risk score comes only from the analysis).", 422)
        if "image_url" in fields:
            fields["image_url"] = https_url(fields["image_url"], "image_url")
        if "mint_url" in fields:
            fields["mint_url"] = https_url(fields["mint_url"], "mint_url")
        self._row(item_id)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
            with self._lock, self._conn() as c:
                c.execute(f"UPDATE featured SET {sets} WHERE id = ?", (*fields.values(), iso(now()), item_id))
        return self.get(item_id, admin=True)

    def remove(self, item_id: str) -> None:
        self._row(item_id)
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM featured WHERE id = ?", (item_id,))
            c.execute("DELETE FROM featured_events WHERE item_id = ?", (item_id,))

    def reanalyze(self, item_id: str) -> dict:
        row = self._row(item_id)
        analysis = self._analyze(row["chain"], row["contract"])
        with self._lock, self._conn() as c:
            c.execute("UPDATE featured SET analysis = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(analysis), iso(now()), item_id))
        return self.get(item_id, admin=True)

    def sponsor(self, item_id: str, plan: str, start: str | None = None) -> dict:
        if not self.sponsored_enabled:
            raise FeaturedError("Sponsored slots are disabled (MINTSCOPE_SPONSORED_ENABLED is off).", 403)
        if plan not in SPONSOR_PLANS:
            raise FeaturedError(f"plan must be one of {sorted(SPONSOR_PLANS)}.", 422)
        self._row(item_id)
        begin = datetime.fromisoformat(start) if start else now()
        if begin.tzinfo is None:
            begin = begin.replace(tzinfo=timezone.utc)
        end = begin + timedelta(days=SPONSOR_PLANS[plan]["days"])
        with self._lock, self._conn() as c:
            c.execute("UPDATE featured SET sponsor_plan = ?, sponsor_start = ?, sponsor_end = ?, updated_at = ? "
                      "WHERE id = ?", (plan, iso(begin), iso(end), iso(now()), item_id))
        return self.get(item_id, admin=True)

    def unsponsor(self, item_id: str) -> dict:
        self._row(item_id)
        with self._lock, self._conn() as c:
            c.execute("UPDATE featured SET sponsor_plan = NULL, sponsor_start = NULL, sponsor_end = NULL "
                      "WHERE id = ?", (item_id,))
        return self.get(item_id, admin=True)

    # ---- views ------------------------------------------------------------------------------
    def _row(self, item_id: str) -> sqlite3.Row:
        with self._conn() as c:
            row = c.execute("SELECT * FROM featured WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise FeaturedError("Featured item not found.", 404)
        return row

    def _stats(self, item_id: str, since: str | None = None, until: str | None = None) -> dict:
        q = "SELECT kind, COALESCE(SUM(count), 0) AS n FROM featured_events WHERE item_id = ?"
        args = [item_id]
        if since:
            q += " AND day >= ?"; args.append(since[:10])
        if until:
            q += " AND day <= ?"; args.append(until[:10])
        with self._conn() as c:
            rows = {r["kind"]: r["n"] for r in c.execute(q + " GROUP BY kind", args)}
        views, clicks = rows.get("view", 0), rows.get("click", 0)
        return {"views": views, "mint_clicks": clicks, "click_rate": round(clicks / views, 4) if views else None}

    def _sponsorship(self, row) -> dict | None:
        if not self.sponsored_enabled or not row["sponsor_plan"]:
            return None
        t = iso(now())
        return {"label": SPONSORED_LABEL, "plan": row["sponsor_plan"],
                "price_usd": SPONSOR_PLANS[row["sponsor_plan"]]["price_usd"],
                "start": row["sponsor_start"], "end": row["sponsor_end"],
                "active": row["sponsor_start"] <= t < row["sponsor_end"],
                "stats_during_sponsorship": self._stats(row["id"], row["sponsor_start"], row["sponsor_end"]),
                "disclosure": "Paid promotion. Sponsorship does not change the risk score or the analysis."}

    def _item(self, row, admin: bool = False) -> dict:
        analysis = json.loads(row["analysis"])
        item = {
            "id": row["id"], "name": row["name"], "image_url": row["image_url"],
            "mint_url": row["mint_url"], "mint_redirect": f"/featured/{row['id']}/mint" if row["mint_url"] else None,
            "chain": row["chain"], "contract_address": row["contract"], "note": row["note"],
            "risk": {"score": score_of(analysis), "max": 100, "meaning": analysis.get("score", {}).get("meaning"),
                     "red_flags": analysis.get("red_flags", []), "analyzed_with": analysis.get("source"),
                     "note": "Score from the keyless onchain analysis only. Featuring or sponsorship never "
                             "changes it."},
            "featured_at": row["created_at"], "sponsored": self._sponsorship(row),
        }
        if admin:
            item.update({"active": bool(row["active"]), "source": row["source"], "analysis": analysis,
                         "stats": self._stats(row["id"]), "sponsor_raw": {
                             "plan": row["sponsor_plan"], "start": row["sponsor_start"], "end": row["sponsor_end"]}})
        return self._check_integrity(item, analysis)

    @staticmethod
    def _check_integrity(item: dict, analysis: dict) -> dict:
        if item["risk"]["score"] != score_of(analysis):
            raise RuntimeError("Integrity violation: a featured item's score differs from its analysis.")
        return item

    def get(self, item_id: str, admin: bool = False) -> dict:
        return self._item(self._row(item_id), admin)

    def list(self, admin: bool = False) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM featured" + ("" if admin else " WHERE active = 1")
                             + " ORDER BY created_at DESC").fetchall()
        items = [self._item(r, admin) for r in rows]
        # Active sponsored slots are shown first, clearly labeled; ordering is the only effect of sponsorship.
        items.sort(key=lambda i: 0 if (i["sponsored"] or {}).get("active") else 1)
        return items

    def record(self, item_id: str, kind: str, n: int = 1) -> None:
        if kind not in ("view", "click"):
            raise ValueError(kind)
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO featured_events (item_id, day, kind, count) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(item_id, day, kind) DO UPDATE SET count = count + ?",
                      (item_id, now().strftime("%Y-%m-%d"), kind, n, n))

    def public_list(self) -> list[dict]:
        items = self.list()
        for i in items:
            self.record(i["id"], "view")
        return items

    def click(self, item_id: str) -> str:
        row = self._row(item_id)
        if not row["active"] or not row["mint_url"]:
            raise FeaturedError("No mint link for this item.", 404)
        self.record(item_id, "click")
        return row["mint_url"]

    # ---- submissions ---------------------------------------------------------------------------
    def submit(self, text: str, chain: str | None = None, mint_url: str | None = None, note: str | None = None,
               submitter: str | None = None) -> dict:
        try:
            chain, contract = self._target(None, chain, text)
        except onchain.InputError as exc:
            raise FeaturedError(str(exc), 422)
        if mint_url and len(mint_url) > 500:
            raise FeaturedError("mint_url is too long.", 422)
        checks = mint_url_checks(mint_url)
        with self._conn() as c:
            if c.execute("SELECT 1 FROM submissions WHERE contract = ? AND chain = ? AND status = 'pending'",
                         (contract, chain)).fetchone():
                raise FeaturedError("This collection is already waiting for review.", 409)
            if c.execute("SELECT 1 FROM featured WHERE contract = ? AND chain = ?", (contract, chain)).fetchone():
                checks.append({"code": "already_featured", "severity": "info", "message": "Already featured."})
        analysis = None
        try:
            analysis = self.analyze_fn(chain, contract)
        except onchain.NotAContract as exc:
            checks.append({"code": "not_a_contract", "severity": "high", "message": str(exc)})
        except Exception as exc:
            checks.append({"code": "analysis_failed", "severity": "warning", "message": str(exc)[:150]})
        if analysis:
            checks += [{"code": f["code"], "severity": f["severity"], "message": f["message"]}
                       for f in analysis.get("red_flags", []) if f["severity"] in ("high", "warning")]
            checks += impersonation_check(analysis.get("name"), contract)
        suspicious = any(c["severity"] == "high" for c in checks)
        sub_id = "s_" + uuid.uuid4().hex[:12]
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO submissions (id, input, chain, contract, mint_url, note, analysis, checks, status, "
                      "created_at, submitter) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sub_id, text[:300], chain, contract, mint_url, (note or "")[:1000],
                       json.dumps(analysis) if analysis else None, json.dumps(checks), "pending", iso(now()),
                       submitter))
        return {"id": sub_id, "status": "pending", "chain": chain, "contract_address": contract,
                "risk_score": score_of(analysis) if analysis else None, "suspicious": suspicious,
                "checks": checks, "message": "Thanks. The collection was analyzed and is waiting for admin review."}

    def submissions(self, status: str | None = "pending") -> list[dict]:
        q, args = "SELECT * FROM submissions", []
        if status:
            q += " WHERE status = ?"; args.append(status)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY created_at", args).fetchall()
        out = []
        for r in rows:
            a = json.loads(r["analysis"]) if r["analysis"] else None
            checks = json.loads(r["checks"])
            out.append({"id": r["id"], "input": r["input"], "chain": r["chain"], "contract_address": r["contract"],
                        "mint_url": r["mint_url"], "note": r["note"], "status": r["status"],
                        "risk_score": score_of(a) if a else None, "name": (a or {}).get("name"),
                        "suspicious": any(c["severity"] == "high" for c in checks), "checks": checks,
                        "created_at": r["created_at"], "reviewed_at": r["reviewed_at"]})
        return out

    def review(self, sub_id: str, approve: bool, image_url: str | None = None, note: str | None = None) -> dict:
        with self._conn() as c:
            r = c.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        if not r:
            raise FeaturedError("Submission not found.", 404)
        if r["status"] != "pending":
            raise FeaturedError(f"Submission already {r['status']}.", 409)
        item = None
        if approve:
            analysis = json.loads(r["analysis"]) if r["analysis"] else None
            mint = r["mint_url"] if r["mint_url"] and urlparse(r["mint_url"]).scheme == "https" else None
            item = self.add(contract=r["contract"], chain=r["chain"], image_url=image_url, mint_url=mint,
                            note=note if note is not None else r["note"], source="submission", analysis=analysis)
        with self._lock, self._conn() as c:
            c.execute("UPDATE submissions SET status = ?, reviewed_at = ? WHERE id = ?",
                      ("approved" if approve else "rejected", iso(now()), sub_id))
        return {"id": sub_id, "status": "approved" if approve else "rejected", "featured": item}
