"""Classifier prompt-version tests. No model calls, no data room."""

from __future__ import annotations

from src.classify import _choice_question, _llm_payload, prompt_version
from src.normalize import NormalizedTicket

V1_CRITERIA = {
    "REFUND": "customer asks for a refund or money back for an item",
    "RETURN": "customer wants to return an item, e.g. change of mind",
    "ORDER_STATUS": "customer asks about order or shipment status",
    "DELIVERY_ISSUE": "delivery problem, late delivery or non-delivery",
    "OTHER": "none of the above",
}


def _ticket() -> NormalizedTicket:
    return NormalizedTicket(
        ticket_id="T999",
        received_at="2026-11-09T10:00:00+00:00",
        sender="customer@example.com",
        order_ids=(),
        has_image_attachment=False,
        subject="Where is my order?",
        body="It has not arrived.",
        text="Where is my order?\nIt has not arrived.",
    )


def test_default_prompt_version_is_v2(monkeypatch):
    monkeypatch.delenv("CLASSIFY_PROMPT_VERSION", raising=False)

    assert prompt_version() == "v2"
    criteria = _choice_question()["criteria"]
    assert "not asking for money back" in criteria["RETURN"]
    assert "does not say the parcel has failed to arrive" in criteria["ORDER_STATUS"]


def test_v1_uses_the_exact_original_option_text(monkeypatch):
    monkeypatch.setenv("CLASSIFY_PROMPT_VERSION", "v1")

    assert prompt_version() == "v1"
    assert _choice_question()["criteria"] == V1_CRITERIA
    system = _llm_payload("model", _ticket(), "v1")["messages"][0]["content"]
    assert (
        "ticket_type (REFUND, RETURN, ORDER_STATUS, DELIVERY_ISSUE or OTHER)"
        in system
    )
    assert "ticket_type is one of" not in system


def test_v2_sends_the_sharpened_definitions(monkeypatch):
    monkeypatch.setenv("CLASSIFY_PROMPT_VERSION", "v2")

    system = _llm_payload("model", _ticket())["messages"][0]["content"]
    assert "ticket_type is one of" in system
    assert "including a damaged, faulty, wrong or missing item" in system
    assert "does not say the parcel has failed to arrive" in system


def test_unknown_prompt_version_falls_back_to_v2(monkeypatch):
    monkeypatch.setenv("CLASSIFY_PROMPT_VERSION", "v9")

    assert prompt_version() == "v2"
