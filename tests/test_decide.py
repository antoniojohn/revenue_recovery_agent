"""
Tests for the decision layer's policy gate.

decide.py's boundaries (MIN_RETRY_AMOUNT, MAX_RETRIES) now come from
agent/settings_store.py (SQLite-backed, editable via the admin panel)
instead of hardcoded constants. Every test below is isolated from the
real instance/settings.db and logs/settings_audit_log.json via an
autouse fixture, so running this file never touches the DB or log used
by the actual pipeline / admin panel, and always runs against the
documented defaults (₹150 minimum, same per-cause caps as before).

Run with: pytest tests/test_decide.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import settings_store
from agent.decide import choose_action, MIN_RETRY_AMOUNT


@pytest.fixture(autouse=True)
def isolated_settings_db(tmp_path, monkeypatch):
    """Point the dynamic settings store at a throwaway DB and audit log
    for every test in this file, so these tests never read or write
    the real instance/settings.db or logs/settings_audit_log.json used
    by the actual pipeline / admin panel."""
    monkeypatch.setattr(settings_store, "DB_PATH", str(tmp_path / "settings.db"))
    monkeypatch.setattr(settings_store, "SETTINGS_AUDIT_LOG_PATH", str(tmp_path / "settings_audit_log.json"))
    yield


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
    exactly at the live minimum-retry-amount setting should NOT be
    rejected. Uses settings_store directly (not the decide.py import
    snapshot) since the fixture's isolated DB is what choose_action()
    actually reads at call time."""
    threshold = settings_store.get_min_retry_amount()
    record = {"payment_id": "pay_test_boundary", "amount": threshold}
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


def test_admin_panel_change_to_min_retry_amount_takes_effect_immediately():
    """This is the point of the whole Dynamic Configuration feature:
    choose_action() must reflect a settings_store update on its very
    next call, with no restart or re-import required."""
    record = {"payment_id": "pay_test_dynamic", "amount": 300}

    before = choose_action("CARD_DECLINED", record, source="rule")
    assert before["policy_approved"] is True  # ₹300 clears the ₹150 default

    settings_store.update_min_retry_amount(500, updated_by="test_admin")

    after = choose_action("CARD_DECLINED", record, source="rule")
    assert after["policy_approved"] is False  # ₹300 no longer clears ₹500
    assert after["type"] == "ESCALATE"


def test_admin_panel_change_to_max_retries_takes_effect_immediately():
    """Same immediacy guarantee for per-cause retry caps."""
    record = {"payment_id": "pay_test_dynamic_caps", "amount": 999}

    before = choose_action("INSUFFICIENT_FUNDS", record, source="rule")
    assert before["max_attempts"] == 2  # documented default

    settings_store.update_max_retries("INSUFFICIENT_FUNDS", 5, updated_by="test_admin")

    after = choose_action("INSUFFICIENT_FUNDS", record, source="rule")
    assert after["max_attempts"] == 5


def test_high_amount_retry_requires_afa_and_is_escalated():
    """A retry-eligible cause above the AFA threshold must be escalated,
    not auto-retried - this is a regulatory boundary (NPCI e-mandate AFA
    requirement), not a business-tunable one, so it must hold regardless
    of the current min_retry_amount/max_retries settings."""
    record = {"payment_id": "pay_test_afa", "amount": 19999}
    action = choose_action("CARD_DECLINED", record, source="rule")

    assert action["type"] == "ESCALATE"
    assert action["max_attempts"] == 0
    assert action["policy_approved"] is False
    assert "AFA" in action["policy_note"]


def test_amount_exactly_at_afa_threshold_is_not_escalated():
    """The AFA gate uses a strict greater-than comparison, so an amount
    exactly at the threshold should NOT trigger it - only amounts
    strictly above ₹15,000 require renewed authentication."""
    from agent.decide import AFA_REQUIRED_ABOVE_AMOUNT
    record = {"payment_id": "pay_test_afa_boundary", "amount": AFA_REQUIRED_ABOVE_AMOUNT}
    action = choose_action("BANK_TIMEOUT", record, source="rule")

    assert action["policy_approved"] is True
    assert action["type"] == "RETRY_IMMEDIATE"


def test_afa_gate_takes_precedence_over_admin_configured_min_retry_amount():
    """Even if an admin loosens/raises min_retry_amount via the panel,
    the AFA threshold must still hold - it's a compliance boundary, not
    something a business owner's settings change can override."""
    settings_store.update_min_retry_amount(1, updated_by="test_admin")
    record = {"payment_id": "pay_test_afa_override_attempt", "amount": 25000}

    action = choose_action("CARD_DECLINED", record, source="rule")

    assert action["type"] == "ESCALATE"
    assert action["policy_approved"] is False
    assert "AFA" in action["policy_note"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])