"""Exact-threshold boundaries: 30 days, 45 days, $150 and 10 days."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

DELIVERED_203 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
DELIVERED_200 = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
LAST_EVENT_208 = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)


def _ts(value: datetime) -> str:
    return value.isoformat()


def test_30_days_exactly_is_inside(make_ticket, run):
    ticket = make_ticket(
        "B-30D",
        "My Glass vase from order LF-100203 arrived damaged.",
        "dev.costa3@mail.example",
        _ts(DELIVERED_203 + timedelta(days=30)),
    )
    result = run(ticket)

    assert result.outcome == "PROCEED"
    assert result.rule_id == 12
    assert result.amount == Decimal("45.00")
    assert result.resolution.hours_since_delivery == Decimal("720")
    assert result.resolution.window_hours == Decimal("720")


def test_30_days_plus_one_second_is_outside(make_ticket, run):
    ticket = make_ticket(
        "B-30D-1S",
        "My Glass vase from order LF-100203 arrived damaged.",
        "dev.costa3@mail.example",
        _ts(DELIVERED_203 + timedelta(days=30, seconds=1)),
    )
    result = run(ticket)

    assert result.outcome == "REJECT"
    assert result.rule_id == 8
    assert result.reason_codes == ["OUT_OF_WINDOW"]
    assert result.resolution.hours_since_delivery > Decimal("720")


def test_45_days_with_photo_is_inside_and_gets_damage_exception(
    make_ticket, run
):
    ticket = make_ticket(
        "B-45D",
        "My Glass vase from order LF-100203 arrived damaged (photo attached).",
        "dev.costa3@mail.example",
        _ts(DELIVERED_203 + timedelta(days=45)),
        image=True,
    )
    result = run(ticket)

    assert result.outcome == "PROCEED"
    assert result.rule_id == 12
    assert result.reason_codes == ["DAMAGE_EXCEPTION"]
    assert result.amount == Decimal("45.00")
    assert result.resolution.hours_since_delivery == Decimal("1080")
    assert result.resolution.window_hours == Decimal("1080")


def test_45_days_plus_one_second_with_photo_is_outside(make_ticket, run):
    ticket = make_ticket(
        "B-45D-1S",
        "My Glass vase from order LF-100203 arrived damaged (photo attached).",
        "dev.costa3@mail.example",
        _ts(DELIVERED_203 + timedelta(days=45, seconds=1)),
        image=True,
    )
    result = run(ticket)

    assert result.outcome == "REJECT"
    assert result.rule_id == 8
    assert result.reason_codes == ["OUT_OF_WINDOW"]
    assert result.resolution.hours_since_delivery > Decimal("1080")


def test_amount_exactly_150_is_inside_the_no_approval_band(
    make_ticket, run, ledger_refund
):
    ticket = make_ticket(
        "B-150",
        "My Round wall mirror from order LF-100200 arrived damaged (photo attached).",
        "asha.shah0@mail.example",
        _ts(DELIVERED_200 + timedelta(days=10)),
        image=True,
    )
    prior = ledger_refund(
        run_id="run-prior",
        ticket_id="T-PRIOR",
        order_id="LF-100200",
        line_no=1,
        amount=Decimal("60.00"),
        status="PROPOSED",
    )
    result = run(ticket, ledger=[prior])

    assert result.amount == Decimal("150.00")
    assert result.outcome == "PROCEED_MODIFIED"
    assert result.approver_role is None
    assert result.human_approval_required is False


def test_amount_one_cent_over_150_needs_approval(
    make_ticket, run, ledger_refund
):
    ticket = make_ticket(
        "B-150-01",
        "My Round wall mirror from order LF-100200 arrived damaged (photo attached).",
        "asha.shah0@mail.example",
        _ts(DELIVERED_200 + timedelta(days=10)),
        image=True,
    )
    prior = ledger_refund(
        run_id="run-prior",
        ticket_id="T-PRIOR",
        order_id="LF-100200",
        line_no=1,
        amount=Decimal("59.99"),
        status="PROPOSED",
    )
    result = run(ticket, ledger=[prior])

    assert result.amount == Decimal("150.01")
    assert result.outcome == "PEND"
    assert result.rule_id == 10
    assert result.approver_role == "SUPPORT_LEAD"
    assert result.human_approval_required is True


def test_10_days_exactly_is_not_lost(make_ticket, run):
    ticket = make_ticket(
        "B-10D",
        "Hi, can you tell me where order LF-100208 is? It hasn't arrived yet.",
        "isla.bose8@mail.example",
        _ts(LAST_EVENT_208 + timedelta(days=10)),
    )
    result = run(ticket)

    assert result.outcome == "PROCEED"
    assert result.rule_id == 6
    assert result.resolution.hours_since_last_event == Decimal("240")


def test_10_days_plus_one_second_is_lost(make_ticket, run):
    ticket = make_ticket(
        "B-10D-1S",
        "Hi, can you tell me where order LF-100208 is? It hasn't arrived yet.",
        "isla.bose8@mail.example",
        _ts(LAST_EVENT_208 + timedelta(days=10, seconds=1)),
    )
    result = run(ticket)

    assert result.outcome == "ESCALATE"
    assert result.rule_id == 5
    assert result.reason_codes == ["LOST_IN_TRANSIT"]
    assert result.approver_role == "CARRIER_CLAIMS"
    assert result.resolution.hours_since_last_event > Decimal("240")
