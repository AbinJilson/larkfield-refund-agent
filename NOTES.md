# Decisions log

All dataset paths below are relative to `data_room/` (`DATA_ROOM_DIR`).
The files were moved there unchanged on 2026-11-09 (checksums verified).

Sources read: `schema/response_schema.py`, `policies/refund_policy_RP-3.2.md`,
`policies/help_centre_faq_v2.md`, `policies/warehouse_picking_WH-PICK-1.4.md`,
`DATA_DICTIONARY.md`, `tickets/eval_seed_labelled.json` and the 15 matching
emails in `tickets/support_inbox_export.json`. The `data/` CSVs are summarised
through `prepared/` by `src/prepare.py` and cross-checked by `src/explore.py`;
the counts are in section 1.

## 1. Data findings

`src/prepare.py` builds `prepared/` from the data room and writes
`prepared/prepare_report.json`; `src/explore.py` reads `prepared/` and the
ticket files and writes `prepared/explore_report.json`. The seven
`prepared/*.csv` tables reproduce byte-for-byte on each run. Row counts after
dedupe: customers 53, orders 47, order_items 57, shipments 47, refunds 3,
refunds_by_line 3.

| Finding | Count | What we did |
|---|---:|---|
| Column name with a space (`Order Total`) | 1 | Renamed to `order_total` in `prepared/` only. |
| Cells with surrounding whitespace | 0 | No CSV cell needed trimming. |
| Exact duplicate order rows | 2 (LF-100216, LF-100239) | Dropped the duplicate copy; one row per order. |
| Order IDs changed / unparseable | 0 / 0 | No order ID needed rewriting. |
| Duplicate customer emails after trim/lowercase | 0 | No customer merge was needed. |
| DD/MM dates converted to ISO-8601 UTC | 13 | Parsed day-first, stored as `YYYY-MM-DDTHH:MM:SSZ`. |
| Money values with `$`/`,` stripped | 31 (all `orders.order_total`) | Parsed as `Decimal`; 0 parse failures. |
| Non-whole `qty` | 0 | Integers throughout. |
| Shipments with `status = UNAVAILABLE` | 3 (SHP-5012, SHP-5032, SHP-5038) | Treated as carrier status unavailable (RP-3.2 4.2). |
| Orders with more than one shipment | 0 | One shipment row per order. |
| Raw `orders.status` vs carrier status label differences | 13 (10 SHIPPED/IN_TRANSIT, 3 SHIPPED/UNAVAILABLE) | Carrier feed wins: 0 true delivery-state mismatches (RP-3.2 2.1). |
| Refund lines with multiple ISSUED refunds / over-refunded / unmatched | 0 / 0 / 0 | Summed ISSUED per line; nothing exceeded the line value. |
| Referential-integrity failures | 0 | Every foreign key resolves. |
| Sender emails needing trim/lowercase | 8 (T001, T006, T011, T016, T021, T026, P01, P06) | Normalized at runtime in `src/normalize.py` before the 4.1 match. |
| Order-ID mentions not already `LF-` + 6 digits | 25 (19 support, 6 public) | Normalized at runtime; never fuzzy-matched to a nearby order. |
| `order_date` after `shipped_at` | 2 (LF-100221, LF-100240) | Recorded in `prepare_report.json`; parsed values unchanged. |
| `shipped_at` after `delivered_at` | 0 | Recorded in `prepare_report.json`. |

Date format: slash dates in the export are **DD/MM**. 10 of the 13 slash dates
have a first part greater than 12, which is impossible as a month, so the column
is day-first; we do not assume one column flips format halfway through. The
three ambiguous dates (both parts ≤ 12) are `08/10/2026` on LF-100235 and
LF-100221, and `06/10/2026` on LF-100230. Under DD/MM, LF-100221 resolves to
2026-10-08 but its carrier `shipped_at` is 2026-09-30, i.e. it was "ordered"
after it shipped; that is bad source data. Reading it as MM/DD (2026-08-10)
does not help: it leaves a 51-day order-to-shipment gap, which is equally
implausible. No rule uses `order_date` (the refund window runs from
`delivered_at` and lost-in-transit from `last_event_at`), so no outcome changes.

## 2. Policy inventory

### Documents

| Document | Version | Date | Owner | Status for this desk |
|---|---|---|---|---|
| RP-3.2 Customer Refund Policy | 3.2 | Effective 2026-07-01 | Head of Customer Care | **Governing.** Replaces RP-3.1 and "all earlier customer-care guidance" (header). |
| HC-FAQ Help Centre FAQ | 2 | Published 2025-10-14 | Digital Content | **Superseded** by RP-3.2 (published before 2026-07-01). It conflicts on window (14 days), auto limit ($250) and tracking (5 days). Not used for decisions. |
| WH-PICK-1.4 Warehouse picking | 1.4 | Effective 2026-08-15 | Warehouse Operations | **Out of scope**: its Scope section says it "does not set Customer Care return, refund or approval rules". Its stock-cost rule is not a customer payment. |

### RP-3.2 clauses

