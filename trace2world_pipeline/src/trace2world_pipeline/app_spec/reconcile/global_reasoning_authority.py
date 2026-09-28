
from __future__ import annotations

import hashlib
import re
from typing import Any


_GENERIC_IDENTITY_LABELS = {
    "current_page",
    "generic_page",
    "page",
    "screen",
    "interface",
    "unknown",
    "unknown_page",
    "页面",
    "界面",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _stable_slug(text: str) -> str:
    text = _clean_text(text).lower()
    if not text:
        return ""
    text = re.sub(r"\b(page|screen|interface|view|surface)\b", " ", text)
    text = text.replace("页面", " ").replace("界面", " ").replace("页", " ")
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text)
    if not tokens:
        return ""
    slug = "_".join(tokens).strip("_")
    if not slug or slug in _GENERIC_IDENTITY_LABELS:
        return ""
    if _has_cjk(slug):
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return f"u_{digest}"
    return slug


def _obj_dict(value: Any, attr: str) -> dict[str, Any]:
    if isinstance(value, dict):
        candidate = value.get(attr)
    else:
        candidate = getattr(value, attr, None)
    return candidate if isinstance(candidate, dict) else {}


def _phase1_page_state(segment: dict[str, Any], obs: Any | None = None) -> dict[str, Any]:
    page_state = segment.get("phase1_page_state")
    if isinstance(page_state, dict) and page_state:
        return page_state
    return _obj_dict(obs, "page_state")


def _phase1_transition(segment: dict[str, Any], obs: Any | None = None) -> dict[str, Any]:
    transition = segment.get("phase1_transition")
    if isinstance(transition, dict) and transition:
        return transition
    out: dict[str, Any] = {}
    if isinstance(obs, dict):
        source = obs
    else:
        source = {
            "transition_kind": getattr(obs, "transition_kind", "") if obs is not None else "",
            "transition_from_previous": getattr(obs, "transition_from_previous", None) if obs is not None else None,
            "transition_evidence": getattr(obs, "transition_evidence", []) if obs is not None else [],
        }
    for key in ("transition_kind", "transition_from_previous", "transition_evidence"):
        value = source.get(key) if isinstance(source, dict) else None
        if value not in ("", [], {}, None):
            out[key] = value
    page_state = _phase1_page_state(segment, obs)
    for key in ("transition_kind", "transition_from_previous", "transition_evidence"):
        if key not in out and page_state.get(key) not in ("", [], {}, None):
            out[key] = page_state.get(key)
    return out


def _route_hint(segment: dict[str, Any]) -> dict[str, Any]:
    hint = segment.get("route_namespace_hint")
    return hint if isinstance(hint, dict) else {}


def _segment_identity_text(segment: dict[str, Any], page_state: dict[str, Any], transition: dict[str, Any]) -> str:
    values: list[str] = []
    for source in (page_state, transition, segment):
        for key in (
            "host_page_identity",
            "opened_surface_identity",
            "host_page_evidence",
            "standalone_page_evidence",
            "transition_evidence",
            "description",
            "visual_signature",
            "header_pattern",
            "shell_section",
            "body_pattern",
            "sticky_action_pattern",
            "page_title",
        ):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, list):
                values.extend(_clean_text(item) for item in value if _clean_text(item))
            elif _clean_text(value):
                values.append(_clean_text(value))
    for key in ("representative_actions", "representative_thoughts", "action_classes"):
        value = segment.get(key)
        if isinstance(value, list):
            values.extend(_clean_text(item) for item in value if _clean_text(item))
        elif _clean_text(value):
            values.append(_clean_text(value))
    hint = _route_hint(segment)
    if hint:
        values.extend(_clean_text(hint.get(key)) for key in ("label", "route_path") if _clean_text(hint.get(key)))
    return "\n".join(item for item in values if item)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered or marker in text for marker in markers)


