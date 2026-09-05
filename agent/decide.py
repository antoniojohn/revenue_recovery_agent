"""
Decision layer: maps a diagnosed cause to a bounded recovery action.
Every action must be capped (no infinite retries) and explainable.

Retry caps (MAX_RETRIES) and the minimum-retry-amount policy gate
(MIN_RETRY_AMOUNT) used to be hardcoded constants in this file. They
now live in agent/settings_store.py, backed by SQLite and editable at
runtime through agent/admin_panel.py.
"""

from agent import settings_store

ACTION_MAP = {
    "INSUFFICIENT_FUNDS": "RETRY_AFTER_DELAY",
    "BANK_TIMEOUT": "RETRY_IMMEDIATE",
    "CARD_DECLINED": "RETRY_IMMEDIATE",
    "INVALID_CVV": "NOTIFY_USER",
    "EXPIRED_CARD": "NOTIFY_USER_CARD_UPDATE",
    "UNKNOWN": "ESCALATE",
}

# Compliance boundary, not a tunable business threshold - deliberately a
# hardcoded constant here, same reasoning as ACTION_MAP staying hardcoded
# (see module docstring). Modeled on NPCI's e-mandate / UPI Autopay
# framework: a recurring-payment retry above this amount requires renewed
# Additional Factor of Authentication (AFA) from the customer - it is not
# something an automated agent is permitted to silently re-attempt, no
# matter how the amount-vs-min-retry policy gate below is configured by
# an admin. This is why it's checked BEFORE the configurable
# min_retry_amount gate: a business owner can loosen or tighten how
# aggressively small amounts get retried via the admin panel, but cannot
# accidentally configure their way past a regulatory requirement this
# constant encodes.
AFA_REQUIRED_ABOVE_AMOUNT = 15000

# Backward-compatible module-level snapshots, for any code (or existing
# tests) that import these names directly. choose_action() below always
# re-reads the LIVE value from settings_store on every call, so an
# admin-panel change takes effect immediately for actual decisions -
# these two names only reflect the value as of process start / import
# time and should not be relied on for anything but display/compat.
MIN_RETRY_AMOUNT = settings_store.get_min_retry_amount()
MAX_RETRIES = settings_store.get_max_retries()


def choose_action(cause: str, record: dict, source: str = "rule") -> dict:
    """Return a bounded action dict: {type, max_attempts, source, policy, reasoning}."""
    min_retry_amount = settings_store.get_min_retry_amount()
    max_retries = settings_store.get_max_retries()

    action_type = ACTION_MAP.get(cause, "ESCALATE")
    max_attempts = max_retries.get(cause, 0)
    amount = record.get("amount", 0)

    policy_approved = True
    policy_note = "within bounds"

    if action_type.startswith("RETRY") and amount > AFA_REQUIRED_ABOVE_AMOUNT:
        # Regulatory gate takes precedence over the business-configurable
        # min-retry-amount gate below - see AFA_REQUIRED_ABOVE_AMOUNT
        # docstring. A large recurring-payment retry is escalated to a
        # human/manual re-authentication flow, never auto-retried.
        action_type = "ESCALATE"
        max_attempts = 0
        policy_approved = False
        policy_note = (
            f"retry rejected - amount ₹{amount} exceeds ₹{AFA_REQUIRED_ABOVE_AMOUNT} "
            "internal AFA-inspired safeguard threshold; modeled on NPCI's "
            "e-mandate Additional Factor of Authentication requirement as a "
            "conservative compliance-minded cap, not a literal e-mandate "
            "transaction - large retries are escalated for manual/human "
            "re-authentication rather than auto-retried"
        )
    elif action_type.startswith("RETRY") and amount < min_retry_amount:
        # Policy gate rejects the retry and downgrades to escalation
        # instead - never silently drops the case.
        action_type = "ESCALATE"
        max_attempts = 0
        policy_approved = False
        policy_note = f"retry rejected - amount below ₹{min_retry_amount} minimum retry threshold"

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