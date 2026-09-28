
import os
import json
import re
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.app_spec.model import AppSpec, FrameObservation


class PersistenceMixin:


    CONTENT_EXTRACTION_DEBUG_DIR = "content_extraction"
    CONTENT_EXTRACTION_CLUSTER_DIR = "clusters"
    CONTENT_EXTRACTION_INSTANCE_DIR = "instances"
    INSTANCE_PLAN_DEBUG_FILE = "instance_plan.json"


    _FORMAL_FILES = {
        "instance_plan.json",
        "reference_trace_semantics.json",
        "runtime_state_observations.json",
        "content_lifecycle_plan.json",
        "screenshot_render_classification.json",
        "global_reasoning_final.json",
        "global_reasoning_page_assignment_audited.json",
    }

    def _safe_debug_name(self, value: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
        return text[:180] or "unknown"

    def _content_extraction_cluster_relpath(self, cluster_key: str) -> str:
        return os.path.join(
            self.CONTENT_EXTRACTION_CLUSTER_DIR,
            f"{self._safe_debug_name(cluster_key)}.json",
        )

    def _content_extraction_instance_relpath(
        self,
        cluster_key: str,
        instance_key: str,
        instance_order: int,
    ) -> str:
        key = self._safe_debug_name(instance_key or f"instance_{instance_order}")
        return os.path.join(
            self.CONTENT_EXTRACTION_INSTANCE_DIR,
            self._safe_debug_name(cluster_key),
            f"{int(instance_order):03d}_{key}.json",
        )

    def _content_entity_keys(self, content: Dict[str, Any]) -> List[str]:
        extracted_items = content.get("extracted_items", {}) if isinstance(content, dict) else {}
        if not isinstance(extracted_items, dict):
            return []
        return sorted([
            key for key, value in extracted_items.items()
            if key != "_metadata" and isinstance(value, list) and value
        ])

    def _stable_json_fingerprint(self, payload: Any) -> str:
        encoded = json.dumps(
            payload if payload is not None else {},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _instance_plan_entry_fingerprint(self, instance_planning: Dict[str, Any]) -> str:
        if not isinstance(instance_planning, dict) or not instance_planning:
            return ""
        return self._stable_json_fingerprint(instance_planning)

    def _write_phase_manifest(
        self,
        output_dir: str,
        phase: str,
        filename: str,
        *,
        inputs: Dict[str, Any],
        outputs: Dict[str, Any],
        producer: str,
        producer_version: str = "v1",
    ) -> Dict[str, Any]:
        manifest = {
            "format": "ghostweb_phase_manifest",
            "phase": phase,
            "producer": producer,
            "producer_version": producer_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "inputs": inputs,
            "outputs": outputs,
        }
        path = (
            str(self.pom.formal(phase, filename))
            if self.pom
            else os.path.join(output_dir, f"phase_{phase}", filename)
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return manifest

    def _content_instance_summary(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        content = candidate.get("content_data", {}) if isinstance(candidate, dict) else {}
        return {
            "instance_key": candidate.get("instance_key", ""),
            "instance_label": candidate.get("instance_label", ""),
            "obs_keys": candidate.get("obs_keys", []),
            "content_file": candidate.get("content_file", ""),
            "entity_keys": self._content_entity_keys(content),
        }

    def _content_cluster_debug_payload(
        self,
        cluster_key: str,
        content: Dict[str, Any],
        instance_planning: Dict[str, Any],
        instance_plan_entry_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary_content = dict(content or {})
        candidates = summary_content.get("instance_content_candidates", [])
        if isinstance(candidates, list) and candidates:
            summaries = [
                self._content_instance_summary(candidate)
                for candidate in candidates
                if isinstance(candidate, dict)
            ]
            summary_content["instance_content_candidates"] = summaries
            summary_content["instance_content_refs"] = summaries
        fingerprint = (
            str(instance_plan_entry_fingerprint or "").strip()
            or self._instance_plan_entry_fingerprint(instance_planning)
        )
        return {
            "contract_version": getattr(self, "CONTENT_EXTRACTION_CONTRACT_VERSION", ""),
            "cluster_id": cluster_key,
            "authority": {
                "content_extraction_consumes_instance_plan": True,
                "instance_plan_entry_hash": fingerprint,
                "embedded_instance_plan_authority": "debug_snapshot_only",
            },
            "content": summary_content,
            "consumed_instance_plan_snapshot": instance_planning,
            "instance_planning": instance_planning,
        }

    def _hydrate_content_cluster_debug(
        self,
        payload: Dict[str, Any],
        debug_root: str,
    ) -> Dict[str, Any]:
        content = dict(payload.get("content", {}) if isinstance(payload, dict) else {})
        candidates = content.get("instance_content_candidates", [])
        if not isinstance(candidates, list) or not candidates:
            return content
        if any(
            isinstance(candidate, dict) and isinstance(candidate.get("content_data"), dict)
            for candidate in candidates
        ):
            return content

        hydrated: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            relpath = str(candidate.get("content_file", "")).strip()
            if not relpath:
                continue
            instance_path = os.path.join(debug_root, relpath)
            instance_payload = self._read_json_file_safely(instance_path, {})
            instance_content = instance_payload.get("content", {}) if isinstance(instance_payload, dict) else {}
            hydrated.append({
                "instance_key": candidate.get("instance_key", ""),
                "instance_label": candidate.get("instance_label", ""),
                "obs_keys": candidate.get("obs_keys", []),
                "content_file": relpath,
                "content_data": instance_content,
            })
        content["instance_content_candidates"] = hydrated
        return content

    def _load_debug_content_extraction_cluster(
        self,
        output_dir: str,
        cluster_key: str,
        expected_instance_plan_entry_fingerprint: Optional[str] = None,
        require_instance_plan_fingerprint: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not output_dir:
            return None
        debug_root = str(self.pom.formal("content_extraction", "content_extraction")) if self.pom else os.path.join(output_dir, "phase_content_extraction", "content_extraction")
        path = os.path.join(debug_root, self._content_extraction_cluster_relpath(cluster_key))
        if not os.path.exists(path):
            return None
        payload = self._read_json_file_safely(path, {})
        if not isinstance(payload, dict):
            return None
        expected_contract = getattr(self, "CONTENT_EXTRACTION_CONTRACT_VERSION", "")
        if expected_contract and payload.get("contract_version") != expected_contract:
            return None
        authority = payload.get("authority", {})
        if not isinstance(authority, dict):
            authority = {}
        actual_fingerprint = str(authority.get("instance_plan_entry_hash", "") or "").strip()
        expected_fingerprint = str(expected_instance_plan_entry_fingerprint or "").strip()
        if require_instance_plan_fingerprint:
            if not expected_fingerprint or not actual_fingerprint:
                return None
            if actual_fingerprint != expected_fingerprint:
                return None
        elif expected_fingerprint and actual_fingerprint and actual_fingerprint != expected_fingerprint:
            return None
        return {
            "content": self._hydrate_content_cluster_debug(payload, debug_root),
            "consumed_instance_plan_snapshot": payload.get("consumed_instance_plan_snapshot", {}),
            "authority": authority,
        }

    def _save_debug_content_extraction_instance(
        self,
        output_dir: str,
        cluster_key: str,
        instance_key: str,
        instance_label: str,
        trace_ids: List[str],
        obs_keys: List[str],
        content: Dict[str, Any],
        instance_order: int,
    ) -> str:
        debug_root = str(self.pom.formal("content_extraction", "content_extraction")) if self.pom else os.path.join(output_dir, "phase_content_extraction", "content_extraction")
        relpath = self._content_extraction_instance_relpath(
            cluster_key,
            instance_key,
            instance_order,
        )
        path = os.path.join(debug_root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "contract_version": getattr(self, "CONTENT_EXTRACTION_CONTRACT_VERSION", ""),
            "cluster_id": cluster_key,
            "job_type": "instance",
            "instance_key": instance_key,
            "instance_label": instance_label,
            "trace_ids": trace_ids,
            "obs_keys": obs_keys,
            "content": content,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return relpath

    def _load_debug_content_extraction_instance(
        self,
        output_dir: str,
        cluster_key: str,
        instance_key: str,
        instance_order: int,
    ) -> Optional[Dict[str, Any]]:
        if not output_dir:
            return None
        debug_root = str(self.pom.formal("content_extraction", "content_extraction")) if self.pom else os.path.join(output_dir, "phase_content_extraction", "content_extraction")
        relpath = self._content_extraction_instance_relpath(
            cluster_key,
            instance_key,
            instance_order,
        )
        path = os.path.join(debug_root, relpath)
        if not os.path.exists(path):
            return None
        payload = self._read_json_file_safely(path, {})
        if not isinstance(payload, dict):
            return None
        expected_contract = getattr(self, "CONTENT_EXTRACTION_CONTRACT_VERSION", "")
        if expected_contract and payload.get("contract_version") != expected_contract:
            return None
        if str(payload.get("cluster_id", "") or "") != str(cluster_key):
            return None
        if payload.get("job_type") != "instance":
            return None
        if str(payload.get("instance_key", "") or "") != str(instance_key or ""):
            return None
        content = payload.get("content", {})
        if not isinstance(content, dict) or not content:
            return None
        return {
            "content": content,
            "content_file": relpath,
            "payload": payload,
        }

    def _save_debug_content_extraction_cluster(
        self,
        output_dir: str,
        cluster_key: str,
        content: Dict[str, Any],
        instance_planning: Dict[str, Any],
        instance_plan_entry_fingerprint: Optional[str] = None,
    ) -> None:
        debug_root = str(self.pom.formal("content_extraction", "content_extraction")) if self.pom else os.path.join(output_dir, "phase_content_extraction", "content_extraction")
        path = os.path.join(debug_root, self._content_extraction_cluster_relpath(cluster_key))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = self._content_cluster_debug_payload(
            cluster_key,
            content,
            instance_planning,
            instance_plan_entry_fingerprint=instance_plan_entry_fingerprint,
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _save_debug_content_extraction_index(
        self,
        output_dir: str,
        cluster_content_obs: Dict[str, Dict[str, Any]],
        total_clusters: int,
    ) -> None:
        if not output_dir:
            return
        debug_root = str(self.pom.formal("content_extraction", "content_extraction")) if self.pom else os.path.join(output_dir, "phase_content_extraction", "content_extraction")
        os.makedirs(debug_root, exist_ok=True)
        payload = {
            "contract_version": getattr(self, "CONTENT_EXTRACTION_CONTRACT_VERSION", ""),
            "total_clusters": total_clusters,
            "completed_clusters": len(cluster_content_obs),
            "pending_clusters": max(0, total_clusters - len(cluster_content_obs)),
            "cluster_files": {
                key: self._content_extraction_cluster_relpath(key)
                for key in sorted(cluster_content_obs)
            },
            "debug_layout": {
                "root": self.CONTENT_EXTRACTION_DEBUG_DIR,
                "cluster_summaries": self.CONTENT_EXTRACTION_CLUSTER_DIR,
                "instance_contents": self.CONTENT_EXTRACTION_INSTANCE_DIR,
            },
        }
        with open(os.path.join(debug_root, "index.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _save_debug_schema_context_content_obs(
        self,
        output_dir: str,
        cluster_content_obs: Dict[str, Dict[str, Any]],
        instance_planning: Dict[str, Any],
    ) -> None:
        if not output_dir:
            return
        if self.pom:
            content_path = str(self.pom.formal("content_extraction", "extracted_content.json"))
        else:
            content_path = os.path.join(output_dir, "phase_content_extraction", "extracted_content.json")
        artifacts = {
            content_path: cluster_content_obs,
        }
        for path, payload in artifacts.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

    def _save_parallel_summary(self, summary: Dict[str, Any], output_dir: str):
        pom = self.pom or PhaseOutputManager(output_dir)
        path = str(pom.debug("consolidation", "site_model_parallel_summary.json"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

    def _save_debug_frame_observation(self, obs: FrameObservation, output_dir: str):
        if self.pom:
            obs_dir = str(self.pom.formal("frame_analysis", "frame_observations"))
        else:
            obs_dir = os.path.join(output_dir, "phase_frame_analysis", "frame_observations")
        os.makedirs(obs_dir, exist_ok=True)
        filename = f"{obs.trace_id}_step{obs.step_index}.json"
        path = os.path.join(obs_dir, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obs.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save frame observation {filename}: {e}")

    def _save_debug_frame_observations(
        self, observations: List[FrameObservation], output_dir: str
    ):
        if self.pom:
            obs_dir = str(self.pom.formal("frame_analysis", "frame_observations"))
        else:
            obs_dir = os.path.join(output_dir, "phase_frame_analysis", "frame_observations")
        os.makedirs(obs_dir, exist_ok=True)
        saved = 0
        for obs in observations:
            record = obs.to_dict()
            filename = f"{obs.trace_id}_step{obs.step_index}.json"
            path = os.path.join(obs_dir, filename)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2, ensure_ascii=False)
                saved += 1
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Debug] Failed to save frame observation {filename}: {e}")
        if self.logger:
            self.logger.log_info(f"[Debug] Saved {saved} frame observations to {obs_dir}")

    def _save_debug_page_structures(
        self, cluster_structures: Dict[str, Dict[str, Any]], output_dir: str
    ):
        if self.pom:
            struct_dir = str(self.pom.debug("l0_fidelity", "page_structures"))
            layout_dir = str(self.pom.debug("l0_fidelity", "page_layouts"))
        else:
            phase_dir = os.path.join(output_dir, "phase_l0_fidelity", "debug")
            struct_dir = os.path.join(phase_dir, "page_structures")
            layout_dir = os.path.join(phase_dir, "page_layouts")
        os.makedirs(struct_dir, exist_ok=True)
        os.makedirs(layout_dir, exist_ok=True)
        saved = 0
        for cluster_key, structure in cluster_structures.items():
            safe_key = str(cluster_key).replace("/", "_").replace(":", "_").replace(" ", "_")

            json_path = os.path.join(struct_dir, f"{safe_key}.json")
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(structure, f, indent=2, ensure_ascii=False)
                saved += 1
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Debug] Failed to save page structure {safe_key}.json: {e}")

            layout_text = structure.get("visual_layout_text", "") if isinstance(structure, dict) else ""
            if layout_text:
                txt_path = os.path.join(layout_dir, f"{safe_key}.txt")
                try:
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(layout_text)
                except Exception as e:
                    if self.logger:
                        self.logger.log_warning(f"[Debug] Failed to save page layout {safe_key}.txt: {e}")
        if self.logger:
            self.logger.log_info(f"[Debug] Saved {saved} page structures to {struct_dir}")

    def _save_debug_instance_planning(
        self,
        instance_planning: Dict[str, Any],
        output_dir: str,
    ):
        if not instance_planning:
            return
        if self.pom:
            path = str(self.pom.formal("instance_splitting", self.INSTANCE_PLAN_DEBUG_FILE))
        else:
            path = os.path.join(output_dir, "phase_instance_splitting", self.INSTANCE_PLAN_DEBUG_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(instance_planning, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info(
                    f"[Debug] Saved instance plan: {len(instance_planning)} clusters"
                )
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save instance plan: {e}")

    def _save_debug_global_reasoning_fixloop(self, payload: Dict[str, Any], output_dir: str):
        if not payload:
            return
        if self.pom:
            debug_dir = str(self.pom.debug("global_reasoning"))
        else:
            debug_dir = os.path.join(output_dir, "phase_global_reasoning", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        primary_path = os.path.join(debug_dir, "trace_level_fix.json")
        legacy_path = os.path.join(debug_dir, "global_reasoning_fixloop.json")
        try:
            for path in (primary_path, legacy_path):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info("[Debug] Saved trace-level reasoning fix diagnostics")
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save trace-level reasoning fix diagnostics: {e}")

    def _save_debug_page_level_fix(self, payload: Dict[str, Any], output_dir: str):
        if not payload:
            return
        path = str(self.pom.debug("global_reasoning", "page_level_fix.json")) if self.pom else os.path.join(output_dir, "phase_global_reasoning", "debug", "page_level_fix.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info("[Debug] Saved page-level reasoning fix diagnostics")
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save page-level reasoning fix diagnostics: {e}")

    def _save_debug_global_reasoning_raw(self, global_reasoning: Dict[str, Any], output_dir: str):
        path = str(self.pom.debug("global_reasoning", "global_reasoning.json")) if self.pom else os.path.join(output_dir, "phase_global_reasoning", "debug", "global_reasoning.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(global_reasoning, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save global reasoning: {e}")

    def _save_debug_global_reasoning_stage(self, filename: str, payload: Dict[str, Any], output_dir: str):
        if not filename or payload is None:
            return
        if self.pom:
            if filename in self._FORMAL_FILES:

                instance_splitting_files = {
                    "instance_plan.json",
                    "runtime_state_observations.json",
                    "runtime_state_obligations.json",
                }
                trace_semantic_files = {
                    "reference_trace_semantics.json",
                    "content_lifecycle_plan.json",
                }
                screenshot_classification_files = {
                    "screenshot_render_classification.json",
                }
                if filename in trace_semantic_files:
                    path = str(self.pom.formal("trace_semantic_extraction", filename))
                elif filename in instance_splitting_files:
                    path = str(self.pom.formal("instance_splitting", filename))
                elif filename in screenshot_classification_files:
                    path = str(self.pom.formal("l0_fidelity", filename))
                else:
                    path = str(self.pom.formal("global_reasoning", filename))
            else:

                instance_splitting_files = {
                    "runtime_state_obligations.json",
                }
                if filename in instance_splitting_files:
                    path = str(self.pom.debug("instance_splitting", filename))
                else:
                    path = str(self.pom.debug("global_reasoning", filename))
        else:

            instance_splitting_files = {
                "instance_plan.json",
                "runtime_state_observations.json",
                "runtime_state_obligations.json",
            }
            trace_semantic_files = {
                "reference_trace_semantics.json",
                "content_lifecycle_plan.json",
            }
            screenshot_classification_files = {
                "screenshot_render_classification.json",
            }
            if filename in trace_semantic_files:
                debug_dir = os.path.join(output_dir, "phase_trace_semantic_extraction")
            elif filename in instance_splitting_files:
                if filename in self._FORMAL_FILES:
                    debug_dir = os.path.join(output_dir, "phase_instance_splitting")
                else:
                    debug_dir = os.path.join(output_dir, "phase_instance_splitting", "debug")
            elif filename in screenshot_classification_files:
                debug_dir = os.path.join(output_dir, "phase_l0_fidelity")
            elif filename in self._FORMAL_FILES:
                debug_dir = os.path.join(output_dir, "phase_global_reasoning")
            else:
                debug_dir = os.path.join(output_dir, "phase_global_reasoning", "debug")
            path = os.path.join(debug_dir, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save {filename}: {e}")

    def _save_debug_task_elements(self, global_reasoning: Dict[str, Any], output_dir: str):
        task_elements = {
            "elements_by_page": global_reasoning.get("task_related_elements", {})
        }
        if not task_elements["elements_by_page"]:
            return
        path = str(self.pom.debug("global_reasoning", "task_elements.json")) if self.pom else os.path.join(output_dir, "phase_global_reasoning", "debug", "task_elements.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task_elements, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info(
                    f"[Debug] Saved task_elements: "
                    f"{len(task_elements['elements_by_page'])} pages"
                )
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save task_elements: {e}")

    def _save_capture_actions(self, capture_actions: Dict[str, list], output_dir: str):
        if not capture_actions:
            return
        path = str(self.pom.debug("global_reasoning", "capture_actions.json")) if self.pom else os.path.join(output_dir, "phase_global_reasoning", "debug", "capture_actions.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(capture_actions, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info(
                    f"[Debug] Saved capture_actions: {len(capture_actions)} traces"
                )
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save capture_actions: {e}")

    def _save_shell_materialization(self, shell_materialization: Dict[str, Any], output_dir: str):
        if not shell_materialization:
            return
        path = str(self.pom.debug("global_reasoning", "shell_materialization.json")) if self.pom else os.path.join(output_dir, "phase_global_reasoning", "debug", "shell_materialization.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(shell_materialization, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info(
                    f"[Debug] Saved shell materialization for {len(shell_materialization.get('pages', {}))} pages"
                )
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save shell materialization: {e}")

    def _save_debug_global_reasoning(
        self,
        global_reasoning: Dict[str, Any],
        obs_cluster: Dict[str, str],
        clusters: Dict[str, List[FrameObservation]],
        output_dir: str
    ):
        if self.pom:
            debug_dir = str(self.pom.debug("global_reasoning"))
        else:
            debug_dir = os.path.join(output_dir, "phase_global_reasoning", "debug")
        os.makedirs(debug_dir, exist_ok=True)


        gr_path = os.path.join(debug_dir, "global_reasoning.json")
        try:
            with open(gr_path, "w", encoding="utf-8") as f:
                json.dump(global_reasoning, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save global reasoning: {e}")


        cluster_assignment_path = os.path.join(debug_dir, "cluster_assignments.json")
        try:


            cluster_summary = {}
            for cluster_key, obs_list in sorted(clusters.items()):
                cluster_summary[cluster_key] = {
                    "count": len(obs_list),
                    "members": [
                        {
                            "key": f"{o.trace_id}:{o.step_index}",
                            "description": (o.description or "")[:120],
                            "confidence": o.confidence,
                            "screenshot": os.path.basename(o.screenshot_path) if o.screenshot_path else "",
                        }
                        for o in obs_list
                    ]
                }
            payload = {
                "obs_to_cluster": dict(sorted(obs_cluster.items())),
                "clusters": cluster_summary
            }
            with open(cluster_assignment_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save cluster assignments: {e}")

        if self.logger:
            self.logger.log_info(
                f"[Debug] Saved global reasoning + cluster assignments "
                f"({len(obs_cluster)} observations -> {len(clusters)} clusters) to {debug_dir}"
            )


    def _infer_from_page(self, site_model: AppSpec, screenshot_path: str) -> Optional[str]:
        for proto in site_model.structure.page_prototypes:
            if screenshot_path in proto.screenshot_refs:
                return proto.page_id

        if site_model.structure.page_prototypes:
            return site_model.structure.page_prototypes[0].page_id
        return None

    def _merge_schema_updates(self, site_model: AppSpec, schema_updates: List[Dict]):
        existing = {s.get("name"): s for s in site_model.content.entity_schemas}
        for update in schema_updates:
            name = update.get("name")
            if not name:
                continue
            if name in existing:

                existing_fields = {f["name"] for f in existing[name].get("fields", [])}
                for field in update.get("fields", []):
                    if field.get("name") not in existing_fields:
                        existing[name].setdefault("fields", []).append(field)
            else:
                existing[name] = update
        site_model.content.entity_schemas = list(existing.values())


    def _compute_changelog(self, before: Dict, after: Dict,
                           trace_index: int, trace_id: str, task: str) -> Dict:
        before_pages = {p["page_id"]: p for p in before.get("structure", {}).get("page_prototypes", [])}
        after_pages = {p["page_id"]: p for p in after.get("structure", {}).get("page_prototypes", [])}

        before_edges = {(e["from_page_id"], e["to_page_id"], e["trigger"])
                        for e in before.get("structure", {}).get("site_graph", {}).get("edges", [])}
        after_edges_list = after.get("structure", {}).get("site_graph", {}).get("edges", [])

        before_entities = {e["name"]: e for e in before.get("content", {}).get("entity_schemas", [])}
        after_entities = {e["name"]: e for e in after.get("content", {}).get("entity_schemas", [])}


        pages_added = []
        for pid, page in after_pages.items():
            if pid not in before_pages:
                pages_added.append({
                    "page_id": pid,
                    "page_type": page.get("page_type", ""),
                    "page_name": page.get("page_name", ""),
                    "filename": page.get("filename", ""),
                    "confidence": page.get("confidence", 0)
                })


        pages_updated = []
        for pid, page in after_pages.items():
            if pid in before_pages:
                old = before_pages[pid]
                if old.get("confidence") != page.get("confidence"):
                    pages_updated.append({
                        "page_id": pid,
                        "field": "confidence",
                        "old_value": old.get("confidence"),
                        "new_value": page.get("confidence")
                    })
                old_comp_count = len(old.get("components", []))
                new_comp_count = len(page.get("components", []))
                if new_comp_count > old_comp_count:
                    pages_updated.append({
                        "page_id": pid,
                        "field": "components",
                        "old_value": old_comp_count,
                        "new_value": new_comp_count
                    })


        edges_added = []
        for edge in after_edges_list:
            key = (edge["from_page_id"], edge["to_page_id"], edge["trigger"])
            if key not in before_edges:
                edges_added.append({
                    "from_page_id": edge["from_page_id"],
                    "to_page_id": edge["to_page_id"],
                    "trigger": edge["trigger"]
                })


        entities_added = []
        for name, entity in after_entities.items():
            if name not in before_entities:
                entities_added.append({
                    "name": name,
                    "fields_count": len(entity.get("fields", []))
                })


        entities_updated = []
        for name, entity in after_entities.items():
            if name in before_entities:
                old_fields = {f["name"] for f in before_entities[name].get("fields", [])}
                new_fields = [f["name"] for f in entity.get("fields", []) if f["name"] not in old_fields]
                if new_fields:
                    entities_updated.append({
                        "name": name,
                        "new_fields": new_fields,
                        "total_fields_after": len(entity.get("fields", []))
                    })

        return {
            "trace_index": trace_index,
            "trace_id": trace_id,
            "task": task,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "pages_added": len(pages_added),
                "pages_updated": len(pages_updated),
                "edges_added": len(edges_added),
                "entities_added": len(entities_added),
                "entities_updated": len(entities_updated)
            },
            "changes": {
                "pages_added": pages_added,
                "pages_updated": pages_updated,
                "edges_added": edges_added,
                "entities_added": entities_added,
                "entities_updated": entities_updated
            }
        }

    def _save_snapshot(self, site_model: AppSpec, output_dir: str, label: str):
        pom = self.pom or PhaseOutputManager(output_dir)
        path = str(pom.debug("consolidation", "site_model_snapshots", label))
        site_model.save(path, preserve_semantic_design=True)
        if self.logger:
            self.logger.log_info(f"Snapshot saved: {path}")

    def _save_changelog(self, changelog: Dict, output_dir: str, trace_index: int):
        pom = self.pom or PhaseOutputManager(output_dir)
        path = str(pom.debug("consolidation", "changelogs", f"changelog_trace_{trace_index}.json"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(changelog, f, indent=2, ensure_ascii=False)

    def _save_combined_changelog(self, changelogs: List[Dict], output_dir: str):
        pom = self.pom or PhaseOutputManager(output_dir)
        path = str(pom.debug("consolidation", "site_model_changelog.json"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(changelogs, f, indent=2, ensure_ascii=False)
        if self.logger:
            self.logger.log_info(f"Combined changelog saved: {path}")

    def _log_changelog_summary(self, changelog: Dict):
        if not self.logger:
            return
        s = changelog["summary"]
        self.logger.log_info(
            f"Changelog trace {changelog['trace_index']} ({changelog['trace_id']}): "
            f"+{s['pages_added']} pages, ~{s['pages_updated']} updated, "
            f"+{s['edges_added']} edges, "
            f"+{s['entities_added']} entities, ~{s['entities_updated']} entity updates"
        )


    def _merge_content_samples(self, site_model: AppSpec,
                                content_result: Dict) -> None:

        extracted = content_result.get("extracted_items", {})
        for entity_name, items in extracted.items():
            if not isinstance(items, list):
                continue
            existing = site_model.content.content_samples.setdefault(entity_name, [])

            existing_keys = set()
            for item in existing:
                try:
                    key = frozenset(sorted(
                        (k, str(v)) for k, v in item.items()
                    ))
                    existing_keys.add(key)
                except TypeError:
                    pass
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    key = frozenset(sorted(
                        (k, str(v)) for k, v in item.items()
                    ))
                except TypeError:
                    key = None
                if key and key not in existing_keys and len(existing) < 50:
                    existing.append(item)
                    existing_keys.add(key)


            self._merge_observed_examples(site_model, entity_name, items)


        constraints = content_result.get("content_constraints", {})
        for entity_name, field_constraints in constraints.items():
            if not isinstance(field_constraints, dict):
                continue
            existing_c = site_model.content.content_constraints.setdefault(entity_name, {})
            for field_name, constraint in field_constraints.items():
                if not isinstance(constraint, dict):
                    continue
                if field_name not in existing_c:
                    existing_c[field_name] = constraint
                else:

                    old = existing_c[field_name]
                    c_type = constraint.get("type", "")
                    if c_type == "enum" and old.get("type") == "enum":
                        old_vals = set(old.get("values", []))
                        new_vals = constraint.get("values", [])
                        old["values"] = list(old_vals | set(new_vals))
                    elif c_type == "range" and old.get("type") == "range":
                        if "min" in constraint:
                            old["min"] = min(old.get("min", constraint["min"]), constraint["min"])
                        if "max" in constraint:
                            old["max"] = max(old.get("max", constraint["max"]), constraint["max"])

    def _merge_observed_examples(self, site_model: AppSpec,
                                  entity_name: str, items: List[Dict]) -> None:
        for schema in site_model.content.entity_schemas:
            if schema.get("name") != entity_name:
                continue
            for f in schema.get("fields", []):
                fname = f.get("name", "")
                examples = f.setdefault("observed_examples", [])
                existing_set = set(str(v) for v in examples)
                for item in items:
                    val = item.get(fname)
                    if val is not None and str(val) not in existing_set and len(examples) < 20:
                        examples.append(val)
                        existing_set.add(str(val))
            break
