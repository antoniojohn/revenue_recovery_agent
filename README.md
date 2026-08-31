# Revenue Recovery Agent

Built as a submission for **Razorpay's AI Buildathon 2026** — AI Revenue Recovery track.

## Problem

Payments fail for many reasons — declined cards, insufficient funds, expired
cards, bank timeouts — and most of that revenue is never recovered because
there's no system that diagnoses *why* a payment failed and takes a bounded,
appropriate recovery action per cause.

## What this agent does

1. **Ingests** a batch of failed payment records (synthetic data, modeled on
   real-world Razorpay failure reasons).
2. **Diagnoses** the failure cause using a hybrid pipeline: rule-based
   classification for known error codes, Groq LLM fallback for ambiguous or
   free-text failure reasons.
3. **Decides** a bounded recovery action per cause (capped retries, no
   infinite loops — every action is explainable).
4. **Executes** the action via a bounded retry loop (stops on success or
   at the cap, whichever comes first) and logs a full audit trail
   (decision, reasoning, timestamp, outcome) to `logs/audit_log.json`.
5. **Reports** recovery rate, ₹ amount recovered, and an honest list of
   unresolved exceptions — nothing cherry-picked.

## What's real vs. simulated

Being explicit about this, since it matters for judging:

- **Real**: Razorpay test-mode payment ingestion (`razorpay_client.py`),
  Groq LLM classification for ambiguous failure reasons, the policy gate
  logic, and — for any payment that came from the real Razorpay test
  account specifically — a genuine follow-up API call to verify that
  payment's live status (`razorpay_client.check_payment_status`). Occasionally the LLM fallback returns `UNKNOWN` when no category fits — this is handled explicitly, not treated as an error. Re-running the pipeline is safe: `diagnose.py` checks `logs/audit_log.json` and skips any payment_id (real or synthetic) already processed in a prior run, so the audit trail and reported metrics never double-count a case.
- **Simulated**: retry/notify/escalate *outcomes* (`SIMULATED_SUCCESS_RATE`
  in `execute.py`) are drawn from a fixed probability per action type,
  not a live payment-retry API call — Razorpay's test mode has no
  endpoint to actually re-attempt a failed payment. A production
  integration would replace this with a real retry/notification call.

## Architecture

![Architecture diagram](docs/architecture.png)

<details>
<summary>Text version</summary>

```
Razorpay test account          data/failed_payments.json
(real failed payments)         (synthetic batch)
        │                              │
        └──────────────┬───────────────┘
                        ▼
        agent/diagnose.py  ──► dedup against logs/audit_log.json
                                (skips already-processed real AND
                                synthetic payment_ids from prior runs)
                        │
                        ▼
        agent/diagnose.py  ──► cause bucket (rule-based → Groq LLM fallback)
                        │
                        ▼
        agent/decide.py    ──► bounded action (retry / notify / escalate)
                                + policy gate (rejects sub-₹150 retries)
                        │
                        ▼
        agent/execute.py   ──► bounded retry loop: up to max_attempts
                                tries, stops the instant one succeeds
                                (real records also get a genuine
                                Razorpay API call to verify live status)
                        │
                        ▼
        agent/execute.py   ──► writes full audit trail to
                                logs/audit_log.json
                        │
                        ▼
        agent/report.py    ──► recovery %, ₹ recovered, exception list
                                → logs/summary_report.json
```
</details>

## Setup

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
python -m venv venv311
.\venv311\Scripts\Activate.ps1
pip install -r requirements.txt

# Add your Groq API key (get one free at console.groq.com)
Copy-Item .env.example .env
# then edit .env and set GROQ_API_KEY=your_key_here

# Generate a fresh synthetic batch (optional - a sample batch is included)
python scripts/generate_data.py

python app.py
```

## Sample output

```
=== Revenue Recovery Report ===
Total failed payments processed: 75
Recovered: 36 (48.0%)
Exceptions (unresolved): 39
Amount recovered: ₹140,064
Amount still at risk: ₹83,661

Exceptions by cause:
  EXPIRED_CARD: 11
  INVALID_CVV: 5
  CARD_DECLINED: 12
  INSUFFICIENT_FUNDS: 7
  BANK_TIMEOUT: 4
================================
```

Full per-record audit trail is written to `logs/audit_log.json`, and the
same summary is written to `logs/summary_report.json`.

## Testing

```powershell
pip install pytest
pytest tests/ -v
```

Covers the policy gate's core behavior: rejecting low-amount retries,
approving normal ones, the exact threshold boundary, and that non-retry
causes and unknown causes are unaffected.

## Status

✅ Core pipeline complete and working end-to-end, including the Groq LLM
fallback for ambiguous failure reasons. Built for the Razorpay AI Buildathon
(deadline: 5 Sept 2026).

## Author

Antonio John — [github.com/antoniojohn](https://github.com/antoniojohn) · [LinkedIn](https://linkedin.com/in/antonio-john-b57014307)