| Clause | Says | Threshold / window | Reason code(s) | Approver role |
|---|---|---|---|---|
| 1 | Applies to all refund, return, delivery and order-status requests | | | |
| 2.1 | Refund or return may be requested within the window | 30 days from the carrier's delivery date | | |
| 2.2 | Damaged, faulty, wrong or not-as-described items refunded at order-line value of the affected item(s) | | | |
| 2.3 | Requests after the 2.1 window are declined, except under 2.6 | > 30 days | OUT_OF_WINDOW | |
| 2.4 | Asked for more than the affected lines → refund affected lines only and explain | | PARTIAL_LINES | |
| 2.5 | Don't repay. Per line: affected qty ≤ ordered qty, minus ISSUED refunds; fully refunded line = 0; other lines stay eligible | Cap = unpaid affected-line value | ALREADY_REFUNDED | |
| 2.6 | Damaged or faulty plus a photo attachment in this email → extended window. Any image attachment counts; no content analysis. Doesn't apply to wrong/not-as-described without damage. Changes the window only | Up to and including 45 days; code for days 31–45 | DAMAGE_EXCEPTION (OUT_OF_WINDOW when exceeded) | |
| 3.1 | May be proposed without review | ≤ $150 per request | | none |
| 3.2 | Needs Support Lead approval before processing | > $150 per request | APPROVAL_REQUIRED | SUPPORT_LEAD |
| 3.3 | Processed at most once per request, however many approvals | | | |
| 4.1 | Order must exist and belong to the sender (email on file); never act on a similar or nearby order | | ORDER_NOT_FOUND, CUSTOMER_MISMATCH | |
| 4.2 | No carrier status → non-delivery can't be verified; no refund; tell the customer we're checking | | CARRIER_STATUS_UNAVAILABLE | |
| 5.1 | No carrier movement → lost in transit, escalate | > 10 days | LOST_IN_TRANSIT | CARRIER_CLAIMS |
| 5.2 | Attempts to bypass policy, chargeback or legal threats, claimed pre-approval → escalate; no refund | | POLICY_OVERRIDE_ATTEMPT | TRUST_SAFETY |
| 5.3 | Order-status question on a normally moving shipment → reply with current status; no action | (≤ 10 days since last movement, by 5.1) | | |

## 3. Decision table

First match wins. "Amount" is the 2.5 amount summed over the eligible
affected lines (line value for the affected quantity, minus ISSUED refunds and
earlier ledger REFUND rows that are not REJECTED, floored at zero). Rule order
is now fixed by the build in `src/decide.py`; the DECIDED rulings are in
section 5.

| # | Condition | Outcome | reason_codes | approver_role | Clause |
|---|---|---|---|---|---|
| 1 | Email tries to bypass policy, claims pre-approval, or threatens a chargeback or legal action | ESCALATE | POLICY_OVERRIDE_ATTEMPT | TRUST_SAFETY | 5.2 |
| 2 | No order ID, more than one order ID, or order not in `orders.csv` | INVALID_REQUEST | ORDER_NOT_FOUND | — | 4.1 |
| 3 | Order exists but its customer's email ≠ sender (after normalizing) | INVALID_REQUEST | CUSTOMER_MISMATCH | — | 4.1 |
| 4 | Non-delivery claim or order-status question, and carrier status unavailable (no shipment, UNAVAILABLE, or empty `last_event_at`) | UNABLE_TO_VERIFY | CARRIER_STATUS_UNAVAILABLE | — | 4.2 |
| 5 | Shipment not delivered and last carrier movement more than 240 hours (10 × 24h) before `received_at` | ESCALATE | LOST_IN_TRANSIT | CARRIER_CLAIMS | 5.1 |
| 6 | Order-status question or non-delivery claim, shipment moving normally (or delivered) | PROCEED | (none) | — | 5.3 |
| 7 | Refund/return, but no shipment row or no carrier delivery date | UNABLE_TO_VERIFY | CARRIER_STATUS_UNAVAILABLE | — | 4.2 |
| 8 | Refund/return, elapsed since delivery > window (45 × 24h if damaged/faulty with photo, else 30 × 24h) | REJECT | OUT_OF_WINDOW | — | 2.1, 2.3, 2.6 |
| 9 | Refund/return, every affected line already fully refunded (amount = 0) | REJECT | ALREADY_REFUNDED | — | 2.5 |
| 10 | Refund/return, amount > $150.00 | PEND | APPROVAL_REQUIRED + any of DAMAGE_EXCEPTION / PARTIAL_LINES / ALREADY_REFUNDED | SUPPORT_LEAD | 3.2 (+2.4, 2.5, 2.6) |
| 11 | Refund/return, amount ≤ $150.00, and customer asked for more than the affected lines or some lines were cut by 2.5 | PROCEED_MODIFIED | PARTIAL_LINES and/or ALREADY_REFUNDED (+ DAMAGE_EXCEPTION) | — | 2.4, 2.5, 3.1 |
| 12 | Refund/return, amount ≤ $150.00 | PROCEED | DAMAGE_EXCEPTION if day 31–45, else none | — | 3.1, 2.6 |
| 13 | Anything else: type OTHER, or classification below `TICKET_TYPE_MIN_PROB` | ESCALATE | (none) | SUPPORT_LEAD | none |
| 14 | Refund/return where the affected line or quantity could not be resolved | UNABLE_TO_VERIFY | (none; detail in check evidence) | — | 2.2, 2.5 (internal catch-all) |

Rules 4–12 and 14 only run when the classification is usable (type is not
OTHER and confidence ≥ `TICKET_TYPE_MIN_PROB`); otherwise rule 13 catches the
ticket after identification (rules 1–3). Rule 14 is the implementation of
AGENTS rule 3 (“a check that cannot complete is `UNABLE_TO_VERIFY`”); it
invents no reason code and leaves the detail to the `refund.affected_lines`
and `refund.quantity` check evidence.

