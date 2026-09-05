"""
Tests for the reporting layer (agent/report.py).

Covers:
  - The three-way split: recovered / exception / pending, and that
    pending cases are excluded from recovery_rate entirely.
  - Real (API-confirmed) vs. simulated outcomes are kept as separate
    figures, never silently blended into one "the" number.
  - Backward compatibility: an outcome dict missing fields (older
    audit log format, or a hand-edited entry) must not crash
    summarize() - every field access should degrade gracefully via
    .get() with a sensible default, per report.py's own docstring
    promise.
  - _write_summary() creates its output directory if missing, same as
    every other file-writing function in this codebase.

Run with: pytest tests/test_report.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import report


def _make_outcome(**overrides):
    """A fully-populated, realistic outcome dict - the shape execute.py
    actually produces. Tests override only the fields they care about."""
    base = {
        "status": "resolved",
        "resolution_method": "simulated",
        "payment_id": "pay_test_1",
        "amount": 500,
        "cause": "CARD_DECLINED",
        "source": "rule",
        "action_type": "RETRY_IMMEDIATE",
        "max_attempts": 1,
        "attempts_used": 1,
        "attempts": [{"attempt": 1, "succeeded": True}],
        "policy_approved": True,
        "policy_note": "within bounds",
        "reasoning": "test reasoning",
        "recovered": True,
        "verified_live": False,
        "live_status": None,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_recovered_and_exception_cases_are_split_correctly():
    """A resolved+recovered case counts toward recovered_cases; a
    resolved+not-recovered case counts toward exception_cases."""
    results = [
        _make_outcome(payment_id="pay_1", recovered=True),
        _make_outcome(payment_id="pay_2", recovered=False),
    ]

    summary = report.summarize(results)

    assert summary["recovered_cases"] == 1
    assert summary["exception_cases"] == 1
    assert summary["total_cases"] == 2


def test_pending_cases_are_excluded_from_recovery_rate():
    """A pending case is neither recovered nor an exception yet - it
    must not distort recovery_rate_percent_blended in either
    direction."""
    results = [
        _make_outcome(payment_id="pay_1", recovered=True, resolution_method="webhook"),
        _make_outcome(payment_id="pay_2", status="pending", recovered=None,
                      resolution_method=None, amount=1000),
    ]

    summary = report.summarize(results)

    assert summary["pending_cases"] == 1
    assert summary["resolved_cases"] == 1
    # Recovery rate is over resolved cases only: 1/1 = 100%, not 1/2.
    assert summary["recovery_rate_percent_blended"] == 100.0
    assert summary["pending_amount"] == 1000


def test_real_and_simulated_outcomes_are_reported_separately():
    """A webhook-confirmed outcome and a probability-draw outcome must
    never be blended into revenue_recovered_real - each has its own
    figure, matching the docstring's REAL vs. PROJECTED distinction."""
    results = [
        _make_outcome(payment_id="pay_real", amount=300, recovered=True,
                      resolution_method="webhook"),
        _make_outcome(payment_id="pay_sim", amount=700, recovered=True,
                      resolution_method="simulated"),
    ]

    summary = report.summarize(results)

    assert summary["revenue_recovered_real"] == 300
    assert summary["projected_revenue_recovered_simulated"] == 700
    assert summary["real_resolved_cases"] == 1
    assert summary["simulated_resolved_cases"] == 1
    # Blended figure still sums both, but is separately labeled.
    assert summary["recovered_amount_blended"] == 1000


def test_missing_resolution_method_is_treated_as_simulated():
    """Pre-webhook-migration audit log entries have no
    resolution_method field at all - these must be treated as
    simulated/projected, not silently dropped or miscounted as real."""
    results = [_make_outcome(payment_id="pay_old", recovered=True, resolution_method=None)]
    del results[0]["resolution_method"]

    summary = report.summarize(results)

    assert summary["real_resolved_cases"] == 0
    assert summary["simulated_resolved_cases"] == 1


def test_missing_status_field_is_treated_as_resolved():
    """An outcome with no 'status' key at all (older audit log format,
    predating the pending/resolved distinction) must be treated as
    resolved, not silently excluded or crash the report."""
    results = [_make_outcome(payment_id="pay_legacy", recovered=True)]
    del results[0]["status"]

    summary = report.summarize(results)

    assert summary["resolved_cases"] == 1
    assert summary["pending_cases"] == 0
    assert summary["recovered_cases"] == 1


def test_outcome_missing_optional_fields_does_not_crash():
    """A sparse/legacy outcome missing 'cause', 'action_type',
    'reasoning', and 'amount' must not raise KeyError - every field
    access in summarize() should degrade to a safe default instead."""
    sparse = {"status": "resolved", "recovered": False, "payment_id": "pay_sparse"}

    summary = report.summarize([sparse])

    assert summary["total_cases"] == 1
    assert summary["exception_cases"] == 1
    assert summary["cases_by_cause"]["UNKNOWN"] == 1
    assert summary["exceptions_by_cause"]["UNKNOWN"] == 1
    assert summary["total_amount"] == 0


def test_outcome_missing_recovered_key_counts_as_exception():
    """A resolved outcome with no 'recovered' key at all must default
    to False (an exception), not crash on missing key."""
    sparse = {"status": "resolved", "payment_id": "pay_no_recovered", "amount": 100}

    summary = report.summarize([sparse])

    assert summary["exception_cases"] == 1
    assert summary["recovered_cases"] == 0


def test_escalated_cases_counts_only_escalate_action_type():
    """escalated_cases should count exceptions whose action_type is
    specifically ESCALATE, not every exception generally."""
    results = [
        _make_outcome(payment_id="pay_escalated", recovered=False, action_type="ESCALATE"),
        _make_outcome(payment_id="pay_other_exception", recovered=False, action_type="NOTIFY_USER"),
    ]

    summary = report.summarize(results)

    assert summary["escalated_cases"] == 1


def test_decisions_by_source_counts_rule_and_llm():
    """Every case's classification source (rule vs. llm) must be
    tallied, defaulting to 'rule' for entries with no source field."""
    results = [
        _make_outcome(payment_id="pay_1", source="rule"),
        _make_outcome(payment_id="pay_2", source="llm"),
        _make_outcome(payment_id="pay_3"),
    ]
    del results[2]["source"]

    summary = report.summarize(results)

    assert summary["decisions_by_source"]["rule"] == 2
    assert summary["decisions_by_source"]["llm"] == 1


def test_write_summary_creates_missing_directory(tmp_path):
    """_write_summary must create its target directory if it doesn't
    exist yet, matching every other file-writing function in this
    codebase - a fresh checkout with no logs/ dir must not crash on
    the first report."""
    nested_path = tmp_path / "fresh_logs_dir" / "summary_report.json"
    assert not nested_path.parent.exists()

    report._write_summary({"total_cases": 0}, path=str(nested_path))

    assert nested_path.exists()
    with open(nested_path) as f:
        written = json.load(f)
    assert written == {"total_cases": 0}


def test_empty_results_list_is_a_clean_no_op(tmp_path, monkeypatch):
    """Running summarize() with zero results (e.g. an empty batch)
    must not crash and must report all-zero figures."""
    monkeypatch.chdir(tmp_path)

    summary = report.summarize([])

    assert summary["total_cases"] == 0
    assert summary["recovered_cases"] == 0
    assert summary["recovery_rate_percent_blended"] == 0.0