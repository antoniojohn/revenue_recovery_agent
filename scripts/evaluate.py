"""
Evaluation harness: rule-only baseline vs. rule+LLM-fallback, on a
labeled batch (data/eval_batch.json, from generate_eval_data.py).

Reports:
  1. Classification accuracy per config, and accuracy of the LLM path
     specifically (the rule path is deterministic, so its accuracy is
     always 100% on records with a known error_code — the number worth
     reporting is whether the LLM fallback gets the ambiguous ones right).
  2. A/B recovery comparison: same records run through decide.py +
     execute.py under both configs, with the RNG seeded identically per
     config so any difference in recovery rate is attributable to
     classification (rule-only misses => ESCALATE => 0% recovery),
     not to random noise.
  3. Cost-efficiency: how many LLM calls the fallback actually needed
     (i.e. how much the rule-based fast path saves), plus an estimated
     $ cost using Groq's published openai/gpt-oss-20b rate
     ($0.075 / 1M input tokens, $0.30 / 1M output tokens as of mid-2026
     — check console.groq.com/docs/model/openai/gpt-oss-20b for current
     pricing before citing this number anywhere final).

Run: python scripts/evaluate.py [path-to-eval-batch]
Writes: logs/eval_report.json
"""

import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import diagnose, decide, execute

GROQ_INPUT_COST_PER_M = 0.075   # $ per 1M input tokens, openai/gpt-oss-20b on Groq
GROQ_OUTPUT_COST_PER_M = 0.30   # $ per 1M output tokens
EST_INPUT_TOKENS_PER_CALL = 90   # rough: fixed prompt template + one error_code string
EST_OUTPUT_TOKENS_PER_CALL = 4   # a single category word


def _strip_ground_truth(record: dict) -> dict:
    r = dict(record)
    r.pop("ground_truth", None)
    return r


def run_config(records: list[dict], use_llm: bool, rng_seed: int) -> dict:
    """Classify + decide + (bounded, simulated) execute every record under
    one configuration. Returns per-record results plus rollups."""
    random.seed(rng_seed)

    original_classify_with_llm = diagnose.classify_with_llm
    llm_calls = 0

    if not use_llm:
        # Rule-only baseline: force every non-rule-mapped record to
        # UNKNOWN, exactly like diagnose.py already does when no
        # GROQ_API_KEY is configured — this *is* the no-LLM code path,
        # not a separate simulation of it.
        diagnose.classify_with_llm = lambda record: "UNKNOWN"
    else:
        def counting_wrapper(record):
            nonlocal llm_calls
            llm_calls += 1
            return original_classify_with_llm(record)
        diagnose.classify_with_llm = counting_wrapper

    per_record = []
    try:
        for gt_record in records:
            clean = _strip_ground_truth(gt_record)
            cause, source = diagnose.classify(clean)
            action = decide.choose_action(cause, clean, source)
            outcome = _simulate_execute_no_log(action, clean)

            per_record.append({
                "payment_id": gt_record["payment_id"],
                "ground_truth": gt_record["ground_truth"],
                "predicted": cause,
                "correct": cause == gt_record["ground_truth"],
                "source": source,
                "action_type": action["type"],
                "recovered": outcome["recovered"],
                "amount": gt_record["amount"],
            })
    finally:
        diagnose.classify_with_llm = original_classify_with_llm

    return {
        "config": "rule_plus_llm" if use_llm else "rule_only_baseline",
        "llm_calls_made": llm_calls,
        "records": per_record,
    }


def _simulate_execute_no_log(action: dict, record: dict) -> dict:
    """Same bounded-attempt logic as execute.run_action, without writing
    to the shared audit log (eval runs shouldn't pollute production logs)
    and without live Razorpay calls (eval batch is synthetic)."""
    success_rate = execute.SIMULATED_SUCCESS_RATE.get(action["type"], 0.0)
    max_attempts = action["max_attempts"]
    recovered = False
    for _ in range(max_attempts):
        if random.random() < success_rate:
            recovered = True
            break
    return {"recovered": recovered}