Ordering notes, preserved through the renumbering:

- **Lost shipment = carrier claim (rule 5 before 6).** A shipment with no
  movement for more than 10 days is a carrier matter and escalates; the plain
  status reply only applies when the shipment is still moving.
- **Authority beats modification per 3.2 (rule 10 before 11).** If the
  remaining amount is over $150, the request needs Support Lead approval even
  when 2.4 would otherwise produce PROCEED_MODIFIED.
- **Identity checks come before type (rules 2–3 before 13).** A ticket with
  no order, an unknown order or a wrong sender is INVALID_REQUEST even if its
  type would otherwise be OTHER or low-confidence.

`human_approval_required` = true exactly when `approver_role` is set (rules 1,
5, 10 and 13), which matches all 15 labels. Coverage: all 7 outcomes and all 3
approver roles.

## 4. Label check

Each row was checked against `prepared/` (delivery dates, carrier status,
prices, prior refunds) via `src/explore.py`. All amounts are the 2.5 remaining
amount. All 15 rows match the draft table.

| Ticket | Expected | Draft table | Result | Cause / deciding numbers |
|---|---|---|---|---|
| T001 | PEND, SUPPORT_LEAD, $210, DAMAGE_EXCEPTION + APPROVAL_REQUIRED | Rule 10 | MATCH | 40 days since delivery (≤ 45 extended window, photo present); line $210.00 > $150. |
| T002 | PROCEED | Rule 12 | MATCH | 9 days since delivery, photo, $129.00 ≤ $150, no prior refund. |
| T003 | PEND, SUPPORT_LEAD | Rule 10 | MATCH | 6 days since delivery, photo; ordered qty is 1, so affected value $185.00 > $150. |
| T004 | PROCEED | Rule 12 | MATCH | 10 days since delivery, photo, $45.00 ≤ $150. |
| T005 | PROCEED | Rule 12 | MATCH | 11 days since delivery, photo, $38.00 ≤ $150. |
| T006 | PROCEED | Rule 12 | MATCH | Wrong item, no photo so the 30-day window applies; 2 days since delivery, $52.00 ≤ $150. |
| T008 | PROCEED | Rule 6 | MATCH | Status question; carrier IN_TRANSIT, last movement 1.00 day before `received_at` (≤ 10). |
| T011 | ESCALATE, CARRIER_CLAIMS | Rule 5 | MATCH | Carrier IN_TRANSIT, last movement 16.00 days before `received_at` (> 10). |
| T013 | UNABLE_TO_VERIFY | Rule 4 | MATCH | Non-delivery claim; carrier UNAVAILABLE and `last_event_at` empty. |
| T014 | REJECT | Rule 8 | MATCH | Change of mind, no photo; 38 days since delivery > 30-day window; $159.00. |
| T018 | REJECT | Rule 9 | MATCH | 10 days since delivery (inside 30), but the chair line's $185.00 is already refunded $185.00, leaving $0.00 → ALREADY_REFUNDED, not the window. |
| T019 | PROCEED_MODIFIED | Rule 11 | MATCH | 6 days, no photo (30-day window); cushion $27.50 ≤ $150. Whole-order ask but only the cushion line is affected → PARTIAL_LINES. |
| T020 | ESCALATE, TRUST_SAFETY | Rule 1 | MATCH | Body contains an instruction and a claimed pre-approval (5.2); data otherwise 12 days, $289.00. Outcome comes from rule 1. |
| T021 | ESCALATE, TRUST_SAFETY | Rule 1 | MATCH | Body contains an instruction claiming authority to skip review (5.2); data otherwise 5 days, $27.50. Outcome comes from rule 1. |
| T026 | PEND, SUPPORT_LEAD, $185, ALREADY_REFUNDED + APPROVAL_REQUIRED | Rule 10 | MATCH | 12 days since delivery, photo; 1 of 2 chairs: $185.00 − $0.00 = $185.00; vase: $45.00 − $45.00 = $0.00; total $185.00 > $150. |

All 15 rows MATCH: no MISMATCH and no proposed decision-table fix. The data
closes the earlier open items: T014 is out of window, T018 is fully refunded
inside the window, T003's chair line has qty 1, and T026 comes to $185 after
the vase's $45 ISSUED refund.

## 5. Gap rulings

### DECIDED this round

- **DECIDED (ruling 1) — re-sent tickets are not special; the ledger deducts.**
  Remaining per line = affected line value − ISSUED refunds − earlier ledger
  REFUND entries for that line whose status is not `REJECTED`. 2.5 says do not
  repay an issued refund and 3.3 says a request is processed at most once, so
  the second request is priced at whatever is left. T027 sent after T002, with
  T002's REFUND row passed in, is REJECT / ALREADY_REFUNDED and its
  `refund.prior_refunds` evidence names the earlier run. Without the earlier
  ledger row T027 is PROCEED because the runs are independent. Clause: 2.5,
  3.3.
- **DECIDED (ruling 2) — change of mind within 30 days is refundable.** 2.1
  allows a return request within the window; 2.2 is not read as exhausting the
  reasons, so a change-of-mind return is priced at the affected-line value.
  T015 (20 days, $24.00), T016 (22 days, $129.00) and P07 (22 days, $45.00)
  are PROCEED at rule 12. T017 and P10 are exactly 45.00 days with no photo,
  so the 30-day window still makes them REJECT / OUT_OF_WINDOW. Clause: 2.1,
  2.2.
