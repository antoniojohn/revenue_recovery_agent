"""
Tests for the execution layer (agent/execute.py).

Covers:
  1. The bounded simulated retry loop: stops the instant it succeeds,
     never exceeds max_attempts, and an action with max_attempts=0
     (e.g. ESCALATE) never attempts anything.
  2. Routing: a synthetic/demo record always takes the simulated path;
     a real Razorpay record only takes the live-retry path when the
     action is actually a retry - NOTIFY_*/ESCALATE on a live record
     still goes through the simulated path, since those never had a
     real endpoint to call (see README).
  3. The live-retry path itself: successful order creation produces a
     'pending' outcome (recovered=None) and registers the case in
     pending_store; failed order creation produces a 'resolved' outcome
     with recovered=False, not a phantom pending case.
  4. append_to_audit_log: read-modify-write across multiple calls, and
     graceful handling of a missing/corrupt log file.

random.random() and all Razorpay API calls are monkeypatched - these
tests never sleep, hit the network, or touch the real logs/ directory.

Run with: pytest tests/test_execute.py -v
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import execute, pending_store


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Redirect the audit log and pending store to throwaway files for
    every test in this module."""
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.json"))
    monkeypatch.setattr(pending_store, "PENDING_STORE_PATH", str(tmp_path / "pending_retries.json"))
    yield


def _action(action_type="RETRY_IMMEDIATE", max_attempts=1, cause="CARD_DECLINED", **overrides):
    base = {
        "type": action_type,
        "max_attempts": max_attempts,
        "cause": cause,
        "source": "rule",
        "policy_approved": True,
        "policy_note": "within bounds",
        "reasoning": "test action",
    }
    base.update(overrides)
    return base


def _record(payment_id="pay_test_001", amount=500, **overrides):
    base = {"payment_id": payment_id, "amount": amount}
    base.update(overrides)
    return base


def _read_audit_log(path):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------
# Bounded simulated retry loop
# ---------------------------------------------------------------------

def test_retry_stops_immediately_on_first_success(monkeypatch):
    """The loop must stop the instant an attempt succeeds - it should
    not burn remaining attempts even if max_attempts allows more."""
    monkeypatch.setattr(execute.random, "random", lambda: 0.0)  # always "succeeds"
    action = _action(action_type="RETRY_AFTER_DELAY", max_attempts=3)
    record = _record()

    outcome = execute.run_action(action, record)

    assert outcome["recovered"] is True
    assert outcome["attempts_used"] == 1
    assert len(outcome["attempts"]) == 1
    assert outcome["attempts"][0]["succeeded"] is True


def test_retry_exhausts_all_attempts_when_never_successful(monkeypatch):
    """If every attempt fails, the loop must run exactly max_attempts
    times, never more, and report recovered=False."""
    monkeypatch.setattr(execute.random, "random", lambda: 0.999)  # always "fails"
    action = _action(action_type="RETRY_AFTER_DELAY", max_attempts=2)
    record = _record()

    outcome = execute.run_action(action, record)

    assert outcome["recovered"] is False
    assert outcome["attempts_used"] == 2
    assert len(outcome["attempts"]) == 2
    assert all(a["succeeded"] is False for a in outcome["attempts"])


def test_zero_max_attempts_action_never_attempts_anything(monkeypatch):
    """An ESCALATE (or any action with max_attempts=0) must make zero
    attempts and be recovered=False - there is no infinite-loop risk,
    but also no silent single freebie attempt."""
    monkeypatch.setattr(execute.random, "random", lambda: 0.0)
    action = _action(action_type="ESCALATE", max_attempts=0, cause="UNKNOWN")
    record = _record()

    outcome = execute.run_action(action, record)

    assert outcome["recovered"] is False
    assert outcome["attempts_used"] == 0
    assert outcome["attempts"] == []


def test_resolved_outcome_is_written_to_audit_log(monkeypatch):
    """Every resolved outcome (not just pending ones) must be appended
    to the audit log so the full lifecycle is traceable from one
    place."""
    monkeypatch.setattr(execute.random, "random", lambda: 0.0)
    action = _action()
    record = _record(payment_id="pay_audit_check")

    execute.run_action(action, record)

    log = _read_audit_log(execute.AUDIT_LOG_PATH)
    assert len(log) == 1
    assert log[0]["payment_id"] == "pay_audit_check"
    assert log[0]["status"] == "resolved"


# ---------------------------------------------------------------------
# Routing: synthetic vs. live, retry vs. non-retry
# ---------------------------------------------------------------------

def test_synthetic_record_always_uses_simulated_path(monkeypatch):
    """A record with no 'source' field (synthetic/demo data) must
    always resolve via the simulated path, never attempt a live API
    call."""
    monkeypatch.setattr(execute.random, "random", lambda: 0.0)
    action = _action(action_type="RETRY_IMMEDIATE", max_attempts=1)
    record = _record()  # no "source" key

    outcome = execute.run_action(action, record)

    assert outcome["resolution_method"] == "simulated"
    assert outcome["status"] == "resolved"


def test_non_retry_action_on_live_record_still_uses_simulated_path(monkeypatch):
    """NOTIFY_*/ESCALATE actions never had a real endpoint to call (see
    README) - even a record from the live Razorpay test account must
    fall through to the simulated path for these action types, not
    attempt a live retry."""
    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "check_payment_status", lambda payment_id: None)
    monkeypatch.setattr(execute.random, "random", lambda: 0.0)
    action = _action(action_type="NOTIFY_USER", max_attempts=0)
    record = _record(source="razorpay_live_test_account")

    outcome = execute.run_action(action, record)

    assert outcome["resolution_method"] == "simulated"
    assert outcome["status"] == "resolved"


