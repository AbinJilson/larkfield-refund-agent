"""Per-run trace and model cost accounting.

Every model call is recorded with the model id, role, input/output tokens from
the provider's ``usage`` block, and a list-price cost computed with
``Decimal`` from ``EXTRACT_PRICE_INPUT_PER_MTOK`` and
``EXTRACT_PRICE_OUTPUT_PER_MTOK``.  The schema's ``Trace`` uses ``float`` for
``cost_usd``, so conversion happens only in :meth:`Trace.to_schema`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

MTOK = Decimal("1_000_000")
CENT = Decimal("0.01")


def _price(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return Decimal(default)
    try:
        return Decimal(raw.strip())
    except Exception:  # pragma: no cover - defensive
        return Decimal(default)


@dataclass(frozen=True)
class ModelCallRecord:
    model: str
    role: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cache_hit: bool = False
    request_id: str | None = None
    provider: str | None = None
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class Trace:
    """The calls made for one run, in order."""

    calls: list[ModelCallRecord] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def mark_tool(self, name: str) -> None:
        """Record a pipeline stage in the order it ran."""
        if name not in self.tools:
            self.tools.append(name)

    def record(
        self,
        *,
        model: str,
        role: str,
        input_tokens: int,
        output_tokens: int,
        cache_hit: bool = False,
        request_id: str | None = None,
        provider: str | None = None,
    ) -> ModelCallRecord:
        input_price = _price("EXTRACT_PRICE_INPUT_PER_MTOK", "0.25")
        output_price = _price("EXTRACT_PRICE_OUTPUT_PER_MTOK", "1.50")
        cost = (
            Decimal(int(input_tokens)) * input_price
            + Decimal(int(output_tokens)) * output_price
        ) / MTOK
        record = ModelCallRecord(
            model=model,
            role=role,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=cost.quantize(Decimal("0.00000001")),
            cache_hit=cache_hit,
            request_id=request_id,
            provider=provider,
        )
        self.calls.append(record)
        return record

    @property
    def input_tokens(self) -> int:
        return sum(call.input_tokens for call in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(call.output_tokens for call in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> Decimal:
        """List-price cost from the recorded tokens (cache hits included)."""
        return sum((call.cost_usd for call in self.calls), Decimal("0"))

    @property
    def billed_cost_usd(self) -> Decimal:
        """Cost actually incurred: cache hits are free."""
        return sum(
            (call.cost_usd for call in self.calls if not call.cache_hit),
            Decimal("0"),
        )

    @property
    def cache_hits(self) -> int:
        return sum(1 for call in self.calls if call.cache_hit)

    def to_schema(self):
        """Build the contract ``Trace`` (the only float conversion point)."""
        from .normalize import get_schema

        schema = get_schema()
        return schema.Trace(
            model_calls=[
                schema.ModelCall(
                    model=call.model,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                )
                for call in self.calls
            ],
            tools_called=list(self.tools),
            documents_used=[
                schema.DocumentUsed(
                    document="policies/refund_policy_RP-3.2.md",
                    version="3.2",
                )
            ],
            cost_usd=float(self.cost_usd),
        )
