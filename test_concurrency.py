import json
import os
import threading
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import execute

TEST_LOG_PATH = "logs/concurrency_test_log.json"
NUM_THREADS = 20

execute.AUDIT_LOG_PATH = TEST_LOG_PATH
if os.path.exists(TEST_LOG_PATH):
    os.remove(TEST_LOG_PATH)

def _racy_append(outcome):
    try:
        with open(execute.AUDIT_LOG_PATH, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []

    log.append(outcome)

    time.sleep(0.05)

    os.makedirs(os.path.dirname(execute.AUDIT_LOG_PATH) or ".", exist_ok=True)
    with open(execute.AUDIT_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)

def worker(i):
    _racy_append({"payment_id": "pay_concurrent_" + str(i), "status": "resolved"})

threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

with open(TEST_LOG_PATH) as f:
    final_log = json.load(f)

expected_ids = set("pay_concurrent_" + str(i) for i in range(NUM_THREADS))
actual_ids = set(entry["payment_id"] for entry in final_log)
lost = expected_ids - actual_ids

print("Attempted writes: " + str(NUM_THREADS))
print("Entries actually in log: " + str(len(final_log)))
print("Lost writes: " + str(len(lost)))
if lost:
    print("Lost payment_ids: " + str(sorted(lost)))
    print("RESULT: RACE CONDITION CONFIRMED - writes were silently lost.")
else:
    print("RESULT: No writes lost in this run (race conditions can be intermittent - rerun to confirm).")
