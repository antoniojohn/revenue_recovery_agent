"""
Tests for the diagnosis layer (agent/diagnose.py).

Covers:
  1. Rule-based fast path takes priority over the LLM fallback for any
     known error_code.
  2. The LLM fallback: no-API-key graceful degradation, a clean valid
     response, the truncated-response prefix-match recovery (a
     reasoning model cut off mid-word, e.g. "CARD_DECL" should still
     resolve to CARD_DECLINED), an unrecognized/garbage response, and
     an API exception - all of these must resolve to a cause bucket or
     UNKNOWN, never raise.
  3. Dedup: _already_processed_ids() reading real audit log content,
     and load_batch() actually skipping payment_ids already seen in a
     prior run (both real and synthetic).

All filesystem/network dependencies (litellm.completion, razorpay_client,
audit log path) are monkeypatched to isolated fakes - these tests never
make a real API call or touch the real logs/ directory.

Run with: pytest tests/test_diagnose.py -v
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import diagnose


# ---------------------------------------------------------------------
# Rule-based fast path
# ---------------------------------------------------------------------

def test_classify_uses_rule_map_for_known_error_code():
    """A record with a known error_code must resolve via the rule map
    and report source='rule' - it should never reach the LLM fallback."""
    record = {"payment_id": "pay_1", "error_code": "insufficient_funds"}

    cause, source = diagnose.classify(record)

    assert cause == "INSUFFICIENT_FUNDS"
    assert source == "rule"


def test_classify_rule_lookup_is_case_insensitive():
    """error_code is lowercased before the rule-map lookup, so an
    unexpected-case value from an upstream source should still match."""
    record = {"payment_id": "pay_2", "error_code": "EXPIRED_CARD"}

    cause, source = diagnose.classify(record)

    assert cause == "EXPIRED_CARD"
    assert source == "rule"


def test_classify_falls_back_to_llm_for_unmapped_error_code(monkeypatch):
    """An error_code not in RULE_MAP must be routed to the LLM fallback,
    and the reported source must be 'llm', not 'rule'."""
    monkeypatch.setattr(diagnose, "classify_with_llm", lambda record: "BANK_TIMEOUT")
    record = {"payment_id": "pay_3", "error_code": "customer_bank_server_not_responding"}

    cause, source = diagnose.classify(record)

    assert cause == "BANK_TIMEOUT"
    assert source == "llm"


# ---------------------------------------------------------------------
# LLM fallback - graceful degradation and response parsing
# ---------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _fake_completion(content=None, exception=None):
    """Builds a stand-in for litellm.completion(**kwargs) that either
    returns a fake response with the given content, or raises the
    given exception - mirrors what the real call returns/raises."""
    def _completion(**kwargs):
        if exception:
            raise exception
        return _FakeResponse(content)
    return _completion


def test_classify_with_llm_returns_unknown_when_no_api_key(monkeypatch):
    """With no GROQ_API_KEY set, the fallback must return UNKNOWN rather
    than crash the batch - it should never even attempt to call the LLM."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    record = {"payment_id": "pay_4", "error_code": "some_weird_reason"}

    result = diagnose.classify_with_llm(record)

    assert result == "UNKNOWN"


def test_classify_with_llm_parses_a_clean_valid_response(monkeypatch):
    """A well-formed response (exact category, uppercase) should map
    directly to that cause."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(content="BANK_TIMEOUT"))
    record = {"payment_id": "pay_5", "error_code": "issuer_bank_rejected_transaction_temporarily"}

    result = diagnose.classify_with_llm(record)

    assert result == "BANK_TIMEOUT"


def test_classify_with_llm_recovers_from_truncated_response_via_prefix_match(monkeypatch):
    """A reasoning model cut off mid-word (e.g. token budget exhausted)
    can return a truncated prefix of the real category, like
    'CARD_DECL' instead of 'CARD_DECLINED'. This must still resolve to
    the intended cause rather than falling through to UNKNOWN."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(content="CARD_DECL"))
    record = {"payment_id": "pay_6", "error_code": "some_ambiguous_reason"}

    result = diagnose.classify_with_llm(record)

    assert result == "CARD_DECLINED"


def test_classify_with_llm_matches_when_response_contains_extra_text(monkeypatch):
    """A reasoning model sometimes wraps the answer in extra text
    despite instructions. If a known cause appears anywhere in the
    reply, it should still be extracted rather than treated as
    unrecognized."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(content="the category is EXPIRED_CARD here"))
    record = {"payment_id": "pay_7", "error_code": "card_no_longer_valid"}

    result = diagnose.classify_with_llm(record)

    assert result == "EXPIRED_CARD"


def test_classify_with_llm_returns_unknown_for_short_unrecognized_text(monkeypatch):
    """Garbage output that is too short to safely prefix-match against
    any known cause (len < 4) and doesn't contain one must fall
    through to UNKNOWN rather than guessing."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(content="N/A"))
    record = {"payment_id": "pay_8", "error_code": "truly_unclassifiable_reason"}

    result = diagnose.classify_with_llm(record)

    assert result == "UNKNOWN"


