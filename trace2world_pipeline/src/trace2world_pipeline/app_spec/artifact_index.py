
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


API_OR_DET_VALUES = {"api", "det", "mixed"}
ARTIFACT_KIND_VALUES = {
    "api_semantic_source",
    "det_registry",
    "det_projection",
    "validation_report",
    "final_consumer_artifact",
    "debug_export",
}

LEGACY_ROOT_DIRS = {"data", "reports", "frontend", "backend", "sprites", "images"}
ROOT_FLAT_LEGACY_FILENAMES = {"business_logic.js", "test_flows.js"}


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: str
    phase: str
    artifact_kind: str
    api_or_det: str
    canonical_source: bool
    logical_owner: str
    producer: str
    allowed_consumers: tuple[str, ...] = field(default_factory=tuple)
    debug_only: bool = False
    semantic_role: str = ""
    source_selection: Mapping[str, Any] | None = None
    canonical_semantic_source: bool | None = None
    canonical_prompt_boundary: bool | None = None
    artifact_role: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.api_or_det not in API_OR_DET_VALUES:
            raise ValueError(f"invalid api_or_det for {self.artifact_id}: {self.api_or_det}")
        if self.artifact_kind not in ARTIFACT_KIND_VALUES:
            raise ValueError(f"invalid artifact_kind for {self.artifact_id}: {self.artifact_kind}")
        if self.canonical_source and self.debug_only:
            raise ValueError(f"canonical artifact cannot be debug_only: {self.artifact_id}")
        if self.canonical_source and self.artifact_kind in {"debug_export", "validation_report"}:
            raise ValueError(f"debug/report artifact cannot be canonical source: {self.artifact_id}")
        if self.canonical_semantic_source and self.debug_only:
            raise ValueError(f"semantic source artifact cannot be debug_only: {self.artifact_id}")

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "phase": self.phase,
            "artifact_kind": self.artifact_kind,
            "api_or_det": self.api_or_det,
            "canonical_source": self.canonical_source,
            "logical_owner": self.logical_owner,
            "producer": self.producer,
            "allowed_consumers": list(self.allowed_consumers),
            "debug_only": self.debug_only,
        }
        if self.semantic_role:
            payload["semantic_role"] = self.semantic_role
        if self.source_selection:
            payload["source_selection"] = dict(self.source_selection)
        if self.canonical_semantic_source is not None:
            payload["canonical_semantic_source"] = self.canonical_semantic_source
        if self.canonical_prompt_boundary is not None:
            payload["canonical_prompt_boundary"] = self.canonical_prompt_boundary
        if self.artifact_role:
            payload["artifact_role"] = self.artifact_role
        if self.notes:
            payload["notes"] = self.notes
        return payload


