
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


_CONTROL_TYPE_KEYWORDS: Dict[str, str] = {
    "checkbox": "checkbox",
    "check": "checkbox",
    "勾选": "checkbox",
    "全选": "checkbox",
    "radio": "radio",
    "toggle": "toggle",
    "switch": "toggle",
    "开关": "toggle",
    "segmented": "segmented",
    "tab": "tab",
    "badge": "count_badge",
    "数量": "count_badge",
    "count": "count_badge",
}


def _infer_control_type(control: Mapping[str, Any]) -> str:
    explicit = _text(control.get("control_type"))
    if explicit in ("checkbox", "radio", "toggle", "segmented", "tab", "count_badge"):
        return explicit
    label = (_text(control.get("label")) + " " + _text(control.get("description"))).lower()
    for keyword, ctype in _CONTROL_TYPE_KEYWORDS.items():
        if keyword in label:
            return ctype
    element_type = _text(control.get("element_type") or control.get("type")).lower()
    if element_type in ("checkbox", "radio", "toggle"):
        return element_type
    return "checkbox"


def _extract_dom_oracle(control: Mapping[str, Any], control_type: str) -> str:
    explicit = _text(control.get("dom_oracle"))
    if explicit:
        return explicit
    expected_checked = control.get("expected_checked")
    if expected_checked is True:
        return f"visible_checked_{control_type}"
    if expected_checked is False:
        return f"visible_unchecked_{control_type}"
    return f"visible_{control_type}"


def _requirements_by_control(spec: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    initial_state = _as_dict(spec.get("initial_state"))
    requirements = (
        _as_list(spec.get("initial_control_state_requirements"))
        + _as_list(spec.get("trace_review_initial_control_state_requirements"))
        + _as_list(initial_state.get("control_requirements"))
    )
    result: Dict[str, Dict[str, Any]] = {}
    for item in requirements:
        if not isinstance(item, dict):
            continue
        for key in (
            _text(item.get("control_id") or item.get("id")),
            _text(item.get("label")),
        ):
            if key:
                result[key] = item
    return result


def compile_initial_control_state_contracts(
    page_environment_specs: Mapping[str, Any],
    content_samples: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))

    contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    issues: List[Dict[str, Any]] = []
    total_control_count = 0

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        initial_state = _as_dict(spec.get("initial_state"))
        controls = _as_list(initial_state.get("controls"))
        requirements_by_control = _requirements_by_control(spec)
        seen_controls = set()

        for control in controls:
            if not isinstance(control, dict):
                continue
            control_id = _text(control.get("control_id") or control.get("id"))
            label = _text(control.get("label"))
            if not control_id and not label:
                continue
            control_type = _infer_control_type(control)
            state_ref = _text(control.get("state_ref"))

            expected_checked = control.get("expected_checked")
            expected_value = control.get("expected_value")
            expected_visible = control.get("expected_visible", True)
            requirement = (
                requirements_by_control.get(control_id)
                or requirements_by_control.get(label)
                or {}
            )
            if requirement:
                for field in ("expected_checked", "expected_value", "expected_visible"):
                    if field in requirement and control.get(field) != requirement.get(field):
                        issues.append({
                            "code": "initial_control_state_wrong",
                            "severity": "blocking",
                            "page_filename": filename,
                            "control_id": control_id or label,
                            "message": (
                                f"control '{label or control_id}' on {filename} has "
                                f"{field}={control.get(field)!r} but expected {requirement.get(field)!r}"
                            ),
                        })
                if "expected_checked" in requirement:
                    expected_checked = requirement.get("expected_checked")
                if "expected_value" in requirement:
                    expected_value = requirement.get("expected_value")
                if "expected_visible" in requirement:
                    expected_visible = requirement.get("expected_visible")

            if control_type in ("checkbox", "radio", "toggle") and expected_checked is None:
                issues.append({
                    "code": "initial_control_state_contract_missing",
                    "severity": "blocking",
                    "page_filename": filename,
                    "control_id": control_id or label,
                    "message": (
                        f"control '{label or control_id}' on {filename} "
                        f"is {control_type} but has no expected_checked"
                    ),
                })
                continue

            if control_type == "count_badge" and expected_value is None:
                issues.append({
                    "code": "initial_control_state_contract_missing",
                    "severity": "blocking",
                    "page_filename": filename,
                    "control_id": control_id or label,
                    "message": (
                        f"control '{label or control_id}' on {filename} "
                        f"is count_badge but has no expected_value"
                    ),
                })
                continue

            dom_oracle = _extract_dom_oracle(control, control_type)
            contract: Dict[str, Any] = {
                "page_id": filename,
                "control_id": control_id or label,
                "label": label,
                "control_type": control_type,
                "state_ref": state_ref,
                "expected_checked": expected_checked,
                "expected_value": expected_value,
                "expected_visible": expected_visible,
                "dom_oracle": dom_oracle,
                "source_evidence_refs": [f"{filename}:initial_state:{control_id or label}"],
            }
            contracts_by_page[filename].append(contract)
            total_control_count += 1
            seen_controls.add(control_id or label)

        for key, requirement in requirements_by_control.items():
            if key in seen_controls:
                continue
            issues.append({
                "code": "initial_control_state_contract_missing",
                "severity": "blocking",
                "page_filename": filename,
                "control_id": key,
                "message": f"required initial control '{key}' on {filename} is missing",
            })

        group_refs: Dict[str, List[str]] = defaultdict(list)
        for contract in contracts_by_page.get(filename, []):
            ref = contract.get("state_ref", "")
            if ref:
                group_refs[ref].append(contract.get("control_id", ""))

        for ref, control_ids in group_refs.items():
            if len(control_ids) > 1:
                has_group_all = any(
                    "全选" in _text(c.get("label")) or "all" in _text(c.get("label")).lower()
                    for c in contracts_by_page.get(filename, [])
                    if c.get("state_ref") == ref
                )
                if has_group_all:
                    issues.append({
                        "code": "initial_control_state_inferred_from_count",
                        "severity": "warning",
                        "page_filename": filename,
                        "control_id": ref,
                        "message": (
                            f"state_ref '{ref}' on {filename} has group checkbox and "
                            f"individual items sharing the same ref — "
                            f"use separate state_refs for group vs individual"
                        ),
                    })

    return {
        "format": "initial_control_state_contracts",
        "schema_revision": 1,
        "initial_control_state_contracts_by_page_id": dict(contracts_by_page),
        "issues": issues,
        "summary": {
            "initial_control_page_count": len(contracts_by_page),
            "initial_control_count": total_control_count,
            "issue_count": len(issues),
        },
    }
