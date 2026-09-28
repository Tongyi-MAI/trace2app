
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Sequence

from .text_patch import apply_text_patch_edits


IssueList = List[Dict[str, Any]]
Validator = Callable[[str], Sequence[Dict[str, Any]]]
PatchPlanner = Callable[[str, IssueList, int], Dict[str, Any] | Awaitable[Dict[str, Any]]]


def _issue_count(issues: Sequence[Dict[str, Any]]) -> int:
    return len([issue for issue in issues if isinstance(issue, dict)])


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def run_text_fix_loop(
    *,
    initial_content: str,
    validate: Validator,
    plan_patch: PatchPlanner,
    target_id: str,
    max_rounds: int = 1,
) -> Dict[str, Any]:

    current = initial_content
    current_issues = list(validate(current) or [])
    report: Dict[str, Any] = {
        "format": "fixloop_result",
        "target_id": target_id,
        "initial_issue_count": _issue_count(current_issues),
        "rounds": [],
        "final_ok": not current_issues,
        "final_issue_count": _issue_count(current_issues),
        "content": current,
    }
    if not current_issues:
        return report

    for round_index in range(max(0, int(max_rounds or 0))):
        patch = await _maybe_await(plan_patch(current, current_issues, round_index))
        candidate, apply_report = apply_text_patch_edits(current, patch or {}, target_id=target_id)
        round_report: Dict[str, Any] = {
            "round_index": round_index,
            "issues_before": current_issues,
            "patch": patch,
            "apply_report": apply_report,
        }
        if not apply_report.get("success"):
            round_report["decision"] = "rejected"
            round_report["reject_reason"] = "patch_apply_failed"
            report["rounds"].append(round_report)
            break

        candidate_issues = list(validate(candidate) or [])
        round_report["issues_after"] = candidate_issues
        if _issue_count(candidate_issues) < _issue_count(current_issues):
            current = candidate
            current_issues = candidate_issues
            round_report["decision"] = "accepted"
            report["rounds"].append(round_report)
            if not current_issues:
                break
            continue

        round_report["decision"] = "rejected"
        round_report["reject_reason"] = "validation_not_improved"
        report["rounds"].append(round_report)
        break

    report["final_ok"] = not current_issues
    report["final_issue_count"] = _issue_count(current_issues)
    report["final_issues"] = current_issues
    report["content"] = current
    return report
