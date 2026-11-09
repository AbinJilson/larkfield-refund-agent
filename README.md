# Larkfield Home ticket desk

A FastAPI service that takes one customer-support email, normalizes it, classifies the request, extracts the order/item fields with a model, resolves them against the prepared data room, runs policy checks, and returns the desk's decision in the required `TicketResponse` contract. The service never refunds, sends or changes anything: it proposes a run, records the actions in a ledger, and stops for a person when the policy requires approval, escalation or a fact it cannot verify. The only decision source is `data_room/policies/refund_policy_RP-3.2.md` (v3.2, effective 2026-07-01).

**Live:** https://larkfield-ticket-desk.fly.dev (`/health`, `POST /tickets`, `/ledger`; see Deploy below).

## Pipeline

```
normalize -> classify (llm | decider) -> extract -> resolve -> checks -> decide -> reply + persist
```

`src/decide.py` owns the ordered rule list (first match wins). The models only fill the internal `Extraction`; they never choose an outcome, reason code or approver. A check that cannot complete is `UNABLE_TO_VERIFY`, never `PASS`. Money stays `Decimal` until the schema boundary. The ledger is idempotent: one `runs` row per `ticket_id`, one REPLY row per run, a REFUND row when money is proposed, and an ESCALATION row for `ESCALATE`.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENROUTER_API_KEY`, `EXTRACT_MODEL` and `CLASSIFIER=llm` in `.env`. `.env` is ignored and must never be committed. The recommended mode is `llm`; `CLASSIFIER=decider` is also supported. `CLASSIFY_PROMPT_VERSION` defaults to `v2`; `v1` reproduces the original classifier prompt for the before/after eval.

```bash
python -m src.prepare      # reproducible derived tables + prepared/prepare_report.json
python -m pytest           # 123 tests
python -m src.solution     # uvicorn on $PORT (default 8000)
```

## Dataset

The course data room is **not included in this repository** for confidentiality. Place it at `data_room/` in the repository root before preparing or running anything data-dependent. Expected layout:

```
data_room/
  data/          customers.csv, orders.csv, order_items.csv, shipments.csv, refunds.csv
  policies/      refund_policy_RP-3.2.md, help_centre_faq_v2.md, warehouse_picking_WH-PICK-1.4.md
  tickets/       support_inbox_export.json, eval_seed_labelled.json
  inbox/         public_tickets.json
  schema/        response_schema.py
  DATA_DICTIONARY.md
```

`data_room/` is read-only and git-ignored. `python -m src.prepare` reads it and builds `prepared/` plus `prepared/prepare_report.json`; `prepared/` is also ignored. Without the data room, the pure unit tests still run and the data-dependent tests skip with an instruction to add it and run the preparation command.

## API

All request and response bodies are the models in `data_room/schema/response_schema.py`.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness and database check |
| `POST /tickets` | Run one ticket |
| `GET /runs/{run_id}` | Current run + its ledger rows |
| `POST /runs/{run_id}/decision` | Approve or reject a PEND run |
| `GET /ledger` | All ledger rows, newest first |
| `GET /ledger?ticket_id=...` | Ledger rows for one ticket |

```bash
# health
curl -s http://localhost:8000/health

# create a run
curl -s -X POST http://localhost:8000/tickets \
  -H 'content-type: application/json' \
  -d '{"ticket_id":"demo-1","received_at":"2026-11-09T10:00:00+00:00","channel":"email","from":"customer@example.com","to":"support@larkfield.example","subject":"Damaged lamp","body":"My Arc floor lamp from order LF100201 arrived damaged. I would like a refund.","attachments":[]}'

# read a run
curl -s http://localhost:8000/runs/<run_id>

# approve the PEND run
curl -s -X POST http://localhost:8000/runs/<run_id>/decision \
  -H 'content-type: application/json' \
  -d '{"decision":"approve","approver":"Asha Shah"}'

# read the ledger for a ticket
curl -s 'http://localhost:8000/ledger?ticket_id=demo-1'

# read every ledger row, newest first
curl -s 'http://localhost:8000/ledger'
```

### Response shapes added by this service

No fields were added to `TicketResponse` itself. These response shapes are service additions, documented here as required:

- `GET /runs/{run_id}` and `POST /runs/{run_id}/decision` return `{"run": <TicketResponse>, "ledger": [<LedgerEntry>]}`.
- `GET /ledger` returns `[<LedgerEntry>]`. `ticket_id` is optional; without it the route returns every row, newest first, and with it the route returns that ticket's rows.
- `GET /health` returns `{"status": "ok", "database": "ok", "classify_prompt_version": "v1"|"v2"}` when the database is reachable, and `503` otherwise. The prompt version is the classifier prompt in use.
- The internal `ledger` table also stores `order_id`, `line_no` and `detail_json` for the RP-3.2 2.5 deduction. Those columns are not returned; they are not part of the contract.

## Evaluation

The classifier prompt is selectable with `CLASSIFY_PROMPT_VERSION=v1|v2` (default `v2`). `v1` is the original option text; `v2` is the sharpened status/non-delivery and return/refund wording. Start the service on a fresh database for each run, then point the eval at it:

```bash
# run 1: v1 prompt, fresh SQLite, decider mode
$env:DATABASE_URL = "sqlite:///eval_v1.db"       # PowerShell
$env:CLASSIFIER = "decider"
$env:CLASSIFY_PROMPT_VERSION = "v1"
$env:PORT = "8000"
python -m src.solution

# in another shell
$env:EVAL_BASE_URL = "http://127.0.0.1:8000"
$env:CLASSIFY_PROMPT_VERSION = "v1"
python -m eval.run_eval

# run 2: stop the service, use a new database file, CLASSIFY_PROMPT_VERSION=v2,
# and run the same two commands again.
```

The eval posts all 40 data-room tickets in `received_at` order, validates every response and ledger row, compares the 15 human labels and the 40-row gold decisions table, repeats all 40 posts to prove idempotency, then approves every PEND run twice and checks the conflicting reject returns `409`. Each run writes `eval/results/run_<timestamp>.json`; `eval/results/summary.md` is regenerated and compares every committed run side by side on labels, gold, rule-13 fallbacks, cost and latency.

Headline results from the two committed live before/after runs (`CLASSIFIER=decider`, `MODEL_CACHE_DISABLED=1`, fresh SQLite). In the default `llm` mode both prompt versions score 15/15 labels, 40/40 gold and 0 fallbacks.

| Check | v1 (`run_20261109T151707Z.json`) | v2 (`run_20261109T152744Z.json`) |
|---|---:|---:|
| 15 human labels | 13/15 | 15/15 |
| 40 gold decisions | 33/40 | 39/40 |
| Ledger-dependent matches (T027/T028, ruling 1) | 2 | 2 |
| Rule-13 fallbacks | 7 | 1 (T010) |
| 6 hand-written negative cases | 6/6 | 6/6 |
| Tokens | 30,867 input + 5,785 output | 32,937 input + 5,784 output |
| List-price cost | $0.02372260 total | $0.02434110 total |
| Median cost per request | $0.00052330 | $0.00053680 |
| Latency | p50 2,820.86 ms, p95 3,600.63 ms | p50 2,469.44 ms, p95 3,479.45 ms |
| Idempotency / approvals | all pass | all pass |

See `APPROACH.md` for the architecture and `ARTEFACT.md` for the data-preparation and evaluation detail.
