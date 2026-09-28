
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


def _base_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split("?", 1)[0].split("#", 1)[0].split("/")[-1]


def _build_page_registry(pages: list) -> tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]], Dict[str, list[Dict[str, Any]]]]:
    by_materialized: Dict[str, Dict[str, Any]] = {}
    by_general_instance: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_general: Dict[str, list[Dict[str, Any]]] = {}
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            continue
        filename = _base_filename(page.get("filename"))
        general_id = str(page.get("general_page_id") or "").strip()
        materialized_id = str(page.get("materialized_page_id") or "").strip()
        instance_key = str(page.get("instance_key") or "").strip()
        if not filename or not general_id:
            continue
        normalized = {
            "filename": filename,
            "page_id": str(page.get("page_id") or ""),
            "page_name": str(page.get("page_name") or ""),
            "page_type": str(page.get("page_type") or ""),
            "general_page_id": general_id,
            "materialized_page_id": materialized_id or str(page.get("page_id") or ""),
            "instance_key": instance_key,
            "route_context": page.get("route_context") if isinstance(page.get("route_context"), dict) else {},
            "query_state_contract": page.get("query_state_contract") if isinstance(page.get("query_state_contract"), dict) else {},
            "input_surface_contract": page.get("input_surface_contract") if isinstance(page.get("input_surface_contract"), dict) else {},
        }
        by_general.setdefault(general_id, []).append(normalized)
        if materialized_id:
            by_materialized[materialized_id] = normalized
        if instance_key:
            by_general_instance[(general_id, instance_key)] = normalized
        if materialized_id and "::" in materialized_id:
            by_general_instance[(general_id, materialized_id.split("::", 1)[1])] = normalized
        elif materialized_id and not instance_key:
            by_general_instance[(general_id, "")] = normalized
    return by_materialized, by_general_instance, by_general


def _is_query_entry_page(page: Dict[str, Any]) -> bool:
    instance_key = str(page.get("instance_key") or "").strip()
    materialized_id = str(page.get("materialized_page_id") or "").strip()
    page_type = str(page.get("page_type") or "").strip()
    page_name = str(page.get("page_name") or "").strip().lower()
    route_context = page.get("route_context") if isinstance(page.get("route_context"), dict) else {}
    route_path = str(route_context.get("route_path") or "").strip().rstrip("/")
    lowered_key = instance_key.lower()
    lowered_materialized = materialized_id.lower()

    def has_submitted_token(text: str) -> bool:
        normalized = text.replace("-", "_").replace("/", "_")
        tokens = [token for token in normalized.split("_") if token]
        return "submitted" in tokens or "submittedquery" in tokens

    if has_submitted_token(lowered_key) or has_submitted_token(lowered_materialized) or "已提交" in page_name:
        return False
    return (
        page_type == "search_entry"
        or instance_key in {"entry", "search_entry", "blank", "empty_query"}
        or "unsubmitted" in lowered_key
        or "input_discovery" in lowered_key
        or "search_entry" in lowered_key
        or materialized_id.endswith("::entry")
        or "未提交" in page_name
        or "历史/发现/联想" in page_name
        or route_path.endswith("/search")
    )


def _has_committed_submit_target(page: Dict[str, Any]) -> bool:
    query_contract = page.get("query_state_contract") if isinstance(page.get("query_state_contract"), dict) else {}
    for state in query_contract.get("states", []) if isinstance(query_contract.get("states"), list) else []:
        if not isinstance(state, dict):
            continue
        targets = state.get("submit_targets", [])
        if isinstance(targets, list) and targets:
            return True
    return False


def _is_query_entry_runtime_projection(page: Dict[str, Any]) -> bool:
    if _is_query_entry_page(page):
        return False
    input_contract = page.get("input_surface_contract") if isinstance(page.get("input_surface_contract"), dict) else {}
    preview = input_contract.get("preview") if isinstance(input_contract.get("preview"), dict) else {}
    states = input_contract.get("states", []) if isinstance(input_contract.get("states"), list) else []
    if _has_committed_submit_target(page):
        return False
    return (
        str(input_contract.get("surface_intent") or "") == "search"
        and preview.get("observed") is True
        and str(preview.get("render_scope") or "") == "same_page_surface"
        and "preview_visible" in {str(state) for state in states}
    )


