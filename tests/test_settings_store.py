"""
Tests for the dynamic policy settings store (agent/settings_store.py).

Covers:
  1. Defaults are seeded and returned correctly when the DB is empty,
     missing, or unreadable.
  2. get_min_retry_amount / get_max_retries fall back to hardcoded
     defaults on corrupt or non-numeric stored values.
  3. update_min_retry_amount / update_max_retries reject negative
     values, and wrap a failing DB write as a clean ValueError rather
     than letting sqlite3.Error propagate to the Flask layer.
  4. Regression test for the audit-log concurrency fix: concurrent
     calls to _log_change() must not lose writes, same pattern as the
     fix already proven for execute.py, checkout_recovery.py, and
     receivables.py.
  5. Regression test for the update_max_retries lost-update fix:
     concurrent updates to DIFFERENT causes must not silently revert
     each other - this is a separate bug from #4 above, since
     _log_change()'s lock only ever protected the audit log append,
     never the settings VALUE's own read-modify-write.
  6. get_all_settings() does not leak a connection on a query failure.

Run with: pytest tests/test_settings_store.py -v
"""

import json
import sqlite3
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import settings_store


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "DB_PATH", str(tmp_path / "settings.db"))
    monkeypatch.setattr(settings_store, "SETTINGS_AUDIT_LOG_PATH", str(tmp_path / "settings_audit_log.json"))
    yield


def test_get_min_retry_amount_returns_default_on_fresh_db():
    assert settings_store.get_min_retry_amount() == settings_store.DEFAULT_MIN_RETRY_AMOUNT


def test_get_max_retries_returns_defaults_on_fresh_db():
    assert settings_store.get_max_retries() == settings_store.DEFAULT_MAX_RETRIES


def test_update_min_retry_amount_persists_and_is_read_back():
    settings_store.update_min_retry_amount(500, updated_by="alice")
    assert settings_store.get_min_retry_amount() == 500


def test_update_min_retry_amount_rejects_negative():
    with pytest.raises(ValueError):
        settings_store.update_min_retry_amount(-1)


def test_update_max_retries_persists_single_cause_without_disturbing_others():
    settings_store.update_max_retries("CARD_DECLINED", 5, updated_by="bob")
    result = settings_store.get_max_retries()
    assert result["CARD_DECLINED"] == 5
    assert result["BANK_TIMEOUT"] == settings_store.DEFAULT_MAX_RETRIES["BANK_TIMEOUT"]


def test_update_max_retries_rejects_negative():
    with pytest.raises(ValueError):
        settings_store.update_max_retries("CARD_DECLINED", -1)


def test_get_min_retry_amount_falls_back_on_corrupt_value(monkeypatch):
    monkeypatch.setattr(settings_store, "_get_raw", lambda key: "not_a_number")
    assert settings_store.get_min_retry_amount() == settings_store.DEFAULT_MIN_RETRY_AMOUNT


def test_get_max_retries_falls_back_on_corrupt_json(monkeypatch):
    monkeypatch.setattr(settings_store, "_get_raw", lambda key: "{not valid json")
    assert settings_store.get_max_retries() == settings_store.DEFAULT_MAX_RETRIES


def test_get_max_retries_falls_back_when_stored_value_is_not_a_dict(monkeypatch):
    monkeypatch.setattr(settings_store, "_get_raw", lambda key: json.dumps([1, 2, 3]))
    assert settings_store.get_max_retries() == settings_store.DEFAULT_MAX_RETRIES


def test_update_min_retry_amount_wraps_db_error_as_value_error(monkeypatch):
    def broken_connection():
        raise sqlite3.Error("disk full")
    monkeypatch.setattr(settings_store, "_get_connection", broken_connection)

    with pytest.raises(ValueError):
        settings_store.update_min_retry_amount(200)


