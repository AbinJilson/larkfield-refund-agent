"""Failure paths must never be reported as PASS or PROCEED."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.decide import process_ticket


def test_missing_shipment_is_unable_to_verify(
    make_ticket, data, config, check_by_id
):
    shipments = dict(data.shipments)
    del shipments["LF-100203"]
    data_without_shipment = replace(data, shipments=shipments)

    ticket = make_ticket(
        "F-NO-SHIPMENT",
        "My Glass vase from order LF-100203 arrived damaged.",
        "dev.costa3@mail.example",
        "2026-10-02T12:00:00+00:00",
    )
    result = process_ticket(
        ticket, data_without_shipment, config=config
    )

    assert result.outcome == "UNABLE_TO_VERIFY"
    assert result.rule_id == 7
    assert check_by_id(result, "carrier.status").result == "UNABLE_TO_VERIFY"
    assert check_by_id(result, "delivery.window").result == "UNABLE_TO_VERIFY"
    assert result.outcome != "PROCEED"


@pytest.mark.parametrize(
    ("ticket_id", "check_id"),
    [
        ("T013", "carrier.status"),
        ("P09", "carrier.status"),
        ("T022", "identity.order_exists"),
        ("P04", "identity.order_exists"),
        ("T023", "identity.customer_match"),
    ],
)
def test_failure_check_is_never_pass(
    ticket_id, check_id, tickets, run, check_by_id
):
    result = run(tickets[ticket_id])
    check = check_by_id(result, check_id)

    assert check.result != "PASS", (
        f"{ticket_id}: {check_id} returned PASS ({check.evidence})"
    )
    assert result.outcome != "PROCEED", ticket_id


def test_unavailable_carrier_is_unable_to_verify(tickets, run):
    result = run(tickets["T013"])
    assert result.outcome == "UNABLE_TO_VERIFY"
    assert result.rule_id == 4
    assert result.reason_codes == ["CARRIER_STATUS_UNAVAILABLE"]
    assert result.amount is None


def test_unknown_order_is_invalid(tickets, run):
    result = run(tickets["T022"])
    assert result.outcome == "INVALID_REQUEST"
    assert result.rule_id == 2
    assert result.reason_codes == ["ORDER_NOT_FOUND"]
    assert result.amount is None


def test_wrong_sender_is_invalid(tickets, run):
    result = run(tickets["T023"])
    assert result.outcome == "INVALID_REQUEST"
    assert result.rule_id == 3
    assert result.reason_codes == ["CUSTOMER_MISMATCH"]
    assert result.amount is None
