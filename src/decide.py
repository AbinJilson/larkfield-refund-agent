"""The ordered rule list: the only place an outcome is chosen.

Each :class:`Rule` has an id, a policy clause, a condition and a result.
First match wins.  Money and probabilities are handled as ``Decimal`` here;
the schema's float fields are only for the HTTP boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from .checks import build_checks
from .normalize import (
    Extraction,
    NormalizedTicket,
    REFUND_CLAIMS,
    STATUS_CLAIMS,
    extract,
    normalize_ticket,
)
from .resolve import (
    LedgerRefund,
    PreparedData,
    Resolution,
    ZERO,
    resolve,
)

APPROVAL_LIMIT = Decimal("150.00")
LOST_AFTER_HOURS = Decimal("240")
EXTENDED_WINDOW_HOURS = Decimal("1080")  # 45 * 24
DAY_HOURS = Decimal(24)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionConfig:
    ticket_type_min_prob: Decimal = Decimal("0.6")
    injection_min_prob: Decimal = Decimal("0.5")

    @classmethod
    def from_env(cls) -> "DecisionConfig":
        """Read the two thresholds without touching ``.env``."""
        return cls(
            ticket_type_min_prob=Decimal(
                os.getenv("TICKET_TYPE_MIN_PROB", "0.6")
            ),
            injection_min_prob=Decimal(os.getenv("INJECTION_MIN_PROB", "0.5")),
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerAction:
    action: str
    amount: Decimal | None
    status: str


@dataclass(frozen=True)
class RuleResult:
    outcome: str
    reason_codes: list[str]
    approver_role: str | None
    amount: Decimal | None
    basis: str


@dataclass(frozen=True)
class DecisionResult:
    ticket_id: str
    ticket_type: str
    outcome: str
    reason_codes: list[str]
    approver_role: str | None
    human_approval_required: bool
    amount: Decimal | None
    rule_id: int
    policy_clause: str | None
    basis: str
    checks: list
    extraction: Extraction
    resolution: Resolution
    ledger_action: LedgerAction


@dataclass(frozen=True)
class RuleContext:
    extraction: Extraction
    normalized: NormalizedTicket
    resolution: Resolution
    checks: list
    config: DecisionConfig

    @property
    def type_ok(self) -> bool:
        return (
            self.extraction.ticket_type != "OTHER"
            and Decimal(str(self.extraction.ticket_type_confidence))
            >= self.config.ticket_type_min_prob
        )

    @property
    def is_refund(self) -> bool:
        return self.extraction.ticket_type in {"REFUND", "RETURN"}

    @property
    def is_status(self) -> bool:
        return self.extraction.ticket_type in {
            "ORDER_STATUS",
            "DELIVERY_ISSUE",
        }

    @property
    def total(self) -> Decimal | None:
        return self.resolution.total_remaining

    @property
    def damage_exception(self) -> bool:
        hours = self.resolution.hours_since_delivery
        return (
            self.extraction.claim_kind in {"DAMAGED", "FAULTY"}
            and self.normalized.has_image_attachment
            and hours is not None
            and Decimal("720") < hours <= EXTENDED_WINDOW_HOURS
        )

    @property
    def partial_lines(self) -> bool:
        return (
            self.extraction.asks_whole_order or self.extraction.claims_fee
        )

    @property
    def already_refunded(self) -> bool:
        return self.resolution.any_prior_refund


@dataclass(frozen=True)
class Rule:
    rule_id: int
    clause: str | None
    condition: Callable[[RuleContext], bool]
    result: Callable[[RuleContext], RuleResult]
    description: str


# ---------------------------------------------------------------------------
# Helpers for rule results
# ---------------------------------------------------------------------------


def _reasons(ctx: RuleContext, *, approval: bool = False) -> list[str]:
    reasons: list[str] = []
    if ctx.damage_exception:
        reasons.append("DAMAGE_EXCEPTION")
    if ctx.partial_lines:
        reasons.append("PARTIAL_LINES")
    if ctx.already_refunded:
        reasons.append("ALREADY_REFUNDED")
    if approval:
        reasons.append("APPROVAL_REQUIRED")
    return reasons


def _unresolved_detail(ctx: RuleContext) -> str:
    """Detail for rule 14; the code stays empty (no invented reason codes)."""
    if ctx.resolution.has_issue("AFFECTED_QTY_UNKNOWN"):
        return "the affected quantity could not be resolved (2.2, 2.5)"
    if (
        ctx.resolution.has_issue("PRODUCT_AMBIGUOUS")
        or ctx.resolution.has_issue("PRODUCT_NOT_IN_ORDER")
        or ctx.resolution.has_issue("NO_AFFECTED_LINE")
    ):
        return "the affected line could not be resolved (2.2, 2.5)"
    return "the affected line or quantity could not be resolved (2.2, 2.5)"


# ---------------------------------------------------------------------------
# Rule conditions and results
# ---------------------------------------------------------------------------


def _r1_condition(ctx: RuleContext) -> bool:
    return (
        Decimal(str(ctx.extraction.injection_probability))
        >= ctx.config.injection_min_prob
    )


def _r1_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "ESCALATE",
        ["POLICY_OVERRIDE_ATTEMPT"],
        "TRUST_SAFETY",
        None,
        "instruction-like text / claimed pre-approval (5.2); no refund proposed",
    )


def _r2_condition(ctx: RuleContext) -> bool:
    return (
        ctx.resolution.order_id is None
        or ctx.resolution.order is None
        or ctx.resolution.has_issue("MULTIPLE_ORDER_IDS")
    )


def _r2_result(ctx: RuleContext) -> RuleResult:
    basis = (
        "no order id mentioned"
        if ctx.resolution.order_id is None
        and not ctx.resolution.has_issue("MULTIPLE_ORDER_IDS")
        else (
            "more than one order id mentioned"
            if ctx.resolution.has_issue("MULTIPLE_ORDER_IDS")
            else f"{ctx.resolution.order_id} is not in orders.csv"
        )
    )
    return RuleResult(
        "INVALID_REQUEST", ["ORDER_NOT_FOUND"], None, None, basis
    )


def _r3_condition(ctx: RuleContext) -> bool:
    return (
        ctx.resolution.order is not None
        and ctx.resolution.customer_mismatch
    )


def _r3_result(ctx: RuleContext) -> RuleResult:
    email = ctx.resolution.customer.email if ctx.resolution.customer else "(none)"
    return RuleResult(
        "INVALID_REQUEST",
        ["CUSTOMER_MISMATCH"],
        None,
        None,
        (
            f"{ctx.resolution.order_id} belongs to {email}; "
            f"sender is {ctx.resolution.sender}"
        ),
    )


def _r4_condition(ctx: RuleContext) -> bool:
    if not ctx.type_ok or ctx.extraction.claim_kind not in STATUS_CLAIMS:
        return False
    shipment = ctx.resolution.shipment
    return (
        shipment is None
        or shipment.status == "UNAVAILABLE"
        or shipment.last_event_at is None
    )


def _r4_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "UNABLE_TO_VERIFY",
        ["CARRIER_STATUS_UNAVAILABLE"],
        None,
        None,
        "status/non-delivery claim but the carrier status cannot be obtained (4.2)",
    )


def _r5_condition(ctx: RuleContext) -> bool:
    shipment = ctx.resolution.shipment
    return (
        ctx.type_ok
        and shipment is not None
        and shipment.status != "DELIVERED"
        and ctx.resolution.hours_since_last_event is not None
        and ctx.resolution.hours_since_last_event > LOST_AFTER_HOURS
    )


def _r5_result(ctx: RuleContext) -> RuleResult:
    hours = ctx.resolution.hours_since_last_event
    return RuleResult(
        "ESCALATE",
        ["LOST_IN_TRANSIT"],
        "CARRIER_CLAIMS",
        None,
        f"{hours:.2f} hours since the last carrier scan (> 240; 5.1)",
    )


def _r6_condition(ctx: RuleContext) -> bool:
    return ctx.type_ok and ctx.extraction.claim_kind in STATUS_CLAIMS


def _r6_result(ctx: RuleContext) -> RuleResult:
    shipment = ctx.resolution.shipment
    status = shipment.status if shipment else "unknown"
    return RuleResult(
        "PROCEED",
        [],
        None,
        None,
        f"status question; carrier status {status}, movement within 10 days (5.3)",
    )


def _r6b_condition(ctx: RuleContext) -> bool:
    if not ctx.type_ok or ctx.extraction.claim_kind not in REFUND_CLAIMS:
        return False
    shipment = ctx.resolution.shipment
    return shipment is None or shipment.delivered_at is None


def _r6b_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "UNABLE_TO_VERIFY",
        ["CARRIER_STATUS_UNAVAILABLE"],
        None,
        None,
        "refund/return but no carrier delivery date, so the window cannot be applied (4.2)",
    )


def _r7_condition(ctx: RuleContext) -> bool:
    return (
        ctx.type_ok
        and ctx.is_refund
        and ctx.resolution.hours_since_delivery is not None
        and ctx.resolution.hours_since_delivery
        > ctx.resolution.window_hours
    )


def _r7_result(ctx: RuleContext) -> RuleResult:
    hours = ctx.resolution.hours_since_delivery
    window = ctx.resolution.window_hours
    return RuleResult(
        "REJECT",
        ["OUT_OF_WINDOW"],
        None,
        None,
        (
            f"{hours:.2f} hours since delivery > {window / DAY_HOURS:.0f}-day "
            "window (2.1, 2.3, 2.6)"
        ),
    )


def _r8_condition(ctx: RuleContext) -> bool:
    return (
        ctx.type_ok
        and ctx.is_refund
        and ctx.total is not None
        and ctx.total == ZERO
    )


def _r8_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "REJECT",
        ["ALREADY_REFUNDED"],
        None,
        None,
        "every affected line is fully refunded (2.5)",
    )


def _r9_condition(ctx: RuleContext) -> bool:
    return (
        ctx.type_ok
        and ctx.is_refund
        and ctx.total is not None
        and ctx.total > APPROVAL_LIMIT
    )


def _r9_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "PEND",
        _reasons(ctx, approval=True),
        "SUPPORT_LEAD",
        ctx.total,
        f"remaining {ctx.total:.2f} > 150.00 (3.2)",
    )


def _r10_condition(ctx: RuleContext) -> bool:
    return (
        ctx.type_ok
        and ctx.is_refund
        and ctx.total is not None
        and ctx.total <= APPROVAL_LIMIT
        and (ctx.partial_lines or ctx.already_refunded)
    )


def _r10_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "PROCEED_MODIFIED",
        _reasons(ctx),
        None,
        ctx.total,
        (
            f"remaining {ctx.total:.2f} <= 150.00, cut to the affected lines "
            "or prior refunds (2.4, 2.5, 3.1)"
        ),
    )


def _r11_condition(ctx: RuleContext) -> bool:
    return (
        ctx.type_ok
        and ctx.is_refund
        and ctx.total is not None
        and ctx.total <= APPROVAL_LIMIT
    )


def _r11_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "PROCEED",
        _reasons(ctx),
        None,
        ctx.total,
        f"remaining {ctx.total:.2f} <= 150.00 (3.1)",
    )


def _r12_condition(ctx: RuleContext) -> bool:
    return not ctx.type_ok


def _r12_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "ESCALATE",
        [],
        "SUPPORT_LEAD",
        None,
        (
            f"type {ctx.extraction.ticket_type} at confidence "
            f"{ctx.extraction.ticket_type_confidence:.2f} is below the "
            "classification threshold; no clause picks an outcome (rule 13)"
        ),
    )


def _r13_condition(ctx: RuleContext) -> bool:
    return True


def _r13_result(ctx: RuleContext) -> RuleResult:
    return RuleResult(
        "UNABLE_TO_VERIFY",
        [],
        None,
        None,
        _unresolved_detail(ctx),
    )


# ---------------------------------------------------------------------------
# The ordered rule list
# ---------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        1,
        "RP-3.2 5.2",
        _r1_condition,
        _r1_result,
        "instruction-like text, claimed pre-approval, chargeback or legal threat",
    ),
    Rule(
        2,
        "RP-3.2 4.1",
        _r2_condition,
        _r2_result,
        "no order id, multiple order ids, or order not in orders.csv",
    ),
    Rule(
        3,
        "RP-3.2 4.1",
        _r3_condition,
        _r3_result,
        "order exists but does not belong to the sender",
    ),
    Rule(
        4,
        "RP-3.2 4.2",
        _r4_condition,
        _r4_result,
        "status/non-delivery claim and carrier status unavailable",
    ),
    Rule(
        5,
        "RP-3.2 5.1",
        _r5_condition,
        _r5_result,
        "shipment not delivered and no movement for more than 10 days",
    ),
    Rule(
        6,
        "RP-3.2 5.3",
        _r6_condition,
        _r6_result,
        "status question on a normally moving or delivered shipment",
    ),
    Rule(
        7,
        "RP-3.2 4.2",
        _r6b_condition,
        _r6b_result,
        "refund/return but the carrier delivery date cannot be obtained",
    ),
    Rule(
        8,
        "RP-3.2 2.1, 2.3, 2.6",
        _r7_condition,
        _r7_result,
        "refund/return outside the exact 30/45-day window",
    ),
    Rule(
        9,
        "RP-3.2 2.5",
        _r8_condition,
        _r8_result,
        "every affected line is already fully refunded",
    ),
    Rule(
        10,
        "RP-3.2 3.2",
        _r9_condition,
        _r9_result,
        "remaining amount above $150",
    ),
    Rule(
        11,
        "RP-3.2 2.4, 2.5, 3.1",
        _r10_condition,
        _r10_result,
        "within $150 but cut to the affected lines or prior refunds",
    ),
    Rule(
        12,
        "RP-3.2 3.1, 2.6",
        _r11_condition,
        _r11_result,
        "within $150 and within the affected lines",
    ),
    Rule(
        13,
        None,
        _r12_condition,
        _r12_result,
        "type OTHER or classification below TICKET_TYPE_MIN_PROB",
    ),
    Rule(
        14,
        "RP-3.2 2.2, 2.5",
        _r13_condition,
        _r13_result,
        "anything else that could not be resolved",
    ),
)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _ledger_action(result: RuleResult) -> LedgerAction:
    if result.outcome in {"PROCEED", "PROCEED_MODIFIED", "PEND"}:
        return LedgerAction("REFUND", result.amount, "PROPOSED")
    if result.outcome == "ESCALATE":
        return LedgerAction("ESCALATION", None, "PROPOSED")
    return LedgerAction("REPLY", None, "PROPOSED")


def decide(
    extraction: Extraction,
    normalized: NormalizedTicket,
    resolution: Resolution,
    checks: list,
    config: DecisionConfig | None = None,
) -> DecisionResult:
    """Walk the ordered rules.  The first matching rule picks the outcome."""
    resolved_config = config or DecisionConfig.from_env()
    context = RuleContext(
        extraction=extraction,
        normalized=normalized,
        resolution=resolution,
        checks=checks,
        config=resolved_config,
    )
    for rule in RULES:
        if not rule.condition(context):
            continue
        result = rule.result(context)
        return DecisionResult(
            ticket_id=extraction.ticket_id,
            ticket_type=extraction.ticket_type,
            outcome=result.outcome,
            reason_codes=list(result.reason_codes),
            approver_role=result.approver_role,
            human_approval_required=result.approver_role is not None,
            amount=result.amount,
            rule_id=rule.rule_id,
            policy_clause=rule.clause,
            basis=result.basis,
            checks=checks,
            extraction=extraction,
            resolution=resolution,
            ledger_action=_ledger_action(result),
        )
    raise RuntimeError("the ordered rule list did not match")  # pragma: no cover


def process_ticket(
    ticket,
    data: PreparedData,
    ledger: Sequence[LedgerRefund] = (),
    config: DecisionConfig | None = None,
) -> DecisionResult:
    """normalize -> extract -> resolve -> checks -> decide for one ticket."""
    resolved_config = config or DecisionConfig.from_env()
    normalized = normalize_ticket(ticket)
    extraction = extract(normalized, data.product_names)
    return process_extraction(
        ticket, extraction, data, ledger=ledger, config=resolved_config
    )


def process_extraction(
    ticket,
    extraction: Extraction,
    data: PreparedData,
    ledger: Sequence[LedgerRefund] = (),
    config: DecisionConfig | None = None,
) -> DecisionResult:
    """Same as :func:`process_ticket` but with a supplied extraction (gold)."""
    resolved_config = config or DecisionConfig.from_env()
    normalized = normalize_ticket(ticket)
    resolution = resolve(extraction, normalized, data, ledger)
    checks = build_checks(
        normalized,
        extraction,
        resolution,
        ticket_type_min_prob=resolved_config.ticket_type_min_prob,
        injection_min_prob=resolved_config.injection_min_prob,
    )
    return decide(
        extraction,
        normalized,
        resolution,
        checks,
        config=resolved_config,
    )
