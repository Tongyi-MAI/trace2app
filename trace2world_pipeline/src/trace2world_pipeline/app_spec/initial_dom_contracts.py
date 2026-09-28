
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


_OBLIGATION_TYPES = frozenset({
    "visible_section", "hidden_section", "absent_overlay",
    "visible_list", "visible_tab", "selected_tab",
    "visible_counter", "visible_input", "visible_control",
    "visible_button", "default_empty_state",
})


def _compile_section_obligations(
    initial_state: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for section in _as_list(initial_state.get("visible_sections")):
        if isinstance(section, dict):
            obligations.append({
                "obligation_type": "visible_section",
                "element_id": _text(section.get("section_id") or section.get("id")),
                "label": _text(section.get("label") or section.get("title")),
                "expected_visible": True,
                "source": "initial_state.visible_sections",
            })
        elif isinstance(section, str) and section.strip():
            obligations.append({
                "obligation_type": "visible_section",
                "element_id": section.strip(),
                "label": section.strip(),
                "expected_visible": True,
                "source": "initial_state.visible_sections",
            })

    for section in _as_list(initial_state.get("hidden_sections")):
        sid = _text(section) if isinstance(section, str) else _text(
            section.get("section_id") or section.get("id")
        ) if isinstance(section, dict) else ""
        if sid:
            obligations.append({
                "obligation_type": "hidden_section",
                "element_id": sid,
                "label": sid,
                "expected_visible": False,
                "source": "initial_state.hidden_sections",
            })

    return obligations


def _compile_overlay_obligations(
    initial_state: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for overlay in _as_list(initial_state.get("absent_overlays")):
        oid = _text(overlay) if isinstance(overlay, str) else _text(
            overlay.get("overlay_id") or overlay.get("id")
        ) if isinstance(overlay, dict) else ""
        if oid:
            obligations.append({
                "obligation_type": "absent_overlay",
                "element_id": oid,
                "label": oid,
                "expected_visible": False,
                "source": "initial_state.absent_overlays",
            })
    return obligations


def _compile_list_obligations(
    initial_state: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for collection in _as_list(initial_state.get("visible_lists")):
        if not isinstance(collection, dict):
            continue
        list_id = _text(collection.get("list_id") or collection.get("id"))
        if not list_id:
            continue
        expected_count = collection.get("expected_item_count")
        obligations.append({
            "obligation_type": "visible_list",
            "element_id": list_id,
            "label": _text(collection.get("label") or list_id),
            "expected_visible": True,
            "expected_item_count": expected_count,
            "source": "initial_state.visible_lists",
        })
    return obligations


def _compile_tab_obligations(
    initial_state: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for tab in _as_list(initial_state.get("tabs")):
        if not isinstance(tab, dict):
            continue
        tab_id = _text(tab.get("tab_id") or tab.get("id"))
        if not tab_id:
            continue
        obligations.append({
            "obligation_type": "selected_tab" if tab.get("selected") else "visible_tab",
            "element_id": tab_id,
            "label": _text(tab.get("label") or tab_id),
            "expected_visible": True,
            "expected_selected": bool(tab.get("selected")),
            "source": "initial_state.tabs",
        })
    return obligations


def _compile_counter_obligations(
    initial_state: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for counter in _as_list(initial_state.get("counters")):
        if not isinstance(counter, dict):
            continue
        cid = _text(counter.get("counter_id") or counter.get("id"))
        if not cid:
            continue
        obligations.append({
            "obligation_type": "visible_counter",
            "element_id": cid,
            "label": _text(counter.get("label") or cid),
            "expected_visible": True,
            "expected_value": counter.get("expected_value"),
            "source": "initial_state.counters",
        })
    return obligations


def compile_initial_dom_contracts(
    page_environment_specs: Mapping[str, Any],
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))

    contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    issues: List[Dict[str, Any]] = []
    total_obligation_count = 0

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        initial_state = _as_dict(spec.get("initial_state"))

        if not initial_state:
            continue

        obligations: List[Dict[str, Any]] = []
        obligations.extend(_compile_section_obligations(initial_state))
        obligations.extend(_compile_overlay_obligations(initial_state))
        obligations.extend(_compile_list_obligations(initial_state))
        obligations.extend(_compile_tab_obligations(initial_state))
        obligations.extend(_compile_counter_obligations(initial_state))

        if obligations:
            contracts_by_page[filename] = obligations
            total_obligation_count += len(obligations)

        has_evidence = bool(
            initial_state.get("visible_sections")
            or initial_state.get("visible_lists")
            or initial_state.get("tabs")
            or initial_state.get("counters")
            or initial_state.get("controls")
        )
        if has_evidence and not obligations and not _as_list(initial_state.get("controls")):
            issues.append({
                "code": "initial_dom_contract_missing",
                "severity": "blocking",
                "page_filename": filename,
                "message": f"page {filename} has initial_state evidence but no DOM obligations compiled",
            })

        for collection in _as_list(initial_state.get("visible_lists")):
            if isinstance(collection, dict):
                list_id = _text(collection.get("list_id") or collection.get("id"))
                if list_id and collection.get("expected_item_count") is None:
                    issues.append({
                        "code": "initial_collection_oracle_missing",
                        "severity": "warning",
                        "page_filename": filename,
                        "element_id": list_id,
                        "message": f"list {list_id} on {filename} has no expected_item_count oracle",
                    })

    return {
        "format": "initial_dom_contracts",
        "schema_revision": 1,
        "initial_dom_contracts_by_page_id": dict(contracts_by_page),
        "issues": issues,
        "summary": {
            "page_count": len(contracts_by_page),
            "obligation_count": total_obligation_count,
            "issue_count": len(issues),
        },
    }
