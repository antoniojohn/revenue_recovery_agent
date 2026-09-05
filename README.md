# RecoverX — Agentic Revenue Recovery

Built for **Razorpay's AI Buildathon 2026** (AI Revenue Recovery track).

## The problem

Payments fail all the time — insufficient funds, a declined card, a bank
timeout, an expired card. Most of that money just never comes back,
because there's no system in the loop that actually looks at *why* a
payment failed and does something sensible about it. Someone eventually
notices the revenue is missing, or they don't.

RecoverX is my attempt at that missing piece: an agent that diagnoses
*why* something failed — a payment, an abandoned checkout, an overdue
invoice — and takes a bounded, explainable action per cause, instead of
either doing nothing or retrying blindly forever.

## The core loop

Every pipeline in this repo follows the same four steps:

```
diagnose  →  decide  →  execute  →  report
```

1. **Diagnose** — figure out *why* something went wrong. For payments
   this is a rule lookup first (`insufficient_funds`, `card_declined`,
   `expired_card`, `bank_timeout`, `invalid_cvv`), and for anything the
   rule map doesn't recognize, a Groq LLM call classifies the free-text
   reason instead. If Groq is down or rate-limited, it automatically
   fails over to Gemini via LiteLLM rather than giving up and calling
   everything `UNKNOWN`. Receivables and checkouts don't need this step
   at all — `days_overdue` and `time_since_abandonment_hours` are just
   numbers, so classification there is a straight bucket lookup.
2. **Decide** — turn the cause into a bounded action: retry (with a
   cap), notify the customer, offer a discount, chase harder, or
   escalate to a human. Nothing here is allowed to loop forever or
   spend money it hasn't been told it can spend — see the policy gates
   below.
3. **Execute** — actually do the thing, and write down what happened.
   Every outcome — decision, reasoning, timestamp, result — gets
   appended to an audit log, under a file lock so two things writing
   at once can't quietly stomp on each other (I had this bug, it's
   fixed now, see `test_execute.py::test_concurrent_audit_log_writes_are_not_lost`).
4. **Report** — recovery rate, ₹ recovered, and the honest list of
   everything that *didn't* get recovered. Nothing gets cherry-picked
   out of the exception list.

## What's real and what's simulated (read this part)

I want to be upfront about this because it matters for judging, and
because the report itself never blends the two into one number.

- **Real**: pulling actual failed payments from a connected Razorpay
  test account, the Groq/Gemini classification, the policy gates, and —
  for any record that genuinely came from that test account — the
  whole retry lifecycle is a real async API flow. A retry order gets
  created live (`razorpay_client.attempt_retry`), and the result comes
  back later from an actual signature-verified Razorpay webhook
  (`agent/webhook_server.py`), or, if the webhook never shows up, a
  polling fallback that checks the order directly
  (`agent/reconcile_pending.py`). None of that is a dice roll.
- **Simulated**: for the synthetic demo batch — there's no real prior
  payment behind these, so there's nothing to actually retry — the
  retry/notify/escalate *outcome* is drawn from a fixed success
  probability per action type (`SIMULATED_SUCCESS_RATE` in
  `execute.py`). `report.py` labels every number that comes from this
  bucket as **projected**, separately from `revenue_recovered_real`. A
  deployment running against real, live failed payments end to end
  would have no simulated bucket at all.

Sample real numbers from the last eval run (`logs/eval_report.json`,
300 labeled records):

| Config | Overall accuracy | LLM-path accuracy | Simulated recovery |
|---|---|---|---|
| Rule-only baseline | 85.33% | 0% (never even tries) | 52.0% |
| Rule + Groq fallback | **91.67%** | 43.18% | 58.0% |

That LLM-path number (43.18%) is the honest one — it's the accuracy of
*just* the ambiguous, free-text failure reasons the rule map can't
handle (`issuer_bank_rejected_transaction_temporarily` and friends),
not inflated by the deterministic rule-lookup cases mixed in. It's the
number I'd cite if someone asks "does the LLM fallback actually help,"
not the 91.67% overall figure — that one's flattered by the rule path
being 100% by construction. Cost for those 44 LLM calls: about
$0.00035, on Groq's published `openai/gpt-oss-20b` rate.

