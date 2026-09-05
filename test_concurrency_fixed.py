import json
import os
import threading
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import execute

TEST_LOG_PATH = "logs/concurrency_test_log2.json"
NUM_THREADS = 20

execute.AUDIT_LOG_PATH = TEST_LOG_PATH
if os.path.exists(TEST_LOG_PATH):
    os.remove(TEST_LOG_PATH)
if os.path.exists(TEST_LOG_PATH + ".lock"):
    os.remove(TEST_LOG_PATH + ".lock")

def worker(i):
    execute.append_to_audit_log({"payment_id": "pay_concurrent_" + str(i), "status": "resolved"})

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
    print("RESULT: RACE CONDITION STILL PRESENT.")
else:
    print("RESULT: FIX CONFIRMED - zero writes lost under concurrency.")
