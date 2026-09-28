
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Mapping, Set


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def compile_global_runtime_transactions(
    page_environment_specs: Mapping[str, Any],
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))

    transactions: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    written_refs_by_page: Dict[str, Set[str]] = defaultdict(set)
    read_refs_by_page: Dict[str, Set[str]] = defaultdict(set)

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        transitions = _as_list(spec.get("transitions"))

        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            source = _text(transition.get("source_filename"))
            target = _text(transition.get("target_filename"))
            if not source or not target or source == target:
                continue

            state_effect = _as_dict(transition.get("state_effect"))
            diff = _as_dict(
                state_effect.get("expected_state_diff")
                or transition.get("expected_state_diff")
            )
            state_writes: List[str] = []
            for key in diff:
                ref = _text(key).split(".")[0] if "." in _text(key) else _text(key)
                if ref:
                    state_writes.append(ref)
                    written_refs_by_page[source].add(ref)

            target_ref = _text(state_effect.get("target_state_ref") or state_effect.get("state_ref"))
            if target_ref:
                state_writes.append(target_ref)
                written_refs_by_page[source].add(target_ref)

            if not state_writes:
                continue

            action_id = _text(transition.get("step_key") or transition.get("action_id"))
            reducer = _text(transition.get("reducer") or transition.get("action_intent"))

            target_spec = _as_dict(specs_by_filename.get(target))
            target_reads: List[str] = []
            if target_spec:
                target_rps = _as_list(target_spec.get("render_projections"))
                target_initial = _as_dict(target_spec.get("initial_state"))
                target_state_schema = _as_dict(target_spec.get("state_schema"))
                target_runtime_refs = _as_list(target_state_schema.get("runtime_state_refs"))

                for ref in state_writes:
                    if ref in str(target_rps) or ref in str(target_initial) or ref in target_runtime_refs:
                        target_reads.append(ref)
                        read_refs_by_page[target].add(ref)

            source_spec = _as_dict(specs_by_filename.get(source))
            source_state_schema = _as_dict(source_spec.get("state_schema"))

            transaction = {
                "transaction_id": f"{action_id}::cross_page",
                "source_action_id": action_id,
                "source_page": source,
                "target_page": target,
                "reducer": reducer,
                "state_store_schema": {
                    ref: _as_dict(source_state_schema.get(ref))
                    for ref in state_writes
                    if source_state_schema.get(ref)
                },
                "write_set": sorted(set(state_writes)),
                "read_set": sorted(set(target_reads)),
                "state_writes": sorted(set(state_writes)),
                "target_reads": sorted(set(target_reads)),
                "expected_state_diff": diff,
                "write_read_closed": len(target_reads) > 0,
            }
            transactions.append(transaction)

            if not state_writes:
                issues.append({
                    "code": "global_runtime_transaction_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "source_page": source,
                    "target_page": target,
                    "message": f"cross-page action {action_id} from {source} to {target} has no state writes",
                })
            elif not target_reads:
                issues.append({
                    "code": "global_runtime_transaction_read_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "source_page": source,
                    "target_page": target,
                    "state_writes": sorted(set(state_writes)),
                    "message": (
                        f"cross-page transaction {action_id} writes {state_writes} "
                        f"but target {target} does not read any of them"
                    ),
                })

    return {
        "format": "global_runtime_transactions",
        "schema_revision": 1,
        "transactions": transactions,
        "issues": issues,
        "summary": {
            "transaction_count": len(transactions),
            "closed_count": sum(1 for t in transactions if t.get("write_read_closed")),
            "unclosed_count": sum(1 for t in transactions if not t.get("write_read_closed")),
            "issue_count": len(issues),
        },
    }
