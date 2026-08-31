"""
Tests for the decision layer's policy gate.

Run with: pytest tests/test_decide.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.decide import choose_action, MIN_RETRY_AMOUNT


def test_low_amount_retry_is_rejected_by_policy_gate():
    """A retry-eligible cause below the minimum amount must be downgraded
    to ESCALATE, not silently retried."""
    record = {"payment_id": "pay_test_low", "amount": 99}
    action = choose_action("INSUFFICIENT_FUNDS", record, source="rule")

    assert action["type"] == "ESCALATE"
    assert action["max_attempts"] == 0
    assert action["policy_approved"] is False
    assert "below" in action["policy_note"]


def test_sufficient_amount_retry_is_approved():
    """The same cause, above the threshold, should retry normally and
    not be touched by the policy gate."""
    record = {"payment_id": "pay_test_ok", "amount": 999}
    action = choose_action("INSUFFICIENT_FUNDS", record, source="rule")

    assert action["type"] == "RETRY_AFTER_DELAY"
    assert action["max_attempts"] == 2
    assert action["policy_approved"] is True
    assert action["policy_note"] == "within bounds"


def test_amount_exactly_at_threshold_is_not_rejected():
    """The policy gate uses a strict less-than comparison, so an amount
    exactly at MIN_RETRY_AMOUNT should NOT be rejected."""
    record = {"payment_id": "pay_test_boundary", "amount": MIN_RETRY_AMOUNT}
    action = choose_action("BANK_TIMEOUT", record, source="rule")

    assert action["policy_approved"] is True
    assert action["type"] == "RETRY_IMMEDIATE"


def test_non_retry_causes_are_unaffected_by_policy_gate():
    """Causes that were never going to retry (e.g. EXPIRED_CARD) should
    pass through unaffected by the amount threshold, even below it."""
    record = {"payment_id": "pay_test_notify", "amount": 50}
    action = choose_action("EXPIRED_CARD", record, source="rule")

    assert action["type"] == "NOTIFY_USER_CARD_UPDATE"
    assert action["policy_approved"] is True
    assert action["policy_note"] == "within bounds"


def test_unknown_cause_defaults_to_escalate():
    """An unrecognized cause bucket should always escalate, regardless
    of amount."""
    record = {"payment_id": "pay_test_unknown", "amount": 5000}
    action = choose_action("UNKNOWN", record, source="llm")

    assert action["type"] == "ESCALATE"
    assert action["max_attempts"] == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