def _identity_label(segment: dict[str, Any], page_state: dict[str, Any], transition: dict[str, Any]) -> str:
    standalone = page_state.get("standalone_page_evidence", [])
    has_standalone = isinstance(standalone, list) and any(_clean_text(item) for item in standalone)
    opened = _clean_text(page_state.get("opened_surface_identity"))
    host = _clean_text(page_state.get("host_page_identity"))
    transition_kind = _clean_text(transition.get("transition_kind"))
    if opened and has_standalone and not transition_kind.startswith("same_page"):
        return opened
    if host:
        return host
    if opened and not transition_kind.startswith("same_page"):
        return opened
    hint = _route_hint(segment)
    hint_label = _clean_text(hint.get("label")) if hint else ""
    if hint_label:
        return hint_label
    for key in ("page_title",):
        value = _clean_text(segment.get(key))
        if value and value.lower() not in _GENERIC_IDENTITY_LABELS:
            return value
    description = _clean_text(segment.get("description"))
    for pattern in (r"(?:full-screen|dedicated|standalone)\s+['\"]([^'\"]{3,80})['\"]\s+(?:page|screen|interface)",):
        match = re.search(pattern, description)
        if match:
            return _clean_text(match.group(1))
    return ""


def _page_role(segment: dict[str, Any], page_state: dict[str, Any], transition: dict[str, Any], text: str) -> str:
    lowered = text.lower()
    transition_kind = _clean_text(transition.get("transition_kind"))
    action_classes = {
        _clean_text(item).lower()
        for item in segment.get("action_classes", [])
        if _clean_text(item)
    } if isinstance(segment.get("action_classes"), list) else set()
    if transition_kind.startswith("same_page") and _contains_any(
        text,
        ("搜索", "search", "autocomplete", "suggestion", "候选", "results based on the input"),
    ):
        return "search_entry"
    if _contains_any(text, ("支付", "payment", "pay", "收银台", "checkout")):
        return "payment"
    if _contains_any(text, ("新增", "添加", "add new", "create", "new ")) and _contains_any(
        text, ("输入框", "input", "form", "表单", "保存", "save")
    ):
        return "add_form"
    if _contains_any(
        text,
        ("编辑", "修改", "edit", "modify", "update", "输入框", "input field", "form", "表单", "保存", "save"),
    ) or action_classes & {"type", "fill", "input"}:
        if _contains_any(text, ("搜索", "search", "autocomplete", "suggestion")):
            return "search_entry"
        return "edit_form"
    if _contains_any(text, ("搜索", "search", "autocomplete", "suggestion", "history chips", "recommendation chips")):
        return "search_entry"
    if _contains_any(text, ("详情", "detail", "timeline", "tracking", "状态", "只读", "readonly", "read-only")):
        return "detail_readonly"
    if _contains_any(
        text,
        ("列表", "list", "feed", "grid", "选择", "select", "候选项", "results", "management page"),
    ):
        return "listing"
    route_path = _clean_text(_route_hint(segment).get("route_path"))
    if route_path == "/home" or _contains_any(text, ("首页", "home page", "home tab")):
        return "home_shell"
    if _contains_any(text, ("个人中心", "profile", "我的", "me/profile")):
        return "profile_shell"
    if _clean_text(page_state.get("host_page_identity")) or route_path:
        return "stable_page"
    return ""


def _read_write_mode(role: str, text: str) -> str:
    if role in {"edit_form", "add_form"}:
        return "editable"
    if role in {"detail_readonly", "home_shell", "profile_shell", "stable_page"}:
        return "readonly"
    if role in {"listing", "search_entry"}:
        return "selection"
    if role == "payment":
        return "commit"
    if _contains_any(text, ("提交", "submit", "保存", "save", "支付", "pay")):
        return "commit"
    return ""


def _canonical_page_role(role: str, family: str, route_path: str) -> str:
    if route_path in {"/home", "/me", "/profile"} or family in {"home", "me", "profile", "12306_home"}:
        return "route_shell"
    if role in {"listing", "search_entry"}:
        return "selection_shell"
    return role


