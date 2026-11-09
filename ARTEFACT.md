# Artefact

## Scenario and task

Larkfield Home is a US online retailer of furniture and home goods. Customer Care answers about 1,200 emails a day and cannot keep pace: answers are inconsistent, some actions go through that should not, and some requests are lost. The Head of Customer Care wants a service that handles these emails, can be trusted, stops for a person when it should, and shows what it costs.

The service takes one support email as it arrives and returns the desk's decision in the `TicketResponse` contract from `data_room/schema/response_schema.py`. It proposes and records actions; it never refunds, sends or changes anything itself. A check it cannot complete is never a `PASS`. The working service is `src/app.py`; the only decision source is RP-3.2 v3.2.

## Preparing the data

The course data room is **not included in this repository** for confidentiality. It belongs at `data_room/` in the repository root, with `data/` (customers, orders, order items, shipments, issued refunds), `policies/`, `tickets/`, `inbox/`, `schema/` and `DATA_DICTIONARY.md`. `python -m src.prepare` reads that read-only export and builds `prepared/` plus `prepared/prepare_report.json`; both `data_room/` and `prepared/` are git-ignored. Without the data room, the pure unit tests still run and the data-dependent tests skip with the preparation instruction.

The export had 53 customers, 49 orders, 57 order items, 47 shipments and 3 issued refunds.

What preparation found and did:

- Renamed the one column with a space (`Order Total` -> `order_total`); no cell needed whitespace trimming.
- Dropped 2 exact duplicate order rows (LF-100216, LF-100239), leaving 47 orders. No customer merge, order-id rewrite or referential-integrity failure.
- Converted 13 DD/MM dates to ISO-8601 UTC. Three dates were ambiguous (`08/10/2026` on LF-100221 and LF-100235, `06/10/2026` on LF-100230); they are logged in `prepare_report.json`. `order_date` drives no rule, so the two order-before-ship inversions (LF-100221, LF-100240) are data notes only.
- Stripped `$`/`,` from 31 `order_total` cells and parsed money as `Decimal`; 0 parse failures, 0 non-whole quantities.
- Found 3 shipments with carrier status `UNAVAILABLE` (SHP-5012, SHP-5032, SHP-5038) and 0 orders with multiple shipments. The carrier feed wins over the raw order label; there are 0 true delivery-state mismatches.
- Found 3 ISSUED refund lines, 0 multiple refunds per line, 0 over-refunded lines and 0 unmatched refund lines.
- Counted the ticket files without editing them: 40 tickets, 8 sender addresses needing trim/lowercase and 25 order-id mentions not already in `LF-######` form. Normalization happens at runtime in `src/normalize.py`.

The full clean-up counts are in `prepared/prepare_report.json`; `prepared/` is regenerated, git-ignored, and never edits `data_room/`.

## Policy used

RP-3.2 Customer Refund Policy v3.2 (effective 2026-07-01) is the only decision source. The Help Centre FAQ v2 is superseded (it conflicts on the window, auto limit and tracking) and WH-PICK-1.4 is out of scope by its own Scope section. Clause IDs in `src/decide.py` are RP-3.2 IDs. The 14-rule order and the one-line rulings for every open case are in `NOTES.md` sections 3 and 6.

## Evaluation before and after one change

Change: `CLASSIFY_PROMPT_VERSION` from `v1` to `v2`. `v1` is the exact option text from commit `938668d`; `v2` sharpens the definitions so a status question does not say the parcel failed to arrive, a non-delivery claim does, a return sends goods back without asking for money, and a refund asks for money back. The extractor, rule list, thresholds (`TICKET_TYPE_MIN_PROB=0.6` unchanged) and models are the same in both runs.

The change targets the `decider` classifier, whose type probabilities sat just under the threshold on status-vs-delivery and return-vs-refund wording, sending resolvable tickets to the rule-13 fallback (`ESCALATE` / `SUPPORT_LEAD`). Both runs used `CLASSIFIER=decider`, a fresh SQLite database, `MODEL_CACHE_DISABLED=1` and live calls. They are committed as `eval/results/run_20261109T151707Z.json` (v1, before) and `eval/results/run_20261109T152744Z.json` (v2, after); `eval/results/summary.md` compares them side by side.

