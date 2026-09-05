"""
Razorpay webhook receiver.

Receives, verifies, and logs Razorpay webhook events, and resolves any
matching pending retry (see agent/pending_store.py) via an atomic
check-and-claim (pop_pending) so a webhook and a concurrent poll cycle
in agent/reconcile_pending.py can't both resolve the same case.

Security note (non-negotiable): every request is verified against
RAZORPAY_WEBHOOK_SECRET before its payload is trusted at all. This is
a separate secret from RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET - it's
generated when you configure a webhook in the Razorpay dashboard, not
your API credentials. An endpoint that skips this check would let
anyone who finds the URL POST a fake "payment succeeded" event.

Run: python agent/webhook_server.py
Requires a public URL for Razorpay to call back to in dev (e.g. an
ngrok tunnel pointed at this process) - this file does not set that up.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from agent import execute, pending_store

load_dotenv()

app = Flask(__name__)

WEBHOOK_EVENTS_LOG = "logs/webhook_events.json"

# Events relevant to confirming a retry's outcome, and what each implies
# about the underlying payment.
OUTCOME_EVENTS = {
    "payment.captured": True,
    "order.paid": True,
    "payment.failed": False,
}
HANDLED_EVENTS = set(OUTCOME_EVENTS)


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify X-Razorpay-Signature per Razorpay's documented scheme:
    hex HMAC-SHA256 of the raw request body, keyed with the webhook
    secret. Must be computed over the raw bytes exactly as received -
    parsing to JSON and re-serializing before verifying would produce a
    different signature and silently break this for payloads with
    different key ordering or whitespace. hmac.compare_digest is used
    instead of `==` to avoid leaking timing information about how many
    leading bytes matched.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_entity(payload: dict) -> dict:
    """Pull the payment or order sub-object out of Razorpay's webhook
    envelope. Shape is payload.payload.payment.entity for payment
    events, payload.payload.order.entity for order events - see
    Razorpay's webhook payload docs for the full schema per event type.
    """
    inner = payload.get("payload", {})

    if "payment" in inner:
        p = inner["payment"].get("entity", {})
        return {
            "payment_id": p.get("id"),
            "order_id": p.get("order_id"),
            "status": p.get("status"),
            "amount": round((p.get("amount") or 0) / 100, 2),  # paise -> rupees, no precision loss
        }

    if "order" in inner:
        o = inner["order"].get("entity", {})
        return {
            "payment_id": None,
            "order_id": o.get("id"),
            "status": o.get("status"),
            "amount": round((o.get("amount") or 0) / 100, 2),  # paise -> rupees, no precision loss
        }

    return {}


def _append_event_log(entry: dict) -> None:
    os.makedirs(os.path.dirname(WEBHOOK_EVENTS_LOG) or ".", exist_ok=True)
    try:
        with open(WEBHOOK_EVENTS_LOG, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    log.append(entry)
    with open(WEBHOOK_EVENTS_LOG, "w") as f:
        json.dump(log, f, indent=2)


@app.route("/webhooks/razorpay", methods=["POST"])
def razorpay_webhook():
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    signature = request.headers.get("X-Razorpay-Signature", "")
    raw_body = request.get_data()  # raw bytes - required for correct HMAC verification

    if not secret:
        # Fail closed: never accept a webhook we have no way to verify,
        # even in local dev. A missing secret is a config error, not a
        # reason to trust the request anyway.
        app.logger.error("RAZORPAY_WEBHOOK_SECRET not configured - rejecting webhook.")
        return jsonify({"error": "webhook secret not configured"}), 500

    if not _verify_signature(raw_body, signature, secret):
        app.logger.warning("Rejected webhook: invalid or missing signature.")
        return jsonify({"error": "invalid signature"}), 400

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid JSON body"}), 400

    event = payload.get("event", "unknown")
    entity = _extract_entity(payload)
    order_id = entity.get("order_id")

    resolution = None
    if event in HANDLED_EVENTS and order_id:
        resolution = _resolve_pending(order_id, event, entity)

    _append_event_log({
        "event": event,
        "payment_id": entity.get("payment_id"),
        "order_id": order_id,
        "status": entity.get("status"),
        "amount": entity.get("amount"),
        "handled": event in HANDLED_EVENTS,
        "resolved_pending": resolution is not None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    })

    # Ack with 2xx as soon as the event is verified and (if applicable)
    # reconciled - Razorpay retries webhooks on non-2xx, so we don't
    # want our own processing time to look like a delivery failure.
    return jsonify({"status": "received"}), 200


def _resolve_pending(order_id: str, event: str, entity: dict) -> dict | None:
    """If this order_id matches a pending retry, resolve it: append a
    completed outcome to the audit log. Returns the resolved outcome,
    or None if no pending record matched (e.g. the event is for an
    order we didn't initiate, or it was already resolved by a previous
    webhook or a poll).

    Uses pending_store.pop_pending() - an atomic check-and-remove - not
    a separate get_pending() + remove_pending(). This closes the race
    where a webhook and reconcile_pending's poll loop both see the same
    order as pending at nearly the same moment and both try to resolve
    it: whichever caller's pop_pending() runs first gets the entry (and
    removes it), so the other caller's pop_pending() on the same
    order_id correctly returns None and resolves nothing."""
    pending = pending_store.pop_pending(order_id)
    if pending is None:
        return None

    recovered = OUTCOME_EVENTS.get(event, False)
    now = datetime.now(timezone.utc).isoformat()

    outcome = {
        "status": "resolved",
        "resolution_method": "webhook",
        "payment_id": pending["original_payment_id"],
        "order_id": order_id,
        "retry_payment_id": entity.get("payment_id"),
        "amount": pending["amount"],
        "cause": pending["cause"],
        "source": pending.get("source", "rule"),
        "action_type": pending["action_type"],
        "max_attempts": pending["max_attempts"],
        "attempts_used": 1,
        "attempts": [{"attempt": 1, "succeeded": recovered}],
        "policy_approved": pending.get("policy_approved", True),
        "policy_note": pending.get("policy_note", ""),
        "reasoning": pending["reasoning"],
        "recovered": recovered,
        "verified_live": True,
        "live_status": entity.get("status"),
        "initiated_at": pending.get("initiated_at"),
        "timestamp": now,
    }

    execute.append_to_audit_log(outcome)
    return outcome


@app.route("/webhooks/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    if not os.getenv("RAZORPAY_WEBHOOK_SECRET"):
        print(
            "[webhook_server] WARNING: RAZORPAY_WEBHOOK_SECRET is not set. "
            "Every incoming webhook will be rejected with a 500 until it is "
            "configured (get this value when you set up the webhook in your "
            "Razorpay dashboard - it is NOT your API key secret)."
        )
    app.run(port=5000, debug=False)