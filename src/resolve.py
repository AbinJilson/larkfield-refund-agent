"""Resolve an :class:`~src.normalize.Extraction` against ``prepared/``.

No model calls and no HTTP.  Money is ``Decimal`` from the CSV cell to the
returned remaining amount.  The ticket text is never used to choose an
outcome; it only tells this module which order and which product lines the
customer is pointing at.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .normalize import (
    Extraction,
    NormalizedTicket,
    REFUND_CLAIMS,
    STATUS_CLAIMS,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DAY_HOURS = Decimal(24)


# ---------------------------------------------------------------------------
# Small value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    status: str
    order_total: Decimal
    shipping_fee: Decimal
    carrier_status: str


@dataclass(frozen=True)
class OrderLine:
    order_id: str
    line_no: int
    product_name: str
    qty: int
    unit_price: Decimal
    line_value: Decimal
    refunded_issued: Decimal


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    order_id: str
    carrier: str
    status: str
    shipped_at: str | None
    delivered_at: str | None
    last_event_at: str | None
    carrier_status_available: bool


@dataclass(frozen=True)
class PriorRefund:
    refund_id: str
    order_id: str
    line_no: int
    amount: Decimal
    status: str


@dataclass(frozen=True)
class LedgerRefund:
    """An earlier run's REFUND row, passed in by the caller.

    The schema's ``LedgerEntry`` has no order/line column, so the internal
    ledger row carries them.  ``amount`` is ``Decimal``; the persistence layer
    converts to the schema's float only at the boundary.
    """

    run_id: str
    ticket_id: str
    order_id: str
    line_no: int
    amount: Decimal
    status: str


@dataclass(frozen=True)
class PreparedData:
    customers: Mapping[str, Customer]
    orders: Mapping[str, Order]
    lines_by_order: Mapping[str, tuple[OrderLine, ...]]
    shipments: Mapping[str, Shipment]
    refunds: tuple[PriorRefund, ...]
    refunds_by_line: Mapping[tuple[str, int], Decimal]

    @property
    def product_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for lines in self.lines_by_order.values():
            for line in lines:
                if line.product_name not in names:
                    names.append(line.product_name)
        return tuple(names)

    @classmethod
    def load(cls, prepared_dir: str | Path | None = None) -> "PreparedData":
        root = Path(
            prepared_dir or os.getenv("PREPARED_DIR", "prepared")
        )
        return cls(
            customers=_customers(root / "customers.csv"),
            orders=_orders(root / "orders.csv"),
            lines_by_order=_lines(root / "order_items.csv"),
            shipments=_shipments(root / "shipments.csv"),
            refunds=_refunds(root / "refunds.csv"),
            refunds_by_line=_refunds_by_line(root / "refunds_by_line.csv"),
        )


# ---------------------------------------------------------------------------
# CSV readers
# ---------------------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"missing prepared table: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _money(value: str | None) -> Decimal:
    if value is None or value.strip() == "":
        return ZERO
    return Decimal(value.strip())


def _optional_text(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _customers(path: Path) -> dict[str, Customer]:
    return {
        row["customer_id"]: Customer(
            customer_id=row["customer_id"],
            name=row.get("name", ""),
            email=row.get("email", "").strip().lower(),
        )
        for row in _rows(path)
    }


def _orders(path: Path) -> dict[str, Order]:
    return {
        row["order_id"]: Order(
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            status=row["status"],
            order_total=_money(row.get("order_total")),
            shipping_fee=_money(row.get("shipping_fee")),
            carrier_status=row.get("carrier_status", ""),
        )
        for row in _rows(path)
    }


def _lines(path: Path) -> dict[str, tuple[OrderLine, ...]]:
    grouped: dict[str, list[OrderLine]] = {}
    for row in _rows(path):
        order_id = row["order_id"]
        grouped.setdefault(order_id, []).append(
            OrderLine(
                order_id=order_id,
                line_no=int(row["line_no"]),
                product_name=row["product_name"],
                qty=int(row["qty"]),
                unit_price=_money(row.get("unit_price")),
                line_value=_money(row.get("line_value")),
                refunded_issued=_money(row.get("refunded_issued")),
            )
        )
    return {
        order_id: tuple(sorted(lines, key=lambda line: line.line_no))
        for order_id, lines in grouped.items()
    }


def _shipments(path: Path) -> dict[str, Shipment]:
    return {
        row["order_id"]: Shipment(
            shipment_id=row["shipment_id"],
            order_id=row["order_id"],
            carrier=row.get("carrier", ""),
            status=row.get("status", ""),
            shipped_at=_optional_text(row.get("shipped_at")),
            delivered_at=_optional_text(row.get("delivered_at")),
            last_event_at=_optional_text(row.get("last_event_at")),
            carrier_status_available=row.get("carrier_status_available", "").lower()
            == "true",
        )
        for row in _rows(path)
    }


def _refunds(path: Path) -> tuple[PriorRefund, ...]:
    return tuple(
        PriorRefund(
            refund_id=row["refund_id"],
            order_id=row["order_id"],
            line_no=int(row["line_no"]),
            amount=_money(row.get("amount")),
            status=row.get("status", ""),
        )
        for row in _rows(path)
    )


def _refunds_by_line(path: Path) -> dict[tuple[str, int], Decimal]:
    if not path.is_file():
        return {}
    return {
        (row["order_id"], int(row["line_no"])): _money(row.get("refunded_issued"))
        for row in _rows(path)
    }


# ---------------------------------------------------------------------------
# Time helpers (no floats)
# ---------------------------------------------------------------------------


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_hours(later: datetime, earlier: datetime) -> Decimal:
    """Exact elapsed hours as a Decimal, without ``timedelta.total_seconds``."""
    delta = later - earlier
    seconds = Decimal(delta.days * 86400 + delta.seconds) + (
        Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    return seconds / Decimal(3600)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLine:
    line_no: int
    product_name: str
    unit_price: Decimal
    ordered_qty: int
    affected_qty: int | None
    issued_refund: Decimal
    ledger_refund: Decimal
    remaining: Decimal | None
    ledger_entries: tuple[LedgerRefund, ...] = ()


@dataclass(frozen=True)
class Resolution:
    ticket_id: str
    order_id: str | None
    order: Order | None
    customer: Customer | None
    shipment: Shipment | None
    lines: tuple[ResolvedLine, ...]
    issues: tuple[str, ...]
    sender: str
    sender_matches: bool | None
    product_named: bool
    hours_since_delivery: Decimal | None
    hours_since_last_event: Decimal | None
    last_event_before_shipped: bool
    window_hours: Decimal
    total_remaining: Decimal | None

    @property
    def order_exists(self) -> bool:
        return self.order is not None

    @property
    def customer_mismatch(self) -> bool:
        return self.customer is None or self.sender_matches is False

    @property
    def carrier_status_known(self) -> bool:
        return (
            self.shipment is not None
            and self.shipment.status != "UNAVAILABLE"
            and self.shipment.last_event_at is not None
        )

    @property
    def delivery_date_known(self) -> bool:
        return self.shipment is not None and self.shipment.delivered_at is not None

    @property
    def affected_qty_known(self) -> bool:
        return all(line.affected_qty is not None for line in self.lines)

    @property
    def any_prior_refund(self) -> bool:
        return any(
            line.issued_refund > ZERO or line.ledger_refund > ZERO
            for line in self.lines
        )

    @property
    def all_lines_refunded(self) -> bool:
        return bool(self.lines) and self.total_remaining == ZERO

    def has_issue(self, code: str) -> bool:
        return code in self.issues


def _add_issue(issues: list[str], code: str) -> None:
    if code not in issues:
        issues.append(code)


def _matching_lines(
    wanted_product: str | None, order_lines: Sequence[OrderLine]
) -> tuple[list[OrderLine], str | None]:
    if wanted_product is None:
        if len(order_lines) == 1:
            return [order_lines[0]], "PRODUCT_NOT_NAMED_SINGLE_LINE"
        return [], "PRODUCT_AMBIGUOUS"
    exact = [
        line
        for line in order_lines
        if line.product_name.casefold() == wanted_product.casefold()
    ]
    if exact:
        return exact, None
    contains = [
        line
        for line in order_lines
        if wanted_product.casefold() in line.product_name.casefold()
        or line.product_name.casefold() in wanted_product.casefold()
    ]
    if contains:
        return contains, None
    return [], "PRODUCT_NOT_IN_ORDER"


def _resolve_lines(
    extraction: Extraction, order: Order, data: PreparedData, ledger: Sequence[LedgerRefund]
) -> tuple[tuple[ResolvedLine, ...], tuple[str, ...]]:
    order_lines = data.lines_by_order.get(order.order_id, ())
    issues: list[str] = []
    resolved: list[ResolvedLine] = []
    seen: set[int] = set()

    for wanted in extraction.affected_lines:
        if wanted.product is None:
            # DECIDED ruling 6: no product named -> single-line order only.
            matching, issue = _matching_lines(None, order_lines)
        else:
            matching, issue = _matching_lines(wanted.product, order_lines)
        if issue:
            _add_issue(issues, issue)
        if not matching:
            continue

        for line in matching:
            if line.line_no in seen:
                continue
            seen.add(line.line_no)

            affected_qty: int | None
            if wanted.qty is None:
                if line.qty == 1:
                    affected_qty = 1
                else:
                    _add_issue(issues, "AFFECTED_QTY_UNKNOWN")
                    affected_qty = None
            elif wanted.qty > line.qty:
                _add_issue(issues, "AFFECTED_QTY_CAPPED")
                affected_qty = line.qty
            else:
                affected_qty = wanted.qty

            entries = tuple(
                entry
                for entry in ledger
                if entry.order_id == order.order_id
                and entry.line_no == line.line_no
                and entry.status != "REJECTED"
            )
            ledger_total = sum((entry.amount for entry in entries), ZERO)
            issued = line.refunded_issued

            if affected_qty is None:
                remaining = None
            else:
                gross = (line.unit_price * affected_qty).quantize(CENT)
                remaining = gross - issued - ledger_total
                if remaining < ZERO:
                    remaining = ZERO

            resolved.append(
                ResolvedLine(
                    line_no=line.line_no,
                    product_name=line.product_name,
                    unit_price=line.unit_price,
                    ordered_qty=line.qty,
                    affected_qty=affected_qty,
                    issued_refund=issued,
                    ledger_refund=ledger_total,
                    remaining=remaining,
                    ledger_entries=entries,
                )
            )

    return tuple(resolved), tuple(issues)


def resolve(
    extraction: Extraction,
    normalized: NormalizedTicket,
    data: PreparedData,
    ledger: Sequence[LedgerRefund] = (),
) -> Resolution:
    """Look up the order, customer, shipment and affected lines."""
    issues: list[str] = []
    order: Order | None = None
    order_id: str | None = None

    if len(extraction.order_ids) == 0:
        _add_issue(issues, "NO_ORDER_ID")
    elif len(extraction.order_ids) > 1:
        _add_issue(issues, "MULTIPLE_ORDER_IDS")
    else:
        order_id = extraction.order_ids[0]
        order = data.orders.get(order_id)
        if order is None:
            _add_issue(issues, "ORDER_NOT_FOUND")

    customer = data.customers.get(order.customer_id) if order else None
    sender_matches: bool | None = None
    if customer is not None:
        sender_matches = normalized.sender == customer.email.strip().lower()
        if not sender_matches:
            _add_issue(issues, "CUSTOMER_MISMATCH")

    shipment = data.shipments.get(order_id) if order_id else None
    if order is not None and shipment is None:
        _add_issue(issues, "SHIPMENT_NOT_FOUND")
    if shipment is not None and shipment.status == "UNAVAILABLE":
        _add_issue(issues, "CARRIER_STATUS_UNAVAILABLE")
    if shipment is not None and shipment.last_event_at is None:
        _add_issue(issues, "LAST_EVENT_MISSING")
    if shipment is not None and shipment.delivered_at is None:
        _add_issue(issues, "DELIVERED_AT_MISSING")

    lines: tuple[ResolvedLine, ...] = ()
    if order is not None and extraction.claim_kind not in STATUS_CLAIMS:
        lines, line_issues = _resolve_lines(extraction, order, data, ledger)
        for code in line_issues:
            _add_issue(issues, code)
        if not lines and "PRODUCT_AMBIGUOUS" not in issues:
            _add_issue(issues, "NO_AFFECTED_LINE")

    received_at = parse_timestamp(normalized.received_at)
    shipped_at = parse_timestamp(shipment.shipped_at) if shipment else None
    delivered_at = parse_timestamp(shipment.delivered_at) if shipment else None
    last_event_at = parse_timestamp(shipment.last_event_at) if shipment else None

    hours_since_delivery = (
        elapsed_hours(received_at, delivered_at)
        if received_at and delivered_at
        else None
    )
    hours_since_last_event = (
        elapsed_hours(received_at, last_event_at)
        if received_at and last_event_at
        else None
    )
    last_event_before_shipped = bool(
        shipped_at and last_event_at and last_event_at < shipped_at
    )

    window_days = (
        45
        if extraction.claim_kind in {"DAMAGED", "FAULTY"}
        and normalized.has_image_attachment
        else 30
    )
    window_hours = Decimal(window_days) * DAY_HOURS

    total_remaining: Decimal | None
    if not lines or any(line.remaining is None for line in lines):
        total_remaining = None
    else:
        total_remaining = sum(
            (line.remaining for line in lines if line.remaining is not None), ZERO
        ).quantize(CENT)

    return Resolution(
        ticket_id=extraction.ticket_id,
        order_id=order_id,
        order=order,
        customer=customer,
        shipment=shipment,
        lines=lines,
        issues=tuple(issues),
        sender=normalized.sender,
        sender_matches=sender_matches,
        product_named=bool(
            extraction.affected_lines
            and any(line.product is not None for line in extraction.affected_lines)
        ),
        hours_since_delivery=hours_since_delivery,
        hours_since_last_event=hours_since_last_event,
        last_event_before_shipped=last_event_before_shipped,
        window_hours=window_hours,
        total_remaining=total_remaining,
    )