def test_update_max_retries_wraps_db_error_as_value_error(monkeypatch):
    def broken_connection():
        raise sqlite3.Error("disk full")
    monkeypatch.setattr(settings_store, "_get_connection", broken_connection)

    with pytest.raises(ValueError):
        settings_store.update_max_retries("CARD_DECLINED", 3)


def test_get_all_settings_returns_defaults_when_db_unreadable(monkeypatch):
    def broken_connection():
        raise sqlite3.Error("cannot open database")
    monkeypatch.setattr(settings_store, "_get_connection", broken_connection)

    result = settings_store.get_all_settings()
    assert result["min_retry_amount"]["value"] == settings_store.DEFAULT_MIN_RETRY_AMOUNT
    assert result["max_retries"]["value"] == settings_store.DEFAULT_MAX_RETRIES


def test_get_all_settings_closes_connection_even_when_query_fails(monkeypatch):
    """Regression test for the connection-leak fix: if the connection
    opens successfully but the subsequent query raises, the connection
    must still be closed rather than leaked."""
    closed = {"value": False}

    class FakeConn:
        def execute(self, *a, **kw):
            raise sqlite3.Error("query failed")

        def close(self):
            closed["value"] = True

    monkeypatch.setattr(settings_store, "_get_connection", lambda: FakeConn())
    monkeypatch.setattr(settings_store, "_seed_defaults_if_missing", lambda conn: None)

    settings_store.get_all_settings()
    assert closed["value"] is True


def test_no_audit_log_entry_when_value_is_unchanged():
    settings_store.update_min_retry_amount(settings_store.DEFAULT_MIN_RETRY_AMOUNT)
    assert not os.path.exists(settings_store.SETTINGS_AUDIT_LOG_PATH)


def test_audit_log_entry_written_on_change():
    settings_store.update_min_retry_amount(999, updated_by="carol")
    with open(settings_store.SETTINGS_AUDIT_LOG_PATH) as f:
        log = json.load(f)
    assert len(log) == 1
    assert log[0]["new_value"] == 999
    assert log[0]["updated_by"] == "carol"


def test_concurrent_settings_writes_are_not_lost(tmp_path, monkeypatch):
    """Regression test for the file-lock fix: without it, concurrent
    calls to _log_change() silently lose writes, same bug class already
    fixed (and proven) for execute.py, checkout_recovery.py, and
    receivables.py."""
    log_path = tmp_path / "concurrent_settings_log.json"
    monkeypatch.setattr(settings_store, "SETTINGS_AUDIT_LOG_PATH", str(log_path))

    num_threads = 20

    def worker(i):
        settings_store._log_change(f"key_{i}", "old", "new", f"user_{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(log_path) as f:
        log = json.load(f)

    written_keys = {entry["key"] for entry in log}
    expected_keys = {f"key_{i}" for i in range(num_threads)}

    assert written_keys == expected_keys
    assert len(log) == num_threads


def test_concurrent_max_retries_updates_to_different_causes_are_not_lost():
    """Regression test for the read-modify-write lock fix in
    update_max_retries(). Without it, concurrent calls for DIFFERENT
    causes can each read the same stale snapshot and the second write
    silently reverts the first admin's change - _log_change()'s own
    lock does not protect this, since it only guards the audit log
    append, not the settings value's read-modify-write. This proves
    the VALUE itself now survives real concurrent writers, not just
    the audit trail describing them.

    Uses synthetic cause names (CAUSE_0...CAUSE_14) rather than the
    five real causes so this stresses 15 genuinely concurrent writers
    instead of being limited to 5 slots - valid against the real merge
    behavior in get_max_retries(), since merged.update(parsed)
    preserves any key present in storage, not just the five defaults.
    """
    num_threads = 15

    def worker(i):
        settings_store.update_max_retries(f"CAUSE_{i}", i + 1, updated_by=f"user_{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = settings_store.get_max_retries()
    for i in range(num_threads):
        assert result[f"CAUSE_{i}"] == i + 1


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
