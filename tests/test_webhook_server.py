"""
Tests for the Razorpay webhook receiver (agent/webhook_server.py).

Covers the two things this endpoint must never get wrong:
  1. Requests without a valid, correctly-signed X-Razorpay-Signature
     header must be rejected - accepting an unsigned request would let
     anyone who finds the URL fake a "payment succeeded" event.
  2. A correctly-signed event for a real pending order must resolve it
     (removed from pending_store, appended to the audit log as
     resolved) - and an event for anything else must be logged
     harmlessly without resolving anything.

Uses Flask's test client, so these tests never open a real network
port - and every filesystem path (pending store, audit log, webhook
event log) is monkeypatched to a throwaway location per test, so they
never touch the real logs/ directory used by the actual pipeline.

Run with: pytest tests/test_webhook_server.py -v
"""

import hashlib
import hmac
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import webhook_server, pending_store, execute

TEST_SECRET = "test_secret_123"


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Redirect every file this module touches to throwaway paths, and
    set a known webhook secret, for every test in this file."""
    monkeypatch.setattr(pending_store, "PENDING_STORE_PATH", str(tmp_path / "pending_retries.json"))
    monkeypatch.setattr(execute, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.json"))
    monkeypatch.setattr(webhook_server, "WEBHOOK_EVENTS_LOG", str(tmp_path / "webhook_events.json"))
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    yield


@pytest.fixture
def client():
    webhook_server.app.config["TESTING"] = True
    return webhook_server.app.test_client()


def _signed_post(client, payload: dict, secret: str = TEST_SECRET):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/razorpay",
        data=body,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )


def _payment_event(event: str, order_id: str, status: str, amount_paise: int = 50000):
    return {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_webhook_test",
                    "order_id": order_id,
                    "status": status,
                    "amount": amount_paise,
                }
            }
        },
    }


def _add_pending(order_id, **overrides):
    entry = {
        "order_id": order_id,
        "original_payment_id": f"pay_original_{order_id}",
        "amount": 500,
        "cause": "CARD_DECLINED",
        "source": "rule",
        "action_type": "RETRY_IMMEDIATE",
        "max_attempts": 1,
        "policy_approved": True,
        "policy_note": "within bounds",
        "reasoning": "test",
        "initiated_at": "2026-09-03T20:00:00+00:00",
    }
    entry.update(overrides)
    pending_store.add_pending(entry)


def test_valid_signature_is_accepted(client):
    """A correctly-signed request for an event this endpoint doesn't
    specifically act on should still be accepted with 200 - the
    endpoint's job is to verify and log, not to only accept events it
    recognizes."""
    payload = _payment_event("payment.authorized", "order_unrelated", "authorized")

    response = _signed_post(client, payload)

    assert response.status_code == 200
    assert response.get_json()["status"] == "received"


def test_invalid_signature_is_rejected(client):
    """A request signed with the WRONG secret must be rejected - this
    is the core security guarantee of the whole endpoint."""
    payload = _payment_event("payment.captured", "order_x", "captured")

    response = _signed_post(client, payload, secret="wrong_secret")

    assert response.status_code == 400


def test_missing_signature_header_is_rejected(client):
    """A request with no signature header at all must be rejected, not
    treated as unsigned-but-trusted."""
    body = json.dumps(_payment_event("payment.captured", "order_x", "captured")).encode()

    response = client.post(
        "/webhooks/razorpay", data=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400


def test_missing_webhook_secret_fails_closed(client, monkeypatch):
    """If RAZORPAY_WEBHOOK_SECRET isn't configured at all, the endpoint
    must refuse every request rather than silently trusting them - a
    missing secret is a config error, not a reason to accept anything."""
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    payload = _payment_event("payment.captured", "order_x", "captured")

    response = _signed_post(client, payload)

    assert response.status_code == 500


def test_resolves_a_matching_pending_order_as_recovered(client):
    """The core happy path: a payment.captured event for an order_id
    that IS in the pending store must resolve it as recovered=True,
    remove it from pending_store, and append a 'resolved' outcome to
    the audit log with resolution_method 'webhook'."""
    _add_pending("order_real_test")

    payload = _payment_event("payment.captured", "order_real_test", "captured")
    response = _signed_post(client, payload)

    assert response.status_code == 200
    assert pending_store.get_pending("order_real_test") is None  # resolved, removed

    with open(execute.AUDIT_LOG_PATH) as f:
        log = json.load(f)
    resolved = [r for r in log if r.get("order_id") == "order_real_test"]
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"
    assert resolved[0]["resolution_method"] == "webhook"
    assert resolved[0]["recovered"] is True


def test_payment_failed_resolves_as_not_recovered(client):
    """A payment.failed event for a pending order must resolve it as
    recovered=False - the retry itself failed, this must not be
    silently treated as a success."""
    _add_pending("order_failed_test")

    payload = _payment_event("payment.failed", "order_failed_test", "failed")
    _signed_post(client, payload)

    with open(execute.AUDIT_LOG_PATH) as f:
        log = json.load(f)
    resolved = [r for r in log if r.get("order_id") == "order_failed_test"]
    assert resolved[0]["recovered"] is False


def test_event_for_unknown_order_id_is_logged_but_not_resolved(client):
    """An event for an order_id that was never added to the pending
    store (e.g. it doesn't belong to this system, or was already
    resolved by a previous webhook/poll) must be accepted and logged,
    but must not create a phantom resolution."""
    payload = _payment_event("payment.captured", "order_never_pending", "captured")

    response = _signed_post(client, payload)

    assert response.status_code == 200
    with open(webhook_server.WEBHOOK_EVENTS_LOG) as f:
        events = json.load(f)
    assert events[-1]["resolved_pending"] is False


def test_duplicate_webhook_for_already_resolved_order_is_harmless(client):
    """Razorpay's own docs note webhooks can be delivered more than
    once for the same event - resolving the same order_id a second
    time must not raise or double-append a resolution."""
    _add_pending("order_dup_test")
    payload = _payment_event("payment.captured", "order_dup_test", "captured")

    first = _signed_post(client, payload)
    second = _signed_post(client, payload)  # duplicate delivery

    assert first.status_code == 200
    assert second.status_code == 200
    with open(execute.AUDIT_LOG_PATH) as f:
        log = json.load(f)
    resolved = [
        r for r in log
        if r.get("order_id") == "order_dup_test" and r.get("status") == "resolved"
    ]
    assert len(resolved) == 1  # only resolved once, not twice
