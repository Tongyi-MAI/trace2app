
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List


def _base_filename(value: Any) -> str:
    return str(value or "").split("?", 1)[0].split("#", 1)[0].strip()


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_")[:160] or "requirement"


def _dedupe(values: Iterable[str], limit: int = 64) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _field_bindings(fields: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    bindings: Dict[str, List[str]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        role = str(field.get("field_role") or field.get("name") or "form_value").strip()
        if not role:
            continue
        aliases = [role]
        if role == "form_value":
            aliases.extend(["name", "title_name"])
        if "company" in role and ("title" in role or "name" in role):
            aliases.extend(["company_name", "title_name", "name"])
        if "tax" in role:
            aliases.extend(["tax_id", "taxpayer_id", "tax_no"])
        if "address" in role:
            aliases.append("company_address")
        if "phone" in role:
            aliases.append("company_phone")
        if "bank" in role and "account" in role:
            aliases.append("bank_account")
        if "bank" in role and ("name" in role or role == "bank"):
            aliases.append("bank_name")
        bindings[role] = _dedupe(aliases, limit=8)
    return bindings


def _collection_from_page_env(page_env: Dict[str, Any]) -> str:
    for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
        if not isinstance(projection, dict):
            continue
        if str(projection.get("layout_policy") or "") not in {"appendable_list", "collection_template_from_observed_component"}:
            continue
        reads = str(projection.get("reads_state") or projection.get("state_ref") or "").strip()
        if reads:
            return reads.replace("state::", "")
    for schema in page_env.get("state_schema", {}).get("collections", []) if isinstance(page_env.get("state_schema"), dict) else []:
        if isinstance(schema, dict) and schema.get("state_scope"):
            return str(schema.get("state_scope") or "")
    return ""


def _entity_domain_from_collection(collection: str, draft_ref: str) -> str:
    seed = collection or draft_ref
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", seed) if part]
    if not parts:
        return "Record"
    return "".join(part[:1].upper() + part[1:] for part in parts[-2:])[:64]


def build_interface_requirement_projection(page_environment_specs: Dict[str, Any]) -> Dict[str, Any]:
    page_envs = (
        page_environment_specs.get("page_environment_specs_by_filename", {})
        if isinstance(page_environment_specs, dict)
        else {}
    )
    requirements_by_id: Dict[str, Dict[str, Any]] = {}
    requirements_by_page: Dict[str, List[str]] = defaultdict(list)
    if not isinstance(page_envs, dict):
        page_envs = {}

    for source_filename, source_env in page_envs.items():
        if not isinstance(source_env, dict):
            continue
        for transition in source_env.get("transitions", []) if isinstance(source_env.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            if str(transition.get("direction") or "") != "source":
                continue
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            if str(effect.get("effect") or "") != "commit_draft":
                continue
            fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
            target_filename = _base_filename(transition.get("target_filename"))
            target_env = page_envs.get(target_filename, {}) if target_filename else {}
            collection = (
                str(effect.get("collection_state_ref") or effect.get("collection_ref") or "").replace("state::", "")
                or (_collection_from_page_env(target_env) if isinstance(target_env, dict) else "")
            )
            draft_ref = str(effect.get("draft_ref") or "")
            requirement_id = "req." + _safe_id(
                f"{source_filename}:{transition.get('transition_id') or transition.get('step_key')}:commit_draft"
            )
            requirement = {
                "requirement_id": requirement_id,
                "source_page": _base_filename(source_filename),
                "target_page": target_filename,
                "capability_family": "record_commit",
                "reducer": "submit_form",
                "state_domain": _entity_domain_from_collection(collection, draft_ref),
                "required_effect": "commit_draft",
                "draft_state_ref": draft_ref,
                "required_parameters": ["form_value"],
                "field_bindings": _field_bindings(fields),
                "commit_result": {
                    "collection": collection,
                    "policy": str(effect.get("collection_merge_policy") or "preserve_existing_then_append_or_upsert"),
                },
                "display_binding": {
                    "target_page": target_filename,
                    "reads": collection,
                    "mutation_policy": "preserve_existing_then_append_or_upsert",
                },
                "oracles": [
                    "new_record_visible_after_commit",
                    "existing_records_preserved",
                ],
                "l0_step_refs": _dedupe([str(transition.get("step_key") or transition.get("transition_id") or "")], limit=4),
            }
            requirements_by_id[requirement_id] = requirement
            requirements_by_page[_base_filename(source_filename)].append(requirement_id)
            if target_filename:
                requirements_by_page[target_filename].append(requirement_id)

    return {
        "format": "interface_requirement_projection",
        "schema_revision": 1,
        "source": "semantic_api/06_page_environment_specs/page_environment_specs.json",
        "artifact_metadata": {
            "artifact_id": "semantic_api.interface_requirement_projection",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "interface_requirement_projection",
            "semantic_role": "spec_to_interface_requirements",
            "producer": "site_model.interface_requirement_projection.build_interface_requirement_projection",
            "allowed_consumers": ["interface_design", "runtime_backend", "pagegen", "validator"],
        },
        "requirements_by_id": requirements_by_id,
        "requirements_by_page": dict(requirements_by_page),
        "summary": {
            "requirement_count": len(requirements_by_id),
            "page_count": len(requirements_by_page),
        },
    }


def evaluate_interface_requirement_projection(projection: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    requirements = projection.get("requirements_by_id", {}) if isinstance(projection, dict) else {}
    for requirement_id, requirement in requirements.items() if isinstance(requirements, dict) else []:
        if not isinstance(requirement, dict):
            continue
        commit = requirement.get("commit_result") if isinstance(requirement.get("commit_result"), dict) else {}
        display = requirement.get("display_binding") if isinstance(requirement.get("display_binding"), dict) else {}
        if not commit.get("collection"):
            findings.append({
                "severity": "warning",
                "code": "commit_collection_unresolved",
                "requirement_id": requirement_id,
            })
        if not display.get("reads"):
            findings.append({
                "severity": "warning",
                "code": "display_binding_unresolved",
                "requirement_id": requirement_id,
            })
    blocking = [item for item in findings if item.get("severity") == "error"]
    warnings = [item for item in findings if item.get("severity") == "warning"]
    return {
        "format": "interface_requirement_projection_evaluation",
        "summary": {
            "requirement_count": len(requirements) if isinstance(requirements, dict) else 0,
            "blocking_error_count": len(blocking),
            "warning_count": len(warnings),
        },
        "findings": findings,
    }
