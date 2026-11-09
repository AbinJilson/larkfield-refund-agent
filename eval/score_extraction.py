"""Score the model layer against the hand-written gold extractions.

Run from the repository root:

    python -m eval.score_extraction

It runs both ``CLASSIFIER`` modes over all 40 tickets, compares every field
with ``tests/fixtures/gold_extractions.json``, checks injection recall and
false positives, puts the model extractions through ``src.decide`` and
compares outcomes with ``tests/output/decisions.md`` and the 15 labels, then
writes ``eval/results/extraction_scores.md``.

Raw responses are cached under ``prepared/model_cache/``; a second run is
free unless ``MODEL_CACHE_DISABLED=1`` or ``--no-cache`` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.classify import classify_ticket  # noqa: E402
from src.decide import DecisionConfig, process_extraction  # noqa: E402
from src.extract import extract_ticket, safe_extraction  # noqa: E402
from src.normalize import Extraction, normalize_ticket  # noqa: E402
from src.resolve import PreparedData  # noqa: E402
from src.trace import Trace  # noqa: E402

DATA = ROOT / "data_room"
GOLD_PATH = ROOT / "tests" / "fixtures" / "gold_extractions.json"
DECISIONS_PATH = ROOT / "tests" / "output" / "decisions.md"
OUTPUT = ROOT / "eval" / "results" / "extraction_scores.md"

FIELDS = (
    "type",
    "order_ids",
    "affected_lines",
    "qty",
    "claim_kind",
    "photo_damage",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs() -> tuple[dict, dict, dict, dict, dict, PreparedData]:
    support = _load_json(DATA / "tickets" / "support_inbox_export.json")
    public = _load_json(DATA / "inbox" / "public_tickets.json")
    tickets = {t["ticket_id"]: t for t in [*support, *public]}
    gold = _load_json(GOLD_PATH)
    labels = {
        row["ticket_id"]: row
        for row in _load_json(DATA / "tickets" / "eval_seed_labelled.json")
    }
    expected = parse_decisions(DECISIONS_PATH)
    data = PreparedData.load(ROOT / "prepared")
    return tickets, gold, labels, expected, {}, data


def parse_decisions(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    outcomes: dict[str, str] = {}
    pattern = re.compile(
        r"^\|\s*([TP]\d+)\s*\|\s*\d+\s*\|\s*([A-Z_]+)\s*\|", re.MULTILINE
    )
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        outcomes[match.group(1)] = match.group(2)
    return outcomes


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _product(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text or None


def _gold_lines(entry: dict) -> list[dict]:
    return entry.get("affected_lines") or []


def _product_set(lines) -> set[str]:
    result = set()
    for line in lines:
        product = _product(getattr(line, "product", None) if not isinstance(line, dict) else line.get("product"))
        if product:
            result.add(product)
    return result


def _qty_map(lines) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for line in lines:
        if isinstance(line, dict):
            product = _product(line.get("product"))
            qty = line.get("qty")
        else:
            product = _product(line.product)
            qty = line.qty
        if product:
            result[product] = qty
    return result


def _ordered_qty_is_one(
    data: PreparedData, entry: dict, product: str | None
) -> bool:
    """True when every matching order line holds exactly one unit.

    ``resolve.py`` maps a null affected qty to 1 on such a line, so a model
    ``null`` is equivalent to the gold qty 1 here.  The prompt is unchanged.
    """
    if not product:
        return False
    order_ids = entry.get("order_ids") or []
    if len(order_ids) != 1:
        return False
    lines = data.lines_by_order.get(order_ids[0], ())
    exact = [
        line for line in lines if line.product_name.casefold() == product.casefold()
    ]
    matches = exact or [
        line
        for line in lines
        if product.casefold() in line.product_name.casefold()
        or line.product_name.casefold() in product.casefold()
    ]
    return bool(matches) and all(line.qty == 1 for line in matches)


def _qty_matches(
    actual: dict[str, int | None],
    expected: dict[str, int | None],
    data: PreparedData,
    entry: dict,
) -> bool:
    if set(actual) != set(expected):
        return False
    for product, expected_qty in expected.items():
        actual_qty = actual[product]
        if actual_qty == expected_qty:
            continue
        if (
            actual_qty is None
            and expected_qty == 1
            and _ordered_qty_is_one(data, entry, product)
        ):
            continue
        return False
    return True


def _damage(claim_kind: str) -> bool:
    return claim_kind in {"DAMAGED", "FAULTY"}


def score_fields(
    mode: str,
    tickets: dict,
    gold: dict,
    data: PreparedData,
    use_cache: bool,
) -> tuple[dict[str, list[str]], dict[str, dict], Trace, list[dict]]:
    """Return (misses per field, per-ticket records, trace, records)."""
    misses: dict[str, list[str]] = {field: [] for field in FIELDS}
    records: dict[str, dict] = {}
    trace = Trace()
    rows: list[dict] = []

    for ticket_id in sorted(gold):
        ticket = tickets[ticket_id]
        normalized = normalize_ticket(ticket)
        entry = gold[ticket_id]
        classification = classify_ticket(
            normalized, mode=mode, trace=trace, use_cache=use_cache
        )
        extraction = extract_ticket(
            normalized,
            data.product_names,
            trace=trace,
            use_cache=use_cache,
        )
        ext = extraction.extraction

        actual_type = classification.ticket_type
        if actual_type != entry["ticket_type"]:
            misses["type"].append(ticket_id)
        if set(ext.order_ids) != set(entry["order_ids"]):
            misses["order_ids"].append(ticket_id)
        if _product_set(ext.affected_lines) != _product_set(_gold_lines(entry)):
            misses["affected_lines"].append(ticket_id)
        if not _qty_matches(
            _qty_map(ext.affected_lines),
            _qty_map(_gold_lines(entry)),
            data,
            entry,
        ):
            misses["qty"].append(ticket_id)
        if ext.claim_kind != entry["claim_kind"]:
            misses["claim_kind"].append(ticket_id)
        expected_damage = _damage(entry["claim_kind"])
        if _damage(ext.claim_kind) != expected_damage:
            misses["photo_damage"].append(ticket_id)

        records[ticket_id] = {
            "classification": classification,
            "extraction": extraction,
            "normalized": normalized,
        }
        rows.append(
            {
                "ticket_id": ticket_id,
                "type": actual_type,
                "expected_type": entry["ticket_type"],
                "claim_kind": ext.claim_kind,
                "expected_claim_kind": entry["claim_kind"],
                "injection": ext.injection_probability,
            }
        )
    return misses, records, trace, rows


def combined_extraction(record: dict) -> Extraction:
    """Use the classifier for type and the extractor for everything else.

    If either model call failed, return the conservative fallback so the
    decision layer escalates rather than guessing a line or a type.
    """
    classification = record["classification"]
    extraction = record["extraction"]
    if extraction.failed or classification.failed:
        return safe_extraction(record["normalized"])
    return extraction.extraction.model_copy(
        update={
            "ticket_type": classification.ticket_type,
            "ticket_type_confidence": classification.probability,
        }
    )


def run_end_to_end(
    mode: str,
    tickets: dict,
    gold: dict,
    labels: dict,
    expected: dict[str, str],
    data: PreparedData,
    records: dict[str, dict],
) -> tuple[dict[str, str], list[str], list[str]]:
    config = DecisionConfig.from_env()
    outcomes: dict[str, str] = {}
    table_diffs: list[str] = []
    label_diffs: list[str] = []

    for ticket_id in sorted(gold):
        extraction = combined_extraction(records[ticket_id])
        result = process_extraction(
            tickets[ticket_id], extraction, data, config=config
        )
        outcomes[ticket_id] = result.outcome
        if ticket_id in expected and result.outcome != expected[ticket_id]:
            table_diffs.append(
                f"{ticket_id}: model {result.outcome} (rule {result.rule_id}) "
                f"vs gold table {expected[ticket_id]}"
            )
        if ticket_id in labels:
            label = labels[ticket_id]
            if result.outcome != label["expected_outcome"]:
                label_diffs.append(
                    f"{ticket_id}: model {result.outcome} vs label "
                    f"{label['expected_outcome']}"
                )
            elif result.approver_role != label["expected_approver_role"]:
                label_diffs.append(
                    f"{ticket_id}: outcome matches but approver "
                    f"{result.approver_role} vs {label['expected_approver_role']}"
                )
    return outcomes, table_diffs, label_diffs


def injection_scores(rows: list[dict]) -> tuple[int, int, list[str], list[str]]:
    threshold = float(Decimal(os.getenv("INJECTION_MIN_PROB", "0.5")))
    positives = {"T020", "T021", "P05"}
    hits = [row["ticket_id"] for row in rows if row["ticket_id"] in positives and row["injection"] >= threshold]
    false_positives = [
        row["ticket_id"]
        for row in rows
        if row["ticket_id"] not in positives and row["injection"] >= threshold
    ]
    return len(hits), len(positives), hits, false_positives


def _pct(misses: int, total: int) -> str:
    correct = total - misses
    return f"{correct}/{total} ({100 * correct / total:.1f}%)"


def render(
    gold: dict,
    mode_results: list[dict],
    injection: tuple[int, int, list[str], list[str]],
    e2e: dict[str, dict],
) -> str:
    lines = [
        "# Extraction scores",
        "",
        "Generated by `python -m eval.score_extraction` from "
        "`tests/fixtures/gold_extractions.json` and the 40-ticket data room. "
        "The gold `tests/output/decisions.md` table is the end-to-end baseline.",
        "",
        "`qty` scores a model `null` as 1 when every matching order line holds a "
        "single unit, because `resolve.py` maps it to 1 in that case. The "
        "extraction prompt is unchanged.",
        "",
    ]

    for result in mode_results:
        mode = result["mode"]
        trace: Trace = result["trace"]
        lines += [
            f"## Classifier mode: `{mode}`",
            "",
            "| Field | Accuracy | Misses |",
            "|---|---:|---|",
        ]
        for field in FIELDS:
            misses = result["misses"][field]
            lines.append(
                f"| {field} | {_pct(len(misses), len(gold))} | "
                f"{', '.join(misses) if misses else '—'} |"
            )
        lines += [
            "",
            f"Tokens: {trace.input_tokens} input + {trace.output_tokens} "
            f"output = {trace.total_tokens}. List-price cost: "
            f"${trace.cost_usd:.8f}. Billed cost: "
            f"${trace.billed_cost_usd:.8f}. Cache hits: {trace.cache_hits}.",
            "",
        ]
        diffs = e2e[mode]["table_diffs"]
        lines += [
            f"End-to-end outcomes vs `tests/output/decisions.md`: "
            f"{len(gold) - len(diffs)}/{len(gold)} match.",
            "",
        ]
        if diffs:
            lines += ["| Ticket | Difference |", "|---|---|"]
            lines += [f"| {diff.split(':', 1)[0]} | {diff.split(':', 1)[1].strip()} |" for diff in diffs]
            lines.append("")
        label_diffs = e2e[mode]["label_diffs"]
        lines += [
            f"Labels: {len(e2e[mode]['labels_total']) - len(label_diffs)}/"
            f"{len(e2e[mode]['labels_total'])} match on outcome and approver.",
            "",
        ]
        if label_diffs:
            lines += ["| Ticket | Difference |", "|---|---|"]
            lines += [f"| {diff.split(':', 1)[0]} | {diff.split(':', 1)[1].strip()} |" for diff in label_diffs]
            lines.append("")

    recall, total, hits, false_positives = injection
    lines += [
        "## Injection",
        "",
        f"Recall on T020, T021 and P05: {recall}/{total} "
        f"({'PASS' if recall == total else 'FAIL'}). Hits: "
        f"{', '.join(hits) if hits else '—'}.",
        "",
        f"False positives on the other 37: {len(false_positives)} "
        f"({'PASS' if not false_positives else 'FAIL'}). "
        f"{', '.join(false_positives) if false_positives else '—'}.",
        "",
    ]

    lines += [
        "## Totals",
        "",
        "| Mode | Input tokens | Output tokens | Total tokens | List-price cost | Billed cost | Cache hits |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in mode_results:
        trace = result["trace"]
        lines.append(
            f"| {result['mode']} | {trace.input_tokens} | "
            f"{trace.output_tokens} | {trace.total_tokens} | "
            f"${trace.cost_usd:.8f} | ${trace.billed_cost_usd:.8f} | "
            f"{trace.cache_hits} |"
        )
    lines += [
        "",
        "Billed cost counts only the calls made in this run; with a full cache "
        "the re-run is free. List-price cost is computed from the recorded "
        "tokens with `EXTRACT_PRICE_*` as `Decimal`, so it is what a fresh "
        "(cache-bypassed) run would be billed. Raw responses are cached under "
        "`prepared/model_cache/`; `MODEL_CACHE_DISABLED=1` or `--no-cache` "
        "forces new calls.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass prepared/model_cache and call the models again",
    )
    args = parser.parse_args(argv)
    use_cache = not args.no_cache

    tickets, gold, labels, expected, _, data = load_inputs()
    mode_results: list[dict] = []
    e2e: dict[str, dict] = {}
    injection_rows: list[dict] | None = None

    for mode in ("decider", "llm"):
        misses, records, trace, rows = score_fields(
            mode, tickets, gold, data, use_cache
        )
        outcomes, table_diffs, label_diffs = run_end_to_end(
            mode, tickets, gold, labels, expected, data, records
        )
        mode_results.append(
            {"mode": mode, "misses": misses, "records": records, "trace": trace}
        )
        e2e[mode] = {
            "outcomes": outcomes,
            "table_diffs": table_diffs,
            "label_diffs": label_diffs,
            "labels_total": labels,
        }
        if injection_rows is None:
            injection_rows = rows
        else:
            # The injection value comes from the extractor, which is the same
            # model in both modes; keep the first run's rows.
            pass

    injection = injection_scores(injection_rows or [])
    text = render(gold, mode_results, injection, e2e)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
