"""Build the cleaned tables in ``PREPARED_DIR`` from the read-only data room.

Run from the repository root:

    python -m src.prepare

The data room (``DATA_ROOM_DIR``, default ``data_room``) is never written to.
All derived artefacts go to ``PREPARED_DIR`` (default ``prepared``), which
``.gitignore`` excludes.  See NOTES.md section 1 for the data findings.

Money is handled as ``Decimal`` throughout, never ``float``.  Ticket files are
only counted here: ticket body text is data, and normalization of sender
emails / order-id mentions happens at runtime in ``src/normalize.py``.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

CENT = Decimal("0.01")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SOURCE_TABLES = ("customers", "orders", "order_items", "shipments", "refunds")

DATE_FIELDS = {
    "customers": ("created_at",),
    "orders": ("order_date",),
    "shipments": ("shipped_at", "delivered_at", "last_event_at"),
    "refunds": ("requested_at",),
}

MONEY_FIELDS = {
    "orders": ("order_total", "shipping_fee"),
    "order_items": ("unit_price",),
    "refunds": ("amount",),
}

# Explicit formats only.  No pandas date inference.
_DATE_FORMATS = (
    ("%Y-%m-%dT%H:%M:%S%z", False),
    ("%Y-%m-%dT%H:%M:%S", False),
    ("%Y-%m-%dT%H:%M", False),
    ("%Y-%m-%d", False),
    ("%d/%m/%Y", True),  # orders.order_date slash dates are day-first
)

_ORDER_ID_VALUE_RE = re.compile(
    r"(?:LF-(?P<dash>\d{6})|LF(?P<lf>\d{6})|#(?P<hash>\d{6})|(?P<bare>\d{6}))"
)

# Matches an order-id mention in ticket text: LF-######, LF######, #######,
# "order ######" and "order order ######" (the latter matches on the second
# "order").  Only canonical LF-###### mentions are left alone.
_ORDER_MENTION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:LF\s*-?\s*|#\s*|order\s+(?:id\s+|number\s+)?)"
    r"(\d{6})(?!\d)"
)

_CAMEL_1_RE = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2_RE = re.compile(r"([a-z0-9])([A-Z])")


# ---------------------------------------------------------------------------
# Paths and guards
# ---------------------------------------------------------------------------


def _is_inside(child: Path, parent: Path) -> bool:
    """Case-aware containment check that also covers Windows paths."""
    child_s = os.path.normcase(str(child.resolve()))
    parent_s = os.path.normcase(str(parent.resolve()))
    return child_s == parent_s or child_s.startswith(parent_s + os.sep)


def _guard_output_dir(data_room: Path, prepared: Path) -> None:
    if _is_inside(prepared, data_room):
        raise SystemExit(
            "refusing to run: PREPARED_DIR "
            f"({prepared}) is inside DATA_ROOM_DIR ({data_room})"
        )


# ---------------------------------------------------------------------------
# Reading and generic cell fixes
# ---------------------------------------------------------------------------


def _read_source(data_room: Path, table: str) -> tuple[list[dict[str, Any]], list[str]]:
    path = data_room / "data" / f"{table}.csv"
    if not path.is_file():
        raise SystemExit(f"missing input CSV: {path}")

    frame = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8",
    )
    columns = [str(column) for column in frame.columns]
    records = [
        {str(key): ("" if value is None else str(value)) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]
    return records, columns


def _to_snake_case(name: str) -> str:
    value = name.strip().replace("-", "_")
    value = _CAMEL_1_RE.sub(r"\1_\2", value)
    value = _CAMEL_2_RE.sub(r"\1_\2", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value.lower()


def _rename_columns(
    records: list[dict[str, Any]], columns: list[str]
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    mapping = {
        column: _to_snake_case(column)
        for column in columns
        if _to_snake_case(column) != column
    }
    if not mapping:
        return records, columns, {}

    new_columns = [mapping.get(column, column) for column in columns]
    if len(set(new_columns)) != len(new_columns):
        raise SystemExit(f"column rename produced duplicate names: {new_columns}")

    renamed = [{mapping.get(key, key): value for key, value in row.items()} for row in records]
    return renamed, new_columns, mapping


def _trim_cells(records: list[dict[str, Any]]) -> int:
    trimmed = 0
    for row in records:
        for key, value in list(row.items()):
            stripped = value.strip()
            if stripped != value:
                trimmed += 1
                row[key] = stripped
    return trimmed


def _drop_exact_duplicates(
    records: list[dict[str, Any]], columns: list[str]
) -> tuple[list[dict[str, Any]], int]:
    seen: set[tuple[str, ...]] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in records:
        key = tuple(row.get(column, "") for column in columns)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, dropped


# ---------------------------------------------------------------------------
# Order ids
# ---------------------------------------------------------------------------


def _canonical_order_id(value: str) -> tuple[str, bool]:
    """Return (canonical_value, parseable).  Exact patterns only, no fuzzing."""
    match = _ORDER_ID_VALUE_RE.fullmatch(value)
    if match is None:
        return value, False
    digits = match.group("dash") or match.group("lf") or match.group("hash") or match.group("bare")
    return f"LF-{digits}", True


def _normalise_order_ids(
    records: list[dict[str, Any]], columns: list[str]
) -> dict[str, int]:
    if "order_id" not in columns:
        return {"changed": 0, "unparseable": 0}
    changed = 0
    unparseable = 0
    for row in records:
        original = row["order_id"]
        canonical, parseable = _canonical_order_id(original)
        if not parseable:
            unparseable += 1
            continue
        if canonical != original:
            changed += 1
            row["order_id"] = canonical
    return {"changed": changed, "unparseable": unparseable}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _normalise_date(value: str) -> tuple[str, str | None]:
    """Return (iso_value, kind), kind in {None, "ddmm", "unparseable"}."""
    if value == "":
        return "", None
    for fmt, is_ddmm in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime(UTC_FORMAT), ("ddmm" if is_ddmm else "ok")
    return value, "unparseable"


def _normalise_dates(
    records: list[dict[str, Any]], columns: list[str], table: str
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "ddmm_converted": 0,
        "unparseable": 0,
        "per_column": {},
    }
    for column in DATE_FIELDS.get(table, ()):
        if column not in columns:
            continue
        column_counts = {"ddmm_converted": 0, "unparseable": 0}
        for row in records:
            iso_value, kind = _normalise_date(row[column])
            if kind == "ddmm":
                column_counts["ddmm_converted"] += 1
            elif kind == "unparseable":
                column_counts["unparseable"] += 1
            if kind != "unparseable":
                row[column] = iso_value
        counts["ddmm_converted"] += column_counts["ddmm_converted"]
        counts["unparseable"] += column_counts["unparseable"]
        counts["per_column"][column] = column_counts
    return counts


# ---------------------------------------------------------------------------
# Money and quantity
# ---------------------------------------------------------------------------


def _format_money(value: Decimal) -> str:
    quantized = value.quantize(CENT, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = abs(quantized)
    return format(quantized, "f")


def _as_decimal(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money_needs_strip(value: str) -> bool:
    return "$" in value or "," in value or re.search(r"(?i)usd", value) is not None


def _normalise_money(value: str) -> tuple[str, Decimal | None, bool]:
    """Return (output_string, Decimal or None, whether anything was stripped)."""
    stripped = _money_needs_strip(value)
    cleaned = value.replace("$", "").replace(",", "")
    cleaned = re.sub(r"(?i)usd", "", cleaned).strip()
    if cleaned == "":
        return value, None, stripped
    parsed = _as_decimal(cleaned)
    if parsed is None:
        return value, None, stripped
    return _format_money(parsed), parsed, stripped


def _normalise_money_fields(
    records: list[dict[str, Any]], columns: list[str], table: str
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "values_stripped": 0,
        "unparseable": 0,
        "per_column": {},
    }
    for column in MONEY_FIELDS.get(table, ()):
        if column not in columns:
            continue
        column_counts = {"values_stripped": 0, "unparseable": 0}
        for row in records:
            output, parsed, stripped = _normalise_money(row[column])
            if stripped:
                column_counts["values_stripped"] += 1
            if parsed is None:
                column_counts["unparseable"] += 1
            else:
                row[column] = output
        counts["values_stripped"] += column_counts["values_stripped"]
        counts["unparseable"] += column_counts["unparseable"]
        counts["per_column"][column] = column_counts
    return counts


def _normalise_qty(value: str) -> tuple[str, Decimal | None]:
    parsed = _as_decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return value, None
    return str(int(parsed)), parsed


def _normalise_qtys(records: list[dict[str, Any]], columns: list[str]) -> int:
    if "qty" not in columns:
        return 0
    invalid = 0
    for row in records:
        output, parsed = _normalise_qty(row["qty"])
        if parsed is None:
            invalid += 1
        else:
            row["qty"] = output
    return invalid


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def _created_sort_key(row: dict[str, Any]) -> tuple[int, datetime]:
    iso_value, kind = _normalise_date(row.get("created_at", ""))
    if kind != "unparseable" and iso_value:
        try:
            return 0, datetime.strptime(iso_value, UTC_FORMAT)
        except ValueError:
            pass
    return 1, datetime.max


def _merge_customers(
    customers: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, int],
]:
    groups: dict[str, list[dict[str, Any]]] = {}
    emails_normalised = 0
    for row in customers:
        original = row["email"]
        normalised = original.strip().lower()
        if normalised != original:
            emails_normalised += 1
        row["email"] = normalised
        groups.setdefault(normalised, []).append(row)

    canonical_ids: set[str] = set()
    aliases: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}
    merged = 0

    for email, group in groups.items():
        ordered = sorted(group, key=lambda row: (_created_sort_key(row), row["customer_id"]))
        canonical = ordered[0]
        canonical_ids.add(canonical["customer_id"])
        for duplicate in ordered[1:]:
            merged += 1
            alias_map[duplicate["customer_id"]] = canonical["customer_id"]
            aliases.append(
                {
                    "alias_customer_id": duplicate["customer_id"],
                    "canonical_customer_id": canonical["customer_id"],
                    "normalized_email": email,
                }
            )

    kept = [row for row in customers if row["customer_id"] in canonical_ids]
    id_map = {row["customer_id"]: row["customer_id"] for row in customers}
    id_map.update(alias_map)
    counts = {
        "merged": merged,
        "aliases": len(aliases),
        "emails_normalised": emails_normalised,
    }
    return kept, aliases, id_map, counts


def _add_canonical_customer(
    orders: list[dict[str, Any]], id_map: dict[str, str]
) -> int:
    unknown = 0
    for row in orders:
        customer_id = row.get("customer_id", "")
        canonical = id_map.get(customer_id)
        if canonical is None:
            unknown += 1
            canonical = ""
        row["canonical_customer_id"] = canonical
    return unknown


# ---------------------------------------------------------------------------
# Shipments and orders
# ---------------------------------------------------------------------------


def _annotate_shipments(shipments: list[dict[str, Any]]) -> int:
    unavailable = 0
    for row in shipments:
        status = row.get("status", "").strip().upper()
        has_status = status != "UNAVAILABLE" and row.get("last_event_at", "").strip() != ""
        row["carrier_status_available"] = "true" if has_status else "false"
        if not has_status:
            unavailable += 1
    return unavailable


def _orders_with_multiple_shipments(shipments: list[dict[str, Any]]) -> int:
    counts: dict[str, int] = defaultdict(int)
    for row in shipments:
        counts[row["order_id"]] += 1
    return sum(1 for count in counts.values() if count > 1)


def _annotate_orders(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> dict[str, int]:
    """Add carrier status and flag delivery-state disagreements.

    SHIPPED (OMS) and IN_TRANSIT (carrier) describe the same delivery state, so
    they are not a mismatch.  A mismatch is a disagreement about whether the
    shipment is delivered; the carrier feed is authoritative (RP-3.2 2.1).
    UNAVAILABLE / empty carrier feeds cannot conflict (RP-3.2 4.2).
    """
    by_order: dict[str, dict[str, Any]] = {}
    for row in shipments:
        by_order.setdefault(row["order_id"], row)

    mismatches = 0
    raw_label_differences = 0
    for row in orders:
        shipment = by_order.get(row["order_id"])
        carrier_status = shipment["status"].strip().upper() if shipment else ""
        oms_status = row["status"].strip().upper()
        row["carrier_status"] = carrier_status

        if carrier_status and oms_status != carrier_status:
            raw_label_differences += 1

        if carrier_status in {"DELIVERED", "IN_TRANSIT"}:
            if oms_status in {"DELIVERED", "SHIPPED"}:
                mismatch = (oms_status == "DELIVERED") != (
                    carrier_status == "DELIVERED"
                )
            else:
                mismatch = oms_status != carrier_status
        else:
            mismatch = False

        row["status_mismatch"] = "true" if mismatch else "false"
        if mismatch:
            mismatches += 1

    return {
        "mismatches": mismatches,
        "raw_label_differences": raw_label_differences,
    }


def _date_sanity_checks(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Flag chronology inversions without changing any parsed value.

    All date values have already been normalized to UTC ISO-8601, so
    lexicographic comparison is safe.
    """
    by_order: dict[str, dict[str, Any]] = {}
    for row in shipments:
        by_order.setdefault(row["order_id"], row)

    order_after_ship: set[str] = set()
    ship_after_delivery: set[str] = set()
    for order in orders:
        shipment = by_order.get(order["order_id"])
        if shipment is None:
            continue
        order_date = order.get("order_date", "")
        shipped_at = shipment.get("shipped_at", "")
        delivered_at = shipment.get("delivered_at", "")
        if order_date and shipped_at and order_date > shipped_at:
            order_after_ship.add(order["order_id"])
        if shipped_at and delivered_at and shipped_at > delivered_at:
            ship_after_delivery.add(order["order_id"])

    return {
        "order_date_after_shipped_at": {
            "count": len(order_after_ship),
            "order_ids": sorted(order_after_ship),
        },
        "shipped_at_after_delivered_at": {
            "count": len(ship_after_delivery),
            "order_ids": sorted(ship_after_delivery),
        },
    }


