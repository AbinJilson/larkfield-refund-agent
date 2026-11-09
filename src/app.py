"""FastAPI service for the Larkfield ticket desk.

``POST /tickets`` runs one ticket through classify -> extract -> resolve ->
checks -> decide, stores the run and returns the contract ``TicketResponse``.
The same ``ticket_id`` returns the stored JSON byte-for-byte (no model calls,
no recomputation, no new rows).  ``POST /runs/{run_id}/decision`` approves or
rejects the single REFUND row of a PEND run.  Any internal error still returns
a valid ``TicketResponse`` with ``UNABLE_TO_VERIFY``; a 500 never leaves a
half-written run.

Contract models are imported from ``data_room/schema/response_schema.py`` via
``src.normalize.get_schema``; none are redefined here.
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .classify import classify_ticket, prompt_version
from .decide import DecisionConfig, DecisionResult, process_extraction
from .extract import extract_ticket, safe_extraction
from .normalize import get_schema, normalize_ticket
from .reply import draft_reply, internal_error_reply
from .resolve import PreparedData
from .store import ConflictError, NotFoundError, Store
from .trace import Trace

LOGGER = logging.getLogger("larkfield.app")

DEFAULT_RATE_LIMIT_PER_MINUTE = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class Settings:
    """Runtime configuration; ``None`` means read the environment."""

    database_url: str | None = None
    prepared_dir: str | None = None
    classifier_mode: str | None = None
    rate_limit_per_minute: int | None = None


def _rate_limit_from_env() -> int:
    raw = os.getenv("RATE_LIMIT_PER_MINUTE", str(DEFAULT_RATE_LIMIT_PER_MINUTE))
    try:
        return max(0, int(raw.strip()))
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_PER_MINUTE


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def create_app(
    settings: Settings | None = None,
    *,
    extractor: Callable[..., Any] | None = None,
    classifier: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build the application.  Tests inject fake model functions here."""
    load_dotenv()
    resolved = settings or Settings()
    schema = get_schema()
    store = Store(resolved.database_url)
    data = PreparedData.load(
        resolved.prepared_dir or os.getenv("PREPARED_DIR", "prepared")
    )

    app = FastAPI(title="Larkfield ticket desk", version="1.0")
    app.state.store = store
    app.state.data = data
    app.state.extractor = extractor or extract_ticket
    app.state.classifier = classifier or classify_ticket
    app.state.classifier_mode = resolved.classifier_mode
    app.state.decision_config = DecisionConfig.from_env()

    # -- per-IP rate limit for the expensive POST /tickets -----------------
    rate_limit = (
        resolved.rate_limit_per_minute
        if resolved.rate_limit_per_minute is not None
        else _rate_limit_from_env()
    )
    ticket_hits: dict[str, deque[float]] = defaultdict(deque)
    ticket_hits_lock = threading.Lock()

    def _retry_after(client_ip: str) -> int | None:
        """Seconds to wait, or ``None`` when the call is allowed."""
        if rate_limit <= 0:
            return None
        now = time.monotonic()
        with ticket_hits_lock:
            bucket = ticket_hits[client_ip]
            while bucket and now - bucket[0] >= RATE_LIMIT_WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= rate_limit:
                return max(
                    1,
                    int(RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0])) + 1,
                )
            bucket.append(now)
        return None

    # -- pipeline -------------------------------------------------------

    def _combined(classification, extraction, normalized):
        if classification.failed or extraction.failed:
            return safe_extraction(normalized)
        return extraction.extraction.model_copy(
            update={
                "ticket_type": classification.ticket_type,
                "ticket_type_confidence": classification.probability,
            }
        )

    def _pipeline(ticket, trace: Trace):
        state = app.state
        normalized = normalize_ticket(ticket)
        trace.mark_tool("normalize")
        classification = state.classifier(
            normalized,
            mode=state.classifier_mode,
            trace=trace,
        )
        trace.mark_tool("classify")
        extraction = state.extractor(
            normalized,
            state.data.product_names,
            trace=trace,
        )
        trace.mark_tool("extract")
        combined = _combined(classification, extraction, normalized)
        decision = process_extraction(
            ticket,
            combined,
            state.data,
            ledger=state.store.ledger_refunds(),
            config=state.decision_config,
        )
        trace.mark_tool("resolve")
        trace.mark_tool("checks")
        trace.mark_tool("decide")
        return decision, normalized

    def _response(ticket, run_id: str, decision, normalized, trace):
        resolution = decision.resolution
        customer = resolution.customer
        return schema.TicketResponse(
            ticket_id=ticket.ticket_id,
            run_id=run_id,
            ticket_type=decision.ticket_type,
            resolved=schema.Resolved(
                order_id=resolution.order_id,
                customer_id=customer.customer_id if customer is not None else None,
            ),
            outcome=decision.outcome,
            reason_codes=list(decision.reason_codes),
            checks=decision.checks,
            human_approval_required=decision.human_approval_required,
            approver_role=decision.approver_role,
            draft_reply=draft_reply(decision, normalized),
            trace=trace.to_schema(),
        )

    def _ledger_specs(decision: DecisionResult) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = [
            {"action": "REPLY", "amount": None, "status": "PROPOSED"}
        ]
        action = decision.ledger_action
        if action.action == "REFUND" and action.amount is not None and action.amount > 0:
            allocations = [
                {"line_no": line.line_no, "amount": str(line.remaining)}
                for line in decision.resolution.lines
                if line.remaining is not None and line.remaining > 0
            ]
            specs.append(
                {
                    "action": "REFUND",
                    "amount": action.amount,
                    "status": "PROPOSED",
                    "order_id": decision.resolution.order_id,
                    "line_no": (
                        allocations[0]["line_no"] if len(allocations) == 1 else None
                    ),
                    "detail_json": (
                        json.dumps(allocations, separators=(",", ":"))
                        if allocations
                        else None
                    ),
                }
            )
        if action.action == "ESCALATION":
            specs.append(
                {"action": "ESCALATION", "amount": None, "status": "PROPOSED"}
            )
        return specs

    def _fallback(ticket, run_id: str, trace: Trace):
        try:
            normalized = normalize_ticket(ticket)
            to = normalized.sender
        except Exception:  # pragma: no cover - schema already validated
            to = str(getattr(ticket, "from_", "")).strip().lower()
        check = schema.Check(
            check_id="pipeline",
            result="UNABLE_TO_VERIFY",
            evidence=[
                schema.Evidence(
                    source="internal",
                    ref="pipeline",
                    value="the run could not be completed",
                )
            ],
            policy_clause=None,
        )
        return schema.TicketResponse(
            ticket_id=ticket.ticket_id,
            run_id=run_id,
            ticket_type="OTHER",
            resolved=schema.Resolved(order_id=None, customer_id=None),
            outcome="UNABLE_TO_VERIFY",
            reason_codes=[],
            checks=[check],
            human_approval_required=False,
            approver_role=None,
            draft_reply=internal_error_reply(to),
            trace=trace.to_schema(),
        )

    # -- routes ---------------------------------------------------------

    @app.post("/tickets", response_model=schema.TicketResponse)
    def submit_ticket(ticket: schema.Ticket, request: Request):
        state = request.app.state
        client_ip = request.client.host if request.client else "unknown"
        retry_after = _retry_after(client_ip)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        stored = state.store.stored_response(ticket.ticket_id)
        if stored is not None:
            return Response(content=stored, media_type="application/json")

        run_id = new_run_id()
        trace = Trace()
        try:
            decision, normalized = _pipeline(ticket, trace)
            response = _response(ticket, run_id, decision, normalized, trace)
            ledger_specs = _ledger_specs(decision)
        except Exception:
            LOGGER.exception("pipeline failed for ticket %s", ticket.ticket_id)
            try:
                response = _fallback(ticket, run_id, trace)
            except Exception:  # pragma: no cover - contract itself failed
                return JSONResponse(
                    status_code=200,
                    content=_bare_fallback(ticket.ticket_id, run_id),
                )
            # A run always has its REPLY row, even when the pipeline failed.
            ledger_specs = [
                {"action": "REPLY", "amount": None, "status": "PROPOSED"}
            ]

        payload = response.model_dump_json()
        try:
            saved = state.store.save_run(
                run_id=run_id,
                ticket_id=ticket.ticket_id,
                response_json=payload,
                ledger=ledger_specs,
            )
        except Exception:
            LOGGER.exception("could not store run for ticket %s", ticket.ticket_id)
            saved = False

        if not saved:
            stored = state.store.stored_response(ticket.ticket_id)
            if stored is not None:
                return Response(content=stored, media_type="application/json")
        return Response(content=payload, media_type="application/json")

    @app.post("/runs/{run_id}/decision")
    def decide_run(run_id: str, body: schema.Decision, request: Request):
        store: Store = request.app.state.store
        try:
            store.apply_decision(run_id, body.decision, body.approver)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="unknown run")
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _run_payload(store, run_id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        store: Store = request.app.state.store
        try:
            return _run_payload(store, run_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="unknown run")

    @app.get("/ledger", response_model=list[schema.LedgerEntry])
    def get_ledger(
        request: Request,
        ticket_id: str | None = Query(
            None, description="optional ticket id filter"
        ),
    ):
        entries = request.app.state.store.ledger_entries(ticket_id=ticket_id)
        if ticket_id is None:
            # ``ledger_entries`` orders by insertion id; reverse for newest first.
            entries = list(reversed(entries))
        return entries

    @app.get("/health")
    def health(request: Request):
        try:
            request.app.state.store.health()
        except Exception:
            LOGGER.exception("health check failed")
            return JSONResponse(
                status_code=503, content={"status": "error", "database": "error"}
            )
        return {
            "status": "ok",
            "database": "ok",
            "classify_prompt_version": prompt_version(),
        }

    return app


def _run_payload(store: Store, run_id: str) -> dict[str, Any]:
    schema = get_schema()
    raw = store.run_response(run_id)
    return {
        "run": schema.TicketResponse.model_validate_json(raw),
        "ledger": store.ledger_entries(run_id=run_id),
    }


def _bare_fallback(ticket_id: str, run_id: str) -> dict[str, Any]:
    """Last-resort contract-shaped body if the schema itself cannot load."""
    return {
        "ticket_id": ticket_id,
        "run_id": run_id,
        "ticket_type": "OTHER",
        "resolved": {"order_id": None, "customer_id": None},
        "outcome": "UNABLE_TO_VERIFY",
        "reason_codes": [],
        "checks": [
            {
                "check_id": "pipeline",
                "result": "UNABLE_TO_VERIFY",
                "evidence": [
                    {
                        "source": "internal",
                        "ref": "pipeline",
                        "value": "the run could not be completed",
                    }
                ],
                "policy_clause": None,
            }
        ],
        "human_approval_required": False,
        "approver_role": None,
        "draft_reply": {
            "to": "",
            "subject": "Update on your Larkfield request",
            "body": (
                "Thank you for getting in touch. We could not complete the "
                "checks on your request just now. It has been kept for our "
                "team to look at."
            ),
        },
        "trace": {
            "model_calls": [],
            "tools_called": [],
            "documents_used": [
                {
                    "document": "policies/refund_policy_RP-3.2.md",
                    "version": "3.2",
                }
            ],
            "cost_usd": 0.0,
        },
    }
