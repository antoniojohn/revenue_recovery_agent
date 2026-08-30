"""
Execution layer: runs the bounded action chosen by decide.py, and writes
a full audit trail entry (decision + reasoning + timestamp + outcome).

For the buildathon submission this simulates retries/notifications rather
than calling live payment-retry endpoints, since we're working with
synthetic/test-mode data — but the audit log format is what a real
integration would use.
"""

import json
import random
from datetime import datetime, timezone

AUDIT_LOG_PATH = "logs/audit_log.json"

# Simulated success probability per action type, used only to generate a
# plausible outcome for the demo batch. A real integration would replace
# this with an actual Razorpay retry / notification API call.
SIMULATED_SUCCESS_RATE = {
    "RETRY_IMMEDIATE": 0.55,
    "RETRY_AFTER_DELAY": 0.65,
    "NOTIFY_USER": 0.30,
    "NOTIFY_USER_CARD_UPDATE": 0.25,
    "ESCALATE": 0.0,
}


def run_action(action: dict, record: dict) -> dict:
    """Execute the given action against a failed payment record.

    Returns an outcome dict that gets appended to the audit log and later
    consumed by report.py.
    """
    success_rate = SIMULATED_SUCCESS_RATE.get(action["type"], 0.0)
    recovered = random.random() < success_rate if action["max_attempts"] > 0 else False

    outcome = {
        "payment_id": record.get("payment_id"),
        "amount": record.get("amount"),
        "cause": action["cause"],
        "action_type": action["type"],
        "max_attempts": action["max_attempts"],
        "reasoning": action["reasoning"],
        "recovered": recovered,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _append_to_audit_log(outcome)
    return outcome


def _append_to_audit_log(outcome: dict) -> None:
    try:
        with open(AUDIT_LOG_PATH, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []

    log.append(outcome)

    with open(AUDIT_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
