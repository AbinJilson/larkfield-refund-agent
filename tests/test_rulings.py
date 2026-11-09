"""One test per DECIDED ruling in NOTES.md section 5."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest


# Ruling 1: re-sent tickets / ledger deduction ------------------------------


def test_re_sent_ticket_deducts_earlier_ledger_refund(
    tickets, run, ledger_refund, check_by_id
):
    first = run(tickets["T002"])
    assert first.outcome == "PROCEED"
    assert first.amount == Decimal("129.00")
    assert first.ledger_action.action == "REFUND"

    prior = ledger_refund(
        run_id="run-t002",
        ticket_id="T002",
        order_id="LF-100201",
        line_no=1,
        amount=Decimal("129.00"),
        status="PROPOSED",
    )
    resent = run(tickets["T027"], ledger=[prior])

    assert resent.outcome == "REJECT"
    assert resent.reason_codes == ["ALREADY_REFUNDED"]
    assert resent.amount is None
    assert resent.ledger_action.action == "REPLY"
    assert resent.resolution.total_remaining == Decimal("0.00")

    prior_check = check_by_id(resent, "refund.prior_refunds")
    assert prior_check.result == "FAIL"
    joined = " | ".join(entry.value for entry in prior_check.evidence)
    assert "run-t002" in joined
    assert "T002" in joined


def test_rejected_ledger_refund_does_not_deduct(
    tickets, run, ledger_refund
):
    prior = ledger_refund(
        run_id="run-rejected",
        ticket_id="T002",
        order_id="LF-100201",
        line_no=1,
        amount=Decimal("129.00"),
        status="REJECTED",
    )
    result = run(tickets["T027"], ledger=[prior])
    assert result.outcome == "PROCEED"
    assert result.amount == Decimal("129.00")


def test_ledger_entry_for_another_line_does_not_deduct(
    tickets, run, ledger_refund
):
    prior = ledger_refund(
        run_id="run-other-line",
        ticket_id="T002",
        order_id="LF-100201",
        line_no=2,
        amount=Decimal("129.00"),
        status="PROPOSED",
    )
    result = run(tickets["T027"], ledger=[prior])
    assert result.outcome == "PROCEED"
    assert result.amount == Decimal("129.00")


# Ruling 2: change of mind inside 30 days ----------------------------------


def test_change_of_mind_within_30_days_proceeds_at_line_value(tickets, run):
    for ticket_id, amount in (
        ("T015", "24.00"),
        ("T016", "129.00"),
        ("P07", "45.00"),
    ):
        result = run(tickets[ticket_id])
        assert result.outcome == "PROCEED", ticket_id
        assert result.rule_id == 12, ticket_id
        assert result.amount == Decimal(amount), ticket_id


def test_change_of_mind_at_45_days_still_uses_the_30_day_window(tickets, run):
    for ticket_id in ("T017", "P10"):
        result = run(tickets[ticket_id])
        assert result.outcome == "REJECT", ticket_id
        assert result.rule_id == 8, ticket_id
        assert result.reason_codes == ["OUT_OF_WINDOW"], ticket_id
        assert result.resolution.hours_since_delivery == Decimal("1080"), ticket_id


# Ruling 3: last_event_at before shipped_at --------------------------------


def test_last_event_before_shipped_is_used_and_evidenced(
    tickets, run, check_by_id
):
    for ticket_id, expected_hours in (
        ("T011", "384"),
        ("T012", "408"),
        ("P06", "360"),
    ):
        result = run(tickets[ticket_id])
        assert result.outcome == "ESCALATE", ticket_id
        assert result.rule_id == 5, ticket_id
        assert result.approver_role == "CARRIER_CLAIMS", ticket_id
        assert result.resolution.last_event_before_shipped is True, ticket_id
        assert result.resolution.hours_since_last_event == Decimal(
            expected_hours
        ), ticket_id

        movement = check_by_id(result, "carrier.movement")
        assert movement.result == "FAIL", ticket_id
        joined = " | ".join(entry.value for entry in movement.evidence)
        assert "earlier than shipped_at" in joined, ticket_id


# Ruling 4: exact elapsed time for every day threshold ----------------------


def test_exact_elapsed_time_for_day_thresholds(tickets, run, check_by_id):
    result = run(tickets["T024"])
    assert result.outcome == "ESCALATE"
    assert result.rule_id == 5
    assert result.reason_codes == ["LOST_IN_TRANSIT"]
    assert result.resolution.hours_since_last_event == Decimal("248")

    movement = check_by_id(result, "carrier.movement")
    assert movement.result == "FAIL"
    assert "elapsed_hours=248.00" in " | ".join(
        entry.value for entry in movement.evidence
    )

    window = check_by_id(run(tickets["T017"]), "delivery.window")
    assert window.result == "FAIL"
    assert "elapsed_hours=1080.00" in " | ".join(
        entry.value for entry in window.evidence
    )


# Ruling 5: shipping/delivery fee requests ---------------------------------


def test_fee_request_refunds_only_the_affected_line(
    tickets, run, check_by_id
):
    result = run(tickets["T025"])
    assert result.outcome == "PROCEED_MODIFIED"
    assert result.rule_id == 11
    assert result.reason_codes == ["PARTIAL_LINES"]
    assert result.amount == Decimal("45.00")
    assert result.approver_role is None

    scope = check_by_id(result, "refund.scope")
    assert scope.result == "FAIL"
    assert "delivery/shipping fee" in " | ".join(
        entry.value for entry in scope.evidence
    )


# Ruling 6: no product named, type OTHER and low confidence ----------------


def test_no_product_named_uses_a_single_line_order(make_ticket, run):
    ticket = make_ticket(
        "X-SINGLE",
        "Hello, my item from order LF-100203 is broken, please refund.",
        "dev.costa3@mail.example",
        "2026-10-02T12:00:00+00:00",
    )
    result = run(ticket)

    assert result.outcome == "PROCEED"
    assert result.rule_id == 12
    assert result.amount == Decimal("45.00")
    assert result.resolution.product_named is False
    assert "PRODUCT_NOT_NAMED_SINGLE_LINE" in result.resolution.issues


def test_no_product_named_on_a_multi_line_order_is_unable_to_verify(
    make_ticket, run, check_by_id
):
    ticket = make_ticket(
        "X-MULTI",
        "Hello, my item from order LF-100218 is broken, please refund.",
        "sara.lee18@mail.example",
        "2026-10-10T09:00:00+00:00",
    )
    result = run(ticket)

    assert result.outcome == "UNABLE_TO_VERIFY"
    assert result.rule_id == 14
    assert result.reason_codes == []
    assert "PRODUCT_AMBIGUOUS" in result.resolution.issues
    assert check_by_id(result, "refund.affected_lines").result == "UNABLE_TO_VERIFY"


def test_type_other_escalates_to_support_lead(make_ticket, run):
    ticket = make_ticket(
        "X-OTHER",
        "Hello, can you help me with order LF-100203?",
        "dev.costa3@mail.example",
        "2026-10-02T12:00:00+00:00",
    )
    result = run(ticket)

    assert result.ticket_type == "OTHER"
    assert result.outcome == "ESCALATE"
    assert result.rule_id == 13
    assert result.approver_role == "SUPPORT_LEAD"
    assert result.human_approval_required is True
    assert result.amount is None


def test_low_classification_confidence_escalates_to_support_lead(
    tickets, gold, extraction_from_gold, run_extraction
):
    entry = dict(gold["T002"])
    entry["ticket_type_confidence"] = 0.5
    result = run_extraction(
        tickets["T002"], extraction_from_gold(entry, "T002")
    )

    assert result.outcome == "ESCALATE"
    assert result.rule_id == 13
    assert result.approver_role == "SUPPORT_LEAD"
    assert result.amount is None
