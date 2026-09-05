"""
Dynamic policy configuration store.

Business policy boundaries that used to be hardcoded constants in
decide.py - the ₹150 minimum retry amount and the per-cause retry caps
- now live in a small SQLite database instead, editable at runtime
through agent/admin_panel.py without a code change or a redeploy.

Design choices, and why:

  - SQLite, not Postgres/Redis: this store is read on every single
    choose_action() call, so it needs to be fast and dependency-free.
    A single-file DB with no separate service to run keeps this
    feature from requiring its own piece of deployment infrastructure
    - it's just a file, shipped in the same container/volume as
    everything else. If this ever needs to be shared across multiple
    app instances behind a load balancer, that is the point to
    migrate to Postgres - same class of "known, documented limitation,
    not an oversight" as pending_store.py's flat-file concurrency note.
  - Every change is appended to logs/settings_audit_log.json, not just
    applied silently - a policy boundary is exactly the kind of thing
    this project's whole audit-trail philosophy (see execute.py,
    report.py) says should never change without a visible record of
    who changed it, when, and from what value to what value.
  - Every getter falls back to the original hardcoded defaults if the
    DB is missing, corrupt, or unreadable - a config store going down
    must never crash the recovery pipeline itself. This mirrors
    diagnose.py's "no GROQ_API_KEY -> UNKNOWN, don't crash" philosophy.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("SETTINGS_DB_PATH", "instance/settings.db")
SETTINGS_AUDIT_LOG_PATH = "logs/settings_audit_log.json"

# Original hardcoded values from decide.py, kept here as the fallback
# of last resort if the settings DB can't be read at all.
DEFAULT_MIN_RETRY_AMOUNT = 150
DEFAULT_MAX_RETRIES = {
    "INSUFFICIENT_FUNDS": 2,
    "BANK_TIMEOUT": 1,
    "CARD_DECLINED": 1,
    "INVALID_CVV": 0,
    "EXPIRED_CARD": 0,
}

MIN_RETRY_AMOUNT_KEY = "min_retry_amount"
MAX_RETRIES_KEY = "max_retries"


def _get_connection() -> sqlite3.Connection:
    """Open a connection, creating the DB file, directory, and table on
    first use. Cheap to call every time (CREATE TABLE IF NOT EXISTS),
    so callers don't need a separate init step or startup migration."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system'
        )
        """
    )
    return conn


def _seed_defaults_if_missing(conn: sqlite3.Connection) -> None:
    """INSERT OR IGNORE means this is safe to call on every read - it
    only seeds a key the very first time it's missing, never clobbers
    an admin-set value on a later call."""
    now = datetime.now(timezone.utc).isoformat()
    defaults = {
        MIN_RETRY_AMOUNT_KEY: str(DEFAULT_MIN_RETRY_AMOUNT),
        MAX_RETRIES_KEY: json.dumps(DEFAULT_MAX_RETRIES),
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO policy_settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, 'system_default')",
            (key, value, now),
        )
    conn.commit()


def _get_raw(key: str):
    try:
        conn = _get_connection()
        _seed_defaults_if_missing(conn)
        row = conn.execute(
            "SELECT value FROM policy_settings WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        print(f"[settings_store] Could not read '{key}' from settings DB: {e}")
        return None


def get_min_retry_amount() -> int:
    """The policy gate's minimum retry amount, in ₹. Falls back to the
    original hardcoded default if the DB is unreachable or the stored
    value is somehow non-numeric."""
    raw = _get_raw(MIN_RETRY_AMOUNT_KEY)
    if raw is None:
        return DEFAULT_MIN_RETRY_AMOUNT
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[settings_store] Invalid min_retry_amount value {raw!r} - using default.")
        return DEFAULT_MIN_RETRY_AMOUNT


def get_max_retries() -> dict:
    """Per-cause retry caps. Falls back to the original hardcoded
    defaults if the DB is unreachable or the stored JSON is corrupt -
    same graceful-degradation philosophy as the rest of this project."""
    raw = _get_raw(MAX_RETRIES_KEY)
    if raw is None:
        return dict(DEFAULT_MAX_RETRIES)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("stored max_retries is not a JSON object")
        # Merge over the defaults rather than trusting the stored dict
        # alone - a cause added to the codebase after the DB was last
        # written still gets a sane cap instead of a KeyError downstream.
        merged = dict(DEFAULT_MAX_RETRIES)
        merged.update(parsed)
        return merged
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        print(f"[settings_store] Invalid max_retries value - using defaults: {e}")
        return dict(DEFAULT_MAX_RETRIES)


def get_all_settings() -> dict:
    """Full settings snapshot with metadata, for the admin panel to
    render - includes when each key was last changed and by whom."""
    try:
        conn = _get_connection()
        _seed_defaults_if_missing(conn)
        rows = conn.execute(
            "SELECT key, value, updated_at, updated_by FROM policy_settings"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"[settings_store] Could not read settings DB: {e}")
        return {
            MIN_RETRY_AMOUNT_KEY: {"value": DEFAULT_MIN_RETRY_AMOUNT, "updated_at": None, "updated_by": None},
            MAX_RETRIES_KEY: {"value": dict(DEFAULT_MAX_RETRIES), "updated_at": None, "updated_by": None},
        }

    result = {}
    for key, value, updated_at, updated_by in rows:
        parsed = json.loads(value) if key == MAX_RETRIES_KEY else int(value)
        result[key] = {"value": parsed, "updated_at": updated_at, "updated_by": updated_by}
    return result


def _log_change(key: str, old_value, new_value, updated_by: str) -> None:
    """Append a settings change to its own audit log - separate from
    the payment audit log, since this is a config-change event, not a
    payment-recovery outcome, but the same 'never change a policy
    boundary silently' principle applies."""
    entry = {
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "updated_by": updated_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(SETTINGS_AUDIT_LOG_PATH, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    log.append(entry)
    os.makedirs(os.path.dirname(SETTINGS_AUDIT_LOG_PATH) or ".", exist_ok=True)
    with open(SETTINGS_AUDIT_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def update_min_retry_amount(new_value: int, updated_by: str = "admin") -> None:
    """Update the minimum retry amount and log the change. Raises
    ValueError on a negative amount - the admin panel surfaces this as
    a rejected update rather than silently accepting nonsense."""
    if new_value < 0:
        raise ValueError("min_retry_amount cannot be negative")
    old_value = get_min_retry_amount()
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_connection()
    conn.execute(
        "INSERT INTO policy_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (MIN_RETRY_AMOUNT_KEY, str(new_value), now, updated_by),
    )
    conn.commit()
    conn.close()
    if new_value != old_value:
        _log_change(MIN_RETRY_AMOUNT_KEY, old_value, new_value, updated_by)


def update_max_retries(cause: str, new_value: int, updated_by: str = "admin") -> None:
    """Update the retry cap for a single cause and log the change."""
    if new_value < 0:
        raise ValueError("max_retries cannot be negative")
    current = get_max_retries()
    old_value = current.get(cause)
    current[cause] = new_value
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_connection()
    conn.execute(
        "INSERT INTO policy_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (MAX_RETRIES_KEY, json.dumps(current), now, updated_by),
    )
    conn.commit()
    conn.close()
    if new_value != old_value:
        _log_change(f"max_retries.{cause}", old_value, new_value, updated_by)
