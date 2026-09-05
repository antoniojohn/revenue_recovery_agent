"""
Tests for the polling fallback that resolves pending retries when no
webhook arrives (agent/reconcile_pending.py).

Covers the three outcomes reconcile_once() must distinguish:
  - order status 'paid'                          -> resolved, recovered=True
  - order status not final yet, still fresh       -> left pending
  - order status not final yet, past the timeout  -> resolved,
    recovered=False, as an "unconfirmed" escalation

All filesystem paths are monkeypatched to throwaway locations, and
razorpay_client.check_order_status is monkeypatched to return
controlled values instead of making real API calls - these tests run
offline and never touch the real Razorpay test account.

Run with: pytest tests/test_reconcile_pending.py -v
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import reconcile_pending, pending_store, execute, razorpay_client


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_store, "PENDING_STORE_PATH", str(tmp_path / "pending_retries.json"))
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.json"))
    yield


def _add_pending(order_id, initiated_at_iso, **overrides):
    entry = {
        "order_id": order_id,
        "original_payment_id": f"pay_{order_id}",
        "amount": 500,
        "cause": "CARD_DECLINED",
        "source": "rule",
        "action_type": "RETRY_IMMEDIATE",
        "max_attempts": 1,
        "policy_approved": True,
        "policy_note": "within bounds",
        "reasoning": "test",
        "initiated_at": initiated_at_iso,
    }
    entry.update(overrides)
    pending_store.add_pending(entry)


def _read_audit_log():
    with open(execute.AUDIT_LOG_PATH) as f:
        return json.load(f)


def test_paid_order_is_resolved_as_recovered(monkeypatch):
    """An order Razorpay reports as 'paid' must be resolved with
    recovered=True and resolution_method='poll', and removed from the
    pending store - this is the whole point of the polling fallback."""
    now = datetime.now(timezone.utc)
    _add_pending("order_paid", now.isoformat())
    monkeypatch.setattr(razorpay_client, "check_order_status", lambda order_id: "paid")

    summary = reconcile_pending.reconcile_once(now=now)

    assert summary["resolved_paid"] == 1
    assert summary["still_pending"] == 0
    assert pending_store.get_pending("order_paid") is None

    resolved = [r for r in _read_audit_log() if r["order_id"] == "order_paid"]
    assert resolved[0]["recovered"] is True
    assert resolved[0]["resolution_method"] == "poll"


def test_unpaid_order_within_timeout_stays_pending(monkeypatch):
    """An order that's still 'created' (customer hasn't paid yet) and
    is well within the timeout window must be left pending, not
    prematurely resolved as a failure."""
    now = datetime.now(timezone.utc)
    _add_pending("order_fresh", now.isoformat())
    monkeypatch.setattr(razorpay_client, "check_order_status", lambda order_id: "created")

    summary = reconcile_pending.reconcile_once(now=now)

    assert summary["still_pending"] == 1
    assert summary["resolved_paid"] == 0
    assert summary["resolved_timeout"] == 0
    assert pending_store.get_pending("order_fresh") is not None


def test_unpaid_order_past_timeout_is_resolved_as_unconfirmed(monkeypatch):
    """An order that's been sitting unpaid for longer than
    PENDING_TIMEOUT_MINUTES must be resolved as recovered=False rather
    than left pending forever - this is what keeps pending_cases from
    silently accumulating stuck records indefinitely."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=reconcile_pending.PENDING_TIMEOUT_MINUTES + 5)
    _add_pending("order_stale", stale_time.isoformat())
    monkeypatch.setattr(razorpay_client, "check_order_status", lambda order_id: "created")

    summary = reconcile_pending.reconcile_once(now=now)

    assert summary["resolved_timeout"] == 1
    assert summary["still_pending"] == 0
    assert pending_store.get_pending("order_stale") is None

    resolved = [r for r in _read_audit_log() if r["order_id"] == "order_stale"]
    assert resolved[0]["recovered"] is False
    assert resolved[0]["resolution_method"] == "timeout"


def test_order_exactly_at_timeout_boundary_stays_pending(monkeypatch):
    """The timeout check uses a strict greater-than comparison, so an
    order exactly at PENDING_TIMEOUT_MINUTES old should NOT yet be
    resolved - only orders older than the threshold are."""
    now = datetime.now(timezone.utc)
    boundary_time = now - timedelta(minutes=reconcile_pending.PENDING_TIMEOUT_MINUTES)
    _add_pending("order_boundary", boundary_time.isoformat())
    monkeypatch.setattr(razorpay_client, "check_order_status", lambda order_id: "created")

    summary = reconcile_pending.reconcile_once(now=now)

    assert summary["still_pending"] == 1
    assert summary["resolved_timeout"] == 0


def test_empty_pending_store_is_a_clean_no_op():
    """Running reconcile with nothing pending must not error - this is
    the expected state most of the time in a low-volume demo."""
    summary = reconcile_pending.reconcile_once()

    assert summary == {
        "checked": 0, "resolved_paid": 0, "resolved_timeout": 0,
        "still_pending": 0, "errors": 0,
    }


def test_multiple_pending_orders_are_each_checked_independently(monkeypatch):
    """With several orders pending at once, each must be resolved (or
    not) based on its own status/age - one paid order resolving must
    not affect a separate still-pending one."""
    now = datetime.now(timezone.utc)
    _add_pending("order_a", now.isoformat())
    _add_pending("order_b", now.isoformat())

    def fake_status(order_id):
        return "paid" if order_id == "order_a" else "created"
    monkeypatch.setattr(razorpay_client, "check_order_status", fake_status)

    summary = reconcile_pending.reconcile_once(now=now)

    assert summary["checked"] == 2
    assert summary["resolved_paid"] == 1
    assert summary["still_pending"] == 1
    assert pending_store.get_pending("order_a") is None
    assert pending_store.get_pending("order_b") is not None