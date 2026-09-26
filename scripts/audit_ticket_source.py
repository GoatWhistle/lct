"""Cross-check ticket coverage, caller phones, and explicit Moscow locality.

This script verifies transcription-to-YAML transfer but does not itself compare
scenario facts with the scanned PDF. A separate page-by-page visual audit of
all 32 pages and 96 scenarios is recorded in docs/evidence.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "docs/spec/TICKETS.md"
SCENARIOS = ROOT / "scenarios/tickets"
PHONE = re.compile(r"(?<!\d)(?:\+?7|8)?(?:[\s().-]*\d){10,11}(?!\d)")
HIGHWAY = re.compile(r"(?<![а-яё])м\d+\b", re.IGNORECASE)
CALL = re.compile(r"^\s*([1-3])\.\s*(.*)$")
HEADING = re.compile(r"^## Билет (\d+)\s*$")


def canonical_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits[0] in "78" else digits


def phones(text: str) -> list[str]:
    return [canonical_phone(match.group()) for match in PHONE.finditer(text)]


def source_rows(text: str) -> tuple[dict[tuple[int, int], dict], list[int]]:
    rows: dict[tuple[int, int], dict] = {}
    tickets: list[int] = []
    current_ticket: int | None = None
    current_position: int | None = None
    situation = ""
    address = ""

    def save() -> None:
        if current_ticket is not None and current_position is not None:
            key = (current_ticket, current_position)
            if key in rows:
                raise ValueError(
                    f"повторная строка источника: билет {key[0]}, вызов {key[1]}"
                )
            rows[key] = {"situation": situation.strip(), "address": address.strip()}

    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            save()
            current_ticket = int(heading.group(1))
            tickets.append(current_ticket)
            current_position = None
            situation = address = ""
            continue

        call = CALL.match(line)
        if call:
            save()
            current_position = int(call.group(1))
            situation = call.group(2)
            address = ""
            if "→" in situation:
                situation, address = situation.split("→", 1)
            continue

        if current_position is not None and "→" in line and not address:
            address = line.split("→", 1)[1]

    save()
    return rows, tickets


def audit() -> dict:
    raw = TICKETS.read_text(encoding="utf-8")
    source, headings = source_rows(raw)
    scenario_rows: dict[tuple[int, int], tuple[Path, dict]] = {}
    duplicate_scenarios: list[dict] = []
    for path in sorted(SCENARIOS.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        key = (payload.get("ticket"), payload.get("position"))
        if key[0] is None or key[1] is None:
            continue
        if key in scenario_rows:
            duplicate_scenarios.append(
                {
                    "ticket": key[0],
                    "position": key[1],
                    "files": [
                        str(scenario_rows[key][0].relative_to(ROOT)),
                        str(path.relative_to(ROOT)),
                    ],
                }
            )
        scenario_rows[key] = (path, payload)

    expected = {
        (ticket, position) for ticket in range(1, 33) for position in range(1, 4)
    }
    missing_scenarios = sorted(expected - set(scenario_rows))
    extra_scenarios = sorted(set(scenario_rows) - expected)
    missing_source = sorted(expected - set(source))
    extra_source = sorted(set(source) - expected)
    phone_mismatches = []
    address_locality_mismatches = []
    highway_designation_mismatches = []

    for key in sorted(expected & set(source) & set(scenario_rows)):
        path, payload = scenario_rows[key]
        caller = next(
            (
                fact.get("value", "")
                for fact in payload.get("facts", [])
                if fact.get("id") == "f_caller"
            ),
            "",
        )
        expected_phones = phones(source[key]["situation"])
        actual_phones = phones(caller)
        if Counter(expected_phones) != Counter(actual_phones):
            phone_mismatches.append(
                {
                    "ticket": key[0],
                    "position": key[1],
                    "scenario": str(path.relative_to(ROOT)),
                    "source_phones": expected_phones,
                    "scenario_phones": actual_phones,
                }
            )

        if source[key]["address"].lstrip().startswith("Москва,"):
            address_facts = [
                fact
                for fact in payload.get("facts", [])
                if fact.get("id") == "f_address"
            ]
            fact_values = [str(fact.get("value", "")) for fact in address_facts]
            ground_truth_address = str(
                payload.get("ground_truth", {}).get("address", "")
            )
            missing_fields = []
            if not any(
                value.strip().casefold().startswith("москва,") for value in fact_values
            ):
                missing_fields.append("f_address")
            if not ground_truth_address.strip().casefold().startswith("москва,"):
                missing_fields.append("ground_truth.address")
            if missing_fields:
                address_locality_mismatches.append(
                    {
                        "ticket": key[0],
                        "position": key[1],
                        "scenario": str(path.relative_to(ROOT)),
                        "missing_fields": missing_fields,
                    }
                )

        source_highways = {
            item.casefold().replace("ё", "е")
            for item in HIGHWAY.findall(source[key]["address"])
        }
        if source_highways:
            ground_truth_address = (
                str(payload.get("ground_truth", {}).get("address", ""))
                .casefold()
                .replace("ё", "е")
            )
            address_facts_text = (
                " ".join(
                    str(value)
                    for fact in payload.get("facts", [])
                    if fact.get("id") == "f_address"
                    for value in (fact.get("value", ""), fact.get("refined", ""))
                )
                .casefold()
                .replace("ё", "е")
            )
            missing_highways = {
                item: missing_fields
                for item in sorted(source_highways)
                if (missing_fields := [
                    field
                    for field, address_text in (
                        ("ground_truth.address", ground_truth_address),
                        ("facts.f_address", address_facts_text),
                    )
                    if item not in address_text
                ])
            }
            if missing_highways:
                highway_designation_mismatches.append(
                    {
                        "ticket": key[0],
                        "position": key[1],
                        "scenario": str(path.relative_to(ROOT)),
                        "missing_designations": missing_highways,
                    }
                )

    passed = (
        len(headings) == 32
        and sorted(headings) == list(range(1, 33))
        and len(source) == 96
        and len(scenario_rows) == 96
        and not missing_scenarios
        and not extra_scenarios
        and not missing_source
        and not extra_source
        and not duplicate_scenarios
        and not phone_mismatches
        and not address_locality_mismatches
        and not highway_designation_mismatches
    )
    return {
        "checker": "scripts/audit_ticket_source.py",
        "source": "docs/spec/TICKETS.md (manual transcription; not OCR of source PDF)",
        "scope": (
            "32 ticket headings, 96 ticket/position mappings, exact normalized caller-phone "
            "transfer, explicit Moscow locality preservation, and M-road designation preservation"
        ),
        "ticket_headings": len(headings),
        "transcribed_rows": len(source),
        "scenario_rows": len(scenario_rows),
        "phone_rows_matched": len(source) - len(phone_mismatches),
        "missing_source_rows": missing_source,
        "extra_source_rows": extra_source,
        "missing_scenario_rows": missing_scenarios,
        "extra_scenario_rows": extra_scenarios,
        "duplicate_scenario_rows": duplicate_scenarios,
        "phone_mismatches": phone_mismatches,
        "address_locality_mismatches": address_locality_mismatches,
        "highway_designation_mismatches": highway_designation_mismatches,
        "passed": passed,
        "limitations": [
            "does not prove that scenario meaning, addresses, signs, or ground truth match the scanned PDF",
            "this script performs no semantic comparison with the scanned PDF; see the separate manual audit evidence",
            "locality check only tests source addresses explicitly beginning with 'Москва,'",
            "M-road check only tests designations matching M followed by a number",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, help="also write the JSON result to this path"
    )
    args = parser.parse_args()
    result = audit()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
