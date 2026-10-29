"""SQLite storage for reports and the operational cost / usage ledger."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          TEXT PRIMARY KEY,
    data_mode   TEXT NOT NULL CHECK (data_mode IN ('demo', 'live')),
    identifier  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    category    TEXT NOT NULL,      -- e.g. provider_call, hosting, network_fee
    provider    TEXT,
    amount_usd  REAL NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS free_usage (
    caller      TEXT NOT NULL,      -- "wallet:0x..." or "ip:..."
    month       TEXT NOT NULL,      -- UTC "YYYY-MM"; a new month starts a new counter
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (caller, month)
);
CREATE TABLE IF NOT EXISTS x402_authorizations (
    key         TEXT PRIMARY KEY,   -- x402:<network>:<asset>:<payer>:<nonce>
    status      TEXT NOT NULL,      -- reserved | settled
    created_at  TEXT NOT NULL,
    tx_hash     TEXT
);
"""

# Ledger categories that are income, not spending.
REVENUE_CATEGORIES = ("x402_payment",)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DataStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- reports -------------------------------------------------------
    def save_report(self, report: dict) -> str:
        mode = report["data_mode"]
        # Prefix makes demo and live reports impossible to confuse, even by id.
        report_id = f"{mode}_{uuid.uuid4().hex}"
        report["report_id"] = report_id
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO reports (id, data_mode, identifier, created_at, body) VALUES (?, ?, ?, ?, ?)",
                (report_id, mode, report["project"]["identifier"], report["analyzed_at"], json.dumps(report)),
            )
        return report_id

    def get_report(self, report_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT body FROM reports WHERE id = ?", (report_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    # ---- cost / usage ledger --------------------------------------------
    def record_cost(self, category: str, amount_usd: float, provider: str | None = None, note: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO ledger (ts, category, provider, amount_usd, note) VALUES (?, ?, ?, ?, ?)",
                (utc_now(), category, provider, float(amount_usd), note),
            )

    def total_spent(self) -> float:
        with self._connect() as conn:
            (total,) = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM ledger WHERE category NOT IN (?)", REVENUE_CATEGORIES
            ).fetchone()
        return float(total)

    def total_revenue(self) -> float:
        with self._connect() as conn:
            (total,) = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM ledger WHERE category IN (?)", REVENUE_CATEGORIES
            ).fetchone()
        return float(total)

    # ---- free tier ----------------------------------------------------------
    def free_used(self, callers: list[str], month: str) -> int:
        """Highest usage among the caller identities (so switching one identity does not reset it)."""
        with self._connect() as conn:
            counts = [conn.execute("SELECT count FROM free_usage WHERE caller = ? AND month = ?",
                                   (c, month)).fetchone() for c in callers]
        return max([r["count"] for r in counts if r] or [0])

    def consume_free(self, callers: list[str], month: str, limit: int) -> int | None:
        """Atomically use one free analysis for every identity. Returns analyses left, or None if exhausted."""
        with self._lock, self._connect() as conn:
            used = [conn.execute("SELECT count FROM free_usage WHERE caller = ? AND month = ?",
                                 (c, month)).fetchone() for c in callers]
            current = max([r["count"] for r in used if r] or [0])
            if current >= limit:
                return None
            for c in callers:
                conn.execute("INSERT INTO free_usage (caller, month, count) VALUES (?, ?, 1) "
                             "ON CONFLICT(caller, month) DO UPDATE SET count = count + 1", (c, month))
        return limit - current - 1

    # ---- x402 replay guard --------------------------------------------------
    def reserve_authorization(self, key: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO x402_authorizations (key, status, created_at) "
                               "VALUES (?, 'reserved', ?)", (key, utc_now()))
            return cur.rowcount == 1

    def release_authorization(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM x402_authorizations WHERE key = ? AND status = 'reserved'", (key,))

    def mark_settled(self, key: str, tx_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE x402_authorizations SET status = 'settled', tx_hash = ? WHERE key = ?",
                         (tx_hash, key))

    def live_calls_today(self) -> int:
        today = utc_now()[:10]
        with self._connect() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE category = 'provider_call' AND substr(ts, 1, 10) = ?",
                (today,),
            ).fetchone()
        return int(count)

    def ledger_summary(self, budget_usd: float) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) AS n, COALESCE(SUM(amount_usd), 0) AS total "
                "FROM ledger GROUP BY category ORDER BY category"
            ).fetchall()
        spent = self.total_spent()
        return {
            "revenue_usd": round(self.total_revenue(), 6),
            "budget_usd": budget_usd,
            "spent_usd": round(spent, 6),
            "remaining_usd": round(budget_usd - spent, 6),
            "by_category": [
                {"category": r["category"], "entries": r["n"], "total_usd": round(r["total"], 6)} for r in rows
            ],
            "live_calls_today": self.live_calls_today(),
        }
