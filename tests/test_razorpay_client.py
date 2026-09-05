"""
Tests for the Razorpay test-mode client (agent/razorpay_client.py).

Covers:
  1. Credential gating: _get_client() returns None when credentials are
     missing or are still the unedited placeholder value.
  2. Every public function degrades to None/[] rather than raising when
     no client is configured or the underlying API call fails.
  3. Regression test for the amount-rounding fix in attempt_retry():
     rupees-as-float must be converted to a clean integer number of
     paise, never a near-integer float, before being sent to Razorpay's
     order.create API.

Run with: pytest tests/test_razorpay_client.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch

from agent import razorpay_client


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    yield


def test_get_client_returns_none_when_no_credentials():
    assert razorpay_client._get_client() is None


def test_get_client_returns_none_for_placeholder_key(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_your_test_key_id_here")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "somesecret")
    assert razorpay_client._get_client() is None


def test_get_client_returns_client_with_valid_credentials(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_realkey123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "realsecret")
    with patch.object(razorpay_client.razorpay, "Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        client = razorpay_client._get_client()
        assert client is mock_instance
        mock_client_cls.assert_called_once_with(auth=("rzp_test_realkey123", "realsecret"))


def test_get_client_mounts_timeout_adapter(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_realkey123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "realsecret")
    with patch.object(razorpay_client.razorpay, "Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        razorpay_client._get_client()
        assert mock_instance.session.mount.call_count == 2


def test_fetch_failed_payments_returns_empty_list_without_credentials():
    assert razorpay_client.fetch_failed_payments() == []


def test_fetch_failed_payments_filters_and_normalizes():
    fake_response = {
        "items": [
            {"id": "pay_1", "status": "failed", "amount": 15000, "currency": "INR",
             "error_code": "BAD_REQUEST_ERROR", "error_description": "bank declined",
             "email": "a@example.com"},
            {"id": "pay_2", "status": "captured", "amount": 5000},
        ]
    }
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.payment.all.return_value = fake_response
        mock_get_client.return_value = mock_client

        result = razorpay_client.fetch_failed_payments()

        assert len(result) == 1
        assert result[0]["payment_id"] == "pay_1"
        assert result[0]["amount"] == 150.0
        assert result[0]["error_code"] == "bad_request_error"
        assert result[0]["customer_id"] == "a@example.com"
        assert result[0]["source"] == "razorpay_live_test_account"


def test_fetch_failed_payments_returns_empty_list_on_api_exception():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.payment.all.side_effect = Exception("network error")
        mock_get_client.return_value = mock_client

        assert razorpay_client.fetch_failed_payments() == []


def test_check_payment_status_returns_none_without_client():
    assert razorpay_client.check_payment_status("pay_123") is None


def test_check_payment_status_returns_none_without_payment_id(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_realkey123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "realsecret")
    assert razorpay_client.check_payment_status("") is None
    assert razorpay_client.check_payment_status(None) is None


def test_check_payment_status_returns_status_on_success():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {"status": "captured"}
        mock_get_client.return_value = mock_client

        assert razorpay_client.check_payment_status("pay_123") == "captured"


def test_check_payment_status_returns_none_on_exception():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.payment.fetch.side_effect = Exception("not found")
        mock_get_client.return_value = mock_client

        assert razorpay_client.check_payment_status("pay_123") is None


def test_check_order_status_returns_none_without_client():
    assert razorpay_client.check_order_status("order_123") is None


def test_check_order_status_returns_status_on_success():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.order.fetch.return_value = {"status": "paid"}
        mock_get_client.return_value = mock_client

        assert razorpay_client.check_order_status("order_123") == "paid"


def test_check_order_status_returns_none_on_exception():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.order.fetch.side_effect = Exception("gone")
        mock_get_client.return_value = mock_client

        assert razorpay_client.check_order_status("order_123") is None


def test_attempt_retry_returns_none_without_client():
    assert razorpay_client.attempt_retry({"amount": 100, "payment_id": "pay_1"}) is None


def test_attempt_retry_sends_integer_paise_not_float():
    """Regression test for the amount-rounding bug: record['amount'] is
    rupees as a float, and raw floating point multiplication of some
    values (19.99 * 100 == 1998.9999999999998) does not land on a clean
    integer. Razorpay's order.create requires amount as an integer
    number of paise, so this must be rounded before being sent - not
    sent as whatever float multiplication happens to produce, and not
    truncated with int() either, since that would silently lose a
    paisa (int(1998.9999999999998) == 1998, not 1999)."""
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_abc"}
        mock_get_client.return_value = mock_client

        record = {"amount": 19.99, "currency": "INR", "payment_id": "pay_1"}
        result = razorpay_client.attempt_retry(record)

        assert result == {"id": "order_abc"}
        call_kwargs = mock_client.order.create.call_args[0][0]
        assert call_kwargs["amount"] == 1999
        assert isinstance(call_kwargs["amount"], int)


def test_attempt_retry_returns_none_on_exception():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.order.create.side_effect = Exception("api down")
        mock_get_client.return_value = mock_client

        assert razorpay_client.attempt_retry({"amount": 100, "payment_id": "pay_1"}) is None


def test_attempt_retry_includes_retry_notes():
    with patch.object(razorpay_client, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_xyz"}
        mock_get_client.return_value = mock_client

        record = {"amount": 500, "currency": "INR", "payment_id": "pay_999"}
        razorpay_client.attempt_retry(record)

        call_kwargs = mock_client.order.create.call_args[0][0]
        assert call_kwargs["notes"]["retry_for_payment_id"] == "pay_999"


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])