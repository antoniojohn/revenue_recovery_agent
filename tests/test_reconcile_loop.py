"""
Tests for the reconcile long-running loop wrapper (agent/reconcile_loop.py).

Covers:
  1. A single bad poll cycle (reconcile_once raising) does not crash
     the loop - it is caught, logged, and the loop continues.
  2. KeyboardInterrupt during time.sleep() (i.e. Ctrl+C between polls)
     is caught for a clean shutdown and main() returns 0, rather than
     propagating as a raw traceback.
  3. KeyboardInterrupt raised from inside reconcile_once() itself also
     results in a clean shutdown, not being swallowed by the inner
     "log and continue" exception handler (KeyboardInterrupt does not
     subclass Exception, so it must pass through untouched).

time.sleep is mocked throughout so these tests run instantly rather
than waiting on the real INTERVAL_SECONDS.

Run with: pytest tests/test_reconcile_loop.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock

from agent import reconcile_loop


def _summary(**overrides):
    base = {"checked": 0, "resolved_paid": 0, "resolved_timeout": 0, "still_pending": 0}
    base.update(overrides)
    return base


def test_loop_continues_after_reconcile_once_raises(capsys):
    """A single bad poll cycle must be logged, not crash the service."""
    call_count = {"n": 0}

    def fake_reconcile_once():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("temporary db error")
        return _summary(checked=5)

    def fake_sleep(seconds):
        if call_count["n"] >= 2:
            raise KeyboardInterrupt()

    with patch.object(reconcile_loop.reconcile_pending, "reconcile_once", side_effect=fake_reconcile_once), \
         patch.object(reconcile_loop.time, "sleep", side_effect=fake_sleep):
        result = reconcile_loop.main()

    assert result == 0
    captured = capsys.readouterr()
    assert "Error during reconcile pass" in captured.out
    assert "temporary db error" in captured.out
    assert call_count["n"] == 2


def test_keyboard_interrupt_during_sleep_shuts_down_cleanly(capsys):
    with patch.object(reconcile_loop.reconcile_pending, "reconcile_once", return_value=_summary()), \
         patch.object(reconcile_loop.time, "sleep", side_effect=KeyboardInterrupt()):
        result = reconcile_loop.main()

    assert result == 0
    captured = capsys.readouterr()
    assert "Shutting down" in captured.out


def test_keyboard_interrupt_inside_reconcile_once_is_not_swallowed(capsys):
    """KeyboardInterrupt raised from inside reconcile_once() must not be
    caught by the inner 'log and continue' except Exception block - it
    should propagate up to the outer handler for a clean shutdown."""
    with patch.object(reconcile_loop.reconcile_pending, "reconcile_once", side_effect=KeyboardInterrupt()), \
         patch.object(reconcile_loop.time, "sleep") as mock_sleep:
        result = reconcile_loop.main()

    assert result == 0
    mock_sleep.assert_not_called()
    captured = capsys.readouterr()
    assert "Shutting down" in captured.out
    assert "Error during reconcile pass" not in captured.out


def test_successful_poll_prints_summary_fields(capsys):
    call_count = {"n": 0}

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    with patch.object(
        reconcile_loop.reconcile_pending, "reconcile_once",
        return_value=_summary(checked=10, resolved_paid=3, resolved_timeout=1, still_pending=6),
    ), patch.object(reconcile_loop.time, "sleep", side_effect=fake_sleep):
        reconcile_loop.main()

    captured = capsys.readouterr()
    assert "checked=10" in captured.out
    assert "resolved_paid=3" in captured.out
    assert "resolved_timeout=1" in captured.out
    assert "still_pending=6" in captured.out


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])