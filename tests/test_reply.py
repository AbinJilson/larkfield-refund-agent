"""Reply wording tests: issued refund vs a refund still being processed."""

from __future__ import annotations

import pytest

from src.normalize import normalize_ticket
from src.reply import draft_reply


def _reply(decision, ticket):
    return draft_reply(decision, normalize_ticket(ticket)).body


def test_issued_refund_says_it_has_already_been_refunded(
    tickets, gold, run_extraction, extraction_from_gold
):
    decision = run_extraction(
        tickets["T018"], extraction_from_gold(gold["T018"], "T018")
    )

    assert decision.outcome == "REJECT"
    assert decision.reason_codes == ["ALREADY_REFUNDED"]
    body = _reply(decision, tickets["T018"])
    assert "already been refunded" in body.lower()
    assert "being processed" not in body.lower()


@pytest.mark.parametrize("status", ["PROPOSED", "APPROVED"])
def test_ledger_only_refund_says_it_is_being_processed(
    status,
    tickets,
    gold,
    run_extraction,
    extraction_from_gold,
    ledger_refund,
):
    first = run_extraction(
        tickets["T002"], extraction_from_gold(gold["T002"], "T002")
    )
    assert first.outcome == "PROCEED"
    line = first.resolution.lines[0]
    entry = ledger_refund(
        run_id="run_t002",
        ticket_id="T002",
        order_id=first.resolution.order_id,
        line_no=line.line_no,
        amount=first.amount,
        status=status,
    )

    second = run_extraction(
        tickets["T027"],
        extraction_from_gold(gold["T027"], "T027"),
        ledger=[entry],
    )

    assert second.outcome == "REJECT"
    assert second.reason_codes == ["ALREADY_REFUNDED"]
    body = _reply(second, tickets["T027"])
    assert "already being processed" in body.lower()
    assert "already been refunded" not in body.lower()
    assert "has been refunded" not in body.lower()