def _cannot_merge_roles(role: str) -> list[str]:
    boundaries = {
        "listing": ["add_form", "edit_form", "detail_readonly", "payment"],
        "search_entry": ["add_form", "edit_form", "detail_readonly", "payment"],
        "add_form": ["listing", "search_entry", "edit_form", "detail_readonly", "payment"],
        "edit_form": ["listing", "search_entry", "add_form", "detail_readonly", "payment"],
        "detail_readonly": ["listing", "search_entry", "add_form", "edit_form", "payment"],
        "payment": ["listing", "search_entry", "add_form", "edit_form", "detail_readonly"],
    }
    return boundaries.get(role, [])


def should_inherit_previous_page_identity(segment: dict[str, Any], obs: Any | None = None) -> bool:
    if isinstance(segment.get("same_page_surface_manifest"), dict):
        return True
    page_state = _phase1_page_state(segment, obs)
    transition = _phase1_transition(segment, obs)
    transition_kind = _clean_text(transition.get("transition_kind"))
    if not transition_kind.startswith("same_page"):
        return False
    standalone = page_state.get("standalone_page_evidence", [])
    if isinstance(standalone, list) and any(_clean_text(item) for item in standalone):
        return False
    return True


def inherit_page_identity_manifest(previous: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(previous, dict) or not _clean_text(previous.get("canonical_page_key")):
        return {}
    inherited = dict(previous)
    inherited["source"] = "phase1_same_page_identity_inheritance"
    inherited["inherited_from_canonical_page_key"] = previous.get("canonical_page_key")
    inherited["inherited_reason"] = "same_page_transition_without_standalone_page_evidence"
    return {k: v for k, v in inherited.items() if v not in ("", [], {}, None)}


def compile_page_identity_manifest_for_segment(segment: dict[str, Any], obs: Any | None = None) -> dict[str, Any]:
    existing = segment.get("page_identity_manifest")
    if isinstance(existing, dict) and existing:
        return dict(existing)
    contract = segment.get("page_identity_contract")
    if isinstance(contract, dict) and _clean_text(contract.get("canonical_page_key")):
        manifest = {"source": "explicit_page_identity_contract"}
        manifest.update(contract)
        return {k: v for k, v in manifest.items() if v not in ("", [], {}, None)}
    page_state = _phase1_page_state(segment, obs)
    transition = _phase1_transition(segment, obs)
    label = _identity_label(segment, page_state, transition)
    text = _segment_identity_text(segment, page_state, transition)
    role = _page_role(segment, page_state, transition, "\n".join([label, text]))
    route_path = _clean_text(_route_hint(segment).get("route_path"))
    route_family = _stable_slug(route_path.strip("/").replace("/", " "))
    label_family = _stable_slug(label)
    if not label_family:
        return {}
    if not role:
        return {}
    generic_search = label_family in {"search", "search_results", "results", "search_result"}
    if route_family and (generic_search or label_family.startswith("u_")):
        family = f"{route_family}_{label_family}" if generic_search else route_family
    else:
        family = label_family
    canonical_role = _canonical_page_role(role, family, route_path)
    canonical_page_key = f"{family}:{canonical_role}"
    read_write_mode = _read_write_mode(role, text)
    manifest: dict[str, Any] = {
        "source": "deterministic_phase1_page_identity_compiler",
        "identity_label": label,
        "identity_family": family,
        "page_role": canonical_role,
        "observed_page_role": role,
        "read_write_mode": read_write_mode,
        "canonical_page_key": canonical_page_key,
        "state_variant_policy": "state_fields_do_not_split_page_identity",
    }
    if route_path:
        manifest["route_path_hint"] = route_path
    cannot_roles = _cannot_merge_roles(canonical_role)
    if cannot_roles:
        manifest["cannot_merge_with_page_roles"] = cannot_roles
    return {k: v for k, v in manifest.items() if v not in ("", [], {}, None)}


def _segment_identity_contracts_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for trace in payload.get("traces", []):
        if not isinstance(trace, dict):
            continue
        for segment in trace.get("segments", []):
            if not isinstance(segment, dict):
                continue
            segment_id = _clean_text(segment.get("segment_id"))
            manifest = segment.get("page_identity_manifest") if isinstance(segment.get("page_identity_manifest"), dict) else {}
            if segment_id and _clean_text(manifest.get("canonical_page_key")):
                contracts[segment_id] = dict(manifest)
    constraints = payload.get("page_identity_constraints")
    rows = constraints.get("constraints", []) if isinstance(constraints, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        canonical = _clean_text(row.get("canonical_page_key"))
        if not canonical:
            continue
        segment_ids = row.get("segment_ids")
        ids = (
            [_clean_text(item) for item in segment_ids if _clean_text(item)]
            if isinstance(segment_ids, list)
            else [_clean_text(row.get("segment_id"))]
        )
        for segment_id in ids:
            if segment_id:
                contracts.setdefault(segment_id, dict(row))
    manifest = payload.get("chunk_authority_manifest")
    rows = manifest.get("page_identity_contracts_by_segment", []) if isinstance(manifest, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        segment_id = _clean_text(row.get("segment_id"))
        canonical = _clean_text(row.get("canonical_page_key"))
        if segment_id and canonical:
            contracts.setdefault(segment_id, dict(row))
    return contracts


def _contract_suffix(canonical_page_key: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", canonical_page_key.lower()).strip("_")
    if suffix:
        return suffix[:80]
    return hashlib.sha1(canonical_page_key.encode("utf-8")).hexdigest()[:10]


def _contract_from_rows(rows: list[dict[str, Any]], canonical_page_key: str) -> dict[str, Any]:
    selected = next(row for row in rows if _clean_text(row.get("canonical_page_key")) == canonical_page_key)
    contract = {
        key: selected[key]
        for key in (
            "canonical_page_key",
            "identity_family",
            "page_role",
            "read_write_mode",
            "route_path_hint",
            "state_variant_policy",
            "source",
        )
        if selected.get(key) not in ("", [], {}, None)
    }
    contract["authority_source_segment_count"] = sum(
        1 for row in rows if _clean_text(row.get("canonical_page_key")) == canonical_page_key
    )
    return contract


def attach_page_identity_contracts_to_output(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(payload, dict):
        return result
    segment_contracts = _segment_identity_contracts_from_payload(payload)
    if not segment_contracts:
        return result
    page_to_segments: dict[str, set[str]] = {}
    for row in result.get("segment_assignments", []):
        if not isinstance(row, dict):
            continue
        page_id = _clean_text(row.get("local_page_id"))
        segment_id = _clean_text(row.get("segment_id"))
        if page_id and segment_id:
            page_to_segments.setdefault(page_id, set()).add(segment_id)
    pages = result.get("page_candidates", [])
    if not isinstance(pages, list):
        return result
    replacements: dict[str, dict[str, Any]] = {}
    retained_pages: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = _clean_text(page.get("local_page_id"))
        evidence_segments = {
            _clean_text(item)
            for item in page.get("evidence_segments", [])
            if _clean_text(item)
        } if isinstance(page.get("evidence_segments"), list) else set()
        if page_id:
            evidence_segments |= page_to_segments.get(page_id, set())
        contracts = [segment_contracts[sid] for sid in sorted(evidence_segments) if sid in segment_contracts]
        if not contracts:
            retained_pages.append(page)
            continue
        canonical_keys = sorted({_clean_text(contract.get("canonical_page_key")) for contract in contracts})
        canonical_keys = [key for key in canonical_keys if key]
        if len(canonical_keys) > 1:
            contract_segments_by_key: dict[str, set[str]] = {key: set() for key in canonical_keys}
            for sid in sorted(evidence_segments):
                contract = segment_contracts.get(sid)
                key = _clean_text((contract or {}).get("canonical_page_key"))
                if key in contract_segments_by_key:
                    contract_segments_by_key[key].add(sid)
            known_segments = set().union(*contract_segments_by_key.values()) if contract_segments_by_key else set()
            unknown_segments = sorted(evidence_segments - known_segments)
            if unknown_segments:
                retained = dict(page)
                retained["evidence_segments"] = unknown_segments
                retained["page_identity_contract_split_residue"] = {
                    "source_local_page_id": page_id,
                    "reason": "segments_without_page_identity_contract",
                }
                retained_pages.append(retained)
            for key in canonical_keys:
                segment_ids = sorted(contract_segments_by_key.get(key, set()))
                if not segment_ids:
                    continue
                split_page = dict(page)
                split_page_id = f"{page_id}__{_contract_suffix(key)}" if page_id else f"p_{_contract_suffix(key)}"
                split_page["local_page_id"] = split_page_id
                split_page["evidence_segments"] = segment_ids
                split_page["page_identity_contract"] = _contract_from_rows(contracts, key)
                split_page["page_identity_split"] = {
                    "source_local_page_id": page_id,
                    "split_reason": "mixed_canonical_page_identity_keys",
                    "canonical_page_key": key,
                    "segment_count": len(segment_ids),
                }
                split_page.pop("page_identity_contract_conflict", None)
                retained_pages.append(split_page)
                replacements.update({sid: split_page_id for sid in segment_ids})
            continue
        page["page_identity_contract"] = _contract_from_rows(contracts, canonical_keys[0])
        page.pop("page_identity_contract_conflict", None)
        retained_pages.append(page)
    if retained_pages:
        result["page_candidates"] = retained_pages
    if replacements:
        for row in result.get("segment_assignments", []):
            if not isinstance(row, dict):
                continue
            sid = _clean_text(row.get("segment_id"))
            if sid in replacements:
                row["local_page_id"] = replacements[sid]
        for row in result.get("state_overlays", []):
            if not isinstance(row, dict):
                continue
            sid = _clean_text(row.get("segment_id"))
            if sid in replacements and _clean_text(row.get("underlying_local_page_id")) in page_to_segments:
                row["underlying_local_page_id"] = replacements[sid]
    return result


def validate_phase1_authority_input_report(report: dict[str, Any]) -> dict[str, Any]:
    source_paths = [item for item in report.get("source_paths", []) if str(item or "").strip()]
    producer_chunks = [item for item in report.get("producer_chunks", []) if isinstance(item, dict)]
    expected_segment_count = int((report.get("phase1_authority") or {}).get("segment_count") or 0)
    produced_segment_count = sum(int(item.get("segment_count") or 0) for item in producer_chunks)
    hard_failures: list[str] = []
    if source_paths:
        hard_failures.append("old_gr_chunk_source_path_present")
    if expected_segment_count <= 0:
        hard_failures.append("missing_phase1_authority_segments")
    if expected_segment_count != produced_segment_count:
        hard_failures.append("phase1_segment_coverage_mismatch")
    return {
        "format": "phase1_authority_input_validation_v1",
        "hard_failures": hard_failures,
        "summary": {
            "ok": not hard_failures,
            "old_gr_source_path_count": len(source_paths),
            "phase1_authority_segment_count": expected_segment_count,
            "producer_segment_count": produced_segment_count,
            "producer_chunk_count": len(producer_chunks),
        },
    }


def page_identity_contract_for_page_candidate(page: dict[str, Any]) -> dict[str, Any]:
    contract = page.get("page_identity_contract") if isinstance(page.get("page_identity_contract"), dict) else {}
    if not str((contract or {}).get("canonical_page_key", "") or "").strip():
        return {}
    return dict(contract)


def _report_expected_same_page_surface_segments(report: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for chunk in report.get("producer_chunks", []):
        if not isinstance(chunk, dict):
            continue
        manifest = chunk.get("chunk_authority_manifest")
        if not isinstance(manifest, dict):
            continue
        for item in manifest.get("same_page_surface_segments", []):
            sid = str(item or "").strip()
            if sid:
                out.add(sid)
    return out


def _report_page_identity_manifest_segment_count(report: dict[str, Any]) -> int:
    total = 0
    for chunk in report.get("producer_chunks", []):
        if not isinstance(chunk, dict):
            continue
        manifest = chunk.get("chunk_authority_manifest")
        if not isinstance(manifest, dict):
            continue
        coverage = manifest.get("phase1_authority_coverage")
        if not isinstance(coverage, dict):
            continue
        ratio = coverage.get("page_identity_manifest")
        if not isinstance(ratio, (int, float)):
            continue
        segment_count = int(chunk.get("segment_count") or 0)
        total += int(round(max(0.0, min(1.0, float(ratio))) * max(0, segment_count)))
    return total


def _looks_like_standalone_surface_page(page: dict[str, Any]) -> bool:
    text = " ".join(
        str(page.get(key, "") or "")
        for key in ("local_page_id", "page_kind", "page_name")
    ).lower()
    hard_markers = (
        "filter panel",
        "filter_panel",
        "filter-panel",
        "bottom sheet",
        "bottom_sheet",
        "bottom-sheet",
        "overlay",
        "modal",
        "drawer",
        "popover",
        "dialog",
        "弹窗",
        "浮层",
        "筛选面板",
        "筛选弹层",
        "筛选弹窗",
    )
    page_kind = str(page.get("page_kind", "") or "").strip().lower()
    if page_kind in {"modal", "overlay", "drawer", "panel", "bottom_sheet", "bottom-sheet", "dialog"}:
        return True
    for marker in hard_markers:
        if any("\u4e00" <= ch <= "\u9fff" for ch in marker):
            if marker in text:
                return True
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _compile_coverage_mapping_verdict(report: dict[str, Any]) -> dict[str, Any]:
    api_results = [item for item in report.get("api_results", []) if isinstance(item, dict)]
    api_failure_count = sum(1 for item in api_results if not item.get("ok"))
    coverage_failure_count = 0
    for item in api_results:
        coverage = item.get("coverage")
        if isinstance(coverage, dict) and coverage.get("ok") is False:
            coverage_failure_count += 1
    merge = report.get("hierarchical_merge")
    merge_ok = bool(isinstance(merge, dict) and merge.get("ok") is True)
    mapping = merge.get("mapping_coverage") if isinstance(merge, dict) else {}
    mapping_missing = int((mapping or {}).get("missing_mapping_count") or 0) if isinstance(mapping, dict) else 0
    mapping_extra = int((mapping or {}).get("extra_mapping_count") or 0) if isinstance(mapping, dict) else 0
    return {
        "verified": api_failure_count == 0 and coverage_failure_count == 0 and merge_ok and mapping_missing == 0 and mapping_extra == 0,
        "api_result_count": len(api_results),
        "api_failure_count": api_failure_count,
        "coverage_failure_count": coverage_failure_count,
        "merge_ok": merge_ok,
        "missing_mapping_count": mapping_missing,
        "extra_mapping_count": mapping_extra,
    }


def _compile_producer_output_verdict(report: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    allowed_skip_reasons = {
        "external_system_ui",
        "keyboard_or_ime",
        "notification",
        "permission_sheet",
        "phone_home",
        "status_bar",
        "system_launcher",
        "transient_launch_gate",
    }
    for api_result in report.get("api_results", []):
        if not isinstance(api_result, dict):
            continue
        chunk_id = str(api_result.get("chunk_id", "") or "").strip()
        output = api_result.get("output")
        if not isinstance(output, dict):
            violations.append({"chunk_id": chunk_id, "kind": "missing_output_object"})
            continue
        pages = output.get("page_candidates", [])
        if not isinstance(pages, list):
            pages = []
        page_ids = [
            str(page.get("local_page_id", "") or "").strip()
            for page in pages
            if isinstance(page, dict) and str(page.get("local_page_id", "") or "").strip()
        ]
        for pid in sorted({pid for pid in page_ids if page_ids.count(pid) > 1}):
            violations.append({"chunk_id": chunk_id, "kind": "duplicate_local_page_id", "local_page_id": pid})
        known_pages = set(page_ids)
        seen_segments: dict[str, str] = {}
        for collection in ("segment_assignments", "skip_segments"):
            rows = output.get(collection, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("segment_id", "") or "").strip()
                if not sid:
                    continue
                if sid in seen_segments:
                    violations.append(
                        {
                            "chunk_id": chunk_id,
                            "kind": "duplicate_segment_output",
                            "segment_id": sid,
                            "first_collection": seen_segments[sid],
                            "second_collection": collection,
                        }
                    )
                seen_segments[sid] = collection
                if collection == "segment_assignments":
                    pid = str(row.get("local_page_id", "") or "").strip()
                    if pid and pid not in known_pages:
                        violations.append(
                            {
                                "chunk_id": chunk_id,
                                "kind": "assignment_to_unknown_page",
                                "segment_id": sid,
                                "local_page_id": pid,
                            }
                        )
                else:
                    reason = str(row.get("reason", "") or row.get("skip_reason", "") or "").strip()
                    if reason and reason not in allowed_skip_reasons:
                        violations.append(
                            {
                                "chunk_id": chunk_id,
                                "kind": "unsupported_skip_reason",
                                "segment_id": sid,
                                "reason": reason,
                            }
                        )
    return {"verified": not violations, "violation_count": len(violations), "violations": violations[:20]}


def _compile_same_page_surface_verdict(report: dict[str, Any]) -> dict[str, Any]:
    expected_segments = _report_expected_same_page_surface_segments(report)
    violations: list[dict[str, Any]] = []
    checked = 0
    observed: set[str] = set()
    for api_result in report.get("api_results", []):
        if not isinstance(api_result, dict):
            continue
        chunk_id = str(api_result.get("chunk_id", "") or "").strip()
        output = api_result.get("output")
        if not isinstance(output, dict):
            continue
        pages = {
            str(page.get("local_page_id", "") or "").strip(): page
            for page in output.get("page_candidates", [])
            if isinstance(page, dict) and str(page.get("local_page_id", "") or "").strip()
        }
        assignments = {
            str(row.get("segment_id", "") or "").strip(): str(row.get("local_page_id", "") or "").strip()
            for row in output.get("segment_assignments", [])
            if isinstance(row, dict) and str(row.get("segment_id", "") or "").strip()
        }
        overlay_segments = {
            str(row.get("segment_id", "") or "").strip()
            for row in output.get("state_overlays", [])
            if isinstance(row, dict) and str(row.get("segment_id", "") or "").strip()
        }
        chunk_expected = sorted(sid for sid in expected_segments if sid in assignments or sid in overlay_segments)
        for sid in chunk_expected:
            checked += 1
            observed.add(sid)
            if sid in overlay_segments:
                continue
            local_page_id = assignments.get(sid, "")
            page = pages.get(local_page_id, {})
            if not page:
                violations.append(
                    {
                        "chunk_id": chunk_id,
                        "segment_id": sid,
                        "kind": "same_page_surface_assignment_missing_page",
                        "local_page_id": local_page_id,
                    }
                )
            elif _looks_like_standalone_surface_page(page):
                violations.append(
                    {
                        "chunk_id": chunk_id,
                        "segment_id": sid,
                        "kind": "same_page_surface_became_standalone_page",
                        "local_page_id": local_page_id,
                        "page_name": page.get("page_name", ""),
                        "page_kind": page.get("page_kind", ""),
                    }
                )
    for sid in sorted(sid for sid in expected_segments if sid not in observed)[:20]:
        violations.append({"segment_id": sid, "kind": "same_page_surface_segment_not_observed_in_output"})
    return {
        "verified": not violations,
        "expected_segment_count": len(expected_segments),
        "checked_segment_count": checked,
        "violation_count": len(violations),
        "violations": violations[:20],
    }


def _compile_merge_auditability_verdict(report: dict[str, Any]) -> dict[str, Any]:
    merge = report.get("hierarchical_merge")
    missing_batches: list[dict[str, Any]] = []
    checked = 0
    if isinstance(merge, dict):
        for round_index, round_item in enumerate(merge.get("rounds", [])):
            if not isinstance(round_item, dict):
                continue
            for batch_index, batch in enumerate(round_item.get("batches", [])):
                if not isinstance(batch, dict) or batch.get("pass_through"):
                    continue
                checked += 1
                has_raw = isinstance(batch.get("raw_merge_result"), dict)
                has_guarded = isinstance(batch.get("merge_result"), dict)
                if not has_raw or not has_guarded:
                    missing_batches.append(
                        {
                            "round_index": round_index,
                            "batch_index": batch_index,
                            "stage_suffix": batch.get("stage_suffix", ""),
                            "has_raw_merge_result": has_raw,
                            "has_guarded_merge_result": has_guarded,
                        }
                    )
    return {
        "verified": not missing_batches,
        "checked_batch_count": checked,
        "missing_raw_output_count": len(missing_batches),
        "missing_batches": missing_batches[:20],
    }


def _compile_strong_page_identity_verdict(report: dict[str, Any]) -> dict[str, Any]:
    manifest_segment_count = _report_page_identity_manifest_segment_count(report)
    page_contract_count = 0
    unsupported_contracts: list[dict[str, Any]] = []
    for api_result in report.get("api_results", []):
        if not isinstance(api_result, dict):
            continue
        output = api_result.get("output")
        if not isinstance(output, dict):
            continue
        for page in output.get("page_candidates", []):
            if not isinstance(page, dict):
                continue
            contract = page_identity_contract_for_page_candidate(page)
            if not contract:
                continue
            page_contract_count += 1
            if manifest_segment_count <= 0:
                unsupported_contracts.append(
                    {
                        "chunk_id": api_result.get("chunk_id", ""),
                        "local_page_id": page.get("local_page_id", ""),
                        "canonical_page_key": contract.get("canonical_page_key", ""),
                    }
                )
    blockers: list[str] = []
    if manifest_segment_count <= 0:
        blockers.append("missing_page_identity_authority")
    if unsupported_contracts:
        blockers.append("unsupported_page_identity_contract_without_manifest")
    if manifest_segment_count > 0 and page_contract_count <= 0:
        blockers.append("missing_page_identity_contract_output")
    return {
        "verified": not blockers,
        "authority_level": "strong_page_identity_contract" if manifest_segment_count > 0 else "weak_visual_only",
        "manifest_segment_count": manifest_segment_count,
        "page_contract_count": page_contract_count,
        "unsupported_contract_count": len(unsupported_contracts),
        "unsupported_contract_examples": unsupported_contracts[:20],
        "blockers": blockers,
    }


def compile_authority_verdict(report: dict[str, Any]) -> dict[str, Any]:
    phase1_input = validate_phase1_authority_input_report(report)
    coverage_mapping = _compile_coverage_mapping_verdict(report)
    producer_output = _compile_producer_output_verdict(report)
    same_page_surface = _compile_same_page_surface_verdict(report)
    merge_auditability = _compile_merge_auditability_verdict(report)
    strong_page_identity = _compile_strong_page_identity_verdict(report)
    blocked_reason = ""
    if not phase1_input["summary"]["ok"]:
        blocked_reason = phase1_input["hard_failures"][0]
    elif not coverage_mapping["verified"]:
        blocked_reason = "coverage_mapping_not_verified"
    elif not producer_output["verified"]:
        blocked_reason = "producer_output_not_verified"
    elif not same_page_surface["verified"]:
        blocked_reason = "same_page_surface_not_verified"
    elif not merge_auditability["verified"]:
        blocked_reason = "merge_auditability_not_verified"
    elif not strong_page_identity["verified"]:
        blocked_reason = strong_page_identity["blockers"][0] if strong_page_identity["blockers"] else "strong_page_identity_not_verified"
    summary = {
        "phase1_authority_input_verified": bool(phase1_input["summary"]["ok"]),
        "coverage_mapping_verified": bool(coverage_mapping["verified"]),
        "producer_output_verified": bool(producer_output["verified"]),
        "same_page_surface_verified": bool(same_page_surface["verified"]),
        "merge_auditability_verified": bool(merge_auditability["verified"]),
        "strong_page_identity_verified": bool(strong_page_identity["verified"]),
        "blocked_reason": blocked_reason,
    }
    summary["overall_status"] = "pass" if not blocked_reason else "blocked"
    return {
        "format": "compact_gr_authority_verdict_v1",
        "phase1_authority_input": phase1_input,
        "coverage_mapping": coverage_mapping,
        "producer_output": producer_output,
        "same_page_surface": same_page_surface,
        "merge_auditability": merge_auditability,
        "strong_page_identity": strong_page_identity,
        "summary": summary,
    }
