"""Read-only data exploration for the Larkfield ticket desk.

Run from the repository root:

    python -m src.explore

Reads the cleaned tables in ``PREPARED_DIR`` and the ticket JSON files in
``DATA_ROOM_DIR``.  Writes only ``PREPARED_DIR/explore_report.json``.  Makes no
model calls.  Ticket body text is treated as data only.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

CENT = Decimal("0.01")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

TICKET_FILES = (
    ("tickets/support_inbox_export.json", "support_inbox_export"),
    ("inbox/public_tickets.json", "public_tickets"),
)

ORDER_MENTION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:LF\s*-?\s*|#\s*|order\s+(?:id\s+|number\s+)?)"
    r"(\d{6})(?!\d)"
)

STATUS_MAPPING = {
    "equivalences": [
        {
            "oms_status": "DELIVERED",
            "carrier_status": "DELIVERED",
            "delivered": True,
        },
        {
            "oms_status": "SHIPPED",
            "carrier_status": "IN_TRANSIT",
            "delivered": False,
        },
    ],
    "unknown_carrier_statuses": ["UNAVAILABLE"],
    "mismatch_rule": (
        "carrier status is known (DELIVERED or IN_TRANSIT) and the OMS "
        "delivered flag differs from the carrier delivered flag; the carrier "
        "feed wins (RP-3.2 2.1)"
    ),
    "unavailable_rule": (
        "carrier UNAVAILABLE or empty last_event_at is unknown, so it cannot "
        "be a mismatch; it is handled as CARRIER_STATUS_UNAVAILABLE (RP-3.2 4.2)"
    ),
}


# ---------------------------------------------------------------------------
# Paths and IO
# ---------------------------------------------------------------------------


def _is_inside(child: Path, parent: Path) -> bool:
    child_s = os.path.normcase(str(child.resolve()))
    parent_s = os.path.normcase(str(parent.resolve()))
    return child_s == parent_s or child_s.startswith(parent_s + os.sep)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"missing prepared table: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise SystemExit(f"missing JSON input: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        parsed = datetime.strptime(text, UTC_FORMAT)
    else:
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt_money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _decimal_days(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    seconds = (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    return seconds / Decimal(86400)


def _order_mentions(text: str) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in ORDER_MENTION_RE.finditer(text):
        order_id = f"LF-{match.group(1)}"
        if order_id in seen:
            continue
        seen.add(order_id)
        mentions.append({"order_id": order_id, "raw_mention": match.group(0)})
    return mentions


# ---------------------------------------------------------------------------
# Affected-line inference
# ---------------------------------------------------------------------------


def _affected_lines(
    items: list[dict[str, str]],
    text: str,
    refunds_by_line: dict[tuple[str, str], list[dict[str, str]]],
) -> list[dict[str, Any]]:
    """Match order-line product names to the ticket text.

    The affected quantity defaults to the ordered quantity.  It is reduced to
    one only when the text explicitly says a subset, e.g. T026's "one of my
    two ... chairs" followed by "the second chair is undamaged".
    """
    text_lower = text.lower()
    affected: list[dict[str, Any]] = []

    for item in items:
        product = item["product_name"]
        if product.lower() not in text_lower:
            continue

        ordered_qty = int(item["qty"])
        affected_qty = ordered_qty
        why = f"matched product name '{product}' in the ticket text"
        if ordered_qty > 1:
            position = text_lower.find(product.lower())
            before = text_lower[max(0, position - 100) : position]
            if "one of" in before or "one of my two" in text_lower:
                affected_qty = 1
                why += (
                    f"; ordered qty {ordered_qty}, reduced to 1 because the "
                    "text says 'one of my two' and the second item is undamaged"
                )
            else:
                why += f"; ordered qty {ordered_qty}, no subset wording found"

        unit_price = Decimal(item["unit_price"])
        refunded = Decimal(item["refunded_issued"])
        line_value = (unit_price * affected_qty).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        remaining = max(line_value - refunded, Decimal("0.00")).quantize(
            CENT, rounding=ROUND_HALF_UP
        )

        key = (item["order_id"], item["line_no"])
        refund_records = [
            {
                "refund_id": row["refund_id"],
                "amount": _fmt_money(Decimal(row["amount"])),
                "status": row["status"],
                "requested_at": row["requested_at"],
            }
            for row in refunds_by_line.get(key, [])
        ]

        affected.append(
            {
                "line_no": item["line_no"],
                "sku": item["sku"],
                "product_name": product,
                "unit_price": _fmt_money(unit_price),
                "ordered_qty": ordered_qty,
                "affected_qty": affected_qty,
                "affected_line_value": _fmt_money(line_value),
                "refunds_issued": _fmt_money(refunded),
                "remaining_refundable": _fmt_money(remaining),
                "refund_records": refund_records,
                "why": why,
            }
        )

    return affected


# ---------------------------------------------------------------------------
# Unlabelled-ticket walkthrough (throwaway draft-table simulator)
# ---------------------------------------------------------------------------

INJECTION_RE = re.compile(
    r"(?i)(ignore\s+(your|all|any)\s+(previous|prior|above)"
    r"|note\s+to\s+the\s+ai"
    r"|\[\s*system\s*\]"
    r"|pre[-\s]?approved"
    r"|authori[sz]ed\s+to\s+(process|issue|refund)"
    r"|without\s+(review|approval)"
    r"|mark\s+human_approval)"
)


def _fmt_days(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _fmt_hours(value: Decimal | None) -> str | None:
    return None if value is None else f"{value * 24:.2f}"


def _ticket_profile(text: str, image_attachment: bool) -> dict[str, Any]:
    """Rough ticket type for the walkthrough only (no model call)."""
    lowered = text.lower()
    injection_signals = sorted(
        {match.group(0).strip() for match in INJECTION_RE.finditer(text)}
    )

    if injection_signals:
        ticket_type = "INJECTION"
    elif any(
        key in lowered
        for key in (
            "never arrived",
            "hasn't arrived",
            "has not arrived",
            "not arrived",
            "never received",
            "not received",
            "not delivered",
        )
    ):
        ticket_type = "NON_DELIVERY"
    elif any(
        key in lowered
        for key in (
            "where is my order",
            "tracking hasn't",
            "hasn't changed",
            "stuck for ages",
            "what is going on",
            "gone missing",
            "is it lost",
            "hasn't moved",
            "no movement",
        )
    ):
        ticket_type = "STATUS_QUESTION"
    elif any(
        key in lowered
        for key in ("wrong item", "received something else", "not what i ordered")
    ):
        ticket_type = "REFUND_WRONG_ITEM"
    elif any(
        key in lowered
        for key in ("not what i hoped", "changed my mind", "don't want", "do not want")
    ):
        ticket_type = "REFUND_CHANGE_OF_MIND"
    elif any(
        key in lowered
        for key in ("damaged", "broken", "faulty", "cracked", "fault")
    ):
        ticket_type = "REFUND_DAMAGED_FAULTY"
    elif any(key in lowered for key in ("refund", "return")):
        ticket_type = "REFUND_RETURN"
    else:
        ticket_type = "OTHER"

    return {
        "ticket_type": ticket_type,
        "is_status_like": ticket_type in {"NON_DELIVERY", "STATUS_QUESTION"},
        "is_refund": ticket_type.startswith("REFUND"),
        "is_change_of_mind": ticket_type == "REFUND_CHANGE_OF_MIND",
        "is_damaged_faulty": any(
            key in lowered for key in ("damaged", "broken", "faulty", "cracked", "fault")
        ),
        "injection_signals": injection_signals,
        "asks_whole_order": any(
            key in lowered
            for key in (
                "whole order",
                "full order",
                "money back for the whole",
                "everything",
                "all of it",
            )
        ),
        "asks_shipping": any(
            key in lowered for key in ("delivery charge", "shipping", "postage")
        ),
    }


def _fact_sheet(
    ticket: dict[str, Any],
    file_label: str,
    orders: dict[str, dict[str, str]],
    customers: dict[str, dict[str, str]],
    shipments: dict[str, dict[str, str]],
    items_by_order: dict[str, list[dict[str, str]]],
    refunds_by_line: dict[tuple[str, str], list[dict[str, str]]],
) -> dict[str, Any]:
    text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
    mentions = _order_mentions(text)
    order_id = mentions[0]["order_id"] if mentions else None
    order = orders.get(order_id) if order_id else None
    customer = customers.get(order["customer_id"]) if order else None
    shipment = shipments.get(order_id) if order_id else None

    sender_raw = str(ticket.get("from", ""))
    sender_normalized = sender_raw.strip().lower()
    customer_email = customer["email"].strip().lower() if customer else None
    sender_matches_customer = (
        sender_normalized == customer_email if customer_email is not None else None
    )

    received_at = _parse_ts(str(ticket.get("received_at", "")))
    shipped_at = _parse_ts(shipment["shipped_at"]) if shipment else None
    delivered_at = _parse_ts(shipment["delivered_at"]) if shipment else None
    last_event_at = _parse_ts(shipment["last_event_at"]) if shipment else None

    days_from_delivery_whole = None
    days_from_delivery_exact = None
    if received_at and delivered_at:
        days_from_delivery_whole = (received_at.date() - delivered_at.date()).days
        days_from_delivery_exact = _decimal_days(received_at, delivered_at)

    movement_whole = None
    movement_exact = None
    if (
        shipment
        and shipment["status"] != "DELIVERED"
        and received_at
        and last_event_at
    ):
        movement_whole = (received_at.date() - last_event_at.date()).days
        movement_exact = _decimal_days(received_at, last_event_at)

    attachment_types = [
        str(attachment.get("content_type", ""))
        for attachment in ticket.get("attachments", [])
    ]
    image_attachment = any(
        content_type.startswith("image/") for content_type in attachment_types
    )

    affected_lines = _affected_lines(
        items_by_order.get(order_id, []) if order_id else [],
        text,
        refunds_by_line,
    )
    total_remaining = sum(
        (Decimal(line["remaining_refundable"]) for line in affected_lines),
        Decimal("0.00"),
    )

    profile = _ticket_profile(text, image_attachment)
    window_days = 45 if (profile["is_damaged_faulty"] and image_attachment) else 30

    return {
        "file": file_label,
        "ticket_id": str(ticket["ticket_id"]),
        "subject": str(ticket.get("subject", "")),
        "received_at": ticket.get("received_at"),
        "sender_normalized": sender_normalized,
        "order_ids_mentioned": mentions,
        "order_id": order_id,
        "order_exists": order is not None,
        "customer_id": order["customer_id"] if order else None,
        "customer_email": customer_email,
        "sender_matches_customer": sender_matches_customer,
        "order_status": order["status"] if order else None,
        "carrier_status": shipment["status"] if shipment else None,
        "carrier_status_available": (
            shipment["carrier_status_available"] == "true" if shipment else None
        ),
        "shipped_at": shipment["shipped_at"] if shipment else None,
        "delivered_at": shipment["delivered_at"] if shipment else None,
        "last_event_at": shipment["last_event_at"] if shipment else None,
        "days_from_delivery_whole": days_from_delivery_whole,
        "days_from_delivery_exact": _fmt_days(days_from_delivery_exact),
        "hours_from_delivery_exact": _fmt_hours(days_from_delivery_exact),
        "days_since_last_movement_whole": movement_whole,
        "days_since_last_movement_exact": _fmt_days(movement_exact),
        "hours_since_last_movement_exact": _fmt_hours(movement_exact),
        "image_attachment": image_attachment,
        "attachment_content_types": attachment_types,
        "window_days": window_days,
        "window_basis": (
            "45-day damaged/faulty + photo (2.6)"
            if window_days == 45
            else "30-day default (2.1)"
        ),
        "affected_lines": affected_lines,
        "remaining_refundable_total": _fmt_money(total_remaining),
        "profile": profile,
    }


def _rule(
    number: int,
    outcome: str,
    reason_codes: list[str],
    approver_role: str | None,
    clauses: str,
    basis: str,
) -> dict[str, Any]:
    return {
        "rule": number,
        "outcome": outcome,
        "reason_codes": reason_codes,
        "approver_role": approver_role,
        "clauses": clauses,
        "basis": basis,
    }


def _walk_rule(facts: dict[str, Any]) -> dict[str, Any]:
    """Walk the draft table in order. Throwaway: first match wins."""
    profile = facts["profile"]

    if profile["injection_signals"]:
        return _rule(
            1,
            "ESCALATE",
            ["POLICY_OVERRIDE_ATTEMPT"],
            "TRUST_SAFETY",
            "5.2",
            "prompt injection / claimed pre-approval: "
            + "; ".join(profile["injection_signals"]),
        )
    if not facts["order_id"] or not facts["order_exists"]:
        return _rule(
            2,
            "INVALID_REQUEST",
            ["ORDER_NOT_FOUND"],
            None,
            "4.1",
            "no order id, or the order is not in orders.csv",
        )
    if facts["sender_matches_customer"] is False:
        return _rule(
            3,
            "INVALID_REQUEST",
            ["CUSTOMER_MISMATCH"],
            None,
            "4.1",
            "order belongs to a different customer",
        )

    unavailable = (
        facts["carrier_status"] in (None, "UNAVAILABLE")
        or not facts["last_event_at"]
    )
    if profile["is_status_like"] and unavailable:
        return _rule(
            4,
            "UNABLE_TO_VERIFY",
            ["CARRIER_STATUS_UNAVAILABLE"],
            None,
            "4.2",
            "carrier status unavailable",
        )

    movement = facts["days_since_last_movement_exact"]
    if (
        facts["carrier_status"] != "DELIVERED"
        and movement is not None
        and Decimal(movement) > Decimal("10")
    ):
        return _rule(
            5,
            "ESCALATE",
            ["LOST_IN_TRANSIT"],
            "CARRIER_CLAIMS",
            "5.1",
            f"{movement} days since last movement (> 10)",
        )
    if profile["is_status_like"]:
        return _rule(
            6,
            "PROCEED",
            [],
            None,
            "5.3",
            "status question; shipment moving normally or delivered",
        )

    if not profile["is_refund"]:
        return _rule(
            12,
            "TODO",
            [],
            None,
            "none",
            "type OTHER / unclear; no clause picks an outcome",
        )
    if not facts["affected_lines"]:
        return _rule(
            12,
            "TODO",
            [],
            None,
            "none",
            "refund/return but no affected line could be identified",
        )

    days = facts["days_from_delivery_whole"]
    window = facts["window_days"]
    if days is not None and days > window:
        return _rule(
            7,
            "REJECT",
            ["OUT_OF_WINDOW"],
            None,
            "2.1, 2.3, 2.6",
            f"{days} whole days since delivery > {window}-day window",
        )

    total = Decimal(facts["remaining_refundable_total"])
    if total == 0:
        return _rule(
            8,
            "REJECT",
            ["ALREADY_REFUNDED"],
            None,
            "2.5",
            "every affected line is already fully refunded",
        )
    if total > Decimal("150"):
        reasons = ["APPROVAL_REQUIRED"]
        if (
            profile["is_damaged_faulty"]
            and facts["image_attachment"]
            and days is not None
            and 31 <= days <= 45
        ):
            reasons.append("DAMAGE_EXCEPTION")
        if profile["asks_whole_order"] or profile["asks_shipping"]:
            reasons.append("PARTIAL_LINES")
        if any(
            Decimal(line["refunds_issued"]) > 0 for line in facts["affected_lines"]
        ):
            reasons.append("ALREADY_REFUNDED")
        return _rule(
            9,
            "PEND",
            reasons,
            "SUPPORT_LEAD",
            "3.2 (+2.4, 2.5, 2.6)",
            f"remaining ${facts['remaining_refundable_total']} > $150",
        )

    cut_by_policy = any(
        Decimal(line["refunds_issued"]) > 0 for line in facts["affected_lines"]
    )
    if profile["asks_whole_order"] or profile["asks_shipping"] or cut_by_policy:
        reasons = []
        if profile["asks_whole_order"] or profile["asks_shipping"]:
            reasons.append("PARTIAL_LINES")
        if cut_by_policy:
            reasons.append("ALREADY_REFUNDED")
        if (
            profile["is_damaged_faulty"]
            and facts["image_attachment"]
            and days is not None
            and 31 <= days <= 45
        ):
            reasons.append("DAMAGE_EXCEPTION")
        return _rule(
            10,
            "PROCEED_MODIFIED",
            reasons,
            None,
            "2.4, 2.5, 3.1",
            "customer asked for more than the affected lines, or 2.5 cut the amount",
        )

    reasons = []
    if (
        profile["is_damaged_faulty"]
        and facts["image_attachment"]
        and days is not None
        and 31 <= days <= 45
    ):
        reasons.append("DAMAGE_EXCEPTION")
    return _rule(
        11,
        "PROCEED",
        reasons,
        None,
        "3.1, 2.6",
        f"remaining ${facts['remaining_refundable_total']} <= $150",
    )


def _threshold_notes(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Record every case within one day of a draft-table threshold."""
    notes: list[dict[str, Any]] = []

    days_whole = facts["days_from_delivery_whole"]
    days_exact_raw = facts["days_from_delivery_exact"]
    days_exact = Decimal(days_exact_raw) if days_exact_raw else None
    if days_whole is not None and days_exact is not None:
        for threshold in (30, 45):
            if abs(days_whole - threshold) <= 1 or abs(days_exact - threshold) <= 1:
                notes.append(
                    {
                        "kind": "days_since_delivery",
                        "threshold_days": threshold,
                        "governing_window_days": facts["window_days"],
                        "delivered_at": facts["delivered_at"],
                        "received_at": facts["received_at"],
                        "whole_days": days_whole,
                        "exact_days": days_exact_raw,
                        "exact_hours": facts["hours_from_delivery_exact"],
                        "whole_vs_threshold": (
                            "inside" if days_whole <= threshold else "outside"
                        ),
                        "exact_vs_threshold": (
                            "inside" if days_exact <= threshold else "outside"
                        ),
                        "whole_with_governing_window": (
                            "rule 7 (REJECT/OUT_OF_WINDOW)"
                            if days_whole > facts["window_days"]
                            else "inside governing window"
                        ),
                        "exact_with_governing_window": (
                            "rule 7 (REJECT/OUT_OF_WINDOW)"
                            if days_exact > facts["window_days"]
                            else "inside governing window"
                        ),
                    }
                )

    movement_whole = facts["days_since_last_movement_whole"]
    movement_exact_raw = facts["days_since_last_movement_exact"]
    movement_exact = (
        Decimal(movement_exact_raw) if movement_exact_raw else None
    )
    if movement_whole is not None and movement_exact is not None:
        if abs(movement_whole - 10) <= 1 or abs(movement_exact - 10) <= 1:
            notes.append(
                {
                    "kind": "days_since_last_movement",
                    "threshold_days": 10,
                    "last_event_at": facts["last_event_at"],
                    "received_at": facts["received_at"],
                    "whole_days": movement_whole,
                    "exact_days": movement_exact_raw,
                    "exact_hours": facts["hours_since_last_movement_exact"],
                    "whole_vs_threshold": (
                        "over" if movement_whole > 10 else "not over"
                    ),
                    "exact_vs_threshold": (
                        "over" if movement_exact > 10 else "not over"
                    ),
                    "whole_would_give": (
                        "rule 5 (ESCALATE/LOST_IN_TRANSIT)"
                        if movement_whole > 10
                        else "rule 6 (PROCEED)"
                    ),
                    "exact_would_give": (
                        "rule 5 (ESCALATE/LOST_IN_TRANSIT)"
                        if movement_exact > 10
                        else "rule 6 (PROCEED)"
                    ),
                }
            )

    total = Decimal(facts["remaining_refundable_total"])
    if abs(total - Decimal("150")) <= Decimal("1"):
        notes.append(
            {
                "kind": "amount",
                "threshold": "150.00",
                "amount": facts["remaining_refundable_total"],
                "within_one_dollar": True,
                "rule_if_over": "rule 9 (PEND/SUPPORT_LEAD)",
                "rule_if_at_or_under": "rule 10 or 11 (no approval)",
            }
        )

    return notes


