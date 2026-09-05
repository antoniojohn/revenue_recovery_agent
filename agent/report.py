"""
Reporting layer: takes the list of outcomes produced by execute.py and
summarizes them into the metrics that matter for revenue recovery:
recovery rate, total amount recovered, and an honest list of cases that
were NOT recovered (exceptions), broken down by cause.

Deliberately does not hide or cherry-pick failures — the exception list
is meant to be reported as-is.

Since real retries became asynchronous (initiated via a webhook-
confirmed order rather than decided synchronously — see execute.py),
an outcome can now be in three states, not two:

  - resolved + recovered=True   -> counted as recovered
  - resolved + recovered=False  -> counted as an exception
  - status="pending"            -> NOT yet known either way; excluded
    from recovered/exception/recovery_rate entirely, and reported
    separately as pending_cases so the recovery rate isn't silently
    distorted by cases that just haven't been confirmed yet.

Outcomes with no "status" field at all (pre-webhook-migration audit
log entries) are treated as resolved, since every outcome was decided
synchronously before this change — this keeps summarize() usable
against older audit_log.json files without special-casing the format.
Field access throughout uses .get() with sensible defaults for the
same reason - an older or hand-edited audit log entry missing a field
should degrade gracefully, not crash the whole report.

REAL vs. PROJECTED, and why this file separates them:
Every resolved outcome carries a `resolution_method` (see execute.py,
webhook_server.py, reconcile_pending.py). Some of those methods mean a
genuine Razorpay API round-trip decided the outcome ("webhook", "poll",
"timeout", "retry_initiation_failed" — the last being a real API call
that itself failed, still a real result, just a negative one). Exactly
one method, "simulated" (or a missing field, from before this
distinction existed), means the outcome was a probability draw against
SIMULATED_SUCCESS_RATE, not a live result. Blending these into one
"Amount recovered" figure would materially overstate what's real, so
this file reports them as two separate numbers everywhere - in the
JSON, in the printed summary, and the blended combined figure that's
still provided for convenience is explicitly labeled as blended rather
than presented as if it were a single ground-truth number.

PROMISE-TO-PAY:
For NOTIFY_*/ESCALATE outcomes specifically, execute.py also simulates
a "promise to pay" - the real-world pattern where a human/customer
contact results in a promised future payment date rather than an
automated retry. promises_kept/promises_broken are reported alongside
(not blended into) the recovered/exception split, since a kept promise
is a *future* recovery signal, not a completed one at report time.
"""

import json
import os

REAL_RESOLUTION_METHODS = {"webhook", "poll", "timeout", "retry_initiation_failed"}
# "simulated" and unset (older, pre-distinction audit log entries) are
# both treated as projected/simulated - unset predates this file even
# tracking the distinction, and every outcome before that was in fact
# a probability draw, not a live result.

PROMISE_ACTION_TYPES = {"NOTIFY_USER", "NOTIFY_USER_CARD_UPDATE", "ESCALATE"}


def _is_real(outcome: dict) -> bool:
    return outcome.get("resolution_method") in REAL_RESOLUTION_METHODS


