"""
Tests for the Checkout Drop-off Recovery pipeline (agent/checkout_recovery.py).

Covers:
  1. Time-based classification (UNDER_1HR / 1_TO_24HR / OVER_24HR).
  2. The discount policy gate: small carts get downgraded from
     DISCOUNT_OFFER to EMAIL_NUDGE rather than silently discounted.
  3. Concurrency: the same file-lock fix applied to execute.py's
     append_to_audit_log, verified independently here since this
     module has its own separate implementation, not a shared import.

Run with: pytest tests/test_checkout_recovery.py -v
"""

import json
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import checkout_recovery


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(checkout_recovery, "AUDIT_LOG_PATH", str(tmp_path / "checkout_audit_log.json"))
    monkeypatch.setattr(checkout_recovery, "SUMMARY_PATH", str(tmp_path / "checkout_summary.json"))
    yield


def test_classify_under_one_hour():
    record = {"time_since_abandonment_hours": 0.5}
    assert checkout_recovery.classify_checkout(record) == "UNDER_1HR"


def test_classify_one_to_twenty_four_hours():
    record = {"time_since_abandonment_hours": 12}
    assert checkout_recovery.classify_checkout(record) == "1_TO_24HR"


def test_classify_over_twenty_four_hours():
    record = {"time_since_abandonment_hours": 48}
    assert checkout_recovery.classify_checkout(record) == "OVER_24HR"


def test_classify_boundary_at_exactly_one_hour():
    record = {"time_since_abandonment_hours": 1}
    assert checkout_recovery.classify_checkout(record) == "1_TO_24HR"


def test_classify_boundary_at_exactly_twenty_four_hours():
    record = {"time_since_abandonment_hours": 24}
    assert checkout_recovery.classify_checkout(record) == "1_TO_24HR"


def test_discount_offer_approved_above_threshold():
    record = {"amount": 5000}
    action = checkout_recovery.decide_action("1_TO_24HR", record)
    assert action["type"] == "DISCOUNT_OFFER"
    assert action["policy_approved"] is True


def test_discount_offer_downgraded_below_threshold():
    record = {"amount": 500}
    action = checkout_recovery.decide_action("1_TO_24HR", record)
    assert action["type"] == "EMAIL_NUDGE"
    assert action["policy_approved"] is False
    assert "below" in action["policy_note"]


def test_under_one_hour_always_gets_email_nudge_regardless_of_amount():
    record = {"amount": 50}
    action = checkout_recovery.decide_action("UNDER_1HR", record)
    assert action["type"] == "EMAIL_NUDGE"
    assert action["policy_approved"] is True


def test_concurrent_audit_log_writes_are_not_lost(tmp_path, monkeypatch):
    log_path = tmp_path / "concurrent_checkout_log.json"
    monkeypatch.setattr(checkout_recovery, "AUDIT_LOG_PATH", str(log_path))
    num_threads = 20

    def worker(i):
        checkout_recovery._append_to_audit_log({"checkout_id": f"chk_concurrent_{i}", "status": "resolved"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(log_path) as f:
        final_log = json.load(f)

    expected_ids = {f"chk_concurrent_{i}" for i in range(num_threads)}
    actual_ids = {entry["checkout_id"] for entry in final_log}

    assert actual_ids == expected_ids
    assert len(final_log) == num_threads


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