def _flags_for(
    facts: dict[str, Any],
    order_to_tickets: dict[str, list[str]],
    rule: dict[str, Any],
) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    profile = facts["profile"]

    if profile["injection_signals"]:
        flags.append(
            {
                "code": "PROMPT_INJECTION",
                "detail": "signals: " + " | ".join(profile["injection_signals"]),
            }
        )

    mentions = facts["order_ids_mentioned"]
    if not mentions:
        flags.append(
            {"code": "NO_ORDER_ID", "detail": "no order id in subject/body"}
        )
    if len(mentions) > 1:
        flags.append(
            {
                "code": "MULTIPLE_ORDER_IDS",
                "detail": ", ".join(mention["order_id"] for mention in mentions),
            }
        )
    if facts["order_id"] and not facts["order_exists"]:
        flags.append(
            {
                "code": "ORDER_NOT_FOUND",
                "detail": f"{facts['order_id']} is not in orders.csv",
            }
        )
    if facts["sender_matches_customer"] is False:
        flags.append(
            {
                "code": "CUSTOMER_MISMATCH",
                "detail": (
                    f"{facts['order_id']} belongs to {facts['customer_email']}; "
                    f"sender is {facts['sender_normalized']}"
                ),
            }
        )

    others = [
        ticket_id
        for ticket_id in order_to_tickets.get(facts["order_id"], [])
        if ticket_id != facts["ticket_id"]
    ]
    if others:
        flags.append(
            {
                "code": "SHARED_ORDER_ID",
                "detail": f"{facts['order_id']} is also mentioned by {', '.join(others)}",
            }
        )

    if profile["is_change_of_mind"]:
        flags.append(
            {
                "code": "CHANGE_OF_MIND",
                "detail": (
                    "no damage or wrong item; 2.2 does not price a change-of-mind "
                    "return, so rule 11 is the draft's assumption"
                ),
            }
        )

    if profile["is_status_like"] and facts["carrier_status"] == "DELIVERED":
        flags.append(
            {
                "code": "CLAIM_NOT_DELIVERED_CARRIER_DELIVERED",
                "detail": "customer claims non-delivery but the carrier says DELIVERED",
            }
        )

    for line in facts["affected_lines"]:
        if line["ordered_qty"] > 1 and "no subset wording found" in line["why"]:
            flags.append(
                {
                    "code": "QTY_UNCLEAR",
                    "detail": (
                        f"line {line['line_no']} has qty {line['ordered_qty']} "
                        "and the text does not say how many are affected"
                    ),
                }
            )

    if facts["carrier_status"] == "UNAVAILABLE" or (
        profile["is_status_like"] and not facts["last_event_at"]
    ):
        flags.append(
            {
                "code": "CARRIER_UNAVAILABLE",
                "detail": (
                    f"carrier status {facts['carrier_status']}, "
                    f"last_event_at {facts['last_event_at'] or '(empty)'}"
                ),
            }
        )

    if (
        facts["last_event_at"]
        and facts["shipped_at"]
        and facts["last_event_at"] < facts["shipped_at"]
    ):
        flags.append(
            {
                "code": "LAST_EVENT_BEFORE_SHIPPED",
                "detail": (
                    f"last_event_at {facts['last_event_at']} is before "
                    f"shipped_at {facts['shipped_at']}"
                ),
            }
        )

    if facts["order_id"] in ("LF-100221", "LF-100240"):
        flags.append(
            {
                "code": "ORDER_DATE_AFTER_SHIPPED",
                "detail": (
                    "prepare_report.json flags order_date after shipped_at "
                    "(source-data note; order_date is not used by any rule)"
                ),
            }
        )

    if profile["asks_shipping"]:
        flags.append(
            {
                "code": "ASKS_SHIPPING_REFUND",
                "detail": (
                    "asks for shipping/delivery charge; 2.2 refunds order-line "
                    "value only"
                ),
            }
        )

    if rule["rule"] == 12:
        flags.append({"code": "RULE_12_UNRESOLVED", "detail": rule["basis"]})

    return flags


