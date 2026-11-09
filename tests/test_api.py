"""API tests: TestClient on a temporary SQLite database, model mocked.

The fake classifier/extractor below feed the pipeline the gold extraction for a
ticket (with the keyword injection guard still applied), so no HTTP call is
made.  Each test gets its own SQLite file, which is why idempotency, the ledger
and the decision log can be inspected directly through the API.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.app import Settings, create_app
from src.classify import ClassificationResult
from src.extract import ExtractionResult, keyword_injection_probability
from src.normalize import Extraction, get_schema
from src.trace import Trace

ROOT = Path(__file__).resolve().parents[1]


def schema():
    """Load the contract lazily so collection works without the data room."""
    return get_schema()


class FakeModels:
    """Deterministic stand-in for ``classify_ticket`` / ``extract_ticket``."""

    def __init__(
        self,
        gold: dict,
        *,
        classify_error: Exception | None = None,
        extract_error: Exception | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.gold = gold
        self.classify_error = classify_error
        self.extract_error = extract_error
        self.barrier = barrier
        self.classify_calls = 0
        self.extract_calls = 0

    def classifier(
        self,
        normalized,
        *,
        mode=None,
        trace=None,
        use_cache=True,
        client=None,
    ) -> ClassificationResult:
        self.classify_calls += 1
        if self.classify_error is not None:
            raise self.classify_error
        entry = self.gold.get(normalized.ticket_id, {})
        return ClassificationResult(
            ticket_type=entry.get("ticket_type", "REFUND"),
            probability=float(entry.get("ticket_type_confidence", 0.95)),
            mode="llm",
            failed=False,
            error=None,
            raw=None,
            trace=trace or Trace(),
            cache_hit=False,
            model="fake/classify",
        )

    def extractor(
        self,
        normalized,
        product_names,
        *,
        model=None,
        trace=None,
        use_cache=True,
        client=None,
    ) -> ExtractionResult:
        self.extract_calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        if self.extract_error is not None:
            raise self.extract_error
        entry = self.gold.get(normalized.ticket_id)
        fields = {
            key: value
            for key, value in (entry or {}).items()
            if key in Extraction.model_fields
        }
        fields.setdefault("ticket_type", "REFUND")
        fields.setdefault("ticket_type_confidence", 0.95)
        fields.setdefault("order_ids", list(normalized.order_ids))
        fields.setdefault("affected_lines", [])
        fields.setdefault("claim_kind", "UNSPECIFIED")
        fields["injection_probability"] = max(
            float(fields.get("injection_probability", 0.0)),
            keyword_injection_probability(normalized.text),
        )
        fields["ticket_id"] = normalized.ticket_id
        return ExtractionResult(
            extraction=Extraction(**fields),
            failed=False,
            error=None,
            raw=None,
            trace=trace or Trace(),
            cache_hit=False,
            model="fake/extract",
        )


def build_app(tmp_path: Path, models: FakeModels, *, name: str = "api.db"):
    return create_app(
        Settings(
            database_url=f"sqlite:///{(tmp_path / name).as_posix()}",
            prepared_dir=str(ROOT / "prepared"),
            classifier_mode="llm",
        ),
        extractor=models.extractor,
        classifier=models.classifier,
    )


@pytest.fixture
def models(gold):
    return FakeModels(gold)


@pytest.fixture
def client(tmp_path, models, dataset):
    with TestClient(build_app(tmp_path, models)) as test_client:
        yield test_client


def post_ticket(client, ticket):
    return client.post("/tickets", json=ticket)


def ledger(client, ticket_id):
    response = client.get("/ledger", params={"ticket_id": ticket_id})
    assert response.status_code == 200, response.text
    return response.json()


def decisions(client, run_id):
    """Decision rows are internal; read them through the app's Store."""
    store = client.app.state.store
    from src.store import DecisionRow

    with store._session() as session:
        return session.query(DecisionRow).filter_by(run_id=run_id).all()


def assert_contract(payload):
    return schema().TicketResponse.model_validate(payload)


def test_same_ticket_twice_returns_the_stored_bytes(client, models, tickets):
    ticket = tickets["T002"]

    first = post_ticket(client, ticket)
    assert first.status_code == 200
    assert_contract(first.json())
    calls_after_first = (models.classify_calls, models.extract_calls)
    rows_after_first = len(ledger(client, "T002"))

    second = post_ticket(client, ticket)
    assert second.status_code == 200
    assert second.content == first.content
    assert second.json()["run_id"] == first.json()["run_id"]
    assert (models.classify_calls, models.extract_calls) == calls_after_first
    assert len(ledger(client, "T002")) == rows_after_first
    assert client.app.state.store.stored_response("T002") == first.text