def summarize(results: list[dict]) -> dict:
    pending_cases = [r for r in results if r.get("status") == "pending"]
    resolved_cases = [r for r in results if r.get("status", "resolved") != "pending"]

    real_resolved = [r for r in resolved_cases if _is_real(r)]
    simulated_resolved = [r for r in resolved_cases if not _is_real(r)]

    recovered_cases = [r for r in resolved_cases if r.get("recovered", False)]
    exception_cases = [r for r in resolved_cases if not r.get("recovered", False)]

    recovered_real = [r for r in real_resolved if r.get("recovered", False)]
    recovered_projected = [r for r in simulated_resolved if r.get("recovered", False)]

    total_cases = len(results)
    resolved_count = len(resolved_cases)

    total_amount = sum(r.get("amount") or 0 for r in results)
    recovered_amount = sum(r.get("amount") or 0 for r in recovered_cases)
    recovered_amount_real = sum(r.get("amount") or 0 for r in recovered_real)
    recovered_amount_projected = sum(r.get("amount") or 0 for r in recovered_projected)
    pending_amount = sum(r.get("amount") or 0 for r in pending_cases)

    # Recovery rate is computed over resolved cases only - a pending
    # case is neither a success nor a failure yet, and folding it into
    # either bucket would misrepresent the rate until it's confirmed.
    recovery_rate = (len(recovered_cases) / resolved_count * 100) if resolved_count else 0.0
    recovery_rate_real = (
        len(recovered_real) / len(real_resolved) * 100
    ) if real_resolved else None
    recovery_rate_projected = (
        len(recovered_projected) / len(simulated_resolved) * 100
    ) if simulated_resolved else None

    exceptions_by_cause: dict[str, int] = {}
    for r in exception_cases:
        cause = r.get("cause", "UNKNOWN")
        exceptions_by_cause[cause] = exceptions_by_cause.get(cause, 0) + 1

    # Full breakdown across every case, including pending - this is
    # what a "failure categories" chart needs, since exceptions_by_cause
    # alone only shows the unresolved slice.
    cases_by_cause: dict[str, int] = {}
    for r in results:
        cause = r.get("cause", "UNKNOWN")
        cases_by_cause[cause] = cases_by_cause.get(cause, 0) + 1

    decisions_by_source = {"rule": 0, "llm": 0}
    for r in results:
        source = r.get("source", "rule")
        decisions_by_source[source] = decisions_by_source.get(source, 0) + 1

    escalated_cases = [r for r in exception_cases if r.get("action_type") == "ESCALATE"]

    # Promise-to-pay: only counted for outcomes where execute.py
    # actually recorded a promise (action_type in PROMISE_ACTION_TYPES
    # AND promise_kept is not None - older audit log entries predating
    # this feature have no promise_kept field at all and are correctly
    # excluded, not miscounted as broken promises).
    promise_cases = [
        r for r in resolved_cases
        if r.get("action_type") in PROMISE_ACTION_TYPES and r.get("promise_kept") is not None
    ]
    promises_kept = sum(1 for r in promise_cases if r["promise_kept"])
    promises_broken = len(promise_cases) - promises_kept

    summary = {
        "total_cases": total_cases,
        "resolved_cases": resolved_count,
        "real_resolved_cases": len(real_resolved),
        "simulated_resolved_cases": len(simulated_resolved),
        "recovered_cases": len(recovered_cases),
        "exception_cases": len(exception_cases),
        "pending_cases": len(pending_cases),
        "escalated_cases": len(escalated_cases),

        # Real, API-confirmed figures - decided by an actual Razorpay
        # webhook, poll, or retry-attempt API call. This is the number
        # that survives "did this actually happen?"
        "revenue_recovered_real": recovered_amount_real,
        "recovery_rate_percent_real": (
            round(recovery_rate_real, 2) if recovery_rate_real is not None else None
        ),

        # Projected/simulated figures - decided by SIMULATED_SUCCESS_RATE,
        # a probability draw, not a live API result. Report this as a
        # projection, never as "recovered" on its own.
        "projected_revenue_recovered_simulated": recovered_amount_projected,
        "projected_recovery_rate_percent_simulated": (
            round(recovery_rate_projected, 2) if recovery_rate_projected is not None else None
        ),

        # Blended figures kept for convenience/continuity - explicitly
        # labeled as blended so they're never mistaken for either pure
        # number above.
        "recovered_amount_blended": recovered_amount,
        "recovery_rate_percent_blended": round(recovery_rate, 2),
        "blended_note": (
            "Combines real, API-confirmed outcomes with simulated/projected "
            "outcomes. See revenue_recovered_real and "
            "projected_revenue_recovered_simulated for the two figures kept "
            "separate; do not cite this blended number as 'revenue recovered' "
            "on its own."
        ),

        "recovery_rate_note": (
            "Recovery rates are computed over resolved cases only - excludes "
            "pending cases still awaiting webhook or poll confirmation."
        ) if pending_cases else None,
        "total_amount": total_amount,
        "unrecovered_amount": total_amount - recovered_amount - pending_amount,
        "pending_amount": pending_amount,
        "cases_by_cause": cases_by_cause,
        "exceptions_by_cause": exceptions_by_cause,
        "decisions_by_source": decisions_by_source,

        # Promise-to-pay - reported alongside, not blended into,
        # recovered/exception - a kept promise is a future recovery
        # signal, not a completed one at report time.
        "promises_kept": promises_kept,
        "promises_broken": promises_broken,
        "promise_keep_rate_pct": (
            round(100 * promises_kept / len(promise_cases), 2) if promise_cases else None
        ),

        "exception_list": [
            {
                "payment_id": r.get("payment_id"),
                "amount": r.get("amount"),
                "cause": r.get("cause", "UNKNOWN"),
                "source": r.get("source", "rule"),
                "action_type": r.get("action_type"),
                "policy_approved": r.get("policy_approved", True),
                "resolution_method": r.get("resolution_method"),
                "is_real": _is_real(r),
                "promised_payment_date": r.get("promised_payment_date"),
                "promise_kept": r.get("promise_kept"),
                "reasoning": r.get("reasoning", ""),
            }
            for r in exception_cases
        ],
        "pending_list": [
            {
                "payment_id": r.get("payment_id"),
                "order_id": r.get("order_id"),
                "amount": r.get("amount"),
                "cause": r.get("cause", "UNKNOWN"),
                "action_type": r.get("action_type"),
                "initiated_at": r.get("timestamp"),
            }
            for r in pending_cases
        ],
    }

    _print_summary(summary)
    _write_summary(summary)
    return summary


