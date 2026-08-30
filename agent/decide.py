"""
Decision layer: maps a diagnosed cause to a bounded recovery action.
Every action must be capped (no infinite retries) and explainable.
"""

MAX_RETRIES = {
    "INSUFFICIENT_FUNDS": 2,
    "BANK_TIMEOUT": 1,
    "CARD_DECLINED": 1,
    "INVALID_CVV": 0,   # never auto-retry, always ask user to re-enter
    "EXPIRED_CARD": 0,  # never auto-retry, always ask user to update card
}

ACTION_MAP = {
    "INSUFFICIENT_FUNDS": "RETRY_AFTER_DELAY",
    "BANK_TIMEOUT": "RETRY_IMMEDIATE",
    "CARD_DECLINED": "RETRY_IMMEDIATE",
    "INVALID_CVV": "NOTIFY_USER",
    "EXPIRED_CARD": "NOTIFY_USER_CARD_UPDATE",
    "UNKNOWN": "ESCALATE",
}


def choose_action(cause: str, record: dict) -> dict:
    """Return a bounded action dict: {type, max_attempts, reason}."""
    action_type = ACTION_MAP.get(cause, "ESCALATE")
    max_attempts = MAX_RETRIES.get(cause, 0)

    return {
        "type": action_type,
        "max_attempts": max_attempts,
        "cause": cause,
        "reasoning": f"Cause classified as {cause}; bounded to {max_attempts} attempt(s).",
    }
