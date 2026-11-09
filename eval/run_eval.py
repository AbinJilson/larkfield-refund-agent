"""End-to-end eval against the running ticket service.

Posts all 40 data-room tickets in ``received_at`` order to ``EVAL_BASE_URL``,
validates every response against the contract, compares the 15 human labels and
the 40-row gold decisions table, then checks idempotency and the PEND approval
flow.  The service must be started against a fresh database (the script cannot
reset one remotely); a temporary SQLite file is the intended setup.

Writes ``eval/results/run_<timestamp>.json`` and ``eval/results/summary.md``.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.normalize import data_room_dir, get_schema  # noqa: E402

RESULTS_DIR = ROOT / "eval" / "results"
NEGATIVE_DIR = ROOT / "eval" / "cases" / "negative"
GOLD_TABLE = ROOT / "tests" / "output" / "decisions.md"
EXPECTED_DB_DIFFERENCES = {
    # Re-sends whose earlier REFUND row is now in the database (ruling 1).
    "T027",
    "T028",
}
PENDING_APPROVER = "eval-harness"


def base_url() -> str:
    return os.getenv("EVAL_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def load_tickets() -> list[dict[str, Any]]:
    support = json.loads(
        (data_room_dir() / "tickets" / "support_inbox_export.json").read_text(
            encoding="utf-8"
        )
    )
    public = json.loads(
        (data_room_dir() / "inbox" / "public_tickets.json").read_text(
            encoding="utf-8"
        )
    )
    tickets = [*support, *public]
    tickets.sort(key=lambda row: (row["received_at"], row["ticket_id"]))
    return tickets


def load_negative_cases() -> list[dict[str, Any]]:
    """Hand-written negative tickets with their expected decision."""
    cases: list[dict[str, Any]] = []
    for path in sorted(NEGATIVE_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        cases.append(case)
    return cases


def load_labels() -> dict[str, dict[str, Any]]:
    rows = json.loads(
        (data_room_dir() / "tickets" / "eval_seed_labelled.json").read_text(
            encoding="utf-8"
        )
    )
    return {row["ticket_id"]: row for row in rows}


def _money(value: Any) -> Decimal | None:
    if value is None or value == "" or str(value).strip() in {"—", "-"}:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def load_gold_table() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in GOLD_TABLE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0] or cells[0] == "Ticket":
            continue
        ticket_id, _rule, outcome, amount, reason_codes = cells
        if not ticket_id[0] in "TP" or not ticket_id[1:].isdigit():
            continue
        reasons = (
            []
            if reason_codes in {"—", "-", ""}
            else [part.strip() for part in reason_codes.split(",") if part.strip()]
        )
        rows[ticket_id] = {
            "outcome": outcome,
            "amount": _money(amount),
            "reason_codes": reasons,
        }
    return rows


def pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def ledger_amount(entries: list[dict[str, Any]]) -> Decimal | None:
    amounts = [
        _money(row.get("amount_usd"))
        for row in entries
        if row.get("action") == "REFUND"
    ]
    amounts = [amount for amount in amounts if amount is not None]
    if not amounts:
        return None
    return sum(amounts, Decimal("0.00"))


def ledger_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in entries:
        result[row["action"]] = result.get(row["action"], 0) + 1
    return result


def fallback_count(run: dict[str, Any]) -> int:
    """Rule-13 fallbacks: ESCALATE / SUPPORT_LEAD with no policy reason code."""
    return sum(
        1
        for row in run.get("tickets", [])
        if row.get("outcome") == "ESCALATE"
        and row.get("approver_role") == "SUPPORT_LEAD"
        and not row.get("reason_codes")
    )


def fetch_ledger(client: httpx.Client, ticket_id: str) -> list[dict[str, Any]]:
    response = client.get("/ledger", params={"ticket_id": ticket_id})
    response.raise_for_status()
    rows = response.json()
    schema = get_schema()
    for row in rows:
        schema.LedgerEntry.model_validate(row)
    return rows


def main() -> int:
    schema = get_schema()
    url = base_url()
    tickets = load_tickets()
    labels = load_labels()
    gold_table = load_gold_table()
    negative_cases = load_negative_cases()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    model_mode = os.getenv("CLASSIFIER", "server default")

    report: dict[str, Any] = {
        "base_url": url,
        "started_at": started_at.isoformat(),
        "classifier_mode_of_eval_process": model_mode,
        "classify_prompt_version": (
            os.getenv("CLASSIFY_PROMPT_VERSION", "v2").strip().lower() or "v2"
        ),
        "ticket_count": len(tickets),
        "tickets": [],
        "labels": {},
        "gold": {},
        "negative": {},
        "idempotency": {},
        "approvals": {},
        "totals": {},
    }

    records: dict[str, dict[str, Any]] = {}
    first_content: dict[str, bytes] = {}
    first_ledger: dict[str, list[dict[str, Any]]] = {}
    latencies_ms: list[float] = []
    total_cost = Decimal("0")
    costs: list[Decimal] = []
    input_tokens = 0
    output_tokens = 0

    timeout = float(os.getenv("EVAL_TIMEOUT_SECONDS", "120"))
    with httpx.Client(base_url=url, timeout=timeout) as client:
        health = client.get("/health")
        health.raise_for_status()
        health_payload = health.json()
        if health_payload.get("status") != "ok":
            raise SystemExit(f"service is not healthy: {health.text}")
        report["classify_prompt_version"] = (
            health_payload.get("classify_prompt_version")
            or report["classify_prompt_version"]
        )

        # -- pass 1: contract, labels, gold ---------------------------
        for ticket in tickets:
            schema.Ticket.model_validate(ticket)
            started = time.perf_counter()
            response = client.post("/tickets", json=ticket)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)
            response.raise_for_status()
            payload = schema.TicketResponse.model_validate_json(response.content)
            ledger_rows = fetch_ledger(client, ticket["ticket_id"])

            records[ticket["ticket_id"]] = {
                "response": payload.model_dump(mode="json"),
                "ledger": [row for row in ledger_rows],
            }
            first_content[ticket["ticket_id"]] = response.content
            first_ledger[ticket["ticket_id"]] = ledger_rows
            total_cost += Decimal(str(payload.trace.cost_usd))
            costs.append(Decimal(str(payload.trace.cost_usd)))
            for call in payload.trace.model_calls:
                input_tokens += call.input_tokens
                output_tokens += call.output_tokens

            report["tickets"].append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "received_at": ticket["received_at"],
                    "run_id": payload.run_id,
                    "ticket_type": payload.ticket_type,
                    "outcome": payload.outcome,
                    "approver_role": payload.approver_role,
                    "human_approval_required": payload.human_approval_required,
                    "reason_codes": payload.reason_codes,
                    "amount_usd": (
                        float(amount)
                        if (amount := ledger_amount(ledger_rows)) is not None
                        else None
                    ),
                    "ledger_actions": ledger_counts(ledger_rows),
                    "latency_ms": round(elapsed_ms, 2),
                    "cost_usd": payload.trace.cost_usd,
                }
            )

        # -- label comparison (15 human labels) ----------------------
        label_mismatches: list[dict[str, Any]] = []
        label_correct = 0
        for ticket_id, expected in labels.items():
            actual = records[ticket_id]
            response = actual["response"]
            amount = ledger_amount(actual["ledger"])
            problems: list[str] = []

            if response["outcome"] != expected["expected_outcome"]:
                problems.append(
                    f"outcome {response['outcome']} != {expected['expected_outcome']}"
                )
            if response["approver_role"] != expected["expected_approver_role"]:
                problems.append(
                    "approver_role "
                    f"{response['approver_role']} != {expected['expected_approver_role']}"
                )
            if response["human_approval_required"] != bool(
                expected["expected_human_approval_required"]
            ):
                problems.append(
                    "human_approval_required "
                    f"{response['human_approval_required']} != "
                    f"{expected['expected_human_approval_required']}"
                )
            if "expected_amount_usd" in expected:
                expected_amount = _money(expected["expected_amount_usd"])
                if amount != expected_amount:
                    problems.append(f"amount {amount} != {expected_amount}")
            if "expected_reason_codes" in expected:
                expected_reasons = set(expected["expected_reason_codes"])
                if set(response["reason_codes"]) != expected_reasons:
                    problems.append(
                        f"reason_codes {sorted(response['reason_codes'])} != "
                        f"{sorted(expected_reasons)}"
                    )

            if problems:
                label_mismatches.append(
                    {"ticket_id": ticket_id, "problems": problems}
                )
            else:
                label_correct += 1

        report["labels"] = {
            "correct": label_correct,
            "total": len(labels),
            "accuracy": round(label_correct / len(labels), 4),
            "mismatches": label_mismatches,
        }

        # -- gold decisions table (40 tickets) -----------------------
        gold_mismatches: list[dict[str, Any]] = []
        expected_differences: list[dict[str, Any]] = []
        gold_correct = 0
        for ticket_id, expected in gold_table.items():
            actual = records[ticket_id]
            response = actual["response"]
            amount = ledger_amount(actual["ledger"])
            problems: list[str] = []

            if response["outcome"] != expected["outcome"]:
                problems.append(
                    f"outcome {response['outcome']} != {expected['outcome']}"
                )
            if amount != expected["amount"]:
                problems.append(f"amount {amount} != {expected['amount']}")
            if set(response["reason_codes"]) != set(expected["reason_codes"]):
                problems.append(
                    f"reason_codes {sorted(response['reason_codes'])} != "
                    f"{sorted(expected['reason_codes'])}"
                )

            if not problems:
                gold_correct += 1
                continue

            item = {"ticket_id": ticket_id, "problems": problems}
            expected_here = (
                ticket_id in EXPECTED_DB_DIFFERENCES
                and response["outcome"] == "REJECT"
                and "ALREADY_REFUNDED" in response["reason_codes"]
            )
            if expected_here:
                # Ruling 1: the earlier REFUND row is in the ledger, so the
                # re-send is correctly rejected.  Count it as a match and show
                # why it differs from the per-ticket gold table.
                item["classification"] = (
                    "expected (ruling 1, earlier REFUND in the ledger)"
                )
                expected_differences.append(item)
                gold_correct += 1
            else:
                gold_mismatches.append(item)

        report["gold"] = {
            "correct": gold_correct,
            "total": len(gold_table),
            "accuracy": round(gold_correct / len(gold_table), 4),
            "ledger_dependent": len(expected_differences),
            "ledger_dependent_tickets": [
                item["ticket_id"] for item in expected_differences
            ],
            "mismatches": gold_mismatches,
            "expected_database_differences": expected_differences,
        }

        # -- idempotency: post everything again ----------------------
        idempotent = True
        ledger_unchanged = True
        changed: list[str] = []
        for ticket in tickets:
            ticket_id = ticket["ticket_id"]
            response = client.post("/tickets", json=ticket)
            response.raise_for_status()
            if response.content != first_content[ticket_id]:
                idempotent = False
                changed.append(ticket_id)
            rows = fetch_ledger(client, ticket_id)
            if rows != first_ledger[ticket_id]:
                ledger_unchanged = False
                changed.append(ticket_id)
            schema.TicketResponse.model_validate_json(response.content)

        report["idempotency"] = {
            "responses_identical": idempotent,
            "ledger_unchanged": ledger_unchanged,
            "changed_tickets": sorted(set(changed)),
        }

        # -- approval flow for every PEND run ------------------------
        pending = [
            row for row in report["tickets"] if row["outcome"] == "PEND"
        ]
        approval_rows: list[dict[str, Any]] = []
        approval_ok = True
        for row in pending:
            run_id = row["run_id"]
            ticket_id = row["ticket_id"]
            before = fetch_ledger(client, ticket_id)
            proposed = [
                entry
                for entry in before
                if entry["action"] == "REFUND" and entry["status"] == "PROPOSED"
            ]
            problems: list[str] = []
            if len(proposed) != 1:
                problems.append(f"expected one PROPOSED REFUND, saw {len(proposed)}")

            first = client.post(
                f"/runs/{run_id}/decision",
                json={"decision": "approve", "approver": PENDING_APPROVER},
            )
            second = client.post(
                f"/runs/{run_id}/decision",
                json={"decision": "approve", "approver": PENDING_APPROVER},
            )
            if first.status_code != 200:
                problems.append(f"first approve returned {first.status_code}")
            if second.status_code != 200:
                problems.append(f"second approve returned {second.status_code}")

            if first.status_code == 200 and second.status_code == 200:
                first_payload = first.json()
                second_payload = second.json()
                if first_payload["run"] != second_payload["run"]:
                    problems.append("second approve changed the stored run")
                approved = [
                    entry
                    for entry in second_payload["ledger"]
                    if entry["action"] == "REFUND"
                ]
                if len(approved) != 1:
                    problems.append(
                        f"expected one REFUND row, saw {len(approved)}"
                    )
                elif approved[0]["status"] != "APPROVED":
                    problems.append(
                        f"REFUND row is {approved[0]['status']}, not APPROVED"
                    )

            conflict = client.post(
                f"/runs/{run_id}/decision",
                json={"decision": "reject", "approver": PENDING_APPROVER},
            )
            if conflict.status_code != 409:
                problems.append(
                    f"conflicting reject returned {conflict.status_code}, expected 409"
                )

            after = fetch_ledger(client, ticket_id)
            if len(after) != len(before):
                problems.append("decision changed the ledger row count")

            if problems:
                approval_ok = False
            approval_rows.append(
                {
                    "ticket_id": ticket_id,
                    "run_id": run_id,
                    "problems": problems,
                    "approved_refund": [
                        {
                            "status": entry["status"],
                            "amount_usd": entry["amount_usd"],
                        }
                        for entry in after
                        if entry["action"] == "REFUND"
                    ],
                    "conflicting_reject_status": conflict.status_code,
                }
            )

        report["approvals"] = {
            "pending_runs": [row["ticket_id"] for row in pending],
            "all_ok": approval_ok,
            "runs": approval_rows,
        }

        # -- negative cases (hand-written, eval/cases/negative) ------
        negative_rows: list[dict[str, Any]] = []
        negative_correct = 0
        for case in negative_cases:
            ticket = case["ticket"]
            expected = case["expected"]
            schema.Ticket.model_validate(ticket)
            started = time.perf_counter()
            response = client.post("/tickets", json=ticket)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            response.raise_for_status()
            payload = schema.TicketResponse.model_validate_json(response.content)
            ledger_rows = fetch_ledger(client, ticket["ticket_id"])
            amount = ledger_amount(ledger_rows)

            problems: list[str] = []
            if payload.outcome != expected["outcome"]:
                problems.append(
                    f"outcome {payload.outcome} != {expected['outcome']}"
                )
            if payload.approver_role != expected["approver_role"]:
                problems.append(
                    f"approver_role {payload.approver_role} != "
                    f"{expected['approver_role']}"
                )
            if set(payload.reason_codes) != set(expected["reason_codes"]):
                problems.append(
                    f"reason_codes {sorted(payload.reason_codes)} != "
                    f"{sorted(expected['reason_codes'])}"
                )
            if payload.human_approval_required != bool(
                expected.get("human_approval_required", False)
            ):
                problems.append(
                    "human_approval_required "
                    f"{payload.human_approval_required} != "
                    f"{expected.get('human_approval_required', False)}"
                )
            expected_amount = (
                Decimal(str(expected["amount_usd"]))
                if expected.get("amount_usd") is not None
                else None
            )
            if amount != expected_amount:
                problems.append(f"amount {amount} != {expected_amount}")
            if not problems:
                negative_correct += 1

            total_cost += Decimal(str(payload.trace.cost_usd))
            costs.append(Decimal(str(payload.trace.cost_usd)))
            for call in payload.trace.model_calls:
                input_tokens += call.input_tokens
                output_tokens += call.output_tokens

            negative_rows.append(
                {
                    "case_id": case.get("case_id", ticket["ticket_id"]),
                    "kind": case.get("kind"),
                    "ticket_id": ticket["ticket_id"],
                    "expected": {
                        "outcome": expected["outcome"],
                        "approver_role": expected["approver_role"],
                        "reason_codes": list(expected["reason_codes"]),
                        "amount_usd": expected.get("amount_usd"),
                        "human_approval_required": bool(
                            expected.get("human_approval_required", False)
                        ),
                    },
                    "actual": {
                        "outcome": payload.outcome,
                        "approver_role": payload.approver_role,
                        "reason_codes": list(payload.reason_codes),
                        "amount_usd": (
                            float(amount) if amount is not None else None
                        ),
                        "human_approval_required": payload.human_approval_required,
                    },
                    "latency_ms": round(elapsed_ms, 2),
                    "cost_usd": payload.trace.cost_usd,
                    "problems": problems,
                }
            )

        report["negative"] = {
            "correct": negative_correct,
            "total": len(negative_cases),
            "cases": negative_rows,
            "mismatches": [
                {"case_id": row["case_id"], "problems": row["problems"]}
                for row in negative_rows
                if row["problems"]
            ],
        }

    # -- totals ------------------------------------------------------
    total_ledger_rows = sum(len(rows) for rows in first_ledger.values())
    median_cost = (
        statistics.median(costs)
        if costs
        else Decimal("0")
    ).quantize(Decimal("0.00000001"))
    report["totals"] = {
        "label_accuracy": report["labels"]["accuracy"],
        "gold_accuracy": report["gold"]["accuracy"],
        "gold_correct": report["gold"]["correct"],
        "gold_total": report["gold"]["total"],
        "gold_ledger_dependent": report["gold"]["ledger_dependent"],
        "gold_mismatches_outside_expected": len(report["gold"]["mismatches"]),
        "expected_database_differences": len(
            report["gold"]["expected_database_differences"]
        ),
        "fallbacks": fallback_count(report),
        "negative_correct": report["negative"]["correct"],
        "negative_total": report["negative"]["total"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "list_cost_usd": float(total_cost),
        "median_cost_usd": float(median_cost),
        "ledger_rows": total_ledger_rows,
        "latency_ms": {
            "count": len(latencies_ms),
            "p50": round(percentile(latencies_ms, 0.50), 2),
            "p95": round(percentile(latencies_ms, 0.95), 2),
            "min": round(min(latencies_ms), 2) if latencies_ms else 0.0,
            "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
            "mean": round(statistics.fmean(latencies_ms), 2) if latencies_ms else 0.0,
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    run_path = RESULTS_DIR / f"run_{timestamp}.json"
    run_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path = RESULTS_DIR / "summary.md"
    summary_path.write_text(render_summary(), encoding="utf-8")

    print(json.dumps(report["totals"], indent=2))
    print(f"labels {report['labels']['correct']}/{report['labels']['total']}")
    print(f"gold {report['gold']['correct']}/{report['gold']['total']}")
    if report["labels"]["mismatches"]:
        print("label mismatches:", report["labels"]["mismatches"])
    if report["gold"]["mismatches"]:
        print("gold mismatches:", report["gold"]["mismatches"])
    print("wrote", run_path)
    print("wrote", summary_path)

    ok = (
        not report["labels"]["mismatches"]
        and not report["gold"]["mismatches"]
        and not report["negative"]["mismatches"]
        and report["idempotency"]["responses_identical"]
        and report["idempotency"]["ledger_unchanged"]
        and report["approvals"]["all_ok"]
    )
    return 0 if ok else 1


def load_runs() -> list[tuple[str, dict[str, Any]]]:
    """Every committed run file, oldest first, for the side-by-side summary."""
    runs: list[tuple[str, dict[str, Any]]] = []
    for path in RESULTS_DIR.glob("run_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            runs.append((path.name, data))
    runs.sort(key=lambda item: item[1].get("started_at", ""))
    return runs


def _fmt_money(value: Any) -> str:
    if value is None:
        return "—"
    return f"${float(value):.8f}"


def render_summary() -> str:
    runs = load_runs()
    lines = [
        "# Eval summary",
        "",
        "Side-by-side comparison of every committed `run_*.json` in "
        "`eval/results/`.",
        "Fallbacks are rule-13 `ESCALATE` / `SUPPORT_LEAD` results with no "
        "reason code.",
        "",
        "| Run | Prompt | Mode | Labels | Gold | Negative | Fallbacks | Cost (USD) | "
        "Median cost (USD) | p50 (ms) | p95 (ms) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, run in runs:
        totals = run.get("totals", {})
        labels = run.get("labels", {})
        gold = run.get("gold", {})
        negative = run.get("negative", {})
        latency = totals.get("latency_ms", {})
        lines.append(
            f"| `{name}` | {run.get('classify_prompt_version') or '—'} | "
            f"{run.get('classifier_mode_of_eval_process') or '—'} | "
            f"{labels.get('correct', '?')}/{labels.get('total', '?')} | "
            f"{gold.get('correct', '?')}/{gold.get('total', '?')} | "
            f"{negative.get('correct', '?')}/{negative.get('total', '?')} | "
            f"{fallback_count(run)} | "
            f"{_fmt_money(totals.get('list_cost_usd'))} | "
            f"{_fmt_money(totals.get('median_cost_usd'))} | "
            f"{latency.get('p50', '?')} | {latency.get('p95', '?')} |"
        )
    lines.append("")

    lines += [
        "## Negative cases",
        "",
        "Hand-written tickets in `eval/cases/negative/`, posted after the "
        "40-ticket sweep and checked on outcome, approver role, reason codes "
        "and ledger amount.",
        "",
        "| Run | Negative | Failing cases |",
        "|---|---:|---|",
    ]
    for name, run in runs:
        negative = run.get("negative", {})
        failing = ", ".join(
            item["case_id"] for item in negative.get("mismatches", [])
        )
        lines.append(
            f"| `{name}` | {negative.get('correct', '?')}/"
            f"{negative.get('total', '?')} | {failing or '—'} |"
        )
    lines.append("")

    for name, run in runs:
        labels = run.get("labels", {})
        gold = run.get("gold", {})
        totals = run.get("totals", {})
        negative = run.get("negative", {})
        lines += [
            f"## `{name}`",
            "",
            f"- Started: {run.get('started_at')}",
            f"- Base URL: {run.get('base_url')}",
            f"- Prompt version: {run.get('classify_prompt_version') or '—'}",
            f"- Tokens: {totals.get('input_tokens', '?')} input + "
            f"{totals.get('output_tokens', '?')} output",
            f"- Ledger rows written in the first pass: "
            f"{totals.get('ledger_rows', '?')}",
            f"- Negative cases: **{negative.get('correct', '?')}/"
            f"{negative.get('total', '?')}**",
            f"- Reposts byte-identical: "
            f"**{run.get('idempotency', {}).get('responses_identical')}**; "
            f"ledger unchanged: "
            f"**{run.get('idempotency', {}).get('ledger_unchanged')}**",
            f"- All approval flows passed: "
            f"**{run.get('approvals', {}).get('all_ok')}**",
        ]
        for item in labels.get("mismatches", []):
            lines.append(
                f"- Label mismatch `{item['ticket_id']}`: "
                + "; ".join(item["problems"])
            )
        for item in gold.get("mismatches", []):
            lines.append(
                f"- Gold mismatch `{item['ticket_id']}`: "
                + "; ".join(item["problems"])
            )
        for item in gold.get("expected_database_differences", []):
            lines.append(
                f"- Ledger-dependent `{item['ticket_id']}`: "
                f"{item.get('classification', 'expected')}; "
                + "; ".join(item["problems"])
            )
        for item in negative.get("mismatches", []):
            lines.append(
                f"- Negative mismatch `{item['case_id']}`: "
                + "; ".join(item["problems"])
            )
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