## The three pipelines

The diagnose → decide → execute → report shape isn't payments-specific
— I reused it as-is on two structurally different problems, mostly to
prove the pattern generalizes rather than being one-off glue code for
one dataset.

| Pipeline | Classifies by | Action ladder | Policy gate | Audit log |
|---|---|---|---|---|
| **Payments** (`agent/decide.py` / `execute.py`) | Free-text error code (rule + LLM) | Retry (immediate / delayed) → Notify → Escalate | AFA amount cap (hardcoded) + configurable min-retry | `logs/audit_log.json` |
| **Receivables** (`agent/receivables.py`) | `days_overdue` (0–7 / 8–30 / 30+) | Gentle reminder → Firm reminder → Escalate to collections | Min. collections amount (₹10,000) | `logs/receivables_audit_log.json` |
| **Checkouts** (`agent/checkout_recovery.py`) | `time_since_abandonment_hours` (<1 / 1–24 / 24+) | Email nudge → Discount offer → Escalate/drop | Min. discount-eligible cart (₹1,000) | `logs/checkout_audit_log.json` |

Each keeps its own log and its own summary file on purpose — three
different business processes, and mixing their audit trails would make
all three harder to reason about, not easier.

One gate is worth calling out specifically: `AFA_REQUIRED_ABOVE_AMOUNT`
in `decide.py` (₹15,000) is **not** an admin-tunable setting — it's
modeled on NPCI's e-mandate Additional Factor of Authentication rule,
and it's checked *before* the configurable minimum-retry-amount gate on
purpose. A business owner can loosen or tighten how aggressively small
amounts get retried through the admin panel, but nothing in that panel
can accidentally configure its way past a regulatory boundary.

## The async retry lifecycle

This is the part that took the longest to get right, because a real
retry isn't a yes/no you get back immediately — it's "I created an
order, now I wait."

```
choose_action() → attempt_retry() creates an order → status: pending
                                                          │
                                        ┌─────────────────┴─────────────────┐
                                        ▼                                   ▼
                        agent/webhook_server.py                 agent/reconcile_pending.py
                        (Razorpay calls us back,                (polls Razorpay directly —
                         signature-verified)                     belt-and-suspenders, since
                                                                  Razorpay doesn't guarantee
                                                                  webhook delivery)
```

- **`agent/pending_store.py`** tracks in-flight retries by `order_id`
  (not `payment_id` — the retry's own payment ID doesn't exist yet at
  initiation time). `pop_pending()` is an atomic check-and-remove
  specifically so a webhook arriving at the same moment the poll loop
  runs can't both try to resolve the same case — whichever one gets
  there first wins, the other gets `None` and does nothing.
