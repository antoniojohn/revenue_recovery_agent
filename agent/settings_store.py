"""
Dynamic policy configuration store.

Business policy boundaries that used to be hardcoded constants in
decide.py - the minimum retry amount and the per-cause retry caps -
now live in a small SQLite database instead, editable at runtime
through agent/admin_panel.py without a code change or a redeploy.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from filelock import FileLock

DB_PATH = os.getenv("SETTINGS_DB_PATH", "instance/settings.db")
SETTINGS_AUDIT_LOG_PATH = "logs/settings_audit_log.json"

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
    raw = _get_raw(MIN_RETRY_AMOUNT_KEY)
    if raw is None:
        return DEFAULT_MIN_RETRY_AMOUNT
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[settings_store] Invalid min_retry_amount value {raw!r} - using default.")
        return DEFAULT_MIN_RETRY_AMOUNT


def get_max_retries() -> dict:
    raw = _get_raw(MAX_RETRIES_KEY)
    if raw is None:
        return dict(DEFAULT_MAX_RETRIES)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("stored max_retries is not a JSON object")
        merged = dict(DEFAULT_MAX_RETRIES)
        merged.update(parsed)
        return merged
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        print(f"[settings_store] Invalid max_retries value - using defaults: {e}")
        return dict(DEFAULT_MAX_RETRIES)


def get_all_settings() -> dict:
    """Full settings snapshot with metadata, for the admin panel to
    render - includes when each key was last changed and by whom."""
    conn = None
    try:
        conn = _get_connection()
        _seed_defaults_if_missing(conn)
        rows = conn.execute(
            "SELECT key, value, updated_at, updated_by FROM policy_settings"
        ).fetchall()
    except sqlite3.Error as e:
        print(f"[settings_store] Could not read settings DB: {e}")
        return {
            MIN_RETRY_AMOUNT_KEY: {"value": DEFAULT_MIN_RETRY_AMOUNT, "updated_at": None, "updated_by": None},
            MAX_RETRIES_KEY: {"value": dict(DEFAULT_MAX_RETRIES), "updated_at": None, "updated_by": None},
        }
    finally:
        # Always release the connection, even if the query itself
        # raised after a successful connect - previously this leaked
        # a connection on that specific failure path.
        if conn is not None:
            conn.close()

    result = {}
    for key, value, updated_at, updated_by in rows:
        parsed = json.loads(value) if key == MAX_RETRIES_KEY else int(value)
        result[key] = {"value": parsed, "updated_at": updated_at, "updated_by": updated_by}
    return result


def _log_change(key: str, old_value, new_value, updated_by: str) -> None:
    """Append a settings change to its own audit log. Wrapped in a file
    lock so concurrent admin panel writes cannot race on the
    read-modify-write and silently drop each other's entries - the
    same bug (and fix) already applied to execute.py's,
    checkout_recovery.py's, and receivables.py's audit logs."""
    entry = {
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "updated_by": updated_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(SETTINGS_AUDIT_LOG_PATH) or ".", exist_ok=True)
    lock_path = SETTINGS_AUDIT_LOG_PATH + ".lock"

    with FileLock(lock_path, timeout=10):
        try:
            with open(SETTINGS_AUDIT_LOG_PATH, "r") as f:
                log = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log = []
        log.append(entry)
        with open(SETTINGS_AUDIT_LOG_PATH, "w") as f:
            json.dump(log, f, indent=2)


def update_min_retry_amount(new_value: int, updated_by: str = "admin") -> None:
    """Update the minimum retry amount and log the change. Raises
    ValueError on a negative amount, or if the DB write itself fails -
    the admin panel surfaces either case as a rejected update rather
    than a raw 500.

    Not lock-protected the way update_max_retries() is below - this is
    a plain scalar overwrite (INSERT ... ON CONFLICT DO UPDATE) with no
    read-modify-merge step, so there is nothing for two concurrent
    callers to race on: whichever write commits last simply wins,
    which is the correct "last write wins" semantics for a single
    scalar setting.
    """
    if new_value < 0:
        raise ValueError("min_retry_amount cannot be negative")
    old_value = get_min_retry_amount()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _get_connection()
        try:
            conn.execute(
                "INSERT INTO policy_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (MIN_RETRY_AMOUNT_KEY, str(new_value), now, updated_by),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ValueError(f"could not save min_retry_amount: {e}")
    if new_value != old_value:
        _log_change(MIN_RETRY_AMOUNT_KEY, old_value, new_value, updated_by)


def update_max_retries(cause: str, new_value: int, updated_by: str = "admin") -> None:
    """Update the retry cap for a single cause and log the change.

    Wrapped in a file lock because this is a read-modify-write over the
    *entire* max_retries dict (get -> mutate one key -> write the whole
    dict back) - unlike update_min_retry_amount above, which is a plain
    scalar overwrite with no merge to race on. Without this lock, two
    admins updating two different causes at nearly the same moment can
    each read the same stale snapshot, and whichever writes second
    silently reverts the first admin's change - the same lost-update
    bug already fixed for the audit logs in execute.py,
    checkout_recovery.py, and receivables.py, applied here to the
    settings VALUE itself, not just its audit trail.
    """
    if new_value < 0:
        raise ValueError("max_retries cannot be negative")

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    lock_path = DB_PATH + ".lock"

    with FileLock(lock_path, timeout=10):
        current = get_max_retries()
        old_value = current.get(cause)
        current[cause] = new_value
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn = _get_connection()
            try:
                conn.execute(
                    "INSERT INTO policy_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                    (MAX_RETRIES_KEY, json.dumps(current), now, updated_by),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise ValueError(f"could not save max_retries for {cause}: {e}")

    # Deliberately logged AFTER releasing the lock above - _log_change()
    # acquires its own separate lock file (SETTINGS_AUDIT_LOG_PATH +
    # ".lock"), so nesting it inside the DB_PATH lock would add nothing
    # but does add unnecessary lock-hold time.
    if new_value != old_value:
        _log_change(f"max_retries.{cause}", old_value, new_value, updated_by)
