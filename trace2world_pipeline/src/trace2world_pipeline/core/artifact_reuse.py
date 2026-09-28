from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from trace2world_pipeline.core.step_keys import canonical_step_key


FALLBACK_BINDING_SOURCES = {
    "l0_execution_anchor_route_fallback",
    "route_label_fallback",
    "filename_label_fallback",
    "expected_filename_fallback",
    "global_navigation_fallback",
}


@dataclass
class ArtifactReuseDecision:
    artifact_id: str
    artifact_path: str
    reused: bool
    reason: str
    input_artifacts: List[str] = field(default_factory=list)
    validators: List[str] = field(default_factory=list)
    blocking_issues: List[Dict[str, Any]] = field(default_factory=list)
    schema_revision: int = 1

    def to_json(self) -> Dict[str, Any]:
        return {
            "format": "artifact_reuse_decision",
            "schema_revision": self.schema_revision,
            "artifact_id": self.artifact_id,
            "artifact_path": self.artifact_path,
            "reused": self.reused,
            "reason": self.reason,
            "input_artifacts": list(self.input_artifacts),
            "validators": list(self.validators),
            "blocking_issue_count": len(self.blocking_issues),
            "blocking_issues": list(self.blocking_issues),
        }


def validate_no_fallback_binding_source(payload: Any) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                next_path = f"{path}.{key}" if path else f"$.{key}"
                if key in {"binding_source", "binding_sources"}:
                    values = item if isinstance(item, list) else [item]
                    for raw in values:
                        if str(raw) in FALLBACK_BINDING_SOURCES:
                            issues.append(
                                {
                                    "code": "formal_fallback_binding_source",
                                    "path": next_path,
                                    "value": str(raw),
                                }
                            )
                walk(item, next_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "$")
    return issues


def validate_step_keyed_mapping_is_canonical(
    payload: Dict[str, Any],
    mapping_keys: Iterable[str],
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for mapping_key in mapping_keys:
        mapping = payload.get(mapping_key)
        if not isinstance(mapping, dict):
            continue
        for raw_key in mapping:
            raw = str(raw_key)
            canonical = canonical_step_key(raw)
            if raw and canonical and raw != canonical:
                issues.append(
                    {
                        "code": "noncanonical_step_key",
                        "mapping_key": mapping_key,
                        "raw_key": raw,
                        "canonical_key": canonical,
                    }
                )
    return issues
