# Eval summary

Side-by-side comparison of every committed `run_*.json` in `eval/results/`.
Fallbacks are rule-13 `ESCALATE` / `SUPPORT_LEAD` results with no reason code.

| Run | Prompt | Mode | Labels | Gold | Negative | Fallbacks | Cost (USD) | Median cost (USD) | p50 (ms) | p95 (ms) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `run_20261109T151707Z.json` | v1 | decider | 13/15 | 33/40 | 6/6 | 7 | $0.02372260 | $0.00052330 | 2820.86 | 3600.63 |
| `run_20261109T152744Z.json` | v2 | decider | 15/15 | 39/40 | 6/6 | 1 | $0.02434110 | $0.00053680 | 2469.44 | 3479.45 |

## Negative cases

Hand-written tickets in `eval/cases/negative/`, posted after the 40-ticket sweep and checked on outcome, approver role, reason codes and ledger amount.

| Run | Negative | Failing cases |
|---|---:|---|
| `run_20261109T151707Z.json` | 6/6 | — |
| `run_20261109T152744Z.json` | 6/6 | — |

## `run_20261109T151707Z.json`

- Started: 2026-11-09T15:17:07.499391+00:00
- Base URL: http://127.0.0.1:8767
- Prompt version: v1
- Tokens: 30867 input + 5785 output
- Ledger rows written in the first pass: 67
- Negative cases: **6/6**
- Reposts byte-identical: **True**; ledger unchanged: **True**
- All approval flows passed: **True**
- Label mismatch `T013`: outcome ESCALATE != UNABLE_TO_VERIFY; approver_role SUPPORT_LEAD != None; human_approval_required True != False
- Label mismatch `T014`: outcome ESCALATE != REJECT; approver_role SUPPORT_LEAD != None; human_approval_required True != False
- Gold mismatch `P07`: outcome ESCALATE != PROCEED; amount None != 45.00
- Gold mismatch `P09`: outcome ESCALATE != UNABLE_TO_VERIFY; reason_codes [] != ['CARRIER_STATUS_UNAVAILABLE']
- Gold mismatch `P10`: outcome ESCALATE != REJECT; reason_codes [] != ['OUT_OF_WINDOW']
- Gold mismatch `T013`: outcome ESCALATE != UNABLE_TO_VERIFY; reason_codes [] != ['CARRIER_STATUS_UNAVAILABLE']
- Gold mismatch `T014`: outcome ESCALATE != REJECT; reason_codes [] != ['OUT_OF_WINDOW']
- Gold mismatch `T016`: outcome ESCALATE != PROCEED; amount None != 129.00
- Gold mismatch `T024`: reason_codes [] != ['LOST_IN_TRANSIT']
- Ledger-dependent `T027`: expected (ruling 1, earlier REFUND in the ledger); outcome REJECT != PROCEED; amount None != 129.00; reason_codes ['ALREADY_REFUNDED'] != []
- Ledger-dependent `T028`: expected (ruling 1, earlier REFUND in the ledger); outcome REJECT != PROCEED; amount None != 38.00; reason_codes ['ALREADY_REFUNDED'] != []

## `run_20261109T152744Z.json`

- Started: 2026-11-09T15:27:44.965889+00:00
- Base URL: http://127.0.0.1:8769
- Prompt version: v2
- Tokens: 32937 input + 5784 output
- Ledger rows written in the first pass: 64
- Negative cases: **6/6**
- Reposts byte-identical: **True**; ledger unchanged: **True**
- All approval flows passed: **True**
- Gold mismatch `T010`: outcome ESCALATE != PROCEED
- Ledger-dependent `T027`: expected (ruling 1, earlier REFUND in the ledger); outcome REJECT != PROCEED; amount None != 129.00; reason_codes ['ALREADY_REFUNDED'] != []
- Ledger-dependent `T028`: expected (ruling 1, earlier REFUND in the ledger); outcome REJECT != PROCEED; amount None != 38.00; reason_codes ['ALREADY_REFUNDED'] != []
