"""
Polling fallback for pending retries.

Webhooks are the primary way a pending retry gets resolved
(agent/webhook_server.py), but Razorpay's own docs are explicit that
webhook delivery isn't guaranteed - networks drop packets, endpoints
have brief outages, events can be missed. This script is the
belt-and-suspenders half of that: it polls Razorpay directly for any
order still sitting in the pending store, so a missed webhook doesn't
mean a case sits pending forever.

Uses pending_store.pop_pending() (atomic check-and-remove) rather than
a separate get/remove pair, so this poll loop and a concurrent webhook
delivery for the same order_id can't both resolve the same case - see
pending_store.pop_pending's docstring for the exact race this closes.

Two things this script does that a naive "poll until paid" loop
wouldn't:

  1. Distinguishes "still waiting" from "definitely resolved" - an
     order can sit in 'created' or 'attempted' for a long time
     legitimately (the customer hasn't finished paying yet), so only
     a 'paid' status resolves a case here; anything else is left
     pending unless it has also timed out (see 2).

  2. Enforces a timeout. A pending case that's been sitting
     unconfirmed for longer than PENDING_TIMEOUT_MINUTES is resolved
     as an unconfirmed escalation rather than left pending
     indefinitely - this keeps report.py's pending_cases count
     meaningful (a growing number of genuinely stuck cases) instead of
     silently accumulating cases that will just never resolve.

Run: python agent/reconcile_pending.py
Intended to run on a schedule (cron, systemd timer, etc.), not
continuously - each run does one pass over the current pending store
and exits.
"""

import sys
from datetime import datetime, timedelta, timezone

from agent import execute, pending_store, razorpay_client

PENDING_TIMEOUT_MINUTES = 60


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def reconcile_once(now: datetime | None = None) -> dict:
    """One pass over every currently pending record. Returns a summary
    of what happened, for logging/printing by the caller.

    Each record is reconciled independently inside its own try/except -
    a single malformed pending entry (bad timestamp, missing field, a
    Razorpay API hiccup) is counted as an error and skipped, rather
    than crashing the whole pass and leaving every other pending case
    unchecked until the next scheduled run.
    """
    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "resolved_paid": 0, "resolved_timeout": 0, "still_pending": 0, "errors": 0}

    for pending in pending_store.list_pending():
        summary["checked"] += 1
        order_id = pending.get("order_id")
        try:
            status = razorpay_client.check_order_status(order_id)

            if status == "paid":
                claimed = pending_store.pop_pending(order_id)
                if claimed is None:
                    # Already resolved by a webhook between
                    # list_pending() (above) and this pop - not a bug,
                    # just lost the race to the webhook, which is the
                    # correct outcome (no double-resolution).
                    continue
                _resolve(claimed, recovered=True, method="poll", live_status=status, now=now)
                summary["resolved_paid"] += 1
                continue

            initiated_at = _parse_iso(pending["initiated_at"])
            age = now - initiated_at
            if age > timedelta(minutes=PENDING_TIMEOUT_MINUTES):
                claimed = pending_store.pop_pending(order_id)
                if claimed is None:
                    continue
                _resolve(
                    claimed,
                    recovered=False,
                    method="timeout",
                    live_status=status or "unconfirmed_timeout",
                    now=now,
                )
                summary["resolved_timeout"] += 1
            else:
                summary["still_pending"] += 1
        except Exception as e:
            print(f"[reconcile_pending] Failed to reconcile order {order_id}: {e}")
            summary["errors"] += 1

    return summary


def _resolve(pending: dict, recovered: bool, method: str, live_status, now: datetime) -> None:
    """Append the resolution to the audit log. Does NOT call
    remove_pending - the caller already removed the entry via
    pop_pending() before calling this, as part of the same atomic
    check-and-claim."""
    outcome = {
        "status": "resolved",
        "resolution_method": method,
        "payment_id": pending["original_payment_id"],
        "order_id": pending["order_id"],
        "retry_payment_id": None,
        "amount": pending["amount"],
        "cause": pending["cause"],
        "source": pending.get("source", "rule"),
        "action_type": pending["action_type"],
        "max_attempts": pending["max_attempts"],
        "attempts_used": 1,
        "attempts": [{"attempt": 1, "succeeded": recovered}],
        "policy_approved": pending.get("policy_approved", True),
        "policy_note": pending.get("policy_note", ""),
        "reasoning": pending["reasoning"] + (
            "" if method == "poll" else
            f" [unconfirmed after {PENDING_TIMEOUT_MINUTES} min - no webhook or paid "
            "status received; escalated rather than left pending indefinitely]"
        ),
        "recovered": recovered,
        "verified_live": method == "poll",
        "live_status": live_status,
        "initiated_at": pending.get("initiated_at"),
        "timestamp": now.isoformat(),
    }
    execute.append_to_audit_log(outcome)


def main():
    summary = reconcile_once()
    print(
        f"[reconcile_pending] checked={summary['checked']} "
        f"resolved_paid={summary['resolved_paid']} "
        f"resolved_timeout={summary['resolved_timeout']} "
        f"still_pending={summary['still_pending']} "
        f"errors={summary['errors']}"
    )


if __name__ == "__main__":
    sys.exit(main())