# ---------------------------------------------------------------------------
# Refunds and line values
# ---------------------------------------------------------------------------


def _build_refunds_by_line(
    refunds: list[dict[str, Any]], items: list[dict[str, Any]]
) -> tuple[dict[tuple[str, str], tuple[Decimal, int]], list[dict[str, Any]], int, int]:
    line_keys = {(row["order_id"], row["line_no"]) for row in items}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in refunds:
        groups[(row["order_id"], row["line_no"])].append(row)

    multiple_lines = sum(1 for group in groups.values() if len(group) > 1)
    refunds_without_line = sum(
        1
        for row in refunds
        if (row["order_id"], row["line_no"]) not in line_keys
    )

    issued: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for key, group in groups.items():
        total = Decimal("0")
        count = 0
        for row in group:
            if row.get("status", "").strip().upper() != "ISSUED":
                continue
            amount = _as_decimal(row.get("amount", ""))
            if amount is None:
                continue
            total += amount
            count += 1
        if count:
            issued[key] = (total.quantize(CENT, rounding=ROUND_HALF_UP), count)

    def sort_key(key: tuple[str, str]) -> tuple[str, int, str]:
        order_id, line_no = key
        try:
            line_number = int(line_no)
        except ValueError:
            line_number = 10**9
        return order_id, line_number, line_no

    refund_lines = [
        {
            "order_id": order_id,
            "line_no": line_no,
            "refunded_issued": _format_money(total),
            "refund_count": str(count),
        }
        for (order_id, line_no), (total, count) in sorted(
            issued.items(), key=lambda item: sort_key(item[0])
        )
    ]
    return issued, refund_lines, multiple_lines, refunds_without_line


