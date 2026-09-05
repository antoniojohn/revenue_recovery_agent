"""
Long-running wrapper around reconcile_pending.reconcile_once(), for use
as its own container service instead of an external cron job.

Run: python agent/reconcile_loop.py
Or, in Docker: see docker-compose.yml (service: reconcile).
"""

import os
import sys
import time

from agent import reconcile_pending

INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "300"))


def main():
    print(f"[reconcile_loop] Starting - polling every {INTERVAL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                summary = reconcile_pending.reconcile_once()
                print(
                    f"[reconcile_loop] checked={summary['checked']} "
                    f"resolved_paid={summary['resolved_paid']} "
                    f"resolved_timeout={summary['resolved_timeout']} "
                    f"still_pending={summary['still_pending']}"
                )
            except Exception as e:
                # A single bad poll cycle must not kill the long-running
                # service - log it and try again next interval.
                print(f"[reconcile_loop] Error during reconcile pass: {e}")
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        # KeyboardInterrupt does not subclass Exception, so it passes
        # through the inner except untouched and lands here for a
        # clean shutdown message instead of a raw traceback.
        print("\n[reconcile_loop] Shutting down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())