def artifact_relative_path(result_dir: Path, path: Path) -> str:


    try:
        return path.absolute().relative_to(result_dir.absolute()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_root_legacy_path(relative_path: str) -> bool:
    parts = relative_path.split("/")
    if not parts:
        return False
    top = parts[0]
    if top in LEGACY_ROOT_DIRS:
        return True
    if len(parts) == 1:
        suffix = Path(relative_path).suffix.lower()
        return suffix in {".html", ".css"} or top in ROOT_FLAT_LEGACY_FILENAMES
    return False


class ArtifactIndex:
    def __init__(self, result_dir: Path, index_path: Path | None = None) -> None:
        self.result_dir = result_dir
        self.index_path = index_path or result_dir / "artifact_index.json"
        self._records: dict[str, dict[str, Any]] = {}
        if self.index_path.is_file():
            try:
                payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            for item in payload.get("artifacts", []) if isinstance(payload, dict) else []:
                if isinstance(item, dict) and item.get("artifact_id"):
                    self._records[str(item["artifact_id"])] = dict(item)

    def upsert(self, record: ArtifactRecord) -> None:
        self._records[record.artifact_id] = record.to_json()

    def has_path(self, path: Path) -> bool:
        relative = artifact_relative_path(self.result_dir, path)
        return any(str(item.get("path") or "") == relative for item in self._records.values())

    def prune_missing_or_deprecated_records(self) -> None:
        candidates_by_path: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for artifact_id, item in self._records.items():
            rel = str(item.get("path") or "")
            if not rel:
                continue
            parts = rel.split("/")
            if "deprecated" in parts:
                continue
            if _is_root_legacy_path(rel):
                continue
            if (self.result_dir / rel).is_file():
                candidates_by_path.setdefault(rel, []).append((artifact_id, item))

        def score(candidate: tuple[str, dict[str, Any]]) -> tuple[int, int, int]:
            _artifact_id, item = candidate
            producer = str(item.get("producer") or "")
            return (
                1 if producer != "site_model.artifact_index.infer_artifact_record" else 0,
                1 if item.get("canonical_source") else 0,
                0 if item.get("debug_only") else 1,
            )

        kept: dict[str, dict[str, Any]] = {}
        for _rel, candidates in candidates_by_path.items():
            artifact_id, item = max(candidates, key=score)
            kept[artifact_id] = item
        self._records = kept

    def write(self, *, refresh_filesystem_summary: bool = True) -> Path:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "ghostweb_artifact_index",
            "version": 1,
            "artifacts": sorted(self._records.values(), key=lambda item: str(item.get("artifact_id", ""))),
            "summary": self._summary(refresh_filesystem=refresh_filesystem_summary),
        }
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.index_path

    def _summary(self, *, refresh_filesystem: bool = True) -> dict[str, Any]:
        counts_by_kind: dict[str, int] = {}
        counts_by_api_or_det: dict[str, int] = {}
        canonical_sources = 0
        canonical_semantic_sources = 0
        canonical_prompt_boundaries = 0
        debug_only = 0
        unclassified_artifacts = 0
        registered_paths = {
            str(item.get("path") or "")
            for item in self._records.values()
            if str(item.get("path") or "")
        }
        for item in self._records.values():
            kind = str(item.get("artifact_kind") or "")
            api_or_det = str(item.get("api_or_det") or "")
            counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
            counts_by_api_or_det[api_or_det] = counts_by_api_or_det.get(api_or_det, 0) + 1
            canonical_sources += 1 if item.get("canonical_source") else 0
            canonical_semantic_sources += 1 if item.get("canonical_semantic_source") else 0
            canonical_prompt_boundaries += 1 if item.get("canonical_prompt_boundary") else 0
            debug_only += 1 if item.get("debug_only") else 0
            if not kind or not api_or_det or not item.get("logical_owner"):
                unclassified_artifacts += 1
        root_flat_artifacts = 0
        root_legacy_dir_artifacts = 0
        unindexed_artifacts = 0
        if refresh_filesystem and self.result_dir.is_dir():
            allowed_root_files = {"run_manifest.json", "artifact_index.json", "README.md"}
            ignored_dirs = {"logs", "deprecated"}
            for child in self.result_dir.iterdir():
                if child.is_dir():
                    if child.name in LEGACY_ROOT_DIRS:
                        root_legacy_dir_artifacts += sum(1 for path in child.rglob("*") if path.is_file())
                    continue
                if child.name not in allowed_root_files:
                    root_flat_artifacts += 1
            for path in self.result_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = artifact_relative_path(self.result_dir, path)
                if rel in {"artifact_index.json"}:
                    continue
                rel_parts = rel.split("/")
                if rel_parts[0] in ignored_dirs or "deprecated" in rel_parts:
                    continue
                if rel not in registered_paths:
                    unindexed_artifacts += 1
        return {
            "artifact_count": len(self._records),
            "counts_by_kind": counts_by_kind,
            "counts_by_api_or_det": counts_by_api_or_det,
            "canonical_source_count": canonical_sources,
            "canonical_semantic_source_count": canonical_semantic_sources,
            "canonical_prompt_boundary_count": canonical_prompt_boundaries,
            "debug_only_count": debug_only,
            "root_flat_artifact_counts": root_flat_artifacts,
            "root_legacy_dir_artifact_counts": root_legacy_dir_artifacts,
            "unclassified_artifact_counts": unclassified_artifacts,
            "unindexed_artifact_counts": unindexed_artifacts,
        }


