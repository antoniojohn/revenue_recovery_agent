"""
Razorpay test-mode client: fetches real failed payments from your
Razorpay test account, normalized into the same record shape used by
the synthetic data generator so diagnose.py can process either source
identically.

Requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env. If they're
not set, or the account has no failed payments yet, this returns an
empty list and the pipeline falls back to synthetic data - it never
crashes the batch.
"""

import os

from dotenv import load_dotenv
import razorpay

load_dotenv()


def _get_client():
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret or "your_test" in key_id:
        return None
    return razorpay.Client(auth=(key_id, key_secret))


def fetch_failed_payments(count: int = 100) -> list[dict]:
    """Fetch real failed payments from the connected Razorpay test account,
    normalized to the same shape as the synthetic data records.

    Returns an empty list if no credentials are configured or no failed
    payments exist yet - callers should fall back to synthetic data.
    """
    client = _get_client()
    if client is None:
        return []

    try:
        response = client.payment.all({"count": count})
    except Exception as e:
        print(f"[razorpay_client] Could not fetch payments: {e}")
        return []

    failed = []
    for p in response.get("items", []):
        if p.get("status") != "failed":
            continue
        failed.append({
            "payment_id": p.get("id"),
            "amount": (p.get("amount") or 0) // 100,  # paise -> rupees
            "currency": p.get("currency", "INR"),
            "error_code": (p.get("error_code") or "unknown_error").lower(),
            "error_description": p.get("error_description", ""),
            "customer_id": p.get("email") or p.get("contact") or "unknown",
            "source": "razorpay_live_test_account",
        })

    return failed


def check_payment_status(payment_id: str):
    """Fetch the current live status of a real payment from Razorpay.

    Used by execute.py to make a genuine API round-trip against payments
    that came from the real test account, rather than relying purely on
    a simulated outcome. Returns None if no credentials are configured,
    the payment isn't found, or the call fails for any reason - callers
    should treat None as "could not verify" and fall back to the
    simulated outcome instead.
    """
    client = _get_client()
    if client is None or not payment_id:
        return None

    try:
        payment = client.payment.fetch(payment_id)
        return payment.get("status")
    except Exception as e:
        print(f"[razorpay_client] Could not verify live status for {payment_id}: {e}")
        return None


def attempt_retry(record: dict):
    """Real API round-trip standing in for a retry attempt: creates a
    fresh Razorpay order for the same amount, since test mode has no
    direct 'retry this failed payment' endpoint. Returns the order dict
    on success, or None if no credentials or the call fails."""
    client = _get_client()
    if client is None:
        return None

    try:
        order = client.order.create({
            "amount": record.get("amount", 0) * 100,
            "currency": record.get("currency", "INR"),
            "notes": {"retry_for_payment_id": record.get("payment_id", "")},
        })
        return order
    except Exception as e:
        print(f"[razorpay_client] Retry attempt failed for {record.get('payment_id')}: {e}")
        return None