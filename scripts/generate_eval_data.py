"""
Generates a LABELED evaluation batch for the rule-vs-LLM accuracy report.

This is separate from generate_data.py (which makes the unlabeled demo
batch app.py runs against). Here every record carries a `ground_truth`
field the pipeline never sees — diagnose.classify() is called on a
stripped copy — so the eval script can score its own output.

Ground-truth mapping for the five known error codes is exact (it's the
same map diagnose.RULE_MAP uses, so those records are just a sanity
check on the rule path). Ground truth for the three ambiguous/free-text
reasons is a judgment call, documented here so it's defensible rather
than arbitrary:

  issuer_bank_rejected_transaction_temporarily -> BANK_TIMEOUT
      "temporarily" + issuer-side rejection reads as a transient bank-side
      failure, the same recovery semantics as a timeout.
  customer_bank_server_not_responding          -> BANK_TIMEOUT
      literally a non-responding server -> timeout by definition.
  card_limit_exceeded_for_the_day              -> INSUFFICIENT_FUNDS
      functionally the same recovery story as insufficient funds: the
      card can't cover it *right now*, retrying later is reasonable.

Run: python scripts/generate_eval_data.py [n]  (default n=300)
Writes: data/eval_batch.json
"""

import json
import random
import sys

ERROR_CODES = [
    "insufficient_funds",
    "card_declined",
    "expired_card",
    "bank_timeout",
    "invalid_cvv",
    "issuer_bank_rejected_transaction_temporarily",
    "customer_bank_server_not_responding",
    "card_limit_exceeded_for_the_day",
]

WEIGHTS = [0.30, 0.26, 0.13, 0.10, 0.07, 0.05, 0.05, 0.04]

GROUND_TRUTH = {
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card_declined": "CARD_DECLINED",
    "expired_card": "EXPIRED_CARD",
    "bank_timeout": "BANK_TIMEOUT",
    "invalid_cvv": "INVALID_CVV",
    "issuer_bank_rejected_transaction_temporarily": "BANK_TIMEOUT",
    "customer_bank_server_not_responding": "BANK_TIMEOUT",
    "card_limit_exceeded_for_the_day": "INSUFFICIENT_FUNDS",
}

AMOUNTS = [199, 499, 999, 1499, 2499, 4999, 9999]


def generate_eval_batch(n: int = 300, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    batch = []
    for i in range(1, n + 1):
        error_code = rng.choices(ERROR_CODES, weights=WEIGHTS, k=1)[0]
        amount = rng.choice(AMOUNTS)
        batch.append({
            "payment_id": f"pay_eval_{i:05d}",
            "amount": amount,
            "currency": "INR",
            "error_code": error_code,
            "customer_id": f"cust_{rng.randint(1000, 9999)}",
            "ground_truth": GROUND_TRUTH[error_code],
        })
    return batch


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    data = generate_eval_batch(n)
    with open("data/eval_batch.json", "w") as f:
        json.dump(data, f, indent=2)
    dist = {}
    for r in data:
        dist[r["ground_truth"]] = dist.get(r["ground_truth"], 0) + 1
    print(f"Generated {len(data)} labeled eval records -> data/eval_batch.json")
    print("Ground-truth distribution:", dist)
