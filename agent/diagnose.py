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


def _already_processed_ids(audit_log_path: str = "logs/audit_log.json") -> set:
    """Payment IDs already present in the audit log from a previous run.

    Used so real Razorpay records aren't silently reprocessed as "new"
    every time the pipeline runs - each real failed payment should only
    count toward the report once.
    """
    try:
        with open(audit_log_path) as f:
            log = json.load(f)
        return {entry.get("payment_id") for entry in log}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def load_batch(path: str) -> list[dict]:
    """Load the batch of failed payments to process.

    Tries real failed payments from the connected Razorpay test account
    first (proof of real platform integration), then adds synthetic
    records from the given path to reach a batch large enough to report
    meaningful metrics on. Works fine with zero real payments too - the
    pipeline never depends on Razorpay data being present.

    Records already seen in a previous run (per the audit log) are
    skipped — for both real Razorpay records and synthetic ones — so
    re-running the pipeline doesn't double-count the same payment_id as
    a fresh case each time.
    """
    from agent import razorpay_client

    processed = _already_processed_ids()

    real_records = razorpay_client.fetch_failed_payments()
    if real_records:
        before = len(real_records)
        real_records = [r for r in real_records if r.get("payment_id") not in processed]
        skipped = before - len(real_records)
        if skipped:
            print(f"[diagnose] Skipped {skipped} already-processed real payment(s) from a prior run.")
        if real_records:
            print(f"[diagnose] Loaded {len(real_records)} new real failed payment(s) from Razorpay test account.")

    with open(path) as f:
        synthetic_records = json.load(f)

    before = len(synthetic_records)
    synthetic_records = [r for r in synthetic_records if r.get("payment_id") not in processed]
    skipped = before - len(synthetic_records)
    if skipped:
        print(f"[diagnose] Skipped {skipped} already-processed synthetic record(s) from a prior run.")

    return real_records + synthetic_records


def classify(record: dict) -> tuple[str, str]:
    """Return (cause_bucket, source) for a failed payment record.

    source is "rule" if the known error-code map matched directly, or
    "llm" if the record had to go through the Groq fallback. This is
    logged end-to-end so the audit trail and UI can show exactly how
    each decision was made, not just what it was.
    """
    raw_reason = record.get("error_code", "").lower()

    if raw_reason in RULE_MAP:
        return RULE_MAP[raw_reason], "rule"

    return classify_with_llm(record), "llm"


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
            # openai/gpt-oss-20b is a reasoning model: it spends part of the
            # token budget on internal reasoning before writing the final
            # answer, and that reasoning counts against max_tokens just like
            # the visible content does. max_tokens=200 truncated mid-answer;
            # max_tokens=1024 still truncates mid-word on harder cases
            # because reasoning length varies per input, not because 1024
            # is close to enough. reasoning_effort isn't in the typed
            # groq==0.11.0 signature, but the SDK passes unknown kwargs
            # through to the API (it's OpenAI-compatible), so we ask for
            # "low" effort via extra_body to cut reasoning tokens at the
            # source instead of just raising the ceiling.
            max_tokens=1024,
            temperature=0,
            extra_body={"reasoning_effort": "low"},
        )
        raw_content = response.choices[0].message.content or ""
        result = raw_content.strip().upper()
        if result == "UNKNOWN":
            return "UNKNOWN"
        # Reasoning models sometimes wrap the answer in extra text, and a
        # truncated response can end mid-word (e.g. "CARD_DECL"). Check
        # both directions: the cause appearing in the reply, or the reply
        # being a truncated prefix of the cause - either way it's a match,
        # not an UNKNOWN.
        for cause in KNOWN_CAUSES:
            if cause in result or (len(result) >= 4 and cause.startswith(result)):
                return cause
        print(f"[diagnose] LLM returned unrecognized text for {record.get('payment_id')}: {raw_content!r}")
        return "UNKNOWN"
    except Exception as e:
        # Network error, rate limit, bad response, etc. - don't crash the
        # batch over one classification call.
        print(f"[diagnose] LLM fallback failed for {record.get('payment_id')}: {e}")
        return "UNKNOWN"