def _add_refund_fields(
    items: list[dict[str, Any]],
    issued: dict[tuple[str, str], tuple[Decimal, int]],
) -> int:
    over_refunded = 0
    for row in items:
        key = (row["order_id"], row["line_no"])
        unit_price = _as_decimal(row.get("unit_price", ""))
        qty = _as_decimal(row.get("qty", ""))
        refunded = issued.get(key, (Decimal("0.00"), 0))[0]

        if unit_price is None or qty is None:
            row["line_value"] = ""
            row["refunded_issued"] = _format_money(refunded)
            row["refundable_remaining"] = ""
            continue

        line_value = (unit_price * qty).quantize(CENT, rounding=ROUND_HALF_UP)
        remaining = line_value - refunded
        if remaining < 0:
            remaining = Decimal("0.00")
        remaining = remaining.quantize(CENT, rounding=ROUND_HALF_UP)

        row["line_value"] = _format_money(line_value)
        row["refunded_issued"] = _format_money(refunded)
        row["refundable_remaining"] = _format_money(remaining)
        if refunded > line_value:
            over_refunded += 1
    return over_refunded


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------


def _referential_counts(
    orders: list[dict[str, Any]],
    items: list[dict[str, Any]],
    shipments: list[dict[str, Any]],
) -> dict[str, int]:
    order_ids = {row["order_id"] for row in orders}
    return {
        "order_lines_unknown_order": sum(
            1 for row in items if row["order_id"] not in order_ids
        ),
        "shipments_unknown_order": sum(
            1 for row in shipments if row["order_id"] not in order_ids
        ),
    }


