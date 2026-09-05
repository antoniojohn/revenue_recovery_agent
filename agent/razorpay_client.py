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
from requests.adapters import HTTPAdapter
import razorpay

load_dotenv()

# Default timeout (seconds) for every Razorpay API call. Without this,
# a hung connection blocks indefinitely instead of degrading gracefully
# like every other failure mode in this file already does.
DEFAULT_TIMEOUT_SECONDS = 15


class _TimeoutHTTPAdapter(HTTPAdapter):
    """A requests HTTPAdapter that applies a default timeout to every
    request unless the caller explicitly overrides it. The Razorpay SDK
    does not expose a timeout parameter directly, so this is applied at
    the underlying requests.Session level instead."""

    def __init__(self, *args, timeout=DEFAULT_TIMEOUT_SECONDS, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return super().send(request, **kwargs)


def _get_client():
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret or "your_test" in key_id:
        return None
    client = razorpay.Client(auth=(key_id, key_secret))
    adapter = _TimeoutHTTPAdapter()
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client


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
            "amount": round((p.get("amount") or 0) / 100, 2),  # paise -> rupees, no precision loss
            "currency": p.get("currency", "INR"),
            "error_code": (p.get("error_code") or "unknown_error").lower(),
            "error_description": p.get("error_description", ""),
            "customer_id": p.get("email") or p.get("contact") or "unknown",
            "source": "razorpay_live_test_account",
        })

    return failed


def check_payment_status(payment_id: str):
    """Fetch the current live status of a real payment from Razorpay.
    Returns None if no credentials are configured, the payment isn't
    found, or the call fails for any reason."""
    client = _get_client()
    if client is None or not payment_id:
        return None

    try:
        payment = client.payment.fetch(payment_id)
        return payment.get("status")
    except Exception as e:
        print(f"[razorpay_client] Could not verify live status for {payment_id}: {e}")
        return None


def check_order_status(order_id: str):
    """Fetch the current status of a retry order ('created', 'attempted',
    or 'paid'). Returns None if no credentials are configured, the order
    isn't found, or the call fails."""
    client = _get_client()
    if client is None or not order_id:
        return None

    try:
        order = client.order.fetch(order_id)
        return order.get("status")
    except Exception as e:
        print(f"[razorpay_client] Could not fetch order status for {order_id}: {e}")
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
            # round() before sending: record["amount"] is rupees as a
            # float, and Razorpay's API requires amount as an integer
            # number of paise. Without rounding first, floating point
            # imprecision (e.g. 99.5 * 100 == 9950.000000000001) can
            # produce a non-integer amount the API may reject or
            # silently mishandle.
            "amount": round(record.get("amount", 0) * 100),
            "currency": record.get("currency", "INR"),
            "notes": {"retry_for_payment_id": record.get("payment_id", "")},
        })
        return order
    except Exception as e:
        print(f"[razorpay_client] Retry attempt failed for {record.get('payment_id')}: {e}")
        return None