
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from trace2world_pipeline.app_spec.trace_loader import TraceLoader


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_step_key(value: str) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(.+):step(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    match = re.match(r"^(.+):(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    return text


def obs_key_from_step_key(step_key: str) -> str:
    normalized = normalize_step_key(step_key)
    match = re.match(r"^(.+):step(\d+)$", normalized)
    if not match:
        return ""
    return f"{match.group(1)}:{int(match.group(2))}"


def step_key_from_obs_key(obs_key: str) -> str:
    text = str(obs_key or "").strip()
    match = re.match(r"^(.+):(\d+)$", text)
    if not match:
        return ""
    return f"{match.group(1)}:step{int(match.group(2))}"


def step_key_from_screenshot_ref(ref: str) -> Optional[str]:
    ref_norm = str(ref or "").replace("\\", "/")
    trajectory_step_key = _trajectory_step_key_from_screenshot_ref(ref_norm)
    if trajectory_step_key:
        return trajectory_step_key
    match = re.search(
        r"/([^/]+)/screenshots/[^/]+-\d+-(\d+)\.(?:jpg|jpeg|png|webp)$",
        ref_norm,
        re.IGNORECASE,
    )
    if match:
        return f"{match.group(1)}:step{max(int(match.group(2)) - 1, 0)}"
    match = re.search(r"/([^/]+)/step[_-]?(\d+)[^/]*\.(?:jpg|jpeg|png|webp)$", ref_norm, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    return None


@lru_cache(maxsize=2048)
def _trajectory_step_key_from_screenshot_ref(ref: str) -> Optional[str]:
    path = Path(str(ref or ""))
    if not path.is_absolute() or path.parent.name not in {"screenshots", "marked_screenshots"}:
        return None
    trace_dir = path.parent.parent
    trajectory_path = trace_dir / "trajectory.json"
    payload = _read_json(trajectory_path)
    entries = payload.get("trajectory", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return None
    target = os.path.normpath(str(path))
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        candidates = [entry.get("screenshot"), entry.get("marked_screenshot")]
        if not any(
            value
            and os.path.normpath(str(trace_dir / str(value))) == target
            for value in candidates
        ):
            continue
        disposition = entry.get("step_disposition")
        step_index = disposition.get("step_index") if isinstance(disposition, dict) else None
        if step_index is None:
            step_index = ordinal
        try:
            normalized_index = int(step_index)
        except (TypeError, ValueError):
            return None
        trace_id = ""
        if isinstance(disposition, dict):
            trace_id = str(disposition.get("trace_id") or "").strip()
        trace_id = trace_id or str(payload.get("task_id") or "").strip() or trace_dir.name
        return f"{trace_id}:step{normalized_index}"
    return None


@dataclass
class StepEvidence:
    trace_id: str
    step_index: int
    step_key: str
    obs_key: str
    canonical_image_path: str = ""
    source_image_path: str = ""
    observation_path: str = ""
    observation_screenshot_path: str = ""
    screenshot_candidates: List[str] = field(default_factory=list)
    filename: str = ""
    page_id: str = ""
    page_name: str = ""
    general_page_id: str = ""
    materialized_page_id: str = ""
    binding_confidence: str = ""
    binding_source: str = ""
    mismatch_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceResolver:

    def __init__(
        self,
        *,
        result_dir: Path,
        trace_input_dir: str = "",
        trace_resource_dir: str = "",
    ) -> None:
        self.result_dir = Path(result_dir)
        self.trace_input_dir = str(trace_input_dir or "").strip()
        self.trace_resource_dir = str(trace_resource_dir or "").strip()
        self._trace_dirs_by_id = self._discover_trace_dirs()
        self._step_keys_by_screenshot_ref = self._load_trace_screenshot_step_keys()
        self._bindings_by_obs_key = self._load_materialized_bindings()
        self._frame_observation_paths = self._load_frame_observation_paths()
        self._step_cache: Dict[str, StepEvidence] = {}

    def _load_trace_screenshot_step_keys(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for trace_id, trace_dir in self._trace_dirs_by_id.items():
            payload = _read_json(trace_dir / "trajectory.json")
            entries = payload.get("trajectory", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                continue
            for ordinal, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                disposition = entry.get("step_disposition")
                step_index = disposition.get("step_index") if isinstance(disposition, dict) else None
                if step_index is None:
                    step_index = ordinal
                try:
                    step_key = f"{trace_id}:step{int(step_index)}"
                except (TypeError, ValueError):
                    continue
                for field_name in ("screenshot", "marked_screenshot"):
                    value = str(entry.get(field_name) or "").strip()
                    if not value:
                        continue
                    absolute = Path(value)
                    if not absolute.is_absolute():
                        absolute = trace_dir / value
                    mapping[os.path.normpath(str(absolute))] = step_key
        return mapping

    @classmethod
    def from_result_dir(
        cls,
        result_dir: Path | str,
        *,
        trace_input_dir: str = "",
        trace_resource_dir: str = "",
    ) -> "EvidenceResolver":
        return cls(
            result_dir=Path(result_dir),
            trace_input_dir=trace_input_dir,
            trace_resource_dir=trace_resource_dir,
        )

    def _discover_trace_dirs(self) -> Dict[str, Path]:
        roots = [self.trace_input_dir, self.trace_resource_dir]
        trace_dirs: Dict[str, Path] = {}
        loader = TraceLoader()
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            root_path = Path(root)
            root_index = _read_json(root_path / "index.json")
            entries = root_index.get("traces", []) if isinstance(root_index, dict) else []
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                trace_id = str(entry.get("id") or "").strip()
                entry_path = str(entry.get("path") or trace_id).strip()
                if not trace_id:
                    continue
                candidates = [
                    root_path / entry_path,
                    root_path / os.path.basename(entry_path),
                    root_path / trace_id,
                    root_path / "traces" / trace_id,
                ]
                for candidate in candidates:
                    if candidate.is_dir():
                        trace_dirs.setdefault(trace_id, candidate)
                        break
            try:
                candidates = loader.discover_trace_dirs(root)
            except Exception:
                candidates = []
            if not candidates and TraceLoader.is_meituan_session(root):
                candidates = [root]
            for candidate in candidates:
                path = Path(candidate)
                if not path.is_dir():
                    continue
                trace_dirs.setdefault(path.name, path)
        return trace_dirs

    def _load_materialized_bindings(self) -> Dict[str, Dict[str, Any]]:
        path = self.result_dir / "data" / "site_model" / "materialized_observation_bindings.json"
        payload = _read_json(path)
        bindings = payload.get("bindings_by_obs_key", {}) if isinstance(payload, dict) else {}
        return bindings if isinstance(bindings, dict) else {}

    def _load_frame_observation_paths(self) -> Dict[str, Path]:
        frame_dir = self.result_dir / "data" / "debug" / "frame_observations"
        result: Dict[str, Path] = {}
        if not frame_dir.is_dir():
            return result
        for path in sorted(frame_dir.glob("*.json")):
            obs = _read_json(path)
            if not isinstance(obs, dict):
                continue
            trace_id = str(obs.get("trace_id") or "").strip()
            step_index = obs.get("step_index")
            if not trace_id or step_index is None:
                continue
            try:
                result[f"{trace_id}:{int(step_index)}"] = path
            except Exception:
                continue
        return result

    def resolve_screenshot_ref(self, ref: str) -> Optional[StepEvidence]:
        step_key = self._step_key_from_screenshot_ref(ref)
        if not step_key:
            return None
        return self.get_step_evidence(step_key, observed_ref=ref)

    def _step_key_from_screenshot_ref(self, ref: str) -> Optional[str]:
        ref_norm = str(ref or "").replace("\\", "/")
        explicit = self._step_keys_by_screenshot_ref.get(os.path.normpath(ref_norm))
        if explicit:
            return explicit
        match = re.search(
            r"/([^/]+)/screenshots/\1-\d+-(\d+)\.(?:jpg|jpeg|png|webp)$",
            ref_norm,
            re.IGNORECASE,
        )
        if match:
            trace_id = match.group(1)
            frame_index = int(match.group(2))
            trace_dir = self._trace_dirs_by_id.get(trace_id)
            if trace_dir:
                offset = TraceLoader._infer_meituan_raw_screenshot_index_offset(str(trace_dir))
                return f"{trace_id}:step{max(frame_index - offset, 0)}"
        return step_key_from_screenshot_ref(ref)

    def get_step_evidence(self, step_key_or_obs_key: str, *, observed_ref: str = "") -> Optional[StepEvidence]:
        step_key = normalize_step_key(step_key_or_obs_key)
        if ":step" not in step_key:
            step_key = step_key_from_obs_key(step_key_or_obs_key)
        if not step_key:
            return None
        cache_key = step_key
        if cache_key not in self._step_cache:
            built = self._build_step_evidence(step_key)
            if not built:
                return None
            self._step_cache[cache_key] = built
        evidence = self._step_cache[cache_key]
        if observed_ref:
            observed = str(observed_ref)
            candidates = _dedupe_keep_order([*evidence.screenshot_candidates, observed])
            evidence.screenshot_candidates = candidates
            if evidence.canonical_image_path and observed != evidence.canonical_image_path:
                if "observed_ref_differs_from_canonical_image" not in evidence.mismatch_flags:
                    evidence.mismatch_flags.append("observed_ref_differs_from_canonical_image")
        return evidence

    def _build_step_evidence(self, step_key: str) -> Optional[StepEvidence]:
        match = re.match(r"^(.+):step(\d+)$", step_key)
        if not match:
            return None
        trace_id = match.group(1)
        step_index = int(match.group(2))
        obs_key = f"{trace_id}:{step_index}"
        trace_dir = self._trace_dirs_by_id.get(trace_id)
        canonical_image_path = ""
        source_image_path = ""
        candidates: List[str] = []

        if trace_dir:
            step_json = trace_dir / f"step_{step_index:04d}.json"
            step_entry = _read_json(step_json)
            explicit_candidates: List[str] = []
            if isinstance(step_entry, dict):
                for key in ("image_file", "source_image", "image_path"):
                    value = str(step_entry.get(key) or "").strip()
                    if not value:
                        continue
                    explicit_candidates.append(value)
                    explicit_candidates.append(os.path.basename(value))
            explicit_candidates.append(f"step_{step_index:04d}.png")
            explicit_candidates.append(f"step_{step_index:04d}.jpg")
            for value in explicit_candidates:
                path = Path(value)
                if not path.is_absolute():
                    path = trace_dir / os.path.basename(value)
                if path.is_file():
                    source_image_path = str(path)
                    canonical_image_path = str(path)
                    candidates.append(str(path))
                    break
            raw_candidate = TraceLoader._resolve_meituan_raw_screenshot(str(trace_dir), step_index)
            if raw_candidate:
                candidates.append(raw_candidate)
                if not canonical_image_path:
                    canonical_image_path = raw_candidate
            screenshots_dir = trace_dir / "screenshots"
            if screenshots_dir.is_dir():
                raw_offset = TraceLoader._infer_meituan_raw_screenshot_index_offset(str(trace_dir))
                prefix = f"{trace_id}-0-{step_index + raw_offset}"
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    candidate = screenshots_dir / f"{prefix}{ext}"
                    if candidate.is_file():
                        candidates.append(str(candidate))

        observation_path = self._frame_observation_paths.get(obs_key)
        observation_screenshot_path = ""
        if observation_path:
            obs = _read_json(observation_path)
            if isinstance(obs, dict):
                observation_screenshot_path = str(obs.get("screenshot_path") or "")
                if observation_screenshot_path:
                    candidates.append(observation_screenshot_path)
                if not canonical_image_path and observation_screenshot_path and Path(observation_screenshot_path).is_file():
                    canonical_image_path = observation_screenshot_path

        binding = self._bindings_by_obs_key.get(obs_key, {})
        evidence = StepEvidence(
            trace_id=trace_id,
            step_index=step_index,
            step_key=step_key,
            obs_key=obs_key,
            canonical_image_path=canonical_image_path,
            source_image_path=source_image_path,
            observation_path=str(observation_path or ""),
            observation_screenshot_path=observation_screenshot_path,
            screenshot_candidates=_dedupe_keep_order(candidates),
            filename=str(binding.get("filename") or "") if isinstance(binding, dict) else "",
            page_id=str(binding.get("page_id") or "") if isinstance(binding, dict) else "",
            page_name=str(binding.get("page_name") or "") if isinstance(binding, dict) else "",
            general_page_id=str(binding.get("general_page_id") or "") if isinstance(binding, dict) else "",
            materialized_page_id=str(binding.get("materialized_page_id") or "") if isinstance(binding, dict) else "",
            binding_confidence=str(binding.get("confidence") or "") if isinstance(binding, dict) else "",
            binding_source=str(binding.get("source") or "") if isinstance(binding, dict) else "",
        )
        if evidence.observation_screenshot_path and evidence.canonical_image_path:
            if evidence.observation_screenshot_path != evidence.canonical_image_path:
                evidence.mismatch_flags.append("observation_screenshot_differs_from_canonical_image")
        return evidence

    def canonicalize_screenshot_refs(
        self,
        refs: Iterable[str],
        *,
        filename: str = "",
        role: str = "primary",
    ) -> tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
        canonical_refs: List[str] = []
        evidence_refs: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []
        expected_filename = str(filename or "").split("?", 1)[0]
        for ref in refs or []:
            ref_text = str(ref or "").strip()
            if not ref_text:
                continue
            evidence = self.resolve_screenshot_ref(ref_text)
            if not evidence:
                canonical_refs.append(ref_text)
                issues.append({
                    "severity": "warning",
                    "issue_type": "unresolved_screenshot_ref",
                    "role": role,
                    "screenshot_ref": ref_text,
                    "filename": expected_filename,
                })
                continue
            canonical = evidence.canonical_image_path or ref_text
            canonical_refs.append(canonical)
            evidence_refs.append({
                "role": role,
                "input_screenshot_ref": ref_text,
                "canonical_image_path": canonical,
                "step_key": evidence.step_key,
                "obs_key": evidence.obs_key,
                "bound_filename": evidence.filename,
                "expected_filename": expected_filename,
                "materialized_page_id": evidence.materialized_page_id,
                "page_name": evidence.page_name,
                "mismatch_flags": list(evidence.mismatch_flags),
            })
            if expected_filename and evidence.filename and evidence.filename != expected_filename:
                issues.append({
                    "severity": "blocking",
                    "issue_type": "pagegen_screenshot_bound_to_different_page",
                    "role": role,
                    "screenshot_ref": ref_text,
                    "canonical_image_path": canonical,
                    "step_key": evidence.step_key,
                    "bound_filename": evidence.filename,
                    "expected_filename": expected_filename,
                })
            for flag in evidence.mismatch_flags:
                issues.append({
                    "severity": "info",
                    "issue_type": flag,
                    "role": role,
                    "screenshot_ref": ref_text,
                    "canonical_image_path": canonical,
                    "step_key": evidence.step_key,
                    "bound_filename": evidence.filename,
                    "expected_filename": expected_filename,
                })
        return _dedupe_keep_order(canonical_refs), evidence_refs, issues
