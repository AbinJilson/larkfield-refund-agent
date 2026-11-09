"""Shared fixtures for the deterministic pipeline tests.

No model calls, no HTTP, no database.  The data room is read-only; only
``tests/output/`` is written.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _path_from_env(name: str, default: Path) -> Path:
    """Honour an override (e.g. an empty folder) and keep it absolute."""
    value = os.environ.get(name)
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


DATA_ROOM = _path_from_env("DATA_ROOM_DIR", ROOT / "data_room")
PREPARED = _path_from_env("PREPARED_DIR", ROOT / "prepared")

# Deterministic test configuration.  These are read at call time.
os.environ["DATA_ROOM_DIR"] = str(DATA_ROOM)
os.environ["PREPARED_DIR"] = str(PREPARED)
os.environ["TICKET_TYPE_MIN_PROB"] = "0.6"
os.environ["INJECTION_MIN_PROB"] = "0.5"

DATASET_MISSING_MESSAGE = (
    "dataset not present; place the course data room in data_room/ and run "
    "python -m src.prepare"
)

DATASET_PRESENT = (
    (DATA_ROOM / "tickets" / "support_inbox_export.json").is_file()
    and (DATA_ROOM / "inbox" / "public_tickets.json").is_file()
    and (DATA_ROOM / "schema" / "response_schema.py").is_file()
    and (PREPARED / "prepare_report.json").is_file()
)


@pytest.fixture(scope="session")
def dataset() -> bool:
    """Skip data-dependent tests when the read-only data room is absent."""
    if not DATASET_PRESENT:
        pytest.skip(DATASET_MISSING_MESSAGE)
    return True

from src.decide import DecisionConfig, process_extraction, process_ticket  # noqa: E402
from src.normalize import Extraction, extract, normalize_ticket  # noqa: E402
from src.resolve import LedgerRefund, PreparedData  # noqa: E402


@pytest.fixture(scope="session")
def data(dataset) -> PreparedData:
    return PreparedData.load(PREPARED)


@pytest.fixture(scope="session")
def tickets(dataset) -> dict:
    support = json.loads(
        (DATA_ROOM / "tickets" / "support_inbox_export.json").read_text(
            encoding="utf-8"
        )
    )
    public = json.loads(
        (DATA_ROOM / "inbox" / "public_tickets.json").read_text(
            encoding="utf-8"
        )
    )
    return {ticket["ticket_id"]: ticket for ticket in [*support, *public]}


@pytest.fixture(scope="session")
def labels(dataset) -> dict:
    rows = json.loads(
        (DATA_ROOM / "tickets" / "eval_seed_labelled.json").read_text(
            encoding="utf-8"
        )
    )
    return {row["ticket_id"]: row for row in rows}


@pytest.fixture(scope="session")
def gold() -> dict:
    return json.loads(
        (ROOT / "tests" / "fixtures" / "gold_extractions.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def config() -> DecisionConfig:
    return DecisionConfig(
        ticket_type_min_prob=DecisionConfig().ticket_type_min_prob,
        injection_min_prob=DecisionConfig().injection_min_prob,
    )


@pytest.fixture
def run(data, config):
    def _run(ticket, ledger=()):
        return process_ticket(ticket, data, ledger=ledger, config=config)

    return _run


@pytest.fixture
def run_extraction(data, config):
    def _run(ticket, extraction, ledger=()):
        return process_extraction(
            ticket, extraction, data, ledger=ledger, config=config
        )

    return _run


@pytest.fixture
def make_ticket():
    def _make(
        ticket_id: str,
        body: str,
        sender: str,
        received_at: str,
        *,
        subject: str = "Test ticket",
        image: bool = False,
    ) -> dict:
        return {
            "ticket_id": ticket_id,
            "received_at": received_at,
            "channel": "email",
            "from": sender,
            "to": "support@larkfield.example",
            "subject": subject,
            "body": body,
            "attachments": (
                [
                    {
                        "file_name": "photo.jpg",
                        "content_type": "image/jpeg",
                        "content_base64": "",
                    }
                ]
                if image
                else []
            ),
        }

    return _make


@pytest.fixture
def extraction_from_gold():
    def _build(entry: dict, ticket_id: str = "GOLD") -> Extraction:
        fields = {key: value for key, value in entry.items() if key != "why"}
        fields.setdefault("ticket_id", ticket_id)
        return Extraction(**fields)

    return _build


@pytest.fixture
def ledger_refund():
    return LedgerRefund


@pytest.fixture(scope="session")
def check_by_id():
    def _get(result, check_id: str):
        matches = [check for check in result.checks if check.check_id == check_id]
        assert matches, f"no check {check_id!r} in {[c.check_id for c in result.checks]}"
        return matches[0]

    return _get


@pytest.fixture
def deterministic_extraction(data):
    def _extract(ticket) -> Extraction:
        return extract(normalize_ticket(ticket), data.product_names)

    return _extract
