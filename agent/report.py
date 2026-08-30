"""
Reporting layer: takes the list of outcomes produced by execute.py and
summarizes them into the metrics that matter for revenue recovery:
recovery rate, total amount recovered, and an honest list of cases that
were NOT recovered (exceptions), broken down by cause.

Deliberately does not hide or cherry-pick failures — the exception list
is meant to be reported as-is.
"""

import json


def summarize(results: list[dict]) -> dict:
    total_cases = len(results)
    recovered_cases = [r for r in results if r["recovered"]]
    exception_cases = [r for r in results if not r["recovered"]]

    total_amount = sum(r["amount"] for r in results)
    recovered_amount = sum(r["amount"] for r in recovered_cases)

    recovery_rate = (len(recovered_cases) / total_cases * 100) if total_cases else 0.0

    exceptions_by_cause: dict[str, int] = {}
    for r in exception_cases:
        exceptions_by_cause[r["cause"]] = exceptions_by_cause.get(r["cause"], 0) + 1

    summary = {
        "total_cases": total_cases,
        "recovered_cases": len(recovered_cases),
        "exception_cases": len(exception_cases),
        "recovery_rate_percent": round(recovery_rate, 2),
        "total_amount": total_amount,
        "recovered_amount": recovered_amount,
        "unrecovered_amount": total_amount - recovered_amount,
        "exceptions_by_cause": exceptions_by_cause,
        "exception_list": [
            {
                "payment_id": r["payment_id"],
                "amount": r["amount"],
                "cause": r["cause"],
                "action_type": r["action_type"],
                "reasoning": r["reasoning"],
            }
            for r in exception_cases
        ],
    }

    _print_summary(summary)
    _write_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    print("\n=== Revenue Recovery Report ===")
    print(f"Total failed payments processed: {summary['total_cases']}")
    print(f"Recovered: {summary['recovered_cases']} ({summary['recovery_rate_percent']}%)")
    print(f"Exceptions (unresolved): {summary['exception_cases']}")
    print(f"Amount recovered: ₹{summary['recovered_amount']:,}")
    print(f"Amount still at risk: ₹{summary['unrecovered_amount']:,}")
    print("\nExceptions by cause:")
    for cause, count in summary["exceptions_by_cause"].items():
        print(f"  {cause}: {count}")
    print("================================\n")


def _write_summary(summary: dict, path: str = "logs/summary_report.json") -> None:
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
