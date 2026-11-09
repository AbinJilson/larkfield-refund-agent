"""SQLAlchemy persistence for runs, the ledger and decision calls.

The service is the only writer.  ``runs`` stores the full ``TicketResponse``
as JSON so a repeated ticket can be returned byte-for-byte without recomputing.
``ledger`` mirrors the contract ``LedgerEntry`` plus the internal
``order_id`` / line allocation needed by the RP-3.2 2.5 deduction.  Money is
``Numeric(12, 2)`` in the database and becomes a ``Decimal`` here; the schema's
float appears only when a ``LedgerEntry`` is built.

Works on SQLite and Postgres: no SQLite-only SQL and no dialect-specific types.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Integer, Numeric, String, Text, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .normalize import get_schema
from .resolve import LedgerRefund


class StoreError(RuntimeError):
    """Base class for persistence errors the API maps to a response."""


class NotFoundError(StoreError):
    """No run with that id."""


class ConflictError(StoreError):
    """The run is not in a state that accepts this decision."""


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class LedgerRow(Base):
    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    recorded_at: Mapped[str] = mapped_column(String(40), nullable=False)
    # Internal only: which order/line a REFUND row covers, for the 2.5
    # deduction on later tickets.  ``detail_json`` holds one allocation per
    # affected line; ``ledger`` rows keep the contract shape otherwise.
    order_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    approver: Mapped[str] = mapped_column(String(200), nullable=False)
    recorded_at: Mapped[str] = mapped_column(String(40), nullable=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_url(explicit: str | None) -> str:
    return (explicit or os.getenv("DATABASE_URL", "sqlite:///runs.db")).strip()


class Store:
    """Thin, synchronous persistence layer used by the FastAPI app."""

    def __init__(self, database_url: str | None = None) -> None:
        url = _database_url(database_url)
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url or url in {"sqlite://", "sqlite:///"}:
                kwargs["poolclass"] = StaticPool
        self.database_url = url
        self.engine = create_engine(url, **kwargs)
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    # -- reads ----------------------------------------------------------

    def health(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def stored_response(self, ticket_id: str) -> str | None:
        with self._session() as session:
            row = session.scalar(
                select(RunRow).where(RunRow.ticket_id == ticket_id)
            )
            return row.response_json if row else None

    def run_exists(self, run_id: str) -> bool:
        with self._session() as session:
            return session.get(RunRow, run_id) is not None

    def run_response(self, run_id: str) -> str:
        with self._session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise NotFoundError(f"no run {run_id!r}")
            return row.response_json

    def ledger_entries(
        self, *, ticket_id: str | None = None, run_id: str | None = None
    ) -> list[Any]:
        schema = get_schema()
        with self._session() as session:
            query = select(LedgerRow).order_by(LedgerRow.id)
            if ticket_id is not None:
                query = query.where(LedgerRow.ticket_id == ticket_id)
            if run_id is not None:
                query = query.where(LedgerRow.run_id == run_id)
            rows = session.scalars(query).all()
            return [
                schema.LedgerEntry(
                    run_id=row.run_id,
                    ticket_id=row.ticket_id,
                    action=row.action,
                    amount_usd=(
                        float(row.amount_usd) if row.amount_usd is not None else None
                    ),
                    status=row.status,
                    recorded_at=row.recorded_at,
                )
                for row in rows
            ]

    def ledger_refunds(self) -> list[LedgerRefund]:
        """REFUND rows that still count for the 2.5 deduction."""
        with self._session() as session:
            rows = session.scalars(
                select(LedgerRow).where(
                    LedgerRow.action == "REFUND",
                    LedgerRow.status != "REJECTED",
                )
            ).all()
            refunds: list[LedgerRefund] = []
            for row in rows:
                if not row.order_id:
                    continue
                allocations = _allocations(row.detail_json)
                if allocations:
                    for line_no, amount in allocations:
                        refunds.append(
                            LedgerRefund(
                                run_id=row.run_id,
                                ticket_id=row.ticket_id,
                                order_id=row.order_id,
                                line_no=line_no,
                                amount=amount,
                                status=row.status,
                            )
                        )
                elif row.line_no is not None and row.amount_usd is not None:
                    refunds.append(
                        LedgerRefund(
                            run_id=row.run_id,
                            ticket_id=row.ticket_id,
                            order_id=row.order_id,
                            line_no=row.line_no,
                            amount=Decimal(row.amount_usd),
                            status=row.status,
                        )
                    )
            return refunds

    # -- writes ---------------------------------------------------------

    def save_run(
        self,
        *,
        run_id: str,
        ticket_id: str,
        response_json: str,
        ledger: Sequence[dict[str, Any]],
        recorded_at: str | None = None,
    ) -> bool:
        """Insert the run and its ledger rows in one transaction.

        Returns ``False`` when the ticket already has a run (the unique
        constraint wins the race); nothing is written in that case.
        """
        now = recorded_at or utc_now()
        try:
            with self._session.begin() as session:
                session.add(
                    RunRow(
                        run_id=run_id,
                        ticket_id=ticket_id,
                        response_json=response_json,
                        created_at=now,
                    )
                )
                session.flush()  # unique(ticket_id) fails here on a race
                for spec in ledger:
                    session.add(
                        LedgerRow(
                            run_id=run_id,
                            ticket_id=ticket_id,
                            action=spec["action"],
                            amount_usd=spec.get("amount"),
                            status=spec["status"],
                            recorded_at=now,
                            order_id=spec.get("order_id"),
                            line_no=spec.get("line_no"),
                            detail_json=spec.get("detail_json"),
                        )
                    )
        except IntegrityError:
            return False
        return True

    def apply_decision(
        self, run_id: str, decision: str, approver: str
    ) -> str:
        """Approve or reject a PEND run's single REFUND row.

        Returns the ticket id.  Repeating the same decision keeps the row and
        the stored run unchanged but logs the call; a different decision after
        one has been recorded raises :class:`ConflictError`.
        """
        target = "APPROVED" if decision == "approve" else "REJECTED"
        with self._session.begin() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id!r}")
            response = json.loads(run.response_json)
            if response.get("outcome") != "PEND":
                raise ConflictError(
                    f"run {run_id} is {response.get('outcome')}, not PEND"
                )
            refunds = session.scalars(
                select(LedgerRow).where(
                    LedgerRow.run_id == run_id,
                    LedgerRow.action == "REFUND",
                )
            ).all()
            if len(refunds) != 1:
                raise ConflictError(
                    f"run {run_id} has {len(refunds)} REFUND rows, expected 1"
                )
            row = refunds[0]
            if row.status in {"APPROVED", "REJECTED"} and row.status != target:
                raise ConflictError(
                    f"run {run_id} is already {row.status}"
                )
            if row.status == "PROPOSED":
                row.status = target
            session.add(
                DecisionRow(
                    run_id=run_id,
                    decision=decision,
                    approver=approver,
                    recorded_at=utc_now(),
                )
            )
            return run.ticket_id


def _allocations(raw: str | None) -> list[tuple[int, Decimal]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:  # pragma: no cover - corrupt detail
        return []
    result: list[tuple[int, Decimal]] = []
    for item in data:
        try:
            result.append((int(item["line_no"]), Decimal(str(item["amount"]))))
        except Exception:  # pragma: no cover - corrupt detail
            continue
    return result