| Metric | v1 before | v2 after |
|---|---:|---:|
| 15 human labels | 13/15 | 15/15 |
| 40 gold decisions | 33/40 | 39/40 |
| Ledger-dependent matches (T027/T028, ruling 1) | 2 | 2 |
| Rule-13 fallbacks | 7 | 1 |
| 6 hand-written negative cases | 6/6 | 6/6 |
| Idempotency (40 reposts) | byte-identical, ledger unchanged | byte-identical, ledger unchanged |
| Approvals (T001, T003, T026, P02) | all pass; conflicting reject `409` | all pass; conflicting reject `409` |
| Tokens | 30,867 input + 5,785 output | 32,937 input + 5,784 output |
| List-price cost | $0.02372260 total | $0.02434110 total |
| Median cost per request | $0.00052330 | $0.00053680 |
| Latency | p50 2,820.86 ms, p95 3,600.63 ms | p50 2,469.44 ms, p95 3,479.45 ms |

v1 fell back on 7 tickets (T013, T014, T016, T024, P07, P09, P10), which cost two human labels (T013, T014). v2 removes six of the seven. One fallback remains: T010, an order-status question the decider still scores below 0.6, so it escalates to a Support Lead instead of getting a status reply. That is a safe failure (a human sees it), not a wrong decision, and the threshold was deliberately not lowered to hide it. v2 costs 2,070 more input tokens in total because its prompt lists the definitions.

The same v1/v2 comparison was also run in `CLASSIFIER=llm` mode (the service default), where it made no difference: 15/15 labels, 40/40 gold and 0 fallbacks in both runs ($0.02212070 vs $0.02341470).

Negative cases: six tickets written by hand in `eval/cases/negative/` (not taken from the data room), each with an expected outcome, approver and reason codes: an injection hidden in a forwarded reply, a claimed manager approval with a fake reference, an order ID one digit off a real order (must not fuzzy-match: `INVALID_REQUEST` / `ORDER_NOT_FOUND`), a real order from a different email (`CUSTOMER_MISMATCH`), a photo-backed damage claim at day 46 (`REJECT` / `OUT_OF_WINDOW`), and a day-35 damage claim with a PDF instead of an image (no 2.6 extension, `REJECT` / `OUT_OF_WINDOW`). They run in `python -m eval.run_eval` and in pytest (`tests/test_negative_cases.py`).

Pass rule: all 15 labels, all gold decisions except documented ledger-dependent cases, all 6 negative cases, byte-identical reposts with unchanged ledger counts, and every PEND approval flow passing. v2 meets it except for the one T010 fallback described above.

## Cost and latency

- Latency was measured client-side with `perf_counter` around each `POST /tickets`, nearest-rank p50/p95 over the 40 first-pass calls, with `MODEL_CACHE_DISABLED=1` so every model call was live.
- Cost comes from each response's real `trace.model_calls` token counts and the list-price `EXTRACT_PRICE_*` settings; no provider invoice or estimate is used.
- v2 (final, decider): 32,937 input + 5,784 output tokens, **$0.02434110** total, **$0.00053680 median per request**, p50 2,469.44 ms, p95 3,479.45 ms.

## What is not built

- Deployment is prepared (`Dockerfile`, `.dockerignore`, `fly.toml`, per-IP rate limit on `POST /tickets`; steps in `README.md` under Deploy) but the public URL is added only after the owner deploys it.
- The data room is not in this repository for confidentiality; it must be placed at `data_room/` and prepared with `python -m src.prepare`.
- No live carrier API, no email sending, no real refund execution and no authentication. The service only proposes and records.
- Postgres is supported by the portable SQLAlchemy types but was exercised only on SQLite.
- One approval decision per PEND run is supported; there is no multi-step approval chain or operator UI.
- The open policy questions from `NOTES.md` section 6 remain policy decisions for the business, though each has a stated one-line ruling in the service.
