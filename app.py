"""
Revenue Recovery Agent — entry point.
Run with: python app.py
"""
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, module="litellm")

from agent import diagnose, decide, execute, report


def run_pipeline(batch_path: str = "data/failed_payments.json"):
    batch = diagnose.load_batch(batch_path)

    results = []
    for record in batch:
        cause, source = diagnose.classify(record)
        action = decide.choose_action(cause, record, source)
        outcome = execute.run_action(action, record)
        results.append(outcome)

    report.summarize(results)


if __name__ == "__main__":
    run_pipeline()
