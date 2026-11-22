#!/usr/bin/env python3
"""Validate one execution packet written by the execute-trace skill.

Checks the packet schema and status, the execution boundary flags, and artifact isolation: the
packet and every artifact it names must live inside its own round directory

    <result_dir>/run_management/execution/<trace_id>/round_<NNNN>/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED_SCHEMA = "trace2world.gui_execution.packet.v1"
REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")
ALLOWED_STATUSES = {
    "execution_complete",
    "stopped_at_first_blocker",
    "execution_invalid",
    "evidence_blocked",
}
ALLOWED_DRIVERS = {"host_api", "playwright"}
MUST_BE_TRUE = ("pure_gui_execution", "artifact_isolation_verified")
MUST_BE_FALSE = (
    "review_performed",
    "sprites_generated",
    "pairwise_alignment_claimed",
    "intermediate_direct_navigation_used",
    "action_id_locator_used",
    "forced_or_dom_click_used",
    "state_or_query_injection_used",
    "inspection_endpoint_used_for_action_choice",
)
SHORTCUT_FLAGS = MUST_BE_FALSE[3:]


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"packet root must be an object: {path}")
    return value


def absolute_path(value: Any, *, field: str, errors: list[str], kind: str | None = None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        errors.append(f"{field} must be absolute: {value}")
        return None
    resolved = path.resolve(strict=False)
    if kind == "file" and not resolved.is_file():
        errors.append(f"{field} file does not exist: {resolved}")
    elif kind == "dir" and not resolved.is_dir():
        errors.append(f"{field} directory does not exist: {resolved}")
    return resolved


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_round_layout(root: Path, trace_id: str, errors: list[str]) -> None:
    expected = (
        (re.fullmatch(r"round_[0-9]{4}", root.name) is not None, "round_<NNNN>"),
        (root.parent.name == trace_id, trace_id),
        (root.parent.parent.name == "execution", "execution"),
        (root.parent.parent.parent.name == "run_management", "run_management"),
    )
    if not all(ok for ok, _ in expected):
        errors.append(
            "execution_attempt_root must be <result_dir>/run_management/execution/"
            f"{trace_id}/round_<NNNN>: {root}"
        )


def validate_flags(packet: dict[str, Any], errors: list[str]) -> None:
    if packet.get("driver") not in ALLOWED_DRIVERS:
        errors.append(f"driver must be one of {sorted(ALLOWED_DRIVERS)}")
    for field in MUST_BE_TRUE:
        if packet.get(field) is not True:
            errors.append(f"{field} must be true")
    invalid = packet.get("status") == "execution_invalid"
    for field in MUST_BE_FALSE:
        if field in SHORTCUT_FLAGS and invalid and isinstance(packet.get(field), bool):
            continue
        if packet.get(field) is not False:
            errors.append(f"{field} must be false")
    used = [field for field in SHORTCUT_FLAGS if packet.get(field) is True]
    if used and packet.get("status") != "execution_invalid":
        errors.append(f"shortcut flags {used} require status=execution_invalid")
    if packet.get("status") == "stopped_at_first_blocker" and not packet.get("first_blocker"):
        errors.append("stopped_at_first_blocker requires a non-empty first_blocker")
    if packet.get("continued_after_anomaly") is True and not str(packet.get("continuation_rationale") or "").strip():
        errors.append("continued_after_anomaly requires continuation_rationale")


def validate_artifact_isolation(packet: dict[str, Any], *, packet_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    trace_id = packet.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        return ["trace_id must be a non-empty string"]
    root = absolute_path(packet.get("execution_attempt_root"), field="execution_attempt_root", errors=errors, kind="dir")
    if root is None:
        return errors
    validate_round_layout(root, trace_id, errors)

    screenshot_dir = absolute_path(packet.get("screenshot_output_dir"), field="screenshot_output_dir", errors=errors, kind="dir")
    capture_program = absolute_path(packet.get("capture_program_path"), field="capture_program_path", errors=errors, kind="file")
    raw_log = absolute_path(packet.get("raw_browser_log_path"), field="raw_browser_log_path", errors=errors, kind="file")
    for field, path in (
        ("screenshot_output_dir", screenshot_dir),
        ("capture_program_path", capture_program),
        ("raw_browser_log_path", raw_log),
    ):
        if path is not None and not is_within(path, root):
            errors.append(f"{field} escapes execution_attempt_root: {path}")
    if screenshot_dir is not None and screenshot_dir != (root / "screenshots").resolve(strict=False):
        errors.append(f"screenshot_output_dir must be {root / 'screenshots'}")

    if packet_path is not None:
        resolved = packet_path.expanduser().resolve(strict=False)
        if not resolved.is_file():
            errors.append(f"execution packet file does not exist: {resolved}")
        if not is_within(resolved, root):
            errors.append(f"execution packet must be stored inside execution_attempt_root: {resolved}")

    states = packet.get("observed_states")
    if not isinstance(states, list) or not states:
        errors.append("observed_states must be a non-empty list")
        return errors
    for index, row in enumerate(states):
        if not isinstance(row, dict):
            errors.append(f"observed_states[{index}] must be an object")
            continue
        screenshot = absolute_path(row.get("screenshot_path"), field=f"observed_states[{index}].screenshot_path", errors=errors, kind="file")
        if screenshot is not None and screenshot_dir is not None and not is_within(screenshot, screenshot_dir):
            errors.append(f"observed_states[{index}].screenshot_path must be inside screenshot_output_dir: {screenshot}")
    return errors


def validate_execution_packet(packet: dict[str, Any], *, packet_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if packet.get("schema") != EXPECTED_SCHEMA:
        errors.append(f"schema must be {EXPECTED_SCHEMA}")
    if packet.get("status") not in ALLOWED_STATUSES:
        errors.append("status must be one of " + ", ".join(sorted(ALLOWED_STATUSES)))
    revision = packet.get("package_revision")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        errors.append("package_revision must be the package_delta.py revision (sha256:<64 hex>)")
    for index, row in enumerate(packet.get("observed_states") or []):
        if isinstance(row, dict) and not (isinstance(row.get("url"), str) and row["url"].strip()):
            errors.append(f"observed_states[{index}].url is required: invalidation maps it to package files")
    validate_flags(packet, errors)
    errors.extend(validate_artifact_isolation(packet, packet_path=packet_path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execution-packet", type=Path, required=True)
    args = parser.parse_args()
    try:
        packet = load_object(args.execution_packet)
    except ValueError as exc:
        print(json.dumps({"status": "fail", "errors": [str(exc)]}, indent=2))
        return 1
    errors = validate_execution_packet(packet, packet_path=args.execution_packet)
    print(
        json.dumps(
            {
                "status": "pass" if not errors else "fail",
                "trace_id": packet.get("trace_id"),
                "execution_attempt_root": packet.get("execution_attempt_root"),
                "observed_state_count": len(packet.get("observed_states") or []),
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
