#!/usr/bin/env python3
"""Audit package pages, scripts, and JSON for business state that a trace, not the app, owns.

Runtime state belongs to the app: the state kernel seeds it from the task's launch state and the
visible actions write it (paper Appendix B.1, "Runtime state ownership"). The scanner is
conservative and static. It reports:

- storage keys that embed a source task id (``task_<digits>``): business state keyed to one trace;
- lines where diagnostic context (step index, action id) decides a business value that is then
  written to the DOM or to storage.

    python audit_state_ownership.py --root "$RESULT/package" --output "$REPAIR/state_ownership_audit.json"

Exit 0 when nothing is found, 1 otherwise. ``--self-test`` checks the detectors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "trace2world.state_ownership_audit.v1"
TEXT_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".cjs", ".json"}
TRACE_STORAGE_RE = re.compile(
    r"(?:localStorage|sessionStorage)(?:\.(?:getItem|setItem|removeItem)\s*\(\s*|\s*\[\s*)"
    r"(?P<quote>['\"])(?P<key>[^'\"]*task_\d+[^'\"]*)(?P=quote)",
    re.IGNORECASE,
)
DIAGNOSTIC_CONTEXT_RE = re.compile(
    r"\b(?:step_index|raw_step_index|action_id|active_action_id|audit_step_index|isStep\w*Context)\b",
    re.IGNORECASE,
)
BUSINESS_TERM_RE = re.compile(
    r"selected|selection|query|draft|committed|recipient|address|quantity|amount|choice",
    re.IGNORECASE,
)
BUSINESS_WRITE_RE = re.compile(
    r"(?:\.value|\.textContent|\.innerText|\.innerHTML)\s*=|"
    r"classList\.(?:add|toggle)\s*\(\s*['\"](?:selected|active|checked)|"
    r"(?:localStorage|sessionStorage)\.setItem\s*\(",
    re.IGNORECASE,
)
DIAGNOSTIC_AUTHORITY_RE = re.compile(
    r"\bif\s*\(|\?|===|!==|==|!=|&&|\|\||"
    r"(?:\.value|\.textContent|\.innerText|\.innerHTML)\s*=",
    re.IGNORECASE,
)


def iter_files(roots: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in roots:
        root = root.expanduser().resolve()
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved


def finding(kind: str, path: Path, *, line: int | None, evidence: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "line": line,
        "evidence": evidence[:500],
    }


def scan_text(path: Path, text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        for match in TRACE_STORAGE_RE.finditer(line):
            findings.append(
                finding(
                    "trace_specific_business_storage_key",
                    path,
                    line=line_number,
                    evidence=match.group("key"),
                )
            )

        if not DIAGNOSTIC_CONTEXT_RE.search(line) or not DIAGNOSTIC_AUTHORITY_RE.search(line):
            continue
        start = max(0, line_number - 2)
        end = min(len(lines), line_number + 3)
        window = "\n".join(lines[start:end])
        if BUSINESS_TERM_RE.search(window) and BUSINESS_WRITE_RE.search(window):
            findings.append(
                finding(
                    "diagnostic_context_controls_business_projection",
                    path,
                    line=line_number,
                    evidence=window,
                )
            )
    return findings


def audit(roots: list[Path]) -> dict[str, Any]:
    files = list(iter_files(roots))
    findings: list[dict[str, Any]] = []
    read_errors: list[dict[str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            read_errors.append({"path": str(path), "error": str(exc)})
            continue
        findings.extend(scan_text(path, text))
    return {
        "schema": SCHEMA,
        "status": "pass" if not findings and not read_errors else "fail",
        "roots": [str(path.expanduser().resolve()) for path in roots],
        "files_scanned": len(files),
        "finding_count": len(findings),
        "findings": findings,
        "read_errors": read_errors,
    }


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="trace2world-state-ownership-audit-") as tmp:
        root = Path(tmp)
        (root / "bad.html").write_text(
            """
<script>
const isStepSixContext = ctx.step_index === 6;
if (isStepSixContext) storeNode.textContent = 'Store A';
localStorage.setItem('app.order.selected_store.task_20260101_000000_000001', 'x');
</script>
""",
            encoding="utf-8",
        )
        bad = audit([root])
        kinds = {row["kind"] for row in bad["findings"]}
        expected = {
            "trace_specific_business_storage_key",
            "diagnostic_context_controls_business_projection",
        }
        if bad["status"] != "fail" or not expected.issubset(kinds):
            print(json.dumps({"status": "fail", "report": bad}, ensure_ascii=False, indent=2))
            return 1

        for path in root.iterdir():
            path.unlink()
        (root / "good.html").write_text(
            """
<script>
const packet = JSON.parse(sessionStorage.getItem('app.store_selection.return.v1'));
if (packet && packet.role === 'store') storeNode.textContent = packet.selected_value;
</script>
""",
            encoding="utf-8",
        )
        good = audit([root])
        if good["status"] != "pass":
            print(json.dumps({"status": "fail", "report": good}, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps({"status": "pass", "schema": SCHEMA}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit package files for business state owned by a trace instead of the app."
    )
    parser.add_argument("--root", action="append", type=Path, help="File or directory to scan; repeatable")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.root:
        parser.error("at least one --root is required unless --self-test is used")
    report = audit(args.root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
