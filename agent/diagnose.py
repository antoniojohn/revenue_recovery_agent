"""
Diagnosis layer: classifies why a payment failed.
Pipeline: rule-based match -> LLM fallback for ambiguous cases.
"""

import json

# Known failure reason strings -> cause bucket (rule-based, fast path)
RULE_MAP = {
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card_declined": "CARD_DECLINED",
    "expired_card": "EXPIRED_CARD",
    "bank_timeout": "BANK_TIMEOUT",
    "invalid_cvv": "INVALID_CVV",
}


def load_batch(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def classify(record: dict) -> str:
    """Return a cause bucket for a failed payment record."""
    raw_reason = record.get("error_code", "").lower()

    if raw_reason in RULE_MAP:
        return RULE_MAP[raw_reason]

    # TODO: fall back to LLM (Groq) classification for unmapped/ambiguous
    # error codes or free-text failure descriptions.
    return classify_with_llm(record)


def classify_with_llm(record: dict) -> str:
    """Fallback classifier for cases the rule map doesn't cover."""
    # TODO: call Groq API with the record's raw error text and ask it to
    # pick one of the known cause buckets, or return "UNKNOWN" if it can't.
    return "UNKNOWN"
