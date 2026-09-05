"""
Diagnosis layer: classifies why a payment failed.
Pipeline: rule-based match (fast path) -> LLM fallback (Groq, with
automatic provider failover to Gemini via LiteLLM) for unmapped or
ambiguous failure reasons.

Requires GROQ_API_KEY for the primary path. If GEMINI_API_KEY is also
set, a Groq outage or rate limit automatically fails over to Gemini
instead of degrading straight to UNKNOWN.
"""

import json
import os

import litellm
from dotenv import load_dotenv

load_dotenv()

RULE_MAP = {
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card_declined": "CARD_DECLINED",
    "expired_card": "EXPIRED_CARD",
    "bank_timeout": "BANK_TIMEOUT",
    "invalid_cvv": "INVALID_CVV",
}

KNOWN_CAUSES = list(dict.fromkeys(RULE_MAP.values()))


def _already_processed_ids(audit_log_path: str = "logs/audit_log.json") -> set:
    try:
        with open(audit_log_path) as f:
            log = json.load(f)
        return {entry.get("payment_id") for entry in log}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def load_batch(path: str) -> list[dict]:
    from agent import razorpay_client

    processed = _already_processed_ids()

    real_records = razorpay_client.fetch_failed_payments() or []
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
    raw_reason = (record.get("error_code") or "").lower()

    if raw_reason in RULE_MAP:
        return RULE_MAP[raw_reason], "rule"

    return classify_with_llm(record), "llm"


def classify_with_llm(record: dict) -> str:
    """Fallback classifier for failure reasons the rule map doesn't cover.

    Uses LiteLLM so a Groq rate limit or outage automatically fails
    over to Gemini (see the `fallbacks` list below) rather than
    immediately degrading to UNKNOWN. Gemini was chosen as the fallback
    provider because it's the second key actually available in this
    project's .env - UNKNOWN is now reserved for "no provider could
    classify this", not "the first provider we tried was down".
    """
    if not os.getenv("GROQ_API_KEY"):
        return "UNKNOWN"

    raw_reason = record.get("error_code") or "unknown reason"
    prompt = (
        f"A payment failed with this reason: \"{raw_reason}\".\n"
        f"Classify it into exactly one of these categories: {', '.join(KNOWN_CAUSES)}.\n"
        f"Reply with ONLY the category name in uppercase, nothing else. "
        f"If none of the categories fit, reply UNKNOWN."
    )

    try:
        response = litellm.completion(
            model="groq/openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0,
            extra_body={"reasoning_effort": "low"},
            # Automatic provider failover: if Groq is rate-limited or
            # down, LiteLLM retries with Gemini instead of raising - the
            # except block below is the last resort, reached only if
            # every provider in the chain fails. Requires GEMINI_API_KEY
            # in .env; without it, this fallback attempt also fails and
            # we land in the except block below, same as before.
            fallbacks=["gemini/gemini-2.5-flash"],
            api_key=os.getenv("GROQ_API_KEY"),
        )
        raw_content = response.choices[0].message.content or ""
        result = raw_content.strip().upper()
        if result == "UNKNOWN":
            return "UNKNOWN"
        for cause in KNOWN_CAUSES:
            if cause in result or (len(result) >= 4 and cause.startswith(result)):
                return cause
        print(f"[diagnose] LLM returned unrecognized text for {record.get('payment_id')}: {raw_content!r}")
        return "UNKNOWN"
    except Exception as e:
        print(f"[diagnose] LLM fallback chain exhausted for {record.get('payment_id')}: {e}")
        return "UNKNOWN"