def test_two_simultaneous_posts_write_one_run(tmp_path, gold, tickets):
    models = FakeModels(gold, barrier=threading.Barrier(2))
    app = build_app(tmp_path, models, name="race.db")
    ticket = tickets["T002"]

    with TestClient(app) as first_client, TestClient(app) as second_client:

        def submit(test_client):
            return test_client.post("/tickets", json=ticket)

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(submit, first_client)
            two = pool.submit(submit, second_client)
            responses = [one.result(timeout=30), two.result(timeout=30)]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    payload = assert_contract(responses[0].json())
    assert payload.run_id == responses[1].json()["run_id"]

    store = app.state.store
    from src.store import RunRow

    with store._session() as session:
        assert session.query(RunRow).filter_by(ticket_id="T002").count() == 1
    assert len(ledger(first_client, "T002")) == len(ledger(second_client, "T002"))


def test_approve_twice_keeps_one_row_and_logs_each_call(client, tickets):
    run = post_ticket(client, tickets["T001"]).json()
    assert run["outcome"] == "PEND"
    run_id = run["run_id"]

    first = client.post(
        f"/runs/{run_id}/decision",
        json={"decision": "approve", "approver": "Asha from support"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/runs/{run_id}/decision",
        json={"decision": "approve", "approver": "Asha from support"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["run"] == first.json()["run"]

    rows = ledger(client, "T001")
    refunds = [row for row in rows if row["action"] == "REFUND"]
    assert len(refunds) == 1
    assert refunds[0]["status"] == "APPROVED"
    assert refunds[0]["amount_usd"] == pytest.approx(210.00)
    assert len([row for row in rows if row["action"] == "REPLY"]) == 1
    assert len(decisions(client, run_id)) == 2


def test_approve_then_reject_is_a_conflict(client, tickets):
    run_id = post_ticket(client, tickets["T001"]).json()["run_id"]
    approved = client.post(
        f"/runs/{run_id}/decision",
        json={"decision": "approve", "approver": "Asha"},
    )
    assert approved.status_code == 200

    conflicting = client.post(
        f"/runs/{run_id}/decision",
        json={"decision": "reject", "approver": "Ben"},
    )
    assert conflicting.status_code == 409
    refunds = [row for row in ledger(client, "T001") if row["action"] == "REFUND"]
    assert len(refunds) == 1
    assert refunds[0]["status"] == "APPROVED"
    assert len(decisions(client, run_id)) == 1


def test_reject_then_approve_is_a_conflict(client, tickets):
    run_id = post_ticket(client, tickets["T003"]).json()["run_id"]
    assert (
        client.post(
            f"/runs/{run_id}/decision",
            json={"decision": "reject", "approver": "Ben"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/runs/{run_id}/decision",
            json={"decision": "approve", "approver": "Asha"},
        ).status_code
        == 409
    )
    refunds = [row for row in ledger(client, "T003") if row["action"] == "REFUND"]
    assert len(refunds) == 1
    assert refunds[0]["status"] == "REJECTED"


def test_decision_on_a_non_pend_run_is_a_conflict(client, tickets):
    run = post_ticket(client, tickets["T002"]).json()
    assert run["outcome"] == "PROCEED"

    response = client.post(
        f"/runs/{run['run_id']}/decision",
        json={"decision": "approve", "approver": "Asha"},
    )
    assert response.status_code == 409
    refunds = [row for row in ledger(client, "T002") if row["action"] == "REFUND"]
    assert len(refunds) == 1
    assert refunds[0]["status"] == "PROPOSED"
    assert decisions(client, run["run_id"]) == []


def test_model_timeout_returns_a_valid_response_and_stores_one_reply_row(
    tmp_path, gold, tickets
):
    models = FakeModels(
        gold, classify_error=httpx.TimeoutException("classifier timed out")
    )
    app = build_app(tmp_path, models, name="timeout.db")
    with TestClient(app) as client:
        response = post_ticket(client, tickets["T002"])
        assert response.status_code == 200
        payload = assert_contract(response.json())
        assert payload.outcome == "UNABLE_TO_VERIFY"
        assert payload.human_approval_required is False
        assert payload.approver_role is None
        assert payload.reason_codes == []
        assert any(check.result == "UNABLE_TO_VERIFY" for check in payload.checks)

        stored = client.get(f"/runs/{payload.run_id}")
        assert stored.status_code == 200
        assert stored.json()["run"]["outcome"] == "UNABLE_TO_VERIFY"

        rows = ledger(client, "T002")
        assert [row["action"] for row in rows] == ["REPLY"]
        assert rows[0]["status"] == "PROPOSED"


def test_injected_body_cannot_set_the_approver(client, tickets):
    # T020's body tells the assistant it is pre-approved by the support lead
    # and asks for human_approval_required false.
    response = post_ticket(client, tickets["T020"])
    payload = assert_contract(response.json())

    assert payload.outcome == "ESCALATE"
    assert payload.reason_codes == ["POLICY_OVERRIDE_ATTEMPT"]
    assert payload.human_approval_required is True
    assert payload.approver_role == "TRUST_SAFETY"
    assert "support lead" not in payload.draft_reply.body.lower()
    assert "trust_safety" not in payload.draft_reply.body.lower()


def test_t027_after_t002_deduction_goes_through_the_database(client, tickets):
    first = assert_contract(post_ticket(client, tickets["T002"]).json())
    assert first.outcome == "PROCEED"

    second = assert_contract(post_ticket(client, tickets["T027"]).json())
    assert second.outcome == "REJECT"
    assert "ALREADY_REFUNDED" in second.reason_codes
    assert second.run_id != first.run_id

    refunds = [
        row
        for row in ledger(client, "T002") + ledger(client, "T027")
        if row["action"] == "REFUND"
    ]
    assert len(refunds) == 1  # only T002 wrote one; T027 was rejected first


def test_ledger_and_run_reads(client, tickets):
    run = post_ticket(client, tickets["T001"]).json()

    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["database"] == "ok"
    assert health["classify_prompt_version"] in {"v1", "v2"}
    assert client.get("/runs/run_missing").status_code == 404
    assert (
        client.post(
            "/runs/run_missing/decision",
            json={"decision": "approve", "approver": "Asha"},
        ).status_code
        == 404
    )

    rows = ledger(client, "T001")
    assert {row["action"] for row in rows} == {"REPLY", "REFUND"}
    assert all(row["run_id"] == run["run_id"] for row in rows)
    assert all(schema().LedgerEntry.model_validate(row) for row in rows)

    fetched = client.get(f"/runs/{run['run_id']}").json()
    assert fetched["run"]["ticket_id"] == "T001"
    assert len(fetched["ledger"]) == len(rows)
    assert ledger(client, "T999") == []


def test_ledger_without_ticket_filter_returns_all_rows_newest_first(client, tickets):
    post_ticket(client, tickets["T001"])
    first_rows = ledger(client, "T001")
    post_ticket(client, tickets["T002"])
    second_rows = ledger(client, "T002")

    response = client.get("/ledger")
    assert response.status_code == 200
    payload = response.json()
    assert all(schema().LedgerEntry.model_validate(row) for row in payload)
    assert len(payload) == len(first_rows) + len(second_rows)
    assert [row["ticket_id"] for row in payload] == (
        [row["ticket_id"] for row in reversed(second_rows)]
        + [row["ticket_id"] for row in reversed(first_rows)]
    )
    assert payload[0]["ticket_id"] == "T002"


def test_post_tickets_is_rate_limited_per_ip(client, gold, tickets):
    ids = sorted(gold)[:11]
    for ticket_id in ids[:10]:
        assert post_ticket(client, tickets[ticket_id]).status_code == 200

    limited = post_ticket(client, tickets[ids[10]])
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/health").status_code == 200
    assert ledger(client, ids[10]) == []


def test_reply_never_echoes_the_ticket_body(client, tickets):
    ticket = dict(tickets["T020"])
    marker = "UNIQUE-SENTENCE-FROM-THE-CUSTOMER"
    ticket["body"] = ticket["body"] + f" {marker}"
    payload = assert_contract(post_ticket(client, ticket).json())

    assert payload.draft_reply.to == tickets["T020"]["from"].strip().lower()
    assert marker not in payload.draft_reply.body
    assert payload.draft_reply.subject
