#!/usr/bin/env python3
"""Validate one repair report before it is handed back to the closure loop.

A repair report (``trace2world.repair_report.v1``) is the output of ``Repair(E, z | C)``: the
defect it answered, the diagnosis, the edited dependency set Delta, and the raw browser assertions
the repairer ran. It is not a pass: execution, review, and probes run again after Invalidate.

    python validate_repair_report.py --report "$REPAIR/repair_report.json"

Checks:

- schema, owner (``implementation`` or ``supervision``), status, and the defect fields;
- for ``status=repaired``: ``delta`` and both revisions equal the ``package_delta.py diff`` output
  named by ``delta_path``, that output passed (frozen inputs unchanged, non-empty Delta), and the
  issue class belongs to the owner;
- the raw assertion report exists, its SHA-256 matches, ``all_cases_pass`` is true, and every case
  passes; stateful implementation classes must include the six state-carrier cases;
- every adjacent acceptance check either passed or carries a reason;
- for ``status=not_repairable``: the package revision did not move and a referral names the owner
  (``semantics`` or ``provider``) and the unsupported behavior;
- the report never claims closure (``verified``, ``review_pass``, ``probe_pass`` are rejected).

Exit 0 when the report is valid, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "trace2world.repair_report.v1"
DELTA_SCHEMA = "trace2world.repair_delta.v1"
ISSUE_CLASSES = {
    "implementation": {
        "initialization",
        "state_handoff",
        "parent_projection",
        "shared_page",
        "lifecycle",
        "actionability",
        "route",
        "overlay",
        "visual_layout",
        "resource",
        "content",
    },
    "supervision": {
        "missing_checkpoint",
        "over_grouped_checkpoint",
        "missing_emit",
        "weak_payload",
        "weak_evaluator",
        "stale_test_flow",
        "terminal_policy",
        "stale_episode_read",
    },
}
STATEFUL_CLASSES = {"initialization", "state_handoff", "parent_projection", "shared_page"}
STATE_CARRIER_CASES = (
    "empty_state_with_full_diagnostic_context",
    "legacy_trace_key_only",
    "dirty_sibling_state",
    "valid_app_state_without_step_context",
    "natural_action_then_context_ablation",
    "parent_reload_or_reset",
)
CLOSURE_CLAIMS = ("verified", "review_pass", "probe_pass", "closure_status")
DEFECT_FIELDS = ("stage", "code", "expected_state", "observed_state", "obligation_ids", "packet_path")


def load(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def resolve(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def case_passed(row: Any) -> bool:
    return isinstance(row, dict) and (
        row.get("status") == "pass" or row.get("verdict") == "pass" or row.get("pass") is True
    )


def check_raw_report(report: dict[str, Any], base: Path, stateful: bool, errors: list[str]) -> None:
    assertions = report.get("assertions")
    if not isinstance(assertions, dict):
        errors.append("assertions must name the raw assertion report")
        return
    path = resolve(base, assertions.get("raw_report_path"))
    if path is None or not path.is_file():
        errors.append("assertions.raw_report_path must point to an existing file")
        return
    try:
        raw_report, raw = load(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"raw assertion report is not valid JSON: {exc}")
        return
    if assertions.get("raw_report_sha256") != hashlib.sha256(raw).hexdigest():
        errors.append("assertions.raw_report_sha256 does not match the raw report")
    if not isinstance(raw_report, dict):
        errors.append("raw assertion report must be an object")
        return
    if raw_report.get("all_cases_pass") is not True:
        errors.append("raw assertion report all_cases_pass must be true")
    cases = raw_report.get("cases")
    if not isinstance(cases, dict) or not cases:
        errors.append("raw assertion report cases must be a non-empty object")
        return
    for name, row in cases.items():
        if not case_passed(row):
            errors.append(f"raw assertion case did not pass: {name}")
    if stateful:
        for name in STATE_CARRIER_CASES:
            if name not in cases:
                errors.append(f"stateful repair is missing state-carrier case: {name}")


def validate(report: Any, report_path: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report must be a JSON object"]
    base = report_path.parent
    if report.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    owner = report.get("owner")
    if owner not in ISSUE_CLASSES:
        errors.append("owner must be 'implementation' or 'supervision'")
    for field in CLOSURE_CLAIMS:
        if field in report:
            errors.append(f"a repair report may not claim closure ({field}); execution, review, and probes run again")
    if not report.get("slice_id"):
        errors.append("slice_id is required")

    defect = report.get("defect")
    if not isinstance(defect, dict):
        errors.append("defect must describe the packet this repair answers")
    else:
        for field in DEFECT_FIELDS:
            if defect.get(field) in (None, "", []):
                errors.append(f"defect.{field} is required")
        if defect.get("stage") not in {"review", "probe"}:
            errors.append("defect.stage must be 'review' or 'probe'")

    status = report.get("status")
    if status not in {"repaired", "not_repairable"}:
        errors.append("status must be 'repaired' or 'not_repairable'")
        return errors

    diagnosis = report.get("diagnosis")
    if not isinstance(diagnosis, dict):
        errors.append("diagnosis is required")
        diagnosis = {}
    for field in ("issue_class", "first_stale_consumer", "rationale"):
        if not diagnosis.get(field):
            errors.append(f"diagnosis.{field} is required")
    issue_class = diagnosis.get("issue_class")

    delta_path = resolve(base, report.get("delta_path"))
    delta_record: dict[str, Any] | None = None
    if delta_path is not None and delta_path.is_file():
        try:
            delta_record, _ = load(delta_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"delta_path is not valid JSON: {exc}")

    if status == "repaired":
        if owner in ISSUE_CLASSES and issue_class not in ISSUE_CLASSES[owner]:
            errors.append(f"diagnosis.issue_class {issue_class!r} does not belong to owner {owner!r}")
        if delta_record is None:
            errors.append("delta_path must point to the package_delta.py diff output")
        else:
            if delta_record.get("schema") != DELTA_SCHEMA:
                errors.append(f"delta file schema must be {DELTA_SCHEMA}")
            if delta_record.get("status") != "pass":
                errors.append(f"delta check failed: {delta_record.get('errors')}")
            if sorted(report.get("delta") or []) != sorted(delta_record.get("delta") or []):
                errors.append("report delta differs from the measured delta")
            for field in ("package_revision_before", "package_revision_after"):
                if report.get(field) != delta_record.get(field):
                    errors.append(f"{field} differs from the measured delta")
        check_raw_report(report, base, owner == "implementation" and issue_class in STATEFUL_CLASSES, errors)
        expansion = report.get("acceptance_expansion")
        checks = expansion.get("adjacent_checks") if isinstance(expansion, dict) else None
        if not isinstance(checks, list) or not checks:
            errors.append("acceptance_expansion.adjacent_checks must list the checks around the reported symptom")
        else:
            for index, check in enumerate(checks):
                if not isinstance(check, dict) or not check.get("name"):
                    errors.append(f"adjacent_checks[{index}] needs a name")
                elif check.get("status") != "pass" and not check.get("reason"):
                    errors.append(f"adjacent check {check['name']} did not pass and gives no reason")
    else:
        if report.get("delta"):
            errors.append("a not_repairable report returns an empty delta")
        if delta_record is not None and delta_record.get("package_revision_before") != delta_record.get(
            "package_revision_after"
        ):
            errors.append("a not_repairable report must leave the package revision unchanged")
        referral = report.get("referral")
        if not isinstance(referral, dict) or referral.get("owner") not in {"semantics", "provider"}:
            errors.append("referral.owner must be 'semantics' or 'provider'")
        elif not referral.get("unsupported_behavior"):
            errors.append("referral.unsupported_behavior must name what closure would need")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        report, _ = load(args.report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "errors": [f"cannot read report: {exc}"]}, indent=2))
        return 1
    errors = validate(report, args.report.resolve())
    print(json.dumps({"status": "fail" if errors else "pass", "errors": errors}, indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