# ---------------------------------------------------------------------------
# Main exploration
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv()

    data_room_raw = os.getenv("DATA_ROOM_DIR", "data_room")
    prepared_raw = os.getenv("PREPARED_DIR", "prepared")
    data_room = Path(data_room_raw)
    prepared = Path(prepared_raw)

    if _is_inside(prepared, data_room):
        raise SystemExit(
            "refusing to run: PREPARED_DIR "
            f"({prepared}) is inside DATA_ROOM_DIR ({data_room})"
        )
    if not data_room.is_dir():
        raise SystemExit(f"DATA_ROOM_DIR is not a directory: {data_room}")
    if not prepared.is_dir():
        raise SystemExit(
            f"PREPARED_DIR is not a directory: {prepared}; run python -m src.prepare"
        )

    orders_rows = _read_csv(prepared / "orders.csv")
    customers_rows = _read_csv(prepared / "customers.csv")
    item_rows = _read_csv(prepared / "order_items.csv")
    shipment_rows = _read_csv(prepared / "shipments.csv")
    refund_rows = _read_csv(prepared / "refunds.csv")

    orders = {row["order_id"]: row for row in orders_rows}
    customers = {row["customer_id"]: row for row in customers_rows}
    shipments = {}
    for row in shipment_rows:
        shipments.setdefault(row["order_id"], row)
    items_by_order: dict[str, list[dict[str, str]]] = {}
    for row in item_rows:
        items_by_order.setdefault(row["order_id"], []).append(row)
    refunds_by_line: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in refund_rows:
        refunds_by_line.setdefault((row["order_id"], row["line_no"]), []).append(row)

    ticket_sets: list[tuple[str, list[dict[str, Any]]]] = []
    tickets_by_id: dict[str, dict[str, Any]] = {}
    for relative_path, label in TICKET_FILES:
        tickets = _read_json(data_room / relative_path)
        if not isinstance(tickets, list):
            raise SystemExit(f"ticket file is not a JSON list: {relative_path}")
        ticket_sets.append((label, tickets))
        for ticket in tickets:
            tickets_by_id[str(ticket["ticket_id"])] = ticket

    labels = _read_json(data_room / "tickets/eval_seed_labelled.json")
    if not isinstance(labels, list):
        raise SystemExit("eval_seed_labelled.json is not a JSON list")

    label_differences = [
        {
            "order_id": row["order_id"],
            "oms_status": row["status"],
            "carrier_status": row["carrier_status"],
        }
        for row in sorted(orders_rows, key=lambda item: item["order_id"])
        if row["carrier_status"] and row["status"] != row["carrier_status"]
    ]
    pair_counts = Counter(
        f"{row['oms_status']}/{row['carrier_status']}" for row in label_differences
    )
    mismatch_count = sum(
        1 for row in orders_rows if row["status_mismatch"].lower() == "true"
    )

    order_221_mentions: list[dict[str, str]] = []
    for file_label, tickets in ticket_sets:
        for ticket in tickets:
            text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
            for mention in _order_mentions(text):
                if mention["order_id"] == "LF-100221":
                    order_221_mentions.append(
                        {
                            "file": file_label,
                            "ticket_id": str(ticket["ticket_id"]),
                            "subject": str(ticket.get("subject", "")),
                            "raw_mention": mention["raw_mention"],
                        }
                    )

    labelled_results: list[dict[str, Any]] = []
    for label in labels:
        ticket_id = str(label["ticket_id"])
        ticket = tickets_by_id.get(ticket_id)
        if ticket is None:
            labelled_results.append(
                {"ticket_id": ticket_id, "error": "ticket not found in ticket files"}
            )
            continue

        text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
        mentions = _order_mentions(text)
        order_id = mentions[0]["order_id"] if mentions else None
        order = orders.get(order_id) if order_id else None
        customer = customers.get(order["customer_id"]) if order else None
        shipment = shipments.get(order_id) if order_id else None

        sender_raw = str(ticket.get("from", ""))
        sender_normalized = sender_raw.strip().lower()
        customer_email = customer["email"].strip().lower() if customer else None
        sender_matches_customer = (
            sender_normalized == customer_email if customer_email is not None else None
        )

        received_at = _parse_ts(str(ticket.get("received_at", "")))
        shipped_at = _parse_ts(shipment["shipped_at"]) if shipment else None
        delivered_at = _parse_ts(shipment["delivered_at"]) if shipment else None
        last_event_at = _parse_ts(shipment["last_event_at"]) if shipment else None

        days_from_delivery = None
        if received_at and delivered_at:
            days_from_delivery = (
                received_at.date() - delivered_at.date()
            ).days

        carrier_status = shipment["status"] if shipment else None
        days_since_last_movement = None
        movement_over_10_days = None
        if (
            shipment
            and carrier_status != "DELIVERED"
            and received_at
            and last_event_at
        ):
            movement_days = _decimal_days(received_at, last_event_at)
            days_since_last_movement = f"{movement_days:.2f}"
            movement_over_10_days = movement_days > Decimal("10")

        attachment_types = [
            str(attachment.get("content_type", ""))
            for attachment in ticket.get("attachments", [])
        ]
        image_attachment = any(
            content_type.startswith("image/") for content_type in attachment_types
        )

        affected_lines = _affected_lines(
            items_by_order.get(order_id, []) if order_id else [],
            text,
            refunds_by_line,
        )
        total_remaining = sum(
            (Decimal(line["remaining_refundable"]) for line in affected_lines),
            Decimal("0.00"),
        )

        if affected_lines:
            decision = (
                "Matched order_items.product_name as a case-insensitive substring "
                "of subject+body. Affected qty defaults to ordered qty; it is 1 "
                "only where the text says 'one of my two' (T026). A request for "
                "the whole order does not add lines: only product names matched."
            )
        else:
            decision = (
                "No order_items.product_name matched the ticket text, so no line "
                "is affected (status or non-delivery request)."
            )

        labelled_results.append(
            {
                "ticket_id": ticket_id,
                "received_at": ticket.get("received_at"),
                "sender_raw": sender_raw,
                "sender_normalized": sender_normalized,
                "order_ids_mentioned": mentions,
                "order_id": order_id,
                "order_exists": order is not None,
                "customer_id": order["customer_id"] if order else None,
                "customer_email": customer_email,
                "sender_matches_customer": sender_matches_customer,
                "order_status": order["status"] if order else None,
                "carrier_status": carrier_status,
                "carrier_status_available": (
                    shipment["carrier_status_available"] == "true"
                    if shipment
                    else None
                ),
                "status_mismatch": order["status_mismatch"] if order else None,
                "shipped_at": shipment["shipped_at"] if shipment else None,
                "delivered_at": shipment["delivered_at"] if shipment else None,
                "last_event_at": shipment["last_event_at"] if shipment else None,
                "days_from_delivery_to_received": days_from_delivery,
                "days_since_last_movement": days_since_last_movement,
                "movement_over_10_days": movement_over_10_days,
                "image_attachment": image_attachment,
                "attachment_content_types": attachment_types,
                "affected_line_decision": decision,
                "affected_lines": affected_lines,
                "remaining_refundable_total": _fmt_money(total_remaining),
                "expected": {
                    key: value for key, value in label.items() if key != "ticket_id"
                },
            }
        )

    labelled_ids = {str(label["ticket_id"]) for label in labels}

    order_to_tickets: dict[str, list[str]] = {}
    for file_label, tickets in ticket_sets:
        for ticket in tickets:
            text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
            ticket_id = str(ticket["ticket_id"])
            for mention in _order_mentions(text):
                bucket = order_to_tickets.setdefault(mention["order_id"], [])
                if ticket_id not in bucket:
                    bucket.append(ticket_id)

    unlabelled_results: list[dict[str, Any]] = []
    for file_label, tickets in ticket_sets:
        for ticket in tickets:
            ticket_id = str(ticket["ticket_id"])
            if ticket_id in labelled_ids:
                continue
            facts = _fact_sheet(
                ticket,
                file_label,
                orders,
                customers,
                shipments,
                items_by_order,
                refunds_by_line,
            )
            rule = _walk_rule(facts)
            unlabelled_results.append(
                {
                    **facts,
                    "rule_hit": rule,
                    "threshold_notes": _threshold_notes(facts),
                    "flags": _flags_for(facts, order_to_tickets, rule),
                }
            )

    last_event_before_shipped: list[dict[str, Any]] = []
    for file_label, tickets in ticket_sets:
        for ticket in tickets:
            facts = _fact_sheet(
                ticket,
                file_label,
                orders,
                customers,
                shipments,
                items_by_order,
                refunds_by_line,
            )
            if not (
                facts["last_event_at"]
                and facts["shipped_at"]
                and facts["last_event_at"] < facts["shipped_at"]
            ):
                continue
            received_at = _parse_ts(str(ticket["received_at"]))
            shipped_at = _parse_ts(facts["shipped_at"])
            alternate = dict(facts)
            alternate["days_since_last_movement_exact"] = _fmt_days(
                _decimal_days(received_at, shipped_at)
            )
            alternate["days_since_last_movement_whole"] = (
                received_at.date() - shipped_at.date()
            ).days
            rule_from_last_event = _walk_rule(facts)
            rule_from_shipped_at = _walk_rule(alternate)
            last_event_before_shipped.append(
                {
                    "ticket_id": facts["ticket_id"],
                    "file": file_label,
                    "order_id": facts["order_id"],
                    "shipped_at": facts["shipped_at"],
                    "last_event_at": facts["last_event_at"],
                    "received_at": facts["received_at"],
                    "days_from_last_event": facts["days_since_last_movement_exact"],
                    "days_from_shipped_at": alternate[
                        "days_since_last_movement_exact"
                    ],
                    "rule_from_last_event": rule_from_last_event["rule"],
                    "outcome_from_last_event": rule_from_last_event["outcome"],
                    "rule_from_shipped_at": rule_from_shipped_at["rule"],
                    "outcome_from_shipped_at": rule_from_shipped_at["outcome"],
                }
            )

    t011_check = next(
        (
            entry
            for entry in last_event_before_shipped
            if entry["ticket_id"] == "T011"
        ),
        None,
    )

    report = {
        "generated_by": "src/explore.py",
        "inputs": {
            "data_room_dir": data_room_raw,
            "prepared_dir": prepared_raw,
            "ticket_files": [label for label, _ in TICKET_FILES],
        },
        "status_mapping": {
            **STATUS_MAPPING,
            "mismatch_count": mismatch_count,
            "raw_label_difference_count": len(label_differences),
            "raw_label_differences_by_pair": dict(sorted(pair_counts.items())),
        },
        "status_label_differences": label_differences,
        "order_100221_ticket_mentions": order_221_mentions,
        "affected_line_rule": (
            "Case-insensitive substring match of order_items.product_name in "
            "ticket subject+body. Ordered qty is used unless the text explicitly "
            "describes a subset ('one of my two ...'); then affected qty is 1. "
            "Refunds are the prepared ISSUED sum per (order_id, line_no); "
            "remaining = max(unit_price * affected_qty - refunds_issued, 0)."
        ),
        "labelled_tickets": labelled_results,
        "unlabelled_tickets": unlabelled_results,
        "last_event_before_shipped_at": last_event_before_shipped,
        "t011_carrier_date_check": t011_check,
    }

    report_path = prepared / "explore_report.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

    _print_report(report)