def summarize_full_audit_log(path: str = "logs/audit_log.json") -> dict:
    """
    Load the FULL accumulated audit log (every run, ever appended by
    execute.py) and summarize it exactly like summarize() does for a
    single batch's results.

    Why this exists: run_pipeline() in app.py only ever passes THIS
    run's newly-processed `results` to summarize() - which is often an
    empty list, since diagnose.load_batch() skips any payment_id
    already present in the audit log. That means the terminal report
    can print all-zeros even when logs/audit_log.json (and therefore
    the RecoverX dashboard, which reads that same file directly) has
    dozens of accumulated records. This function reads the same file
    the dashboard reads, so the two are guaranteed to agree.
    """
    try:
        with open(path) as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    return summarize(log)


def _print_summary(summary: dict) -> None:
    print("\n=== Revenue Recovery Report ===")
    print(f"Total failed payments processed: {summary['total_cases']}")
    print(f"Exceptions (unresolved): {summary['exception_cases']}")
    if summary["pending_cases"]:
        print(f"Pending (awaiting webhook/poll confirmation): {summary['pending_cases']} "
              f"(₹{summary['pending_amount']:,})")
    print()

    if summary["real_resolved_cases"]:
        rate = summary["recovery_rate_percent_real"]
        print(f"Revenue Recovered (real, API-confirmed): ₹{summary['revenue_recovered_real']:,} "
              f"({rate}% of {summary['real_resolved_cases']} real-resolved cases)")
    else:
        print("Revenue Recovered (real, API-confirmed): ₹0 (no real-resolved cases yet)")

    if summary["simulated_resolved_cases"]:
        rate = summary["projected_recovery_rate_percent_simulated"]
        print(f"Projected Revenue Recovered (simulated outcomes): "
              f"₹{summary['projected_revenue_recovered_simulated']:,} "
              f"({rate}% of {summary['simulated_resolved_cases']} simulated cases)")

    if summary["promise_keep_rate_pct"] is not None:
        print(f"\nPromise-to-pay: {summary['promises_kept']} kept, "
              f"{summary['promises_broken']} broken "
              f"({summary['promise_keep_rate_pct']}% keep rate)")

    print(f"\nAmount still at risk: ₹{summary['unrecovered_amount']:,}")
    print("\nExceptions by cause:")
    for cause, count in summary["exceptions_by_cause"].items():
        print(f"  {cause}: {count}")
    print("================================\n")


def _write_summary(summary: dict, path: str = "logs/summary_report.json") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)