# ---------------------------------------------------------------------------
# Tickets: count only, never act on body text
# ---------------------------------------------------------------------------


def _ticket_counts(data_room: Path) -> dict[str, Any]:
    files = (
        ("tickets/support_inbox_export.json", "support_inbox_export"),
        ("inbox/public_tickets.json", "public_tickets"),
    )
    per_file: dict[str, Any] = {}
    total_tickets = 0
    total_senders = 0
    total_mentions = 0

    for relative_path, label in files:
        path = data_room / relative_path
        if not path.is_file():
            raise SystemExit(f"missing ticket file: {path}")
        tickets = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(tickets, list):
            raise SystemExit(f"ticket file is not a JSON list: {path}")

        senders = 0
        mentions = 0
        for ticket in tickets:
            raw_from = ticket.get("from", "")
            if isinstance(raw_from, str) and raw_from != raw_from.strip().lower():
                senders += 1

            text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
            for match in _ORDER_MENTION_RE.finditer(text):
                if re.fullmatch(r"LF-\d{6}", match.group(0)) is None:
                    mentions += 1

        per_file[label] = {
            "tickets": len(tickets),
            "sender_emails_needing_normalization": senders,
            "order_id_mentions_not_lf_######": mentions,
        }
        total_tickets += len(tickets)
        total_senders += senders
        total_mentions += mentions

    return {
        "tickets": total_tickets,
        "sender_emails_needing_normalization": total_senders,
        "order_id_mentions_noncanonical": total_mentions,
        "per_file": per_file,
        "note": (
            "Counted only; sender trim/lowercase and order-id normalization "
            "are applied at runtime by src/normalize.py. Ticket body text is "
            "never acted on."
        ),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _columns_after(
    columns: list[str], anchor: str, derived: list[str]
) -> list[str]:
    output: list[str] = []
    inserted = False
    for column in columns:
        output.append(column)
        if column == anchor:
            output.extend(derived)
            inserted = True
    if not inserted:
        output.extend(derived)
    return output


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_csv(path: Path, columns: list[str], records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in records:
            writer.writerow({column: _cell(row.get(column, "")) for column in columns})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv()

    data_room_raw = os.getenv("DATA_ROOM_DIR", "data_room")
    prepared_raw = os.getenv("PREPARED_DIR", "prepared")
    data_room = Path(data_room_raw)
    prepared = Path(prepared_raw)

    _guard_output_dir(data_room, prepared)
    if not data_room.is_dir():
        raise SystemExit(f"DATA_ROOM_DIR is not a directory: {data_room}")

    tables: dict[str, list[dict[str, Any]]] = {}
    columns_by_table: dict[str, list[str]] = {}
    source_rows: dict[str, int] = {}
    dropped_by_table: dict[str, int] = {}
    renamed_by_table: dict[str, dict[str, str]] = {}
    trimmed_total = 0

    for table in SOURCE_TABLES:
        records, columns = _read_source(data_room, table)
        source_rows[table] = len(records)

        records, columns, renames = _rename_columns(records, columns)
        renamed_by_table[table] = renames

        trimmed_total += _trim_cells(records)

        records, dropped = _drop_exact_duplicates(records, columns)
        dropped_by_table[table] = dropped

        tables[table] = records
        columns_by_table[table] = columns

    order_id_counts: dict[str, dict[str, int]] = {}
    for table in SOURCE_TABLES:
        order_id_counts[table] = _normalise_order_ids(
            tables[table], columns_by_table[table]
        )

    customers, aliases, customer_id_map, customer_counts = _merge_customers(
        tables["customers"]
    )
    orders_unknown_customer = _add_canonical_customer(
        tables["orders"], customer_id_map
    )

    raw_order_dates = {
        row["order_id"]: row["order_date"] for row in tables["orders"]
    }

    date_counts = {
        table: _normalise_dates(tables[table], columns_by_table[table], table)
        for table in SOURCE_TABLES
    }

    ambiguous_slash_dates: list[dict[str, str]] = []
    for row in tables["orders"]:
        raw_value = raw_order_dates.get(row["order_id"], "")
        parts = raw_value.split("/")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            first, second, year = (int(parts[0]), int(parts[1]), parts[2])
            if first <= 12 and second <= 12:
                ambiguous_slash_dates.append(
                    {
                        "order_id": row["order_id"],
                        "raw": raw_value,
                        "as_dd_mm": row["order_date"][:10],
                        "as_mm_dd": f"{year}-{first:02d}-{second:02d}",
                    }
                )
    ambiguous_slash_dates.sort(key=lambda item: item["order_id"])

    money_counts = {
        table: _normalise_money_fields(tables[table], columns_by_table[table], table)
        for table in SOURCE_TABLES
    }
    qty_invalid = _normalise_qtys(
        tables["order_items"], columns_by_table["order_items"]
    )

    shipments_unavailable = _annotate_shipments(tables["shipments"])
    multiple_shipment_orders = _orders_with_multiple_shipments(tables["shipments"])
    order_status_counts = _annotate_orders(tables["orders"], tables["shipments"])
    date_sanity = _date_sanity_checks(tables["orders"], tables["shipments"])

    issued, refund_lines, multiple_refunds, refunds_without_line = _build_refunds_by_line(
        tables["refunds"], tables["order_items"]
    )
    over_refunded_lines = _add_refund_fields(tables["order_items"], issued)

    referential = _referential_counts(
        tables["orders"],
        tables["order_items"],
        tables["shipments"],
    )
    referential["orders_unknown_customer"] = orders_unknown_customer

    ticket_counts = _ticket_counts(data_room)

    total_renamed = sum(len(renames) for renames in renamed_by_table.values())
    order_id_changed = sum(counts["changed"] for counts in order_id_counts.values())
    order_id_unparseable = sum(
        counts["unparseable"] for counts in order_id_counts.values()
    )
    ddmm_total = sum(counts["ddmm_converted"] for counts in date_counts.values())
    dates_unparseable = sum(counts["unparseable"] for counts in date_counts.values())
    money_stripped = sum(counts["values_stripped"] for counts in money_counts.values())
    money_unparseable = sum(counts["unparseable"] for counts in money_counts.values())

    # Column order in the written tables.
    orders_columns = _columns_after(
        columns_by_table["orders"], "customer_id", ["canonical_customer_id"]
    ) + ["carrier_status", "status_mismatch"]
    items_columns = columns_by_table["order_items"] + [
        "line_value",
        "refunded_issued",
        "refundable_remaining",
    ]
    shipments_columns = columns_by_table["shipments"] + ["carrier_status_available"]
    refund_line_columns = [
        "order_id",
        "line_no",
        "refunded_issued",
        "refund_count",
    ]

    outputs: dict[str, tuple[list[dict[str, Any]], list[str]]] = {
        "customers": (customers, columns_by_table["customers"]),
        "customer_aliases": (
            aliases,
            ["alias_customer_id", "canonical_customer_id", "normalized_email"],
        ),
        "orders": (tables["orders"], orders_columns),
        "order_items": (tables["order_items"], items_columns),
        "shipments": (tables["shipments"], shipments_columns),
        "refunds": (tables["refunds"], columns_by_table["refunds"]),
        "refunds_by_line": (refund_lines, refund_line_columns),
    }

    report = {
        "generated_by": "src/prepare.py",
        "inputs": {
            "data_room_dir": data_room_raw,
            "prepared_dir": prepared_raw,
            "source_rows": source_rows,
        },
        "outputs": {
            "table_count": len(outputs),
            "tables": {name: len(records) for name, (records, _) in outputs.items()},
        },
        "fixes": {
            "01_column_names": {
                "columns_renamed": total_renamed,
                "per_table": renamed_by_table,
            },
            "02_whitespace": {"cells_trimmed": trimmed_total},
            "03_duplicate_rows": {
                "dropped": dropped_by_table,
                "total_dropped": sum(dropped_by_table.values()),
            },
            "04_order_ids": {
                "changed": order_id_changed,
                "unparseable": order_id_unparseable,
                "per_table": order_id_counts,
            },
            "05_customer_merge": {
                **customer_counts,
                "customers_before": len(tables["customers"]),
                "customers_after": len(customers),
            },
            "06_dates": {
                "ddmm_converted": ddmm_total,
                "unparseable": dates_unparseable,
                "ambiguous_slash_dates": ambiguous_slash_dates,
                "per_table": date_counts,
            },
            "07_money": {
                "values_stripped": money_stripped,
                "unparseable": money_unparseable,
                "qty_not_whole": qty_invalid,
                "per_table": money_counts,
            },
            "08_shipments": {
                "carrier_status_unavailable": shipments_unavailable,
                "orders_with_multiple_shipments": multiple_shipment_orders,
            },
            "09_order_status": {
                **order_status_counts,
                "comparison": (
                    "delivery state: SHIPPED and IN_TRANSIT are consistent; "
                    "carrier feed wins (RP-3.2 2.1)"
                ),
            },
            "10_refunds": {
                "lines_with_multiple_refunds": multiple_refunds,
                "over_refunded_lines": over_refunded_lines,
                "refunds_without_matching_line": refunds_without_line,
                "refund_lines": len(refund_lines),
            },
            "11_referential_integrity": referential,
            "12_tickets": ticket_counts,
            "13_date_sanity": date_sanity,
        },
    }

    prepared.mkdir(parents=True, exist_ok=True)
    for name, (records, columns) in outputs.items():
        _write_csv(prepared / f"{name}.csv", columns, records)

    report_path = prepared / "prepare_report.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

    # Exactly one line per numbered data fix, even when the count is zero.
    column_word = "column" if total_renamed == 1 else "columns"
    print(f"renamed {total_renamed} {column_word} to snake_case")
    print(f"trimmed surrounding whitespace in {trimmed_total} cells")
    print(
        "dropped duplicate rows: "
        + ", ".join(f"{table}={dropped_by_table[table]}" for table in SOURCE_TABLES)
    )
    print(
        f"normalized order ids: {order_id_changed} changed, "
        f"{order_id_unparseable} unparseable"
    )
    print(
        f"merged {customer_counts['merged']} duplicate customers by normalized email"
    )
    print(
        f"converted {ddmm_total} DD/MM dates to ISO-8601 UTC, "
        f"{dates_unparseable} unparseable dates"
    )
    print(
        f"stripped currency and commas from {money_stripped} money values, "
        f"{money_unparseable} unparseable, {qty_invalid} non-whole qty"
    )
    print(
        f"carrier status unavailable for {shipments_unavailable} shipments, "
        f"{multiple_shipment_orders} orders with more than one shipment"
    )
    print(
        f"order status vs carrier status: {order_status_counts['mismatches']} "
        f"mismatches ({order_status_counts['raw_label_differences']} raw label "
        "differences, carrier wins)"
    )
    print(
        f"refunds: {multiple_refunds} lines with multiple refunds, "
        f"{over_refunded_lines} over-refunded lines, "
        f"{refunds_without_line} refunds with no matching line"
    )
    print(
        f"referential integrity: {referential['orders_unknown_customer']} orders "
        f"with unknown customer, {referential['order_lines_unknown_order']} order "
        f"lines with unknown order, {referential['shipments_unknown_order']} "
        "shipments with unknown order"
    )
    print(
        f"tickets: {ticket_counts['sender_emails_needing_normalization']} sender "
        f"emails need trim/lowercase, "
        f"{ticket_counts['order_id_mentions_noncanonical']} order-id mentions are "
        "not LF-###### (applied at runtime by src/normalize.py)"
    )
    print(
        "date sanity: "
        f"{date_sanity['order_date_after_shipped_at']['count']} orders with "
        "order_date after shipped_at, "
        f"{date_sanity['shipped_at_after_delivered_at']['count']} orders with "
        "shipped_at after delivered_at"
    )
    print(
        f"wrote {len(outputs)} tables and prepare_report.json to {prepared_raw}"
    )


if __name__ == "__main__":
    main()
