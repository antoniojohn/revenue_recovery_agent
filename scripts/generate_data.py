"""
Generates a synthetic batch of failed payment records for the demo.
Run once to produce data/failed_payments.json.

Amounts and error codes are randomized but weighted to be plausible for
an Indian payments context (amounts in INR paise-free integers).
"""

import json
import random

ERROR_CODES = [
    "insufficient_funds",
    "card_declined",
    "expired_card",
    "bank_timeout",
    "invalid_cvv",
    # Ambiguous / free-text reasons not in the rule map - these should
    # get routed through the LLM fallback in diagnose.py.
    "issuer_bank_rejected_transaction_temporarily",
    "customer_bank_server_not_responding",
    "card_limit_exceeded_for_the_day",
]

# Weighted so insufficient_funds / card_declined are most common,
# matching real-world failure distributions. Ambiguous reasons are rarer.
WEIGHTS = [0.30, 0.26, 0.13, 0.10, 0.07, 0.05, 0.05, 0.04]

random.seed(42)  # reproducible batch for demo purposes


def generate_batch(n: int = 75) -> list[dict]:
    batch = []
    for i in range(1, n + 1):
        error_code = random.choices(ERROR_CODES, weights=WEIGHTS, k=1)[0]
        amount = random.choice([199, 499, 999, 1499, 2499, 4999, 9999])
        batch.append({
            "payment_id": f"pay_synthetic_{i:04d}",
            "amount": amount,
            "currency": "INR",
            "error_code": error_code,
            "customer_id": f"cust_{random.randint(1000, 9999)}",
        })
    return batch


if __name__ == "__main__":
    data = generate_batch()
    with open("data/failed_payments.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"Generated {len(data)} synthetic failed-payment records -> data/failed_payments.json")