def register_artifact(
    result_dir: Path,
    path: Path,
    *,
    artifact_id: str,
    phase: str,
    artifact_kind: str,
    api_or_det: str,
    canonical_source: bool,
    logical_owner: str,
    producer: str,
    allowed_consumers: Iterable[str] = (),
    debug_only: bool = False,
    semantic_role: str = "",
    source_selection: Mapping[str, Any] | None = None,
    canonical_semantic_source: bool | None = None,
    canonical_prompt_boundary: bool | None = None,
    artifact_role: str = "",
    notes: str = "",
    refresh_filesystem_summary: bool = True,
) -> Path:
    index = ArtifactIndex(result_dir)
    index.upsert(
        ArtifactRecord(
            artifact_id=artifact_id,
            path=artifact_relative_path(result_dir, path),
            phase=phase,
            artifact_kind=artifact_kind,
            api_or_det=api_or_det,
            canonical_source=canonical_source,
            logical_owner=logical_owner,
            producer=producer,
            allowed_consumers=tuple(str(item) for item in allowed_consumers),
            debug_only=debug_only,
            semantic_role=semantic_role,
            source_selection=source_selection,
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            artifact_role=artifact_role,
            notes=notes,
        )
    )
    return index.write(refresh_filesystem_summary=refresh_filesystem_summary)


