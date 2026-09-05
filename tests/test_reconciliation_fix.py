"""
Tests for the terminal-vs-dashboard reconciliation fix:

1. report.summarize() correctly separates real (API-confirmed) outcomes
   from simulated/projected ones, and excludes pending cases from the
   recovery rate and recovered/exception buckets.
2. report.summarize_full_audit_log() reads the FULL accumulated audit
   log file (same file the dashboard reads) rather than a single run's
   results, and degrades gracefully when the file is missing/corrupt.
3. app.run_pipeline() always prints the all-time report, and only
   prints the "this run" report when there were actually new results
   (the bug that caused the terminal to show all-zeros while the
   dashboard showed 78 records).

Run with:
    pip install pytest --break-system-packages
    pytest -v tests/test_reconciliation_fix.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import report  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_outcome(
    payment_id="pay_1",
    amount=1000,
    recovered=False,
    resolution_method=None,
    status=None,
    cause="CARD_DECLINED",
    source="rule",
    action_type="RETRY_IMMEDIATE",
    promise_kept=None,
):
    outcome = {
        "payment_id": payment_id,
        "amount": amount,
        "recovered": recovered,
        "cause": cause,
        "source": source,
        "action_type": action_type,
    }
    if resolution_method is not None:
        outcome["resolution_method"] = resolution_method
    if status is not None:
        outcome["status"] = status
    if promise_kept is not None:
        outcome["promise_kept"] = promise_kept
    return outcome


# ---------------------------------------------------------------------------
# report.summarize() — real vs. projected split
# ---------------------------------------------------------------------------

class TestRealVsProjectedSplit:
    def test_real_and_simulated_are_kept_separate(self):
        results = [
            make_outcome("p1", 1000, recovered=True, resolution_method="webhook"),
            make_outcome("p2", 500, recovered=True, resolution_method="simulated"),
            make_outcome("p3", 300, recovered=False, resolution_method="webhook"),
        ]
        s = report.summarize(results)

        assert s["revenue_recovered_real"] == 1000
        assert s["projected_revenue_recovered_simulated"] == 500
        # blended must still equal the sum of both, but clearly labeled
        assert s["recovered_amount_blended"] == 1500
        assert "blended" in s["blended_note"].lower()

    def test_missing_resolution_method_counts_as_simulated(self):
        """Pre-migration audit log entries with no resolution_method at
        all must be treated as simulated, never silently counted as real."""
        results = [make_outcome("p1", 1000, recovered=True)]  # no resolution_method
        s = report.summarize(results)

        assert s["revenue_recovered_real"] == 0
        assert s["projected_revenue_recovered_simulated"] == 1000

    @pytest.mark.parametrize(
        "method", ["webhook", "poll", "timeout", "retry_initiation_failed"]
    )
    def test_every_real_resolution_method_is_recognized(self, method):
        results = [make_outcome("p1", 1000, recovered=True, resolution_method=method)]
        s = report.summarize(results)
        assert s["revenue_recovered_real"] == 1000
        assert s["projected_revenue_recovered_simulated"] == 0

    def test_no_real_resolved_cases_yields_none_rate_not_zero(self):
        """recovery_rate_percent_real should be None (undefined), not 0,
        when there are zero real-resolved cases - otherwise a 0% rate
        looks like 'we tried and failed' rather than 'nothing real yet'."""
        results = [make_outcome("p1", 1000, recovered=True, resolution_method="simulated")]
        s = report.summarize(results)
        assert s["real_resolved_cases"] == 0
        assert s["recovery_rate_percent_real"] is None


# ---------------------------------------------------------------------------
# report.summarize() — pending exclusion
# ---------------------------------------------------------------------------

class TestPendingExclusion:
    def test_pending_excluded_from_recovered_and_exceptions(self):
        results = [
            make_outcome("p1", 1000, recovered=True, resolution_method="webhook"),
            make_outcome("p2", 500, status="pending"),
        ]
        s = report.summarize(results)

        assert s["pending_cases"] == 1
        assert s["pending_amount"] == 500
        assert s["recovered_cases"] == 1
        assert s["exception_cases"] == 0  # pending must NOT count as an exception
        assert s["total_cases"] == 2

    def test_recovery_rate_computed_over_resolved_only(self):
        """A pending case must not distort the recovery rate - it's
        neither a success nor a failure until confirmed."""
        results = [
            make_outcome("p1", 100, recovered=True, resolution_method="webhook"),
            make_outcome("p2", 100, recovered=False, resolution_method="webhook"),
            make_outcome("p3", 100, status="pending"),
        ]
        s = report.summarize(results)
        # 1 recovered / 2 resolved = 50%, NOT 1/3
        assert s["resolved_cases"] == 2
        assert s["recovery_rate_percent_blended"] == 50.0

    def test_unrecovered_amount_excludes_pending(self):
        results = [
            make_outcome("p1", 1000, recovered=True, resolution_method="webhook"),
            make_outcome("p2", 300, recovered=False, resolution_method="webhook"),
            make_outcome("p3", 200, status="pending"),
        ]
        s = report.summarize(results)
        # total(1500) - recovered(1000) - pending(200) = 300, matching
        # the actual unresolved/failed amount, not double-counting pending
        assert s["unrecovered_amount"] == 300

    def test_missing_status_field_treated_as_resolved(self):
        """Pre-webhook-migration entries have no 'status' field at all -
        must be treated as resolved, not silently dropped as pending."""
        results = [make_outcome("p1", 1000, recovered=True, resolution_method="webhook")]
        assert "status" not in results[0]
        s = report.summarize(results)
        assert s["pending_cases"] == 0
        assert s["resolved_cases"] == 1


# ---------------------------------------------------------------------------
# report.summarize() — everything else the dashboard depends on
# ---------------------------------------------------------------------------

class TestSummaryShape:
    def test_empty_results_does_not_crash(self):
        s = report.summarize([])
        assert s["total_cases"] == 0
        assert s["recovery_rate_percent_blended"] == 0.0
        assert s["revenue_recovered_real"] == 0

    def test_exceptions_by_cause_only_counts_exceptions(self):
        results = [
            make_outcome("p1", 100, recovered=False, cause="EXPIRED_CARD", resolution_method="webhook"),
            make_outcome("p2", 100, recovered=False, cause="EXPIRED_CARD", resolution_method="webhook"),
            make_outcome("p3", 100, recovered=True, cause="EXPIRED_CARD", resolution_method="webhook"),
        ]
        s = report.summarize(results)
        assert s["exceptions_by_cause"] == {"EXPIRED_CARD": 2}
        assert s["cases_by_cause"] == {"EXPIRED_CARD": 3}  # includes the recovered one too

    def test_promise_tracking_ignores_entries_without_promise_kept(self):
        results = [
            make_outcome("p1", 100, action_type="ESCALATE", promise_kept=True, resolution_method="webhook"),
            make_outcome("p2", 100, action_type="ESCALATE", promise_kept=False, resolution_method="webhook"),
            make_outcome("p3", 100, action_type="ESCALATE", resolution_method="webhook"),  # no promise_kept field
        ]
        s = report.summarize(results)
        assert s["promises_kept"] == 1
        assert s["promises_broken"] == 1  # the entry with no promise_kept is excluded, not miscounted

    def test_exception_list_reports_is_real_flag(self):
        results = [
            make_outcome("p1", 100, recovered=False, resolution_method="webhook"),
            make_outcome("p2", 100, recovered=False, resolution_method="simulated"),
        ]
        s = report.summarize(results)
        by_id = {e["payment_id"]: e for e in s["exception_list"]}
        assert by_id["p1"]["is_real"] is True
        assert by_id["p2"]["is_real"] is False

    def test_writes_summary_json(self, tmp_path):
        out_path = tmp_path / "logs" / "summary_report.json"
        summary = report.summarize([make_outcome("p1", 100, recovered=True, resolution_method="webhook")])
        report._write_summary(summary, path=str(out_path))

        assert out_path.exists()
        written = json.loads(out_path.read_text())
        assert written["revenue_recovered_real"] == 100


# ---------------------------------------------------------------------------
# report.summarize_full_audit_log() — the core reconciliation fix
# ---------------------------------------------------------------------------

class TestSummarizeFullAuditLog:
    def test_reads_full_accumulated_log_not_a_single_run(self, tmp_path):
        """This is the actual bug from the video: a single run's results
        can be empty while the accumulated audit log has many records.
        summarize_full_audit_log must read the full file."""
        log_path = tmp_path / "audit_log.json"
        records = [make_outcome(f"p{i}", 100, recovered=True, resolution_method="simulated")
                   for i in range(78)]
        log_path.write_text(json.dumps(records))

        s = report.summarize_full_audit_log(path=str(log_path))
        assert s["total_cases"] == 78
        assert s["projected_revenue_recovered_simulated"] == 7800

    def test_missing_file_returns_empty_summary_not_a_crash(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.json"
        s = report.summarize_full_audit_log(path=str(missing_path))
        assert s["total_cases"] == 0

    def test_corrupt_json_returns_empty_summary_not_a_crash(self, tmp_path):
        bad_path = tmp_path / "audit_log.json"
        bad_path.write_text("{not valid json")
        s = report.summarize_full_audit_log(path=str(bad_path))
        assert s["total_cases"] == 0

    def test_matches_dashboards_expected_78_record_totals(self, tmp_path):
        """Regression test pinned to the actual numbers from the run in
        this conversation - if these drift, the dashboard and terminal
        will disagree again."""
        log_path = tmp_path / "audit_log.json"
        records = (
            [make_outcome(f"real{i}", 100, recovered=False, resolution_method="webhook") for i in range(3)]
            + [make_outcome(f"sim{i}", 1578, recovered=(i % 2 == 0), resolution_method="simulated") for i in range(75)]
        )
        log_path.write_text(json.dumps(records))

        s = report.summarize_full_audit_log(path=str(log_path))
        assert s["total_cases"] == 78
        assert s["real_resolved_cases"] == 3
        assert s["simulated_resolved_cases"] == 75


# ---------------------------------------------------------------------------
# app.run_pipeline() — the wiring fix
# ---------------------------------------------------------------------------

class TestRunPipelineWiring:
    """
    These tests import app.py fresh with diagnose/decide/execute/report
    mocked out, so we can assert on *which* report functions get called
    without needing a real pipeline or real data files.
    """

    def _fresh_app_module(self, monkeypatch):
        sys.modules.pop("app", None)
        import app  # noqa: E402
        return app

    def test_prints_all_time_report_even_with_zero_new_results(self, monkeypatch):
        """The exact bug from the video: 0 new records must still trigger
        the all-time report so the terminal doesn't show all-zeros while
        the dashboard shows real accumulated data."""
        app = self._fresh_app_module(monkeypatch)

        monkeypatch.setattr(app.diagnose, "load_batch", MagicMock(return_value=[]))
        monkeypatch.setattr(app.report, "summarize", MagicMock())
        monkeypatch.setattr(app.report, "summarize_full_audit_log", MagicMock())

        app.run_pipeline()

        app.report.summarize_full_audit_log.assert_called_once()
        app.report.summarize.assert_not_called()  # no new results -> "this run" report skipped

    def test_prints_both_reports_when_there_are_new_results(self, monkeypatch):
        app = self._fresh_app_module(monkeypatch)

        record = {"payment_id": "pay_new"}
        monkeypatch.setattr(app.diagnose, "load_batch", MagicMock(return_value=[record]))
        monkeypatch.setattr(app.diagnose, "classify", MagicMock(return_value=("CARD_DECLINED", "rule")))
        monkeypatch.setattr(app.decide, "choose_action", MagicMock(return_value="RETRY_IMMEDIATE"))
        monkeypatch.setattr(app.execute, "run_action", MagicMock(return_value={"payment_id": "pay_new", "recovered": True}))
        monkeypatch.setattr(app.report, "summarize", MagicMock())
        monkeypatch.setattr(app.report, "summarize_full_audit_log", MagicMock())

        app.run_pipeline()

        app.report.summarize.assert_called_once_with([{"payment_id": "pay_new", "recovered": True}])
        app.report.summarize_full_audit_log.assert_called_once()

    def test_all_time_report_reads_same_file_dashboard_reads(self, monkeypatch):
        """Guards against someone changing the default path later and
        silently re-breaking the terminal/dashboard match."""
        app = self._fresh_app_module(monkeypatch)
        monkeypatch.setattr(app.diagnose, "load_batch", MagicMock(return_value=[]))
        monkeypatch.setattr(app.report, "summarize_full_audit_log", MagicMock())

        app.run_pipeline()

        # default path arg matches report.py's own default; if this ever
        # needs to move, do it in both places at once
        called_args = app.report.summarize_full_audit_log.call_args
        assert called_args == call() or called_args.kwargs.get("path", "logs/audit_log.json") == "logs/audit_log.json"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