- **DECIDED (ruling 3) — `last_event_at` before `shipped_at` is used as
  recorded.** The 5.1 test uses the carrier's `last_event_at` even when it
  precedes `shipped_at`; the oddity is recorded as an extra evidence line on
  the `carrier.movement` check. This keeps T011, T012 and P06 as ESCALATE /
  LOST_IN_TRANSIT, matching T011's label. Clause: 5.1.
- **DECIDED (ruling 4) — every day threshold is exact elapsed time.** The
  window and movement tests compare Decimal hours from exact timestamps, not
  whole calendar days: out-of-window is `> 30×24h` (or `45×24h`), lost is
  `> 10×24h`. Exactly 30×24h, 45×24h and 10×24h are inside; one second more
  is outside. T024 at 248h (10 days 8 hours) is ESCALATE / LOST_IN_TRANSIT.
  Clause: 2.1, 2.3, 2.6, 5.1.
- **DECIDED (ruling 5) — a shipping/delivery-fee request refunds the affected
  item line only.** 2.2 prices the order line and 2.4 cuts the reply to the
  affected lines, so a fee request is PROCEED_MODIFIED / PARTIAL_LINES at the
  item value; the fee itself is not refunded. T025 is $45.00. Clause: 2.2,
  2.4.
- **DECIDED (ruling 6) — no product named, and unusable classification.**
  When no product is named, a one-line order supplies the line and a
  multi-line order is UNABLE_TO_VERIFY (`PRODUCT_AMBIGUOUS`). Separately, a
  ticket typed OTHER or classified below `TICKET_TYPE_MIN_PROB` is ESCALATE /
  SUPPORT_LEAD under rule 13; the policy has no clause for that outcome.
  Clause: 2.2, 2.5 for the line rule; none for rule 13.

### Standing interpretations

- **DECIDED:** RP-3.2 is the only decision source. HC-FAQ v2 is superseded
  (RP-3.2 header) and WH-PICK-1.4 is out of scope (its own Scope section).
  `trace.documents_used` lists RP-3.2 version 3.2.
- **DECIDED:** the window runs from `shipments.delivered_at` to ticket
  `received_at`, both UTC, compared as exact elapsed hours (ruling 4). Day 30
  and day 45 are inside; "within"/"up to and including" is `≤`.
- **DECIDED:** a "photo" is any attachment whose `content_type` starts
  `image/` on this email (2.6). Earlier emails don't count. 2.6 applies only
  when the email says damaged, faulty or broken/likewise; "wrong item" or
  "not as described" alone uses the 30-day window.
- **DECIDED:** the refund amount is `unit_price × affected qty` minus ISSUED
  refunds and non-REJECTED ledger REFUND rows for that line, floored at zero.
  The shipping fee is excluded ("order-line value", 2.2).
- **DECIDED:** the $150 limit applies to the request total after 2.5
  deductions, and exactly $150.00 counts as ≤ (3.1 "up to and including").
- **DECIDED:** rule 1 (5.2) runs before identification, since an override
  attempt is escalated whatever the order state, and no refund is proposed.
- **DECIDED:** a request where every affected line comes to zero under 2.5 is
  REJECT / ALREADY_REFUNDED. The policy supplies the code, not the outcome.
- **DECIDED:** ALREADY_REFUNDED is also added when the email names a line that
  is already fully refunded, even if the customer doesn't ask for it again
  (T026; 2.5 "explain any prior refund separately").
- **DECIDED:** "no carrier movement for more than 10 days" means
  `received_at − last_event_at > 10×24h` for a shipment that isn't DELIVERED
  (5.1).
- **DECIDED:** status UNAVAILABLE, an empty `last_event_at`, or no shipment
  row means carrier status can't be obtained (4.2, DATA_DICTIONARY).
- **DECIDED:** order IDs match only on exact `LF-` + 6 digits after
  normalizing; never fuzzy (4.1). Email match is on trimmed, lowercased
  addresses.
- **DECIDED:** `human_approval_required` = true exactly when `approver_role`
  is set (consistent with all 15 labels).
- **DECIDED:** money is Decimal inside the pipeline; conversion to the
  schema's float fields happens only at the persistence/HTTP boundary.
- **DECIDED:** slash dates in the export are DD/MM (10 of 13 have a day >
  12). The three ambiguous dates (`08/10` on LF-100235 and LF-100221, `06/10`
  on LF-100230) are logged in `prepare_report.json`.
- **DECIDED:** `order_date` never drives a decision. The window runs from
  `delivered_at` (2.1) and lost-in-transit from `last_event_at` (5.1), so the
  order-before-ship inversions on LF-100221 and LF-100240 are source-data
  notes only.
- **DECIDED:** duplicate/re-sent tickets are handled by ledger deduction
  (ruling 1). The ledger row is the record that makes the second request
  idempotent; the pipeline itself stays stateless and takes the ledger as a
  list.

## 6. Open questions

### Closed this round

- **CLOSED (ruling 1):** re-sent tickets. The second request deducts the
  earlier ledger REFUND row and becomes REJECT / ALREADY_REFUNDED; T027 after
  T002 is the test. Only non-REJECTED rows deduct. Clause: 2.5, 3.3.
