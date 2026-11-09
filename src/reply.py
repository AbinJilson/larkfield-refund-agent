"""Templated customer replies.  No model calls, no ticket text echoed.

Every sentence is a constant in this module.  The only values inserted are the
normalized sender and a ``Decimal`` amount from the resolution, so nothing a
customer wrote can be quoted back or used to change the reply.  Internal roles
and policy clause ids never appear in customer-facing text.
"""

from __future__ import annotations

from decimal import Decimal

from .normalize import NormalizedTicket, get_schema

SUBJECT = "Update on your Larkfield request"


def _money(amount: Decimal | None) -> str:
    if amount is None:
        return ""
    return f"${amount:,.2f}"


def _ledger_only_refund(decision) -> bool:
    """True when the refund already recorded is only a ledger row.

    An ``ISSUED`` refund in ``refunds.csv`` is money that has gone out.  An
    earlier run's REFUND row that is still PROPOSED (or APPROVED but not yet
    issued) is only being processed, so the reply must not claim it has been
    refunded.
    """
    lines = decision.resolution.lines
    return any(line.ledger_refund > 0 for line in lines) and not any(
        line.issued_refund > 0 for line in lines
    )


def _reject_reasons(decision, reason_codes: list[str]) -> str:
    if "OUT_OF_WINDOW" in reason_codes:
        return (
            "The request is outside the refund window that applies to this "
            "order, so we are not able to approve it."
        )
    if "ALREADY_REFUNDED" in reason_codes:
        if _ledger_only_refund(decision):
            return (
                "A refund for that item is already being processed. We will "
                "not issue a second refund for it."
            )
        return (
            "This item has already been refunded, so there is nothing further "
            "for us to refund."
        )
    return "We are not able to approve this request under our refund policy."


def _invalid_reasons(reason_codes: list[str]) -> str:
    if "ORDER_NOT_FOUND" in reason_codes:
        return (
            "We could not find that order. Please check the order number and "
            "reply with it so we can look again."
        )
    if "CUSTOMER_MISMATCH" in reason_codes:
        return (
            "The order number does not match the email address on this "
            "account. Please write to us from the address used for the order."
        )
    return (
        "We could not verify the details needed to handle this request. "
        "Please reply with the order number and the item concerned."
    )


def _body(decision, normalized: NormalizedTicket) -> str:
    greeting = f"Hello {normalized.sender},\n\n"
    outcome = decision.outcome
    reasons = list(decision.reason_codes)
    amount = decision.amount

    if outcome == "PEND":
        text = (
            "Thank you for getting in touch. Your request is under review and "
            "no refund has been issued yet. We will contact you again once the "
            "review is complete."
        )
    elif outcome == "PROCEED":
        text = (
            f"Thank you for getting in touch. We have approved a refund of "
            f"{_money(amount)} for the affected item. It will be returned to "
            "your original payment method."
        )
    elif outcome == "PROCEED_MODIFIED":
        text = (
            f"Thank you for getting in touch. We have approved a refund of "
            f"{_money(amount)} for the affected item. This covers the item the "
            "request applies to; it does not include delivery charges or items "
            "that have already been refunded."
        )
    elif outcome == "REJECT":
        text = _reject_reasons(decision, reasons)
    elif outcome == "INVALID_REQUEST":
        text = _invalid_reasons(reasons)
    elif outcome == "ESCALATE":
        text = (
            "Thank you for getting in touch. Your case has been passed to the "
            "right team, who will follow up with you directly."
        )
    else:  # UNABLE_TO_VERIFY
        if "CARRIER_STATUS_UNAVAILABLE" in reasons:
            text = (
                "Thank you for getting in touch. We are checking with the "
                "carrier about your delivery and will contact you as soon as "
                "we have an update."
            )
        else:
            text = (
                "Thank you for getting in touch. We are still checking the "
                "details of your request and will contact you as soon as we "
                "can."
            )

    return greeting + text + "\n\nKind regards,\nLarkfield Customer Care"


def draft_reply(decision, normalized: NormalizedTicket):
    """Build the contract ``DraftReply`` for a decided run."""
    return get_schema().DraftReply(
        to=normalized.sender,
        subject=SUBJECT,
        body=_body(decision, normalized),
    )


def internal_error_reply(to: str):
    """Reply used when the pipeline itself failed (no check could complete)."""
    return get_schema().DraftReply(
        to=to,
        subject=SUBJECT,
        body=(
            f"Hello {to},\n\nThank you for getting in touch. We could not "
            "complete the checks on your request just now. It has been kept "
            "for our team to look at, and we will contact you as soon as we "
            "can.\n\nKind regards,\nLarkfield Customer Care"
        ),
    )
