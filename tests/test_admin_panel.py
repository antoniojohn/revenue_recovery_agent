"""
Tests for the admin settings panel (agent/admin_panel.py).

Covers:
  1. Auth fails closed when ADMIN_PASSWORD is not configured, and
     rejects wrong credentials / accepts correct ones otherwise.
  2. The two update routes reject negative values and unrecognized
     causes with a rendered "Update rejected" message, not a 500.
  3. Regression test for the stored-XSS fix: a crafted `updated_by`
     value must be HTML-escaped in the rendered page, not reproduced
     verbatim.
  4. /health is reachable without auth.

Run with: pytest tests/test_admin_panel.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import admin_panel, settings_store


@pytest.fixture
def client():
    admin_panel.app.config["TESTING"] = True
    return admin_panel.app.test_client()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    yield


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    """Stub settings_store so these tests never touch a real DB, and so
    we can control exactly what get_all_settings() returns for the
    XSS-escaping test below."""
    fake_settings = {
        "min_retry_amount": {"value": 150, "updated_at": "2026-01-01T00:00:00+00:00", "updated_by": "system"},
        "max_retries": {
            "value": dict(settings_store.DEFAULT_MAX_RETRIES),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "updated_by": "system",
        },
    }
    monkeypatch.setattr(settings_store, "get_all_settings", lambda: fake_settings)
    monkeypatch.setattr(settings_store, "update_min_retry_amount", lambda *a, **kw: None)
    monkeypatch.setattr(settings_store, "update_max_retries", lambda *a, **kw: None)
    yield fake_settings


def _auth_header(username, password):
    import base64
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_index_requires_auth(client):
    resp = client.get("/")
    assert resp.status_code == 401


def test_index_rejects_wrong_password(client):
    resp = client.get("/", headers=_auth_header("admin", "wrong"))
    assert resp.status_code == 401


def test_index_accepts_correct_credentials(client):
    resp = client.get("/", headers=_auth_header("admin", "correct-horse"))
    assert resp.status_code == 200


def test_auth_fails_closed_when_no_password_configured(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    resp = client.get("/", headers=_auth_header("admin", "anything"))
    assert resp.status_code == 401


def test_health_does_not_require_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_update_min_retry_amount_rejects_negative_with_clean_message(client, monkeypatch):
    def raise_negative(*a, **kw):
        raise ValueError("min_retry_amount cannot be negative")
    monkeypatch.setattr(settings_store, "update_min_retry_amount", raise_negative)

    resp = client.post(
        "/update-min-retry-amount",
        data={"min_retry_amount": "-5"},
        headers=_auth_header("admin", "correct-horse"),
    )
    assert resp.status_code == 200
    assert b"Update rejected" in resp.data


def test_update_max_retries_rejects_unrecognized_cause(client):
    resp = client.post(
        "/update-max-retries",
        data={"cause": "NOT_A_REAL_CAUSE", "max_attempts": "3"},
        headers=_auth_header("admin", "correct-horse"),
    )
    assert resp.status_code == 200
    assert b"Update rejected" in resp.data
    assert b"unrecognized cause" in resp.data


def test_update_min_retry_amount_rejects_non_numeric_input(client):
    resp = client.post(
        "/update-min-retry-amount",
        data={"min_retry_amount": "not_a_number"},
        headers=_auth_header("admin", "correct-horse"),
    )
    assert resp.status_code == 200
    assert b"Update rejected" in resp.data


def test_updated_by_is_escaped_in_rendered_page(client, stub_settings):
    """Regression test for the stored-XSS fix: updated_by originates
    from request.authorization.username, which is attacker/admin
    controllable, and is persisted then re-rendered on every page load.
    A raw <script> tag must never appear unescaped in the response."""
    stub_settings["min_retry_amount"]["updated_by"] = "<script>alert(1)</script>"

    resp = client.get("/", headers=_auth_header("admin", "correct-horse"))

    assert b"<script>alert(1)</script>" not in resp.data
    assert b"&lt;script&gt;" in resp.data


def test_success_message_is_escaped_too(client, monkeypatch):
    """The banner message can also embed unvalidated form input (e.g.
    a ValueError's text echoing back the raw submitted value) - it must
    be escaped the same way updated_by is."""
    def raise_with_injection(*a, **kw):
        raise ValueError("<img src=x onerror=alert(1)>")
    monkeypatch.setattr(settings_store, "update_min_retry_amount", raise_with_injection)

    resp = client.post(
        "/update-min-retry-amount",
        data={"min_retry_amount": "10"},
        headers=_auth_header("admin", "correct-horse"),
    )
    assert b"<img src=x onerror=alert(1)>" not in resp.data
    assert b"&lt;img" in resp.data


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])