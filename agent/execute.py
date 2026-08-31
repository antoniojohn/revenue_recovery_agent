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

    Bounded attempt loop: makes at most `max_attempts` simulated retry
    attempts, stopping as soon as one succeeds (or after the cap is hit -
    this is the real "STOP" the architecture diagram describes, not just
    a single probability draw). For records that came from the real
    Razorpay test account, also makes a genuine API call to check the
    payment's live status, so the audit trail records real verification
    alongside the simulated retry outcome rather than pure simulation.

    Returns an outcome dict that gets appended to the audit log and later
    consumed by report.py.
    """
    success_rate = SIMULATED_SUCCESS_RATE.get(action["type"], 0.0)
    max_attempts = action["max_attempts"]

    attempts = []
    recovered = False
    for attempt_num in range(1, max_attempts + 1):
        if record.get("source") == "razorpay_live_test_account":
            from agent import razorpay_client
            result = razorpay_client.attempt_retry(record)
            success = result is not None and result.get("status") == "created"
        else:
            success = random.random() < success_rate
        attempts.append({"attempt": attempt_num, "succeeded": success})
        if success:
            recovered = True
            break  # bounded loop stops the moment it succeeds

    live_status = None
    verified_live = False
    if record.get("source") == "razorpay_live_test_account":
        from agent import razorpay_client
        live_status = razorpay_client.check_payment_status(record.get("payment_id"))
        verified_live = live_status is not None

    outcome = {
        "payment_id": record.get("payment_id"),
        "amount": record.get("amount"),
        "cause": action["cause"],
        "source": action.get("source", "rule"),
        "action_type": action["type"],
        "max_attempts": max_attempts,
        "attempts_used": len(attempts),
        "attempts": attempts,
        "policy_approved": action.get("policy_approved", True),
        "policy_note": action.get("policy_note", ""),
        "reasoning": action["reasoning"],
        "recovered": recovered,
        "verified_live": verified_live,
        "live_status": live_status,
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
