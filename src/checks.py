"""Policy checks for one resolved ticket.

Every check returns a schema ``Check`` with evidence (source, row key, value)
and the RP-3.2 clause it applies.  Data that cannot be obtained is
``UNABLE_TO_VERIFY``; it is never ``PASS``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from .normalize import Extraction, NormalizedTicket, get_schema
from .resolve import Resolution, ZERO

APPROVAL_LIMIT = Decimal("150.00")
LOST_AFTER_HOURS = Decimal("240")  # 10 * 24
DAY_HOURS = Decimal(24)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _hours(value: Decimal) -> str:
    return f"{value:.2f}"


def _evidence(source: str, ref: str, value: str):
    return get_schema().Evidence(source=source, ref=ref, value=value)


def _check(check_id: str, result: str, clause: str | None, evidence: list):
    return get_schema().Check(
        check_id=check_id,
        result=result,
        evidence=evidence,
        policy_clause=clause,
    )


def _order_ref(resolution: Resolution) -> str:
    return resolution.order_id or "(none)"


def _issued_evidence(resolution: Resolution, data_line=None) -> list:
    """Evidence for every ISSUED refund and every earlier ledger REFUND row."""
    evidence = []
    for line in resolution.lines:
        if line.issued_refund > ZERO:
            evidence.append(
                _evidence(
                    "prepared/refunds.csv",
                    f"{_order_ref(resolution)}:{line.line_no}",
                    f"ISSUED {_money(line.issued_refund)}",
                )
            )
        for entry in line.ledger_entries:
            evidence.append(
                _evidence(
                    "ledger",
                    f"{entry.run_id}:{entry.ticket_id}",
                    (
                        f"run {entry.run_id} ticket {entry.ticket_id}: "
                        f"{entry.status} {_money(entry.amount)} "
                        f"(order {entry.order_id} line {entry.line_no})"
                    ),
                )
            )
    return evidence


def _line_evidence(resolution: Resolution) -> list:
    evidence = []
    for line in resolution.lines:
        affected = (
            str(line.affected_qty) if line.affected_qty is not None else "unknown"
        )
        evidence.append(
            _evidence(
                "prepared/order_items.csv",
                f"{_order_ref(resolution)}:{line.line_no}",
                (
                    f"product={line.product_name}; ordered_qty={line.ordered_qty}; "
                    f"affected_qty={affected}; unit_price={_money(line.unit_price)}"
                ),
            )
        )
        remaining = (
            _money(line.remaining) if line.remaining is not None else "unknown"
        )
        evidence.append(
            _evidence(
                "computed",
                f"{_order_ref(resolution)}:{line.line_no}",
                (
                    f"issued={_money(line.issued_refund)}; "
                    f"ledger={_money(line.ledger_refund)}; remaining={remaining}"
                ),
            )
        )
    return evidence


def build_checks(
    normalized: NormalizedTicket,
    extraction: Extraction,
    resolution: Resolution,
    *,
    ticket_type_min_prob: Decimal = Decimal("0.6"),
    injection_min_prob: Decimal = Decimal("0.5"),
) -> list:
    """Build the audit trail.  Order is stable for tests and replies."""
    checks = []
    is_refund = extraction.is_refund

    # -- 4.1 identification -------------------------------------------------
    if resolution.has_issue("MULTIPLE_ORDER_IDS"):
        checks.append(
            _check(
                "identity.order_exists",
                "FAIL",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "ticket",
                        normalized.ticket_id,
                        "multiple order ids: " + ", ".join(extraction.order_ids),
                    )
                ],
            )
        )
    elif resolution.order_id is None:
        checks.append(
            _check(
                "identity.order_exists",
                "FAIL",
                "RP-3.2 4.1",
                [_evidence("ticket", normalized.ticket_id, "no order id mentioned")],
            )
        )
    elif resolution.order is None:
        checks.append(
            _check(
                "identity.order_exists",
                "FAIL",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/orders.csv",
                        resolution.order_id,
                        "order not found",
                    )
                ],
            )
        )
    else:
        checks.append(
            _check(
                "identity.order_exists",
                "PASS",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/orders.csv",
                        resolution.order_id,
                        (
                            f"customer_id={resolution.order.customer_id}; "
                            f"status={resolution.order.status}"
                        ),
                    )
                ],
            )
        )

    if resolution.order is None:
        checks.append(
            _check(
                "identity.customer_match",
                "UNABLE_TO_VERIFY",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/customers.csv",
                        _order_ref(resolution),
                        "order not found, so the customer cannot be matched",
                    )
                ],
            )
        )
    elif resolution.customer is None:
        checks.append(
            _check(
                "identity.customer_match",
                "UNABLE_TO_VERIFY",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/customers.csv",
                        resolution.order.customer_id,
                        "no customer record for the order",
                    )
                ],
            )
        )
    elif resolution.sender_matches:
        checks.append(
            _check(
                "identity.customer_match",
                "PASS",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/customers.csv",
                        resolution.customer.customer_id,
                        f"email on file={resolution.customer.email}",
                    ),
                    _evidence(
                        "ticket",
                        normalized.ticket_id,
                        f"sender={resolution.sender}",
                    ),
                ],
            )
        )
    else:
        checks.append(
            _check(
                "identity.customer_match",
                "FAIL",
                "RP-3.2 4.1",
                [
                    _evidence(
                        "prepared/customers.csv",
                        resolution.customer.customer_id,
                        f"email on file={resolution.customer.email}",
                    ),
                    _evidence(
                        "ticket",
                        normalized.ticket_id,
                        f"sender={resolution.sender}",
                    ),
                ],
            )
        )

    # -- 4.2 carrier status -------------------------------------------------
    if resolution.shipment is None:
        checks.append(
            _check(
                "carrier.status",
                "UNABLE_TO_VERIFY",
                "RP-3.2 4.2",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        "no shipment row",
                    )
                ],
            )
        )
    elif resolution.shipment.status == "UNAVAILABLE":
        checks.append(
            _check(
                "carrier.status",
                "UNABLE_TO_VERIFY",
                "RP-3.2 4.2",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        (
                            f"status=UNAVAILABLE; "
                            f"last_event_at={resolution.shipment.last_event_at or '(empty)'}"
                        ),
                    )
                ],
            )
        )
    elif resolution.shipment.last_event_at is None:
        checks.append(
            _check(
                "carrier.status",
                "UNABLE_TO_VERIFY",
                "RP-3.2 4.2",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        f"status={resolution.shipment.status}; last_event_at=(empty)",
                    )
                ],
            )
        )
    else:
        checks.append(
            _check(
                "carrier.status",
                "PASS",
                "RP-3.2 4.2",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        (
                            f"status={resolution.shipment.status}; "
                            f"shipped_at={resolution.shipment.shipped_at or '(empty)'}; "
                            f"delivered_at={resolution.shipment.delivered_at or '(empty)'}; "
                            f"last_event_at={resolution.shipment.last_event_at}"
                        ),
                    )
                ],
            )
        )

    # -- 2.1 / 2.3 / 2.6 delivery window -----------------------------------
    if not is_refund:
        checks.append(
            _check(
                "delivery.window",
                "NOT_APPLICABLE",
                "RP-3.2 2.1",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    elif not resolution.delivery_date_known:
        checks.append(
            _check(
                "delivery.window",
                "UNABLE_TO_VERIFY",
                "RP-3.2 2.1",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        "no delivery date recorded",
                    )
                ],
            )
        )
    elif resolution.hours_since_delivery is None:
        checks.append(
            _check(
                "delivery.window",
                "UNABLE_TO_VERIFY",
                "RP-3.2 2.1",
                [
                    _evidence(
                        "ticket",
                        normalized.ticket_id,
                        f"received_at={normalized.received_at}",
                    )
                ],
            )
        )
    else:
        out_of_window = (
            resolution.hours_since_delivery > resolution.window_hours
        )
        checks.append(
            _check(
                "delivery.window",
                "FAIL" if out_of_window else "PASS",
                "RP-3.2 2.1, 2.3, 2.6",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        f"{_order_ref(resolution)}.delivered_at",
                        resolution.shipment.delivered_at or "(empty)",
                    ),
                    _evidence(
                        "ticket",
                        f"{normalized.ticket_id}.received_at",
                        normalized.received_at,
                    ),
                    _evidence(
                        "computed",
                        normalized.ticket_id,
                        (
                            f"elapsed_hours={_hours(resolution.hours_since_delivery)}; "
                            f"window_days={int(resolution.window_hours / DAY_HOURS)}"
                        ),
                    ),
                ],
            )
        )

    # -- 2.2 / 2.5 affected lines ------------------------------------------
    if not is_refund:
        checks.append(
            _check(
                "refund.affected_lines",
                "NOT_APPLICABLE",
                "RP-3.2 2.2",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    elif not resolution.lines:
        if resolution.has_issue("PRODUCT_AMBIGUOUS"):
            result = "UNABLE_TO_VERIFY"
            detail = "product not named and the order has more than one line"
        elif resolution.has_issue("PRODUCT_NOT_IN_ORDER"):
            result = "UNABLE_TO_VERIFY"
            detail = "the named product is not on the order"
        else:
            result = "FAIL"
            detail = "no affected line could be resolved"
        checks.append(
            _check(
                "refund.affected_lines",
                result,
                "RP-3.2 2.2",
                [
                    _evidence(
                        "prepared/order_items.csv",
                        _order_ref(resolution),
                        detail,
                    )
                ],
            )
        )
    else:
        checks.append(
            _check(
                "refund.affected_lines",
                "PASS",
                "RP-3.2 2.2",
                _line_evidence(resolution),
            )
        )

    if not is_refund:
        checks.append(
            _check(
                "refund.quantity",
                "NOT_APPLICABLE",
                "RP-3.2 2.5",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    elif any(line.affected_qty is None for line in resolution.lines):
        checks.append(
            _check(
                "refund.quantity",
                "UNABLE_TO_VERIFY",
                "RP-3.2 2.5",
                [
                    _evidence(
                        "prepared/order_items.csv",
                        _order_ref(resolution),
                        "affected quantity is unknown on a line with qty > 1",
                    )
                ],
            )
        )
    else:
        evidence = _line_evidence(resolution)
        if resolution.has_issue("AFFECTED_QTY_CAPPED"):
            evidence.append(
                _evidence(
                    "computed",
                    _order_ref(resolution),
                    "requested qty capped at the ordered qty (2.5)",
                )
            )
        checks.append(
            _check("refund.quantity", "PASS", "RP-3.2 2.5", evidence)
        )

    # -- 2.5 prior refunds --------------------------------------------------
    if not is_refund:
        checks.append(
            _check(
                "refund.prior_refunds",
                "NOT_APPLICABLE",
                "RP-3.2 2.5",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    elif not resolution.lines or resolution.total_remaining is None:
        checks.append(
            _check(
                "refund.prior_refunds",
                "UNABLE_TO_VERIFY",
                "RP-3.2 2.5",
                [
                    _evidence(
                        "computed",
                        _order_ref(resolution),
                        "remaining amount cannot be computed",
                    )
                ],
            )
        )
    else:
        evidence = _issued_evidence(resolution) + _line_evidence(resolution)
        checks.append(
            _check(
                "refund.prior_refunds",
                "FAIL" if resolution.total_remaining == ZERO else "PASS",
                "RP-3.2 2.5",
                evidence,
            )
        )

    # -- 3.1 / 3.2 amount limit --------------------------------------------
    if not is_refund:
        checks.append(
            _check(
                "refund.amount_limit",
                "NOT_APPLICABLE",
                "RP-3.2 3.1",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    elif resolution.total_remaining is None:
        checks.append(
            _check(
                "refund.amount_limit",
                "UNABLE_TO_VERIFY",
                "RP-3.2 3.1",
                [
                    _evidence(
                        "computed",
                        _order_ref(resolution),
                        "remaining amount cannot be computed",
                    )
                ],
            )
        )
    else:
        over = resolution.total_remaining > APPROVAL_LIMIT
        checks.append(
            _check(
                "refund.amount_limit",
                "FAIL" if over else "PASS",
                "RP-3.2 3.2" if over else "RP-3.2 3.1",
                [
                    _evidence(
                        "computed",
                        normalized.ticket_id,
                        (
                            f"amount={_money(resolution.total_remaining)}; "
                            f"limit={_money(APPROVAL_LIMIT)}"
                        ),
                    )
                ],
            )
        )

    # -- 2.4 / 2.5 scope ----------------------------------------------------
    if not is_refund:
        checks.append(
            _check(
                "refund.scope",
                "NOT_APPLICABLE",
                "RP-3.2 2.4",
                [_evidence("ticket", normalized.ticket_id, "not a refund/return")],
            )
        )
    else:
        scope_reasons = []
        if extraction.asks_whole_order:
            scope_reasons.append("customer asked for the whole order")
        if extraction.claims_fee:
            scope_reasons.append("customer asked for the delivery/shipping fee")
        if resolution.any_prior_refund:
            scope_reasons.append("a line already has a prior refund")
        checks.append(
            _check(
                "refund.scope",
                "FAIL" if scope_reasons else "PASS",
                "RP-3.2 2.4, 2.5" if scope_reasons else "RP-3.2 2.4",
                [
                    _evidence(
                        "ticket",
                        normalized.ticket_id,
                        "; ".join(scope_reasons) if scope_reasons else "within affected lines",
                    )
                ],
            )
        )

    # -- 5.1 movement -------------------------------------------------------
    if resolution.shipment is None:
        checks.append(
            _check(
                "carrier.movement",
                "UNABLE_TO_VERIFY",
                "RP-3.2 5.1",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        "no shipment row",
                    )
                ],
            )
        )
    elif resolution.shipment.status == "DELIVERED":
        checks.append(
            _check(
                "carrier.movement",
                "NOT_APPLICABLE",
                "RP-3.2 5.1",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        "carrier status DELIVERED; the 5.1 movement test does not apply",
                    )
                ],
            )
        )
    elif resolution.hours_since_last_event is None:
        checks.append(
            _check(
                "carrier.movement",
                "UNABLE_TO_VERIFY",
                "RP-3.2 5.1",
                [
                    _evidence(
                        "prepared/shipments.csv",
                        _order_ref(resolution),
                        "last_event_at is empty",
                    )
                ],
            )
        )
    else:
        evidence = [
            _evidence(
                "prepared/shipments.csv",
                f"{_order_ref(resolution)}.last_event_at",
                resolution.shipment.last_event_at or "(empty)",
            ),
            _evidence(
                "ticket",
                f"{normalized.ticket_id}.received_at",
                normalized.received_at,
            ),
            _evidence(
                "computed",
                normalized.ticket_id,
                (
                    f"elapsed_hours={_hours(resolution.hours_since_last_event)}; "
                    f"threshold_hours={LOST_AFTER_HOURS}"
                ),
            ),
        ]
        if resolution.last_event_before_shipped:
            evidence.append(
                _evidence(
                    "prepared/shipments.csv",
                    f"{_order_ref(resolution)}.last_event_at",
                    (
                        f"{resolution.shipment.last_event_at} (earlier than "
                        f"shipped_at {resolution.shipment.shipped_at}; the 5.1 "
                        "test uses last_event_at as recorded, DECIDED ruling 3)"
                    ),
                )
            )
        over = resolution.hours_since_last_event > LOST_AFTER_HOURS
        checks.append(
            _check(
                "carrier.movement",
                "FAIL" if over else "PASS",
                "RP-3.2 5.1",
                evidence,
            )
        )

    # -- 5.2 injection ------------------------------------------------------
    injection = (
        Decimal(str(extraction.injection_probability)) >= injection_min_prob
    )
    checks.append(
        _check(
            "policy.injection",
            "FAIL" if injection else "PASS",
            "RP-3.2 5.2",
            [
                _evidence(
                    "ticket",
                    normalized.ticket_id,
                    f"injection_probability={Decimal(str(extraction.injection_probability)):.2f}; "
                    f"threshold={injection_min_prob:.2f}",
                )
            ],
        )
    )

    # -- classification -----------------------------------------------------
    type_known = extraction.ticket_type != "OTHER"
    confident = (
        Decimal(str(extraction.ticket_type_confidence)) >= ticket_type_min_prob
    )
    checks.append(
        _check(
            "ticket.type",
            "PASS" if type_known and confident else "UNABLE_TO_VERIFY",
            None,
            [
                _evidence(
                    "ticket",
                    normalized.ticket_id,
                    (
                        f"type={extraction.ticket_type}; "
                        f"confidence={Decimal(str(extraction.ticket_type_confidence)):.2f}; "
                        f"threshold={ticket_type_min_prob:.2f}"
                    ),
                )
            ],
        )
    )

    return checks
