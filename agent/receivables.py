"""
B2B Receivables Chaser — same bounded-action, audit-trail pattern as
the payment recovery pipeline (diagnose -> decide -> execute -> report),
applied to a structurally different problem: overdue invoices instead
of failed payments. This exists to demonstrate the core architecture
generalizes, not just that it works for one specific data shape.

Deliberately kept as its own module with its own audit log and summary
report (logs/receivables_audit_log.json, logs/receivables_summary.json)
rather than writing into the payment pipeline's logs/audit_log.json -
these are two different business processes, and mixing them would make
both audit trails harder to reason about, not easier.

Classification here is fully rule-based (days_overdue is a number, not
free text) - there is no LLM fallback need or ambiguity to resolve, so
this pipeline has no diagnose-equivalent step at all.

Run: python -m agent.receivables
"""

import json
import os
import random
from datetime import datetime, timezone

from filelock import FileLock

AUDIT_LOG_PATH = "logs/receivables_audit_log.json"
SUMMARY_PATH = "logs/receivables_summary.json"

# Cause buckets: purely a function of days_overdue, not a free-text
# classification problem - always deterministic, never ambiguous.
ACTION_MAP = {
    "DAYS_OVERDUE_0_7": "GENTLE_REMINDER",
    "DAYS_OVERDUE_8_30": "FIRM_REMINDER",
    "DAYS_OVERDUE_30_PLUS": "ESCALATE_TO_COLLECTIONS",
}

# Simulated response probability per action, same pattern as
# execute.SIMULATED_SUCCESS_RATE - these are demo/synthetic invoices,
# there is no real collections API to call.
SIMULATED_RESPONSE_RATE = {
    "GENTLE_REMINDER": 0.55,
    "FIRM_REMINDER": 0.40,
    "ESCALATE_TO_COLLECTIONS": 0.20,
}

# Policy gate: escalating a tiny invoice to collections costs more in
# staff time/relationship damage than the invoice is worth - same
# "bounded, explainable" philosophy as the payment pipeline's
# MIN_RETRY_AMOUNT gate, applied here to the collections threshold
# instead of the retry threshold.
MIN_COLLECTIONS_AMOUNT = 10000


def classify_invoice(record: dict) -> str:
    """Return the cause bucket for an overdue invoice - a pure function
    of days_overdue, never ambiguous, never needs an LLM."""
    days = record.get("days_overdue", 0)
    if days <= 7:
        return "DAYS_OVERDUE_0_7"
    if days <= 30:
        return "DAYS_OVERDUE_8_30"
    return "DAYS_OVERDUE_30_PLUS"


def decide_action(cause: str, record: dict) -> dict:
    """Same shape as decide.choose_action(): a bounded action dict with
    an explicit policy gate and human-readable reasoning."""
    action_type = ACTION_MAP.get(cause, "ESCALATE_TO_COLLECTIONS")
    amount = record.get("amount", 0)

    policy_approved = True
    policy_note = "within bounds"

    if action_type == "ESCALATE_TO_COLLECTIONS" and amount < MIN_COLLECTIONS_AMOUNT:
        # Downgrade rather than silently drop - same principle as the
        # payment pipeline's policy gate.
        action_type = "FIRM_REMINDER"
        policy_approved = False
        policy_note = (
            f"collections escalation rejected - invoice below "
            f"₹{MIN_COLLECTIONS_AMOUNT} minimum collections threshold"
        )

    return {
        "type": action_type,
        "cause": cause,
        "policy_approved": policy_approved,
        "policy_note": policy_note,
        "reasoning": (
            f"Invoice classified as {cause}; policy {policy_note}."
        ),
    }


def execute_action(action: dict, record: dict) -> dict:
    """Simulated outcome + audit trail entry, same pattern as
    execute._run_simulated() - a single bounded attempt, not a loop,
    since a reminder/escalation is a one-shot action, not a retry."""
    success_rate = SIMULATED_RESPONSE_RATE.get(action["type"], 0.0)
    responded = random.random() < success_rate

    outcome = {
        "invoice_id": record.get("invoice_id"),
        "amount": record.get("amount"),
        "cause": action["cause"],
        "action_type": action["type"],
        "policy_approved": action["policy_approved"],
        "policy_note": action["policy_note"],
        "reasoning": action["reasoning"],
        "responded": responded,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _append_to_audit_log(outcome)
    return outcome


def _append_to_audit_log(outcome: dict) -> None:
    """Wrapped in a file lock so concurrent callers cannot race on the
    read-modify-write and silently drop each other's writes. See
    agent/execute.py's append_to_audit_log for the same fix and the
    concurrency test that proved this pattern was unsafe without it."""
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH) or ".", exist_ok=True)
    lock_path = AUDIT_LOG_PATH + ".lock"

    with FileLock(lock_path, timeout=10):
        try:
            with open(AUDIT_LOG_PATH, "r") as f:
                log = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log = []
        log.append(outcome)
        with open(AUDIT_LOG_PATH, "w") as f:
            json.dump(log, f, indent=2)


def summarize(results: list[dict]) -> dict:
    """Same reporting shape as report.summarize() - responded/
    unresponded split, ₹ amounts, breakdown by cause - applied to
    invoices instead of payments."""
    total = len(results)
    responded = [r for r in results if r["responded"]]
    unresponded = [r for r in results if not r["responded"]]

    total_amount = sum(r["amount"] for r in results)
    responded_amount = sum(r["amount"] for r in responded)

    by_cause: dict[str, int] = {}
    for r in results:
        by_cause[r["cause"]] = by_cause.get(r["cause"], 0) + 1

    escalated = [r for r in results if r["action_type"] == "ESCALATE_TO_COLLECTIONS"]

    summary = {
        "total_invoices": total,
        "responded_count": len(responded),
        "unresponded_count": len(unresponded),
        "response_rate_pct": round(100 * len(responded) / total, 2) if total else 0.0,
        "total_amount": total_amount,
        "responded_amount": responded_amount,
        "unresponded_amount": total_amount - responded_amount,
        "escalated_to_collections_count": len(escalated),
        "invoices_by_cause": by_cause,
    }
    _print_summary(summary)
    _write_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    print("\n=== Receivables Chaser Report ===")
    print(f"Total overdue invoices processed: {summary['total_invoices']}")
    print(f"Responded: {summary['responded_count']} ({summary['response_rate_pct']}%)")
    print(f"Unresponded: {summary['unresponded_count']}")
    print(f"₹ Responded amount: ₹{summary['responded_amount']:,}")
    print(f"₹ Still outstanding: ₹{summary['unresponded_amount']:,}")
    print(f"Escalated to collections: {summary['escalated_to_collections_count']}")
    print("Invoices by cause:")
    for cause, count in summary["invoices_by_cause"].items():
        print(f"  {cause}: {count}")
    print("==================================\n")


def _write_summary(summary: dict) -> None:
    os.makedirs(os.path.dirname(SUMMARY_PATH) or ".", exist_ok=True)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)


def run_pipeline(batch_path: str = "data/overdue_invoices.json"):
    with open(batch_path) as f:
        invoices = json.load(f)

    results = []
    for record in invoices:
        cause = classify_invoice(record)
        action = decide_action(cause, record)
        outcome = execute_action(action, record)
        results.append(outcome)

    summarize(results)


if __name__ == "__main__":
    run_pipeline()