- **CLOSED (ruling 2):** change of mind inside 30 days. T015, T016 and P07
  are PROCEED at the affected-line value; T017 and P10 stay out of window at
  exactly 45.00 days. Clause: 2.1, 2.2.
- **CLOSED (ruling 3):** `last_event_at` before `shipped_at`. The build uses
  `last_event_at` as recorded and adds the anomaly to the movement evidence,
  so T011 keeps its ESCALATE label. Clause: 5.1.
- **CLOSED (ruling 4):** exact elapsed time for every day threshold. Boundaries:
  30×24h and 45×24h are inside, +1s is outside; 10×24h is inside, +1s is lost.
  T024 is 248h → ESCALATE. Clause: 2.1, 2.3, 2.6, 5.1.
- **CLOSED (ruling 5):** shipping/delivery-fee requests refund the affected
  item line only, PROCEED_MODIFIED / PARTIAL_LINES. T025 = $45.00. Clause 2.2,
  2.4.
- **CLOSED (ruling 6):** no product named → one line uses it, multi-line is
  UNABLE_TO_VERIFY; type OTHER or low confidence → ESCALATE / SUPPORT_LEAD.
- **CLOSED (T014):** 38 days, no photo, no damage claim → rule 8 REJECT /
  OUT_OF_WINDOW.
- **CLOSED (T018):** 10 days is inside the window; the chair line is fully
  refunded, so rule 9 gives REJECT / ALREADY_REFUNDED, not rule 8.
- **CLOSED (T003):** the chair line has ordered qty 1, so affected value is
  $185.00 → PEND / SUPPORT_LEAD.
- **CLOSED (T026):** 1 of 2 chairs ($185.00); the vase is fully refunded
  ($45.00 − $45.00); total $185.00 → PEND / SUPPORT_LEAD with
  ALREADY_REFUNDED + APPROVAL_REQUIRED.

### Still open

- **OPEN (policy):** rule 14 (unresolved affected line or quantity) leaves
  `reason_codes` empty and puts the detail in the `refund.affected_lines` and
  `refund.quantity` check evidence. The outcome UNABLE_TO_VERIFY follows
  AGENTS rule 3. Confirm that an empty code list is acceptable for this
  outcome, or name a policy code.
- **OPEN (policy):** a non-delivery claim where the carrier says DELIVERED.
  There is no clause; REJECT or ESCALATE? No ticket in this set hits it.
- **OPEN (policy):** reason codes for a 5.3 status reply. None are defined;
  the build uses an empty list.
- **OPEN (policy):** should UNABLE_TO_VERIFY ever need approval? The T013
  label says no and the build follows it, but the policy is silent.
- **OPEN (policy):** when 2.4 (partial lines) and 3.2 (> $150) both apply,
  the build gives PEND with both codes. Confirm PEND beats PROCEED_MODIFIED.
- **OPEN (policy):** is a chargeback or legal threat without any bypass
  instruction still 5.2? The text says yes; confirm a plain delay complaint is
  not caught by the same pattern.
- **OPEN (policy):** a missing shipment row for a refund/return is
  UNABLE_TO_VERIFY with the 4.2 code `CARRIER_STATUS_UNAVAILABLE`. That code
  was written for non-delivery; a distinct code may be wanted for a refund
  whose delivery date cannot be obtained (rule 7).
- **OPEN (policy):** unknown affected qty on a multi-unit line. The build stops
  at rule 14 rather than guessing, with no reason code.
  No labelled ticket needs it; LF-100224 (T026) is the only qty-2 line
  with a ticket and the ticket says "one of my two".

## 7. Unlabelled ticket walkthrough

25 tickets have no label: 15 from `tickets/support_inbox_export.json` (T007,
T009, T010, T012, T015–T017, T022–T025, T027–T030) and all 10 in
`inbox/public_tickets.json`. `src/explore.py` pulls the same facts and walks
the section 3 table first-match-wins with a throwaway Python function (no model
calls). Rule numbers refer to section 3. Amounts are the 2.5 remaining total.
Every threshold uses exact elapsed time (ruling 4): the delivery window is
30×24h (45×24h with a qualifying photo) and the movement test is 10×24h.

