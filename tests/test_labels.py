"""All 15 labelled tickets: outcome, approver, approval flag, amount, reasons."""

from __future__ import annotations

from decimal import Decimal

import pytest

CENT = Decimal("0.01")


def test_label_fixture_has_15_rows(labels):
    assert len(labels) == 15


@pytest.mark.parametrize(
    "ticket_id",
    [
        "T001",
        "T002",
        "T003",
        "T004",
        "T005",
        "T006",
        "T008",
        "T011",
        "T013",
        "T014",
        "T018",
        "T019",
        "T020",
        "T021",
        "T026",
    ],
)
def test_labelled_ticket(ticket_id, tickets, labels, run):
    label = labels[ticket_id]
    result = run(tickets[ticket_id])

    assert result.outcome == label["expected_outcome"], (
        f"{ticket_id}: rule {result.rule_id} gave {result.outcome}, "
        f"expected {label['expected_outcome']} ({result.basis})"
    )
    assert result.approver_role == label["expected_approver_role"]
    assert result.human_approval_required is label["expected_human_approval_required"]

    expected_amount = label.get("expected_amount_usd")
    if expected_amount is not None:
        amount = Decimal(str(expected_amount)).quantize(CENT)
        assert result.ledger_action.action == "REFUND"
        assert result.ledger_action.amount == amount
        assert result.amount == amount
    elif result.outcome in {"REJECT", "ESCALATE", "INVALID_REQUEST", "UNABLE_TO_VERIFY"}:
        assert result.amount is None
        assert result.ledger_action.action != "REFUND"

    expected_reasons = label.get("expected_reason_codes")
    if expected_reasons is not None:
        assert result.reason_codes == expected_reasons