def test_retry_action_on_live_record_uses_live_retry_path(monkeypatch):
    """A retry-type action on a record explicitly sourced from the live
    Razorpay test account must go through the live-order-creation path,
    not the probability-draw simulated path."""
    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "attempt_retry", lambda record: {"id": "order_live_123"})

    action = _action(action_type="RETRY_IMMEDIATE", max_attempts=1)
    record = _record(source="razorpay_live_test_account")

    outcome = execute.run_action(action, record)

    assert outcome["status"] == "pending"
    assert outcome["order_id"] == "order_live_123"
    assert outcome["recovered"] is None


# ---------------------------------------------------------------------
# Live retry path: pending vs. resolved-failure
# ---------------------------------------------------------------------

def test_successful_order_creation_produces_pending_outcome_and_registers_it(monkeypatch):
    """When attempt_retry successfully creates an order, the outcome
    must be 'pending' (not a guessed success/failure), and the case
    must be registered in pending_store so a later webhook or poll can
    resolve it."""
    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "attempt_retry", lambda record: {"id": "order_abc"})

    action = _action(action_type="RETRY_IMMEDIATE", max_attempts=1)
    record = _record(payment_id="pay_live_001", source="razorpay_live_test_account")

    outcome = execute.run_action(action, record)

    assert outcome["status"] == "pending"
    assert outcome["live_status"] == "order_created_awaiting_confirmation"
    pending = pending_store.get_pending("order_abc")
    assert pending is not None
    assert pending["original_payment_id"] == "pay_live_001"


def test_failed_order_creation_is_resolved_not_pending(monkeypatch):
    """If attempt_retry itself fails (no credentials, API error, order
    dict missing an id), there is no order for a webhook to ever
    confirm - this must be a resolved failure, not a phantom pending
    case sitting forever."""
    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "attempt_retry", lambda record: None)

    action = _action(action_type="RETRY_IMMEDIATE", max_attempts=1)
    record = _record(payment_id="pay_live_fail", source="razorpay_live_test_account")

    outcome = execute.run_action(action, record)

    assert outcome["status"] == "resolved"
    assert outcome["resolution_method"] == "retry_initiation_failed"
    assert outcome["recovered"] is False
    assert pending_store.list_pending() == []


def test_order_creation_returning_dict_without_id_is_treated_as_failure(monkeypatch):
    """A malformed order response (dict but no 'id') must be treated
    the same as attempt_retry returning None - there's still no
    resolvable order to track."""
    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "attempt_retry", lambda record: {})

    action = _action(action_type="RETRY_IMMEDIATE", max_attempts=1)
    record = _record(payment_id="pay_live_malformed", source="razorpay_live_test_account")

    outcome = execute.run_action(action, record)

    assert outcome["status"] == "resolved"
    assert outcome["resolution_method"] == "retry_initiation_failed"


# ---------------------------------------------------------------------
# append_to_audit_log
# ---------------------------------------------------------------------

def test_append_to_audit_log_does_not_overwrite_prior_entries(tmp_path, monkeypatch):
    """Two consecutive appends must both be present afterward - a
    read-modify-write bug here would silently lose earlier records."""
    log_path = tmp_path / "audit_log.json"
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(log_path))

    execute.append_to_audit_log({"payment_id": "pay_a", "status": "resolved"})
    execute.append_to_audit_log({"payment_id": "pay_b", "status": "resolved"})

    log = _read_audit_log(str(log_path))
    ids = {entry["payment_id"] for entry in log}
    assert ids == {"pay_a", "pay_b"}


def test_append_to_audit_log_creates_file_when_missing(tmp_path, monkeypatch):
    """The very first append of a fresh run must create the log file
    from scratch, not raise FileNotFoundError."""
    log_path = tmp_path / "audit_log.json"
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(log_path))
    assert not log_path.exists()

    execute.append_to_audit_log({"payment_id": "pay_first", "status": "resolved"})

    assert log_path.exists()
    log = _read_audit_log(str(log_path))
    assert len(log) == 1


def test_append_to_audit_log_recovers_from_corrupt_existing_file(tmp_path, monkeypatch):
    """A corrupted/partially-written audit log must not block future
    appends - treat it as an empty log rather than crashing the whole
    pipeline on a single bad write."""
    log_path = tmp_path / "audit_log.json"
    log_path.write_text("{not valid json")
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(log_path))

    execute.append_to_audit_log({"payment_id": "pay_after_corruption", "status": "resolved"})

    log = _read_audit_log(str(log_path))
    assert len(log) == 1
    assert log[0]["payment_id"] == "pay_after_corruption"



def test_concurrent_audit_log_writes_are_not_lost(tmp_path, monkeypatch):
    """Regression test for a real, confirmed bug: without a file lock,
    concurrent calls to append_to_audit_log() silently lose writes
    (observed: 19/20 lost in a live reproduction). This test fails if
    the file-lock fix is ever removed or bypassed."""
    import threading

    log_path = tmp_path / "concurrent_audit_log.json"
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(log_path))

    num_threads = 20

    def worker(i):
        execute.append_to_audit_log({"payment_id": f"pay_concurrent_{i}", "status": "resolved"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log = _read_audit_log(str(log_path))
    expected_ids = {f"pay_concurrent_{i}" for i in range(num_threads)}
    actual_ids = {entry["payment_id"] for entry in log}

    assert actual_ids == expected_ids
    assert len(log) == num_threads
if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])

