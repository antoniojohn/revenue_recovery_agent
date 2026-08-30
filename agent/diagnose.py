"""
Diagnosis layer: classifies why a payment failed.
Pipeline: rule-based match (fast path) -> LLM fallback (Groq) for
unmapped or ambiguous failure reasons.
"""

import json
import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

# Known failure reason strings -> cause bucket (rule-based, fast path)
RULE_MAP = {
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card_declined": "CARD_DECLINED",
    "expired_card": "EXPIRED_CARD",
    "bank_timeout": "BANK_TIMEOUT",
    "invalid_cvv": "INVALID_CVV",
}

KNOWN_CAUSES = list(dict.fromkeys(RULE_MAP.values()))  # unique, ordered

_groq_client = None


def _get_groq_client():
    """Lazily create the Groq client. Returns None if no API key is set,
    so the pipeline can fail gracefully instead of crashing."""
    global _groq_client
    if _groq_client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            return None
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def load_batch(path: str) -> list[dict]:
    """Load the batch of failed payments to process.

    Tries real failed payments from the connected Razorpay test account
    first (proof of real platform integration), then adds synthetic
    records from the given path to reach a batch large enough to report
    meaningful metrics on. Works fine with zero real payments too - the
    pipeline never depends on Razorpay data being present.
    """
    from agent import razorpay_client

    real_records = razorpay_client.fetch_failed_payments()
    if real_records:
        print(f"[diagnose] Loaded {len(real_records)} real failed payment(s) from Razorpay test account.")

    with open(path) as f:
        synthetic_records = json.load(f)

    return real_records + synthetic_records


def classify(record: dict) -> str:
    """Return a cause bucket for a failed payment record."""
    raw_reason = record.get("error_code", "").lower()

    if raw_reason in RULE_MAP:
        return RULE_MAP[raw_reason]

    return classify_with_llm(record)


def classify_with_llm(record: dict) -> str:
    """Fallback classifier for failure reasons the rule map doesn't cover.

    Sends the raw failure text to Groq and asks it to map the failure into
    one of the known cause buckets, or return UNKNOWN if none fit.
    """
    client = _get_groq_client()
    if client is None:
        # No API key configured - fail gracefully rather than crash the
        # whole batch. This keeps the pipeline runnable even without a
        # Groq key, just without the LLM fallback active.
        return "UNKNOWN"

    raw_reason = record.get("error_code", "unknown reason")
    prompt = (
        f"A payment failed with this reason: \"{raw_reason}\".\n"
        f"Classify it into exactly one of these categories: {', '.join(KNOWN_CAUSES)}.\n"
        f"Reply with ONLY the category name in uppercase, nothing else. "
        f"If none of the categories fit, reply UNKNOWN."
    )

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
            reasoning_effort="low",
        )
        raw_content = response.choices[0].message.content or ""
        result = raw_content.strip().upper()
        # Reasoning models sometimes wrap the answer in extra text - pull
        # out the category name if it appears anywhere in the reply.
        for cause in KNOWN_CAUSES:
            if cause in result:
                return cause
        print(f"[diagnose] LLM returned unrecognized text for {record.get('payment_id')}: {raw_content!r}")
        return "UNKNOWN"
    except Exception as e:
        # Network error, rate limit, bad response, etc. - don't crash the
        # batch over one classification call.
        print(f"[diagnose] LLM fallback failed for {record.get('payment_id')}: {e}")
        return "UNKNOWN"