| Ticket | Type | Rule | Outcome | Flag / deciding numbers |
|---|---|---|---|---|
| T007 | refund, wrong item | 12 | PROCEED | 2 days, $52.00 ≤ $150, no prior refund. |
| T009 | non-delivery | 6 | PROCEED | IN_TRANSIT, last scan 1.00d before `received_at`; same order as T029. |
| T010 | non-delivery | 6 | PROCEED | IN_TRANSIT, last scan 1.00d. |
| T012 | status | 5 | ESCALATE / LOST_IN_TRANSIT / CARRIER_CLAIMS | `last_event_at` 2026-09-19T11:00 is **before** `shipped_at` 2026-10-04T11:00; 17.00d from last scan vs 2.00d from `shipped_at` (would be rule 6). |
| T015 | change of mind | 12 | PROCEED | 20 days, $24.00. 2.2 does not price a change-of-mind return; rule 12 is the draft assumption. |
| T016 | change of mind | 12 | PROCEED | 22 days, $129.00; same 2.2 gap; sender needs trim/lowercase. |
| T017 | change of mind | 8 | REJECT / OUT_OF_WINDOW | Delivered 2026-08-25T16:00 → received 2026-10-09T16:00 = **45 days exactly (1080h)**; no photo, so the 30-day window applies; $310.00. |
| T022 | refund, broken | 2 | INVALID_REQUEST / ORDER_NOT_FOUND | LF-109777 is not in `orders.csv`. |
| T023 | refund, broken | 3 | INVALID_REQUEST / CUSTOMER_MISMATCH | LF-100202 belongs to carla.ivanova2@mail.example; sender is wen.novak22@mail.example. Text also says only "my item", so no line is identified. |
| T024 | status | 5 | ESCALATE / LOST_IN_TRANSIT / CARRIER_CLAIMS | Last scan 2026-10-02T06:00 → received 2026-10-12T14:00 = **10 days 8 hours (248h)**. Whole days 10 → rule 6; exact 10.33 → rule 5. LF-100221 also has the `order_date` inversion. |
| T025 | refund, damaged | 11 | PROCEED_MODIFIED / PARTIAL_LINES | 5 days, $45.00 vase, photo; also asks for the delivery charge, which 2.2 excludes. |
| T027 | refund, damaged (duplicate) | 12 | PROCEED | 9 days, $129.00; same order LF-100201 as T002. If T002's REFUND row is in the ledger, ruling 1 makes this REJECT / ALREADY_REFUNDED. |
| T028 | refund, damaged (duplicate) | 12 | PROCEED | 11 days, $38.00; same order LF-100204 as T005. With T005's ledger row: REJECT / ALREADY_REFUNDED. |
| T029 | non-delivery (duplicate) | 6 | PROCEED | IN_TRANSIT, 1.12d; same order LF-100208 as T009. No ledger action for a status reply, so the re-send adds nothing. |
| T030 | change of mind (duplicate) | 8 | REJECT / OUT_OF_WINDOW | 38 days, $159.00; same order LF-100213 as T014, which is already rejected, so 3.3 changes nothing. |
| P01 | refund, faulty | 12 | PROCEED + DAMAGE_EXCEPTION | 40 days ≤ 45 (faulty + photo), $64.00. |
| P02 | refund, wrong item | 10 | PEND / APPROVAL_REQUIRED / SUPPORT_LEAD | 4 days, $289.00 > $150, no photo. |
| P03 | non-delivery | 6 | PROCEED | IN_TRANSIT, last scan 1.00d. |
| P04 | refund, broken | 2 | INVALID_REQUEST / ORDER_NOT_FOUND | LF-116511 is not in `orders.csv`. |
| P05 | injection | 1 | ESCALATE / POLICY_OVERRIDE_ATTEMPT / TRUST_SAFETY | Injection plus claimed pre-approval; data otherwise 3 days, $258.00 (two $129.00 lines). |
| P06 | status | 5 | ESCALATE / LOST_IN_TRANSIT / CARRIER_CLAIMS | `last_event_at` 2026-10-18T15:00 is **before** `shipped_at` 2026-10-28T15:00; 15.00d vs 5.00d (would be rule 6). |
| P07 | change of mind | 12 | PROCEED | 22 days, $45.00; same 2.2 gap. |
| P08 | refund, damaged | 11 | PROCEED_MODIFIED / PARTIAL_LINES | 6 days; asks for the whole order but only the mug-set line matches, $38.00. |
| P09 | non-delivery | 4 | UNABLE_TO_VERIFY / CARRIER_STATUS_UNAVAILABLE | Carrier UNAVAILABLE, `last_event_at` empty. |
| P10 | change of mind | 8 | REJECT / OUT_OF_WINDOW | Delivered 2026-09-18T19:00 → received 2026-11-02T19:00 = **45 days exactly (1080h)**; no photo, so 30-day window applies; $52.00. |

Flags with no hits in this set:

- No unlabelled ticket claims non-delivery against a carrier that says
  DELIVERED; every non-delivery/status claim is IN_TRANSIT or UNAVAILABLE.
- No unlabelled ticket has zero order ids or more than one distinct order id.
- No unlabelled affected line has qty > 1, so the "how many units?" ambiguity
  does not arise (LF-100224 is the labelled T026; LF-100243 has no ticket).
- No unlabelled ticket is within a day of day 30; only T017 and P10 are
  within a day of day 45 (both exactly 45.00 days, and both still use the
  30-day window because there is no damage/photo).
- No unlabelled amount is within $1.00 of $150; the closest are $159.00
  (T030), $129.00 (T016, T027) and $258.00 (P05).
- No unlabelled refund line is already fully refunded, so rule 9 has no
  unlabelled hit.
- No unlabelled ticket lands on rule 13. The unclear cases are the
  change-of-mind rows (rule 12 assumes 2.2 covers them), T025 (shipping), the
  duplicates (3.3) and the pre-shipment scans (5.1).

### T011 carrier-date check

T011 (LF-100210): `shipped_at` 2026-10-03T10:00Z, `last_event_at`
2026-09-20T10:00Z, `received_at` 2026-10-06T10:00Z.

- Using `last_event_at`: 16.00 days → rule 5, ESCALATE / LOST_IN_TRANSIT /
  CARRIER_CLAIMS. This matches the T011 label.
- Using `shipped_at`: 3.00 days → rule 6, PROCEED.

