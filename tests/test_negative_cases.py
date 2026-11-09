"""Hand-written negative tickets in ``eval/cases/negative/``.

Each case is a schema ``Ticket`` plus the expected decision.  The deterministic
extractor is used here, so these tests need no model calls; the same cases are
posted to the live service by ``eval/run_eval.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.decide import process_ticket
from src.normalize import get_schema

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "eval" / "cases" / "negative"


def _load_cases() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CASES_DIR.glob("*.json"))
    ]


CASES = _load_cases()


def test_six_hand_written_negative_cases_exist():
    assert len(CASES) == 6
    assert len({case["case_id"] for case in CASES}) == 6
    assert len({case["ticket"]["ticket_id"] for case in CASES}) == 6


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_negative_case_meets_its_expected_decision(case, data, config, dataset):
    get_schema().Ticket.model_validate(case["ticket"])

    result = process_ticket(case["ticket"], data, config=config)
    expected = case["expected"]

    assert result.outcome == expected["outcome"]
    assert result.approver_role == expected["approver_role"]
    assert sorted(result.reason_codes) == sorted(expected["reason_codes"])
    assert result.human_approval_required is bool(
        expected["human_approval_required"]
    )


def test_negative_cases_cover_six_distinct_kinds():
    kinds = {case["kind"] for case in CASES}
    assert kinds == {
        "injection_in_forwarded_reply",
        "claimed_manager_approval_reference",
        "order_id_one_digit_off",
        "order_belongs_to_a_different_customer",
        "damage_claim_day_46_with_photo",
        "damage_claim_day_35_pdf_attachment",
    }
