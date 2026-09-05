"""
Execution layer: runs the bounded action chosen by decide.py, and writes
a full audit trail entry (decision + reasoning + timestamp + outcome).

Two execution paths, matching what's actually real vs. simulated:

  - Synthetic/demo records (no live Razorpay order to wait on): outcome
    is decided synchronously from SIMULATED_SUCCESS_RATE, same as
    before. There is nothing to wait for, so there is nothing to make
    async. For NOTIFY_*/ESCALATE actions specifically, a "promise to
    pay" is also simulated (see PROMISE_ACTION_TYPES below) - this
    models the real-world pattern where a human contact results in a
    customer promising a payment date, not an automated retry.

  - Real records from the Razorpay test account: a retry now means
    *initiating* a new order via attempt_retry() and returning a
    "pending" outcome immediately - NOT deciding success/failure from
    the order-creation response, which only tells you the order exists,
    not whether the customer paid it. Confirmation arrives later, out
    of band, via agent/webhook_server.py (primary path) or
    agent/reconcile_pending.py's polling fallback (for missed webhooks
    or a timeout). See agent/pending_store.py for where those pending
    records live between initiation and resolution.

This means `recovered` in a real-record outcome may not be knowable at
the moment run_action() returns - report.py must treat `status: pending`
as a third bucket, not fold it into recovered or exception.
"""

import json
import os
import random
random.seed(42)
from datetime import datetime, timedelta, timezone

from filelock import FileLock

from agent import pending_store

AUDIT_LOG_PATH = "logs/audit_log.json"

# Simulated success probability per action type - used only for
# synthetic/demo records, which have no live order to wait on. A real
# integration path (razorpay_live_test_account records) never reads
# this; see run_action().
SIMULATED_SUCCESS_RATE = {
    "RETRY_IMMEDIATE": 0.55,
    "RETRY_AFTER_DELAY": 0.65,
    "NOTIFY_USER": 0.30,
    "NOTIFY_USER_CARD_UPDATE": 0.25,
    "ESCALATE": 0.0,
}

# Promise-to-pay tracking: for action types where a human/customer
# contact happens instead of an automated retry, model the real-world
# pattern of a customer promising a future payment date. A promise is
# only recorded for these action types - RETRY_* actions are automated
# attempts, not a customer commitment, so they never get a promise.
PROMISE_ACTION_TYPES = {"NOTIFY_USER", "NOTIFY_USER_CARD_UPDATE", "ESCALATE"}
PROMISE_KEPT_RATE = {
    "NOTIFY_USER": 0.45,
    "NOTIFY_USER_CARD_UPDATE": 0.55,
    "ESCALATE": 0.30,
}
PROMISE_WINDOW_DAYS = 7


def run_action(action: dict, record: dict) -> dict:
    """Execute the given action against a failed payment record.

    Returns an outcome dict with a `status` field:
      - "resolved": recovered is a real True/False, safe to count in
        report.py's recovered/exception totals immediately.
      - "pending": a real retry was initiated but its outcome is not
        yet known; recovered is None. report.py must exclude these
        from recovered/exception counts until a later resolution
        (webhook or poll) replaces this entry in the audit log.

    Every outcome, resolved or pending, is appended to the audit log so
    the full lifecycle of a case is traceable from one place.
    """
    is_live_record = record.get("source") == "razorpay_live_test_account"

    if is_live_record and action["type"].startswith("RETRY"):
        outcome = _initiate_live_retry(action, record)
    else:
        outcome = _run_simulated(action, record)

    append_to_audit_log(outcome)
    return outcome