def infer_artifact_record(result_dir: Path, path: Path) -> ArtifactRecord | None:
    if not path.is_file():
        return None
    rel = artifact_relative_path(result_dir, path)
    if rel == "artifact_index.json" or rel.startswith("logs/") or rel.startswith("deprecated/"):
        return None
    suffix = path.suffix.lower()
    parts = rel.split("/")
    top = parts[0] if parts else ""
    stem = path.stem
    if "deprecated" in parts:
        return None

    def record(
        *,
        artifact_id: str,
        phase: str,
        artifact_kind: str,
        api_or_det: str,
        canonical_source: bool,
        logical_owner: str,
        allowed_consumers: tuple[str, ...] = (),
        debug_only: bool = False,
        semantic_role: str = "",
        canonical_semantic_source: bool | None = None,
        canonical_prompt_boundary: bool | None = None,
        artifact_role: str = "",
        notes: str = "inferred_from_path",
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=artifact_id,
            path=rel,
            phase=phase,
            artifact_kind=artifact_kind,
            api_or_det=api_or_det,
            canonical_source=canonical_source,
            logical_owner=logical_owner,
            producer="site_model.artifact_index.infer_artifact_record",
            allowed_consumers=allowed_consumers,
            debug_only=debug_only,
            semantic_role=semantic_role,
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            artifact_role=artifact_role,
            notes=notes,
        )

    safe_id = rel.replace("/", ".").replace(" ", "_")
    if top == "semantic_api":
        semantic_phase = parts[1] if len(parts) > 1 else "semantic_api"
        if semantic_phase == "06_page_environment_specs" and path.name != "page_environment_specs.json":
            return record(
                artifact_id=f"validation_mirror.{safe_id}",
                phase="page_environment_compiler",
                artifact_kind="validation_report",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_environment_spec",
                allowed_consumers=("developer", "artifact_inventory"),
                semantic_role="misplaced_spec_validation_mirror",
            )
        if semantic_phase == "00_reference_trace_rewrite":
            return record(
                artifact_id=f"semantic_api.{safe_id}",
                phase=semantic_phase,
                artifact_kind="api_semantic_source",
                api_or_det="mixed",
                canonical_source=True,
                logical_owner="reference_trace_rewrite",
                allowed_consumers=("global_reasoning", "page_environment_compiler", "validator"),
                semantic_role="rewritten_reference_trace_semantics",
            )
        return record(
            artifact_id=f"semantic_api.{safe_id}",
            phase=semantic_phase,
            artifact_kind="api_semantic_source",
            api_or_det="mixed",
            canonical_source=True,
            logical_owner=semantic_phase,
            allowed_consumers=("page_environment_compiler", "pagegen", "runtime_backend", "validator"),
        )
    if top == "registries":
        return record(
            artifact_id=f"registries.{safe_id}",
            phase=parts[1] if len(parts) > 1 else "registries",
            artifact_kind="det_registry",
            api_or_det="det",
            canonical_source=True,
            logical_owner=parts[1] if len(parts) > 1 else "registry",
            allowed_consumers=("page_environment_compiler", "runtime_backend", "validator"),
        )
    if top == "asset_semantics":
        return record(
            artifact_id=f"asset_semantics.{safe_id}",
            phase=parts[1] if len(parts) > 1 else "asset_semantics",
            artifact_kind="api_semantic_source",
            api_or_det="api",
            canonical_source=True,
            logical_owner="pagegen_resource_semantic_labeling",
            allowed_consumers=("pagegen",),
            semantic_role="asset_binding_semantics",
            canonical_semantic_source=True,
            artifact_role="authority",
        )
    if top == "configs":
        return record(
            artifact_id=f"configs.{safe_id}",
            phase="run_configuration",
            artifact_kind="det_projection",
            api_or_det="det",
            canonical_source=False,
            logical_owner="run_configuration",
            allowed_consumers=("developer", "cache_resume"),
        )
    if top == "projections":
        return record(
            artifact_id=f"projections.{safe_id}",
            phase=parts[1] if len(parts) > 1 else "projection",
            artifact_kind="det_projection",
            api_or_det="det",
            canonical_source=False,
            logical_owner=parts[1] if len(parts) > 1 else "projection",
            allowed_consumers=("final_consumer", "validation"),
        )
    if top in LEGACY_ROOT_DIRS:
        return None
    if top == "validation":
        return record(
            artifact_id=f"{top}.{safe_id}",
            phase=parts[1] if len(parts) > 1 else top,
            artifact_kind="validation_report",
            api_or_det="det",
            canonical_source=False,
            logical_owner=parts[1] if len(parts) > 1 else top,
            allowed_consumers=("developer", "artifact_inventory"),
        )
    if top.startswith("phase_"):
        phase_name = top.removeprefix("phase_")
        in_debug = len(parts) > 1 and parts[1] == "debug"
        if phase_name == "consolidation" and len(parts) > 2 and parts[1] == "site_model":
            name = path.name
            if name == "schema_registry.json":
                kind, owner, canonical = "det_registry", "schema_registry", True
            elif name == "page_environment_specs.json":
                kind, owner, canonical = "final_consumer_artifact", "page_environment_spec", False
            else:
                kind, owner, canonical = "final_consumer_artifact", stem, False
            return record(
                artifact_id=f"phase_consolidation.site_model.{safe_id}",
                phase="consolidation",
                artifact_kind=kind,
                api_or_det="det",
                canonical_source=canonical,
                logical_owner=owner,
                allowed_consumers=("pagegen", "runtime_backend", "validator", "developer"),
            )
        if phase_name == "pagegen_input_scope" and not in_debug and path.name == "pagegen_scope_selection.json":
            return record(
                artifact_id=f"phase_pagegen_input_scope.{safe_id}",
                phase="pagegen_input_scope",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="frontend.pagegen_input_scope",
                allowed_consumers=("pagegen", "developer"),
                semantic_role="pagegen_scope_selection",
                canonical_semantic_source=False,
                canonical_prompt_boundary=True,
                artifact_role="projection",
            )
        if phase_name == "pagegen":
            if len(parts) > 1 and parts[1] == "pages" and suffix in {".html", ".css", ".js"}:
                return record(
                    artifact_id=f"phase_pagegen.pages.{safe_id}",
                    phase="pagegen",
                    artifact_kind="final_consumer_artifact",
                    api_or_det="mixed" if suffix in {".html", ".css"} else "det",
                    canonical_source=True,
                    logical_owner="frontend.pages",
                    allowed_consumers=("browser", "validator", "debug_visualization"),
                    semantic_role="canonical_frontend_page",
                )
            if len(parts) > 1 and parts[1] == "sprites":
                if path.name == "pagegen_prompt_manifest.json":
                    return record(
                        artifact_id=f"phase_pagegen.sprites.{safe_id}",
                        phase="pagegen_input_assembly",
                        artifact_kind="det_projection",
                        api_or_det="det",
                        canonical_source=False,
                        logical_owner="frontend.pagegen_prompt_manifest",
                        allowed_consumers=("pagegen",),
                        semantic_role="canonical_pagegen_prompt_boundary",
                        canonical_semantic_source=False,
                        canonical_prompt_boundary=True,
                        artifact_role="projection",
                    )
                return record(
                    artifact_id=f"phase_pagegen.sprites.{safe_id}",
                    phase="pagegen_input_assembly",
                    artifact_kind="debug_export" if in_debug else "det_projection",
                    api_or_det="mixed",
                    canonical_source=False,
                    logical_owner="frontend.pagegen_input_scope",
                    allowed_consumers=("pagegen", "debug_visualization"),
                    debug_only=in_debug,
                    semantic_role="pagegen_resource_asset",
                )
            if len(parts) > 1 and parts[1] == "validation":
                return record(
                    artifact_id=f"phase_pagegen.validation.{safe_id}",
                    phase="pagegen",
                    artifact_kind="validation_report",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner=parts[2] if len(parts) > 2 else "pagegen_validation",
                    allowed_consumers=("developer", "artifact_inventory"),
                )
        return record(
            artifact_id=f"phase.{safe_id}",
            phase=phase_name,
            artifact_kind="debug_export" if in_debug else "det_projection",
            api_or_det="det",
            canonical_source=False,
            logical_owner=parts[2] if in_debug and len(parts) > 2 else phase_name,
            allowed_consumers=("developer", "artifact_inventory"),
            debug_only=in_debug,
        )
    if suffix in {".html", ".css"}:
        return None
    if path.name in {"business_logic.js", "test_flows.js"}:
        return None
    return record(
        artifact_id=f"unclassified.{safe_id}",
        phase="unclassified",
        artifact_kind="debug_export",
        api_or_det="det",
        canonical_source=False,
        logical_owner="unclassified",
        allowed_consumers=("developer",),
        debug_only=True,
        notes="fallback_inferred_noncanonical",
    )


