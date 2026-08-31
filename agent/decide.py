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

# Policy gate: retrying a payment has a real cost (gateway fees, customer
# friction), so we don't auto-retry very small amounts - it's cheaper to
# let those fail than to spend a retry attempt on them. This is the one
# rule the policy gate enforces today; it's intentionally simple and
# logged so it stays explainable.
MIN_RETRY_AMOUNT = 150


def choose_action(cause: str, record: dict, source: str = "rule") -> dict:
    """Return a bounded action dict: {type, max_attempts, source, policy, reasoning}."""
    action_type = ACTION_MAP.get(cause, "ESCALATE")
    max_attempts = MAX_RETRIES.get(cause, 0)
    amount = record.get("amount", 0)

    policy_approved = True
    policy_note = "within bounds"

    if action_type.startswith("RETRY") and amount < MIN_RETRY_AMOUNT:
        # Policy gate rejects the retry and downgrades to escalation
        # instead - never silently drops the case.
        action_type = "ESCALATE"
        max_attempts = 0
        policy_approved = False
        policy_note = f"retry rejected - amount below ₹{MIN_RETRY_AMOUNT} minimum retry threshold"

    return {
        "type": action_type,
        "max_attempts": max_attempts,
        "cause": cause,
        "source": source,
        "policy_approved": policy_approved,
        "policy_note": policy_note,
        "reasoning": (
            f"Cause classified as {cause} via {source}; bounded to "
            f"{max_attempts} attempt(s); policy {policy_note}."
        ),
    }