- **`agent/webhook_server.py`** verifies every incoming request against
  `RAZORPAY_WEBHOOK_SECRET` (HMAC-SHA256 over the *raw* request body,
  `hmac.compare_digest` so it doesn't leak timing info) before trusting
  a single byte of it. No secret configured → every request gets
  rejected, on principle. A `payment.captured`/`order.paid` event
  resolves the case as recovered; `payment.failed` resolves it as not
  recovered — a failed retry never gets quietly counted as a win.
- **`agent/reconcile_pending.py`** is the fallback for missed webhooks.
  It polls each pending order and treats `paid` as resolved,
  anything else as still-waiting — unless it's been sitting for more
  than 60 minutes (`PENDING_TIMEOUT_MINUTES`), in which case it
  escalates as an unconfirmed timeout instead of sitting there forever.
  `agent/reconcile_loop.py` wraps this as a long-running service (every
  5 minutes by default) instead of relying on an external cron job.

`report.py` treats `pending` as a genuine third state — not recovered,
not an exception, excluded from the recovery-rate math entirely until
it actually resolves one way or the other.

## Dynamic policy configuration

The two thresholds that most obviously shouldn't need a code change to
adjust — the minimum ₹ amount worth retrying, and the per-cause retry
caps — live in a small SQLite database (`agent/settings_store.py`)
instead of hardcoded constants, and are editable live through
`agent/admin_panel.py` (port 5001, HTTP Basic Auth).

- `decide.choose_action()` re-reads both values from the DB on **every
  call** — an admin change takes effect on the very next decision, no
  restart needed.
- Every change is appended to `logs/settings_audit_log.json` — who
  changed what, old value, new value, when.
- If the settings DB is ever missing or unreadable, every getter falls
  back to the original hardcoded defaults instead of taking the
  pipeline down with it.
- `ACTION_MAP` (which action a cause maps to) deliberately stays a
  hardcoded constant — that's recovery *logic*, not a tunable number.

```powershell
python agent/admin_panel.py
# → http://localhost:5001 (needs ADMIN_USERNAME / ADMIN_PASSWORD in .env)
```

This is intentionally lightweight auth — for anything beyond a demo,
put it behind your cloud provider's VPN/IAM the same way you would any
internal tool.

## The dashboard (`recoverx.html`)

A single-file dashboard — no build step, no framework — that reads
`logs/audit_log.json` directly:

```powershell
python -m http.server 8000   # from the project root
# open http://localhost:8000/recoverx.html
```

If it can fetch the real audit log, it shows real data and flips a
badge to "Live data · N records." If it can't (e.g. you open the file
directly with `file://`, which browsers block from fetching local
JSON), it falls back to a demo-data generator that mirrors
`diagnose.py`/`decide.py`'s logic closely enough that the UI behaves
identically either way — same cause labels, same action ladder, same
policy-gate math. There's also a "Run Recovery Simulation" button that
streams in a few fake transactions live, for demoing without a
connected Razorpay account at all.

## Testing

```powershell
pip install pytest
pytest tests/ -v
```

Coverage, roughly:

- **`test_diagnose.py` / `test_diagnose_fallback.py`** — rule-path
  priority, the LLM fallback's response parsing (including recovering
  from a truncated response like `"CARD_DECL"` → `CARD_DECLINED`), the
  Groq → Gemini failover, and dedup against a prior run's audit log.
- **`test_decide.py`** — the policy gate, including that an
  admin-panel-style settings change takes effect immediately, and that
  the AFA compliance gate can't be overridden by loosening the
  configurable minimum.
- **`test_execute.py`** — the bounded retry loop, live-vs-simulated
  routing, and the pending/resolved-failure split for real retries.
- **`test_pending_store.py`, `test_reconcile_pending.py`,
  `test_webhook_server.py`** — the async lifecycle end to end,
  including the webhook signature check and the timeout escalation.
