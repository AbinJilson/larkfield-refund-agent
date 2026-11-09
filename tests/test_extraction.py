"""The deterministic extractor must reproduce the hand-written gold fixture."""

from __future__ import annotations

import pytest

from src.normalize import extract, normalize_ticket


def _line_map(lines) -> dict:
    mapped = {}
    for line in lines:
        product = line.product if hasattr(line, "product") else line["product"]
        qty = line.qty if hasattr(line, "qty") else line["qty"]
        key = product.casefold() if product else None
        mapped[key] = qty
    return mapped


def test_gold_covers_all_40_tickets(gold, tickets):
    assert len(gold) == 40
    assert set(gold) == set(tickets)


def test_every_gold_extraction_validates(gold, extraction_from_gold):
    for ticket_id, entry in gold.items():
        extraction = extraction_from_gold(entry, ticket_id)
        assert extraction.ticket_id == ticket_id
        assert extraction.ticket_type in {
            "REFUND",
            "RETURN",
            "ORDER_STATUS",
            "DELIVERY_ISSUE",
            "OTHER",
        }, ticket_id


@pytest.mark.parametrize("ticket_id", sorted(
    [
        "T001", "T002", "T003", "T004", "T005", "T006", "T007", "T008",
        "T009", "T010", "T011", "T012", "T013", "T014", "T015", "T016",
        "T017", "T018", "T019", "T020", "T021", "T022", "T023", "T024",
        "T025", "T026", "T027", "T028", "T029", "T030", "P01", "P02",
        "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10",
    ]
))
def test_deterministic_extraction_matches_gold(
    ticket_id, tickets, gold, data
):
    deterministic = extract(
        normalize_ticket(tickets[ticket_id]), data.product_names
    )
    entry = gold[ticket_id]

    assert deterministic.ticket_type == entry["ticket_type"], ticket_id
    assert deterministic.order_ids == entry["order_ids"], ticket_id
    assert deterministic.claim_kind == entry["claim_kind"], ticket_id
    assert deterministic.claims_fee == entry["claims_fee"], ticket_id
    assert deterministic.asks_whole_order == entry["asks_whole_order"], ticket_id
    assert _line_map(deterministic.affected_lines) == _line_map(
        entry["affected_lines"]
    ), ticket_id
    assert (
        deterministic.injection_probability >= 0.5
    ) == (entry["injection_probability"] >= 0.5), ticket_id