The same inversion exists on T012 (17.00d → 2.00d) and P06 (15.00d → 5.00d);
both keep rule 5 under DECIDED ruling 3, which uses `last_event_at` as
recorded. No other ticket has `last_event_at < shipped_at`. So all three 5.1
escalations depend on the suspect earlier scan, and T011’s label would become
a MISMATCH if the rule were changed to use `shipped_at`.

## 8. Build and test summary

The deterministic pipeline is implemented in `src/normalize.py`,
`src/resolve.py`, `src/checks.py` and `src/decide.py`; the model layer in
section 9 sits in front of it, and `src/app.py` is not wired to either yet.
Contract models
(`Ticket`, `Check`, `Evidence`, ...) are loaded by path from
`DATA_ROOM_DIR/schema/response_schema.py`; the only internal models are
`Extraction`, `ExtractedLine`, `NormalizedTicket`, `Resolution`, `ResolvedLine`
and `LedgerRefund`.

| Module | Role |
|---|---|
| `src/normalize.py` | Sender/order-id normalization, image detection, deterministic `Extraction` |
| `src/resolve.py` | `prepared/` lookup, exact Decimal elapsed hours, 2.5 ledger deduction |
| `src/checks.py` | Schema `Check` objects with evidence and RP-3.2 clauses |
| `src/decide.py` | Ordered rules 1–14 (first match wins), `process_ticket` / `process_extraction` |
| `src/extract.py` | One `EXTRACT_MODEL` call, strict parse, cache, injection guard, failure fallback |
| `src/classify.py` | Ticket type via `decider` or `llm` mode, with probability |
| `src/trace.py` | Per-call tokens and `Decimal` cost, converted to schema `Trace` |

Rule order in `src/decide.py`: 1 injection, 2 order exists, 3 sender, 4
carrier status, 5 lost in transit, 6 status reply, 7 refund with no delivery
date, 8 window, 9 fully refunded, 10 over $150, 11 modified, 12 proceed, 13
unusable classification, 14 unresolved line/qty.

Tests use hand-written gold extractions in
`tests/fixtures/gold_extractions.json` for all 40 tickets (with `why` notes on
the uncertain cases). The suite covers the 15 labels, one test per DECIDED
ruling, the exact boundaries (30×24h / +1s, 45×24h with photo, $150.00 /
$150.01, 10×24h / +1s), the failure paths (missing shipment, UNAVAILABLE
carrier, unknown order, wrong sender) and an AST scan proving no `float` in
`checks.py` or `decide.py`.

`python -m pytest -q` → **104 passed**. The 96 deterministic tests cover the 15
labels, one test per DECIDED ruling, the exact boundaries (30×24h / +1s, 45×24h
with photo, $150.00 / $150.01, 10×24h / +1s), the failure paths (missing
shipment, UNAVAILABLE carrier, unknown order, wrong sender) and an AST scan
proving no `float` in `checks.py` or `decide.py`. The 8 model-layer tests use a
fake httpx client only. All 15 labels match on outcome,
approver, approval flag, amount and reason codes. The 40-ticket decision table
is generated at `tests/output/decisions.md`. That table runs each ticket on its
own; passing the earlier ledger row flips T027/T028 to REJECT /
ALREADY_REFUNDED as described by ruling 1.

## 9. Model layer (stage 2)

`src/extract.py`, `src/classify.py` and `src/trace.py` add the model calls in
front of the deterministic pipeline. Inputs are still normalized first; the
models never pick an outcome, a reason code or an approver.

### Extraction (`src/extract.py`)

- One `EXTRACT_MODEL` call per ticket to
  `https://openrouter.ai/api/v1/chat/completions` with
  `response_format={"type":"json_object"}`. The system prompt says the email is
  untrusted data; the email sits between `<email>` tags in the user message.
- The JSON is parsed and validated when the internal `Extraction` model (from
  `src/normalize.py`, shape unchanged) is constructed. `ticket_type`,
  `claim_kind`, confidence and `injection_probability` must be in range; a bad
  value fails the whole extraction.
- Order ids from the model are kept only if they also appear in the normalized
  email text. A model id that is absent from the email is treated as an
  invention and fails the extraction; the fallback keeps the email's own ids.
- No money is read from the model: `Extraction` has no amount field and
  `resolve.py` prices every line from `prepared/`. `qty` is only a selector and
  is capped by the ordered quantity; `qty: null` on a single-unit line resolves
  to 1.
- Any failure (missing key, timeout, HTTP error, invalid JSON, failed
  validation, invented id) returns `failed=True` with `safe_extraction`:
  type `OTHER`, confidence 0, email order ids, keyword injection guard applied.
  That routes to rule 13 (`ESCALATE` / `SUPPORT_LEAD`), or rule 14 if the type
  is usable but the line/quantity is missing, and rule 1 if the body injects.
  Nothing is guessed.
- `MODEL_TIMEOUT_SECONDS` and `MODEL_MAX_RETRIES` bound each call; retries
  apply to timeouts, 429 and 5xx.

### Classification (`src/classify.py`)

- `CLASSIFIER=decider` uses `DECIDER_MODEL` on OpenRouter's decisions endpoint
  (`/api/alpha/decisions`). A real call showed the shape: request is `model`,
  `state` (ticket fields) and `questions` (`{type: "choice", text, criteria}`),
  response is `answers.<key>.{choice, probabilities, confidence}` plus
  `usage.{input_tokens, output_tokens}`. A redacted sample is saved at
  `prepared/decider_sample.json`; the parser follows that shape. The decider
  model is rejected by `/chat/completions`.