def accuracy_table(result: dict) -> dict:
    records = result["records"]
    total = len(records)
    correct = sum(1 for r in records if r["correct"])

    by_source = {}
    for r in records:
        s = by_source.setdefault(r["source"], {"total": 0, "correct": 0})
        s["total"] += 1
        s["correct"] += int(r["correct"])

    per_cause = {}
    for r in records:
        c = per_cause.setdefault(r["ground_truth"], {"total": 0, "correct": 0})
        c["total"] += 1
        c["correct"] += int(r["correct"])

    return {
        "config": result["config"],
        "overall_accuracy_pct": round(100 * correct / total, 2) if total else 0.0,
        "accuracy_by_source": {
            k: round(100 * v["correct"] / v["total"], 2) for k, v in by_source.items()
        },
        "accuracy_by_ground_truth_cause": {
            k: round(100 * v["correct"] / v["total"], 2) for k, v in per_cause.items()
        },
        "recovery_rate_pct": round(
            100 * sum(1 for r in records if r["recovered"]) / total, 2
        ) if total else 0.0,
        "recovered_amount": sum(r["amount"] for r in records if r["recovered"]),
    }


def cost_table(result: dict) -> dict:
    calls = result["llm_calls_made"]
    input_cost = calls * EST_INPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_INPUT_COST_PER_M
    output_cost = calls * EST_OUTPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_OUTPUT_COST_PER_M
    return {
        "llm_calls_made": calls,
        "est_total_tokens": calls * (EST_INPUT_TOKENS_PER_CALL + EST_OUTPUT_TOKENS_PER_CALL),
        "est_cost_usd": round(input_cost + output_cost, 6),
        "note": (
            "Estimated from Groq's published openai/gpt-oss-20b rate "
            f"(${GROQ_INPUT_COST_PER_M}/1M input, ${GROQ_OUTPUT_COST_PER_M}/1M output) "
            "and a rough per-call token count, not measured usage. Verify the rate "
            "at console.groq.com/docs/model/openai/gpt-oss-20b before citing."
        ),
    }


def main():
    batch_path = sys.argv[1] if len(sys.argv) > 1 else "data/eval_batch.json"
    with open(batch_path) as f:
        records = json.load(f)

    has_key = bool(os.getenv("GROQ_API_KEY"))
    if not has_key:
        print(
            "[evaluate] WARNING: GROQ_API_KEY not set. The 'rule_plus_llm' arm "
            "below will fall back to UNKNOWN for every ambiguous record, same as "
            "the baseline — it will NOT reflect real LLM accuracy. Set the key "
            "and re-run before putting these numbers in the submission.\n"
        )

    baseline = run_config(copy.deepcopy(records), use_llm=False, rng_seed=42)
    with_llm = run_config(copy.deepcopy(records), use_llm=True, rng_seed=42)

    report = {
        "eval_batch": batch_path,
        "n_records": len(records),
        "groq_api_key_configured": has_key,
        "accuracy": {
            "rule_only_baseline": accuracy_table(baseline),
            "rule_plus_llm": accuracy_table(with_llm),
        },
        "cost_efficiency": {
            "rule_only_baseline": cost_table(baseline),
            "rule_plus_llm": cost_table(with_llm),
        },
    }

    os.makedirs("logs", exist_ok=True)
    with open("logs/eval_report.json", "w") as f:
        json.dump(report, f, indent=2)

    _print_report(report)


def _print_report(report: dict) -> None:
    print(f"=== Eval Report ({report['n_records']} records) ===\n")
    for key in ("rule_only_baseline", "rule_plus_llm"):
        acc = report["accuracy"][key]
        cost = report["cost_efficiency"][key]
        print(f"-- {key} --")
        print(f"  Overall accuracy:      {acc['overall_accuracy_pct']}%")
        print(f"  Accuracy by source:    {acc['accuracy_by_source']}")
        print(f"  Accuracy by cause:     {acc['accuracy_by_ground_truth_cause']}")
        print(f"  Simulated recovery:    {acc['recovery_rate_pct']}% "
              f"(₹{acc['recovered_amount']:,})")
        print(f"  LLM calls made:        {cost['llm_calls_made']}")
        print(f"  Est. LLM cost:         ${cost['est_cost_usd']}")
        print()
    print("Full report -> logs/eval_report.json")


if __name__ == "__main__":
    main()
