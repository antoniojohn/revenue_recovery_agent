"""
Tests for the B2B Receivables Chaser pipeline (agent/receivables.py).

Covers:
  1. Days-overdue classification (0-7 / 8-30 / 30+).
  2. The collections policy gate: small invoices get downgraded from
     ESCALATE_TO_COLLECTIONS to FIRM_REMINDER rather than silently
     escalated.
  3. Concurrency: the same file-lock fix applied to execute.py's
     append_to_audit_log, verified independently here since this
     module has its own separate implementation, not a shared import.

Run with: pytest tests/test_receivables.py -v
"""

import json
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import receivables


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(receivables, "AUDIT_LOG_PATH", str(tmp_path / "receivables_audit_log.json"))
    monkeypatch.setattr(receivables, "SUMMARY_PATH", str(tmp_path / "receivables_summary.json"))
    yield


def test_classify_zero_to_seven_days():
    record = {"days_overdue": 3}
    assert receivables.classify_invoice(record) == "DAYS_OVERDUE_0_7"


def test_classify_eight_to_thirty_days():
    record = {"days_overdue": 15}
    assert receivables.classify_invoice(record) == "DAYS_OVERDUE_8_30"


def test_classify_thirty_plus_days():
    record = {"days_overdue": 45}
    assert receivables.classify_invoice(record) == "DAYS_OVERDUE_30_PLUS"


def test_classify_boundary_at_exactly_seven_days():
    record = {"days_overdue": 7}
    assert receivables.classify_invoice(record) == "DAYS_OVERDUE_0_7"


def test_classify_boundary_at_exactly_thirty_days():
    record = {"days_overdue": 30}
    assert receivables.classify_invoice(record) == "DAYS_OVERDUE_8_30"


def test_collections_escalation_approved_above_threshold():
    record = {"amount": 50000}
    action = receivables.decide_action("DAYS_OVERDUE_30_PLUS", record)
    assert action["type"] == "ESCALATE_TO_COLLECTIONS"
    assert action["policy_approved"] is True


def test_collections_escalation_downgraded_below_threshold():
    record = {"amount": 500}
    action = receivables.decide_action("DAYS_OVERDUE_30_PLUS", record)
    assert action["type"] == "FIRM_REMINDER"
    assert action["policy_approved"] is False
    assert "below" in action["policy_note"]


def test_gentle_reminder_never_touches_collections_gate():
    record = {"amount": 50}
    action = receivables.decide_action("DAYS_OVERDUE_0_7", record)
    assert action["type"] == "GENTLE_REMINDER"
    assert action["policy_approved"] is True


def test_concurrent_audit_log_writes_are_not_lost(tmp_path, monkeypatch):
    log_path = tmp_path / "concurrent_receivables_log.json"
    monkeypatch.setattr(receivables, "AUDIT_LOG_PATH", str(log_path))
    num_threads = 20

    def worker(i):
        receivables._append_to_audit_log({"invoice_id": f"inv_concurrent_{i}", "status": "resolved"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(log_path) as f:
        final_log = json.load(f)

    expected_ids = {f"inv_concurrent_{i}" for i in range(num_threads)}
    actual_ids = {entry["invoice_id"] for entry in final_log}

    assert actual_ids == expected_ids
    assert len(final_log) == num_threads


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
