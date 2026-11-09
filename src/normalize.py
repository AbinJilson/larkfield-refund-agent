"""Normalize a ticket and extract the internal :class:`Extraction`.

This module has no model calls.  The deterministic extractor here is the
fallback that the LLM extractor will later replace: everything downstream
works from the same ``Extraction`` object.

The email body is data only.  Instruction-like text raises
``injection_probability`` (RP-3.2 5.2); it never changes the extraction or an
outcome.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Literal, Mapping, Sequence, get_args

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Contract schema, loaded by path from DATA_ROOM_DIR
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _load_schema(path: str) -> ModuleType:
    module_name = "larkfield_response_schema"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load contract schema from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def data_room_dir() -> Path:
    """Data room path from ``DATA_ROOM_DIR`` (default ``data_room``)."""
    return Path(os.getenv("DATA_ROOM_DIR", "data_room"))


def get_schema() -> ModuleType:
    """Import ``schema/response_schema.py`` by path, never redefining it."""
    path = (data_room_dir() / "schema" / "response_schema.py").resolve()
    if not path.is_file():
        raise FileNotFoundError(f"contract schema not found: {path}")
    return _load_schema(str(path))


# ---------------------------------------------------------------------------
# Claim vocabulary and the internal extraction model
# ---------------------------------------------------------------------------

ClaimKind = Literal[
    "DAMAGED",
    "FAULTY",
    "WRONG_ITEM",
    "NOT_AS_DESCRIBED",
    "CHANGE_OF_MIND",
    "NOT_DELIVERED",
    "ORDER_STATUS",
    "FEE",
    "UNSPECIFIED",
]

CLAIM_KINDS: tuple[str, ...] = get_args(ClaimKind)

REFUND_CLAIMS = frozenset(
    {
        "DAMAGED",
        "FAULTY",
        "WRONG_ITEM",
        "NOT_AS_DESCRIBED",
        "CHANGE_OF_MIND",
        "FEE",
        "UNSPECIFIED",
    }
)
STATUS_CLAIMS = frozenset({"NOT_DELIVERED", "ORDER_STATUS"})


class ExtractedLine(BaseModel):
    """A product the customer points at, with the affected quantity."""

    product: str | None = None
    qty: int | None = Field(default=None, ge=1)


class Extraction(BaseModel):
    """Structured fields every downstream check and rule works from."""

    ticket_id: str
    ticket_type: str
    ticket_type_confidence: float = Field(ge=0.0, le=1.0)
    order_ids: list[str]
    affected_lines: list[ExtractedLine]
    claim_kind: ClaimKind
    claims_fee: bool = False
    asks_whole_order: bool = False
    injection_probability: float = Field(ge=0.0, le=1.0)

    @field_validator("ticket_type")
    @classmethod
    def _known_ticket_type(cls, value: str) -> str:
        allowed = get_args(get_schema().TicketType)
        if value not in allowed:
            raise ValueError(f"unknown ticket_type {value!r}; expected {allowed}")
        return value

    @property
    def is_refund(self) -> bool:
        return self.ticket_type in {"REFUND", "RETURN"}

    @property
    def is_status(self) -> bool:
        return self.ticket_type in {"ORDER_STATUS", "DELIVERY_ISSUE"}


class NormalizedTicket(BaseModel):
    """A ticket after trimming the sender and canonicalizing order ids."""

    ticket_id: str
    received_at: str
    sender: str
    order_ids: list[str]
    has_image_attachment: bool
    subject: str
    body: str
    text: str


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# LF-######, LF######, #######, "order ######", "order order ######".
ORDER_MENTION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:LF\s*-?\s*|#\s*|order\s+(?:id\s+|number\s+)?)"
    r"(\d{6})(?!\d)"
)


def normalize_order_id(digits: str) -> str:
    return f"LF-{digits}"


def order_ids_in(text: str) -> list[str]:
    """All distinct order ids mentioned, in first-mention order."""
    seen: list[str] = []
    for match in ORDER_MENTION_RE.finditer(text):
        order_id = normalize_order_id(match.group(1))
        if order_id not in seen:
            seen.append(order_id)
    return seen


def normalize_sender(raw: str) -> str:
    return raw.strip().lower()


def has_image_attachment(ticket: Any) -> bool:
    return any(
        str(attachment.content_type).lower().startswith("image/")
        for attachment in ticket.attachments
    )


def normalize_ticket(ticket: Mapping[str, Any] | Any) -> NormalizedTicket:
    """Accept a dict or a schema ``Ticket`` and return the normalized form."""
    model = ticket
    if not hasattr(model, "attachments"):
        model = get_schema().Ticket.model_validate(dict(ticket))

    text = f"{model.subject}\n{model.body}"
    return NormalizedTicket(
        ticket_id=model.ticket_id,
        received_at=model.received_at,
        sender=normalize_sender(model.from_),
        order_ids=order_ids_in(text),
        has_image_attachment=has_image_attachment(model),
        subject=model.subject,
        body=model.body,
        text=text,
    )


# ---------------------------------------------------------------------------
# Deterministic extraction (the LLM will replace this later)
# ---------------------------------------------------------------------------

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:your|all|any)\s+(?:previous|prior|above)\s+instructions?", re.I),
    re.compile(r"note\s+to\s+the\s+ai", re.I),
    re.compile(r"\[\s*system\s*\]", re.I),
    re.compile(r"pre[-\s]?approved", re.I),
    re.compile(r"authori[sz]ed\s+to\s+(?:process|issue|refund)", re.I),
    re.compile(r"without\s+(?:review|approval)", re.I),
    re.compile(r"mark\s+human_approval", re.I),
)

NOT_DELIVERED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\bnever arrived\b",
        r"\bhasn'?t arrived\b",
        r"\bhas not arrived\b",
        r"\bnot arrived\b",
        r"\bnever received\b",
        r"\bnot received\b",
        r"\bnot delivered\b",
        r"\bnever turned up\b",
    )
)

STATUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\bwhere is my order\b",
        r"\btracking hasn'?t\b",
        r"\btracking has not\b",
        r"\bstuck for ages\b",
        r"\bhasn'?t moved\b",
        r"\bhas not moved\b",
        r"\bgone missing\b",
        r"\bis it lost\b",
        r"\bwhat is going on\b",
    )
)

WRONG_ITEM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\bwrong item\b",
        r"\breceived something else\b",
        r"\bnot what i ordered\b",
        r"\bwrong product\b",
    )
)

CHANGE_OF_MIND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\bnot what i hoped\b",
        r"\bchanged my mind\b",
        r"\bdon'?t want\b",
        r"\bdo not want\b",
        r"\bno longer want\b",
    )
)

FAULTY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (r"\bfaulty\b", r"\bfault\b", r"\bdefective\b")
)

DAMAGED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (r"\bdamaged\b", r"\bbroken\b", r"\bcracked\b", r"\bsmashed\b", r"\bchipped\b")
)

NOT_AS_DESCRIBED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (r"\bnot as described\b", r"\bdoesn'?t match\b", r"\bnot what was described\b")
)

FEE_PATTERN = re.compile(
    r"\b(?:delivery charge|shipping fee|shipping charge|postage)\b", re.I
)
RETURN_PATTERN = re.compile(r"\breturn(?:ing|ed)?\b", re.I)
REFUND_PATTERN = re.compile(r"\brefund", re.I)
WHOLE_ORDER_PATTERN = re.compile(
    r"\b(?:whole order|full order|money back for the whole|everything|all of it)\b",
    re.I,
)
ONE_OF_TWO_RE = r"one of (?:my |the )?(?:two|2)\s+[\w\s()\-]{0,40}"


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def injection_probability(text: str) -> float:
    """1.0 when any 5.2 pattern is present, else 0.0.

    The probability is deliberately decisive: these are explicit
    instruction-like phrases, not tone.  ``INJECTION_MIN_PROB`` gates it.
    """
    return 1.0 if _matches(INJECTION_PATTERNS, text) else 0.0


def claim_kind_for(text: str) -> ClaimKind:
    if _matches(NOT_DELIVERED_PATTERNS, text):
        return "NOT_DELIVERED"
    if _matches(STATUS_PATTERNS, text):
        return "ORDER_STATUS"
    if _matches(WRONG_ITEM_PATTERNS, text):
        return "WRONG_ITEM"
    if _matches(CHANGE_OF_MIND_PATTERNS, text):
        return "CHANGE_OF_MIND"
    if _matches(FAULTY_PATTERNS, text):
        return "FAULTY"
    if _matches(DAMAGED_PATTERNS, text):
        return "DAMAGED"
    if _matches(NOT_AS_DESCRIBED_PATTERNS, text):
        return "NOT_AS_DESCRIBED"
    if FEE_PATTERN.search(text):
        return "FEE"
    return "UNSPECIFIED"


def ticket_type_for(claim_kind: str, text: str) -> str:
    if claim_kind == "NOT_DELIVERED":
        return "DELIVERY_ISSUE"
    if claim_kind == "ORDER_STATUS":
        return "ORDER_STATUS"
    if claim_kind in {"DAMAGED", "FAULTY", "WRONG_ITEM", "NOT_AS_DESCRIBED"}:
        return "REFUND"
    if claim_kind == "CHANGE_OF_MIND":
        return "RETURN" if RETURN_PATTERN.search(text) else "REFUND"
    if claim_kind == "FEE":
        return "REFUND"
    # UNSPECIFIED: an explicit refund/return wording still gives a type.
    if RETURN_PATTERN.search(text):
        return "RETURN"
    if REFUND_PATTERN.search(text):
        return "REFUND"
    return "OTHER"


def _affected_qty(text: str, product: str) -> int:
    """Affected quantity for a named product.

    Defaults to one (the ticket set is single-unit wording except T026, which
    says "one of my two").  ``None`` is reserved for genuinely plural wording
    with no count; resolve.py then stops rather than guessing.
    """
    lowered = text.lower()
    product_lowered = product.lower()
    if re.search(ONE_OF_TWO_RE + re.escape(product_lowered), lowered):
        return 1
    plural = re.search(
        r"\b(?:the|my|both|all)\s+" + re.escape(product_lowered) + r"s\b", lowered
    )
    if plural:
        return None  # type: ignore[return-value]
    return 1


def _product_mentions(text: str, product_names: Sequence[str]) -> list[str]:
    """Known product names mentioned in the text, longest name first."""
    lowered = text.lower()
    found: list[str] = []
    for product in sorted(product_names, key=len, reverse=True):
        if product.lower() in lowered and product not in found:
            found.append(product)
    # Drop a shorter name that is wholly inside a longer matched name.
    kept: list[str] = []
    for product in found:
        if any(
            product != other and product.lower() in other.lower() for other in found
        ):
            continue
        kept.append(product)
    return kept


def extract(
    normalized: NormalizedTicket, product_names: Sequence[str]
) -> Extraction:
    """Deterministic extraction for one normalized ticket."""
    text = normalized.text
    claim = claim_kind_for(text)
    ticket_type = ticket_type_for(claim, text)
    injection = injection_probability(text)

    lines: list[ExtractedLine] = []
    if claim in STATUS_CLAIMS:
        lines = []
    else:
        for product in _product_mentions(text, product_names):
            lines.append(
                ExtractedLine(product=product, qty=_affected_qty(text, product))
            )
        if not lines and REFUND_PATTERN.search(text):
            # A refund/return with no product named: null signals "use the
            # single line if the order has exactly one" (DECIDED ruling 6).
            lines = [ExtractedLine(product=None, qty=None)]

    confidence = 0.95 if ticket_type != "OTHER" else 0.30
    return Extraction(
        ticket_id=normalized.ticket_id,
        ticket_type=ticket_type,
        ticket_type_confidence=confidence,
        order_ids=list(normalized.order_ids),
        affected_lines=lines,
        claim_kind=claim,
        claims_fee=bool(FEE_PATTERN.search(text)),
        asks_whole_order=bool(WHOLE_ORDER_PATTERN.search(text)),
        injection_probability=injection,
    )