def _print_report(report: dict[str, Any]) -> None:
    mapping = report["status_mapping"]
    print("Status mapping used for status_mismatch:")
    print("  OMS DELIVERED <-> carrier DELIVERED (delivered)")
    print("  OMS SHIPPED   <-> carrier IN_TRANSIT (not delivered)")
    print("  carrier UNAVAILABLE -> unknown, cannot mismatch (RP-3.2 4.2)")
    print(
        "  mismatch only when carrier status is known and the delivered flags "
        "differ; carrier wins (RP-3.2 2.1)"
    )
    print(
        f"  result: {mapping['mismatch_count']} mismatches; "
        f"{mapping['raw_label_difference_count']} raw label differences"
    )
    print("Raw label differences:")
    for difference in report["status_label_differences"]:
        print(
            f"  {difference['order_id']}  OMS={difference['oms_status']}  "
            f"carrier={difference['carrier_status']}"
        )

    print()
    print("LF-100221 ticket mentions:")
    if not report["order_100221_ticket_mentions"]:
        print("  none")
    else:
        for mention in report["order_100221_ticket_mentions"]:
            print(
                f"  {mention['ticket_id']} ({mention['file']}) "
                f"subject={mention['subject']!r} "
                f"mention={mention['raw_mention']!r}"
            )

    print()
    print("Labelled ticket facts:")
    for result in report["labelled_tickets"]:
        if "error" in result:
            print(f"  {result['ticket_id']}: {result['error']}")
            continue
        print(
            f"  {result['ticket_id']} order={result['order_id']} "
            f"exists={result['order_exists']} "
            f"sender_match={result['sender_matches_customer']} "
            f"carrier={result['carrier_status']} "
            f"available={result['carrier_status_available']} "
            f"delivered_at={result['delivered_at'] or '-'} "
            f"last_event_at={result['last_event_at'] or '-'} "
            f"days_delivery={result['days_from_delivery_to_received']} "
            f"days_last_movement={result['days_since_last_movement'] or '-'} "
            f"image={result['image_attachment']} "
            f"remaining_total={result['remaining_refundable_total']}"
        )
        expected = result["expected"]
        print(
            f"    expected={expected.get('expected_outcome')} "
            f"approval={expected.get('expected_human_approval_required')} "
            f"role={expected.get('expected_approver_role')}"
        )
        if not result["affected_lines"]:
            print("    affected lines: none")
            continue
        for line in result["affected_lines"]:
            print(
                f"    line {line['line_no']} {line['product_name']!r}: "
                f"unit_price={line['unit_price']} ordered_qty={line['ordered_qty']} "
                f"affected_qty={line['affected_qty']} "
                f"refunds_issued={line['refunds_issued']} "
                f"remaining={line['remaining_refundable']}"
            )
            print(f"      why: {line['why']}")

    print()
    print("Unlabelled ticket walkthrough:")
    for result in report["unlabelled_tickets"]:
        rule = result["rule_hit"]
        print(
            f"  {result['ticket_id']} ({result['file']}) "
            f"type={result['profile']['ticket_type']} "
            f"order={result['order_id']} exists={result['order_exists']} "
            f"rule={rule['rule']} outcome={rule['outcome']} "
            f"reasons={','.join(rule['reason_codes']) or '-'} "
            f"approver={rule['approver_role'] or '-'} "
            f"days_delivery={result['days_from_delivery_whole']} "
            f"exact_days={result['days_from_delivery_exact'] or '-'} "
            f"movement={result['days_since_last_movement_exact'] or '-'} "
            f"amount={result['remaining_refundable_total']} "
            f"photo={result['image_attachment']}"
        )
        print(f"    basis: {rule['basis']}")
        for note in result["threshold_notes"]:
            print(f"    THRESHOLD: {note}")
        for flag in result["flags"]:
            print(f"    FLAG {flag['code']}: {flag['detail']}")

    print()
    print("Tickets whose last_event_at is before shipped_at:")
    if not report["last_event_before_shipped_at"]:
        print("  none")
    for entry in report["last_event_before_shipped_at"]:
        print(
            f"  {entry['ticket_id']} {entry['order_id']}: "
            f"shipped={entry['shipped_at']} last_event={entry['last_event_at']} "
            f"received={entry['received_at']} | "
            f"last_event->{entry['days_from_last_event']}d rule "
            f"{entry['rule_from_last_event']} {entry['outcome_from_last_event']} | "
            f"shipped->{entry['days_from_shipped_at']}d rule "
            f"{entry['rule_from_shipped_at']} {entry['outcome_from_shipped_at']}"
        )

    print()
    print(f"Affected-line rule: {report['affected_line_rule']}")


if __name__ == "__main__":
    main()