def _run_simulated(action: dict, record: dict) -> dict:
    """Synchronous simulated outcome for synthetic/demo records, or for
    real records whose action isn't a retry (NOTIFY_*/ESCALATE never
    had a real endpoint to call in the first place - see README)."""
    success_rate = SIMULATED_SUCCESS_RATE.get(action["type"], 0.0)
    max_attempts = action["max_attempts"]

    attempts = []
    recovered = False
    for attempt_num in range(1, max_attempts + 1):
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

    # Promise-to-pay: only for actions where a human/customer contact
    # happens instead of an automated retry attempt.
    promised_payment_date = None
    promise_kept = None
    if action["type"] in PROMISE_ACTION_TYPES:
        promised_payment_date = (
            datetime.now(timezone.utc) + timedelta(days=PROMISE_WINDOW_DAYS)
        ).date().isoformat()
        promise_kept = random.random() < PROMISE_KEPT_RATE.get(action["type"], 0.0)

    return {
        "status": "resolved",
        "resolution_method": "simulated",
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
        "promised_payment_date": promised_payment_date,
        "promise_kept": promise_kept,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _initiate_live_retry(action: dict, record: dict) -> dict:
    """Create a real retry order for a live record and return a pending
    outcome. Does NOT decide recovered from the order-creation response
    - order creation only proves the order exists, not that it was
    paid. That confirmation comes later via webhook or polling."""
    from agent import razorpay_client

    order = razorpay_client.attempt_retry(record)
    now = datetime.now(timezone.utc).isoformat()

    if order is None or not order.get("id"):
        # Retry initiation itself failed (no credentials, API error,
        # etc.) - this is a resolved failure, not a pending case, since
        # there is no order for a webhook to ever confirm.
        return {
            "status": "resolved",
            "resolution_method": "retry_initiation_failed",
            "payment_id": record.get("payment_id"),
            "amount": record.get("amount"),
            "cause": action["cause"],
            "source": action.get("source", "rule"),
            "action_type": action["type"],
            "max_attempts": action["max_attempts"],
            "attempts_used": 1,
            "attempts": [{"attempt": 1, "succeeded": False}],
            "policy_approved": action.get("policy_approved", True),
            "policy_note": action.get("policy_note", ""),
            "reasoning": action["reasoning"],
            "recovered": False,
            "verified_live": False,
            "live_status": None,
            "promised_payment_date": None,
            "promise_kept": None,
            "timestamp": now,
        }

    pending_store.add_pending({
        "order_id": order["id"],
        "original_payment_id": record.get("payment_id"),
        "amount": record.get("amount"),
        "cause": action["cause"],
        "source": action.get("source", "rule"),
        "action_type": action["type"],
        "max_attempts": action["max_attempts"],
        "policy_approved": action.get("policy_approved", True),
        "policy_note": action.get("policy_note", ""),
        "reasoning": action["reasoning"],
        "initiated_at": now,
    })

    return {
        "status": "pending",
        "resolution_method": None,
        "payment_id": record.get("payment_id"),
        "order_id": order["id"],
        "amount": record.get("amount"),
        "cause": action["cause"],
        "source": action.get("source", "rule"),
        "action_type": action["type"],
        "max_attempts": action["max_attempts"],
        "attempts_used": 1,
        "attempts": [{"attempt": 1, "succeeded": None}],
        "policy_approved": action.get("policy_approved", True),
        "policy_note": action.get("policy_note", ""),
        "reasoning": action["reasoning"],
        "recovered": None,
        "verified_live": False,
        "live_status": "order_created_awaiting_confirmation",
        "promised_payment_date": None,
        "promise_kept": None,
        "timestamp": now,
    }


def append_to_audit_log(outcome: dict) -> None:
    """Public so webhook_server.py and reconcile_pending.py can append
    resolution outcomes to the same log, rather than duplicating the
    read-modify-write logic in two more places.

    Wrapped in a file lock so concurrent callers (a webhook arriving at
    the same moment reconcile_pending.py's poll loop is running, or two
    pipeline runs overlapping) cannot race on the read-modify-write and
    silently drop each other's writes. Without this lock, two processes
    reading the same "before" state and writing back independently
    would result in only the last writer's entry surviving - a real,
    confirmed failure mode (see test_concurrency.py), not a theoretical
    one. The lock file lives next to the log itself.
    """
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