def test_classify_with_llm_returns_unknown_when_model_says_unknown(monkeypatch):
    """The model explicitly replying UNKNOWN must be honored as-is,
    not matched against any cause by accident."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(content="UNKNOWN"))
    record = {"payment_id": "pay_9", "error_code": "totally_novel_reason"}

    result = diagnose.classify_with_llm(record)

    assert result == "UNKNOWN"


def test_classify_with_llm_returns_unknown_on_api_exception(monkeypatch):
    """A network error, rate limit, or malformed response must not
    crash the batch - it should degrade to UNKNOWN for that one record
    and let the rest of the batch continue."""
    monkeypatch.setenv("GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(diagnose.litellm, "completion", _fake_completion(exception=RuntimeError("rate limited")))
    record = {"payment_id": "pay_10", "error_code": "some_reason"}

    result = diagnose.classify_with_llm(record)

    assert result == "UNKNOWN"


# ---------------------------------------------------------------------
# Dedup against the audit log
# ---------------------------------------------------------------------

def test_already_processed_ids_reads_payment_ids_from_audit_log(tmp_path):
    """_already_processed_ids must return the set of payment_ids found
    in a real audit log file's entries."""
    log_path = tmp_path / "audit_log.json"
    log_path.write_text(json.dumps([
        {"payment_id": "pay_seen_1", "recovered": True},
        {"payment_id": "pay_seen_2", "recovered": False},
    ]))

    result = diagnose._already_processed_ids(str(log_path))

    assert result == {"pay_seen_1", "pay_seen_2"}


def test_already_processed_ids_returns_empty_set_when_file_missing(tmp_path):
    """A first-ever run has no audit log yet - this must return an
    empty set, not raise FileNotFoundError."""
    missing_path = tmp_path / "does_not_exist.json"

    result = diagnose._already_processed_ids(str(missing_path))

    assert result == set()


def test_already_processed_ids_returns_empty_set_for_corrupt_json(tmp_path):
    """A partially-written or corrupted audit log must not crash the
    pipeline on startup - treat it the same as no prior run."""
    log_path = tmp_path / "audit_log.json"
    log_path.write_text("{not valid json")

    result = diagnose._already_processed_ids(str(log_path))

    assert result == set()


def test_load_batch_skips_already_processed_synthetic_records(tmp_path, monkeypatch):
    """A synthetic record whose payment_id is already in the audit log
    from a prior run must be excluded from the batch, so re-running the
    pipeline doesn't double-count it."""
    monkeypatch.setattr(diagnose, "_already_processed_ids", lambda *a, **k: {"pay_synthetic_0001"})

    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "fetch_failed_payments", lambda *a, **k: [])

    batch_path = tmp_path / "failed_payments.json"
    batch_path.write_text(json.dumps([
        {"payment_id": "pay_synthetic_0001", "amount": 199, "error_code": "expired_card"},
        {"payment_id": "pay_synthetic_0002", "amount": 499, "error_code": "insufficient_funds"},
    ]))

    result = diagnose.load_batch(str(batch_path))

    ids = {r["payment_id"] for r in result}
    assert ids == {"pay_synthetic_0002"}


def test_load_batch_skips_already_processed_real_records(tmp_path, monkeypatch):
    """A real Razorpay record already present in the audit log must be
    excluded too - dedup applies identically to real and synthetic
    sources."""
    monkeypatch.setattr(diagnose, "_already_processed_ids", lambda *a, **k: {"pay_real_seen"})

    from agent import razorpay_client
    monkeypatch.setattr(
        razorpay_client,
        "fetch_failed_payments",
        lambda *a, **k: [
            {"payment_id": "pay_real_seen", "amount": 500, "error_code": "card_declined"},
            {"payment_id": "pay_real_new", "amount": 700, "error_code": "bank_timeout"},
        ],
    )

    batch_path = tmp_path / "failed_payments.json"
    batch_path.write_text(json.dumps([]))

    result = diagnose.load_batch(str(batch_path))

    ids = {r["payment_id"] for r in result}
    assert ids == {"pay_real_new"}


def test_load_batch_works_with_zero_real_records(tmp_path, monkeypatch):
    """The pipeline must never depend on a connected Razorpay test
    account having failed payments - an empty real-records list should
    fall through cleanly to synthetic data alone."""
    monkeypatch.setattr(diagnose, "_already_processed_ids", lambda *a, **k: set())

    from agent import razorpay_client
    monkeypatch.setattr(razorpay_client, "fetch_failed_payments", lambda *a, **k: [])

    batch_path = tmp_path / "failed_payments.json"
    batch_path.write_text(json.dumps([
        {"payment_id": "pay_only_synthetic", "amount": 199, "error_code": "expired_card"},
    ]))

    result = diagnose.load_batch(str(batch_path))

    assert len(result) == 1
    assert result[0]["payment_id"] == "pay_only_synthetic"


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])