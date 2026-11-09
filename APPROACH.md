# Approach

## Problem summary

Larkfield Customer Care answers ~1,200 emails/day and needs consistent, auditable decisions. RP-3.2 v3.2 is the only decision source; the help-centre FAQ is superseded and warehouse picking is out of scope. It must return the contract, propose rather than perform, stop for a person when policy requires approval or escalation, treat email as data, and report token cost.

## Technical approach

`src/app.py` runs normalize -> classify -> extract -> resolve -> checks -> decide -> reply/persist. Models fill only `Extraction`; `src/decide.py` holds 14 ordered rules and is the only place an outcome is chosen. A check that cannot complete becomes `UNABLE_TO_VERIFY`. Money is `Decimal` until `src/store.py` builds the schema's float. The ledger is transactional and idempotent: `ticket_id` is unique, repeats return stored bytes, and a PEND decision flips one REFUND row without adding another. Live on Fly.io: https://larkfield-ticket-desk.fly.dev

## AI tool usage

Staged: prepare -> explore -> label check -> rulings -> deterministic core -> model layer -> service -> eval, each gate checked against the 15 labels, 40-ticket table and boundary tests ([tests/](tests/)); [eval/run_eval.py](eval/run_eval.py) runs it. Human decisions overrode proposals: keep `last_event_at` as recorded for T011, exact elapsed hours not whole days, no invented reason codes, keep `TICKET_TYPE_MIN_PROB=0.6`, and drop a worse two-level decider (27/40 vs 29/40).

## Trade-offs and limitations

Gold is same-agent, so the 15 labels are the independent check. `llm` is default; decider is less calibrated. The keyword injection guard favours recall. Postgres was not live-tested. Open policy rulings are in [NOTES.md](NOTES.md).
