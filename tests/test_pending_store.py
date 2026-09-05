"""
Tests for the pending-retry store (agent/pending_store.py).

Each test uses a fresh, isolated file (monkeypatched PENDING_STORE_PATH)
so these tests never touch the real logs/pending_retries.json used by
the actual pipeline.

Run with: pytest tests/test_pending_store.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import pending_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the module at a throwaway file for every test in this file."""
    fake_path = tmp_path / "pending_retries.json"
    monkeypatch.setattr(pending_store, "PENDING_STORE_PATH", str(fake_path))
    yield fake_path


def _entry(order_id="order_abc123", **overrides):
    base = {
        "order_id": order_id,
        "original_payment_id": "pay_test_001",
        "amount": 500,
        "cause": "CARD_DECLINED",
        "source": "rule",
        "action_type": "RETRY_IMMEDIATE",
        "max_attempts": 1,
        "policy_approved": True,
        "policy_note": "within bounds",
        "reasoning": "test entry",
        "initiated_at": "2026-09-03T20:16:37.307642+00:00",
    }
    base.update(overrides)
    return base


def test_add_and_get_pending_round_trip():
    """An entry added with add_pending should be retrievable by its
    order_id afterward, with all fields intact."""
    pending_store.add_pending(_entry())

    result = pending_store.get_pending("order_abc123")

    assert result is not None
    assert result["original_payment_id"] == "pay_test_001"
    assert result["amount"] == 500


def test_add_pending_requires_order_id():
    """add_pending must reject an entry with no order_id - that's the
    only key webhooks/polling can resolve against later, so silently
    accepting one without it would create an unresolvable record."""
    entry = _entry()
    del entry["order_id"]

    with pytest.raises(ValueError):
        pending_store.add_pending(entry)


def test_get_pending_returns_none_for_unknown_order():
    """Looking up an order_id that was never added should return None,
    not raise - callers (webhook_server, reconcile_pending) rely on
    this to distinguish 'not ours' from an error."""
    assert pending_store.get_pending("order_never_added") is None


def test_remove_pending_deletes_the_entry():
    """After remove_pending, the entry must no longer be retrievable -
    this is what keeps a resolved case from being resolved twice."""
    pending_store.add_pending(_entry())

    pending_store.remove_pending("order_abc123")

    assert pending_store.get_pending("order_abc123") is None


def test_remove_pending_on_unknown_order_is_a_no_op():
    """Removing an order_id that isn't present shouldn't raise or
    corrupt the store - a webhook could plausibly arrive twice for the
    same event, and the second removal must be harmless."""
    pending_store.add_pending(_entry())

    pending_store.remove_pending("order_does_not_exist")  # should not raise

    assert pending_store.get_pending("order_abc123") is not None


def test_list_pending_returns_all_current_entries():
    """list_pending is what reconcile_pending.py iterates over each
    poll - it must return every entry currently in the store, each
    with its order_id intact."""
    pending_store.add_pending(_entry(order_id="order_1"))
    pending_store.add_pending(_entry(order_id="order_2"))

    result = pending_store.list_pending()

    order_ids = {r["order_id"] for r in result}
    assert order_ids == {"order_1", "order_2"}


def test_multiple_entries_do_not_overwrite_each_other():
    """Adding a second entry must not clobber the first - this must
    hold for correct sequential calls, which is the store's only
    supported usage pattern (see its docstring on concurrency)."""
    pending_store.add_pending(_entry(order_id="order_1", amount=100))
    pending_store.add_pending(_entry(order_id="order_2", amount=200))

    first = pending_store.get_pending("order_1")
    second = pending_store.get_pending("order_2")

    assert first["amount"] == 100
    assert second["amount"] == 200


def test_store_persists_to_disk(isolated_store):
    """_load/_save read and write the same file each time - confirm the
    file itself is written to disk (not just held in memory), since
    reconcile_pending.py and webhook_server.py run as separate
    processes from app.py and must see the same state."""
    pending_store.add_pending(_entry())

    assert isolated_store.exists()
    content = isolated_store.read_text()
    assert "order_abc123" in content
