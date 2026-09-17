"""Startup DB housekeeping (batch DB-health).

SQLite never returns deleted pages to the file by itself: rows freed by
project deletion sit in the freelist, so a busy DB file only grows. This module
runs a bounded, gated maintenance pass at server startup — the one moment the
process holds the DB alone (no worker writers yet):

  1. checkpoint the WAL back into the main file and truncate it
  2. VACUUM to reclaim the freelist and shrink the file

Both are cheap at startup and skipped entirely unless a cadence or size
threshold is crossed (recorded in ``maintenance_state``), so an idle install
never pays anything.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)

_STATE_KEY = "last_vacuum_at"
_VACUUM_INTERVAL_SECONDS = 30 * 24 * 3600   # at most once a month
_FREELIST_TRIGGER_BYTES = 100 * 1024 * 1024  # …unless 100 MB is reclaimable


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_vacuum_at(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM maintenance_state WHERE key = ?", (_STATE_KEY,)
        ).fetchone()
        return row["value"] if row else None
    except sqlite3.Error:
        return None


def _record_vacuum(conn: sqlite3.Connection, now_iso: str) -> None:
    conn.execute(
        "INSERT INTO maintenance_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_STATE_KEY, now_iso),
    )


def last_vacuum_at(conn: sqlite3.Connection) -> str | None:
    """上次 VACUUM 时间（给存储概览用）。"""
    return _last_vacuum_at(conn)


def run_startup_maintenance(db_path: Path) -> dict:
    """Best-effort gated maintenance; never raises. Returns what happened."""
    result = {"vacuumed": False, "reason": "not_due"}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Due when never run, older than the cadence, or a huge freelist.
            last = _last_vacuum_at(conn)
            now_iso = _utcnow_iso()
            freelist_bytes = 0
            try:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
                freelist_bytes = page_size * free_pages
            except sqlite3.Error:
                pass
            due = True
            if last is not None:
                try:
                    last_dt = datetime.fromisoformat(last)
                    elapsed = datetime.now(timezone.utc) - last_dt
                    due = elapsed.total_seconds() >= _VACUUM_INTERVAL_SECONDS
                except ValueError:
                    due = True  # corrupt marker → just re-run
            if not due and freelist_bytes < _FREELIST_TRIGGER_BYTES:
                result["reason"] = f"not_due freelist={freelist_bytes // 1024}KB"
                return result

            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            _record_vacuum(conn, now_iso)
            conn.commit()
            result["vacuumed"] = True
            result["reason"] = f"ran freelist_reclaimed={freelist_bytes // 1024}KB"
            LOG.info("db maintenance: vacuum ran (freelist %d KB)", freelist_bytes // 1024)
            return result
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # Busy/locked (another server instance?) or corrupt — skip, retry next start.
        result["reason"] = f"error: {exc}"
        LOG.warning("db maintenance skipped: %s", exc)
        return result
