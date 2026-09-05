"""
Checkout Drop-off Recovery - same bounded-action, audit-trail pattern
as the payment recovery pipeline (diagnose -> decide -> execute ->
report) and the receivables chaser, applied here to abandoned checkouts.

Classification is time-based (time_since_abandonment_hours), not free
text, so like receivables.py there is no LLM fallback step needed.

Run: python -m agent.checkout_recovery
"""

import json
import os
import random
from datetime import datetime, timezone

from filelock import FileLock

AUDIT_LOG_PATH = "logs/checkout_audit_log.json"
SUMMARY_PATH = "logs/checkout_summary.json"

ACTION_MAP = {
    "UNDER_1HR": "EMAIL_NUDGE",
    "1_TO_24HR": "DISCOUNT_OFFER",
    "OVER_24HR": "ESCALATE_OR_DROP",
}

SIMULATED_RESPONSE_RATE = {
    "EMAIL_NUDGE": 0.35,
    "DISCOUNT_OFFER": 0.50,
    "ESCALATE_OR_DROP": 0.10,
}

MIN_DISCOUNT_AMOUNT = 1000


def classify_checkout(record: dict) -> str:
    hours = record.get("time_since_abandonment_hours", 0)
    if hours < 1:
        return "UNDER_1HR"
    if hours <= 24:
        return "1_TO_24HR"
    return "OVER_24HR"


def decide_action(cause: str, record: dict) -> dict:
    action_type = ACTION_MAP.get(cause, "EMAIL_NUDGE")
    amount = record.get("amount", 0)

    policy_approved = True
    policy_note = "within bounds"

    if action_type == "DISCOUNT_OFFER" and amount < MIN_DISCOUNT_AMOUNT:
        action_type = "EMAIL_NUDGE"
        policy_approved = False
        policy_note = (
            f"discount offer rejected - cart below "
            f"Rs.{MIN_DISCOUNT_AMOUNT} minimum discount threshold"
        )

    return {
        "type": action_type,
        "cause": cause,
        "policy_approved": policy_approved,
        "policy_note": policy_note,
        "reasoning": (
            f"Checkout classified as {cause}; policy {policy_note}."
        ),
    }


def execute_action(action: dict, record: dict) -> dict:
    success_rate = SIMULATED_RESPONSE_RATE.get(action["type"], 0.0)
    responded = random.random() < success_rate

    outcome = {
        "checkout_id": record.get("checkout_id"),
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
    total = len(results)
    responded = [r for r in results if r["responded"]]
    unresponded = [r for r in results if not r["responded"]]

    total_amount = sum(r["amount"] for r in results)
    responded_amount = sum(r["amount"] for r in responded)

    by_cause: dict[str, int] = {}
    for r in results:
        by_cause[r["cause"]] = by_cause.get(r["cause"], 0) + 1

        discounted = [r for r in results if r["action_type"] == "DISCOUNT_OFFER"]
    escalated = [r for r in results if r["action_type"] == "ESCALATE_OR_DROP"]

    summary = {
        "total_checkouts": total,
        "responded_count": len(responded),
        "unresponded_count": len(unresponded),
        "response_rate_pct": round(100 * len(responded) / total, 2) if total else 0.0,
        "total_amount": total_amount,
        "responded_amount": responded_amount,
        "unresponded_amount": total_amount - responded_amount,
        "discount_offer_count": len(discounted),
        "escalated_or_dropped_count": len(escalated),
        "checkouts_by_cause": by_cause,
    }
    print(f"Discount offers made: {summary['discount_offer_count']}")
    print(f"Escalated or dropped (cold carts): {summary['escalated_or_dropped_count']}")
    _print_summary(summary)
    _write_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    print("\n=== Checkout Drop-off Recovery Report ===")
    print(f"Total abandoned checkouts processed: {summary['total_checkouts']}")
    print(f"Responded: {summary['responded_count']} ({summary['response_rate_pct']}%)")
    print(f"Unresponded: {summary['unresponded_count']}")
    print(f"Recovered amount: Rs.{summary['responded_amount']:,}")
    print(f"Still abandoned: Rs.{summary['unresponded_amount']:,}")
    print(f"Discount offers made: {summary['discount_offer_count']}")
    print("Checkouts by cause:")
    for cause, count in summary["checkouts_by_cause"].items():
        print(f"  {cause}: {count}")
    print("==========================================\n")


def _write_summary(summary: dict) -> None:
    os.makedirs(os.path.dirname(SUMMARY_PATH) or ".", exist_ok=True)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)


def run_pipeline(batch_path: str = "data/abandoned_checkouts.json"):
    with open(batch_path) as f:
        checkouts = json.load(f)

    results = []
    for record in checkouts:
        cause = classify_checkout(record)
        action = decide_action(cause, record)
        outcome = execute_action(action, record)
        results.append(outcome)

    summarize(results)


if __name__ == "__main__":
    run_pipeline()