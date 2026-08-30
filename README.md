# Revenue Recovery Agent

Built for the Razorpay AI Buildathon — **AI Revenue Recovery** track.

## Problem

Payments fail for many reasons — declined cards, insufficient funds, expired
cards, bank timeouts — and most of that revenue is never recovered because
there's no system that diagnoses *why* a payment failed and takes a bounded,
appropriate recovery action per cause.

## What this agent does

1. **Ingests** a batch of failed payment records (Razorpay test-mode + synthetic data).
2. **Diagnoses** the failure cause using a hybrid pipeline: rule-based
   classification first, LLM fallback for ambiguous cases.
3. **Decides** a bounded recovery action per cause (capped retries, no
   infinite loops — every action is explainable).
4. **Executes** the action and logs a full audit trail (decision, reasoning,
   timestamp, outcome).
5. **Reports** recovery rate, ₹ amount recovered, and an honest list of
   unresolved exceptions.

## Architecture

```
data/failed_payments.json
        │
        ▼
agent/diagnose.py   ──►  cause bucket (rule-based → LLM fallback)
        │
        ▼
agent/decide.py     ──►  bounded action (retry / notify / escalate)
        │
        ▼
agent/execute.py    ──►  runs action, writes audit log
        │
        ▼
agent/report.py     ──►  recovery %, ₹ recovered, exception list
```

## Setup

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
python -m venv venv311
.\venv311\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Status

🚧 In active development for the Razorpay AI Buildathon (deadline: 5 Sept 2026).

## Author

Antonio John — [github.com/antoniojohn](https://github.com/antoniojohn) · [LinkedIn](https://linkedin.com/in/antonio-john-b57014307)