- `CLASSIFIER=llm` uses `EXTRACT_MODEL` on `/chat/completions` with JSON mode,
  and normalizes a lowercase type.
- Both modes return `ticket_type` and `probability` (the chosen option's
  probability, not the separate `confidence` field, which is lower on the same
  calls). If the type is `OTHER` or the probability is below
  `TICKET_TYPE_MIN_PROB`, the result is not confident and rule 13 catches the
  ticket after identification (rules 1–3); on any error the result is marked
  failed and lands in the same rule 13.

### Injection

The final probability is `max(model probability, deterministic guard)`. The
guard is a case-insensitive phrase list: "ignore/disregard previous
instructions", "system:", "note to the assistant", claimed pre-approval or
"authorised to process", "without review/approval", "bypass/override policy",
"set the outcome", "issue/approve the refund", chargeback, legal action, sue,
attorney, lawyer. It is compared with `INJECTION_MIN_PROB` inside rule 1, and
also runs in `safe_extraction`, so a failed model call cannot hide an
injection.

### Cache and trace

- Raw responses are cached in `prepared/model_cache/` as
  `<sha256(model + endpoint + payload + ticket_id)>.json`. The key includes the
  prompt and the ticket, so a prompt change misses. `MODEL_CACHE_DISABLED=1`
  (or `use_cache=False`, or `--no-cache` in the eval) bypasses it.
- Cache hits still record the original tokens, so list-price cost is
  comparable between runs; `billed_cost_usd` counts only real calls, which is
  why a re-run is nearly free.
- `src/trace.py` records model, role, input/output tokens from the provider's
  `usage` block and computes `cost_usd` with `Decimal` from
  `EXTRACT_PRICE_INPUT_PER_MTOK` / `EXTRACT_PRICE_OUTPUT_PER_MTOK`. The single
  `float` conversion is in `Trace.to_schema()`, where the contract requires it.

### Scores (`python -m eval.score_extraction`)

Full table: `eval/results/extraction_scores.md`. Gold source:
`tests/fixtures/gold_extractions.json`; E2E baseline: `tests/output/decisions.md`
plus the 15 labels.

| Field | `decider` | `llm` |
|---|---:|---:|
| type | 29/40 | 23/40 |
| order_ids | 40/40 | 40/40 |
| affected_lines | 40/40 | 40/40 |
| qty | 36/40 | 36/40 |
| claim_kind | 30/40 | 30/40 |
| photo_damage | 40/40 | 40/40 |
| E2E vs decisions table | 36/40 | 40/40 |
| 15 labels | 13/15 | 15/15 |
| input / output tokens | 26,463 / 4,990 | 24,244 / 5,935 |
| list cost / billed (first run) | $0.02041390 / $0.01989370 | $0.02211070 / $0.00433160 |

A second run replayed every call from the cache (80/80 hits, $0 billed) and
produced identical scores, so the table above is reproducible.

`decider` E2E differences: P09 and T013 `ESCALATE` (rule 13) vs
`UNABLE_TO_VERIFY`; T014 `ESCALATE` vs `REJECT`; T016 `ESCALATE` vs `PROCEED`.
All four are the decider's own low confidence (0.51–0.59) on an ambiguous
status-vs-delivery or change-of-mind wording, so rule 13 escalates instead of
reaching rules 4–12. `llm` has no E2E differences.

Injection: recall 3/3 (T020, T021, P05) and 0 false positives on the other 37,
in both modes.

Field failures that persist, and general fixes (no ticket-specific prompt
tweaks):

- **type (both modes).** The models split on the same two boundaries:
  `ORDER_STATUS` vs `DELIVERY_ISSUE` for "where is my order?" against a
  non-delivery claim, and `RETURN` vs `REFUND` for change of mind. The
  extractor's `claim_kind` shows the same split (`ORDER_STATUS` vs
  `NOT_DELIVERED`). General fix: define the boundary in the class descriptions
  (a status question mentions no missing parcel; a non-delivery claim says the
  parcel did not arrive) and, for the decider, treat the two pairs as a
  hierarchy rather than mutually exclusive options. `decide.py` already treats
  each pair identically, so E2E is unaffected in `llm` mode.
- **qty (both modes, 4 tickets: P05, T006, T020, T021).** The model returns
  `null` where gold says 1 because those emails never state a quantity and the
  prompt says to use null when unstated. This is a convention mismatch, not a
  wrong answer: `resolve.py` maps null to 1 on a single-unit line and caps at
  the ordered qty. General fix: score null as correct when the line has ordered
  qty 1, or add an explicit "default to 1 when unstated" rule on both sides.
  Do not change the prompt just to match these four tickets.
- **claim_kind (both modes, 10 tickets).** Same status vs non-delivery split as
  `type`, plus T021 `CHANGE_OF_MIND` vs `UNSPECIFIED` (a return with no reason).
  `claim_kind` only affects damage/fee handling, and `photo_damage` is 40/40,
  so no outcome changes.

Recommended mode: **`llm`**. It has the same extraction quality, better
calibration at the 0.6 threshold (no rule-13 fallbacks), 15/15 labels and
40/40 E2E, while the decider's status/delivery ambiguity sits just under the
threshold and escalates four resolvable tickets. The decider is cheaper to
reason about (fixed choices, no prompt to drift), so it is the fallback if
calibration is tuned later.