- **`test_settings_store.py`, `test_admin_panel.py`** — the SQLite
  store's fallback behavior, and a regression test for a stored-XSS fix
  in the admin panel (an admin username gets HTML-escaped before it's
  re-rendered on the settings page — see the cleanup note below,
  though, this fix isn't in every copy of the file floating around).
- **`test_checkout_recovery.py`, `test_receivables.py`,
  `test_razorpay_client.py`, `test_report.py`, `test_reconcile_loop.py`**
  — the other two pipelines' classification/policy logic, the
  Razorpay client's graceful-degradation behavior, and report.py's
  real-vs-simulated/pending-exclusion math.
- Three standalone concurrency scripts (`test_concurrency.py`,
  `test_concurrency_fixed.py`, `test_concurrency_all_pipelines.py`)
  reproduce and then confirm the fix for a genuine bug I hit early on:
  20 threads writing to the audit log without a file lock lost 19 of
  20 writes. They're not pytest files — just run them directly and
  read the printed verdict.

## Evaluation

`scripts/generate_eval_data.py` builds a labeled batch (300 records by
default) where every record carries a `ground_truth` field the
pipeline itself never sees. `scripts/evaluate.py` runs the same batch
through a rule-only baseline and a rule+LLM config with an identical
RNG seed, so any difference in simulated recovery rate is attributable
to classification accuracy, not random noise. It reports accuracy
overall, by classification source, by cause, and an estimated LLM cost
— see the numbers table above. Output goes to `logs/eval_report.json`.

## Deployment

```powershell
docker compose up -d --build      # webhook + admin panel + reconcile loop
docker compose run --rm pipeline  # the batch pipeline is one-shot, run on demand
```

| Service | Runs | Port |
|---|---|---|
| `webhook` | `agent/webhook_server.py` (gunicorn) | 5000 |
| `admin` | `agent/admin_panel.py` (gunicorn) | 5001 |
| `reconcile` | `agent/reconcile_loop.py` (polls forever) | — |
| `pipeline` | `app.py` (one-shot, `profiles: [manual]`) | — |

`logs/`, `data/`, and `instance/` (the SQLite settings DB) are all
bind-mounted, so a `docker compose down && up -d` doesn't wipe your
audit history or reset your policy thresholds.

## Setup

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
python -m venv venv311
.\venv311\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
# set GROQ_API_KEY (free at console.groq.com), and optionally
# GEMINI_API_KEY, RAZORPAY_KEY_ID/SECRET, RAZORPAY_WEBHOOK_SECRET,
# ADMIN_USERNAME/PASSWORD

python scripts/generate_data.py   # optional, a sample batch is already included
python app.py
```

## Housekeeping notes (for me, mostly)

Since I'm still actively working on this, a few things worth fixing
before this goes any further, based on what's actually sitting in the
project right now:

- **There are two versions of `admin_panel.py` and `settings_store.py`**
  floating around — one under `agent/` with the security hardening
  (`hmac.compare_digest` for the auth check, HTML-escaping the
  `updated_by` field to close a stored-XSS hole, `FileLock` around the
  settings audit log) and one at the project root that's missing all
  three. `test_admin_panel.py`'s XSS regression test only actually
  proves anything against the hardened copy. Worth deleting the stale
  one so there's no chance of the old copy being what actually ships.
- **Backup/scratch files should move out of the repo root** —
  `audit_log_backup.json`, `audit_log_backup_before_reset.json`,
  `register_pending.py` (a one-off script I used to manually register
  a pending retry for a webhook test), and the `checkout_test*.html` /
  `razorpay_test_checkout.html` pages (manual test-mode checkout pages
  for triggering specific failure reasons) were all genuinely useful
  while wiring up the live Razorpay integration, but they're dev
  scratch space, not part of the deliverable — a `scripts/manual_test/`
  or `.gitignore`'d folder would keep the root clean.
- **The three concurrency scripts overlap** — `test_concurrency.py`
  proves the bug existed, `test_concurrency_fixed.py` proves the fix
  for `execute.py`, `test_concurrency_all_pipelines.py` proves the same
  fix for the other two pipelines. All three are genuinely useful as
  a record, but only the "fixed" ones need to run again if I touch this
  code — the buggy one is there to document what the bug looked like,
  not to be re-run.
- **`webhook_server.log`** is a captured terminal session from one
  local test run (PowerShell's `Tee-Object`) — fine to keep as
  evidence a real webhook round-trip happened, but it's a one-off
  artifact, not something that needs to be regenerated or committed
  going forward.

None of this affects correctness of the pipeline itself — it's tidiness,
not bugs — but it's the kind of thing worth cleaning up before calling
this "done."

## Status

Core pipeline complete and working end to end: rule + Groq/Gemini LLM
classification, all three recovery pipelines, the real async
webhook/poll retry lifecycle, dynamic runtime-configurable policy
thresholds, a working dashboard, and containerized deployment. Built
for the Razorpay AI Buildathon (deadline: 5 Sept 2026).

## Author

Antonio John — [github.com/antoniojohn](https://github.com/antoniojohn) · [LinkedIn](https://linkedin.com/in/antonio-john-b57014307)
