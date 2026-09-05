"""
Pending-retry store: tracks real Razorpay retries that have been
initiated (a new order created via attempt_retry) but not yet confirmed
one way or the other.

This exists because a real retry is asynchronous: attempt_retry()
returns the instant the order is created, but whether the customer
actually pays it is unknown until Razorpay tells us - via a webhook
(agent/webhook_server.py) or, as a fallback for missed webhooks, a
polled status check (agent/reconcile_pending.py). Until one of those
resolves it, the record needs to live somewhere other than "recovered"
or "exception" - this file is that somewhere.

Storage is a flat JSON file for now, same as the audit log. It is a
known limitation, not an oversight: this file is read-modify-written on
every add/resolve, which is not safe against concurrent writers (e.g.
a webhook arriving while reconcile_pending.py's poll loop is also
running). pop_pending() below closes the specific check-then-remove
race between those two callers (see its docstring), but does not make
this file safe against true simultaneous OS-level writes - that is one
of the two problems an eventual Postgres/Redis migration is meant to
fix (the other being audit_log.json's dedup lookup) - see the
"Enterprise database" roadmap item. For a single-process demo it's
adequate; it should not be trusted under real concurrent load.
"""

import json
import os

PENDING_STORE_PATH = "logs/pending_retries.json"


def _load() -> list[dict]:
    try:
        with open(PENDING_STORE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(records: list[dict]) -> None:
    """Write records to disk atomically: write to a temp file first,
    then rename it over the real path. os.replace() is atomic on both
    Windows and Linux, so a crash or power loss mid-write leaves the
    original file untouched instead of corrupted/half-written."""
    os.makedirs(os.path.dirname(PENDING_STORE_PATH) or ".", exist_ok=True)
    tmp_path = PENDING_STORE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp_path, PENDING_STORE_PATH)


def add_pending(entry: dict) -> None:
    """Add a newly-initiated retry. `entry` must include `order_id` -
    that's the key webhooks and polling both resolve against, since the
    retry order's payment_id doesn't exist yet at initiation time."""
    if not entry.get("order_id"):
        raise ValueError("pending entry requires an order_id to be resolvable later")
    records = _load()
    records.append(entry)
    _save(records)


def get_pending(order_id: str) -> dict | None:
    for r in _load():
        if r.get("order_id") == order_id:
            return r
    return None


def remove_pending(order_id: str) -> None:
    records = _load()
    records = [r for r in records if r.get("order_id") != order_id]
    _save(records)


def list_pending() -> list[dict]:
    return _load()


def pop_pending(order_id: str) -> dict | None:
    """Atomically check-and-remove: load the store, find the entry, and
    if present, remove it and save in the same load/save cycle - not
    two separate get_pending() + remove_pending() calls.

    This exists specifically to close the race between webhook_server
    and reconcile_pending both trying to resolve the same order_id at
    nearly the same moment: whichever caller's pop_pending() runs first
    gets the entry back (and it's now removed, so the other caller's
    pop_pending() on the same order_id returns None). Only the caller
    that receives a non-None result should proceed to resolve the case
    - a None result means "someone else already claimed this," not
    "nothing was ever pending."

    Still not safe against true simultaneous OS-level file writes (see
    this module's docstring on flat-file concurrency), but it removes
    the specific TOCTOU window that existed when check and remove were
    two separate _load()/_save() round-trips.
    """
    records = _load()
    match = None
    remaining = []
    for r in records:
        if r.get("order_id") == order_id and match is None:
            match = r
        else:
            remaining.append(r)
    if match is not None:
        _save(remaining)
    return match