def rebuild_artifact_index(result_dir: Path, *, preserve_existing: bool = True) -> Path:
    index_path = result_dir / "artifact_index.json"
    if preserve_existing:
        index = ArtifactIndex(result_dir, index_path=index_path)
        index.prune_missing_or_deprecated_records()
    else:
        if index_path.exists():
            deprecated_dir = result_dir / "deprecated" / "artifact_index"
            deprecated_dir.mkdir(parents=True, exist_ok=True)
            index_path.replace(deprecated_dir / f"artifact_index.{int(time.time())}.json")
        index = ArtifactIndex(result_dir, index_path=index_path)
    for path in sorted(result_dir.rglob("*")):
        if not path.is_file():
            continue
        record = infer_artifact_record(result_dir, path)
        if record is not None:
            if index.has_path(path) and record.artifact_id not in index._records:
                relative = artifact_relative_path(result_dir, path)
                stale_ids = [
                    artifact_id
                    for artifact_id, item in index._records.items()
                    if str(item.get("path") or "") == relative
                    and str(item.get("producer") or "") == "site_model.artifact_index.infer_artifact_record"
                    and (
                        str(artifact_id).startswith("unclassified.")
                        or str(artifact_id).startswith("root_flat.")
                    )
                ]
                if stale_ids:
                    for artifact_id in stale_ids:
                        index._records.pop(artifact_id, None)
                else:
                    continue
            index.upsert(record)
    return index.write()