def _is_submitted_query_group(group: Dict[str, Any]) -> bool:
    split_dimension = str(group.get("split_dimension") or "").strip().lower()
    if split_dimension == "submitted_query":
        return True
    split_reason = group.get("split_reason", {})
    if isinstance(split_reason, dict):
        return str(split_reason.get("dimension") or "").strip().lower() == "submitted_query"
    return False


def _submitted_query_state_params(group: Dict[str, Any]) -> Dict[str, str]:
    source = str(group.get("instance_label") or group.get("instance_key") or "").strip()
    params = {
        "lifecycle": "submitted",
        "split_dimension": "submitted_query",
    }
    if source:
        params["submitted_query_source"] = source
    source_key = str(group.get("instance_key") or "").strip()
    if source_key:
        params["source_instance_key"] = source_key
    return params


def _query_entry_pages_by_general(by_general: Dict[str, list[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for general_id, pages in by_general.items():
        entries = [page for page in pages if _is_query_entry_page(page)]
        if not entries:
            continue
        result[general_id] = sorted(
            entries,
            key=lambda page: (
                0 if str(page.get("page_type") or "").strip() == "search_entry" else 1,
                0 if str(page.get("instance_key") or "").strip() in {"entry", "search_entry", "blank", "empty_query"} else 1,
                len([part for part in str(page.get("route_context", {}).get("route_path", "")).split("/") if part]),
                _base_filename(page.get("filename")),
            ),
        )[0]
    return result


def _bind(
    bindings: Dict[str, Dict[str, Any]],
    conflicts: list[Dict[str, Any]],
    *,
    obs_key: str,
    page: Dict[str, Any],
    source: str,
    confidence: str,
    source_general_page_id: str = "",
    source_instance_key: str = "",
    state_params: Dict[str, Any] | None = None,
) -> None:
    obs_key = str(obs_key or "").strip()
    filename = _base_filename(page.get("filename"))
    if not obs_key or not filename:
        return
    binding = {
        "obs_key": obs_key,
        "filename": filename,
        "page_id": str(page.get("page_id") or ""),
        "page_name": str(page.get("page_name") or ""),
        "general_page_id": str(page.get("general_page_id") or source_general_page_id or ""),
        "materialized_page_id": str(page.get("materialized_page_id") or ""),
        "instance_key": str(page.get("instance_key") or source_instance_key or ""),
        "source": source,
        "confidence": confidence,
    }


    boundary = (
        state_params.get("page_boundary_authority")
        if isinstance(state_params, dict)
        and isinstance(state_params.get("page_boundary_authority"), dict)
        else {}
    )
    if boundary:
        binding["page_boundary_authority"] = dict(boundary)
        for key in (
            "boundary_decision",
            "canonical_runtime_filename",
            "authority_ref",
        ):
            if boundary.get(key) not in (None, "", [], {}):
                binding[key] = boundary[key]
    if isinstance(state_params, dict) and state_params:
        binding["state_params"] = dict(state_params)
    previous = bindings.get(obs_key)
    if previous and previous.get("filename") != binding["filename"]:
        conflicts.append({
            "obs_key": obs_key,
            "previous": previous,
            "candidate": binding,
            "conflict_type": "obs_binding_conflict",
        })
        return
    bindings[obs_key] = binding


def build_materialized_observation_bindings(
    site_model: Any,
) -> Dict[str, Any]:
    if isinstance(site_model, (str, Path)):
        return build_materialized_observation_bindings_from_disk(Path(site_model))

    contracts = site_model.page_execution_contracts
    _by_materialized, by_general_instance, by_general = _build_page_registry(
        contracts.get("pages", []) if isinstance(contracts, dict) else []
    )
    query_entry_by_general = _query_entry_pages_by_general(by_general)
    instance_plan = site_model.instance_plan
    cluster_assignments = site_model.cluster_assignments
    bindings: Dict[str, Dict[str, Any]] = {}
    conflicts: list[Dict[str, Any]] = []
    missing_instance_pages: list[Dict[str, str]] = []
    runtime_projection_rewrites: list[Dict[str, str]] = []
    state_parameterized_binding_count = 0

    for general_id, plan in instance_plan.items() if isinstance(instance_plan, dict) else []:
        if not isinstance(plan, dict):
            continue
        plan_state_params_by_obs_key = (
            plan.get("state_params_by_obs_key", {})
            if isinstance(plan.get("state_params_by_obs_key"), dict)
            else {}
        )
        groups = plan.get("instance_groups", [])
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            instance_key = str(group.get("instance_key") or "").strip()
            page = by_general_instance.get((str(general_id), instance_key))
            if not page:
                missing_instance_pages.append({
                    "general_page_id": str(general_id),
                    "instance_key": instance_key,
                    "source": "instance_plan",
                })
                continue
            target_page = page
            entry_page = query_entry_by_general.get(str(general_id))
            group_state_params_by_obs_key = (
                group.get("state_params_by_obs_key", {})
                if isinstance(group.get("state_params_by_obs_key"), dict)
                else {}
            )
            source = "instance_plan"
            confidence = "materialized_instance"
            default_state_params: Dict[str, Any] = {}
            if entry_page and _is_query_entry_runtime_projection(page):
                target_page = entry_page
                source = "query_entry_runtime_projection"
                confidence = "runtime_state_of_query_entry"
            elif entry_page and _is_submitted_query_group(group):
                target_page = entry_page
                source = "submitted_query_parameterized_state"
                confidence = "runtime_state_of_query_entry"
                default_state_params = _submitted_query_state_params(group)
            for obs_key in group.get("obs_keys", []) if isinstance(group.get("obs_keys"), list) else []:
                if target_page is not page:
                    runtime_projection_rewrites.append({
                        "obs_key": str(obs_key),
                        "general_page_id": str(general_id),
                        "instance_key": instance_key,
                        "from_filename": _base_filename(page.get("filename")),
                        "to_filename": _base_filename(target_page.get("filename")),
                        "reason": source,
                    })
                state_params = (
                    group_state_params_by_obs_key.get(str(obs_key))
                    if isinstance(group_state_params_by_obs_key.get(str(obs_key)), dict)
                    else (
                        plan_state_params_by_obs_key.get(str(obs_key))
                        if isinstance(plan_state_params_by_obs_key.get(str(obs_key)), dict)
                        else default_state_params
                    )
                )
                if isinstance(state_params, dict) and state_params:
                    state_parameterized_binding_count += 1
                _bind(
                    bindings,
                    conflicts,
                    obs_key=str(obs_key),
                    page=target_page,
                    source=source,
                    confidence=confidence,
                    source_general_page_id=str(general_id),
                    source_instance_key=instance_key,
                    state_params=state_params,
                )

    obs_to_cluster = cluster_assignments.get("obs_to_cluster", {}) if isinstance(cluster_assignments, dict) else {}
    for obs_key, general_id in obs_to_cluster.items() if isinstance(obs_to_cluster, dict) else []:
        obs_key = str(obs_key or "").strip()
        if not obs_key or obs_key in bindings:
            continue
        pages = by_general.get(str(general_id), [])
        if len(pages) != 1:
            continue
        plan = (
            instance_plan.get(str(general_id), {})
            if isinstance(instance_plan, dict)
            else {}
        )
        plan_state_params_by_obs_key = (
            plan.get("state_params_by_obs_key", {})
            if isinstance(plan, dict)
            and isinstance(plan.get("state_params_by_obs_key"), dict)
            else {}
        )
        state_params = (
            plan_state_params_by_obs_key.get(obs_key)
            if isinstance(plan_state_params_by_obs_key.get(obs_key), dict)
            else {}
        )
        if state_params:
            state_parameterized_binding_count += 1
        _bind(
            bindings,
            conflicts,
            obs_key=obs_key,
            page=pages[0],
            source=(
                "cluster_runtime_projection"
                if state_params
                else "cluster_assignment_single_materialized_page"
            ),
            confidence=(
                "runtime_state_of_single_materialized_page"
                if state_params
                else "single_materialized_page_for_general_id"
            ),
            source_general_page_id=str(general_id),
            state_params=state_params,
        )

    return {
        "format": "materialized_observation_bindings",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "data.site_model.materialized_observation_bindings",
            "artifact_kind": "det_materialization_registry",
            "api_or_det": "det",
            "canonical_source": True,
            "canonical_semantic_source": True,
            "canonical_prompt_boundary": False,
            "artifact_role": "authority",
            "logical_owner": "materialization",
            "semantic_role": "canonical_obs_to_materialized_page_binding",
            "producer": "site_model.materialized_observation_bindings.build_materialized_observation_bindings",
            "allowed_consumers": [
                "l0_execution",
                "page_execution_contracts",
                "page_environment_compiler",
                "validator",
                "repair",
            ],
            "debug_only": False,
            "source_selection": {
                "derived_from": [
                    "site_model.instance_plan",
                    "site_model.cluster_assignments",
                    "site_model.page_execution_contracts",
                ],
            },
        },
        "bindings_by_obs_key": dict(sorted(bindings.items())),
        "summary": {
            "binding_count": len(bindings),
            "conflict_count": len(conflicts),
            "missing_instance_page_count": len(missing_instance_pages),
            "runtime_projection_rewrite_count": len(runtime_projection_rewrites),
            "state_parameterized_binding_count": state_parameterized_binding_count,
        },
        "conflicts": conflicts,
        "missing_instance_pages": missing_instance_pages,
        "runtime_projection_rewrites": runtime_projection_rewrites,
    }


def write_materialized_observation_bindings(
    site_model: Any,
    output_path: Path | None = None,
) -> Path:
    if output_path is None:
        if not isinstance(site_model, (str, Path)):
            raise TypeError("output_path is required when site_model is not a result directory")
        result_dir = Path(site_model)
        output_path = result_dir / "data" / "site_model" / "materialized_observation_bindings.json"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_materialized_observation_bindings(site_model)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def load_obs_to_filename_from_materialized_bindings(site_model: Any) -> Dict[str, str]:
    if isinstance(site_model, (str, Path)):
        return load_obs_to_filename_from_materialized_bindings_from_disk(Path(site_model))

    bindings_payload = site_model.materialized_observation_bindings
    bindings = bindings_payload.get("bindings_by_obs_key", {}) if isinstance(bindings_payload, dict) else {}
    if not isinstance(bindings, dict):
        return {}
    return {
        str(obs_key): _base_filename(item.get("filename"))
        for obs_key, item in bindings.items()
        if isinstance(item, dict) and _base_filename(item.get("filename"))
    }


def _read_json_from_disk(path: Path) -> Any:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_first_json_from_disk(paths: Iterable[Path]) -> Any:
    for path in paths:
        payload = _read_json_from_disk(path)
        if payload:
            return payload
    return {}


def build_materialized_observation_bindings_from_disk(
    result_dir: Path,
    *,
    page_execution_contracts: Dict[str, Any] | None = None,
    instance_plan: Dict[str, Any] | None = None,
    cluster_assignments: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    from trace2world_pipeline.core.phase_output import PhaseOutputManager

    result_dir = Path(result_dir)
    pom = PhaseOutputManager(result_dir)
    if page_execution_contracts is None:
        page_execution_contracts = _read_first_json_from_disk([
            pom.formal("consolidation", "site_model", "page_execution_contracts.json"),
            result_dir / "data" / "site_model" / "page_execution_contracts.json",
        ])
    if instance_plan is None:
        instance_plan = _read_first_json_from_disk([
            pom.formal("instance_splitting", "instance_plan.json"),
            result_dir / "data" / "debug" / "instance_plan.json",
        ])
    if cluster_assignments is None:
        cluster_assignments = _read_first_json_from_disk([
            pom.debug("global_reasoning", "cluster_assignments.json"),
            result_dir / "data" / "debug" / "cluster_assignments.json",
        ])

    class _AppSpecProxy:
        pass

    proxy = _AppSpecProxy()
    proxy.page_execution_contracts = page_execution_contracts
    proxy.instance_plan = instance_plan
    proxy.cluster_assignments = cluster_assignments
    return build_materialized_observation_bindings(proxy)


def load_obs_to_filename_from_materialized_bindings_from_disk(result_dir: Path) -> Dict[str, str]:
    from trace2world_pipeline.core.phase_output import PhaseOutputManager

    pom = PhaseOutputManager(Path(result_dir))
    path = pom.formal("consolidation", "site_model", "materialized_observation_bindings.json")
    payload = _read_first_json_from_disk([
        path,
        Path(result_dir) / "data" / "site_model" / "materialized_observation_bindings.json",
    ])
    bindings = payload.get("bindings_by_obs_key", {}) if isinstance(payload, dict) else {}
    if not isinstance(bindings, dict):
        return {}
    return {
        str(obs_key): _base_filename(item.get("filename"))
        for obs_key, item in bindings.items()
        if isinstance(item, dict) and _base_filename(item.get("filename"))
    }
