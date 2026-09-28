
import os
import random
import asyncio
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from trace2world_pipeline.app_spec.model import (
    AppSpec, FrameObservation
)
from trace2world_pipeline.app_spec.trace_loader import TraceData
from trace2world_pipeline.app_spec.reconcile.data_models import FrameJob
from trace2world_pipeline.app_spec.provenance import Provenance, attach_provenance
from trace2world_pipeline.core.llm_caller import (
    LLMAPIResponseError,
    LLMJsonResponseError,
    _schema_validation_errors,
    call_openai_api_json_async,
    encode_image,
    estimate_llm_stage_parallelism,
)
from trace2world_pipeline.core.llm_response_schemas import (
    INSTANCE_SPLIT_MERGE_SCHEMA,
    INSTANCE_SPLIT_PRODUCER_SHARD_SCHEMA,
    INSTANCE_SPLIT_SCHEMA,
)
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.core.step_keys import canonical_step_key, normalize_step_keyed_mapping

from trace2world_pipeline.app_spec.reconcile.prompts import PromptsMixin
from trace2world_pipeline.app_spec.reconcile.content_extraction_contract import normalize_content_extraction_payload
from trace2world_pipeline.app_spec.reconcile.frame_analysis import FrameAnalysisMixin
from trace2world_pipeline.app_spec.reconcile.global_reasoning import GlobalReasoningMixin
from trace2world_pipeline.app_spec.reconcile.screenshot_render_classification import (
    ScreenshotRenderClassificationError,
    apply_screenshot_render_classification,
)
from trace2world_pipeline.app_spec.reconcile.consolidation import ConsolidationMixin
from trace2world_pipeline.app_spec.reconcile.persistence import PersistenceMixin


class GlobalReasoningStopAfterReached(Exception):
    ...


class InstanceSplittingStopAfterReached(Exception):
    ...


class SelectedInstanceCompileStopAfterReached(Exception):
    ...


class ContentExtractionPhaseError(RuntimeError):
    ...


class InstanceSplitProducerContractError(ValueError):

    def __init__(self, errors: List[str]):
        self.errors = [str(error) for error in errors]
        super().__init__("; ".join(self.errors))


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


class AppSpecBuilder(
    PromptsMixin,
    FrameAnalysisMixin,
    GlobalReasoningMixin,
    ConsolidationMixin,
    PersistenceMixin,
):

    CONTENT_EXTRACTION_MAX_SCREENSHOTS = _env_positive_int(
        "T2W_CONTENT_EXTRACTION_MAX_SCREENSHOTS",
        4,
    )
    CONTENT_EXTRACTION_CALL_MAX_ATTEMPTS = _env_positive_int(
        "T2W_CONTENT_EXTRACTION_CALL_MAX_ATTEMPTS",
        4,
    )
    INSTANCE_SPLIT_INPUT_TOKEN_LIMIT = 24000
    INSTANCE_SPLIT_PRODUCER_TARGET_TOKENS = 6000
    INSTANCE_SPLIT_CALL_BATCH_SIZE = 4
    INSTANCE_SPLIT_MAX_PRODUCER_ATTEMPTS = 3
    INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION = 2
    CONTENT_EXTRACTION_RETRY_BACKOFF_SECONDS = _env_positive_int(
        "T2W_CONTENT_EXTRACTION_RETRY_BACKOFF_SECONDS",
        90,
    )
    CONTENT_EXTRACTION_CONTRACT_VERSION = "lifecycle_v1"
    REFERENCE_TRACE_SEMANTIC_MAX_FRAMES_PER_PACK = 24
    CONTENT_EXTRACTION_L0_RENDER_ROLES = {
        "canonical_layout",
        "interaction_state_change",
        "post_interaction_overlay",
        "runtime_status",
    }

    def __init__(self,
                 logger=None,
                 model=None,
                 reasoning_effort="medium",
                 parallel_enabled: bool = True,
                 max_concurrent: int = 32,
                 consistency_mode: str = "deterministic",
                 frame_quality_refinement_enabled: bool = False,
                 site_model_random_seed: int = 42,
                 unknown_url_decision_threshold: float = 0.75,
                 use_url_signals: bool = False,
                 global_reasoning_enabled: bool = True,
                 global_reasoning_overflow_strategy: str = "chunk_reduce",
                 global_reasoning_token_budget: int = 60000,
                 global_reasoning_chunk_size: int = 4,
                 global_reasoning_merge_batch_size: int = 4,
                 global_reasoning_compact_chunk_retry_attempts: int = 1,
                 page_name_soft_limit: int = 256,
                 global_reasoning_logic_flow: str = "compact_transition_resolver",
                 global_reasoning_input_path: str = "",
                 stop_after_global_reasoning: bool = False,
                 stop_after_instance_splitting: bool = False,
                 selected_instance_compile_manifest_path: str = "",
                 schema_boundary_slice_token_budget: int = 12000,
                 schema_boundary_slice_candidate_budget: int = 24,
                 skip_full_extraction_enabled: bool = False,
                 screenshot_render_classification_enabled: bool = True,
                 instance_split_mode: str = "llm_split",
                 task_interaction_contract_logic_flow: str = "l0_schema_registry",
                 task_interaction_contract_planner_model: Optional[str] = None,
                 task_interaction_contract_planner_reasoning_effort: Optional[str] = None,
                 frame_observation_reasoning_effort: Optional[str] = None):
        self.logger = logger
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.frame_observation_reasoning_effort = frame_observation_reasoning_effort or self.reasoning_effort
        self.parallel_enabled = parallel_enabled
        self.max_concurrent = max(1, int(max_concurrent))
        self.transition_max_concurrent = self.max_concurrent
        self.delta_max_concurrent = self.max_concurrent
        self.consistency_mode = consistency_mode
        self.frame_quality_refinement_enabled = bool(frame_quality_refinement_enabled)
        self.unknown_url_decision_threshold = float(unknown_url_decision_threshold)
        self.use_url_signals = bool(use_url_signals)
        self.global_reasoning_enabled = bool(global_reasoning_enabled)
        self.global_reasoning_overflow_strategy = (
            str(global_reasoning_overflow_strategy or "chunk_reduce").strip().lower()
        )
        if self.global_reasoning_overflow_strategy not in {"chunk_reduce", "fail_fast"}:
            self.global_reasoning_overflow_strategy = "chunk_reduce"
        self.global_reasoning_token_budget = max(1000, int(global_reasoning_token_budget))
        self.global_reasoning_chunk_size = max(1, int(global_reasoning_chunk_size))
        self.global_reasoning_merge_batch_size = max(2, int(global_reasoning_merge_batch_size))
        self.global_reasoning_compact_chunk_retry_attempts = max(
            0,
            int(global_reasoning_compact_chunk_retry_attempts),
        )
        self.page_name_soft_limit = max(16, int(page_name_soft_limit))
        self.global_reasoning_logic_flow = str(
            global_reasoning_logic_flow or "compact_transition_resolver"
        ).strip().lower()
        valid_global_reasoning_flows = {
            "compact_transition_resolver",
            "compact_authority_namespace_budget",
            "compact_phase1_semantic_balanced",
        }
        if self.global_reasoning_logic_flow not in valid_global_reasoning_flows:
            raise ValueError(
                "Unsupported global_reasoning_logic_flow="
                f"{self.global_reasoning_logic_flow!r}; expected one of "
                f"{sorted(valid_global_reasoning_flows)}"
            )
        self.global_reasoning_input_path = str(global_reasoning_input_path or "").strip()
        self.screenshot_render_classification_enabled = bool(screenshot_render_classification_enabled)
        self.stop_after_global_reasoning = bool(stop_after_global_reasoning)
        self.stop_after_instance_splitting = bool(stop_after_instance_splitting)
        self.selected_instance_compile_manifest_path = str(
            selected_instance_compile_manifest_path or ""
        ).strip()
        self.schema_planning_mode = "boundary_planner"
        self.schema_boundary_slice_token_budget = max(1000, int(schema_boundary_slice_token_budget))
        self.schema_boundary_slice_candidate_budget = max(1, int(schema_boundary_slice_candidate_budget))
        self.skip_full_extraction_enabled = bool(skip_full_extraction_enabled)
        self.instance_split_mode = "llm_split"
        self.task_interaction_contract_logic_flow = str(
            task_interaction_contract_logic_flow or "l0_schema_registry"
        ).strip().lower()
        if self.task_interaction_contract_logic_flow != "l0_schema_registry":
            raise ValueError(
                "Unsupported task_interaction_contract_logic_flow="
                f"{self.task_interaction_contract_logic_flow!r}; expected 'l0_schema_registry'"
            )
        self.task_interaction_contract_planner_model = task_interaction_contract_planner_model or self.model
        self.task_interaction_contract_planner_reasoning_effort = (
            task_interaction_contract_planner_reasoning_effort or self.reasoning_effort or "medium"
        )
        self._random = random.Random(int(site_model_random_seed))
        self._page_counter = 0
        self.pom: Optional[PhaseOutputManager] = None


        if self.logger:
            self.logger.log_info(
                f"[AppSpecBuilder] global_reasoning_logic_flow = '{self.global_reasoning_logic_flow}', "
                f"chunk_size={self.global_reasoning_chunk_size}, "
                f"merge_batch_size={self.global_reasoning_merge_batch_size}"
            )
            if self.skip_full_extraction_enabled:
                self.logger.log_info("[AppSpecBuilder] skip_full_extraction_enabled = True")
            if self.global_reasoning_input_path:
                self.logger.log_info(
                    f"[AppSpecBuilder] global_reasoning_input_path = '{self.global_reasoning_input_path}'"
                )
            self.logger.log_info(
                f"[AppSpecBuilder] schema_planning_mode = '{self.schema_planning_mode}'"
            )
            self.logger.log_info(
                f"[AppSpecBuilder] schema_boundary_slice_token_budget = {self.schema_boundary_slice_token_budget}, "
                f"schema_boundary_slice_candidate_budget = {self.schema_boundary_slice_candidate_budget}"
            )
            self.logger.log_info(
                f"[AppSpecBuilder] instance_split_mode = '{self.instance_split_mode}'"
            )
            self.logger.log_info(
                "[AppSpecBuilder] task_interaction_contract_logic_flow = "
                f"'{self.task_interaction_contract_logic_flow}'"
            )

    def _next_page_id(self) -> str:
        self._page_counter += 1
        return f"p_{self._page_counter:03d}"

    def collect_frame_jobs_v3(self, traces: List[TraceData]) -> List[FrameJob]:
        jobs: List[FrameJob] = []
        for trace in traces:
            sorted_steps = sorted(trace.steps, key=lambda item: item.step_index)
            previous_action = ""
            observed_keys: set[tuple[str, int]] = set()
            for step in sorted_steps:
                if not os.path.exists(step.screenshot_path):
                    previous_action = step.action
                    continue
                jobs.append(FrameJob(
                    trace_id=trace.trace_id,
                    task=trace.task,
                    website_url=trace.website_url,
                    step_index=step.step_index,
                    action=step.action,
                    thought=step.thought,
                    screenshot_path=step.screenshot_path,
                    previous_action=previous_action,
                ))
                observed_keys.add((trace.trace_id, int(step.step_index)))
                previous_action = step.action
            for terminal in sorted(
                [
                    item
                    for item in (getattr(trace, "terminal_observations", []) or [])
                    if isinstance(item, dict)
                ],
                key=lambda item: int(item.get("step_index", -1)),
            ):
                try:
                    step_index = int(terminal.get("step_index", -1))
                except (TypeError, ValueError):
                    continue
                screenshot_path = str(terminal.get("screenshot_path", "") or "").strip()
                if (
                    step_index < 0
                    or not screenshot_path
                    or not os.path.exists(screenshot_path)
                    or (trace.trace_id, step_index) in observed_keys
                ):
                    continue
                jobs.append(FrameJob(
                    trace_id=trace.trace_id,
                    task=trace.task,
                    website_url=trace.website_url,
                    step_index=step_index,
                    action="",
                    thought=str(
                        terminal.get("summary")
                        or terminal.get("detail")
                        or terminal.get("status")
                        or ""
                    ),
                    screenshot_path=screenshot_path,
                    previous_action=previous_action,
                    observation_role="terminal_observation",
                    action_disposition="exclude",
                    observation_disposition="retain",
                    semantic_effect=str(
                        terminal.get("semantic_effect") or "terminal_evidence"
                    ),
                ))
                observed_keys.add((trace.trace_id, step_index))
        jobs.sort(key=lambda x: (x.trace_id, x.step_index))
        return jobs

    @staticmethod
    def _trace_id_from_evidence_ref(ref: Any) -> str:
        text = str(ref or "").strip()
        if ":" not in text:
            return ""
        return text.split(":", 1)[0]

    @classmethod
    def _filter_global_reasoning_to_trace_ids(
        cls,
        payload: Dict[str, Any],
        allowed_trace_ids: set[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not isinstance(payload, dict) or not allowed_trace_ids:
            return payload, {"filtered": False}

        filtered = deepcopy(payload)
        report: Dict[str, Any] = {
            "filtered": False,
            "allowed_trace_count": len(allowed_trace_ids),
            "removed_trace_ids": [],
            "removed_step_labels": 0,
            "removed_skipped_steps": 0,
            "removed_routes": 0,
            "removed_pages": 0,
        }

        original_labels = [
            item for item in filtered.get("step_labels", [])
            if isinstance(item, dict)
        ]
        kept_labels = [
            item for item in original_labels
            if str(item.get("trace_id", "") or "") in allowed_trace_ids
        ]
        removed_trace_ids = sorted(
            {
                str(item.get("trace_id", "") or "")
                for item in original_labels
                if str(item.get("trace_id", "") or "")
                and str(item.get("trace_id", "") or "") not in allowed_trace_ids
            }
        )
        filtered["step_labels"] = kept_labels
        report["removed_trace_ids"] = removed_trace_ids
        report["removed_step_labels"] = len(original_labels) - len(kept_labels)

        if isinstance(filtered.get("skipped_steps"), list):
            skipped = [item for item in filtered["skipped_steps"] if isinstance(item, dict)]
            kept_skipped = [
                item for item in skipped
                if str(item.get("trace_id", "") or "") in allowed_trace_ids
            ]
            filtered["skipped_steps"] = kept_skipped
            report["removed_skipped_steps"] = len(skipped) - len(kept_skipped)

        referenced_pages = {
            str(
                item.get("page_cluster_id")
                or item.get("general_page_id")
                or item.get("source_page_cluster_id")
                or item.get("materialized_page_id")
                or ""
            ).strip()
            for item in kept_labels
        }
        referenced_pages.discard("")

        kept_routes = []
        route_endpoint_pages = set()
        original_routes = [
            item for item in filtered.get("canonical_routes", [])
            if isinstance(item, dict)
        ]
        for route in original_routes:
            evidence = route.get("evidence_steps")
            if isinstance(evidence, list):
                kept_evidence = [
                    ref for ref in evidence
                    if cls._trace_id_from_evidence_ref(ref) in allowed_trace_ids
                ]
                route["evidence_steps"] = kept_evidence
                if not kept_evidence:
                    continue
            from_page = str(route.get("from_page_cluster_id", "") or "").strip()
            to_page = str(route.get("to_page_cluster_id", "") or "").strip()
            if not evidence and (from_page or to_page):
                if from_page not in referenced_pages or to_page not in referenced_pages:
                    continue
            kept_routes.append(route)
            route_endpoint_pages.update(x for x in (from_page, to_page) if x)
        filtered["canonical_routes"] = kept_routes
        report["removed_routes"] = len(original_routes) - len(kept_routes)

        keep_pages = referenced_pages | route_endpoint_pages
        original_pages = [
            item for item in filtered.get("canonical_pages", [])
            if isinstance(item, dict)
        ]
        if keep_pages and original_pages:
            kept_pages = [
                page for page in original_pages
                if str(page.get("page_cluster_id", "") or "").strip() in keep_pages
            ]
            filtered["canonical_pages"] = kept_pages
            report["removed_pages"] = len(original_pages) - len(kept_pages)

        report["filtered"] = any(
            int(report.get(key, 0) or 0) > 0
            for key in (
                "removed_step_labels",
                "removed_skipped_steps",
                "removed_routes",
                "removed_pages",
            )
        )
        return filtered, report

    def _write_global_reasoning_trace_filter_report(self, output_dir: str, report: Dict[str, Any]) -> None:
        if not output_dir or not report.get("filtered"):
            return
        path = str(self.pom.debug("global_reasoning", "global_reasoning_trace_filter.json"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def _load_existing_global_reasoning(
        self,
        output_dir: str,
        allowed_trace_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        explicit_path = str(getattr(self, "global_reasoning_input_path", "") or "").strip()
        if explicit_path:
            payload = self._read_json_file_safely(explicit_path, {})
            if (
                isinstance(payload, dict)
                and payload.get("step_labels")
                and payload.get("canonical_pages")
            ):
                payload, report = self._filter_global_reasoning_to_trace_ids(
                    payload,
                    allowed_trace_ids or set(),
                )
                self._write_global_reasoning_trace_filter_report(output_dir, report)
                if report.get("filtered") and self.logger:
                    self.logger.log_info(
                        "[SM-P2/GR] Filtered replayed global reasoning to current trace set: "
                        f"removed {report.get('removed_step_labels', 0)} step labels, "
                        f"{report.get('removed_routes', 0)} routes, "
                        f"{report.get('removed_pages', 0)} pages from "
                        f"{report.get('removed_trace_ids', [])}"
                    )
                return payload
        if not output_dir:
            return {}
        candidates = [
            str(self.pom.formal("global_reasoning", "global_reasoning_final.json")) if self.pom
            else os.path.join(output_dir, "phase_global_reasoning", "global_reasoning_final.json"),
        ]
        for path in candidates:
            if not os.path.exists(path):
                continue
            payload = self._read_json_file_safely(path, {})
            if not isinstance(payload, dict):
                continue
            if not payload.get("step_labels") or not payload.get("canonical_pages"):
                continue
            payload, report = self._filter_global_reasoning_to_trace_ids(
                payload,
                allowed_trace_ids or set(),
            )
            self._write_global_reasoning_trace_filter_report(output_dir, report)
            if report.get("filtered") and self.logger:
                self.logger.log_info(
                    "[SM-P2/GR] Filtered existing global reasoning to current trace set: "
                    f"removed {report.get('removed_step_labels', 0)} step labels, "
                    f"{report.get('removed_routes', 0)} routes, "
                    f"{report.get('removed_pages', 0)} pages from "
                    f"{report.get('removed_trace_ids', [])}"
                )
            return payload
        return {}

    def _load_existing_instance_planning(self, output_dir: str) -> Dict[str, Any]:
        if not output_dir:
            return {}
        path = (
            str(self.pom.formal("instance_splitting", "instance_plan.json"))
            if self.pom
            else os.path.join(output_dir, "phase_instance_splitting", "instance_plan.json")
        )
        if not os.path.exists(path):
            return {}
        payload = self._read_json_file_safely(path, {})
        if isinstance(payload, dict) and payload:
            return payload
        return {}

    @staticmethod
    def _existing_global_reasoning_covers_retained_observations(
        payload: Dict[str, Any],
        observations: List[FrameObservation],
    ) -> Tuple[bool, List[str]]:
        labeled: set[str] = set()
        for item in payload.get("step_labels", []):
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except (TypeError, ValueError):
                continue
            if trace_id and step_index >= 0:
                labeled.add(f"{trace_id}:{step_index}")
        required = {
            f"{obs.trace_id}:{obs.step_index}"
            for obs in observations
            if isinstance(getattr(obs, "page_state", None), dict)
            and str(obs.page_state.get("observation_role", "")).strip()
            == "terminal_observation"
            and str(obs.page_state.get("observation_disposition", "")).strip()
            == "retain"
        }
        missing = sorted(required - labeled)
        return not missing, missing

    def _existing_global_reasoning_covers_route_occurrences(
        self,
        payload: Dict[str, Any],
        traces: List[TraceData],
    ) -> Tuple[bool, List[str]]:
        expected_routes = self._derive_routes_from_step_labels(
            payload.get("step_labels", []),
            traces,
            skipped_steps=payload.get("skipped_steps", []),
        )
        expected_by_key = {
            (
                str(route.get("from_page_cluster_id", "")).strip(),
                str(route.get("to_page_cluster_id", "")).strip(),
                str(route.get("trigger", "")).strip(),
            ): {
                str(step).strip()
                for step in route.get("evidence_steps", [])
                if str(step).strip()
            }
            for route in expected_routes
            if isinstance(route, dict)
        }
        actual_by_key: Dict[Tuple[str, str, str], set[str]] = {}
        for route in payload.get("canonical_routes", []):
            if not isinstance(route, dict):
                continue
            key = (
                str(route.get("from_page_cluster_id", "")).strip(),
                str(route.get("to_page_cluster_id", "")).strip(),
                str(route.get("trigger", "")).strip(),
            )
            actual_by_key.setdefault(key, set()).update(
                str(step).strip()
                for step in route.get("evidence_steps", [])
                if str(step).strip()
            )

        missing = [
            f"{step} ({key[0]} -> {key[1]} via {key[2]})"
            for key, expected_steps in expected_by_key.items()
            for step in sorted(expected_steps - actual_by_key.get(key, set()))
        ]
        return not missing, missing

    def _chunk_content_extraction_observations(
        self,
        cluster_obs: List[FrameObservation],
        max_screenshots: Optional[int] = None,
    ) -> List[List[FrameObservation]]:
        limit = int(max_screenshots or self.CONTENT_EXTRACTION_MAX_SCREENSHOTS)
        if limit < 1:
            limit = self.CONTENT_EXTRACTION_MAX_SCREENSHOTS

        chunks: List[List[FrameObservation]] = []
        current: List[FrameObservation] = []
        current_paths = set()

        for obs in cluster_obs:
            screenshot_path = str(getattr(obs, "screenshot_path", "") or "")
            adds_unique_path = bool(screenshot_path) and screenshot_path not in current_paths
            if current and adds_unique_path and len(current_paths) >= limit:
                chunks.append(current)
                current = []
                current_paths = set()
            current.append(obs)
            if screenshot_path:
                current_paths.add(screenshot_path)

        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _append_unique(values: List[Any], source_values: Any) -> None:
        if not isinstance(source_values, list):
            source_values = [source_values]
        for item in source_values:
            if item not in values:
                values.append(item)

    def _merge_content_extraction_payloads(
        self,
        payloads: List[Dict[str, Any]],
        lifecycle_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        valid_payloads = [
            self._apply_content_lifecycle_filters(
                normalize_content_extraction_payload(payload),
                lifecycle_context or {},
            )
            for payload in payloads if isinstance(payload, dict) and payload
        ]
        if not valid_payloads:
            return {}
        if len(valid_payloads) == 1:
            return dict(valid_payloads[0])

        merged: Dict[str, Any] = {
            "extracted_items": {"_metadata": {}},
            "runtime_items": {},
            "excluded_from_initial": [],
            "item_lifecycle": [],
            "entity_component_mapping": [],
            "entity_task_relevance": {},
        }
        descriptions: List[str] = []

        for payload in valid_payloads:
            extracted_items = payload.get("extracted_items", {})
            if isinstance(extracted_items, dict):
                for entity_name, items in extracted_items.items():
                    if entity_name == "_metadata":
                        metadata = merged["extracted_items"].setdefault("_metadata", {})
                        if isinstance(items, dict):
                            for key, value in items.items():
                                if isinstance(value, list):
                                    self._append_unique(metadata.setdefault(key, []), value)
                                elif key not in metadata:
                                    metadata[key] = value
                        continue
                    if isinstance(items, list):
                        merged["extracted_items"].setdefault(entity_name, []).extend(items)

            runtime_items = payload.get("runtime_items", {})
            if isinstance(runtime_items, dict):
                for entity_name, items in runtime_items.items():
                    if isinstance(items, list):
                        merged["runtime_items"].setdefault(entity_name, []).extend(items)

            for excluded in payload.get("excluded_from_initial", []):
                if isinstance(excluded, dict) and excluded not in merged["excluded_from_initial"]:
                    merged["excluded_from_initial"].append(excluded)

            for item_lifecycle in payload.get("item_lifecycle", []):
                if isinstance(item_lifecycle, dict) and item_lifecycle not in merged["item_lifecycle"]:
                    merged["item_lifecycle"].append(item_lifecycle)

            for mapping in payload.get("entity_component_mapping", []):
                if isinstance(mapping, dict) and mapping not in merged["entity_component_mapping"]:
                    merged["entity_component_mapping"].append(mapping)

            relevance = payload.get("entity_task_relevance", {})
            if isinstance(relevance, dict):
                for entity_name, rel in relevance.items():
                    if not isinstance(rel, dict):
                        continue
                    current = merged["entity_task_relevance"].setdefault(entity_name, dict(rel))
                    current["task_related"] = bool(current.get("task_related")) or bool(rel.get("task_related"))
                    for key in ("evidence_steps", "task_instruction_refs"):
                        current_values = current.setdefault(key, [])
                        self._append_unique(current_values, rel.get(key, []))

            description = str(payload.get("description", "") or "").strip()
            if description and description not in descriptions:
                descriptions.append(description)

        if descriptions:
            merged["description"] = "\n".join(descriptions)
        return merged

    def _validate_content_extraction_phase_complete(
        self,
        *,
        expected_clusters: set[str],
        cluster_content_obs: Dict[str, Dict[str, Any]],
        failed_jobs: List[Dict[str, Any]],
        expected_parent_jobs: set[str],
        completed_parent_jobs: set[str],
        expected_call_jobs: int,
        completed_call_jobs: int,
    ) -> None:
        observed_clusters = set(cluster_content_obs)
        missing_clusters = sorted(expected_clusters - observed_clusters)
        empty_clusters = sorted(
            cluster_id
            for cluster_id, content in cluster_content_obs.items()
            if not isinstance(content, dict) or not content
        )
        missing_parent_jobs = sorted(expected_parent_jobs - completed_parent_jobs)
        errors: Dict[str, Any] = {}
        if missing_clusters:
            errors["missing_clusters"] = missing_clusters
        if empty_clusters:
            errors["empty_clusters"] = empty_clusters
        if failed_jobs:
            errors["failed_jobs"] = failed_jobs
        if missing_parent_jobs:
            errors["missing_parent_jobs"] = missing_parent_jobs
        if completed_call_jobs != expected_call_jobs:
            errors["call_job_coverage"] = {
                "expected": expected_call_jobs,
                "completed": completed_call_jobs,
            }
        if errors:
            raise ContentExtractionPhaseError(
                "content_extraction phase incomplete: "
                + json.dumps(errors, ensure_ascii=False, sort_keys=True)
            )

    @staticmethod
    def _content_call_job_key(job: Dict[str, Any]) -> str:
        parent_key = str(job.get("parent_job_key", "") or "")
        if not parent_key:
            if job.get("job_type") == "instance":
                parent_key = (
                    f"{job.get('cluster_key', '')}:"
                    f"instance:{int(job.get('instance_order', 0))}"
                )
            else:
                parent_key = f"{job.get('cluster_key', '')}:cluster"
        return f"{parent_key}:chunk:{int(job.get('chunk_order', 0))}"

    @staticmethod
    def _format_content_extraction_planning_log(
        *,
        total_clusters: int,
        cached_clusters: int,
        planned_jobs: int,
        instance_jobs: int,
        cluster_jobs: int,
    ) -> str:
        return (
            f"[SM-P5] Planned {planned_jobs} new content extraction jobs after reusing "
            f"{cached_clusters}/{total_clusters} cached Materialized Page clusters "
            f"({instance_jobs} instance jobs, {cluster_jobs} unsplit cluster jobs)"
        )

    async def _iter_content_extraction_call_results(self, content_call_jobs, run_job_checked):
        limit = max(1, int(self.max_concurrent))
        semaphore = asyncio.Semaphore(limit)

        async def _gated(job):
            async with semaphore:
                return await run_job_checked(job)

        tasks = [
            asyncio.create_task(_gated(job))
            for job in content_call_jobs
        ]
        try:
            for task in asyncio.as_completed(tasks):
                yield await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _iter_content_extraction_call_results_with_retries(
        self,
        content_call_jobs,
        run_job_checked,
        max_attempts: Optional[int] = None,
        retry_backoff_seconds: Optional[int] = None,
    ):
        attempts = max(1, int(max_attempts or self.CONTENT_EXTRACTION_CALL_MAX_ATTEMPTS))
        retry_backoff = max(
            0,
            int(
                self.CONTENT_EXTRACTION_RETRY_BACKOFF_SECONDS
                if retry_backoff_seconds is None
                else retry_backoff_seconds
            ),
        )
        pending_jobs = list(content_call_jobs)
        successful_job_keys: set[str] = set()

        for attempt in range(1, attempts + 1):
            if not pending_jobs:
                return
            retry_jobs = []
            async for job, content, error in self._iter_content_extraction_call_results(
                pending_jobs,
                run_job_checked,
            ):
                call_key = self._content_call_job_key(job)
                if call_key in successful_job_keys:
                    continue
                if error is None:
                    successful_job_keys.add(call_key)
                    yield job, content, None, attempt
                    continue
                if attempt < attempts:
                    retry_jobs.append(job)
                    continue
                yield job, {}, error, attempt
            pending_jobs = retry_jobs
            if pending_jobs and attempt < attempts and retry_backoff:
                if self.logger:
                    self.logger.log_warning(
                        "[SM-P5] Retrying "
                        f"{len(pending_jobs)} failed content extraction chunks "
                        f"after {retry_backoff}s cooldown-aware backoff "
                        f"(attempt {attempt + 1}/{attempts})"
                    )
                await asyncio.sleep(retry_backoff)

    async def _reduce_content_extraction_payloads(
        self,
        payloads: List[Dict[str, Any]],
        page_context: Dict[str, str],
        parent_job_key: str = "",
        lifecycle_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        valid_payloads = [
            self._apply_content_lifecycle_filters(payload, lifecycle_context or {})
            for payload in payloads if isinstance(payload, dict) and payload
        ]
        if not valid_payloads:
            return {}
        if len(valid_payloads) == 1:
            return dict(valid_payloads[0])

        result = self._merge_content_extraction_payloads(
            valid_payloads,
            lifecycle_context=lifecycle_context,
        )
        if isinstance(result, dict) and isinstance(result.get("extracted_items", {}), dict):
            return self._apply_content_lifecycle_filters(result, lifecycle_context or {})
        page_context = page_context if isinstance(page_context, dict) else {}
        raise ContentExtractionPhaseError(
            "content_extraction deterministic reduce returned invalid payload for "
            f"{parent_job_key or page_context.get('page_cluster_id', '?')}"
        )

    def _build_instance_groups_from_planning(
        self,
        cluster_key: str,
        cluster_obs: List[FrameObservation],
        planning_entry: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not isinstance(planning_entry, dict):
            return []
        groups = planning_entry.get("instance_groups", [])
        if not isinstance(groups, list):
            return []
        obs_lookup = {f"{obs.trace_id}:{obs.step_index}": obs for obs in cluster_obs}
        instance_groups: List[Dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            obs_keys = [str(key).strip() for key in group.get("obs_keys", []) if str(key).strip()]
            group_obs = [obs_lookup[key] for key in obs_keys if key in obs_lookup]
            if not group_obs:
                continue
            instance_group = {
                "instance_key": str(group.get("instance_key", "")).strip(),
                "instance_label": str(group.get("instance_label", "")).strip(),
                "trace_ids": [
                    str(trace_id).strip()
                    for trace_id in group.get("trace_ids", [])
                    if str(trace_id).strip()
                ],
                "obs_keys": obs_keys,
                "cluster_obs": group_obs,
            }
            split_dimension = str(group.get("split_dimension", "") or "").strip()
            split_reason = group.get("split_reason", {})
            if split_dimension:
                instance_group["split_dimension"] = split_dimension
            if isinstance(split_reason, dict) and split_reason:
                instance_group["split_reason"] = split_reason
            if isinstance(group.get("state_params_by_obs_key"), dict):
                instance_group["state_params_by_obs_key"] = dict(group.get("state_params_by_obs_key") or {})
            if isinstance(group.get("parameterized_state_variants"), list):
                instance_group["parameterized_state_variants"] = list(group.get("parameterized_state_variants") or [])
                instance_group["parameterized_state_variant_count"] = len(instance_group["parameterized_state_variants"])
            instance_groups.append(instance_group)
        return self._filter_out_of_general_page_instance_groups(cluster_key, instance_groups)

    def _require_complete_instance_plan_for_content_extraction(
        self,
        instance_planning: Dict[str, Any],
        clusters: Dict[str, List[FrameObservation]],
    ) -> None:
        missing = [
            cluster_key
            for cluster_key in sorted(clusters)
            if not isinstance(instance_planning.get(cluster_key), dict)
        ]
        if missing:
            sample = ", ".join(missing[:8])
            raise RuntimeError(
                "content extraction requires formal "
                "phase_instance_splitting/instance_plan.json before planning jobs; "
                f"missing cluster entries: {sample}"
            )

    @classmethod
    def _validate_reusable_instance_planning_entry(
        cls,
        cluster_key: str,
        planning_entry: Dict[str, Any],
        cluster_obs: List[FrameObservation],
        state_authority_by_obs_key: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
        traces: Optional[List[TraceData]] = None,
    ) -> List[str]:
        errors: List[str] = []
        expected_keys = [cls._obs_key(obs) for obs in cluster_obs]
        expected_set = set(expected_keys)
        eligible_entry_obs_keys = set(
            cls._instance_split_entry_action_obs_keys_from_observations(
                cluster_obs,
                traces or [],
            )
        )
        if len(expected_keys) != len(expected_set):
            errors.append("cluster observations contain duplicate obs_keys")
        if not isinstance(planning_entry, dict):
            return ["planning entry must be an object"]
        state_params_by_obs_key = planning_entry.get("state_params_by_obs_key", {})
        if state_params_by_obs_key and not isinstance(state_params_by_obs_key, dict):
            errors.append("state_params_by_obs_key must be an object")
        elif isinstance(state_params_by_obs_key, dict):
            unknown_state_keys = sorted(
                set(str(key) for key in state_params_by_obs_key)
                - expected_set
            )
            if unknown_state_keys:
                errors.append(
                    f"state_params_by_obs_key has unknown obs_keys: {unknown_state_keys}"
                )
        groups = planning_entry.get("instance_groups", [])
        if not isinstance(groups, list):
            return ["instance_groups must be a list"]
        if not groups:
            return errors

        assignment_counts: Dict[str, int] = {}
        seen_instance_keys: set[str] = set()
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                errors.append(f"instance_groups[{index}] must be an object")
                continue
            instance_key = str(group.get("instance_key", "") or "").strip()
            if not instance_key:
                errors.append(f"instance_groups[{index}] missing instance_key")
            elif instance_key in seen_instance_keys:
                errors.append(f"duplicate instance_key: {instance_key}")
            seen_instance_keys.add(instance_key)
            obs_keys = [
                str(value or "").strip()
                for value in group.get("obs_keys", []) or []
                if str(value or "").strip()
            ]
            if not obs_keys:
                errors.append(
                    f"instance_groups[{index}] {instance_key or '<missing>'} has no obs_keys"
                )
            if len(obs_keys) != len(set(obs_keys)):
                errors.append(
                    f"instance_groups[{index}] {instance_key or '<missing>'} has duplicate obs_keys"
                )
            for obs_key in obs_keys:
                assignment_counts[obs_key] = assignment_counts.get(obs_key, 0) + 1
            split_reason = group.get("split_reason", {})
            if isinstance(split_reason, dict) and split_reason.get("same_general_page") is False:
                errors.append(
                    f"instance_groups[{index}] {instance_key or '<missing>'} "
                    "has same_general_page=false"
                )
            split_dimension = str(
                group.get("split_dimension", "")
                or (
                    split_reason.get("dimension", "")
                    if isinstance(split_reason, dict)
                    else ""
                )
                or ""
            ).strip().lower()
            if (
                split_dimension in {
                    "semantic_mode",
                    "state_variant",
                    "lifecycle_variant",
                }
                and state_authority_by_obs_key
                and any(
                    state_authority_by_obs_key.get(obs_key)
                    for obs_key in obs_keys
                )
            ):
                errors.append(
                    f"instance_groups[{index}] {instance_key or '<missing>'} "
                    f"uses parameterizable {split_dimension} despite runtime-state "
                    "authority; parameterize the component state or use a specific "
                    "durable mode dimension backed by primary-content-universe "
                    "evidence"
                )
            if (
                split_dimension
                != "fixed_capacity_task_content"
            ):
                continue
            rows = (
                split_reason.get("member_action_audit", [])
                if isinstance(split_reason, dict)
                else []
            )
            if not isinstance(rows, list):
                rows = []
            row_keys = [
                str(row.get("obs_key", "") or "").strip()
                for row in rows
                if isinstance(row, dict)
                and str(row.get("obs_key", "") or "").strip()
            ]
            expected_member_audit_keys = set(obs_keys) & eligible_entry_obs_keys
            if (
                len(row_keys) != len(set(row_keys))
                or set(row_keys) != expected_member_audit_keys
            ):
                errors.append(
                    f"instance_groups[{index}] {instance_key or '<missing>'} "
                    "member_action_audit must exactly cover machine-eligible member obs_keys"
                )
            visible_entities_by_slot: Dict[str, set[str]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                obs_key = str(row.get("obs_key", "") or "").strip()
                if str(row.get("target_role", "") or "").strip() != "task_content":
                    errors.append(
                        f"instance_groups[{index}] {instance_key or '<missing>'} "
                        f"has non-task-content member {obs_key or '<missing>'}"
                    )
                bounded_slot = str(
                    row.get("bounded_slot", "") or "none"
                ).strip().lower()
                visible_entity = cls._normalized_instance_split_text(
                    row.get("visible_entity", "")
                ).strip()
                if bounded_slot not in ("", "none"):
                    if not visible_entity:
                        errors.append(
                            f"instance_groups[{index}] {instance_key or '<missing>'} "
                            f"member {obs_key or '<missing>'} has bounded slot without visible_entity"
                        )
                    else:
                        visible_entities_by_slot.setdefault(
                            bounded_slot,
                            set(),
                        ).add(visible_entity)
            for bounded_slot, visible_entities in sorted(
                visible_entities_by_slot.items()
            ):
                if len(visible_entities) > 1:
                    errors.append(
                        f"instance_groups[{index}] {instance_key or '<missing>'} "
                        f"merges incompatible visible entities in bounded_slot={bounded_slot}: "
                        f"{sorted(visible_entities)}"
                    )

        missing = sorted(
            obs_key
            for obs_key in expected_set
            if assignment_counts.get(obs_key, 0) == 0
        )
        duplicates = {
            obs_key: assignment_counts.get(obs_key, 0)
            for obs_key in sorted(expected_set)
            if assignment_counts.get(obs_key, 0) > 1
        }
        unknown = sorted(set(assignment_counts) - expected_set)
        if missing:
            errors.append(f"missing cluster obs_keys: {missing}")
        if duplicates:
            errors.append(f"duplicate cluster assignments: {duplicates}")
        if unknown:
            errors.append(f"unknown cluster obs_keys: {unknown}")
        return errors

    def _admit_reusable_instance_planning(
        self,
        existing_instance_planning: Dict[str, Any],
        clusters: Dict[str, List[FrameObservation]],
        state_authority_by_obs_key: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
        traces: Optional[List[TraceData]] = None,
        producer_candidates_by_cluster: Optional[
            Dict[str, Dict[str, Any]]
        ] = None,
        boundary_preservation_constraints_by_cluster: Optional[
            Dict[str, Dict[str, Any]]
        ] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
        admitted: Dict[str, Dict[str, Any]] = {}
        rejected: Dict[str, List[str]] = {}
        for cluster_key in sorted(clusters):
            planning_entry = existing_instance_planning.get(cluster_key)
            if not isinstance(planning_entry, dict):
                continue
            errors = self._validate_reusable_instance_planning_entry(
                cluster_key,
                planning_entry,
                clusters[cluster_key],
                state_authority_by_obs_key=state_authority_by_obs_key,
                traces=traces,
            )
            producer_candidate = (producer_candidates_by_cluster or {}).get(
                cluster_key
            )
            if isinstance(producer_candidate, dict):
                errors.extend(
                    self._validate_instance_split_opened_overlay_group_contract(
                        producer_candidate,
                        clusters[cluster_key],
                    )
                )
            boundary_constraint = (
                boundary_preservation_constraints_by_cluster or {}
            ).get(cluster_key)
            if isinstance(boundary_constraint, dict) and boundary_constraint:
                groups = planning_entry.get("instance_groups", [])
                errors.extend(
                    self._validate_instance_split_boundary_preservation_constraint(
                        {
                            "split_needed": bool(groups),
                            "instances": groups if isinstance(groups, list) else [],
                        },
                        boundary_constraint,
                    )
                )
            if errors:
                rejected[cluster_key] = errors
            else:
                admitted[cluster_key] = dict(planning_entry)
        return admitted, rejected

    @staticmethod
    def _load_instance_split_audit_repair_constraints(
        output_dir: str,
        clusters: Dict[str, List[FrameObservation]],
    ) -> Dict[str, Dict[str, Any]]:
        if not output_dir:
            return {}
        audit_path = (
            Path(output_dir)
            / "run_management"
            / "reference_page_split_audit_after_instance_splitting.json"
        )
        if not audit_path.is_file():
            return {}
        try:
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot read instance-splitting boundary audit: {audit_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("instance-splitting boundary audit must be an object")
        if (
            payload.get("schema") != "trace2world.reference_page_split_audit.v1"
            or payload.get("boundary") != "after_instance_splitting"
        ):
            raise RuntimeError(
                "instance-splitting boundary audit has incompatible schema/boundary"
            )
        status = str(payload.get("status", "") or "").strip().lower()
        if status == "pass":
            plan_binding = payload.get("instance_plan", {})
            if not isinstance(plan_binding, dict):
                raise RuntimeError(
                    "passed instance-splitting audit lacks instance_plan binding"
                )
            plan_path = Path(str(plan_binding.get("path", "") or ""))
            expected_plan_sha = str(
                plan_binding.get("sha256", "") or ""
            ).removeprefix("sha256:")
            if not plan_path.is_absolute() or not plan_path.is_file():
                raise RuntimeError(
                    f"passed instance-splitting audit plan is missing: {plan_path}"
                )
            actual_plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            if not expected_plan_sha or actual_plan_sha != expected_plan_sha:
                raise RuntimeError(
                    "passed instance-splitting audit plan digest mismatch"
                )

            resolved_rows = payload.get("resolved_repairs", [])
            if not isinstance(resolved_rows, list):
                raise RuntimeError("resolved_repairs must be a list")
            constraints: Dict[str, Dict[str, Any]] = {}
            for index, row in enumerate(resolved_rows):
                if not isinstance(row, dict):
                    raise RuntimeError(
                        f"resolved_repairs[{index}] must be an object"
                    )
                cluster_id = str(row.get("cluster_id", "") or "").strip()
                if cluster_id not in clusters:
                    raise RuntimeError(
                        f"resolved_repairs[{index}] has unknown cluster_id={cluster_id!r}"
                    )
                safe_key = re.sub(
                    r"[^A-Za-z0-9_.-]+", "_", cluster_id or "cluster"
                )
                accepted_manifest_path = (
                    Path(output_dir)
                    / "phase_instance_splitting"
                    / "debug"
                    / "lossless_context"
                    / safe_key
                    / "accepted"
                    / "manifest.json"
                )
                try:
                    accepted_manifest = json.loads(
                        accepted_manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "passed boundary repair lacks its accepted producer "
                        f"manifest: {accepted_manifest_path}: {exc}"
                    ) from exc
                if (
                    not isinstance(accepted_manifest, dict)
                    or accepted_manifest.get("producer_contract")
                    != "complete_lossless_producer"
                    or int(
                        accepted_manifest.get("producer_contract_version", 0)
                        or 0
                    )
                    != AppSpecBuilder.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                    or accepted_manifest.get("contract_errors") != []
                    or str(accepted_manifest.get("cluster_key", "") or "").strip()
                    != cluster_id
                ):
                    raise RuntimeError(
                        "passed boundary repair has an incompatible accepted "
                        f"producer manifest for {cluster_id}"
                    )
                constraint = accepted_manifest.get(
                    "boundary_preservation_constraint", {}
                )
                if not isinstance(constraint, dict) or not constraint:
                    raise RuntimeError(
                        f"passed boundary repair lacks its bound constraint for {cluster_id}"
                    )
                if Path(
                    str(constraint.get("audit_path", "") or "")
                ).resolve() != audit_path.resolve():
                    raise RuntimeError(
                        f"passed boundary repair audit binding drifted for {cluster_id}"
                    )
                if str(
                    row.get("boundary_decision", "") or ""
                ).strip() != str(
                    constraint.get("boundary_decision", "") or ""
                ).strip():
                    raise RuntimeError(
                        f"passed boundary repair decision drifted for {cluster_id}"
                    )
                minimum = int(
                    constraint.get("minimum_instance_count", 0) or 0
                )
                if int(row.get("formal_instance_count", 0) or 0) < minimum:
                    raise RuntimeError(
                        f"passed boundary repair instance count regressed for {cluster_id}"
                    )
                anchor_owners = row.get("anchor_owners", {})
                if not isinstance(anchor_owners, dict):
                    raise RuntimeError(
                        f"passed boundary repair lacks anchor owners for {cluster_id}"
                    )
                group_owners: List[str] = []
                for group_index, group in enumerate(
                    constraint.get("must_separate_obs_key_groups", []) or []
                ):
                    owners = {
                        str(anchor_owners.get(str(obs_key), "") or "").strip()
                        for obs_key in group
                    } if isinstance(group, list) else set()
                    owners.discard("")
                    if len(owners) != 1:
                        raise RuntimeError(
                            "passed boundary repair has incomplete/mixed anchor "
                            f"ownership for {cluster_id} group {group_index}"
                        )
                    group_owners.append(next(iter(owners)))
                if len(group_owners) < 2 or len(set(group_owners)) != len(
                    group_owners
                ):
                    raise RuntimeError(
                        f"passed boundary repair collapsed anchor owners for {cluster_id}"
                    )
                constraints[cluster_id] = dict(constraint)
            return constraints
        if status not in {"blocked", "repairing"}:
            raise RuntimeError(
                "instance-splitting boundary audit must be pass, blocked, or repairing"
            )
        rows = payload.get("required_instance_plan_repairs", [])
        if not isinstance(rows, list):
            raise RuntimeError("required_instance_plan_repairs must be a list")
        constraints: Dict[str, Dict[str, Any]] = {}
        allowed_decisions = {"materialized_page_split", "general_page_split"}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] must be an object"
                )
            cluster_id = str(row.get("cluster_id", "") or "").strip()
            if cluster_id not in clusters:
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] has unknown cluster_id={cluster_id!r}"
                )
            decision = str(row.get("boundary_decision", "") or "").strip()
            if decision not in allowed_decisions:
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] needs a split decision"
                )
            minimum = int(row.get("minimum_instance_count", 0) or 0)
            dimensions = sorted(
                {
                    str(value or "").strip()
                    for value in row.get("required_split_dimensions", []) or []
                    if str(value or "").strip()
                }
            )
            if minimum < 2 or not dimensions:
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] lacks a durable split contract"
                )
            separation_groups = row.get("must_separate_obs_key_groups", [])
            if (
                not isinstance(separation_groups, list)
                or len(separation_groups) < 2
                or any(not isinstance(group, list) or not group for group in separation_groups)
            ):
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] lacks positive separation anchors"
                )
            expected_obs_keys = {
                AppSpecBuilder._obs_key(obs) for obs in clusters[cluster_id]
            }
            normalized_groups: List[List[str]] = []
            seen_anchor_keys: set[str] = set()
            for group_index, group in enumerate(separation_groups):
                normalized = [
                    str(value or "").strip()
                    for value in group
                    if str(value or "").strip()
                ]
                if (
                    len(normalized) != len(group)
                    or len(normalized) != len(set(normalized))
                ):
                    raise RuntimeError(
                        f"repair[{index}] separation group {group_index} has invalid/duplicate obs_keys"
                    )
                unknown = sorted(set(normalized) - expected_obs_keys)
                overlap = sorted(set(normalized) & seen_anchor_keys)
                if unknown or overlap:
                    raise RuntimeError(
                        f"repair[{index}] separation group {group_index} has unknown={unknown}, overlap={overlap}"
                    )
                seen_anchor_keys.update(normalized)
                normalized_groups.append(normalized)
            evidence_refs = row.get("evidence_refs", [])
            if not isinstance(evidence_refs, list) or not evidence_refs:
                raise RuntimeError(
                    f"required_instance_plan_repairs[{index}] lacks evidence_refs"
                )
            for evidence_index, evidence in enumerate(evidence_refs):
                if not isinstance(evidence, dict):
                    raise RuntimeError(
                        f"repair[{index}].evidence_refs[{evidence_index}] must be an object"
                    )
                evidence_path = Path(str(evidence.get("path", "") or ""))
                expected_sha = str(evidence.get("sha256", "") or "").removeprefix(
                    "sha256:"
                )
                if not evidence_path.is_absolute() or not evidence_path.is_file():
                    raise RuntimeError(
                        f"repair[{index}] evidence path is missing: {evidence_path}"
                    )
                actual_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                if not expected_sha or actual_sha != expected_sha:
                    raise RuntimeError(
                        f"repair[{index}] evidence digest mismatch: {evidence_path}"
                    )
            if cluster_id in constraints:
                raise RuntimeError(
                    f"duplicate boundary repair constraint for cluster {cluster_id}"
                )
            constraints[cluster_id] = {
                "source": "human_reviewed_after_instance_boundary_audit",
                "audit_path": str(audit_path.resolve()),
                "boundary_decision": decision,
                "minimum_instance_count": minimum,
                "required_split_dimensions": dimensions,
                "must_separate_obs_key_groups": normalized_groups,
                "selection_basis": str(
                    row.get("selection_basis", "") or ""
                ).strip(),
            }
        return constraints

    @staticmethod
    def _load_existing_instance_split_producer_candidate(
        output_dir: str,
        cluster_key: str,
    ) -> Optional[Dict[str, Any]]:
        if not output_dir:
            return None
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(cluster_key or "cluster"))
        root = (
            Path(output_dir)
            / "phase_instance_splitting"
            / "debug"
            / "lossless_context"
            / safe_key
        )
        accepted_manifest_path = root / "accepted" / "manifest.json"
        accepted_is_current = False
        if accepted_manifest_path.is_file():
            try:
                accepted_manifest = json.loads(
                    accepted_manifest_path.read_text(encoding="utf-8")
                )
                accepted_is_current = (
                    isinstance(accepted_manifest, dict)
                    and int(
                        accepted_manifest.get("producer_contract_version", 0) or 0
                    )
                    == AppSpecBuilder.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                )
            except (OSError, ValueError, json.JSONDecodeError):
                accepted_is_current = False
        paths = [root / "single" / "output.json"]
        if accepted_is_current:
            paths.insert(0, root / "accepted" / "output.json")
        for path in paths:
            if not path.is_file():
                continue
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                return candidate
        return None

    def _load_reusable_instance_split_accepted_candidate(
        self,
        cluster_key: str,
        steps_payload: List[Dict[str, Any]],
        boundary_preservation_constraint: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        root = self._instance_split_lossless_debug_dir(cluster_key)
        if root is None:
            return None
        accepted_dir = root / "accepted"
        manifest_path = accepted_dir / "manifest.json"
        output_path = accepted_dir / "output.json"
        input_rows_path = root / "input_rows.json"
        if not (
            manifest_path.is_file()
            and output_path.is_file()
            and input_rows_path.is_file()
        ):
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            candidate = json.loads(output_path.read_text(encoding="utf-8"))
            persisted_input_rows = json.loads(
                input_rows_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict) or not isinstance(candidate, dict):
            return None
        if (
            str(manifest.get("cluster_key", "") or "").strip()
            != str(cluster_key or "").strip()
            or int(manifest.get("producer_contract_version", 0) or 0)
            != self.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
            or manifest.get("producer_contract") != "complete_lossless_producer"
            or manifest.get("contract_errors") != []
        ):
            return None
        if self._stable_json_fingerprint(persisted_input_rows) != (
            self._stable_json_fingerprint(steps_payload)
        ):
            return None
        if self._stable_json_fingerprint(
            manifest.get("boundary_preservation_constraint", {})
        ) != self._stable_json_fingerprint(
            boundary_preservation_constraint or {}
        ):
            return None
        self._write_instance_split_debug_json(
            accepted_dir / "reuse_validation.json",
            {
                "cluster_key": cluster_key,
                "producer_contract_version": (
                    self.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                ),
                "input_rows_hash": self._stable_json_fingerprint(steps_payload),
                "boundary_preservation_constraint_hash": (
                    self._stable_json_fingerprint(
                        boundary_preservation_constraint or {}
                    )
                ),
                "accepted_output_hash": self._stable_json_fingerprint(candidate),
                "validation_state": "pending_normal_raw_postprocess_formal_validation",
            },
        )
        return candidate

    def _save_instance_plan_seed_admission(
        self,
        output_dir: str,
        admitted: Dict[str, Dict[str, Any]],
        rejected: Dict[str, List[str]],
        clusters: Dict[str, List[FrameObservation]],
    ) -> None:
        if not output_dir:
            return
        path = (
            str(
                self.pom.formal(
                    "instance_splitting",
                    "instance_plan_seed_admission.json",
                )
            )
            if self.pom
            else os.path.join(
                output_dir,
                "phase_instance_splitting",
                "instance_plan_seed_admission.json",
            )
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "format": "instance_plan_seed_admission",
            "producer": "AppSpecBuilder._admit_reusable_instance_planning",
            "cluster_count": len(clusters),
            "admitted_cluster_count": len(admitted),
            "rejected_cluster_count": len(rejected),
            "admitted_cluster_ids": sorted(admitted),
            "rejected_clusters": [
                {
                    "cluster_id": cluster_key,
                    "problems": problems,
                }
                for cluster_key, problems in sorted(rejected.items())
            ],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

    def _load_selected_instance_compile_binding(
        self,
        instance_planning: Dict[str, Dict[str, Any]],
        clusters: Optional[Dict[str, List[FrameObservation]]] = None,
    ) -> Optional[Dict[str, Any]]:
        manifest_path_value = self.selected_instance_compile_manifest_path
        if not manifest_path_value:
            return None
        if self.pom is None:
            raise ValueError("selected-instance compile requires an output directory")
        manifest_path = Path(manifest_path_value).expanduser().resolve()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"cannot read selected-instance compile manifest {manifest_path}: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise ValueError("selected-instance compile manifest must be an object")
        if manifest.get("schema") != "ghostweb.selected-instance-compile.v1":
            raise ValueError("selected-instance compile manifest has incompatible schema")
        if manifest.get("failure_policy") != "hard_fail":
            raise ValueError("selected-instance compile requires failure_policy=hard_fail")
        if manifest.get("preserve_unselected_members") is not True:
            raise ValueError(
                "selected-instance compile requires preserve_unselected_members=true"
            )

        plan_binding = manifest.get("instance_plan", {})
        if not isinstance(plan_binding, dict):
            raise ValueError("selected-instance compile instance_plan must be an object")
        canonical_plan_path = self.pom.formal(
            "instance_splitting", "instance_plan.json"
        ).resolve()
        bound_plan_path = Path(
            str(plan_binding.get("path", "") or "")
        ).expanduser().resolve()
        if bound_plan_path != canonical_plan_path:
            raise ValueError(
                "selected-instance compile must bind the current canonical instance plan"
            )
        actual_plan_sha = hashlib.sha256(canonical_plan_path.read_bytes()).hexdigest()
        expected_plan_sha = str(plan_binding.get("sha256", "") or "").removeprefix(
            "sha256:"
        )
        if actual_plan_sha != expected_plan_sha:
            raise ValueError("selected-instance compile instance-plan SHA mismatch")

        full_cluster_count = len(instance_planning)
        full_instance_pairs = {
            (str(cluster_id), str(group.get("instance_key", "") or "").strip())
            for cluster_id, entry in instance_planning.items()
            if isinstance(entry, dict)
            for group in entry.get("instance_groups", []) or []
            if isinstance(group, dict)
            and str(group.get("instance_key", "") or "").strip()
        }
        full_cluster_default_ids = {
            str(cluster_id)
            for cluster_id, entry in instance_planning.items()
            if isinstance(entry, dict)
            and not [
                group
                for group in entry.get("instance_groups", []) or []
                if isinstance(group, dict)
                and str(group.get("instance_key", "") or "").strip()
            ]
        }
        if int(manifest.get("expected_full_cluster_count", -1)) != full_cluster_count:
            raise ValueError("selected-instance compile full cluster denominator mismatch")
        if int(manifest.get("expected_full_instance_count", -1)) != len(
            full_instance_pairs
        ):
            raise ValueError("selected-instance compile full instance denominator mismatch")
        expected_full_default_count = int(
            manifest.get(
                "expected_full_cluster_default_count",
                len(full_cluster_default_ids),
            )
        )
        if expected_full_default_count != len(full_cluster_default_ids):
            raise ValueError(
                "selected-instance compile full cluster-default denominator mismatch"
            )

        selected_rows = manifest.get("selected_instances", [])
        selected_cluster_defaults_preview = manifest.get(
            "selected_cluster_defaults", []
        )
        if not isinstance(selected_rows, list) or not isinstance(
            selected_cluster_defaults_preview, list
        ):
            raise ValueError(
                "selected-instance compile selections must be lists"
            )
        if not selected_rows and not selected_cluster_defaults_preview:
            raise ValueError(
                "selected-instance compile requires at least one explicit "
                "instance or cluster-default selection"
            )
        selected_pairs = [
            (
                str(row.get("cluster_id", "") or "").strip(),
                str(row.get("instance_key", "") or "").strip(),
            )
            for row in selected_rows
            if isinstance(row, dict)
        ]
        if (
            len(selected_pairs) != len(selected_rows)
            or any(not cluster_id or not instance_key for cluster_id, instance_key in selected_pairs)
            or len(set(selected_pairs)) != len(selected_pairs)
        ):
            raise ValueError(
                "selected-instance compile rows must have unique cluster_id/instance_key pairs"
            )
        selected_pair_set = set(selected_pairs)
        unknown_pairs = sorted(selected_pair_set - full_instance_pairs)
        if unknown_pairs:
            raise ValueError(
                f"selected-instance compile references unknown instances: {unknown_pairs}"
            )
        if int(manifest.get("expected_selected_instance_count", -1)) != len(
            selected_pair_set
        ):
            raise ValueError("selected-instance compile selected denominator mismatch")

        selected_cluster_defaults = manifest.get(
            "selected_cluster_defaults", []
        )
        if not isinstance(selected_cluster_defaults, list):
            raise ValueError(
                "selected-instance compile selected_cluster_defaults must be a list"
            )
        selected_cluster_default_ids = {
            str(cluster_id or "").strip()
            for cluster_id in selected_cluster_defaults
            if str(cluster_id or "").strip()
        }
        if len(selected_cluster_default_ids) != len(selected_cluster_defaults):
            raise ValueError(
                "selected-instance compile cluster-default ids must be unique and nonempty"
            )
        unknown_cluster_defaults = sorted(
            selected_cluster_default_ids - full_cluster_default_ids
        )
        if unknown_cluster_defaults:
            raise ValueError(
                "selected-instance compile references non-default clusters: "
                f"{unknown_cluster_defaults}"
            )
        if int(
            manifest.get("expected_selected_cluster_default_count", 0)
        ) != len(selected_cluster_default_ids):
            raise ValueError(
                "selected-instance compile selected cluster-default denominator mismatch"
            )

        selected_cluster_ids = {
            cluster_id for cluster_id, _ in selected_pair_set
        } | selected_cluster_default_ids
        selected_obs_keys: set[str] = set()
        if manifest.get("require_complete_selected_clusters") is not True:
            raise ValueError(
                "selected-instance compile requires complete selected-cluster membership"
            )
        for cluster_id in selected_cluster_ids:
            cluster_pairs = {
                pair for pair in full_instance_pairs if pair[0] == cluster_id
            }
            if cluster_pairs != {
                pair for pair in selected_pair_set if pair[0] == cluster_id
            }:
                raise ValueError(
                    "selected-instance compile must select every explicit instance in "
                    f"cluster {cluster_id}"
                )
            for group in instance_planning[cluster_id].get("instance_groups", []) or []:
                if not isinstance(group, dict):
                    continue
                pair = (
                    cluster_id,
                    str(group.get("instance_key", "") or "").strip(),
                )
                if pair not in selected_pair_set:
                    continue
                selected_obs_keys.update(
                    str(obs_key or "").strip()
                    for obs_key in group.get("obs_keys", []) or []
                    if str(obs_key or "").strip()
                )
        for cluster_id in selected_cluster_default_ids:
            cluster_obs = (clusters or {}).get(cluster_id, [])
            if not cluster_obs:
                raise ValueError(
                    "selected-instance compile cluster-default observation "
                    f"membership is unavailable for {cluster_id}"
                )
            selected_obs_keys.update(
                self._obs_key(obs) for obs in cluster_obs
            )
        expected_selected_obs_count = int(
            manifest.get("expected_selected_observation_count", -1)
        )
        if expected_selected_obs_count != len(selected_obs_keys):
            raise ValueError(
                "selected-instance compile selected observation denominator mismatch"
            )

        return {
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "instance_plan_path": str(canonical_plan_path),
            "instance_plan_sha256": actual_plan_sha,
            "selected_pairs": selected_pair_set,
            "selected_cluster_default_ids": selected_cluster_default_ids,
            "selected_cluster_ids": selected_cluster_ids,
            "selected_obs_keys": selected_obs_keys,
            "full_cluster_count": full_cluster_count,
            "full_instance_count": len(full_instance_pairs),
            "selected_instance_count": len(selected_pair_set),
            "preserved_instance_count": len(full_instance_pairs - selected_pair_set),
            "full_cluster_default_count": len(full_cluster_default_ids),
            "selected_cluster_default_count": len(
                selected_cluster_default_ids
            ),
            "preserved_cluster_default_count": len(
                full_cluster_default_ids - selected_cluster_default_ids
            ),
            "stop_after": str(manifest.get("stop_after", "") or "").strip(),
        }

    @staticmethod
    def _filter_selected_instance_content_jobs(
        content_jobs: List[Dict[str, Any]],
        binding: Optional[Dict[str, Any]],
        completed_cluster_ids: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not binding:
            return content_jobs
        selected_pairs = set(binding.get("selected_pairs", set()))
        selected_cluster_default_ids = set(
            binding.get("selected_cluster_default_ids", set())
        )
        selected_jobs: List[Dict[str, Any]] = []
        observed_pairs: List[Tuple[str, str]] = []
        observed_cluster_defaults: List[str] = []
        for job in content_jobs:
            pair = (
                str(job.get("cluster_key", "") or "").strip(),
                str(
                    (job.get("instance_group", {}) or {}).get(
                        "instance_key", ""
                    )
                    or ""
                ).strip(),
            )
            job_type = job.get("job_type")
            if job_type == "cluster":
                if pair[0] not in selected_cluster_default_ids:
                    continue
                selected_jobs.append(job)
                observed_cluster_defaults.append(pair[0])
                continue
            if pair not in selected_pairs:
                continue
            if job_type != "instance":
                raise ValueError(
                    f"selected instance pair resolved to non-instance job: {pair}"
                )
            selected_jobs.append(job)
            observed_pairs.append(pair)
        if len(observed_pairs) != len(set(observed_pairs)):
            raise ValueError("selected-instance compile produced duplicate content jobs")
        if len(observed_cluster_defaults) != len(set(observed_cluster_defaults)):
            raise ValueError(
                "selected-instance compile produced duplicate cluster-default jobs"
            )
        completed_cluster_ids = set(completed_cluster_ids or set())
        completed_pairs = {
            pair for pair in selected_pairs if pair[0] in completed_cluster_ids
        }
        missing_pairs = sorted(
            selected_pairs - set(observed_pairs) - completed_pairs
        )
        if missing_pairs:
            raise ValueError(
                f"selected-instance compile did not materialize selected jobs: {missing_pairs}"
            )
        missing_cluster_defaults = sorted(
            selected_cluster_default_ids
            - set(observed_cluster_defaults)
            - completed_cluster_ids
        )
        if missing_cluster_defaults:
            raise ValueError(
                "selected-instance compile did not materialize selected "
                f"cluster-default jobs: {missing_cluster_defaults}"
            )
        return selected_jobs

    @staticmethod
    def _build_selected_instance_site_model_compile_receipt(
        binding: Dict[str, Any],
        page_prototypes: List[Any],
    ) -> Dict[str, Any]:
        selected_pairs = set(binding["selected_pairs"])
        selected_cluster_default_ids = set(
            binding.get("selected_cluster_default_ids", set())
        )
        materialized_pairs = {
            (
                str(page.structure_data.get("source_cluster_id", "") or "").strip(),
                str(page.structure_data.get("instance_key", "") or "").strip(),
            )
            for page in page_prototypes
            if isinstance(getattr(page, "structure_data", None), dict)
            and str(page.structure_data.get("source_cluster_id", "") or "").strip()
            and str(page.structure_data.get("instance_key", "") or "").strip()
        }
        if materialized_pairs != selected_pairs:
            raise ValueError(
                "selected-instance AppSpec materialization mismatch: "
                f"expected={sorted(selected_pairs)}, "
                f"materialized={sorted(materialized_pairs)}"
            )
        materialized_cluster_default_ids = {
            str(page.structure_data.get("cluster_id", "") or "").strip()
            for page in page_prototypes
            if isinstance(getattr(page, "structure_data", None), dict)
            and not str(
                page.structure_data.get("instance_key", "") or ""
            ).strip()
            and str(page.structure_data.get("cluster_id", "") or "").strip()
        }
        if materialized_cluster_default_ids != selected_cluster_default_ids:
            raise ValueError(
                "selected-instance AppSpec cluster-default materialization "
                f"mismatch: expected={sorted(selected_cluster_default_ids)}, "
                f"materialized={sorted(materialized_cluster_default_ids)}"
            )
        exact_union_count = (
            len(materialized_pairs) + binding["preserved_instance_count"]
        )
        if exact_union_count != binding["full_instance_count"]:
            raise ValueError(
                "selected-instance AppSpec exact-union conservation failed: "
                f"selected={len(materialized_pairs)}, "
                f"preserved={binding['preserved_instance_count']}, "
                f"full={binding['full_instance_count']}"
            )
        return {
            "format": "ghostweb.selected-instance-site-model-compile-receipt.v1",
            "status": "site_model_complete",
            "manifest_path": binding["manifest_path"],
            "manifest_sha256": binding["manifest_sha256"],
            "instance_plan_path": binding["instance_plan_path"],
            "instance_plan_sha256": binding["instance_plan_sha256"],
            "full_cluster_count": binding["full_cluster_count"],
            "full_instance_count": binding["full_instance_count"],
            "selected_instance_count": len(materialized_pairs),
            "selected_observation_count": len(binding["selected_obs_keys"]),
            "preserved_instance_count": binding["preserved_instance_count"],
            "exact_union_instance_count": exact_union_count,
            "full_cluster_default_count": binding.get(
                "full_cluster_default_count", 0
            ),
            "selected_cluster_default_count": len(
                materialized_cluster_default_ids
            ),
            "preserved_cluster_default_count": binding.get(
                "preserved_cluster_default_count", 0
            ),
            "materialized_instances": [
                {"cluster_id": cluster_id, "instance_key": instance_key}
                for cluster_id, instance_key in sorted(materialized_pairs)
            ],
            "materialized_cluster_defaults": sorted(
                materialized_cluster_default_ids
            ),
            "failure_policy": "hard_fail",
            "unselected_members_mutated": False,
        }

    def _instance_splitting_page_context(
        self,
        cluster_key: str,
        page_info_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, str]:
        page_meta = page_info_map.get(cluster_key, {})
        return {
            "page_cluster_id": cluster_key,
            "page_name": str(page_meta.get("page_name", "")).strip(),
            "page_type_hint": str(page_meta.get("page_type_hint", "")).strip(),
            "description": str(page_meta.get("description", "")).strip(),
            "route_path": str(page_meta.get("route_path", "")).strip(),
        }

    def _instance_split_state_authority_by_obs_key(
        self,
        reference_trace_semantics: Dict[str, Any],
        *,
        cluster_key: str = "",
        cluster_obs: Optional[List[FrameObservation]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        expected_obs_keys = {
            self._obs_key(obs)
            for obs in (cluster_obs or [])
            if isinstance(obs, FrameObservation)
        }
        observations = (
            reference_trace_semantics.get("runtime_state_observations")
            or reference_trace_semantics.get("observations")
            or []
        ) if isinstance(reference_trace_semantics, dict) else []
        by_obs_key: Dict[str, List[Dict[str, Any]]] = {}
        for item in observations if isinstance(observations, list) else []:
            if not isinstance(item, dict):
                continue
            item_cluster = str(
                item.get("page_cluster_id")
                or item.get("general_page_id")
                or ""
            ).strip()
            if cluster_key and item_cluster != cluster_key:
                continue
            trace_id = str(item.get("trace_id", "") or "").strip()
            step_index = item.get("step_index", "")
            obs_key = str(item.get("obs_key", "") or "").strip()
            if not obs_key and trace_id and str(step_index) != "":
                try:
                    obs_key = f"{trace_id}:{int(step_index)}"
                except (TypeError, ValueError):
                    continue
            if not obs_key or (expected_obs_keys and obs_key not in expected_obs_keys):
                continue
            state_scope = str(
                item.get("state_scope")
                or item.get("scope")
                or item.get("state_label")
                or ""
            ).strip()
            if not state_scope:
                continue
            authority_row = {
                "observation_id": str(item.get("observation_id", "") or ""),
                "state_scope": state_scope,
                "lifecycle": str(
                    item.get("lifecycle")
                    or item.get("lifecycle_phase")
                    or ""
                ).strip(),
                "mutation": str(
                    item.get("mutation")
                    or item.get("operation_label")
                    or ""
                ).strip(),
                "value_hints": self._list_of_strings(
                    item.get("value_hints", [])
                    or item.get("value_mentions", [])
                ),
                "negative_constraints": self._list_of_strings(
                    item.get("negative_constraints", [])
                ),
                "source": str(
                    item.get("source")
                    or "reference_trace_semantics.runtime_state_observations"
                ).strip(),
            }
            by_obs_key.setdefault(obs_key, []).append({
                key: value
                for key, value in authority_row.items()
                if value not in ("", [], {})
            })
        for obs_key, rows in by_obs_key.items():
            by_obs_key[obs_key] = sorted(
                rows,
                key=lambda row: (
                    str(row.get("state_scope", "")),
                    str(row.get("observation_id", "")),
                ),
            )
        return dict(sorted(by_obs_key.items()))

    @staticmethod
    def _state_authority_row_is_initial(row: Dict[str, Any]) -> bool:
        lifecycle = str(row.get("lifecycle", "") or "").strip().lower()
        mutation = str(row.get("mutation", "") or "").strip().lower()
        return (
            any(token in lifecycle for token in ("initial", "entry", "loaded", "rendered"))
            and mutation in {"", "none", "read", "view", "display", "load", "loaded"}
        )

    @staticmethod
    def _state_authority_row_is_transition(row: Dict[str, Any]) -> bool:
        lifecycle = str(row.get("lifecycle", "") or "").strip().lower()
        mutation = str(row.get("mutation", "") or "").strip().lower()
        return (
            mutation not in {
                "",
                "none",
                "read",
                "view",
                "display",
                "load",
                "loaded",
                "available",
            }
            or any(
                token in lifecycle
                for token in ("mutated", "post_", "commit", "updated", "changed", "opened", "closed")
            )
        )

    def _build_instance_state_authority_projection(
        self,
        cluster_obs: List[FrameObservation],
        state_authority_by_obs_key: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        sorted_obs = sorted(
            [obs for obs in cluster_obs if isinstance(obs, FrameObservation)],
            key=lambda obs: (str(obs.trace_id), int(obs.step_index)),
        )
        first_obs_key_by_trace: Dict[str, str] = {}
        for obs in sorted_obs:
            first_obs_key_by_trace.setdefault(str(obs.trace_id), self._obs_key(obs))

        state_params_by_obs_key: Dict[str, Dict[str, Any]] = {}
        trace_entry_initial_state_by_trace_id: Dict[str, Dict[str, Any]] = {}
        runtime_page_initial_state_candidates_by_obs_key: Dict[str, Dict[str, Any]] = {}
        action_transition_state_diff_by_obs_key: Dict[str, Dict[str, Any]] = {}
        missing_state_authority_obs_keys: List[str] = []

        for obs in sorted_obs:
            obs_key = self._obs_key(obs)
            rows = list(state_authority_by_obs_key.get(obs_key, []) or [])
            if not rows:
                missing_state_authority_obs_keys.append(obs_key)
                continue
            authority_scopes: List[str] = ["trace_l0_source_state_precondition"]
            initial_rows = [
                row for row in rows
                if self._state_authority_row_is_initial(row)
            ]
            transition_rows = [
                row for row in rows
                if self._state_authority_row_is_transition(row)
            ]
            is_trace_entry = first_obs_key_by_trace.get(str(obs.trace_id)) == obs_key
            if is_trace_entry:
                authority_scopes.append("trace_entry_initial_state")
            if initial_rows:
                authority_scopes.append("runtime_page_initial_state_candidate")
            if transition_rows:
                authority_scopes.append("action_transition_state_diff")

            state_params = {
                "projection_kind": "runtime_state",
                "runtime_projection": True,
                "authority_source": "reference_trace_semantics.runtime_state_observations",
                "authority_scopes": authority_scopes,
                "state_scopes": sorted({
                    str(row.get("state_scope", "") or "")
                    for row in rows
                    if str(row.get("state_scope", "") or "")
                }),
                "state_observations": rows,
            }
            state_params_by_obs_key[obs_key] = state_params
            if is_trace_entry:
                trace_entry_initial_state_by_trace_id[str(obs.trace_id)] = {
                    "obs_key": obs_key,
                    "state_params": state_params,
                }
            if initial_rows:
                runtime_page_initial_state_candidates_by_obs_key[obs_key] = {
                    "state_observations": initial_rows,
                    "authority_status": "candidate_requires_page_contract_resolution",
                }
            if transition_rows:
                action_transition_state_diff_by_obs_key[obs_key] = {
                    "state_observations": transition_rows,
                    "authority_status": "observed_transition",
                }

        return {
            "format": "instance_state_authority_projection",
            "schema_revision": 1,
            "authority_source": "reference_trace_semantics.runtime_state_observations",
            "state_params_by_obs_key": state_params_by_obs_key,
            "trace_entry_initial_state_by_trace_id": trace_entry_initial_state_by_trace_id,
            "runtime_page_initial_state_candidates_by_obs_key": (
                runtime_page_initial_state_candidates_by_obs_key
            ),
            "trace_l0_source_state_preconditions_by_obs_key": state_params_by_obs_key,
            "action_transition_state_diff_by_obs_key": (
                action_transition_state_diff_by_obs_key
            ),
            "missing_state_authority_obs_keys": missing_state_authority_obs_keys,
            "summary": {
                "observation_count": len(sorted_obs),
                "state_parameterized_observation_count": len(state_params_by_obs_key),
                "trace_entry_initial_state_count": len(
                    trace_entry_initial_state_by_trace_id
                ),
                "runtime_page_initial_state_candidate_count": len(
                    runtime_page_initial_state_candidates_by_obs_key
                ),
                "action_transition_state_diff_count": len(
                    action_transition_state_diff_by_obs_key
                ),
                "missing_state_authority_count": len(
                    missing_state_authority_obs_keys
                ),
            },
        }

    @staticmethod
    def _attach_state_params_to_instance_groups(
        instance_groups: List[Dict[str, Any]],
        state_params_by_obs_key: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list):
            return []
        enriched: List[Dict[str, Any]] = []
        for group in instance_groups:
            if not isinstance(group, dict):
                continue
            cloned = dict(group)
            group_state_params = dict(cloned.get("state_params_by_obs_key") or {})
            for obs_key in cloned.get("obs_keys", []) or []:
                obs_key = str(obs_key or "").strip()
                params = state_params_by_obs_key.get(obs_key)
                if obs_key and isinstance(params, dict) and params:
                    group_state_params.setdefault(obs_key, dict(params))
            if group_state_params:
                cloned["state_params_by_obs_key"] = group_state_params
            enriched.append(cloned)
        return enriched

    async def _build_instance_splitting_phase(
        self,
        *,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
        content_lifecycle_plan: Dict[str, Any],
        reference_trace_semantics: Dict[str, Any],
        global_reasoning: Dict[str, Any],
        output_dir: str,
        initial_instance_planning: Optional[Dict[str, Dict[str, Any]]] = None,
        all_clusters: Optional[Dict[str, List[FrameObservation]]] = None,
        state_authority_by_obs_key: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
        initial_formal_validation_errors_by_cluster: Optional[
            Dict[str, List[str]]
        ] = None,
        boundary_preservation_constraints_by_cluster: Optional[
            Dict[str, Dict[str, Any]]
        ] = None,
    ) -> Dict[str, Dict[str, Any]]:
        instance_planning: Dict[str, Dict[str, Any]] = {
            str(cluster_key): dict(entry)
            for cluster_key, entry in (initial_instance_planning or {}).items()
            if isinstance(entry, dict)
        }
        planning_clusters = {
            cluster_key: cluster_obs
            for cluster_key, cluster_obs in clusters.items()
            if cluster_key not in instance_planning
        }
        if self.logger:
            self.logger.log_info(
                "[SM-P4] Instance splitting for "
                f"{len(planning_clusters)} missing General Page clusters "
                f"(reused {len(instance_planning)} completed clusters)..."
            )

        async def _plan_cluster(cluster_key: str, cluster_obs: List[FrameObservation]) -> None:
            page_context = self._instance_splitting_page_context(cluster_key, page_info_map)
            lifecycle_context = self._build_content_lifecycle_context(
                cluster_key,
                cluster_obs,
                content_lifecycle_plan,
            )
            cluster_obs_keys = {self._obs_key(obs) for obs in cluster_obs}
            cluster_state_authority = {
                obs_key: list(rows)
                for obs_key, rows in (
                    state_authority_by_obs_key
                    or self._instance_split_state_authority_by_obs_key(
                        reference_trace_semantics,
                        cluster_key=cluster_key,
                        cluster_obs=cluster_obs,
                    )
                ).items()
                if obs_key in cluster_obs_keys
            }
            state_authority_projection = (
                self._build_instance_state_authority_projection(
                    cluster_obs,
                    cluster_state_authority,
                )
            )
            cluster_state_params = state_authority_projection.get(
                "state_params_by_obs_key",
                {},
            )
            formal_validation_errors = list(
                (initial_formal_validation_errors_by_cluster or {}).get(
                    cluster_key,
                    [],
                )
            )
            planning_entry: Dict[str, Any] = {}
            for formal_round in range(1, 4):
                instance_groups = await self._run_instance_split_llm(
                    cluster_key,
                    cluster_obs,
                    page_context=page_context,
                    traces=traces,
                    lifecycle_context=lifecycle_context,
                    state_authority_by_obs_key=cluster_state_authority,
                    formal_validation_errors=formal_validation_errors,
                    boundary_preservation_constraint=(
                        boundary_preservation_constraints_by_cluster or {}
                    ).get(cluster_key),
                )
                planning_source = "llm_split"
                parameterization: Dict[str, Any] = {}
                if instance_groups:
                    normalized_groups = self._apply_materialized_split_principle(
                        cluster_key=cluster_key,
                        instance_groups=instance_groups,
                        cluster_obs=cluster_obs,
                        lifecycle_plan=content_lifecycle_plan,
                        page_context=page_context,
                        state_authority_by_obs_key=cluster_state_authority,
                        parameterization_sink=parameterization,
                        boundary_preservation_constraint=(
                            boundary_preservation_constraints_by_cluster or {}
                        ).get(cluster_key),
                    )
                    if normalized_groups != instance_groups:
                        instance_groups = normalized_groups
                        planning_source = (
                            f"{planning_source}+split_principle"
                            if instance_groups
                            else "cluster"
                        )
                instance_groups = self._attach_state_params_to_instance_groups(
                    instance_groups,
                    cluster_state_params,
                )
                parameterized_state_variants = list(
                    parameterization.get("parameterized_state_variants", [])
                    or []
                )
                if not instance_groups and cluster_state_params:
                    planning_source = "cluster_runtime_projection"

                planning_entry = {
                    "page_name": page_context["page_name"],
                    "page_type_hint": page_context["page_type_hint"],
                    "route_path": page_context["route_path"],
                    "extraction_mode": "instance" if instance_groups else "cluster",
                    "instance_split_mode": self.instance_split_mode,
                    "planning_source": (
                        planning_source
                        if instance_groups or planning_source != "llm_split"
                        else "cluster"
                    ),
                    "state_authority_projection": state_authority_projection,
                    "state_params_by_obs_key": cluster_state_params,
                    "parameterized_state_variant_count": len(
                        parameterized_state_variants
                    ),
                    "parameterized_state_variants": parameterized_state_variants,
                    "instance_groups": [
                        {
                            "instance_key": group.get("instance_key", ""),
                            "instance_label": group.get("instance_label", ""),
                            "trace_ids": group.get(
                                "trace_ids",
                                [group.get("trace_id", "")],
                            ),
                            "obs_keys": group.get("obs_keys", []),
                            "split_dimension": group.get("split_dimension", ""),
                            "split_reason": group.get("split_reason", {}),
                            "state_params_by_obs_key": group.get(
                                "state_params_by_obs_key",
                                {},
                            ),
                            "parameterized_state_variant_count": group.get(
                                "parameterized_state_variant_count",
                                0,
                            ),
                            "parameterized_state_variants": group.get(
                                "parameterized_state_variants",
                                [],
                            ),
                        }
                        for group in instance_groups
                    ],
                }
                formal_validation_errors = (
                    self._validate_reusable_instance_planning_entry(
                        cluster_key,
                        planning_entry,
                        cluster_obs,
                        state_authority_by_obs_key=cluster_state_authority,
                        traces=traces,
                    )
                )
                formal_validation_errors.extend(
                    self._validate_instance_split_boundary_preservation_constraint(
                        {
                            "split_needed": bool(
                                planning_entry["instance_groups"]
                            ),
                            "instances": planning_entry["instance_groups"],
                        },
                        (
                            boundary_preservation_constraints_by_cluster or {}
                        ).get(cluster_key),
                    )
                )
                if not formal_validation_errors:
                    if formal_round > 1 and self.logger:
                        self.logger.log_info(
                            "[Instance Split Formal Admission] "
                            f"{cluster_key} passed in round {formal_round}"
                        )
                    break
                if self.logger:
                    self.logger.log_warning(
                        "[Instance Split Formal Admission] "
                        f"{cluster_key} round {formal_round} invalid: "
                        f"{formal_validation_errors}"
                    )
            if formal_validation_errors:
                raise ValueError(
                    "instance split formal admission failed after 3 rounds for "
                    f"{cluster_key}: {formal_validation_errors}"
                )

            instance_planning[cluster_key] = planning_entry
            if output_dir:
                self._save_debug_instance_planning(instance_planning, output_dir)

        await asyncio.gather(*[
            _plan_cluster(cluster_key, cluster_obs)
            for cluster_key, cluster_obs in planning_clusters.items()
        ])

        if output_dir:
            self._save_debug_instance_planning(instance_planning, output_dir)
            manifest_clusters = all_clusters or clusters
            cluster_membership = {
                cluster_key: [self._obs_key(obs) for obs in cluster_obs]
                for cluster_key, cluster_obs in sorted(manifest_clusters.items())
            }
            instance_plan_hash = self._stable_json_fingerprint(instance_planning)
            self._write_phase_manifest(
                output_dir,
                "instance_splitting",
                "instance_split_manifest.json",
                inputs={
                    "global_reasoning_hash": self._stable_json_fingerprint(global_reasoning),
                    "cluster_membership_hash": self._stable_json_fingerprint(cluster_membership),
                    "reference_trace_semantics_hash": self._stable_json_fingerprint(reference_trace_semantics),
                    "content_lifecycle_plan_hash": self._stable_json_fingerprint(content_lifecycle_plan),
                },
                outputs={
                    "instance_plan_hash": instance_plan_hash,
                    "cluster_count": len(instance_planning),
                    "reused_cluster_count": len(initial_instance_planning or {}),
                    "produced_cluster_count": len(planning_clusters),
                },
                producer="AppSpecBuilder._build_instance_splitting_phase",
            )
        return instance_planning

    def _clip_schema_context_text(self, value: str, limit: int = 240) -> str:
        text = " ".join(str(value or "").strip().split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _normalize_instance_key_hint(value: str) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        normalized = re.sub(r"[^a-z0-9_]+", "_", raw)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if len(normalized) < 3 or len(normalized) > 64:
            return ""
        if normalized.isdigit():
            return ""
        if not any(ch.isalpha() for ch in normalized):
            return ""
        if re.fullmatch(r"instance_?\d+", normalized):
            return ""
        return normalized

    def _build_instance_key(self, cluster_key: str, index: int, label: str, key_hint: str, instance_id: str) -> str:
        for candidate in (key_hint, self._slugify_instance_label(label), self._normalize_instance_key_hint(instance_id)):
            normalized = self._normalize_instance_key_hint(candidate)
            if normalized:
                return normalized
        return f"{cluster_key}_i{index}"

    @staticmethod
    def _dedupe_instance_key(candidate: str, seen: set) -> str:
        key = candidate
        suffix = 2
        while key in seen:
            key = f"{candidate}_{suffix}"
            suffix += 1
        seen.add(key)
        return key

    @staticmethod
    def _is_false_like(value: Any) -> bool:
        if isinstance(value, bool):
            return value is False
        if value is None:
            return False
        text = str(value).strip().lower()
        return text in {"false", "no", "0", "not_same_general_page", "different_general_page"}

    @classmethod
    def _is_out_of_general_page_instance_group(cls, group: Dict[str, Any]) -> bool:
        if not isinstance(group, dict):
            return False
        split_reason = group.get("split_reason", {})
        if not isinstance(split_reason, dict):
            return False
        return cls._is_false_like(split_reason.get("same_general_page"))

    def _filter_out_of_general_page_instance_groups(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or not instance_groups:
            return []
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for group in instance_groups:
            if self._is_out_of_general_page_instance_group(group):
                dropped.append(group)
            else:
                kept.append(group)
        if dropped and self.logger:
            dropped_labels = [
                str(group.get("instance_label") or group.get("instance_key") or "").strip()
                for group in dropped
            ]
            self.logger.log_info(
                "[Instance Split Principle] {}: dropped {} out-of-General-Page "
                "instance candidate(s): {}".format(
                    cluster_key,
                    len(dropped),
                    ", ".join(label for label in dropped_labels if label) or "(unlabeled)",
                )
            )
        return kept

    @staticmethod
    def _is_opened_overlay_observation(obs: Any) -> bool:
        return (
            isinstance(obs, FrameObservation)
            and isinstance(obs.page_state, dict)
            and str(
                obs.page_state.get("interaction_surface_role", "") or "none"
            ).strip().lower()
            in {"opened_overlay", "opened_drawer", "opened_action_sheet", "opened_modal"}
            and bool(obs.page_state.get("host_page_still_visible", False))
        )

    @classmethod
    def _is_opened_overlay_instance_group(cls, group: Dict[str, Any]) -> bool:
        if not isinstance(group, dict):
            return False
        cluster_obs = group.get("cluster_obs", [])
        if not cluster_obs:
            return False
        return all(cls._is_opened_overlay_observation(obs) for obs in cluster_obs)

    @classmethod
    def _validate_instance_split_opened_overlay_group_contract(
        cls,
        candidate: Dict[str, Any],
        cluster_obs: List[FrameObservation],
    ) -> List[str]:
        if not isinstance(candidate, dict) or not bool(
            candidate.get("split_needed", True)
        ):
            return []
        instances = candidate.get("instances", [])
        if not isinstance(instances, list):
            return []
        obs_by_key = {cls._obs_key(obs): obs for obs in cluster_obs}
        errors: List[str] = []
        for index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                continue
            member_keys = cls._instance_split_candidate_instance_obs_keys(instance)
            member_obs = [obs_by_key[key] for key in member_keys if key in obs_by_key]
            if not member_obs or len(member_obs) != len(member_keys):
                continue
            if all(cls._is_opened_overlay_observation(obs) for obs in member_obs):
                errors.append(
                    f"instances[{index}] is opened-interaction-surface-only for "
                    f"obs_keys={member_keys}; opened overlays are runtime projections "
                    "and must remain in the visible host instance. Each observation is "
                    "the source screenshot before action_context.current_action, so do "
                    "not attribute that current action's future overlay or selected "
                    "state to the source observation."
                )
        return errors

    @classmethod
    def _derive_instance_split_boundary_preservation_constraint(
        cls,
        candidate: Dict[str, Any],
        cluster_obs: List[FrameObservation],
    ) -> Dict[str, Any]:
        if not isinstance(candidate, dict) or not bool(
            candidate.get("split_needed", True)
        ):
            return {}
        instances = candidate.get("instances", [])
        if not isinstance(instances, list):
            return {}
        obs_by_key = {cls._obs_key(obs): obs for obs in cluster_obs}
        survivors: List[Dict[str, Any]] = []
        overlay_only_indices: List[int] = []
        for index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                continue
            member_keys = cls._instance_split_candidate_instance_obs_keys(instance)
            member_obs = [obs_by_key[key] for key in member_keys if key in obs_by_key]
            if (
                member_obs
                and len(member_obs) == len(member_keys)
                and all(cls._is_opened_overlay_observation(obs) for obs in member_obs)
            ):
                overlay_only_indices.append(index)
            else:
                survivors.append(instance)
        if not overlay_only_indices or len(survivors) < 2:
            return {}
        durable_dimensions = sorted(
            {
                str(instance.get("split_dimension", "") or "").strip()
                for instance in survivors
                if str(instance.get("split_dimension", "") or "").strip()
                not in {"", "none", "semantic_mode", "state_variant", "lifecycle_variant"}
            }
        )
        return {
            "source": "prior_complete_producer_non_overlay_survivors",
            "minimum_instance_count": len(survivors),
            "required_split_dimensions": durable_dimensions,
            "overlay_only_instance_indices": overlay_only_indices,
        }

    @staticmethod
    def _validate_instance_split_boundary_preservation_constraint(
        candidate: Dict[str, Any],
        constraint: Optional[Dict[str, Any]],
    ) -> List[str]:
        if not isinstance(constraint, dict) or not constraint:
            return []
        minimum = int(constraint.get("minimum_instance_count", 0) or 0)
        if minimum < 2:
            return []
        errors: List[str] = []
        if not isinstance(candidate, dict) or not bool(
            candidate.get("split_needed", True)
        ):
            errors.append(
                "bound positive boundary evidence retains at least "
                f"{minimum} independent non-overlay instances; correcting an "
                "overlay-only group must not demote the durable boundary to "
                "split_needed=false"
            )
            instances: Any = []
        else:
            instances = candidate.get("instances", [])
        actual_count = len(instances) if isinstance(instances, list) else 0
        if actual_count < minimum:
            errors.append(
                "corrected candidate must retain at least "
                f"{minimum} independent non-overlay instances; got {actual_count}"
            )
        required_dimensions = {
            str(value or "").strip()
            for value in constraint.get("required_split_dimensions", []) or []
            if str(value or "").strip()
        }
        actual_dimensions = {
            str(instance.get("split_dimension", "") or "").strip()
            for instance in instances
            if isinstance(instance, dict)
        } if isinstance(instances, list) else set()
        missing_dimensions = sorted(required_dimensions - actual_dimensions)
        if missing_dimensions:
            errors.append(
                "corrected candidate dropped prior durable split dimensions: "
                f"{missing_dimensions}"
            )
        separation_groups = constraint.get("must_separate_obs_key_groups", [])
        if isinstance(separation_groups, list) and separation_groups:
            owner_by_obs_key: Dict[str, int] = {}
            if isinstance(instances, list):
                for instance_index, instance in enumerate(instances):
                    if not isinstance(instance, dict):
                        continue
                    for obs_key in AppSpecBuilder._instance_split_candidate_instance_obs_keys(
                        instance
                    ):
                        owner_by_obs_key[obs_key] = instance_index
            group_owners: List[int] = []
            for group_index, group in enumerate(separation_groups):
                keys = [
                    str(value or "").strip()
                    for value in group
                    if str(value or "").strip()
                ] if isinstance(group, list) else []
                owners = {owner_by_obs_key.get(key) for key in keys}
                if None in owners or len(owners) != 1:
                    errors.append(
                        "bound boundary evidence group must remain in one explicit "
                        f"instance: group={group_index}, obs_keys={keys}, owners={sorted(str(owner) for owner in owners)}"
                    )
                    continue
                group_owners.append(next(iter(owners)))
            if len(group_owners) == len(separation_groups) and len(
                set(group_owners)
            ) != len(group_owners):
                errors.append(
                    "bound positive boundary evidence requires distinct materialized "
                    f"instances for obs_key groups={separation_groups}; owners={group_owners}"
                )
        return errors

    @classmethod
    def _filter_opened_overlay_instance_groups(
        cls,
        instance_groups: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or not instance_groups:
            return []
        kept: List[Dict[str, Any]] = []
        for group in instance_groups:
            if cls._is_opened_overlay_instance_group(group):
                continue
            kept.append(group)
        return kept

    @staticmethod
    def _observation_transition(obs: Optional[FrameObservation]) -> Tuple[str, Optional[bool]]:
        if obs is None:
            return "", None
        transition_kind = str(
            obs.transition_kind
            or (obs.page_state.get("transition_kind", "") if isinstance(obs.page_state, dict) else "")
            or ""
        ).strip()
        transition_from_previous = (
            obs.transition_from_previous
            if obs.transition_from_previous is not None
            else (obs.page_state.get("transition_from_previous") if isinstance(obs.page_state, dict) else None)
        )
        if not isinstance(transition_from_previous, bool):
            transition_from_previous = None
        return transition_kind, transition_from_previous

    @classmethod
    def _is_same_page_observation(cls, obs: Optional[FrameObservation]) -> bool:
        transition_kind, _transition_from_previous = cls._observation_transition(obs)
        return transition_kind == "same_page_scroll"

    @classmethod
    def _is_true_same_page_continuation_observation(
        cls,
        obs: Optional[FrameObservation],
    ) -> bool:
        transition_kind, transition_from_previous = cls._observation_transition(obs)
        return transition_kind == "same_page_scroll" and transition_from_previous is True

    def _merge_same_page_continuation_instance_groups(
        self,
        instance_groups: List[Dict[str, Any]],
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        anchor_group_by_obs_key: Dict[str, int] = {}
        if isinstance(boundary_preservation_constraint, dict):
            separation_groups = boundary_preservation_constraint.get(
                "must_separate_obs_key_groups", []
            )
            if isinstance(separation_groups, list):
                for anchor_group_index, anchor_group in enumerate(separation_groups):
                    if not isinstance(anchor_group, list):
                        continue
                    for obs_key in anchor_group:
                        normalized = str(obs_key or "").strip()
                        if normalized:
                            anchor_group_by_obs_key[normalized] = anchor_group_index

        def protected_anchor_groups(group_index: int) -> set[int]:
            group = instance_groups[group_index]
            return {
                anchor_group_by_obs_key[normalized]
                for obs_key in group.get("obs_keys", []) or []
                if (normalized := str(obs_key or "").strip())
                in anchor_group_by_obs_key
            }

        owner_by_trace_step: Dict[Tuple[str, int], int] = {}
        for group_index, group in enumerate(instance_groups):
            for obs in group.get("cluster_obs", []) or []:
                if isinstance(obs, FrameObservation):
                    owner_by_trace_step[(str(obs.trace_id), int(obs.step_index))] = group_index

        predecessor_groups_by_index: Dict[int, set[int]] = {}
        groups_with_observations: set[int] = set()
        same_page_group_indices: set[int] = set()
        for group_index, group in enumerate(instance_groups):
            group_obs = [obs for obs in group.get("cluster_obs", []) or [] if isinstance(obs, FrameObservation)]
            if not group_obs:
                continue
            groups_with_observations.add(group_index)
            if all(self._is_true_same_page_continuation_observation(obs) for obs in group_obs):
                same_page_group_indices.add(group_index)
            predecessor_groups = {
                owner_by_trace_step.get((str(obs.trace_id), int(obs.step_index) - 1))
                for obs in group_obs
            }
            predecessor_groups.discard(None)
            predecessor_groups.discard(group_index)
            predecessor_groups_by_index[group_index] = {
                index for index in predecessor_groups if isinstance(index, int)
            }

        if groups_with_observations and groups_with_observations <= same_page_group_indices:
            return []

        def resolve_non_same_page_roots(group_index: int, seen: Optional[set[int]] = None) -> set[int]:
            seen = seen or set()
            if group_index in seen:
                return set()
            seen.add(group_index)

            roots: set[int] = set()
            for predecessor_index in predecessor_groups_by_index.get(group_index, set()):
                if predecessor_index in same_page_group_indices:
                    roots.update(resolve_non_same_page_roots(predecessor_index, seen))
                else:
                    roots.add(predecessor_index)
            return roots

        merge_targets: Dict[int, int] = {}
        for group_index in same_page_group_indices:
            roots = resolve_non_same_page_roots(group_index)
            if len(roots) == 1:
                target_index = next(iter(roots))
                if len(
                    protected_anchor_groups(group_index)
                    | protected_anchor_groups(target_index)
                ) > 1:
                    continue
                merge_targets[group_index] = target_index

        if not merge_targets:
            return instance_groups

        merged: List[Dict[str, Any]] = []
        index_map: Dict[int, int] = {}
        for group_index, group in enumerate(instance_groups):
            if group_index in merge_targets:
                continue
            cloned = dict(group)
            cloned["trace_ids"] = list(group.get("trace_ids", []) or [])
            cloned["obs_keys"] = list(group.get("obs_keys", []) or [])
            cloned["cluster_obs"] = list(group.get("cluster_obs", []) or [])
            index_map[group_index] = len(merged)
            merged.append(cloned)

        for source_index, target_index in merge_targets.items():
            if target_index not in index_map:
                continue
            target = merged[index_map[target_index]]
            source = instance_groups[source_index]
            existing_obs_keys = {str(key) for key in target.get("obs_keys", []) or []}
            existing_trace_ids = {str(trace_id) for trace_id in target.get("trace_ids", []) or []}
            existing_obs = set()
            for obs in target.get("cluster_obs", []) or []:
                if isinstance(obs, FrameObservation):
                    existing_obs.add((str(obs.trace_id), int(obs.step_index)))

            for obs in source.get("cluster_obs", []) or []:
                if not isinstance(obs, FrameObservation):
                    continue
                obs_id = (str(obs.trace_id), int(obs.step_index))
                if obs_id not in existing_obs:
                    target.setdefault("cluster_obs", []).append(obs)
                    existing_obs.add(obs_id)
                obs_key = self._obs_key(obs)
                if obs_key not in existing_obs_keys:
                    target.setdefault("obs_keys", []).append(obs_key)
                    existing_obs_keys.add(obs_key)
                if obs.trace_id not in existing_trace_ids:
                    target.setdefault("trace_ids", []).append(obs.trace_id)
                    existing_trace_ids.add(obs.trace_id)

            target["trace_ids"] = sorted(str(trace_id) for trace_id in existing_trace_ids)
            target["cluster_obs"] = sorted(
                target.get("cluster_obs", []) or [],
                key=lambda obs: (str(obs.trace_id), int(obs.step_index)) if isinstance(obs, FrameObservation) else ("", -1),
            )
            target["obs_keys"] = [self._obs_key(obs) for obs in target["cluster_obs"] if isinstance(obs, FrameObservation)]

        return merged

    @staticmethod
    def _is_submitted_query_instance_group(group: Dict[str, Any]) -> bool:
        if not isinstance(group, dict):
            return False
        split_dimension = str(group.get("split_dimension") or "").strip().lower()
        if split_dimension == "submitted_query":
            return True
        split_reason = group.get("split_reason", {})
        if isinstance(split_reason, dict):
            return str(split_reason.get("dimension") or "").strip().lower() == "submitted_query"
        return False

    @staticmethod
    def _submitted_query_state_params(group: Dict[str, Any]) -> Dict[str, str]:
        source = str(group.get("submitted_query") or group.get("instance_label") or group.get("instance_key") or "").strip()
        params = {
            "lifecycle": "submitted",
            "split_dimension": "submitted_query",
        }
        if group.get("submitted_query"):
            params["submitted_query"] = str(group.get("submitted_query") or "").strip()
        if source:
            params["submitted_query_source"] = source
        source_key = str(group.get("instance_key") or "").strip()
        if source_key:
            params["source_instance_key"] = source_key
        return params

    @classmethod
    def _merge_instance_group_into_target(
        cls,
        target: Dict[str, Any],
        source: Dict[str, Any],
        *,
        state_params: Optional[Dict[str, str]] = None,
    ) -> None:
        existing_obs_keys = {str(key) for key in target.get("obs_keys", []) or []}
        existing_trace_ids = {str(trace_id) for trace_id in target.get("trace_ids", []) or []}
        existing_obs = {
            (str(obs.trace_id), int(obs.step_index))
            for obs in target.get("cluster_obs", []) or []
            if isinstance(obs, FrameObservation)
        }

        for obs in source.get("cluster_obs", []) or []:
            if not isinstance(obs, FrameObservation):
                continue
            obs_id = (str(obs.trace_id), int(obs.step_index))
            obs_key = f"{obs.trace_id}:{int(obs.step_index)}"
            if obs_id not in existing_obs:
                target.setdefault("cluster_obs", []).append(obs)
                existing_obs.add(obs_id)
            if obs_key not in existing_obs_keys:
                target.setdefault("obs_keys", []).append(obs_key)
                existing_obs_keys.add(obs_key)
            existing_trace_ids.add(str(obs.trace_id))
            if state_params:
                target.setdefault("state_params_by_obs_key", {})[obs_key] = dict(state_params)

        for obs_key in source.get("obs_keys", []) or []:
            obs_key = str(obs_key or "").strip()
            if not obs_key or obs_key in existing_obs_keys:
                continue
            target.setdefault("obs_keys", []).append(obs_key)
            existing_obs_keys.add(obs_key)
            if state_params:
                target.setdefault("state_params_by_obs_key", {})[obs_key] = dict(state_params)

        for trace_id in source.get("trace_ids", []) or []:
            trace_id = str(trace_id or "").strip()
            if trace_id:
                existing_trace_ids.add(trace_id)

        target["trace_ids"] = sorted(existing_trace_ids)
        target["cluster_obs"] = sorted(
            target.get("cluster_obs", []) or [],
            key=lambda obs: (str(obs.trace_id), int(obs.step_index)) if isinstance(obs, FrameObservation) else ("", -1),
        )
        target["obs_keys"] = [
            f"{obs.trace_id}:{int(obs.step_index)}"
            for obs in target["cluster_obs"]
            if isinstance(obs, FrameObservation)
        ] or list(target.get("obs_keys", []) or [])

    @staticmethod
    def _is_search_results_page_context(page_context: Optional[Dict[str, str]]) -> bool:
        page_text = " ".join(
            str((page_context or {}).get(key, "") or "")
            for key in ("page_name", "page_type_hint", "description", "route_path")
        ).lower()
        return "search_results" in page_text or "search/results" in page_text

    @staticmethod
    def _query_value_from_search_results_group(group: Dict[str, Any]) -> str:
        sources: List[str] = []
        split_reason = group.get("split_reason", {})
        if isinstance(split_reason, dict):
            sources.extend(
                str(split_reason.get(key) or "")
                for key in ("why_not_same_instance", "evidence", "reason")
            )
        sources.extend(
            str(group.get(key) or "")
            for key in ("instance_label", "instance_key")
        )
        joined = "\n".join(source for source in sources if source)
        for pattern in (
            r"submitted query [\"“”']([^\"“”']+)[\"“”']",
            r"提交查询[为是:：\s]*[\"“”']?([^\"“”'。，,.\n]+)",
            r"搜索结果[：: ]+([^，,。\n]+)",
            r"搜索[：: ]+([^，,。\n]+)",
        ):
            match = re.search(pattern, joined, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        label = str(group.get("instance_label") or "").strip()
        if label:
            cleaned = re.sub(r"^(外卖|全局|商品|店铺)?搜索结果[：:\s]*", "", label).strip()
            cleaned = re.sub(r"(搜索)?结果$", "", cleaned).strip(" ：:")
            if cleaned:
                return cleaned
        return ""

    @staticmethod
    def _has_submitted_search_result_evidence(obs: FrameObservation, lifecycle_step: Dict[str, Any]) -> bool:
        phase = str((lifecycle_step or {}).get("lifecycle_phase") or "").strip().lower()
        text = "\n".join(
            str(value or "")
            for value in (
                getattr(obs, "description", ""),
                getattr(obs, "action", ""),
                (lifecycle_step or {}).get("reason", ""),
                json.dumps((lifecycle_step or {}).get("exclude_from_initial_value_hints", []), ensure_ascii=False),
            )
        ).lower()
        has_commit_phase = phase.startswith("post_commit") or "submitted" in phase
        has_result_surface = any(
            marker in text
            for marker in (
                "search results",
                "result collection",
                "product grid",
                "submitted query",
                "executing search",
                "搜索结果",
                "结果页",
                "商品",
                "提交查询",
            )
        )
        return has_commit_phase and has_result_surface

    @staticmethod
    def _query_value_from_search_results_observation(
        obs: FrameObservation,
        lifecycle_step: Dict[str, Any],
    ) -> str:
        text = "\n".join(
            str(value or "")
            for value in (
                getattr(obs, "description", ""),
                getattr(obs, "action", ""),
                (lifecycle_step or {}).get("reason", ""),
            )
        )
        patterns = (
            r"submitted query [\"“”']([^\"“”']+)[\"“”']",
            r"executing search for [\"“”']([^\"“”']+)[\"“”']",
            r"search bar showing the query [\"“”']([^\"“”']+)[\"“”']",
            r"提交查询[为是:：\s]*[\"“”']?([^\"“”'。，,.\n；;]+)",
            r"搜索(?:词|栏|框)?(?:显示|为|是|中)?[：:\s]*[\"“”']([^\"“”']+)[\"“”']",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _normalize_search_results_submitted_query_groups(
        self,
        instance_groups: List[Dict[str, Any]],
        page_context: Optional[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups
        if not self._is_search_results_page_context(page_context):
            return instance_groups

        normalized_groups: List[Dict[str, Any]] = []
        changed = False
        for group in instance_groups:
            if not isinstance(group, dict):
                normalized_groups.append(group)
                continue
            query_value = self._query_value_from_search_results_group(group)
            split_dimension = str(group.get("split_dimension") or "").strip().lower()
            split_reason = group.get("split_reason", {})
            reason_dimension = ""
            if isinstance(split_reason, dict):
                reason_dimension = str(split_reason.get("dimension") or "").strip().lower()
            has_query_evidence = bool(query_value) or split_dimension == "submitted_query" or reason_dimension == "submitted_query"
            if not has_query_evidence:
                normalized_groups.append(group)
                continue

            cloned = dict(group)
            cloned["split_dimension"] = "submitted_query"
            if query_value:
                cloned["submitted_query"] = query_value
            cloned_reason = dict(split_reason) if isinstance(split_reason, dict) else {}
            cloned_reason["dimension"] = "submitted_query"
            cloned["split_reason"] = cloned_reason
            state_params = self._submitted_query_state_params(cloned)
            by_obs_key = dict(cloned.get("state_params_by_obs_key") or {})
            obs_keys = [str(key or "").strip() for key in cloned.get("obs_keys", []) or []]
            for obs in cloned.get("cluster_obs", []) or []:
                if isinstance(obs, FrameObservation):
                    obs_keys.append(self._obs_key(obs))
            for obs_key in sorted({key for key in obs_keys if key}):
                by_obs_key.setdefault(obs_key, dict(state_params))
            if by_obs_key:
                cloned["state_params_by_obs_key"] = by_obs_key
            changed = True
            normalized_groups.append(cloned)

        return normalized_groups if changed else instance_groups

    def _complete_search_results_submitted_query_groups(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
        cluster_obs: List[FrameObservation],
        lifecycle_plan: Dict[str, Any],
        page_context: Optional[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups
        if not self._is_search_results_page_context(page_context):
            return instance_groups

        claimed_obs_keys = {
            str(obs_key or "").strip()
            for group in instance_groups
            if isinstance(group, dict)
            for obs_key in (group.get("obs_keys", []) or [])
        }
        for group in instance_groups:
            if not isinstance(group, dict):
                continue
            for obs in group.get("cluster_obs", []) or []:
                if isinstance(obs, FrameObservation):
                    claimed_obs_keys.add(self._obs_key(obs))

        lifecycle_steps = (lifecycle_plan or {}).get("steps", [])
        if not lifecycle_steps and isinstance((lifecycle_plan or {}).get("clusters"), dict):
            cluster_plan = ((lifecycle_plan or {}).get("clusters") or {}).get(cluster_key, {})
            if isinstance(cluster_plan, dict):
                lifecycle_steps = cluster_plan.get("steps", [])
        lifecycle_steps_by_obs_key = {
            str(step.get("obs_key", "") or ""): step
            for step in lifecycle_steps
            if isinstance(step, dict)
        }
        seen_instance_keys = {
            str(group.get("instance_key") or "").strip()
            for group in instance_groups
            if isinstance(group, dict) and str(group.get("instance_key") or "").strip()
        }
        completed_groups = list(instance_groups)
        completed_count = 0
        for obs in sorted(cluster_obs or [], key=lambda item: (str(item.trace_id), int(item.step_index))):
            obs_key = self._obs_key(obs)
            if obs_key in claimed_obs_keys:
                continue
            lifecycle_step = lifecycle_steps_by_obs_key.get(obs_key, {})
            if not self._has_submitted_search_result_evidence(obs, lifecycle_step):
                continue
            submitted_query = self._query_value_from_search_results_observation(obs, lifecycle_step)
            if not submitted_query:
                continue

            label = f"搜索结果：{submitted_query}"
            raw_key = self._build_instance_key(
                cluster_key=cluster_key,
                index=len(completed_groups) + 1,
                label=label,
                key_hint="submitted_query_results",
                instance_id="",
            )
            instance_key = self._dedupe_instance_key(raw_key, seen_instance_keys)
            split_reason = {
                "dimension": "submitted_query",
                "same_general_page": True,
                "why_not_same_instance": self._clip_schema_context_text(
                    lifecycle_step.get("reason", "") or obs.description,
                    300,
                ),
            }
            group = {
                "instance_key": instance_key,
                "instance_label": label,
                "trace_ids": [str(obs.trace_id)],
                "obs_keys": [obs_key],
                "cluster_obs": [obs],
                "split_dimension": "submitted_query",
                "submitted_query": submitted_query,
                "split_reason": split_reason,
                "state_params_by_obs_key": {
                    obs_key: self._submitted_query_state_params(
                        {
                            "instance_key": instance_key,
                            "instance_label": label,
                            "submitted_query": submitted_query,
                        }
                    )
                },
            }
            completed_groups.append(group)
            claimed_obs_keys.add(obs_key)
            completed_count += 1

        if completed_count and self.logger:
            self.logger.log_info(
                f"[Instance Split Principle] {cluster_key}: completed {completed_count} "
                "unclaimed submitted-query search-result observation(s)"
            )
        return completed_groups

    def _demote_submitted_query_instance_groups(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
        page_context: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        if self._is_search_results_page_context(page_context):
            return instance_groups

        submitted_indices = [
            index
            for index, group in enumerate(instance_groups)
            if self._is_submitted_query_instance_group(group)
        ]
        if not submitted_indices:
            return instance_groups

        non_submitted_indices = [
            index for index in range(len(instance_groups)) if index not in submitted_indices
        ]
        if not non_submitted_indices:
            if self.logger:
                self.logger.log_info(
                    f"[Instance Split Principle] {cluster_key}: demoted all submitted_query "
                    "instance candidates to a single parameterized search state"
                )
            return []

        cloned_groups = []
        index_map: Dict[int, int] = {}
        for index, group in enumerate(instance_groups):
            if index in submitted_indices:
                continue
            cloned = dict(group)
            cloned["trace_ids"] = list(group.get("trace_ids", []) or [])
            cloned["obs_keys"] = list(group.get("obs_keys", []) or [])
            cloned["cluster_obs"] = list(group.get("cluster_obs", []) or [])
            if isinstance(group.get("state_params_by_obs_key"), dict):
                cloned["state_params_by_obs_key"] = dict(group.get("state_params_by_obs_key") or {})
            index_map[index] = len(cloned_groups)
            cloned_groups.append(cloned)

        def nearest_predecessor_target(source: Dict[str, Any]) -> Optional[int]:
            source_obs = [
                obs for obs in source.get("cluster_obs", []) or [] if isinstance(obs, FrameObservation)
            ]
            if not source_obs:
                return None
            best: Optional[Tuple[int, int]] = None
            for candidate_index in non_submitted_indices:
                candidate = instance_groups[candidate_index]
                candidate_obs = [
                    obs for obs in candidate.get("cluster_obs", []) or [] if isinstance(obs, FrameObservation)
                ]
                for submitted_obs in source_obs:
                    for candidate_ob in candidate_obs:
                        if str(candidate_ob.trace_id) != str(submitted_obs.trace_id):
                            continue
                        delta = int(submitted_obs.step_index) - int(candidate_ob.step_index)
                        if delta <= 0:
                            continue
                        if best is None or delta < best[1]:
                            best = (candidate_index, delta)
            return best[0] if best else None

        demoted_count = 0
        unresolved: List[Dict[str, Any]] = []
        for source_index in submitted_indices:
            source = instance_groups[source_index]
            target_index = nearest_predecessor_target(source)
            if target_index is None and len(non_submitted_indices) == 1:
                target_index = non_submitted_indices[0]
            if target_index is None or target_index not in index_map:
                unresolved.append(source)
                continue
            target = cloned_groups[index_map[target_index]]
            state_params = self._submitted_query_state_params(source)
            self._merge_instance_group_into_target(
                target,
                source,
                state_params=state_params,
            )
            variants = target.setdefault("parameterized_state_variants", [])
            variants.append({
                "dimension": "submitted_query",
                "source_instance_key": str(source.get("instance_key") or ""),
                "source_instance_label": str(source.get("instance_label") or ""),
                "obs_keys": list(source.get("obs_keys", []) or []),
                "state_params": state_params,
            })
            target["parameterized_state_variant_count"] = len(variants)
            demoted_count += 1

        if unresolved:
            cloned_groups.extend(unresolved)
        if demoted_count and self.logger:
            self.logger.log_info(
                f"[Instance Split Principle] {cluster_key}: demoted {demoted_count} "
                "submitted_query instance candidate(s) into parameterized state"
            )
        if len(cloned_groups) < 2:
            return []
        return cloned_groups

    @staticmethod
    def _normalized_instance_split_text(value: Any) -> str:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value or "")
        text = text.lower()
        text = re.sub(r"[\s\-_/]+", " ", text)
        return text

    @classmethod
    def _contains_future_action_split_signal(cls, group: Dict[str, Any]) -> bool:
        text = cls._normalized_instance_split_text({
            "instance_key": group.get("instance_key", ""),
            "instance_label": group.get("instance_label", ""),
            "split_dimension": group.get("split_dimension", ""),
            "split_reason": group.get("split_reason", {}),
            "reason": group.get("reason", ""),
        })
        future_markers = (
            "future",
            "downstream",
            "later",
            "task goal",
            "goal",
            "action chain",
            "workflow",
            "submit",
            "publish",
            "save",
            "draft",
            "confirm",
            "send",
            "share",
            "delete",
            "checkout",
            "metadata",
            "topic",
            "后续",
            "未来",
            "目标",
            "动作链",
            "流程",
            "提交",
            "发布",
            "保存",
            "存草稿",
            "草稿",
            "确认",
            "发送",
            "分享",
            "删除",
            "添加话题",
            "话题",
        )
        return any(marker in text for marker in future_markers)

    @staticmethod
    def _instance_split_page_is_form_like(page_context: Dict[str, str]) -> bool:
        text = " ".join(
            str(page_context.get(key, "") or "")
            for key in ("page_name", "page_type_hint", "description", "route_path")
        ).lower()
        markers = (
            "form",
            "editor",
            "edit",
            "compose",
            "composer",
            "publish",
            "checkout",
            "编辑",
            "发布",
            "创作",
            "表单",
            "填写",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _first_group_observation(group: Dict[str, Any]) -> Optional[FrameObservation]:
        group_obs = [obs for obs in group.get("cluster_obs", []) or [] if isinstance(obs, FrameObservation)]
        if not group_obs:
            return None
        return sorted(group_obs, key=lambda obs: (str(obs.trace_id), int(obs.step_index)))[0]

    @staticmethod
    def _description_word_set(text: str) -> set[str]:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
        return {token for token in normalized.split() if len(token) >= 2}

    @classmethod
    def _description_similarity(cls, left: str, right: str) -> float:
        left_words = cls._description_word_set(left)
        right_words = cls._description_word_set(right)
        if not left_words or not right_words:
            return 0.0
        return len(left_words & right_words) / max(1, len(left_words | right_words))

    @staticmethod
    def _normalized_previous_action(obs: Optional[FrameObservation]) -> str:
        if obs is None or not isinstance(obs.page_state, dict):
            return ""
        text = str(obs.page_state.get("previous_action", "") or "").lower()
        text = re.sub(r"\([^)]*\)", "", text)
        text = re.sub(r"[0-9]+", "", text)
        text = re.sub(r"\s+", "", text)
        return text

    @staticmethod
    def _neutralize_future_action_label(label: str, fallback: str) -> str:
        text = str(label or "").strip()
        if not text:
            return fallback
        text = re.sub(r"[（(][^）)]*(添加话题|话题|存草稿|草稿|发布|保存|提交|确认|发送|分享|删除|topic|draft|publish|save|submit|confirm|send|share|delete)[^）)]*[）)]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ·-_/")
        return text or fallback

    @classmethod
    def _neutralize_future_action_key(cls, key: str, fallback: str) -> str:
        text = str(key or "").strip().lower()
        text = re.sub(
            r"(^|_)(topic|draft|publish|save|submit|confirm|send|share|delete|metadata)(_|$)",
            "_",
            text,
        )
        text = re.sub(r"_+", "_", text).strip("_")
        return text if text and len(text) >= 3 else fallback

    def _form_future_action_split_groups_compatible(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> bool:
        if not (
            self._contains_future_action_split_signal(left)
            or self._contains_future_action_split_signal(right)
        ):
            return False
        left_obs = self._first_group_observation(left)
        right_obs = self._first_group_observation(right)
        if left_obs is None or right_obs is None:
            return False
        left_previous = self._normalized_previous_action(left_obs)
        right_previous = self._normalized_previous_action(right_obs)
        if not left_previous or left_previous != right_previous:
            return False
        similarity = self._description_similarity(left_obs.description, right_obs.description)
        return similarity >= 0.55

    def _merge_form_future_action_instance_groups(
        self,
        cluster_key: str,
        page_context: Dict[str, str],
        instance_groups: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not self._instance_split_page_is_form_like(page_context):
            return instance_groups
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        parent = list(range(len(instance_groups)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left_index in range(len(instance_groups)):
            for right_index in range(left_index + 1, len(instance_groups)):
                if self._form_future_action_split_groups_compatible(
                    instance_groups[left_index],
                    instance_groups[right_index],
                ):
                    union(left_index, right_index)

        components: Dict[int, List[int]] = {}
        for index in range(len(instance_groups)):
            components.setdefault(find(index), []).append(index)
        if all(len(indices) == 1 for indices in components.values()):
            return instance_groups

        merged_groups: List[Dict[str, Any]] = []
        for indices in components.values():
            base = dict(instance_groups[indices[0]])
            base["trace_ids"] = []
            base["obs_keys"] = []
            base["cluster_obs"] = []
            existing_obs: set[Tuple[str, int]] = set()
            for index in indices:
                group = instance_groups[index]
                for obs in group.get("cluster_obs", []) or []:
                    if not isinstance(obs, FrameObservation):
                        continue
                    obs_id = (str(obs.trace_id), int(obs.step_index))
                    if obs_id in existing_obs:
                        continue
                    base["cluster_obs"].append(obs)
                    existing_obs.add(obs_id)
            base["cluster_obs"] = sorted(
                base["cluster_obs"],
                key=lambda obs: (str(obs.trace_id), int(obs.step_index)),
            )
            base["obs_keys"] = [self._obs_key(obs) for obs in base["cluster_obs"]]
            base["trace_ids"] = sorted({str(obs.trace_id) for obs in base["cluster_obs"]})
            if len(indices) > 1:
                fallback_label = str(page_context.get("page_name") or cluster_key)
                fallback_key = f"{cluster_key}_form_state"
                candidate_labels = [str(instance_groups[index].get("instance_label", "") or "") for index in indices]
                candidate_keys = [str(instance_groups[index].get("instance_key", "") or "") for index in indices]
                base["instance_label"] = self._neutralize_future_action_label(candidate_labels[0], fallback_label)
                neutral_keys = [
                    self._neutralize_future_action_key(candidate_key, "")
                    for candidate_key in candidate_keys
                ]
                common_keys = {key for key in neutral_keys if key}
                base["instance_key"] = next(iter(common_keys)) if len(common_keys) == 1 else fallback_key
                base["split_reason"] = {
                    "dimension": "state_variant",
                    "same_general_page": True,
                    "why_not_same_instance": "",
                    "merge_reason": "merged form/editor instances whose split evidence was future task goal or downstream action, not current visible state",
                    "merged_instance_keys": candidate_keys,
                }
                base["reason"] = "Merged by deterministic form/editor current-state identity gate."
            merged_groups.append(base)

        if len(merged_groups) < len(instance_groups) and self.logger:
            self.logger.log_info(
                f"[Instance Split Principle] {cluster_key}: merged "
                f"{len(instance_groups) - len(merged_groups)} future-action form/editor split candidate(s)"
            )
        if len(merged_groups) < 2:
            return []
        return merged_groups

    def _build_cluster_task_trace_context(
        self,
        cluster_obs: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        if not cluster_obs:
            return {"trace_contexts": []}
        trace_map = {trace.trace_id: trace for trace in traces}
        by_trace: Dict[str, List[int]] = {}
        for obs in cluster_obs:
            by_trace.setdefault(obs.trace_id, []).append(int(obs.step_index))

        trace_contexts: List[Dict[str, Any]] = []
        for trace_id, step_indices in sorted(by_trace.items()):
            trace = trace_map.get(trace_id)
            if trace is None:
                continue
            steps_by_index = {int(step.step_index): step for step in trace.steps}
            page_steps = []
            neighbor_steps = []
            seen_neighbors = set()
            for step_index in sorted(set(step_indices)):
                step = steps_by_index.get(step_index)
                if step:
                    page_steps.append({
                        "step": step.step_index,
                        "action": self._clip_schema_context_text(step.action, 180),
                        "thought": self._clip_schema_context_text(step.thought, 220),
                    })
                for neighbor_index in (step_index - 1, step_index + 1):
                    if neighbor_index in step_indices or neighbor_index in seen_neighbors:
                        continue
                    neighbor = steps_by_index.get(neighbor_index)
                    if not neighbor:
                        continue
                    seen_neighbors.add(neighbor_index)
                    neighbor_steps.append({
                        "step": neighbor.step_index,
                        "action": self._clip_schema_context_text(neighbor.action, 160),
                        "thought": self._clip_schema_context_text(neighbor.thought, 180),
                    })
            trace_contexts.append({
                "trace_id": trace_id,
                "task_instruction": self._clip_schema_context_text(trace.task, 600),
                "page_steps": page_steps[:12],
                "neighbor_steps": sorted(neighbor_steps, key=lambda item: item["step"])[:12],
            })
        return {"trace_contexts": trace_contexts}

    def _list_of_strings(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item)]

    def _build_content_lifecycle_plan(
        self,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
        semantic_artifact: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        runtime_observations = self._build_runtime_state_observations(
            clusters=clusters,
            traces=traces,
            page_info_map=page_info_map,
            semantic_artifact=semantic_artifact,
        )
        return runtime_observations.get("derived", {}).get("content_lifecycle_plan", {})

    def _build_reference_trace_evidence_pack(
        self,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        trace_map = {trace.trace_id: trace for trace in traces}
        frames: List[Dict[str, Any]] = []
        for cluster_key, cluster_obs in sorted((clusters or {}).items()):
            page_meta = page_info_map.get(cluster_key, {}) if isinstance(page_info_map, dict) else {}
            for obs in sorted(cluster_obs or [], key=lambda item: (str(item.trace_id), int(item.step_index))):
                trace = trace_map.get(obs.trace_id)
                steps_by_index = {int(step.step_index): step for step in trace.steps} if trace else {}
                step = steps_by_index.get(int(obs.step_index))
                previous_steps = [
                    steps_by_index[index]
                    for index in sorted(steps_by_index)
                    if index < int(obs.step_index)
                ][-4:]
                frames.append({
                    "obs_key": self._obs_key(obs),
                    "trace_id": str(obs.trace_id),
                    "step_index": int(obs.step_index),
                    "page_cluster_id": cluster_key,
                    "page_name": str(page_meta.get("page_name", "") or ""),
                    "page_type_hint": str(page_meta.get("page_type_hint", "") or ""),
                    "task": str(trace.task if trace else ""),
                    "previous_actions": [
                        {
                            "step_index": int(prev.step_index),
                            "action": str(getattr(prev, "action", "") or ""),
                            "thought": str(getattr(prev, "thought", "") or ""),
                        }
                        for prev in previous_steps
                    ],
                    "current_action": str(getattr(step, "action", "") if step else ""),
                    "current_thought": str(getattr(step, "thought", "") if step else ""),
                    "frame_description": str(getattr(obs, "description", "") or ""),
                })
        return {
            "format": "reference_trace_evidence_pack",
            "schema_revision": 1,
            "frames": frames,
        }

    def _build_reference_trace_evidence_packs(
        self,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
        max_frames_per_pack: int = None,
    ) -> List[Dict[str, Any]]:
        max_frames = int(max_frames_per_pack or self.REFERENCE_TRACE_SEMANTIC_MAX_FRAMES_PER_PACK)
        if max_frames <= 0:
            max_frames = self.REFERENCE_TRACE_SEMANTIC_MAX_FRAMES_PER_PACK
        full_pack = self._build_reference_trace_evidence_pack(
            clusters=clusters,
            traces=traces,
            page_info_map=page_info_map,
        )
        frames_by_trace: Dict[str, List[Dict[str, Any]]] = {}
        for frame in full_pack.get("frames", []):
            if not isinstance(frame, dict):
                continue
            trace_id = str(frame.get("trace_id", "") or "")
            frames_by_trace.setdefault(trace_id, []).append(frame)

        packs: List[Dict[str, Any]] = []
        for trace_id in sorted(frames_by_trace):
            frames = sorted(
                frames_by_trace[trace_id],
                key=lambda item: (str(item.get("page_cluster_id", "")), int(item.get("step_index", 0) or 0)),
            )
            for chunk_index, start in enumerate(range(0, len(frames), max_frames)):
                chunk = frames[start:start + max_frames]
                if not chunk:
                    continue
                packs.append({
                    "format": "reference_trace_evidence_pack",
                    "schema_revision": 1,
                    "pack_id": "trace:{}:chunk:{}".format(trace_id, chunk_index),
                    "trace_id": trace_id,
                    "chunk_index": chunk_index,
                    "frame_count": len(chunk),
                    "frames": chunk,
                })
        return packs

    def _build_reference_trace_semantics_prompt(self, evidence_pack: Dict[str, Any]) -> str:
        output_schema = {
            "format": "reference_trace_semantics",
            "runtime_state_observations": [
                {
                    "observation_id": "stable id",
                    "trace_id": "copy from evidence",
                    "step_index": 0,
                    "obs_key": "copy from evidence",
                    "page_cluster_id": "copy from evidence",
                    "state_scope": "model-defined concise semantic label",
                    "lifecycle": "model-defined lifecycle label",
                    "mutation": "model-defined operation label",
                    "value_hints": [],
                    "negative_constraints": [],
                    "evidence": []
                }
            ],
            "content_lifecycle_steps": [
                {
                    "trace_id": "copy from evidence",
                    "step_index": 0,
                    "obs_key": "copy from evidence",
                    "page_cluster_id": "copy from evidence",
                    "lifecycle_phase": "initial_collection|post_commit_collection|model-defined",
                    "exclude_from_initial_value_hints": [],
                    "reason": "short rationale grounded in the evidence"
                }
            ]
        }
        return (
            "You are the reference trace semantic extractor for a website reconstruction pipeline.\n"
            "Only the provided reference trace evidence is prior knowledge. Do not use external domain assumptions, app-specific rules, or keyword heuristics.\n"
            "Your job is to extract semantic runtime-state facts that are explicitly supported by the trace evidence.\n\n"
            "Requirements:\n"
            "- Produce runtime_state_observations only when the trace evidence shows state, lifecycle, mutation, constraints, or values relevant to reproducing the task behavior.\n"
            "- Use concise model-defined labels for state_scope, lifecycle, and mutation. Do not force a fixed taxonomy when the trace evidence calls for a more precise label.\n"
            "- If a value is created or changed during a trace and later appears in a collection/list, put that value in exclude_from_initial_value_hints for that post-commit observation.\n"
            "- Preserve trace_id, step_index, obs_key, and page_cluster_id exactly from the evidence.\n"
            "- Every semantic claim must cite short evidence strings copied or paraphrased from the provided evidence.\n"
            "- Return JSON only. The top-level JSON must be an object, not an array.\n\n"
            "Reference trace evidence:\n```json\n{}\n```\n\n"
            "Return JSON with this shape:\n```json\n{}\n```"
        ).format(
            json.dumps(evidence_pack, indent=2, ensure_ascii=False),
            json.dumps(output_schema, indent=2, ensure_ascii=False),
        )

    def _normalize_reference_trace_semantic_output(self, raw: Any, pack: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None

        result: Dict[str, Any] = {
            "format": "reference_trace_semantics",
            "runtime_state_observations": [],
            "content_lifecycle_steps": [],
            "_source_pack_id": pack.get("pack_id", "") if isinstance(pack, dict) else "",
            "_source_trace_id": pack.get("trace_id", "") if isinstance(pack, dict) else "",
            "_source_frame_count": len(pack.get("frames", [])) if isinstance(pack, dict) else 0,
        }

        if isinstance(raw, dict):
            result.update(raw)
            result.setdefault("format", "reference_trace_semantics")
            result.setdefault("runtime_state_observations", [])
            result.setdefault("content_lifecycle_steps", [])
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if item.get("lifecycle_phase") or item.get("exclude_from_initial_value_hints"):
                    result["content_lifecycle_steps"].append(item)
                else:
                    result["runtime_state_observations"].append(item)
        else:
            result["warning"] = "reference_trace_semantic_extractor returned non-object payload"

        if not isinstance(result.get("runtime_state_observations"), list):
            result["runtime_state_observations"] = []
        if not isinstance(result.get("content_lifecycle_steps"), list):
            result["content_lifecycle_steps"] = []
        result.setdefault("_source_pack_id", pack.get("pack_id", "") if isinstance(pack, dict) else "")
        result.setdefault("_source_trace_id", pack.get("trace_id", "") if isinstance(pack, dict) else "")
        result.setdefault("_source_frame_count", len(pack.get("frames", [])) if isinstance(pack, dict) else 0)
        return result

    def _load_cached_reference_trace_semantics(
        self,
        output_dir: str,
        expected_packs: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not output_dir:
            return None
        path = (
            str(self.pom.formal("trace_semantic_extraction", "reference_trace_semantics.json"))
            if self.pom
            else os.path.join(output_dir, "phase_trace_semantic_extraction", "reference_trace_semantics.json")
        )
        expected_ids = {
            str(pack.get("pack_id", "") or "")
            for pack in expected_packs or []
            if isinstance(pack, dict) and str(pack.get("pack_id", "") or "")
        }
        if not os.path.exists(path):
            self._write_reference_trace_semantics_cache_decision(
                output_dir,
                artifact_path=path,
                reused=False,
                reason="canonical_artifact_missing",
                expected_pack_ids=expected_ids,
                would_call_llm=True,
            )
            return None
        payload = self._read_json_file_safely(path, {})
        if not isinstance(payload, dict):
            self._write_reference_trace_semantics_cache_decision(
                output_dir,
                artifact_path=path,
                reused=False,
                reason="payload_not_object",
                expected_pack_ids=expected_ids,
                would_call_llm=True,
            )
            return None
        if payload.get("format") != "reference_trace_semantics":
            self._write_reference_trace_semantics_cache_decision(
                output_dir,
                artifact_path=path,
                reused=False,
                reason="format_mismatch",
                expected_pack_ids=expected_ids,
                would_call_llm=True,
                details={"observed_format": payload.get("format")},
            )
            return None
        chunks = payload.get("chunks", [])
        if not isinstance(chunks, list):
            self._write_reference_trace_semantics_cache_decision(
                output_dir,
                artifact_path=path,
                reused=False,
                reason="chunks_not_list",
                expected_pack_ids=expected_ids,
                would_call_llm=True,
            )
            return None
        cached_ids = {
            str(chunk.get("pack_id", "") or "")
            for chunk in chunks
            if isinstance(chunk, dict) and str(chunk.get("pack_id", "") or "")
        }
        if expected_ids and not expected_ids.issubset(cached_ids):
            missing_ids = sorted(expected_ids - cached_ids)
            self._write_reference_trace_semantics_cache_decision(
                output_dir,
                artifact_path=path,
                reused=False,
                reason="missing_expected_pack_ids",
                expected_pack_ids=expected_ids,
                cached_pack_ids=cached_ids,
                missing_pack_ids=missing_ids,
                would_call_llm=True,
            )
            return None
        cached = json.loads(json.dumps(payload, ensure_ascii=False))
        cached["cache_reused"] = True
        cached.setdefault("summary", {})["cache_reused"] = True
        self._write_reference_trace_semantics_cache_decision(
            output_dir,
            artifact_path=path,
            reused=True,
            reason="canonical_artifact_present",
            expected_pack_ids=expected_ids,
            cached_pack_ids=cached_ids,
            would_call_llm=False,
        )
        return cached

    def _write_reference_trace_semantics_cache_decision(
        self,
        output_dir: str,
        *,
        artifact_path: str,
        reused: bool,
        reason: str,
        expected_pack_ids: Optional[set[str]] = None,
        cached_pack_ids: Optional[set[str]] = None,
        missing_pack_ids: Optional[List[str]] = None,
        would_call_llm: bool,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not output_dir:
            return
        expected_ids = sorted(str(item) for item in (expected_pack_ids or set()) if str(item))
        cached_ids = sorted(str(item) for item in (cached_pack_ids or set()) if str(item))
        rel_path = os.path.relpath(artifact_path, output_dir) if artifact_path else ""
        decision: Dict[str, Any] = {
            "format": "reference_trace_semantics_cache_decision",
            "schema_revision": 1,
            "artifact_path": rel_path,
            "canonical_only": True,
            "reused": bool(reused),
            "reason": str(reason or "unknown"),
            "would_call_llm": bool(would_call_llm),
            "manual_invalidation_policy": "delete_or_deprecate_canonical_artifact_to_force_recompute",
            "expected_pack_count": len(expected_ids),
            "cached_pack_count": len(cached_ids),
            "expected_pack_ids": expected_ids,
            "cached_pack_ids": cached_ids,
            "missing_pack_ids": list(missing_pack_ids or []),
        }
        if details:
            decision["details"] = dict(details)
        self._last_reference_trace_semantics_cache_decision = decision
        try:
            pom = self.pom or PhaseOutputManager(output_dir)
            pom.write_json(
                "trace_semantic_extraction",
                "debug",
                "reference_trace_semantics_cache_decision.json",
                decision,
            )
        except Exception:
            pass

    async def _extract_reference_trace_semantics(
        self,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
        output_dir: str = "",
    ) -> Dict[str, Any]:
        evidence_packs = self._build_reference_trace_evidence_packs(
            clusters=clusters,
            traces=traces,
            page_info_map=page_info_map,
        )
        cached = self._load_cached_reference_trace_semantics(output_dir, evidence_packs)
        if cached is not None:
            if self.logger:
                self.logger.log_info(
                    "[SM-P4a] Reused reference trace semantic extraction cache: {} chunks, {} runtime observations, {} lifecycle steps".format(
                        len(cached.get("chunks", []) if isinstance(cached.get("chunks", []), list) else []),
                        len(cached.get("runtime_state_observations", []) if isinstance(cached.get("runtime_state_observations", []), list) else []),
                        len(cached.get("content_lifecycle_steps", []) if isinstance(cached.get("content_lifecycle_steps", []), list) else []),
                    )
                )
            return cached
        if self.logger:
            self.logger.log_info(
                "[SM-P4a] Reference trace semantic extraction: {} frames in {} trace-scoped packs (max {} frames/pack)".format(
                    sum(len(pack.get("frames", [])) for pack in evidence_packs),
                    len(evidence_packs),
                    self.REFERENCE_TRACE_SEMANTIC_MAX_FRAMES_PER_PACK,
                )
            )

        producer_concurrency = 1
        if evidence_packs:
            sample_prompt = self._build_reference_trace_semantics_prompt(evidence_packs[0])
            producer_concurrency = estimate_llm_stage_parallelism(
                self.model,
                "reference_trace_semantic_extractor",
                {"type": "json_object"},
                [{"role": "user", "content": sample_prompt}],
                default=self.max_concurrent,
            )
            producer_concurrency = max(1, min(len(evidence_packs), int(producer_concurrency)))
        if self.logger:
            self.logger.log_info(
                "[SM-P4a] Reference trace semantic extraction producer_concurrency={}".format(
                    producer_concurrency
                )
            )
        producer_semaphore = asyncio.Semaphore(producer_concurrency)

        async def _extract_pack(pack: Dict[str, Any]) -> Dict[str, Any]:
            prompt = self._build_reference_trace_semantics_prompt(pack)
            raw, _usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                reasoning_effort="medium",
                model=self.model,
                stage="reference_trace_semantic_extractor",
            )
            normalized = self._normalize_reference_trace_semantic_output(raw, pack)
            if normalized.get("warning") and self.logger:
                self.logger.log_warning(
                    "{} for {}".format(normalized["warning"], pack.get("pack_id", ""))
                )
            return normalized

        async def _extract_pack_gated(pack: Dict[str, Any]) -> Dict[str, Any]:
            async with producer_semaphore:
                return await _extract_pack(pack)

        first_pass = await asyncio.gather(
            *[_extract_pack_gated(pack) for pack in evidence_packs],
            return_exceptions=True,
        )
        outputs: List[Optional[Dict[str, Any]]] = [None] * len(evidence_packs)
        failed_indexes: List[int] = []
        for index, result in enumerate(first_pass):
            if isinstance(result, Exception):
                failed_indexes.append(index)
            else:
                outputs[index] = result

        if failed_indexes:
            if self.logger:
                self.logger.log_warning(
                    "[SM-P4a] Reference trace semantic extraction retrying {} failed pack(s): {}".format(
                        len(failed_indexes),
                        ", ".join(
                            str(evidence_packs[index].get("pack_id", "pack_{}".format(index)))
                            for index in failed_indexes
                        ),
                    )
                )
            retry_pass = await asyncio.gather(
                *[_extract_pack_gated(evidence_packs[index]) for index in failed_indexes],
                return_exceptions=True,
            )
            unrecovered = []
            for index, result in zip(failed_indexes, retry_pass):
                if isinstance(result, Exception):
                    unrecovered.append((index, result))
                else:
                    outputs[index] = result
            if unrecovered:
                detail = "; ".join(
                    "{}: {}".format(
                        evidence_packs[index].get("pack_id", "pack_{}".format(index)),
                        error,
                    )
                    for index, error in unrecovered
                )
                raise RuntimeError(
                    "reference_trace_semantic_extractor failed after retry for {}".format(detail)
                ) from unrecovered[0][1]

        return self._merge_reference_trace_semantic_outputs(
            [output for output in outputs if isinstance(output, dict)]
        )

    def _merge_reference_trace_semantic_outputs(self, outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {
            "format": "reference_trace_semantics",
            "schema_revision": 1,
            "runtime_state_observations": [],
            "content_lifecycle_steps": [],
            "chunks": [],
        }
        seen_runtime = set()
        seen_lifecycle = set()
        for index, output in enumerate(outputs or []):
            if not isinstance(output, dict):
                continue
            pack_id = str(output.get("_source_pack_id", "") or "chunk_{}".format(index))
            merged["chunks"].append({
                "pack_id": pack_id,
                "trace_id": str(output.get("_source_trace_id", "") or ""),
                "frame_count": int(output.get("_source_frame_count", 0) or 0),
                "runtime_state_observation_count": len(output.get("runtime_state_observations", []) if isinstance(output.get("runtime_state_observations", []), list) else []),
                "content_lifecycle_step_count": len(output.get("content_lifecycle_steps", []) if isinstance(output.get("content_lifecycle_steps", []), list) else []),
            })
            observations = output.get("runtime_state_observations", []) or output.get("observations", [])
            for item in observations if isinstance(observations, list) else []:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("observation_id", "") or ""),
                    str(item.get("trace_id", "") or ""),
                    str(item.get("step_index", "") or ""),
                    str(item.get("page_cluster_id", "") or ""),
                    str(item.get("state_scope", "") or item.get("scope", "") or ""),
                )
                if key in seen_runtime:
                    continue
                seen_runtime.add(key)
                cloned = self._clone_jsonish(item) if hasattr(self, "_clone_jsonish") else dict(item)
                cloned.setdefault("source_pack_id", pack_id)
                merged["runtime_state_observations"].append(cloned)
            lifecycle_steps = output.get("content_lifecycle_steps", [])
            for item in lifecycle_steps if isinstance(lifecycle_steps, list) else []:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("trace_id", "") or ""),
                    str(item.get("step_index", "") or ""),
                    str(item.get("obs_key", "") or ""),
                    str(item.get("page_cluster_id", "") or ""),
                    str(item.get("lifecycle_phase", "") or ""),
                )
                if key in seen_lifecycle:
                    continue
                seen_lifecycle.add(key)
                cloned = self._clone_jsonish(item) if hasattr(self, "_clone_jsonish") else dict(item)
                cloned.setdefault("source_pack_id", pack_id)
                merged["content_lifecycle_steps"].append(cloned)
        return merged


    def _derive_content_lifecycle_plan_from_runtime_state_observations(
        self,
        runtime_state_observations: Dict[str, Any],
    ) -> Dict[str, Any]:
        clusters: Dict[str, Dict[str, Any]] = {}
        for item in runtime_state_observations.get("content_lifecycle_steps", []):
            if not isinstance(item, dict):
                continue
            cluster_key = str(item.get("page_cluster_id", "") or "")
            if not cluster_key:
                continue
            cluster = clusters.setdefault(cluster_key, {
                "page_cluster_id": cluster_key,
                "page_name": str(item.get("page_name", "") or ""),
                "page_type_hint": str(item.get("page_type_hint", "") or ""),
                "initial_obs_keys": [],
                "runtime_obs_keys": [],
                "exclude_from_initial_value_hints": [],
                "steps": [],
            })
            obs_key = str(item.get("obs_key", "") or "")
            phase = str(item.get("lifecycle_phase", "") or "")
            if phase == "post_commit_collection":
                if obs_key and obs_key not in cluster["runtime_obs_keys"]:
                    cluster["runtime_obs_keys"].append(obs_key)
                for hint in item.get("exclude_from_initial_value_hints", []) if isinstance(item.get("exclude_from_initial_value_hints", []), list) else []:
                    if hint not in cluster["exclude_from_initial_value_hints"]:
                        cluster["exclude_from_initial_value_hints"].append(hint)
            else:
                if obs_key and obs_key not in cluster["initial_obs_keys"]:
                    cluster["initial_obs_keys"].append(obs_key)
            cluster["steps"].append({
                "obs_key": obs_key,
                "trace_id": str(item.get("trace_id", "") or ""),
                "step_index": int(item.get("step_index", 0) or 0),
                "lifecycle_phase": phase or "initial_collection",
                "exclude_from_initial_value_hints": self._list_of_strings(
                    item.get("exclude_from_initial_value_hints", [])
                ),
                "reason": str(item.get("reason", "") or ""),
            })
        return {
            "schema": "content_lifecycle_plan",
            "contract_version": self.CONTENT_EXTRACTION_CONTRACT_VERSION,
            "clusters": clusters,
        }

    def _build_materialized_semantic_view(
        self,
        runtime_state_observations: Dict[str, Any],
        global_reasoning: Dict[str, Any],
    ) -> Dict[str, Any]:
        label_by_key: Dict[str, Dict[str, Any]] = {}
        for label in global_reasoning.get("step_labels", []) if isinstance(global_reasoning, dict) else []:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "") or "").strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            if trace_id and step_index >= 0:
                label_by_key["{}:{}".format(trace_id, step_index)] = label

        view: Dict[str, Any] = {
            "format": "materialized_semantic_view",
            "schema_revision": 1,
            "observations_by_obs_key": {},
            "observations_by_materialized_page_id": {},
            "unresolved_observation_ids": [],
        }
        observations = runtime_state_observations.get("observations", []) if isinstance(runtime_state_observations, dict) else []
        for item in observations if isinstance(observations, list) else []:
            if not isinstance(item, dict):
                continue
            obs_key = str(item.get("obs_key", "") or "").strip()
            if not obs_key:
                trace_id = str(item.get("trace_id", "") or "").strip()
                step_index = item.get("step_index", "")
                if trace_id and str(step_index) != "":
                    obs_key = "{}:{}".format(trace_id, int(step_index))
            label = label_by_key.get(obs_key, {})
            if not label:
                observation_id = str(item.get("observation_id", "") or obs_key)
                if observation_id:
                    view["unresolved_observation_ids"].append(observation_id)
                continue

            materialized_page_id = str(
                label.get("materialized_page_id")
                or label.get("page_cluster_id")
                or ""
            ).strip()
            general_page_id = str(
                label.get("general_page_id")
                or label.get("source_page_cluster_id")
                or item.get("page_cluster_id", "")
                or ""
            ).strip()
            if not materialized_page_id:
                observation_id = str(item.get("observation_id", "") or obs_key)
                if observation_id:
                    view["unresolved_observation_ids"].append(observation_id)
                continue

            cloned = self._clone_jsonish(item) if hasattr(self, "_clone_jsonish") else dict(item)
            cloned["obs_key"] = obs_key
            cloned["materialized_page_id"] = materialized_page_id
            cloned["general_page_id"] = general_page_id
            cloned["source_page_cluster_id"] = str(label.get("source_page_cluster_id") or general_page_id).strip()
            cloned["instance_key"] = str(label.get("instance_key", "") or "").strip()
            cloned["instance_label"] = str(label.get("instance_label", "") or "").strip()
            view["observations_by_obs_key"].setdefault(obs_key, []).append(cloned)
            group = view["observations_by_materialized_page_id"].setdefault(materialized_page_id, {
                "materialized_page_id": materialized_page_id,
                "general_page_id": general_page_id,
                "source_page_cluster_id": cloned["source_page_cluster_id"],
                "instance_key": cloned["instance_key"],
                "instance_label": cloned["instance_label"],
                "observations": [],
            })
            group["observations"].append(cloned)
        return view

    def _build_runtime_state_obligations(
        self,
        runtime_state_observations: Dict[str, Any],
    ) -> Dict[str, Any]:
        obligations: List[Dict[str, Any]] = []
        by_observation_id: Dict[str, List[Dict[str, Any]]] = {}
        by_trace_id: Dict[str, List[Dict[str, Any]]] = {}
        by_materialized_page_id: Dict[str, List[Dict[str, Any]]] = {}

        observations = (
            runtime_state_observations.get("observations", [])
            if isinstance(runtime_state_observations, dict)
            else []
        )
        materialized_view = {}
        if isinstance(runtime_state_observations, dict) and isinstance(runtime_state_observations.get("derived", {}), dict):
            materialized_view = runtime_state_observations.get("derived", {}).get("materialized_semantic_view", {})
        observations_by_obs_key = (
            materialized_view.get("observations_by_obs_key", {})
            if isinstance(materialized_view, dict)
            else {}
        )

        for item in observations if isinstance(observations, list) else []:
            if not isinstance(item, dict):
                continue
            observation_id = str(item.get("observation_id", "") or "").strip()
            if not observation_id:
                continue
            trace_id = str(item.get("trace_id", "") or "").strip()
            step_index = item.get("step_index", "")
            obs_key = str(item.get("obs_key", "") or "").strip()
            materialized_item = {}
            materialized_matches = observations_by_obs_key.get(obs_key, []) if isinstance(observations_by_obs_key, dict) else []
            if isinstance(materialized_matches, list):
                for candidate in materialized_matches:
                    if not isinstance(candidate, dict):
                        continue
                    if str(candidate.get("observation_id", "") or "").strip() == observation_id:
                        materialized_item = candidate
                        break
                if not materialized_item:
                    materialized_item = next((candidate for candidate in materialized_matches if isinstance(candidate, dict)), {})
            materialized_page_id = str(
                materialized_item.get("materialized_page_id", "")
                if isinstance(materialized_item, dict)
                else ""
            ).strip()
            general_page_id = str(
                materialized_item.get("general_page_id", "")
                if isinstance(materialized_item, dict)
                else item.get("page_cluster_id", "")
            ).strip()
            obligation = {
                "obligation_id": "rso:{}".format(observation_id),
                "observation_id": observation_id,
                "trace_id": trace_id,
                "step_index": int(step_index or 0),
                "obs_key": obs_key,
                "page_cluster_id": str(item.get("page_cluster_id", "") or "").strip(),
                "general_page_id": general_page_id,
                "materialized_page_id": materialized_page_id,
                "state_scope": str(item.get("state_scope", "") or item.get("scope", "") or "").strip(),
                "lifecycle": str(item.get("lifecycle", "") or "").strip(),
                "mutation": str(item.get("mutation", "") or "").strip(),
                "value_hints": self._list_of_strings(item.get("value_hints", [])),
                "terminal_constraints": self._list_of_strings(item.get("negative_constraints", [])),
                "evidence_steps": self._list_of_strings(item.get("evidence_steps", [])) or ([obs_key] if obs_key else []),
                "raw_evidence": str(item.get("raw_evidence", "") or ""),
                "source": "model_reference_trace_semantics",
            }


            if materialized_page_id and obs_key:
                provenance = Provenance(mid=materialized_page_id, source_step=obs_key)
                attach_provenance(obligation, provenance)


                if isinstance(item, dict):
                    attach_provenance(item, provenance)
            item["runtime_state_obligation_ids"] = [obligation["obligation_id"]]
            obligations.append(obligation)
            by_observation_id.setdefault(observation_id, []).append(obligation)
            if trace_id:
                by_trace_id.setdefault(trace_id, []).append(obligation)
            if materialized_page_id:
                by_materialized_page_id.setdefault(materialized_page_id, []).append(obligation)

        return {
            "format": "runtime_state_obligations",
            "schema_revision": 1,
            "obligations": obligations,
            "obligations_by_observation_id": by_observation_id,
            "obligations_by_trace_id": by_trace_id,
            "obligations_by_materialized_page_id": by_materialized_page_id,
        }

    def _build_runtime_state_observations(
        self,
        clusters: Dict[str, List[FrameObservation]],
        traces: List[TraceData],
        page_info_map: Dict[str, Dict[str, Any]],
        semantic_artifact: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "format": "runtime_state_observations",
            "schema_revision": 1,
            "contract_version": self.CONTENT_EXTRACTION_CONTRACT_VERSION,
            "observations": [],
            "observations_by_page_cluster_id": {},
            "content_lifecycle_steps": [],
            "derived": {},
        }

        if not isinstance(semantic_artifact, dict):
            result["derived"]["content_lifecycle_plan"] = self._derive_content_lifecycle_plan_from_runtime_state_observations(result)
            result["derived"]["runtime_state_obligations"] = self._build_runtime_state_obligations(result)
            return result

        valid_cluster_keys = set((clusters or {}).keys())
        semantic_observations = (
            semantic_artifact.get("runtime_state_observations")
            or semantic_artifact.get("observations")
            or []
        )
        for item in semantic_observations if isinstance(semantic_observations, list) else []:
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "") or "").strip()
            step_index = item.get("step_index", "")
            obs_key = str(item.get("obs_key", "") or "").strip()
            if not obs_key and trace_id and str(step_index) != "":
                obs_key = "{}:{}".format(trace_id, int(step_index))
            cluster_key = str(item.get("page_cluster_id", "") or item.get("general_page_id", "") or "").strip()
            if not cluster_key:
                continue
            if valid_cluster_keys and cluster_key not in valid_cluster_keys:
                continue
            page_meta = page_info_map.get(cluster_key, {}) if isinstance(page_info_map, dict) else {}
            scope = str(item.get("state_scope", "") or item.get("scope", "") or item.get("state_label", "") or "").strip()
            if not scope:
                continue
            observation = {
                "observation_id": str(
                    item.get("observation_id", "")
                    or item.get("semantic_event_id", "")
                    or "{}:{}:{}".format(trace_id, step_index, scope)
                ),
                "trace_id": trace_id,
                "step_index": int(step_index or 0),
                "obs_key": obs_key,
                "page_cluster_id": cluster_key,
                "page_name": str(page_meta.get("page_name", "") or item.get("page_name", "") or "").strip(),
                "page_type_hint": str(page_meta.get("page_type_hint", "") or item.get("page_type_hint", "") or "").strip(),
                "scope": scope,
                "state_scope": scope,
                "lifecycle": str(item.get("lifecycle", "") or item.get("lifecycle_phase", "") or "").strip(),
                "mutation": str(item.get("mutation", "") or item.get("operation_label", "") or "").strip(),
                "raw_evidence": "\n".join(self._list_of_strings(item.get("evidence", []))) or str(item.get("raw_evidence", "") or ""),
                "evidence_steps": self._list_of_strings(item.get("evidence_steps", [])) or ([obs_key] if obs_key else []),
                "value_hints": self._list_of_strings(item.get("value_hints", [])) or self._list_of_strings(item.get("value_mentions", [])),
                "negative_constraints": self._list_of_strings(item.get("negative_constraints", [])),
                "source": "model_reference_trace_semantics",
            }
            result["observations"].append(observation)
            result["observations_by_page_cluster_id"].setdefault(cluster_key, []).append(observation)

        lifecycle_steps = semantic_artifact.get("content_lifecycle_steps", [])
        for item in lifecycle_steps if isinstance(lifecycle_steps, list) else []:
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "") or "").strip()
            step_index = item.get("step_index", "")
            obs_key = str(item.get("obs_key", "") or "").strip()
            if not obs_key and trace_id and str(step_index) != "":
                obs_key = "{}:{}".format(trace_id, int(step_index))
            cluster_key = str(item.get("page_cluster_id", "") or item.get("general_page_id", "") or "").strip()
            if not cluster_key:
                continue
            if valid_cluster_keys and cluster_key not in valid_cluster_keys:
                continue
            page_meta = page_info_map.get(cluster_key, {}) if isinstance(page_info_map, dict) else {}
            result["content_lifecycle_steps"].append({
                "page_cluster_id": cluster_key,
                "page_name": str(page_meta.get("page_name", "") or item.get("page_name", "") or "").strip(),
                "page_type_hint": str(page_meta.get("page_type_hint", "") or item.get("page_type_hint", "") or "").strip(),
                "obs_key": obs_key,
                "trace_id": trace_id,
                "step_index": int(step_index or 0),
                "lifecycle_phase": str(item.get("lifecycle_phase", "") or "").strip(),
                "exclude_from_initial_value_hints": self._list_of_strings(item.get("exclude_from_initial_value_hints", [])) or self._list_of_strings(item.get("value_hints", [])),
                "reason": str(item.get("reason", "") or "model_reference_trace_semantics"),
            })
        result["derived"]["content_lifecycle_plan"] = self._derive_content_lifecycle_plan_from_runtime_state_observations(result)
        result["derived"]["runtime_state_obligations"] = self._build_runtime_state_obligations(result)
        return result

    def _build_content_lifecycle_context(
        self,
        cluster_key: str,
        cluster_obs: List[FrameObservation],
        lifecycle_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        if isinstance(lifecycle_plan, dict) and lifecycle_plan.get("format") == "runtime_state_observations":
            lifecycle_plan = lifecycle_plan.get("derived", {}).get("content_lifecycle_plan", {})
        clusters = lifecycle_plan.get("clusters", {}) if isinstance(lifecycle_plan, dict) else {}
        cluster_plan = clusters.get(cluster_key, {}) if isinstance(clusters, dict) else {}
        if not isinstance(cluster_plan, dict):
            return {}
        job_keys = {self._obs_key(obs) for obs in cluster_obs}
        opened_overlay_keys = {
            self._obs_key(obs) for obs in cluster_obs
            if str(obs.page_state.get("interaction_surface_role", "") or "none").strip().lower()
            in {"opened_overlay", "opened_drawer", "opened_action_sheet", "opened_modal"}
        }
        steps = [
            step for step in cluster_plan.get("steps", [])
            if isinstance(step, dict) and str(step.get("obs_key", "")) in job_keys
        ]
        scoped_hints: List[str] = []
        for step in steps:
            for hint in self._list_of_strings(step.get("exclude_from_initial_value_hints", [])):
                if hint not in scoped_hints:
                    scoped_hints.append(hint)
        if not scoped_hints and not cluster_plan.get("steps"):
            runtime_keys = {
                str(key)
                for key in cluster_plan.get("runtime_obs_keys", [])
                if str(key) in job_keys
            }
            if runtime_keys:
                scoped_hints = list(cluster_plan.get("exclude_from_initial_value_hints", []))
        return {
            "contract_version": self.CONTENT_EXTRACTION_CONTRACT_VERSION,
            "page_cluster_id": cluster_key,
            "initial_obs_keys": [
                key for key in cluster_plan.get("initial_obs_keys", [])
                if key in job_keys and key not in opened_overlay_keys
            ],
            "runtime_obs_keys": [
                key for key in cluster_plan.get("runtime_obs_keys", [])
                if key in job_keys
            ],
            "exclude_from_initial_value_hints": scoped_hints,
            "steps": steps,
        }

    @staticmethod
    def _obs_key(obs: FrameObservation) -> str:
        return f"{obs.trace_id}:{int(obs.step_index)}"

    def _apply_materialized_split_principle(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
        cluster_obs: List[FrameObservation],
        lifecycle_plan: Dict[str, Any],
        page_context: Optional[Dict[str, str]] = None,
        state_authority_by_obs_key: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
        parameterization_sink: Optional[Dict[str, Any]] = None,
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        instance_groups = self._normalize_search_results_submitted_query_groups(
            instance_groups,
            page_context=page_context,
        )
        instance_groups = self._complete_search_results_submitted_query_groups(
            cluster_key,
            instance_groups,
            cluster_obs,
            lifecycle_plan,
            page_context,
        )
        instance_groups = self._demote_submitted_query_instance_groups(
            cluster_key,
            instance_groups,
            page_context=page_context,
        )
        instance_groups = self._demote_parameterizable_state_instance_groups(
            cluster_key,
            instance_groups,
            state_authority_by_obs_key=state_authority_by_obs_key or {},
            parameterization_sink=parameterization_sink,
        )
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        def group_obs(group: Dict[str, Any]) -> List[FrameObservation]:
            return [obs for obs in group.get("cluster_obs", []) or [] if isinstance(obs, FrameObservation)]

        cluster_trace_steps = {
            (str(obs.trace_id), int(obs.step_index))
            for obs in cluster_obs
            if isinstance(obs, FrameObservation)
        }

        def is_forced_scroll_continuation_group(group: Dict[str, Any]) -> bool:
            obs_list = group_obs(group)
            return bool(obs_list) and all(
                self._is_true_same_page_continuation_observation(obs)
                and (str(obs.trace_id), int(obs.step_index) - 1) in cluster_trace_steps
                for obs in obs_list
            )

        merge_source_indices = [
            index
            for index, group in enumerate(instance_groups)
            if is_forced_scroll_continuation_group(group)
        ]
        if not merge_source_indices:
            return instance_groups

        default_candidates = [
            (index, group)
            for index, group in enumerate(instance_groups)
            if index not in merge_source_indices
        ]
        if not default_candidates:
            return instance_groups
        default_index, _default_group = max(
            default_candidates,
            key=lambda item: len(group_obs(item[1])),
        )

        anchor_group_by_obs_key: Dict[str, int] = {}
        if isinstance(boundary_preservation_constraint, dict):
            separation_groups = boundary_preservation_constraint.get(
                "must_separate_obs_key_groups", []
            )
            if isinstance(separation_groups, list):
                for anchor_group_index, anchor_group in enumerate(separation_groups):
                    if not isinstance(anchor_group, list):
                        continue
                    for obs_key in anchor_group:
                        normalized = str(obs_key or "").strip()
                        if normalized:
                            anchor_group_by_obs_key[normalized] = anchor_group_index

        def protected_anchor_groups(group_index: int) -> set[int]:
            return {
                anchor_group_by_obs_key[normalized]
                for obs_key in instance_groups[group_index].get("obs_keys", []) or []
                if (normalized := str(obs_key or "").strip())
                in anchor_group_by_obs_key
            }

        merge_source_indices = [
            source_index
            for source_index in merge_source_indices
            if len(
                protected_anchor_groups(source_index)
                | protected_anchor_groups(default_index)
            )
            <= 1
        ]
        if not merge_source_indices:
            return instance_groups

        merged_groups: List[Dict[str, Any]] = []
        default_target: Optional[Dict[str, Any]] = None
        for index, group in enumerate(instance_groups):
            if index in merge_source_indices and index != default_index:
                continue
            cloned = dict(group)
            cloned["trace_ids"] = list(group.get("trace_ids", []) or [])
            cloned["obs_keys"] = list(group.get("obs_keys", []) or [])
            cloned["cluster_obs"] = list(group.get("cluster_obs", []) or [])
            if index == default_index:
                default_target = cloned
            merged_groups.append(cloned)

        if default_target is None:
            return instance_groups

        existing_obs = {
            (str(obs.trace_id), int(obs.step_index))
            for obs in default_target.get("cluster_obs", []) or []
            if isinstance(obs, FrameObservation)
        }
        existing_trace_ids = {str(trace_id) for trace_id in default_target.get("trace_ids", []) or []}
        for source_index in merge_source_indices:
            if source_index == default_index:
                continue
            source = instance_groups[source_index]
            for obs in group_obs(source):
                obs_id = (str(obs.trace_id), int(obs.step_index))
                if obs_id not in existing_obs:
                    default_target.setdefault("cluster_obs", []).append(obs)
                    existing_obs.add(obs_id)
                existing_trace_ids.add(str(obs.trace_id))

        default_target["trace_ids"] = sorted(existing_trace_ids)
        default_target["cluster_obs"] = sorted(
            default_target.get("cluster_obs", []) or [],
            key=lambda obs: (str(obs.trace_id), int(obs.step_index)) if isinstance(obs, FrameObservation) else ("", -1),
        )
        default_target["obs_keys"] = [
            self._obs_key(obs)
            for obs in default_target["cluster_obs"]
            if isinstance(obs, FrameObservation)
        ]

        if len(merged_groups) < len(instance_groups) and self.logger:
            self.logger.log_info(
                f"[Instance Split Principle] {cluster_key}: merged "
                f"{len(instance_groups) - len(merged_groups)} same-page scroll instance candidates"
            )
        if len(merged_groups) < 2:
            return []
        return self._merge_form_future_action_instance_groups(
            cluster_key,
            page_context or {},
            merged_groups,
        )

    def _demote_parameterizable_state_instance_groups(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
        *,
        state_authority_by_obs_key: Dict[str, List[Dict[str, Any]]],
        parameterization_sink: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(instance_groups, list) or len(instance_groups) < 2:
            return instance_groups

        def split_dimension(group: Dict[str, Any]) -> str:
            reason = group.get("split_reason", {})
            return str(
                group.get("split_dimension", "")
                or (reason.get("dimension", "") if isinstance(reason, dict) else "")
                or ""
            ).strip().lower()

        parameterizable_dimensions = {
            "semantic_mode",
            "state_variant",
            "lifecycle_variant",
        }
        parameterizable_indices = [
            index
            for index, group in enumerate(instance_groups)
            if isinstance(group, dict)
            and split_dimension(group) in parameterizable_dimensions
            and any(
                state_authority_by_obs_key.get(str(obs_key or "").strip())
                for obs_key in group.get("obs_keys", []) or []
            )
        ]
        baseline_indices = [
            index
            for index, group in enumerate(instance_groups)
            if isinstance(group, dict)
            and split_dimension(group) in {"", "none", "runtime_projection"}
        ]
        merge_indices = list(parameterizable_indices)
        if len(parameterizable_indices) == len(instance_groups):
            pass
        elif len(baseline_indices) == 1 and (
            len(parameterizable_indices) + 1 == len(instance_groups)
        ):
            merge_indices.append(baseline_indices[0])
        else:
            return instance_groups
        if len(merge_indices) < 2:
            return instance_groups

        def initial_state_score(group: Dict[str, Any]) -> Tuple[int, int]:
            initial_count = 0
            for obs_key in group.get("obs_keys", []) or []:
                for row in state_authority_by_obs_key.get(str(obs_key), []) or []:
                    if self._state_authority_row_is_initial(row):
                        initial_count += 1
            return initial_count, len(group.get("obs_keys", []) or [])

        target_index = (
            baseline_indices[0]
            if len(baseline_indices) == 1
            and baseline_indices[0] in merge_indices
            else max(
                parameterizable_indices,
                key=lambda index: initial_state_score(instance_groups[index]),
            )
        )
        target = dict(instance_groups[target_index])
        target["trace_ids"] = list(target.get("trace_ids", []) or [])
        target["obs_keys"] = list(target.get("obs_keys", []) or [])
        target["cluster_obs"] = list(target.get("cluster_obs", []) or [])
        target_state_params = dict(target.get("state_params_by_obs_key") or {})
        variants: List[Dict[str, Any]] = []

        for source_index in parameterizable_indices:
            source = instance_groups[source_index]
            source_dimension = split_dimension(source)
            source_obs_keys = [
                str(obs_key or "").strip()
                for obs_key in source.get("obs_keys", []) or []
                if str(obs_key or "").strip()
            ]
            state_params_for_variant: Dict[str, Dict[str, Any]] = {}
            for obs_key in source_obs_keys:
                rows = list(state_authority_by_obs_key.get(obs_key, []) or [])
                if not rows:
                    continue
                params = {
                    "projection_kind": "runtime_state",
                    "runtime_projection": True,
                    "authority_source": (
                        "reference_trace_semantics.runtime_state_observations"
                    ),
                    "authority_scopes": [
                        "trace_l0_source_state_precondition",
                        "runtime_projection",
                    ],
                    "state_scopes": sorted({
                        str(row.get("state_scope", "") or "")
                        for row in rows
                        if str(row.get("state_scope", "") or "")
                    }),
                    "state_observations": rows,
                    "source_split_dimension": source_dimension,
                    "source_instance_key": str(
                        source.get("instance_key", "") or ""
                    ),
                }
                state_params_for_variant[obs_key] = params
                target_state_params.setdefault(obs_key, params)
            variants.append({
                "dimension": "runtime_projection",
                "source_split_dimension": source_dimension,
                "source_instance_key": str(source.get("instance_key", "") or ""),
                "source_instance_label": str(source.get("instance_label", "") or ""),
                "obs_keys": source_obs_keys,
                "state_params_by_obs_key": state_params_for_variant,
            })
            if source_index != target_index:
                self._merge_instance_group_into_target(target, source)

        target["state_params_by_obs_key"] = target_state_params
        target["split_dimension"] = "runtime_projection"
        target["split_reason"] = {
            "dimension": "runtime_projection",
            "same_general_page": True,
            "why_not_same_instance": "",
            "merge_reason": (
                "component state/lifecycle is a runtime projection, not "
                "Materialized Page identity"
            ),
            "demoted_instance_keys": [
                str(instance_groups[index].get("instance_key", "") or "")
                for index in parameterizable_indices
            ],
        }
        target["parameterized_state_variants"] = variants
        target["parameterized_state_variant_count"] = len(variants)

        if parameterization_sink is not None:
            parameterization_sink.setdefault(
                "parameterized_state_variants",
                [],
            ).extend(variants)
            parameterization_sink.setdefault(
                "demoted_split_dimensions",
                [],
            ).append({
                "source_dimensions": sorted({
                    split_dimension(instance_groups[index])
                    for index in parameterizable_indices
                }),
                "target_dimension": "runtime_projection",
                "source_instance_keys": [
                    str(instance_groups[index].get("instance_key", "") or "")
                    for index in parameterizable_indices
                ],
            })

        merged = [
            group
            for index, group in enumerate(instance_groups)
            if index not in merge_indices
        ]
        merged.append(target)
        merged = sorted(
            merged,
            key=lambda group: min(
                (
                    (str(obs.trace_id), int(obs.step_index))
                    for obs in group.get("cluster_obs", []) or []
                    if isinstance(obs, FrameObservation)
                ),
                default=("", -1),
            ),
        )
        if self.logger:
            self.logger.log_info(
                f"[Instance Split Principle] {cluster_key}: demoted "
                f"{len(parameterizable_indices)} parameterizable state/lifecycle "
                "instance candidates into runtime state projection"
            )
        if len(merged) < 2:
            return []
        return merged

    def _demote_generic_semantic_mode_instance_groups(
        self,
        cluster_key: str,
        instance_groups: List[Dict[str, Any]],
        *,
        state_authority_by_obs_key: Dict[str, List[Dict[str, Any]]],
        parameterization_sink: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self._demote_parameterizable_state_instance_groups(
            cluster_key,
            instance_groups,
            state_authority_by_obs_key=state_authority_by_obs_key,
            parameterization_sink=parameterization_sink,
        )

    def _content_item_matches_lifecycle_hint(self, item: Any, hints: List[str]) -> bool:
        if not hints:
            return False
        text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        return any(str(hint or "").strip() and str(hint).strip() in text for hint in hints)

    def _apply_content_lifecycle_filters(
        self,
        content: Dict[str, Any],
        lifecycle_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(content, dict):
            return {}
        if not isinstance(lifecycle_context, dict):
            return dict(content)
        hints = [
            str(hint).strip()
            for hint in lifecycle_context.get("exclude_from_initial_value_hints", [])
            if str(hint).strip()
        ]
        if not hints:
            return dict(content)

        filtered = json.loads(json.dumps(content, ensure_ascii=False))
        extracted_items = filtered.get("extracted_items", {})
        if not isinstance(extracted_items, dict):
            return filtered

        runtime_items = filtered.setdefault("runtime_items", {})
        excluded = filtered.setdefault("excluded_from_initial", [])
        for entity_name, items in list(extracted_items.items()):
            if entity_name == "_metadata" or not isinstance(items, list):
                continue
            kept_items = []
            for item in items:
                if self._content_item_matches_lifecycle_hint(item, hints):
                    runtime_items.setdefault(entity_name, []).append(item)
                    excluded.append({
                        "entity": entity_name,
                        "item": item,
                        "matched_hints": [
                            hint for hint in hints
                            if hint in json.dumps(item, ensure_ascii=False, sort_keys=True)
                        ],
                        "reason": "post-commit lifecycle value excluded from initial extracted_items",
                    })
                else:
                    kept_items.append(item)
            extracted_items[entity_name] = kept_items
        return filtered

    @staticmethod
    def _instance_split_entry_action_obs_keys(
        steps_payload: List[Dict[str, Any]],
    ) -> List[str]:
        keys: List[str] = []
        for step in steps_payload:
            if not isinstance(step, dict):
                continue
            action_context = step.get("action_context", {})
            previous_action = (
                str(action_context.get("previous_action", "") or "").strip()
                if isinstance(action_context, dict)
                else ""
            )
            if step.get("transition_from_previous") is not False and previous_action:
                continue
            current_action = (
                str(action_context.get("current_action", "") or "").strip()
                if isinstance(action_context, dict)
                else ""
            )
            if not current_action:
                continue
            obs_key = str(step.get("obs_key", "") or "").strip()
            if obs_key and obs_key not in keys:
                keys.append(obs_key)
        return keys

    @classmethod
    def _instance_split_entry_action_obs_keys_from_observations(
        cls,
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> List[str]:
        trace_map = {
            str(trace.trace_id): trace
            for trace in traces or []
            if isinstance(trace, TraceData)
        }
        steps_payload: List[Dict[str, Any]] = []
        for obs in observations or []:
            if not isinstance(obs, FrameObservation):
                continue
            obs_key = cls._obs_key(obs)
            page_state = obs.page_state if isinstance(obs.page_state, dict) else {}
            transition_from_previous = (
                obs.transition_from_previous
                if obs.transition_from_previous is not None
                else page_state.get("transition_from_previous")
            )
            action_context = page_state.get("action_context", {})
            if not isinstance(action_context, dict):
                action_context = {}
            previous_action = str(action_context.get("previous_action", "") or "").strip()
            trace = trace_map.get(str(obs.trace_id))
            if trace is not None:
                previous_steps = [
                    step
                    for step in trace.steps
                    if int(step.step_index) < int(obs.step_index)
                ]
                if previous_steps:
                    previous_action = str(
                        sorted(previous_steps, key=lambda step: int(step.step_index))[-1].action
                        or ""
                    ).strip()
            current_action = str(
                obs.action or action_context.get("current_action", "") or ""
            ).strip()
            steps_payload.append(
                {
                    "obs_key": obs_key,
                    "transition_from_previous": transition_from_previous,
                    "action_context": {
                        "previous_action": previous_action,
                        "current_action": current_action,
                    },
                }
            )
        return cls._instance_split_entry_action_obs_keys(steps_payload)

    @staticmethod
    def _instance_split_candidate_instance_obs_keys(instance: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        for obs_key in instance.get("obs_keys", []) or []:
            normalized = str(obs_key or "").strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
        for ref in instance.get("trace_steps", []) or []:
            normalized = AppSpecBuilder._instance_split_trace_step_obs_key(ref)
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys

    @staticmethod
    def _instance_split_trace_step_obs_key(ref: Any) -> str:
        if not isinstance(ref, dict):
            return ""
        trace_id = str(ref.get("trace_id", "") or "").strip()
        try:
            step_index = int(ref.get("step_index", -1))
        except (TypeError, ValueError):
            return ""
        return f"{trace_id}:{step_index}" if trace_id and step_index >= 0 else ""

    @classmethod
    def _instance_split_raw_member_duplicate_errors(
        cls,
        instances: List[Any],
        out_of_page: List[Any],
    ) -> List[str]:
        errors: List[str] = []
        for instance_index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                continue
            has_obs_keys = "obs_keys" in instance
            has_trace_steps = "trace_steps" in instance
            raw_obs_keys = instance.get("obs_keys", [])
            raw_trace_steps = instance.get("trace_steps", [])
            normalized_obs_keys: List[str] = []
            normalized_trace_keys: List[str] = []
            if has_obs_keys and not isinstance(raw_obs_keys, list):
                errors.append(f"instances[{instance_index}].obs_keys must be a list")
            elif isinstance(raw_obs_keys, list):
                for raw_index, raw_key in enumerate(raw_obs_keys):
                    key = str(raw_key or "").strip()
                    if not key:
                        errors.append(
                            f"instances[{instance_index}].obs_keys[{raw_index}] must be a non-empty obs_key"
                        )
                    else:
                        normalized_obs_keys.append(key)
            if has_trace_steps and not isinstance(raw_trace_steps, list):
                errors.append(f"instances[{instance_index}].trace_steps must be a list")
            elif isinstance(raw_trace_steps, list):
                for raw_index, ref in enumerate(raw_trace_steps):
                    key = cls._instance_split_trace_step_obs_key(ref)
                    if not key:
                        errors.append(
                            f"instances[{instance_index}].trace_steps[{raw_index}] must be a valid non-empty trace step reference"
                        )
                    else:
                        normalized_trace_keys.append(key)

            for field, keys in (
                ("obs_keys", normalized_obs_keys),
                ("trace_steps", normalized_trace_keys),
            ):
                seen: set[str] = set()
                for key in keys:
                    if key in seen:
                        errors.append(
                            f"instances[{instance_index}].{field} contains duplicate obs_key {key}"
                        )
                    seen.add(key)
        seen_out_of_page: set[str] = set()
        for out_index, item in enumerate(out_of_page):
            if not isinstance(item, dict):
                errors.append(f"out_of_page_obs[{out_index}] must be an object")
                continue
            key = str(item.get("obs_key", "") or "").strip()
            if not key:
                errors.append(
                    f"out_of_page_obs[{out_index}].obs_key must be a non-empty obs_key"
                )
                continue
            if key in seen_out_of_page:
                errors.append(
                    f"out_of_page_obs contains duplicate obs_key {key} (at index {out_index})"
                )
            seen_out_of_page.add(key)
        return errors

    @classmethod
    def _instance_split_candidate_requires_entry_action_audit(
        cls,
        candidate: Dict[str, Any],
        steps_payload: List[Dict[str, Any]],
    ) -> bool:
        eligible_keys = cls._instance_split_entry_action_obs_keys(steps_payload)


        return bool(eligible_keys)

    @classmethod
    def _validate_instance_split_entry_action_audit(
        cls,
        candidate: Dict[str, Any],
        steps_payload: List[Dict[str, Any]],
    ) -> List[str]:
        errors: List[str] = []
        eligible_keys = cls._instance_split_entry_action_obs_keys(steps_payload)
        eligible_set = set(eligible_keys)
        audit = candidate.get("entry_action_audit") if isinstance(candidate, dict) else None
        if not isinstance(audit, dict):
            if eligible_set:
                return ["missing entry_action_audit"]


            audit = {}

        input_keys = {
            str(step.get("obs_key", "") or "").strip()
            for step in steps_payload
            if isinstance(step, dict) and str(step.get("obs_key", "") or "").strip()
        }

        def normalized_list(field: str) -> List[str]:
            raw = audit.get(field, [])
            if not isinstance(raw, list):
                errors.append(f"entry_action_audit.{field} must be a list")
                return []
            values = [str(value or "").strip() for value in raw]
            values = [value for value in values if value]
            if len(values) != len(set(values)):
                errors.append(f"entry_action_audit.{field} contains duplicates")
            return values

        audited = normalized_list("audited_obs_keys")
        task_content = normalized_list("task_content_obs_keys")
        shell_controls = normalized_list("stable_shell_control_obs_keys")
        audited_set = set(audited)
        task_set = set(task_content)
        shell_set = set(shell_controls)

        if audited_set != eligible_set:
            errors.append(
                "audited_obs_keys must exactly cover eligible entry observations: "
                f"missing={sorted(eligible_set - audited_set)}, extra={sorted(audited_set - eligible_set)}"
            )
        if task_set & shell_set:
            errors.append(f"task/shell partition overlaps: {sorted(task_set & shell_set)}")
        if task_set | shell_set != audited_set:
            errors.append("task/shell partition must exactly cover audited_obs_keys")

        fixed_membership: Dict[str, int] = {}
        instances = candidate.get("instances", []) if isinstance(candidate, dict) else []
        if not isinstance(instances, list):
            errors.append("instances must be a list")
            instances = []


        resolved_membership, membership_errors = cls._resolve_instance_split_membership(
            candidate,
            steps_payload,
        )
        errors.extend(membership_errors)
        assignment_counts: Dict[str, int] = {
            obs_key: len(owners)
            for obs_key, owners in resolved_membership.items()
        }
        instance_dimensions_by_obs_key: Dict[str, List[str]] = {}
        if isinstance(candidate, dict) and not bool(candidate.get("split_needed", True)):


            instance_dimensions_by_obs_key = {
                obs_key: ["cluster_default"]
                for obs_key in resolved_membership
            }
        for index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                continue
            instance_member_keys = set(cls._instance_split_candidate_instance_obs_keys(instance))
            instance_dimension = str(instance.get("split_dimension", "") or "none").strip() or "none"
            for obs_key in instance_member_keys:
                instance_dimensions_by_obs_key.setdefault(obs_key, []).append(instance_dimension)
            if str(instance.get("split_dimension", "") or "").strip() != "fixed_capacity_task_content":
                continue
            member_keys = list(instance_member_keys)
            member_set = set(member_keys)
            split_reason = instance.get("split_reason", {})
            rows = split_reason.get("member_action_audit", []) if isinstance(split_reason, dict) else []
            if not isinstance(rows, list):
                rows = []
            row_keys: List[str] = []
            visible_entities_by_slot: Dict[str, set[str]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                obs_key = str(row.get("obs_key", "") or "").strip()
                if obs_key:
                    row_keys.append(obs_key)
                target_role = str(row.get("target_role", "") or "").strip()
                if target_role not in ("task_content", "stable_shell_control"):
                    errors.append(
                        f"fixed-capacity instance {index} member {obs_key or '<missing>'} "
                        f"has unknown target_role={target_role!r}"
                    )
                elif target_role != "task_content":
                    errors.append(
                        f"fixed-capacity instance {index} has non-task-content member {obs_key or '<missing>'}"
                    )
                bounded_slot = str(row.get("bounded_slot", "") or "none").strip().lower()
                visible_entity = cls._normalized_instance_split_text(
                    row.get("visible_entity", "")
                ).strip()
                if bounded_slot not in ("", "none"):
                    if not visible_entity:
                        errors.append(
                            f"fixed-capacity instance {index} member {obs_key or '<missing>'} "
                            "has a bounded slot without visible_entity"
                        )
                    else:
                        visible_entities_by_slot.setdefault(bounded_slot, set()).add(
                            visible_entity
                        )
            for bounded_slot, visible_entities in sorted(visible_entities_by_slot.items()):
                if len(visible_entities) > 1:
                    errors.append(
                        f"fixed-capacity instance {index} merges incompatible visible entities "
                        f"in bounded_slot={bounded_slot}: {sorted(visible_entities)}"
                    )


            expected_member_audit_keys = member_set & eligible_set
            if (
                len(row_keys) != len(set(row_keys))
                or set(row_keys) != expected_member_audit_keys
            ):
                errors.append(
                    f"fixed-capacity instance {index} member_action_audit must exactly cover "
                    "machine-eligible member obs_keys"
                )
            for obs_key in member_set:
                fixed_membership[obs_key] = fixed_membership.get(obs_key, 0) + 1

        out_of_page = candidate.get("out_of_page_obs", []) if isinstance(candidate, dict) else []
        if not isinstance(out_of_page, list):


            out_of_page = []
        missing_or_duplicate = {
            obs_key: assignment_counts.get(obs_key, 0)
            for obs_key in sorted(input_keys)
            if assignment_counts.get(obs_key, 0) != 1
        }
        extra_assignments = sorted(set(assignment_counts) - input_keys)
        if missing_or_duplicate or extra_assignments:
            errors.append(
                "candidate must assign every input observation exactly once: "
                f"bad_counts={missing_or_duplicate}, extra={extra_assignments}"
            )

        for obs_key in sorted(task_set):
            if fixed_membership.get(obs_key, 0) != 1:
                errors.append(
                    f"task-content entry {obs_key} must belong to exactly one fixed-capacity instance"
                )
        for obs_key in sorted(shell_set):
            if fixed_membership.get(obs_key, 0):
                errors.append(f"shell-control entry {obs_key} appears in a fixed-capacity instance")
            dimensions = instance_dimensions_by_obs_key.get(obs_key, [])
            if len(dimensions) != 1 or dimensions[0] == "fixed_capacity_task_content":
                errors.append(
                    f"shell-control entry {obs_key} must belong to exactly one non-fixed instance"
                )
        return errors

    @classmethod
    def _resolve_instance_split_membership(
        cls,
        candidate: Dict[str, Any],
        steps_payload: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, List[str]], List[str]]:
        input_keys = [
            str(step.get("obs_key", "") or "").strip()
            for step in steps_payload
            if isinstance(step, dict) and str(step.get("obs_key", "") or "").strip()
        ]
        input_set = set(input_keys)
        errors: List[str] = []
        if len(input_keys) != len(input_set):
            errors.append("steps_payload contains duplicate obs_keys")
        if not isinstance(candidate, dict):
            return {}, ["instance split response must be a JSON object"]

        split_needed = bool(candidate.get("split_needed", True))
        instances = candidate.get("instances", [])
        out_of_page = candidate.get("out_of_page_obs", [])
        if not isinstance(instances, list):
            errors.append("instances must be a list")
            instances = []
        if not isinstance(out_of_page, list):
            errors.append("out_of_page_obs must be a list")
            out_of_page = []
        errors.extend(
            cls._instance_split_raw_member_duplicate_errors(instances, out_of_page)
        )


        if not split_needed:
            if instances or out_of_page:
                errors.append(
                    "split_needed=false requires empty instances and out_of_page_obs; "
                    "all input observations then bind to cluster_default"
                )
                return {}, errors
            return {obs_key: ["cluster_default"] for obs_key in input_keys}, errors

        membership: Dict[str, List[str]] = {}
        for instance_index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                errors.append(f"instances[{instance_index}] must be an object")
                continue
            owner = f"instances[{instance_index}]"
            for obs_key in cls._instance_split_candidate_instance_obs_keys(instance):
                membership.setdefault(obs_key, []).append(owner)
        for out_index, item in enumerate(out_of_page):
            if not isinstance(item, dict):
                errors.append(f"out_of_page_obs[{out_index}] must be an object")
                continue
            obs_key = str(item.get("obs_key", "") or "").strip()
            if obs_key:
                membership.setdefault(obs_key, []).append(
                    f"out_of_page_obs[{out_index}]"
                )

        missing = sorted(input_set - set(membership))
        unknown = sorted(set(membership) - input_set)
        duplicates = {
            key: owners
            for key, owners in sorted(membership.items())
            if len(owners) != 1
        }
        if missing:
            errors.append(f"missing input obs_keys: {missing}")
        if unknown:
            errors.append(f"unknown obs_keys not present in input: {unknown}")
        if duplicates:
            errors.append(
                "obs_keys assigned more than once: "
                f"{json.dumps(duplicates, ensure_ascii=False, sort_keys=True)}"
            )
        if input_keys and not instances and not out_of_page:
            errors.append("split_needed=true requires instances or out_of_page_obs")
        return membership, errors

    @staticmethod
    def _instance_split_usage_input_tokens(usage: Any) -> int:
        if isinstance(usage, dict):
            return int(
                usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            )
        return int(
            getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0
        )

    @classmethod
    def _enforce_instance_split_token_budget(
        cls,
        usage: Any,
        *,
        stage: str,
        limit: int = 24000,
    ) -> None:
        input_tokens = cls._instance_split_usage_input_tokens(usage)
        if input_tokens > limit:
            raise ValueError(
                f"{stage} input_tokens={input_tokens} exceeds per-call input limit={limit}"
            )

    @staticmethod
    def _estimate_instance_split_prompt_tokens(prompt: str) -> int:
        text = str(prompt or "")
        try:
            import tiktoken
        except Exception as exc:
            raise RuntimeError(
                "Instance Split token preflight requires tiktoken/o200k_base; "
                "refusing to substitute a character-count budget proxy"
            ) from exc
        return max(1, len(tiktoken.get_encoding("o200k_base").encode(text)))

    @classmethod
    def _enforce_instance_split_preflight_budget(
        cls,
        prompt: str,
        *,
        stage: str,
        limit: Optional[int] = None,
    ) -> int:
        hard_limit = int(limit or cls.INSTANCE_SPLIT_INPUT_TOKEN_LIMIT)
        estimate = cls._estimate_instance_split_prompt_tokens(prompt)
        if estimate > hard_limit:
            raise ValueError(
                f"{stage} estimated_input_tokens={estimate} exceeds per-call "
                f"input limit={hard_limit}; lossless producer partitioning is required"
            )
        return estimate

    @staticmethod
    def _instance_split_prompt_with_formal_errors(
        prompt: str,
        formal_validation_errors: Optional[List[str]],
    ) -> str:
        if not formal_validation_errors:
            return prompt
        return (
            prompt
            + "\n\nFORMAL POST-NORMALIZATION VALIDATION ERRORS FROM THE PREVIOUS "
            "PRODUCTION ROUND:\n"
            + json.dumps(formal_validation_errors, ensure_ascii=False, indent=2)
            + "\nReturn a corrected complete candidate. Every input obs_key must remain "
            "assigned exactly once after formal materialization. A "
            "fixed_capacity_task_content group may contain only task_content members, "
            "and one bounded slot may not merge different visible entities. The machine "
            "eligible entry list must be copied exactly once into the action audit; "
            "stable_shell_control covers ordinary filter/tab/action-sheet controls, "
            "while rows with no current_action remain membership-only."
        )

    def _instance_split_lossless_debug_dir(
        self,
        cluster_key: str,
    ) -> Optional[Path]:
        if not self.pom:
            return None
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(cluster_key or "cluster"))
        path = self.pom.debug(
            "instance_splitting",
            "lossless_context",
            safe_key,
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _write_instance_split_debug_json(path: Optional[Path], payload: Any) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_instance_split_debug_text(path: Optional[Path], text: str) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text or ""), encoding="utf-8")

    @staticmethod
    def _bind_instance_split_schema_obs_keys(
        base_schema: Dict[str, Any],
        allowed_obs_keys: List[str],
    ) -> Dict[str, Any]:
        schema = deepcopy(base_schema)
        allowed = [
            str(value or "").strip()
            for value in allowed_obs_keys
            if str(value or "").strip()
        ]
        if not allowed or len(allowed) != len(set(allowed)):
            raise ValueError("instance split schema requires unique allowed obs_keys")
        array_fields = {
            "obs_keys",
            "audited_obs_keys",
            "task_content_obs_keys",
            "stable_shell_control_obs_keys",
        }

        def visit(node: Any) -> None:
            if not isinstance(node, dict):
                return
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    if not isinstance(child, dict):
                        continue
                    if name in array_fields and child.get("type") == "array":
                        child["items"] = {
                            "type": "string",
                            "enum": list(allowed),
                        }
                    elif name == "obs_key" and child.get("type") == "string":
                        child["enum"] = list(allowed)
                    visit(child)
            for keyword in ("anyOf", "oneOf", "allOf"):
                branches = node.get(keyword)
                if isinstance(branches, list):
                    for branch in branches:
                        visit(branch)
            items = node.get("items")
            if isinstance(items, dict):
                visit(items)

        visit(schema)
        return schema

    async def _call_instance_split_lossless_candidate(
        self,
        prompt: str,
        *,
        stage: str,
        debug_dir: Optional[Path],
        allowed_obs_keys: List[str],
        require_explicit_instance: bool,
    ) -> Tuple[Dict[str, Any], Any, int]:
        estimated_tokens = self._enforce_instance_split_preflight_budget(
            prompt,
            stage=stage,
        )
        base_schema = (
            INSTANCE_SPLIT_MERGE_SCHEMA
            if stage.startswith("instance_split_merge_")
            else (
                INSTANCE_SPLIT_PRODUCER_SHARD_SCHEMA
                if require_explicit_instance
                else INSTANCE_SPLIT_SCHEMA
            )
        )
        schema = self._bind_instance_split_schema_obs_keys(
            base_schema,
            allowed_obs_keys,
        )
        prompt_path = debug_dir / "prompt.txt" if debug_dir else None
        output_path = debug_dir / "output.json" if debug_dir else None
        usage_path = debug_dir / "usage.json" if debug_dir else None
        if (
            prompt_path is not None
            and output_path is not None
            and usage_path is not None
            and prompt_path.is_file()
            and output_path.is_file()
            and usage_path.is_file()
            and prompt_path.read_text(encoding="utf-8") == prompt
        ):
            cached_result = json.loads(output_path.read_text(encoding="utf-8"))
            cached_usage_payload = json.loads(usage_path.read_text(encoding="utf-8"))
            cached_usage = cached_usage_payload.get("provider_usage", {})
            cache_errors = _schema_validation_errors(cached_result, schema)
            cache_errors.extend(
                self._validate_instance_split_candidate_coverage(
                    cached_result,
                    [{"obs_key": key} for key in allowed_obs_keys],
                )
            )
            if not cache_errors:
                self._enforce_instance_split_token_budget(cached_usage, stage=stage)
                self._write_instance_split_debug_json(
                    debug_dir / "reuse.json",
                    {
                        "status": "reused",
                        "prompt_exact": True,
                        "schema_validation_errors": [],
                        "membership_coverage_errors": [],
                    },
                )
                return cached_result, cached_usage, estimated_tokens
        self._write_instance_split_debug_text(
            prompt_path,
            prompt,
        )
        result, usage = await call_openai_api_json_async(
            [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            max_tokens=8192,
            reasoning_effort="medium",
            model=self.model,
            stage=stage,
            json_schema=schema,
        )
        self._enforce_instance_split_token_budget(usage, stage=stage)
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError(f"{stage} response must be a JSON object")
        self._write_instance_split_debug_json(
            output_path,
            result,
        )
        self._write_instance_split_debug_json(
            usage_path,
            {
                "estimated_input_tokens": estimated_tokens,
                "provider_usage": usage,
            },
        )
        return result, usage, estimated_tokens

    async def _gather_instance_split_calls_batched(
        self,
        call_factories: List[Any],
    ) -> List[Any]:
        factories = list(call_factories)
        if not factories:
            return []
        limit = max(1, int(self.INSTANCE_SPLIT_CALL_BATCH_SIZE))
        results: List[Any] = [None] * len(factories)
        pending: Dict[asyncio.Task[Any], int] = {}
        next_index = 0

        def schedule(index: int) -> None:
            pending[asyncio.create_task(factories[index]())] = index

        while next_index < min(limit, len(factories)):
            schedule(next_index)
            next_index += 1
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    index = pending.pop(task)
                    results[index] = task.result()
                    if next_index < len(factories):
                        schedule(next_index)
                        next_index += 1
        except Exception:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        return results

    def _partition_instance_split_steps_losslessly(
        self,
        page_context: Dict[str, str],
        steps_payload: List[Dict[str, Any]],
        formal_validation_errors: Optional[List[str]],
    ) -> List[List[Dict[str, Any]]]:
        indexed_rows = list(enumerate(steps_payload))
        indexed_rows.sort(
            key=lambda item: self._estimate_instance_split_prompt_tokens(
                json.dumps(item[1], ensure_ascii=False, sort_keys=True, default=str)
            ),
            reverse=True,
        )
        bins: List[List[Tuple[int, Dict[str, Any]]]] = []
        target = self.INSTANCE_SPLIT_PRODUCER_TARGET_TOKENS

        def shard_prompt(rows: List[Tuple[int, Dict[str, Any]]]) -> str:
            ordered = [row for _, row in sorted(rows, key=lambda item: item[0])]
            prompt = self._build_instance_split_prompt(page_context, ordered)
            prompt = self._instance_split_prompt_with_formal_errors(
                prompt,
                formal_validation_errors,
            )
            return prompt + (
                "\n\nLOSSLESS PRODUCER SHARD CONTRACT:\n"
                "This is one complete-row shard of a larger General Page cluster. "
                "Return split_needed=true and explicitly assign every local obs_key "
                "exactly once, even if all local observations belong to one provisional "
                "instance. Include entry_action_audit for every machine-eligible local "
                "entry row (the prompt's computed list); ordinary filter/tab/action-sheet "
                "controls are stable_shell_control, while no-current-action rows are "
                "membership-only. "
                "Do not infer observations outside this shard."
            )

        for indexed_row in indexed_rows:
            single_prompt = shard_prompt([indexed_row])
            self._enforce_instance_split_preflight_budget(
                single_prompt,
                stage="instance_split_producer_single_row",
            )
            candidates: List[Tuple[int, int]] = []
            for bin_index, current in enumerate(bins):
                estimate = self._estimate_instance_split_prompt_tokens(
                    shard_prompt(current + [indexed_row])
                )
                if estimate <= target:
                    candidates.append((estimate, bin_index))
            if candidates:
                _, bin_index = min(
                    candidates,
                    key=lambda item: (item[0], len(bins[item[1]])),
                )
                bins[bin_index].append(indexed_row)
            else:
                bins.append([indexed_row])
        return [
            [row for _, row in sorted(rows, key=lambda item: item[0])]
            for rows in bins
        ]

    def _build_instance_split_merge_prompt(
        self,
        page_context: Dict[str, str],
        left: Dict[str, Any],
        right: Dict[str, Any],
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> str:
        covered_obs_keys: List[str] = []
        for source in (left, right):
            for obs_key in source.get("covered_obs_keys", []) or []:
                normalized = str(obs_key or "").strip()
                if normalized and normalized not in covered_obs_keys:
                    covered_obs_keys.append(normalized)

        source_candidates = [
            source.get("candidate", {}) if isinstance(source, dict) else {}
            for source in (left, right)
        ]
        source_instances = [
            candidate.get("instances", []) if isinstance(candidate, dict) else []
            for candidate in source_candidates
        ]
        merge_to_one_non_fixed = all(
            isinstance(instances, list)
            and len(instances) == 1
            and isinstance(instances[0], dict)
            and str(instances[0].get("split_dimension", "") or "none").strip()
            != "fixed_capacity_task_content"
            and not (
                candidate.get("out_of_page_obs", [])
                if isinstance(candidate, dict)
                else []
            )
            for candidate, instances in zip(source_candidates, source_instances)
        )
        instance_shape_rule = (
            "Both inputs contain one compatible non-fixed provisional instance. "
            "Return exactly one non-fixed instance when their supplied semantics describe "
            "the same form or page state; its obs_keys must be the ordered union of all "
            "covered keys. Keep separate instances only if the supplied CURRENT visible "
            "state proves a durable Materialized Page boundary."
            if merge_to_one_non_fixed
            else
            "Reconcile the supplied provisional instances without inventing a split. "
            "Keep separate instances only when their supplied CURRENT visible state proves "
            "a durable Materialized Page boundary."
        )
        direct_contract = (
            "\n\nDIRECT OUTPUT CONTRACT:\n"
            "- Return the candidate object directly. Do not return a "
            "covered_obs_keys/candidate wrapper.\n"
            "- This is an intermediate lossless merge. Return split_needed=true even "
            f"when all {len(covered_obs_keys)} observations belong to one provisional instance.\n"
            f"- {instance_shape_rule}\n"
            "- Every covered obs_key must appear exactly once in instances or "
            "out_of_page_obs.\n"
            "- entry_action_audit must be the exact ordered union of the two input "
            "audits, preserving the task_content versus stable_shell_control partition. "
            "stable_shell_control includes ordinary filter/tab/action-sheet controls, "
            "not only persistent app-shell controls.\n"
            "- Do not emit trace_steps or step_indices; they are losslessly derivable "
            "from obs_keys.\n"
            "- Keep reason and split_reason concise. Do not repeat the input prose.\n\n"
            "Return a JSON object with exactly these top-level fields:\n"
            "{\n"
            '  "entry_action_audit": {\n'
            '    "audited_obs_keys": ["exact ordered union"],\n'
            '    "task_content_obs_keys": ["exact ordered union"],\n'
            '    "stable_shell_control_obs_keys": ["exact ordered union"]\n'
            "  },\n"
            '  "instances": [{\n'
            '    "instance_id": "i_001",\n'
            '    "instance_label": "concise visible identity",\n'
            '    "instance_key_hint": "short_ascii_machine_key",\n'
            '    "obs_keys": ["every covered obs_key exactly once"],\n'
            '    "split_dimension": "none or a supplied durable dimension",\n'
            '    "split_reason": {"dimension": "same as split_dimension", '
            '"same_general_page": true},\n'
            '    "reason": "concise evidence-grounded reason"\n'
            "  }],\n"
            '  "out_of_page_obs": [],\n'
            '  "split_needed": true\n'
            "}\n"
        )
        boundary_section = ""
        if isinstance(boundary_preservation_constraint, dict) and boundary_preservation_constraint:
            anchor_keys = {
                str(value or "").strip()
                for group in boundary_preservation_constraint.get(
                    "must_separate_obs_key_groups", []
                ) or []
                if isinstance(group, list)
                for value in group
                if str(value or "").strip()
            }
            if anchor_keys and anchor_keys.issubset(set(covered_obs_keys)):
                boundary_section = (
                    "\n\nBOUND POSITIVE HUMAN-REVIEWED BOUNDARY CONSTRAINT:\n"
                    + json.dumps(
                        boundary_preservation_constraint,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\nThis overrides conflicting transition hints only for the "
                    "explicit anchored obs_key groups. Keep each group together, "
                    "keep different groups in distinct instances, and preserve the "
                    "required split dimensions. Do not change unlisted same-page rules."
                )
        return (
            "You are merging two successful lossless Instance Split producer outputs "
            "for the same General Page. Both complete candidates are semantic authority. "
            "Preserve all candidate fields, visible identities, split reasons, out-of-page "
            "evidence, and entry-action classifications. Reconcile provisional groups only "
            "when their supplied semantics prove they are the same Materialized Page instance. "
            "Return one complete candidate assigning every key in covered_obs_keys exactly "
            "once. Do not invent keys and do not drop prose or audit rows. split_needed=false "
            "is allowed only when every covered observation is one in-page instance.\n\n"
            "Page identity:\n"
            + json.dumps(page_context, ensure_ascii=False, indent=2, default=str)
            + "\n\nLeft producer:\n"
            + json.dumps(left, ensure_ascii=False, indent=2, default=str)
            + "\n\nRight producer:\n"
            + json.dumps(right, ensure_ascii=False, indent=2, default=str)
            + boundary_section
            + direct_contract
        )

    @classmethod
    def _validate_instance_split_merge_candidate(
        cls,
        candidate: Dict[str, Any],
        steps_payload: List[Dict[str, Any]],
    ) -> List[str]:
        errors: List[str] = []
        required_top_level = {
            "instances",
            "out_of_page_obs",
            "split_needed",
        }
        eligible_keys = cls._instance_split_entry_action_obs_keys(steps_payload)
        if eligible_keys:
            required_top_level.add("entry_action_audit")
        actual_top_level = set(candidate) if isinstance(candidate, dict) else set()
        allowed_top_levels = [required_top_level]
        if not eligible_keys:


            allowed_top_levels.append(required_top_level | {"entry_action_audit"})
        if actual_top_level not in allowed_top_levels:
            expected = " or ".join(
                str(sorted(fields)) for fields in allowed_top_levels
            )
            errors.append(
                "merge candidate top-level fields must be exactly "
                f"{expected}: missing={sorted(required_top_level - actual_top_level)}, "
                f"extra={sorted(actual_top_level - set().union(*allowed_top_levels))}"
            )
        if not bool(candidate.get("split_needed", False)):
            errors.append("intermediate merge candidate must keep split_needed=true")
        errors.extend(cls._validate_instance_split_candidate_coverage(candidate, steps_payload))
        if eligible_keys or "entry_action_audit" in actual_top_level:
            errors.extend(cls._validate_instance_split_entry_action_audit(candidate, steps_payload))
        return errors

    @classmethod
    def _canonicalize_instance_split_merge_candidate(
        cls,
        candidate: Dict[str, Any],
        left: Dict[str, Any],
        right: Dict[str, Any],
        steps_payload: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:

        sources = [left, right]
        covered: List[str] = []
        source_instances: List[Dict[str, Any]] = []
        source_out_of_page: Dict[str, Dict[str, Any]] = {}
        source_audits: List[Dict[str, Any]] = []
        errors: List[str] = []

        for source_index, source in enumerate(sources):
            source_keys = [
                str(value or "").strip()
                for value in (source.get("covered_obs_keys", []) or [])
                if str(value or "").strip()
            ]
            for obs_key in source_keys:
                if obs_key in covered:
                    errors.append(
                        f"merge source covered_obs_keys overlap at {obs_key}"
                    )
                else:
                    covered.append(obs_key)
            source_candidate = source.get("candidate", {})
            if not isinstance(source_candidate, dict):
                errors.append(f"merge source {source_index} candidate must be an object")
                continue
            _membership, source_errors = cls._resolve_instance_split_membership(
                source_candidate,
                [{"obs_key": key} for key in source_keys],
            )
            errors.extend(
                f"merge source {source_index}: {error}" for error in source_errors
            )
            for instance_index, instance in enumerate(
                source_candidate.get("instances", []) or []
            ):
                if not isinstance(instance, dict):
                    errors.append(
                        f"merge source {source_index} instances[{instance_index}] must be an object"
                    )
                    continue
                source_instances.append(
                    {
                        "source_index": source_index,
                        "instance_index": instance_index,
                        "instance": instance,
                        "obs_keys": cls._instance_split_candidate_instance_obs_keys(
                            instance
                        ),
                    }
                )
            for item in source_candidate.get("out_of_page_obs", []) or []:
                if not isinstance(item, dict):
                    continue
                obs_key = str(item.get("obs_key", "") or "").strip()
                if obs_key:
                    source_out_of_page[obs_key] = deepcopy(item)
            audit = source_candidate.get("entry_action_audit")
            if isinstance(audit, dict):
                source_audits.append(audit)

        if errors:
            return {}, {"status": "invalid_sources"}, errors
        if not isinstance(candidate, dict):
            return {}, {"status": "invalid_candidate"}, [
                "merge candidate must be an object"
            ]

        allowed_instance_fields = {
            "instance_id",
            "instance_label",
            "instance_key_hint",
            "obs_keys",
            "split_dimension",
            "split_reason",
            "reason",
        }
        output_instances: List[Dict[str, Any]] = []
        removed_legacy_fields: List[Dict[str, Any]] = []
        for output_index, instance in enumerate(candidate.get("instances", []) or []):
            if not isinstance(instance, dict):
                errors.append(f"merge output instances[{output_index}] must be an object")
                continue
            removed = sorted(set(instance) - allowed_instance_fields)
            if removed:
                removed_legacy_fields.append(
                    {"output_instance_index": output_index, "fields": removed}
                )
            output_instances.append(
                {
                    key: deepcopy(value)
                    for key, value in instance.items()
                    if key in allowed_instance_fields
                }
            )


        for source_row in source_instances:
            source_instance = source_row["instance"]
            source_dimension = normalized_dimension = " ".join(
                str(source_instance.get("split_dimension", "") or "none")
                .strip()
                .lower()
                .split()
            )
            if normalized_dimension in {"", "none"}:
                continue
            source_key = " ".join(
                str(source_instance.get("instance_key_hint", "") or "")
                .strip()
                .lower()
                .split()
            )
            matching = [
                output
                for output in output_instances
                if " ".join(
                    str(output.get("split_dimension", "") or "none")
                    .strip()
                    .lower()
                    .split()
                )
                == source_dimension
                and (
                    not source_key
                    or " ".join(
                        str(output.get("instance_key_hint", "") or "")
                        .strip()
                        .lower()
                        .split()
                    )
                    == source_key
                )
            ]
            if not matching:
                output_instances.append(
                    {
                        key: deepcopy(value)
                        for key, value in source_instance.items()
                        if key in allowed_instance_fields
                    }
                )
        if source_instances and not output_instances:
            errors.append("merge output has no instances for in-page source membership")
        if errors:
            return {}, {"status": "invalid_candidate"}, errors

        def normalized(value: Any) -> str:
            return " ".join(str(value or "").strip().lower().split())

        raw_membership: Dict[str, List[int]] = {}
        for output_index, instance in enumerate(output_instances):
            for obs_key in cls._instance_split_candidate_instance_obs_keys(instance):
                raw_membership.setdefault(obs_key, []).append(output_index)

        source_to_output: List[Dict[str, Any]] = []
        assigned_by_output: Dict[int, List[str]] = {
            index: [] for index in range(len(output_instances))
        }
        for source_row in source_instances:
            source_instance = source_row["instance"]
            source_keys = list(source_row["obs_keys"])
            source_dimension = normalized(source_instance.get("split_dimension")) or "none"
            exact_durable_targets = [
                output_index
                for output_index, output_instance in enumerate(output_instances)
                if source_dimension != "none"
                and normalized(output_instance.get("split_dimension"))
                == source_dimension
                and normalized(source_instance.get("instance_key_hint"))
                and normalized(output_instance.get("instance_key_hint"))
                == normalized(source_instance.get("instance_key_hint"))
            ]
            ranked: List[Tuple[int, int]] = []
            for output_index, output_instance in enumerate(output_instances):
                overlap = len(
                    set(source_keys)
                    & set(
                        cls._instance_split_candidate_instance_obs_keys(
                            output_instance
                        )
                    )
                )
                score = overlap * 100
                if normalized(source_instance.get("instance_key_hint")) and normalized(
                    source_instance.get("instance_key_hint")
                ) == normalized(output_instance.get("instance_key_hint")):
                    score += 30
                if normalized(source_instance.get("instance_label")) and normalized(
                    source_instance.get("instance_label")
                ) == normalized(output_instance.get("instance_label")):
                    score += 20
                if normalized(source_instance.get("split_dimension")) == normalized(
                    output_instance.get("split_dimension")
                ):
                    score += 10
                ranked.append((score, output_index))
            if len(exact_durable_targets) == 1:
                target_index = exact_durable_targets[0]
                best_score = next(
                    score for score, index in ranked if index == target_index
                )
            elif len(output_instances) == 1:
                target_index = 0
                best_score = ranked[0][0]
            else:
                best_score = max((score for score, _ in ranked), default=-1)
                best = [index for score, index in ranked if score == best_score]
                if best_score <= 0 or len(best) != 1:
                    errors.append(
                        "source instance has no unique semantic output mapping: "
                        f"source={source_row['source_index']}:{source_row['instance_index']}, "
                        f"scores={ranked}"
                    )
                    continue
                target_index = best[0]
            assigned_by_output[target_index].extend(source_keys)
            source_to_output.append(
                {
                    "source_index": source_row["source_index"],
                    "source_instance_index": source_row["instance_index"],
                    "output_instance_index": target_index,
                    "score": best_score,
                    "obs_keys": source_keys,
                }
            )

        if errors:
            return {}, {"status": "ambiguous_mapping", "mappings": source_to_output}, errors
        for output_index, assigned in assigned_by_output.items():
            if not assigned:
                errors.append(
                    f"merge output instances[{output_index}] has no authoritative source mapping"
                )
        if errors:
            return {}, {"status": "invented_output_instance", "mappings": source_to_output}, errors

        covered_index = {key: index for index, key in enumerate(covered)}
        for output_index, output_instance in enumerate(output_instances):
            unique_keys = sorted(
                set(assigned_by_output[output_index]),
                key=lambda key: covered_index[key],
            )
            output_instance["obs_keys"] = unique_keys

        member_audit_by_output: Dict[int, List[Dict[str, Any]]] = {}
        for mapping, source_row in zip(source_to_output, source_instances):
            split_reason = source_row["instance"].get("split_reason", {})
            rows = (
                split_reason.get("member_action_audit", [])
                if isinstance(split_reason, dict)
                else []
            )
            if isinstance(rows, list):
                member_audit_by_output.setdefault(
                    mapping["output_instance_index"], []
                ).extend(deepcopy(row) for row in rows if isinstance(row, dict))
        for output_index, rows in member_audit_by_output.items():
            if not rows:
                continue
            output_instance = output_instances[output_index]
            split_reason = output_instance.get("split_reason", {})
            if not isinstance(split_reason, dict):
                split_reason = {}
            by_key = {
                str(row.get("obs_key", "") or "").strip(): row
                for row in rows
                if str(row.get("obs_key", "") or "").strip()
            }
            split_reason["member_action_audit"] = [
                by_key[key]
                for key in output_instance.get("obs_keys", [])
                if key in by_key
            ]
            output_instance["split_reason"] = split_reason

        audit_fields = (
            "audited_obs_keys",
            "task_content_obs_keys",
            "stable_shell_control_obs_keys",
        )
        canonical_audit: Dict[str, List[str]] = {}
        if steps_payload is not None:
            eligible = cls._instance_split_entry_action_obs_keys(steps_payload)
            dimension_by_obs_key = {
                obs_key: normalized(source_row["instance"].get("split_dimension"))
                or "none"
                for source_row in source_instances
                for obs_key in source_row["obs_keys"]
            }
            task_content = [
                key
                for key in eligible
                if dimension_by_obs_key.get(key) == "fixed_capacity_task_content"
            ]
            stable_shell = [key for key in eligible if key not in set(task_content)]
            canonical_audit = {
                "audited_obs_keys": eligible,
                "task_content_obs_keys": task_content,
                "stable_shell_control_obs_keys": stable_shell,
            }
        else:
            for field in audit_fields:
                source_values = {
                    str(value or "").strip()
                    for audit in source_audits
                    for value in (audit.get(field, []) or [])
                    if str(value or "").strip()
                }
                canonical_audit[field] = [
                    key for key in covered if key in source_values
                ]
        if set(canonical_audit["task_content_obs_keys"]) & set(
            canonical_audit["stable_shell_control_obs_keys"]
        ):
            errors.append("source entry-action audit role partitions overlap")
        if set(canonical_audit["audited_obs_keys"]) != (
            set(canonical_audit["task_content_obs_keys"])
            | set(canonical_audit["stable_shell_control_obs_keys"])
        ):
            errors.append("source entry-action audit union is incomplete")
        if errors:
            return {}, {"status": "invalid_source_audit"}, errors

        canonical: Dict[str, Any] = {
            "instances": output_instances,
            "out_of_page_obs": [
                source_out_of_page[key]
                for key in covered
                if key in source_out_of_page
            ],
            "split_needed": True,
        }
        if canonical_audit["audited_obs_keys"] or isinstance(
            candidate.get("entry_action_audit"), dict
        ):
            canonical = {"entry_action_audit": canonical_audit, **canonical}

        before_counts = {
            key: len(owners) for key, owners in sorted(raw_membership.items())
        }
        after_counts = {
            key: sum(
                key in instance.get("obs_keys", []) for instance in output_instances
            )
            + int(key in source_out_of_page)
            for key in covered
        }
        manifest = {
            "status": "canonicalized",
            "covered_obs_keys": covered,
            "source_to_output_mappings": source_to_output,
            "raw_membership_counts": before_counts,
            "canonical_membership_counts": after_counts,
            "removed_legacy_fields": removed_legacy_fields,
            "entry_action_audit_source": "exact_ordered_union_of_validated_partials",
        }
        return canonical, manifest, []

    @classmethod
    def _reduce_instance_split_partials_canonically(
        cls,
        partials: List[Dict[str, Any]],
        steps_payload: List[Dict[str, Any]],
        runtime_projection_dimensions: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:

        covered: List[str] = []
        input_instances: List[Dict[str, Any]] = []
        out_of_page_by_key: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []
        for partial_index, partial in enumerate(partials):
            keys = [
                str(value or "").strip()
                for value in (partial.get("covered_obs_keys", []) or [])
                if str(value or "").strip()
            ]
            overlap = sorted(set(covered) & set(keys))
            if overlap:
                errors.append(
                    f"canonical reducer partial {partial_index} overlaps prior keys: {overlap}"
                )
            covered.extend(key for key in keys if key not in covered)
            candidate = partial.get("candidate", {})
            if not isinstance(candidate, dict):
                errors.append(
                    f"canonical reducer partial {partial_index} candidate must be an object"
                )
                continue
            _membership, membership_errors = cls._resolve_instance_split_membership(
                candidate,
                [{"obs_key": key} for key in keys],
            )
            errors.extend(
                f"canonical reducer partial {partial_index}: {error}"
                for error in membership_errors
            )
            input_instances.extend(
                deepcopy(instance)
                for instance in (candidate.get("instances", []) or [])
                if isinstance(instance, dict)
            )
            for row in candidate.get("out_of_page_obs", []) or []:
                if not isinstance(row, dict):
                    continue
                obs_key = str(row.get("obs_key", "") or "").strip()
                if obs_key:
                    out_of_page_by_key[obs_key] = deepcopy(row)
        if errors:
            return {}, {"status": "invalid_partials"}, errors

        def normalized(value: Any) -> str:
            return " ".join(str(value or "").strip().lower().split())

        opened_projection_keys = {
            str(step.get("obs_key", "") or "").strip()
            for step in steps_payload
            if isinstance(step, dict)
            and isinstance(step.get("semantic_identity_evidence"), dict)
            and normalized(
                step["semantic_identity_evidence"].get(
                    "interaction_surface_role", ""
                )
            )
            in {
                "opened_overlay",
                "opened_drawer",
                "opened_action_sheet",
                "opened_modal",
            }
            and bool(
                step["semantic_identity_evidence"].get(
                    "host_page_still_visible", False
                )
            )
        }
        parameterizable_dimensions = {
            "semantic_mode",
            "state_variant",
            "lifecycle_variant",
        }
        parameterizable_dimensions.update(
            normalized(value)
            for value in (runtime_projection_dimensions or [])
            if normalized(value)
        )

        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        group_order: List[Tuple[Any, ...]] = []
        source_group_rows: List[Dict[str, Any]] = []
        for source_index, instance in enumerate(input_instances):
            dimension = normalized(instance.get("split_dimension")) or "none"
            obs_keys = cls._instance_split_candidate_instance_obs_keys(instance)
            if dimension == "fixed_capacity_task_content":
                group_key: Tuple[Any, ...] = (
                    "fixed_capacity_task_content",
                    tuple(obs_keys),
                )
            elif (
                dimension == "none"
                or dimension in parameterizable_dimensions
                or (obs_keys and set(obs_keys).issubset(opened_projection_keys))
            ):
                group_key = ("default_non_fixed",)
            else:
                identity = normalized(instance.get("instance_key_hint"))
                if not identity:
                    errors.append(
                        "durable canonical reducer instance requires instance_key_hint: "
                        f"source_index={source_index}, dimension={dimension}"
                    )
                    continue
                group_key = ("durable", dimension, identity)
            if group_key not in grouped:
                compact = {
                    key: deepcopy(value)
                    for key, value in instance.items()
                    if key
                    in {
                        "instance_id",
                        "instance_label",
                        "instance_key_hint",
                        "obs_keys",
                        "split_dimension",
                        "split_reason",
                        "reason",
                    }
                }
                compact["obs_keys"] = []
                grouped[group_key] = compact
                group_order.append(group_key)
            target = grouped[group_key]
            for obs_key in obs_keys:
                if obs_key not in target["obs_keys"]:
                    target["obs_keys"].append(obs_key)
            source_group_rows.append(
                {
                    "source_instance_index": source_index,
                    "group_key": list(group_key),
                    "obs_keys": obs_keys,
                }
            )

        if errors:
            return {}, {"status": "invalid_durable_identity"}, errors
        covered_index = {key: index for index, key in enumerate(covered)}
        instances: List[Dict[str, Any]] = []
        for group_key in group_order:
            instance = grouped[group_key]
            instance["obs_keys"] = sorted(
                set(instance.get("obs_keys", [])),
                key=lambda key: covered_index[key],
            )
            instance["instance_id"] = f"i_{len(instances) + 1:03d}"
            instances.append(instance)

        dimension_by_obs_key = {
            obs_key: normalized(instance.get("split_dimension")) or "none"
            for instance in instances
            for obs_key in instance.get("obs_keys", [])
        }
        eligible = cls._instance_split_entry_action_obs_keys(steps_payload)
        task_content = [
            key
            for key in eligible
            if dimension_by_obs_key.get(key) == "fixed_capacity_task_content"
        ]
        task_set = set(task_content)
        audit = {
            "audited_obs_keys": eligible,
            "task_content_obs_keys": task_content,
            "stable_shell_control_obs_keys": [
                key for key in eligible if key not in task_set
            ],
        }
        candidate: Dict[str, Any] = {
            "entry_action_audit": audit,
            "instances": instances,
            "out_of_page_obs": [
                out_of_page_by_key[key]
                for key in covered
                if key in out_of_page_by_key
            ],
            "split_needed": True,
        }
        validation_errors = cls._validate_instance_split_merge_candidate(
            candidate,
            steps_payload,
        )
        manifest = {
            "status": "canonicalized" if not validation_errors else "invalid",
            "covered_obs_keys": covered,
            "source_instance_count": len(input_instances),
            "output_instance_count": len(instances),
            "source_group_mappings": source_group_rows,
            "grouping_contract": (
                "none_to_default; fixed_capacity_distinct; "
                "durable_exact_dimension_plus_instance_key_hint"
            ),
            "runtime_projection_dimensions": sorted(parameterizable_dimensions),
        }
        return candidate, manifest, validation_errors

    async def _run_instance_split_lossless_producer(
        self,
        cluster_key: str,
        page_context: Dict[str, str],
        steps_payload: List[Dict[str, Any]],
        formal_validation_errors: Optional[List[str]],
        *,
        producer_attempt: int = 1,
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        debug_root = self._instance_split_lossless_debug_dir(cluster_key)
        if debug_root is not None and int(producer_attempt) > 1:


            debug_root = (
                debug_root
                / "contract_retry"
                / f"attempt_{int(producer_attempt):04d}"
            )
        full_prompt = self._instance_split_prompt_with_formal_errors(
            self._build_instance_split_prompt(
                page_context,
                steps_payload,
                boundary_preservation_constraint=boundary_preservation_constraint,
            ),
            formal_validation_errors,
        )
        full_estimate = self._estimate_instance_split_prompt_tokens(full_prompt)
        self._write_instance_split_debug_json(
            debug_root / "input_rows.json" if debug_root else None,
            steps_payload,
        )

        if full_estimate <= self.INSTANCE_SPLIT_INPUT_TOKEN_LIMIT:
            result, usage, estimate = await self._call_instance_split_lossless_candidate(
                full_prompt,
                stage="instance_split",
                debug_dir=debug_root / "single" if debug_root else None,
                allowed_obs_keys=[
                    str(row.get("obs_key", "") or "") for row in steps_payload
                ],
                require_explicit_instance=False,
            )
            self._write_instance_split_debug_json(
                debug_root / "coverage_manifest.json" if debug_root else None,
                {
                    "mode": "single_lossless_call",
                    "input_obs_keys": [row.get("obs_key") for row in steps_payload],
                    "estimated_input_tokens": estimate,
                    "provider_usage": usage,
                    "contract_validation_state": "deferred_to_complete_producer_retry",
                },
            )
            return result

        shards = self._partition_instance_split_steps_losslessly(
            page_context,
            steps_payload,
            formal_validation_errors,
        )
        shard_key_counts: Dict[str, int] = {}
        shard_rows: List[Dict[str, Any]] = []
        prompts: List[str] = []
        for shard_index, shard in enumerate(shards, start=1):
            keys = [str(row.get("obs_key") or "") for row in shard]
            for key in keys:
                shard_key_counts[key] = shard_key_counts.get(key, 0) + 1
            prompt = self._instance_split_prompt_with_formal_errors(
                self._build_instance_split_prompt(page_context, shard),
                formal_validation_errors,
            ) + (
                "\n\nLOSSLESS PRODUCER SHARD CONTRACT:\n"
                "This is one complete-row shard of a larger General Page cluster. "
                "Return split_needed=true and explicitly assign every local obs_key "
                "exactly once, even if all local observations belong to one provisional "
                "instance. Include entry_action_audit for every machine-eligible local "
                "entry row from the prompt's computed list. Ordinary filter/tab/action-sheet "
                "controls use stable_shell_control; no-current-action rows are membership-only. "
                "Do not infer observations outside this shard."
            )
            prompts.append(prompt)
            shard_rows.append({
                "shard_index": shard_index,
                "obs_keys": keys,
                "row_hashes": {
                    key: hashlib.sha256(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()
                    for key, row in zip(keys, shard)
                },
                "estimated_input_tokens": self._enforce_instance_split_preflight_budget(
                    prompt,
                    stage=f"instance_split_producer_{shard_index}",
                ),
            })
            self._write_instance_split_debug_json(
                debug_root / "producer" / f"shard_{shard_index:04d}" / "input.json"
                if debug_root else None,
                shard,
            )

        expected_keys = [str(row.get("obs_key") or "") for row in steps_payload]
        bad_partition = {
            key: shard_key_counts.get(key, 0)
            for key in expected_keys
            if shard_key_counts.get(key, 0) != 1
        }
        unknown_partition_keys = sorted(set(shard_key_counts) - set(expected_keys))
        if bad_partition or unknown_partition_keys:
            raise ValueError(
                "lossless instance split producer partition is incomplete: "
                f"bad_counts={bad_partition}, unknown={unknown_partition_keys}"
            )

        call_factories = []
        for index, prompt in enumerate(prompts, start=1):
            stage = f"instance_split_producer_{index}"
            call_debug_dir = (
                debug_root / "producer" / f"shard_{index:04d}"
                if debug_root else None
            )
            allowed_obs_keys = [
                str(row.get("obs_key", "") or "") for row in shards[index - 1]
            ]
            call_factories.append(
                lambda prompt=prompt, stage=stage, call_debug_dir=call_debug_dir,
                allowed_obs_keys=allowed_obs_keys: self._call_instance_split_lossless_candidate(
                    prompt,
                    stage=stage,
                    debug_dir=call_debug_dir,
                    allowed_obs_keys=allowed_obs_keys,
                    require_explicit_instance=True,
                )
            )
        raw_results = await self._gather_instance_split_calls_batched(call_factories)
        partials: List[Dict[str, Any]] = []
        for index, ((candidate, usage, estimate), shard) in enumerate(
            zip(raw_results, shards),
            start=1,
        ):
            if not bool(candidate.get("split_needed", True)):
                raise InstanceSplitProducerContractError([
                    f"instance_split_producer_{index} violated explicit local membership contract"
                ])
            errors = self._validate_instance_split_candidate_coverage(candidate, shard)
            if errors:
                raise InstanceSplitProducerContractError(
                    [f"instance_split_producer_{index}: {error}" for error in errors]
                )
            partials.append({
                "covered_obs_keys": [row.get("obs_key") for row in shard],
                "candidate": candidate,
            })
            shard_rows[index - 1]["provider_usage"] = usage
            shard_rows[index - 1]["estimated_input_tokens"] = estimate

        merge_round = 0
        while len(partials) > 1:
            merge_round += 1
            next_partials: List[Optional[Dict[str, Any]]] = [None] * ((len(partials) + 1) // 2)
            merge_call_factories = []
            merge_slots: List[
                Tuple[
                    int,
                    List[str],
                    str,
                    Optional[Path],
                    Dict[str, Any],
                    Dict[str, Any],
                ]
            ] = []
            for pair_index in range(0, len(partials), 2):
                output_index = pair_index // 2
                if pair_index + 1 >= len(partials):
                    next_partials[output_index] = partials[pair_index]
                    continue
                left = partials[pair_index]
                right = partials[pair_index + 1]
                covered = list(left["covered_obs_keys"]) + list(right["covered_obs_keys"])
                prompt = self._build_instance_split_merge_prompt(
                    page_context,
                    left,
                    right,
                    boundary_preservation_constraint=(
                        boundary_preservation_constraint
                    ),
                )
                merge_dir = (
                    debug_root / "merge" / f"round_{merge_round:04d}" / f"batch_{output_index + 1:04d}"
                    if debug_root else None
                )
                merge_stage = f"instance_split_merge_r{merge_round}_b{output_index + 1}"
                merge_call_factories.append(
                    lambda prompt=prompt, merge_stage=merge_stage,
                    merge_dir=merge_dir, covered=covered: self._call_instance_split_lossless_candidate(
                        prompt,
                        stage=merge_stage,
                        debug_dir=merge_dir,
                        allowed_obs_keys=covered,
                        require_explicit_instance=True,
                    )
                )
                merge_slots.append(
                    (output_index, covered, prompt, merge_dir, left, right)
                )
            merged_results = await self._gather_instance_split_calls_batched(
                merge_call_factories
            )
            for (
                output_index,
                covered,
                _prompt,
                merge_dir,
                left,
                right,
            ), (candidate, usage, estimate) in zip(
                merge_slots,
                merged_results,
            ):
                covered_steps = [
                    row for row in steps_payload if row.get("obs_key") in set(covered)
                ]
                candidate, canonicalization_manifest, errors = (
                    self._canonicalize_instance_split_merge_candidate(
                        candidate,
                        left,
                        right,
                        covered_steps,
                    )
                )
                errors.extend(self._validate_instance_split_merge_candidate(
                    candidate,
                    covered_steps,
                ))
                if errors:
                    raise InstanceSplitProducerContractError(
                        [
                            f"instance split merge round {merge_round} "
                            f"batch {output_index + 1}: {error}"
                            for error in errors
                        ]
                    )
                next_partials[output_index] = {
                    "covered_obs_keys": covered,
                    "candidate": candidate,
                }
                self._write_instance_split_debug_json(
                    merge_dir / "canonical_output.json" if merge_dir else None,
                    candidate,
                )
                self._write_instance_split_debug_json(
                    merge_dir / "canonicalization_manifest.json"
                    if merge_dir
                    else None,
                    canonicalization_manifest,
                )
                self._write_instance_split_debug_json(
                    merge_dir / "merge_manifest.json" if merge_dir else None,
                    {
                        "covered_obs_keys": covered,
                        "estimated_input_tokens": estimate,
                        "provider_usage": usage,
                        "coverage_errors": [],
                    },
                )
            partials = [item for item in next_partials if item is not None]


            if len(partials) > 1:
                reduced_candidate, reducer_manifest, reducer_errors = (
                    self._reduce_instance_split_partials_canonically(
                        partials,
                        steps_payload,
                    )
                )
                if reducer_errors:
                    raise InstanceSplitProducerContractError(
                        [
                            f"instance split canonical reducer: {error}"
                            for error in reducer_errors
                        ]
                    )
                reducer_dir = (
                    debug_root / "merge" / "canonical_reducer"
                    if debug_root
                    else None
                )
                self._write_instance_split_debug_json(
                    reducer_dir / "output.json" if reducer_dir else None,
                    reduced_candidate,
                )
                self._write_instance_split_debug_json(
                    reducer_dir / "manifest.json" if reducer_dir else None,
                    reducer_manifest,
                )
                partials = [
                    {
                        "covered_obs_keys": list(expected_keys),
                        "candidate": reduced_candidate,
                    }
                ]

        final_candidate = partials[0]["candidate"]
        final_errors = self._validate_instance_split_merge_candidate(
            final_candidate,
            steps_payload,
        )
        if final_errors:
            raise InstanceSplitProducerContractError(
                [f"lossless instance split final merge: {error}" for error in final_errors]
            )
        self._write_instance_split_debug_json(
            debug_root / "coverage_manifest.json" if debug_root else None,
            {
                "mode": "lossless_producer_merge",
                "input_obs_keys": expected_keys,
                "partition_counts": shard_key_counts,
                "shards": shard_rows,
                "merge_rounds": merge_round,
                "coverage_errors": [],
            },
        )
        return final_candidate

    @classmethod
    def _validate_instance_split_candidate_coverage(
        cls,
        candidate: Dict[str, Any],
        steps_payload: List[Dict[str, Any]],
    ) -> List[str]:
        _membership, errors = cls._resolve_instance_split_membership(
            candidate,
            steps_payload,
        )
        return errors

    async def _audit_instance_split_coverage(
        self,
        page_context: Dict[str, str],
        steps_payload: List[Dict[str, Any]],
        candidate: Dict[str, Any],
        max_rounds: int = 0,
    ) -> Dict[str, Any]:
        errors = self._validate_instance_split_candidate_coverage(
            candidate,
            steps_payload,
        )
        if errors:
            raise ValueError(
                "instance split coverage is incomplete; rerun the complete lossless "
                f"producer instead of repairing a reduced prompt: {errors}"
            )
        return candidate

    async def _audit_instance_split_entry_actions(
        self,
        page_context: Dict[str, str],
        steps_payload: List[Dict[str, Any]],
        candidate: Dict[str, Any],
        max_rounds: int = 0,
    ) -> Dict[str, Any]:
        errors = self._validate_instance_split_entry_action_audit(
            candidate,
            steps_payload,
        )
        if errors:
            raise ValueError(
                "instance split entry-action audit is incomplete; rerun the complete "
                f"lossless producer instead of repairing a reduced prompt: {errors}"
            )
        return candidate

    async def _run_instance_split_llm(
        self,
        cluster_key: str,
        cluster_obs: List[FrameObservation],
        page_context: Dict[str, str],
        traces: List[TraceData],
        lifecycle_context: Optional[Dict[str, Any]] = None,
        state_authority_by_obs_key: Optional[
            Dict[str, List[Dict[str, Any]]]
        ] = None,
        formal_validation_errors: Optional[List[str]] = None,
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
        accepted_only: bool = False,
    ) -> List[Dict[str, Any]]:
        if not cluster_obs:
            return []


        trace_map = {trace.trace_id: trace for trace in traces}


        sorted_obs = sorted(cluster_obs, key=lambda o: (o.trace_id, int(o.step_index)))
        lifecycle_steps_by_obs_key = {
            str(step.get("obs_key", "") or ""): step
            for step in (lifecycle_context or {}).get("steps", [])
            if isinstance(step, dict)
        }
        steps_payload: List[Dict[str, Any]] = []
        for obs in sorted_obs:
            transition_kind = str(
                obs.transition_kind or (obs.page_state.get("transition_kind", "") if isinstance(obs.page_state, dict) else "")
            ).strip()
            transition_from_previous = (
                obs.transition_from_previous
                if obs.transition_from_previous is not None
                else (obs.page_state.get("transition_from_previous") if isinstance(obs.page_state, dict) else None)
            )
            trace = trace_map.get(obs.trace_id)
            task_instruction = str(trace.task or "") if trace else ""
            previous_action = ""
            if trace:
                previous_steps = [
                    step
                    for step in trace.steps
                    if int(step.step_index) < int(obs.step_index)
                ]
                if previous_steps:
                    previous_action = sorted(previous_steps, key=lambda step: int(step.step_index))[-1].action
            obs_key = self._obs_key(obs)
            lifecycle_step = lifecycle_steps_by_obs_key.get(obs_key, {})
            semantic_identity_evidence = {
                "lifecycle_phase": str(lifecycle_step.get("lifecycle_phase", "") or ""),
                "reason": str(lifecycle_step.get("reason", "") or ""),
                "exclude_from_initial_value_hints": self._list_of_strings(
                    lifecycle_step.get("exclude_from_initial_value_hints", [])
                ),
                "state_authority": list(
                    (state_authority_by_obs_key or {}).get(obs_key, [])
                ),
                "interaction_surface_role": str(
                    (
                        obs.page_state.get("interaction_surface_role", "")
                        if isinstance(obs.page_state, dict)
                        else ""
                    )
                    or ""
                ),
                "host_page_still_visible": bool(
                    isinstance(obs.page_state, dict)
                    and obs.page_state.get("host_page_still_visible", False)
                ),
            }
            steps_payload.append({
                "obs_key": obs_key,
                "step_index": int(obs.step_index),
                "trace_id": obs.trace_id,
                "task_instruction": task_instruction,
                "description": str(obs.description or ""),
                "transition_kind": transition_kind or "unknown",
                "transition_from_previous": transition_from_previous,
                "action_context": {
                    "previous_action": str(previous_action or ""),
                    "current_action": str(obs.action or ""),
                },
                "semantic_identity_evidence": {
                    key: value
                    for key, value in semantic_identity_evidence.items()
                    if value not in ("", [], {})
                },
            })

        try:
            producer_errors = list(formal_validation_errors or [])
            producer_retry_receipt: List[Dict[str, Any]] = []
            accepted_reuse_candidate = (
                self._load_reusable_instance_split_accepted_candidate(
                    cluster_key,
                    steps_payload,
                    boundary_preservation_constraint,
                )
            )
            if accepted_only and not isinstance(accepted_reuse_candidate, dict):
                raise InstanceSplitProducerContractError(
                    [
                        "accepted-only instance split requires a current exact-input "
                        "accepted candidate; provider fallback is forbidden"
                    ]
                )
            for producer_attempt in range(
                1,
                self.INSTANCE_SPLIT_MAX_PRODUCER_ATTEMPTS + 1,
            ):
                if accepted_only and producer_attempt > 1:
                    raise ValueError(
                        "accepted-only instance split rejected the preserved candidate; "
                        "provider retry is forbidden"
                    )
                reused_accepted_candidate = (
                    producer_attempt == 1
                    and isinstance(accepted_reuse_candidate, dict)
                )
                try:
                    if reused_accepted_candidate:
                        result = deepcopy(accepted_reuse_candidate)
                    else:
                        result = await self._run_instance_split_lossless_producer(
                            cluster_key,
                            page_context,
                            steps_payload,
                            producer_errors,
                            producer_attempt=producer_attempt,
                            boundary_preservation_constraint=(
                                boundary_preservation_constraint
                            ),
                        )
                    raw_contract_errors = (
                        self._validate_instance_split_candidate_coverage(
                            result,
                            steps_payload,
                        )
                        + self._validate_instance_split_entry_action_audit(
                            result,
                            steps_payload,
                        )
                        + self._validate_instance_split_opened_overlay_group_contract(
                            result,
                            sorted_obs,
                        )
                        + self._validate_instance_split_boundary_preservation_constraint(
                            result,
                            boundary_preservation_constraint,
                        )
                    )
                except InstanceSplitProducerContractError as exc:
                    raw_contract_errors = exc.errors

                contract_errors: List[str] = []
                for error in raw_contract_errors:
                    if error not in contract_errors:
                        contract_errors.append(error)
                if not contract_errors:
                    if self.pom:
                        accepted_dir = (
                            self._instance_split_lossless_debug_dir(cluster_key)
                            / "accepted"
                        )
                        if reused_accepted_candidate:
                            self._write_instance_split_debug_json(
                                accepted_dir / "reuse_validation.json",
                                {
                                    "cluster_key": cluster_key,
                                    "producer_contract_version": (
                                        self.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                                    ),
                                    "input_rows_hash": self._stable_json_fingerprint(
                                        steps_payload
                                    ),
                                    "boundary_preservation_constraint_hash": (
                                        self._stable_json_fingerprint(
                                            boundary_preservation_constraint or {}
                                        )
                                    ),
                                    "accepted_output_hash": (
                                        self._stable_json_fingerprint(result)
                                    ),
                                    "validation_state": "raw_contracts_passed",
                                },
                            )
                        else:
                            self._write_instance_split_debug_json(
                                accepted_dir / "output.json",
                                result,
                            )
                            self._write_instance_split_debug_json(
                                accepted_dir / "manifest.json",
                                {
                                    "cluster_key": cluster_key,
                                    "accepted_producer_attempt": producer_attempt,
                                    "producer_contract": "complete_lossless_producer",
                                    "producer_contract_version": (
                                        self.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                                    ),
                                    "boundary_preservation_constraint": (
                                        boundary_preservation_constraint or {}
                                    ),
                                    "contract_errors": [],
                                },
                            )
                    break

                producer_retry_receipt.append(
                    {
                        "attempt": producer_attempt,
                        "errors": contract_errors,
                    }
                )
                if self.pom:
                    self._write_instance_split_debug_json(
                        self._instance_split_lossless_debug_dir(cluster_key)
                        / "producer_contract_retry_manifest.json",
                        {
                            "cluster_key": cluster_key,
                            "attempts": producer_retry_receipt,
                            "retry_contract": "complete_lossless_producer",
                        },
                    )
                if producer_attempt >= self.INSTANCE_SPLIT_MAX_PRODUCER_ATTEMPTS:
                    raise ValueError(
                        "instance split producer contract is incomplete after "
                        f"{producer_attempt} complete lossless producer attempts: "
                        f"{contract_errors}"
                    )
                producer_errors = contract_errors
                if self.logger:
                    self.logger.log_warning(
                        "[Instance Split Producer Contract] "
                        f"{cluster_key} attempt {producer_attempt} invalid; "
                        "retrying the complete lossless producer with formal errors"
                    )

            if not result.get("split_needed", True):
                return []

            instances = result.get("instances", [])
            if not isinstance(instances, list) or len(instances) < 2:
                return []

            obs_by_key: Dict[str, FrameObservation] = {self._obs_key(o): o for o in sorted_obs}
            obs_by_trace_step: Dict[Tuple[str, int], FrameObservation] = {
                (str(o.trace_id), int(o.step_index)): o for o in sorted_obs
            }
            instance_groups: List[Dict[str, Any]] = []
            seen_instance_keys = set()
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                group_obs: List[FrameObservation] = []
                obs_keys = inst.get("obs_keys", [])
                if isinstance(obs_keys, list):
                    for obs_key in obs_keys:
                        obs = obs_by_key.get(str(obs_key).strip())
                        if obs is not None and obs not in group_obs:
                            group_obs.append(obs)
                trace_steps = inst.get("trace_steps", [])
                if isinstance(trace_steps, list):
                    for ref in trace_steps:
                        if not isinstance(ref, dict):
                            continue
                        trace_id = str(ref.get("trace_id", "")).strip()
                        try:
                            step_index = int(ref.get("step_index", -1))
                        except Exception:
                            continue
                        obs = obs_by_trace_step.get((trace_id, step_index))
                        if obs is not None and obs not in group_obs:
                            group_obs.append(obs)
                if not group_obs:
                    step_indices = inst.get("step_indices", [])
                    if not isinstance(step_indices, list) or not step_indices:
                        continue
                    for idx in step_indices:

                        matching = [obs for obs in sorted_obs if int(obs.step_index) == int(idx)]
                        if len(matching) == 1 and matching[0] not in group_obs:
                            group_obs.append(matching[0])
                if not group_obs:
                    continue
                label = str(inst.get("instance_label", "") or inst.get("instance_id", "")).strip()
                key_hint = str(inst.get("instance_key_hint", "")).strip()
                instance_id = str(inst.get("instance_id", "")).strip()
                split_dimension = str(inst.get("split_dimension", "") or "").strip()
                split_reason = inst.get("split_reason", {})
                reason = str(inst.get("reason", "") or "").strip()
                raw_key = self._build_instance_key(
                    cluster_key=cluster_key,
                    index=len(instance_groups) + 1,
                    label=label,
                    key_hint=key_hint,
                    instance_id=instance_id,
                )
                instance_key = self._dedupe_instance_key(raw_key, seen_instance_keys)
                instance_group = {
                    "instance_key": instance_key,
                    "instance_label": label or f"Instance {len(instance_groups)+1}",
                    "trace_ids": sorted(set(o.trace_id for o in group_obs)),
                    "obs_keys": [self._obs_key(o) for o in group_obs],
                    "cluster_obs": group_obs,
                }
                if split_dimension:
                    instance_group["split_dimension"] = split_dimension
                if isinstance(split_reason, dict) and split_reason:
                    instance_group["split_reason"] = split_reason
                if reason:
                    instance_group["reason"] = reason
                instance_groups.append(instance_group)

            original_instance_count = len(instance_groups)
            instance_groups = self._filter_out_of_general_page_instance_groups(
                cluster_key,
                instance_groups,
            )
            dropped_out_of_page = len(instance_groups) < original_instance_count

            if len(instance_groups) >= 2:
                instance_groups = self._merge_same_page_continuation_instance_groups(
                    instance_groups,
                    boundary_preservation_constraint=boundary_preservation_constraint,
                )
                if len(instance_groups) >= 2:
                    instance_groups = self._merge_form_future_action_instance_groups(
                        cluster_key,
                        page_context,
                        instance_groups,
                    )
                if len(instance_groups) >= 2:
                    instance_groups = self._demote_submitted_query_instance_groups(
                        cluster_key,
                        instance_groups,
                        page_context=page_context,
                    )
                postprocess_boundary_errors = (
                    self._validate_instance_split_boundary_preservation_constraint(
                        {
                            "split_needed": len(instance_groups) >= 2,
                            "instances": instance_groups,
                        },
                        boundary_preservation_constraint,
                    )
                )
                if postprocess_boundary_errors:
                    raise ValueError(
                        "instance split postprocess violated the bound boundary "
                        "preservation constraint: "
                        f"{postprocess_boundary_errors}"
                    )
                if reused_accepted_candidate and self.pom:
                    self._write_instance_split_debug_json(
                        self._instance_split_lossless_debug_dir(cluster_key)
                        / "accepted"
                        / "reuse_validation.json",
                        {
                            "cluster_key": cluster_key,
                            "producer_contract_version": (
                                self.INSTANCE_SPLIT_PRODUCER_CONTRACT_VERSION
                            ),
                            "input_rows_hash": self._stable_json_fingerprint(
                                steps_payload
                            ),
                            "boundary_preservation_constraint_hash": (
                                self._stable_json_fingerprint(
                                    boundary_preservation_constraint or {}
                                )
                            ),
                            "accepted_output_hash": self._stable_json_fingerprint(
                                result
                            ),
                            "postprocess_instance_groups_hash": (
                                self._stable_json_fingerprint(
                                    [
                                        {
                                            key: value
                                            for key, value in group.items()
                                            if key != "cluster_obs"
                                        }
                                        for group in instance_groups
                                    ]
                                )
                            ),
                            "validation_state": (
                                "raw_and_postprocess_contracts_passed"
                            ),
                        },
                    )
                if len(instance_groups) < 2:
                    if self.logger:
                        self.logger.log_info(
                            f"[Instance Split LLM] {cluster_key}: collapsed same-page continuation split"
                        )
                    return []
                if self.logger:
                    self.logger.log_info(
                        f"[Instance Split LLM] {cluster_key}: split into {len(instance_groups)} instances"
                    )
                return instance_groups
            if len(instance_groups) == 1 and dropped_out_of_page:
                if self.logger:
                    self.logger.log_info(
                        f"[Instance Split LLM] {cluster_key}: kept 1 in-page instance after dropping out-of-page candidates"
                    )
                return instance_groups
            return []
        except (LLMAPIResponseError, LLMJsonResponseError) as e:
            if self.logger:
                self.logger.log_error(f"[Instance Split LLM] failed for {cluster_key}: {e}")
            raise
        except Exception as e:
            if self.logger:
                self.logger.log_error(f"[Instance Split LLM] failed for {cluster_key}: {e}")
            raise

    @staticmethod
    def _extract_render_role_reasons(debug: Dict[str, Any]) -> Dict[str, str]:
        reasons: Dict[str, str] = {}
        if not isinstance(debug, dict):
            return reasons
        for cid, cdata in (debug.get("clusters", {}) or {}).items():
            if not isinstance(cdata, dict):
                continue
            for step in cdata.get("unique_steps", []):
                if isinstance(step, dict) and step.get("step_key") and step.get("reason"):
                    reasons[step["step_key"]] = step["reason"]
            for step in cdata.get("duplicate_steps", []):
                if isinstance(step, dict) and step.get("step_key") and step.get("reason"):
                    reasons[step["step_key"]] = step["reason"]
        return reasons

    def _screenshot_render_classification_group_id(self, cluster_key: str, instance_key: str) -> str:
        instance_key = str(instance_key or "").strip()
        if not instance_key:
            return str(cluster_key)
        return f"{cluster_key}::{instance_key}"

    def _build_task_actions_by_cluster(
        self,
        global_reasoning: Dict[str, Any],
        clusters: Dict[str, List[FrameObservation]],
        observations: List[FrameObservation],
    ) -> Dict[str, List[Dict[str, Any]]]:
        canonical_routes = global_reasoning.get("canonical_routes", []) if isinstance(global_reasoning, dict) else []
        step_labels = global_reasoning.get("step_labels", []) if isinstance(global_reasoning, dict) else []
        obs_map: Dict[str, FrameObservation] = {
            f"{obs.trace_id}:{obs.step_index}": obs for obs in observations or []
        }
        trace_steps_by_cluster: Dict[str, Dict[str, List[str]]] = {}
        for label in step_labels:
            if not isinstance(label, dict):
                continue
            cluster_id = str(label.get("page_cluster_id", "")).strip()
            trace_id = str(label.get("trace_id", "")).strip()
            step_idx = int(label.get("step_index", -1))
            if not cluster_id or not trace_id:
                continue
            trace_steps_by_cluster.setdefault(cluster_id, {}).setdefault(trace_id, []).append(
                f"step{step_idx}"
            )
        route_triggers_by_cluster: Dict[str, List[str]] = {}
        for route in canonical_routes:
            if not isinstance(route, dict):
                continue
            src = str(route.get("from_page_cluster_id", "")).strip()
            if src:
                trigger = str(route.get("trigger", "")).strip()
                if trigger:
                    route_triggers_by_cluster.setdefault(src, []).append(trigger)
        result: Dict[str, List[Dict[str, Any]]] = {}
        for cluster_id, traces in trace_steps_by_cluster.items():
            actions: List[Dict[str, Any]] = []
            for trace_id, steps in traces.items():
                sorted_steps = sorted(steps, key=lambda s: int(s.replace("step", "")) if s.replace("step", "").isdigit() else 0)
                task_summary_parts = []
                for step in sorted_steps:
                    step_idx = int(step.replace("step", ""))
                    obs = obs_map.get(f"{trace_id}:{step_idx}")
                    if obs:
                        action = str(getattr(obs, "action", "") or "").strip()
                        if action:
                            task_summary_parts.append(action)
                if not task_summary_parts:
                    continue
                actions.append({
                    "trace_id": trace_id,
                    "task_summary": " → ".join(task_summary_parts[:5]),
                    "steps_on_page": sorted_steps,
                    "entry_step": sorted_steps[0] if sorted_steps else "",
                    "exit_step": sorted_steps[-1] if sorted_steps else "",
                })
            if actions:
                result[cluster_id] = actions
        return result

    def _build_screenshot_render_classification_groups(
        self,
        clusters: Dict[str, List[FrameObservation]],
        content_jobs: List[Dict[str, Any]],
        instance_planning_debug: Dict[str, Dict[str, Any]],
        page_info_map: Dict[str, Dict[str, Any]],
        task_actions_by_cluster: Dict[str, List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        groups: List[Dict[str, Any]] = []
        covered_clusters = set()
        _task_actions = task_actions_by_cluster or {}

        def add_group(
            cluster_key: str,
            group_obs: List[FrameObservation],
            *,
            instance_key: str = "",
            instance_label: str = "",
            group_id_override: str = "",
            source_page_cluster_id_override: str = "",
        ) -> None:
            if not group_obs:
                return
            page_meta = dict(page_info_map.get(cluster_key, {}))
            group_id = str(group_id_override or "").strip() or self._screenshot_render_classification_group_id(cluster_key, instance_key)
            source_page_cluster_id = str(source_page_cluster_id_override or cluster_key).strip()
            page_meta["page_cluster_id"] = group_id
            page_meta["general_page_id"] = cluster_key
            page_meta["materialized_page_id"] = group_id
            page_meta["source_page_cluster_id"] = source_page_cluster_id
            if instance_key:
                page_meta["instance_key"] = instance_key
            if instance_label:
                page_meta["instance_label"] = instance_label
                base_name = str(page_meta.get("page_name", "")).strip()
                page_meta["page_name"] = f"{base_name} · {instance_label}" if base_name else instance_label
            group_entry: Dict[str, Any] = {
                "group_id": group_id,
                "general_page_id": cluster_key,
                "materialized_page_id": group_id,
                "source_page_cluster_id": source_page_cluster_id,
                "instance_key": instance_key,
                "instance_label": instance_label,
                "cluster_obs": group_obs,
                "canonical_page": page_meta,
            }
            cluster_tasks = _task_actions.get(cluster_key, [])
            if cluster_tasks:
                group_entry["task_actions_on_page"] = cluster_tasks
            groups.append(group_entry)

        for job in content_jobs:
            cluster_key = str(job.get("cluster_key", "")).strip()
            if not cluster_key:
                continue
            covered_clusters.add(cluster_key)
            if job.get("job_type") == "instance":
                group = job.get("instance_group", {}) if isinstance(job.get("instance_group", {}), dict) else {}
                add_group(
                    cluster_key,
                    job.get("cluster_obs", []) if isinstance(job.get("cluster_obs", []), list) else [],
                    instance_key=str(group.get("instance_key", "")).strip(),
                    instance_label=str(group.get("instance_label", "")).strip(),
                )
            else:
                add_group(
                    cluster_key,
                    job.get("cluster_obs", []) if isinstance(job.get("cluster_obs", []), list) else [],
                )

        for cluster_key, cluster_obs in clusters.items():
            if cluster_key in covered_clusters:
                continue
            obs_lookup = {self._obs_key(obs): obs for obs in cluster_obs}
            added_instance = False
            planning = instance_planning_debug.get(cluster_key, {})
            instance_groups = planning.get("instance_groups", []) if isinstance(planning, dict) else []
            if isinstance(instance_groups, list):
                for group in instance_groups:
                    if not isinstance(group, dict):
                        continue
                    obs_keys = [str(key).strip() for key in group.get("obs_keys", []) if str(key).strip()]
                    group_obs = [obs_lookup[key] for key in obs_keys if key in obs_lookup]
                    if not group_obs:
                        continue
                    add_group(
                        cluster_key,
                        group_obs,
                        instance_key=str(group.get("instance_key", "")).strip(),
                        instance_label=str(group.get("instance_label", "")).strip(),
                    )
                    added_instance = True
            if not added_instance:
                add_group(cluster_key, cluster_obs)

        return groups

    def _filter_l0_content_extraction_jobs(
        self,
        content_jobs: List[Dict[str, Any]],
        *,
        structure_unique_keys: set,
        render_role_by_step: Dict[str, str],
        output_dir: str = "",
    ) -> List[Dict[str, Any]]:
        if not content_jobs:
            return content_jobs

        unique_keys = {
            canonical_step_key(str(key))
            for key in structure_unique_keys or set()
            if canonical_step_key(str(key))
        }
        roles = {
            canonical_step_key(str(key)): str(value or "").strip()
            for key, value in normalize_step_keyed_mapping(render_role_by_step or {}).items()
            if canonical_step_key(str(key))
        }
        filtered_jobs: List[Dict[str, Any]] = []
        job_records: List[Dict[str, Any]] = []
        input_count = 0
        selected_count = 0

        for index, job in enumerate(content_jobs):
            observations = job.get("cluster_obs", []) if isinstance(job.get("cluster_obs", []), list) else []
            input_count += len(observations)
            selected: List[FrameObservation] = []
            dropped_keys: List[str] = []
            reasons: Dict[str, str] = {}

            for obs in observations:
                step_key = canonical_step_key(self._obs_key(obs))
                role = roles.get(step_key, "")
                if step_key in unique_keys:
                    selected.append(obs)
                    reasons[step_key] = "structure_unique"
                elif role in self.CONTENT_EXTRACTION_L0_RENDER_ROLES:
                    selected.append(obs)
                    reasons[step_key] = f"render_role:{role}"
                else:
                    dropped_keys.append(step_key)

            if not selected and observations:
                canonical = next(
                    (
                        obs for obs in observations
                        if roles.get(canonical_step_key(self._obs_key(obs)), "") == "canonical_layout"
                    ),
                    observations[0],
                )
                step_key = canonical_step_key(self._obs_key(canonical))
                selected = [canonical]
                if step_key in dropped_keys:
                    dropped_keys.remove(step_key)
                reasons[step_key] = "fallback_representative"

            selected_keys = [canonical_step_key(self._obs_key(obs)) for obs in selected]
            selected_count += len(selected)
            filtered_job = dict(job)
            filtered_job["cluster_obs"] = selected
            filtered_job["content_extraction_input_selection"] = {
                "policy": "l0_lightweight_prefilter",
                "input_obs_count": len(observations),
                "selected_obs_count": len(selected),
                "selected_step_keys": selected_keys,
                "dropped_step_keys": dropped_keys,
                "selection_reasons": reasons,
            }
            filtered_jobs.append(filtered_job)
            job_records.append({
                "job_index": index,
                "job_type": job.get("job_type", ""),
                "cluster_key": job.get("cluster_key", ""),
                "instance_order": job.get("instance_order", None),
                "input_obs_count": len(observations),
                "selected_obs_count": len(selected),
                "selected_step_keys": selected_keys,
                "dropped_step_keys": dropped_keys,
                "selection_reasons": reasons,
            })

        payload = {
            "format": "l0_content_extraction_input_selection",
            "schema_revision": 1,
            "selection_policy": "l0_lightweight_prefilter",
            "allowed_render_roles": sorted(self.CONTENT_EXTRACTION_L0_RENDER_ROLES),
            "summary": {
                "job_count": len(content_jobs),
                "input_observation_count": input_count,
                "selected_observation_count": selected_count,
                "dropped_observation_count": max(input_count - selected_count, 0),
            },
            "jobs": job_records,
        }
        if output_dir and self.pom:
            self.pom.write_json(
                "l0_fidelity",
                "formal",
                "content_extraction_input_selection.json",
                payload,
                artifact_id="l0_fidelity.content_extraction_input_selection",
                artifact_kind="det_projection",
                producer="AppSpecBuilder._filter_l0_content_extraction_jobs",
            )
        if self.logger:
            self.logger.log_info(
                "[SM-P4.5] L0 content extraction input prefilter selected "
                f"{selected_count}/{input_count} observations across {len(content_jobs)} jobs"
            )
        return filtered_jobs

    def _load_cached_screenshot_render_classification(
        self,
        output_dir: str,
        page_groups: List[Dict[str, Any]],
    ) -> Optional[Tuple[Dict[str, bool], Dict[str, Dict[str, Any]], Dict[str, Any]]]:
        if not output_dir:
            return None
        path = (
            str(self.pom.formal("l0_fidelity", "screenshot_render_classification.json"))
            if self.pom
            else os.path.join(output_dir, "phase_l0_fidelity", "screenshot_render_classification.json")
        )
        if not os.path.exists(path):
            return None
        debug = self._read_json_file_safely(path, {})
        clusters = debug.get("clusters", {}) if isinstance(debug, dict) else {}
        if not isinstance(clusters, dict) or not clusters:
            return None

        unique_by_key: Dict[str, bool] = {}
        scope_by_key: Dict[str, Dict[str, Any]] = {}
        missing_groups: List[str] = []
        uncovered_steps: List[str] = []
        missing_lifecycle_steps: List[str] = []
        lifecycle_by_key = normalize_step_keyed_mapping(debug.get("screenshot_lifecycle_by_step", {}))
        if not lifecycle_by_key:
            for cdata in clusters.values():
                if isinstance(cdata, dict):
                    lifecycle_by_key.update(
                        normalize_step_keyed_mapping(cdata.get("screenshot_lifecycle_by_step", {}))
                    )
        for group in page_groups:
            group_id = str(group.get("group_id", "") or "").strip()
            if not group_id:
                continue
            cached = clusters.get(group_id)
            if not isinstance(cached, dict):
                missing_groups.append(group_id)
                continue
            group_step_keys = {
                canonical_step_key(self._obs_key(obs))
                for obs in group.get("cluster_obs", [])
            }
            unique_keys = {
                canonical_step_key(key)
                for key in cached.get("unique_step_keys", [])
                if canonical_step_key(key)
            }
            duplicate_keys = {
                canonical_step_key(key)
                for key in cached.get("duplicate_step_keys", [])
                if canonical_step_key(key)
            }
            covered_keys = unique_keys | duplicate_keys
            missing_step_keys = sorted(group_step_keys - covered_keys)
            if missing_step_keys:
                uncovered_steps.extend(missing_step_keys)
                continue
            missing_lifecycle = sorted(group_step_keys - set(lifecycle_by_key))
            if missing_lifecycle:
                missing_lifecycle_steps.extend(missing_lifecycle)
                continue

            general_page_id = str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
            scope = {
                "materialized_page_id": group_id,
                "general_page_id": general_page_id,
                "source_page_cluster_id": str(group.get("source_page_cluster_id") or general_page_id).strip(),
                "instance_key": str(group.get("instance_key") or "").strip(),
                "instance_label": str(group.get("instance_label") or "").strip(),
            }
            for step_key in group_step_keys:
                unique_by_key[step_key] = step_key in unique_keys
                scope_by_key[step_key] = scope

        if missing_groups or uncovered_steps or missing_lifecycle_steps:
            if self.logger:
                self.logger.log_info(
                    "[SM-P4] Existing screenshot_render_classification.json is incomplete "
                    f"(missing_groups={len(missing_groups)}, uncovered_steps={len(uncovered_steps)}, "
                    f"missing_lifecycle_steps={len(missing_lifecycle_steps)}); rerunning labeling"
                )
            return None

        debug = json.loads(json.dumps(debug, ensure_ascii=False))
        summary = debug.setdefault("summary", {})
        if "gr_p4_unique_count" not in summary and "phase4_unique_count" in summary:
            summary["gr_p4_unique_count"] = summary.get("phase4_unique_count", 0)
        if "gr_p4_duplicate_count" not in summary and "phase4_duplicate_count" in summary:
            summary["gr_p4_duplicate_count"] = summary.get("phase4_duplicate_count", 0)
        if "phase4_unique_count" not in summary and "gr_p4_unique_count" in summary:
            summary["phase4_unique_count"] = summary.get("gr_p4_unique_count", 0)
        if "phase4_duplicate_count" not in summary and "gr_p4_duplicate_count" in summary:
            summary["phase4_duplicate_count"] = summary.get("gr_p4_duplicate_count", 0)
        summary["cache_reused"] = True
        debug["cache_reused"] = True
        return unique_by_key, scope_by_key, debug

    async def _apply_screenshot_render_classification_to_materialized_page_groups(
        self,
        global_reasoning: Dict[str, Any],
        observations: List[FrameObservation],
        page_groups: List[Dict[str, Any]],
        output_dir: str = "",
    ) -> Tuple[Dict[str, Any], set, Dict[str, Any], Dict[str, str], Dict[str, str], Dict[str, Dict[str, Any]]]:
        label_by_key: Dict[str, Dict[str, Any]] = {}
        for label in global_reasoning.get("step_labels", []) if isinstance(global_reasoning, dict) else []:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            label_by_key[f"{trace_id}:{step_index}"] = label

        temp_labels: List[Dict[str, Any]] = []
        temp_pages: List[Dict[str, Any]] = []
        seen_pages = set()
        for group in page_groups:
            group_id = str(group.get("group_id", "")).strip()
            if not group_id:
                continue
            page = dict(group.get("canonical_page", {}))
            general_page_id = str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
            page["page_cluster_id"] = group_id
            page["materialized_page_id"] = group_id
            if general_page_id:
                page["general_page_id"] = general_page_id
                page["source_page_cluster_id"] = general_page_id
            if group_id not in seen_pages:
                temp_pages.append(page)
                seen_pages.add(group_id)
            for obs in group.get("cluster_obs", []):
                key = self._obs_key(obs)
                source_label = label_by_key.get(key, {})
                temp_labels.append({
                    "trace_id": str(obs.trace_id),
                    "step_index": int(obs.step_index),
                    "page_cluster_id": group_id,
                    "materialized_page_id": group_id,
                    "general_page_id": general_page_id,
                    "source_page_cluster_id": str(group.get("source_page_cluster_id") or general_page_id),
                    "instance_key": str(group.get("instance_key", "") or ""),
                    "instance_label": str(group.get("instance_label", "") or ""),
                    "is_page_transition": bool(source_label.get("is_page_transition", True)),
                    "has_unique_page_structure": bool(source_label.get("has_unique_page_structure", True)),
                    "confidence": float(source_label.get("confidence", getattr(obs, "confidence", 0.75)) or 0.75),
                })

        cached = self._load_cached_screenshot_render_classification(output_dir, page_groups)
        if cached:
            unique_by_key, scope_by_key, debug = cached
            updated_global = json.loads(json.dumps(global_reasoning, ensure_ascii=False))
            for label in updated_global.get("step_labels", []):
                if not isinstance(label, dict):
                    continue
                key = canonical_step_key(f"{str(label.get('trace_id', '')).strip()}:{int(label.get('step_index', -1))}")
                if key in unique_by_key:
                    label["has_unique_page_structure"] = unique_by_key[key]
                if key in scope_by_key:
                    for field, value in scope_by_key[key].items():
                        if value:
                            label[field] = value
            unique_keys = {key for key, is_unique in unique_by_key.items() if is_unique}
            setattr(self, "_last_screenshot_render_classification_debug", debug)
            if self.logger:
                self.logger.log_info(
                    f"[GR-P4] Reused screenshot render classification cache: "
                    f"{len(unique_keys)} unique / {len(unique_by_key) - len(unique_keys)} duplicate labels"
                )
            cached_render_roles = normalize_step_keyed_mapping(
                debug.get("render_role_by_step", {}) if isinstance(debug, dict) else {}
            )
            cached_iak = normalize_step_keyed_mapping(
                debug.get("initial_authority_kind_by_step", {}) if isinstance(debug, dict) else {}
            )
            if not cached_render_roles:
                for cid, cdata in (debug.get("clusters", {}) if isinstance(debug, dict) else {}).items():
                    if isinstance(cdata, dict):
                        cached_render_roles.update(normalize_step_keyed_mapping(cdata.get("render_role_by_step", {})))
                        cached_iak.update(normalize_step_keyed_mapping(cdata.get("initial_authority_kind_by_step", {})))
            cached_lifecycle = normalize_step_keyed_mapping(
                debug.get("screenshot_lifecycle_by_step", {}) if isinstance(debug, dict) else {}
            )
            if not cached_lifecycle:
                for cdata in (debug.get("clusters", {}) if isinstance(debug, dict) else {}).values():
                    if isinstance(cdata, dict):
                        cached_lifecycle.update(normalize_step_keyed_mapping(cdata.get("screenshot_lifecycle_by_step", {})))
            return updated_global, unique_keys, debug, cached_render_roles, cached_iak, cached_lifecycle

        if not getattr(self, "screenshot_render_classification_enabled", True):
            unique_keys = {
                canonical_step_key(f"{label['trace_id']}:{label['step_index']}")
                for label in temp_labels
                if bool(label.get("has_unique_page_structure", True))
            }
            debug = {
                "schema_version": "screenshot_render_classification.disabled",
                "labeling_scope": "materialized_pages",
                "scope_term": "Materialized Page",
                "parent_term": "General Page",
                "legacy_labeling_scope_alias": "post_instance_split_page_groups",
                "naming": {
                    "scope_term": "Materialized Page",
                    "scope_id_field": "materialized_page_id",
                    "parent_term": "General Page",
                    "parent_id_field": "general_page_id",
                    "legacy_scope_id_field": "page_cluster_id",
                },
                "summary": {
                    "cluster_count": len(temp_pages),
                    "materialized_page_count": len(temp_pages),
                    "general_page_count": len({
                        str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
                        for group in page_groups
                        if str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
                    }),
                    "step_label_count": len(temp_labels),
                    "incoming_unique_count": len(unique_keys),
                    "gr_p4_unique_count": len(unique_keys),
                    "gr_p4_duplicate_count": max(0, len(temp_labels) - len(unique_keys)),
                    "phase4_unique_count": len(unique_keys),
                    "phase4_duplicate_count": max(0, len(temp_labels) - len(unique_keys)),
                    "materialized_unique_count": len(unique_keys),
                    "materialized_duplicate_count": max(0, len(temp_labels) - len(unique_keys)),
                    "unique_delta_from_incoming": 0,
                    "materialized_page_group_count": len(temp_pages),
                },
                "clusters": {},
            }
            setattr(self, "_last_screenshot_render_classification_debug", debug)
            return global_reasoning, unique_keys, debug, {}, {}, {}


        temp_reasoning = {
            "app_type": str(global_reasoning.get("app_type", global_reasoning.get("website_type", ""))).strip(),
            "step_labels": temp_labels,
            "canonical_pages": temp_pages,
        }
        group_task_actions: Dict[str, List[Dict[str, Any]]] = {}
        for group in page_groups:
            gid = str(group.get("group_id", "")).strip()
            actions = group.get("task_actions_on_page")
            if gid and isinstance(actions, list) and actions:
                group_task_actions[gid] = actions
        updated_temp, debug = await apply_screenshot_render_classification(
            temp_reasoning,
            observations,
            self._run_screenshot_render_classification_judge,
            task_actions_by_cluster=group_task_actions,
        )
        debug["labeling_scope"] = "materialized_pages"
        debug["scope_term"] = "Materialized Page"
        debug["parent_term"] = "General Page"
        debug["legacy_labeling_scope_alias"] = "post_instance_split_page_groups"
        debug.setdefault("naming", {
            "scope_term": "Materialized Page",
            "scope_id_field": "materialized_page_id",
            "parent_term": "General Page",
            "parent_id_field": "general_page_id",
            "legacy_scope_id_field": "page_cluster_id",
        })
        general_page_count = len({
            str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
            for group in page_groups
            if str(group.get("general_page_id") or group.get("source_page_cluster_id") or "").strip()
        })
        debug.setdefault("summary", {})["materialized_page_count"] = len(temp_pages)
        debug.setdefault("summary", {})["general_page_count"] = general_page_count
        summary = debug.setdefault("summary", {})
        if "gr_p4_unique_count" not in summary and "phase4_unique_count" in summary:
            summary["gr_p4_unique_count"] = summary.get("phase4_unique_count", 0)
        if "gr_p4_duplicate_count" not in summary and "phase4_duplicate_count" in summary:
            summary["gr_p4_duplicate_count"] = summary.get("phase4_duplicate_count", 0)
        if "phase4_unique_count" not in summary and "gr_p4_unique_count" in summary:
            summary["phase4_unique_count"] = summary.get("gr_p4_unique_count", 0)
        if "phase4_duplicate_count" not in summary and "gr_p4_duplicate_count" in summary:
            summary["phase4_duplicate_count"] = summary.get("gr_p4_duplicate_count", 0)
        summary["materialized_unique_count"] = summary.get("gr_p4_unique_count", 0)
        summary["materialized_duplicate_count"] = summary.get("gr_p4_duplicate_count", 0)
        debug.setdefault("summary", {})["materialized_page_group_count"] = len(temp_pages)
        debug.setdefault("summary", {})["source_page_cluster_count"] = general_page_count

        unique_by_key: Dict[str, bool] = {}
        scope_by_key: Dict[str, Dict[str, Any]] = {}
        for label in updated_temp.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            key = canonical_step_key(f"{str(label.get('trace_id', '')).strip()}:{int(label.get('step_index', -1))}")
            unique_by_key[key] = bool(label.get("has_unique_page_structure", True))
            scope_by_key[key] = {
                "materialized_page_id": str(label.get("materialized_page_id") or label.get("page_cluster_id") or "").strip(),
                "general_page_id": str(label.get("general_page_id") or "").strip(),
                "source_page_cluster_id": str(label.get("source_page_cluster_id") or "").strip(),
                "instance_key": str(label.get("instance_key") or "").strip(),
                "instance_label": str(label.get("instance_label") or "").strip(),
            }

        updated_global = json.loads(json.dumps(global_reasoning, ensure_ascii=False))
        for label in updated_global.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            key = canonical_step_key(f"{str(label.get('trace_id', '')).strip()}:{int(label.get('step_index', -1))}")
            if key in unique_by_key:
                label["has_unique_page_structure"] = unique_by_key[key]
            if key in scope_by_key:
                for field, value in scope_by_key[key].items():
                    if value:
                        label[field] = value
        unique_keys = {key for key, is_unique in unique_by_key.items() if is_unique}
        render_roles = debug.get("render_role_by_step", {}) if isinstance(debug, dict) else {}
        initial_authority_kinds = debug.get("initial_authority_kind_by_step", {}) if isinstance(debug, dict) else {}
        screenshot_lifecycle = debug.get("screenshot_lifecycle_by_step", {}) if isinstance(debug, dict) else {}
        setattr(self, "_last_screenshot_render_classification_debug", debug)
        return updated_global, unique_keys, debug, render_roles, initial_authority_kinds, screenshot_lifecycle

    async def build_from_traces(self, traces: List[TraceData], output_dir: str = None) -> AppSpec:
        return await self.build_from_traces_v3(traces, output_dir)

    async def build_from_traces_v3(self, traces: List[TraceData], output_dir: str = None) -> AppSpec:
        if not traces:
            raise ValueError("No traces provided")
        self._current_output_dir = output_dir or ""
        if output_dir:
            self.pom = PhaseOutputManager(output_dir)
        website_url = traces[0].website_url
        if self.logger:
            self.logger.log_info(
                f"Building AppSpec V3 (parallel={self.parallel_enabled}) from {len(traces)} traces for {website_url}"
            )


        jobs = self.collect_frame_jobs_v3(traces)
        if self.logger:
            self.logger.log_info(f"Collected {len(jobs)} valid frame jobs")
        if not jobs:
            raise ValueError("No valid frame jobs found from traces")


        loaded_route_tree: Dict[str, Any] = {}
        loaded_site_graph: Dict[str, Any] = {}
        allowed_trace_ids = {
            str(getattr(trace, "trace_id", "") or "")
            for trace in traces
            if str(getattr(trace, "trace_id", "") or "")
        }
        existing_global_reasoning = self._load_existing_global_reasoning(
            output_dir or "",
            allowed_trace_ids=allowed_trace_ids,
        )


        if self.logger:
            self.logger.log_info(f"[SM-P1] State description for {len(jobs)} frames...")
        cache_map = self._load_state_description_cache("", output_dir=output_dir)
        state_observations = await self._analyze_state_descriptions_parallel(jobs, cache_map=cache_map, output_dir=output_dir)
        if self.logger:
            self.logger.log_info(f"[SM-P1] Done: {len(state_observations)} state descriptions")

        if existing_global_reasoning:
            covers_retained, missing_retained = (
                self._existing_global_reasoning_covers_retained_observations(
                    existing_global_reasoning,
                    state_observations,
                )
            )
            if not covers_retained:
                if self.logger:
                    self.logger.log_warning(
                        "[SM-P2/GR] Existing global reasoning is stale: retained "
                        "terminal observations have no page labels; regenerating producer "
                        f"artifact, missing={missing_retained[:20]}"
                )
                existing_global_reasoning = {}

        if existing_global_reasoning:
            covers_route_occurrences, missing_route_occurrences = (
                self._existing_global_reasoning_covers_route_occurrences(
                    existing_global_reasoning,
                    traces,
                )
            )
            if not covers_route_occurrences:
                if self.logger:
                    self.logger.log_warning(
                        "[SM-P2/GR] Existing global reasoning is stale: canonical "
                        "routes omit source-step occurrence evidence; regenerating "
                        f"producer artifact, missing={missing_route_occurrences[:20]}"
                    )
                existing_global_reasoning = {}

        if existing_global_reasoning:
            global_reasoning = existing_global_reasoning
            if self.logger:
                self.logger.log_info(
                    f"[SM-P2/GR] Reusing existing global reasoning artifact: "
                    f"{len(global_reasoning.get('step_labels', []))} step labels, "
                    f"{len(global_reasoning.get('canonical_pages', []))} General Pages, "
                    f"{len(global_reasoning.get('canonical_routes', []))} routes"
                )
        else:

            if self.logger:
                self.logger.log_info("[SM-P2/GR] Running global reasoning for General Page routing...")
            if self.global_reasoning_enabled:
                try:
                    global_reasoning = await self._infer_global_reasoning(
                        state_observations, traces, website_url
                    )
                except ScreenshotRenderClassificationError:
                    raise
                except Exception as e:
                    raise RuntimeError(
                        "Global reasoning failed and cannot be replaced by signature clustering. "
                        "Regenerate the producer artifact or fix the upstream API/router failure."
                    ) from e
            else:
                global_reasoning = {}

            if not global_reasoning:
                raise RuntimeError(
                    "Global reasoning produced no artifact. Signature-clustering fallback is disabled "
                    "because it hides incomplete producer coverage."
                )

            if self.logger:
                self.logger.log_info(
                    f"[SM-P2/GR] Done: {len(global_reasoning.get('step_labels', []))} step labels, "
                    f"{len(global_reasoning.get('canonical_pages', []))} General Pages, "
                    f"{len(global_reasoning.get('canonical_routes', []))} routes"
                )
            if output_dir:
                self._save_debug_global_reasoning_raw(global_reasoning, output_dir)
                self._save_debug_task_elements(global_reasoning, output_dir)
        if self.stop_after_global_reasoning:
            raise GlobalReasoningStopAfterReached()


        if self.skip_full_extraction_enabled:
            if self.logger:
                self.logger.log_info("[SM-P1] skip_full_extraction enabled: skipping SM-P3/SM-P4 full extraction")
            site_model = await self._consolidate_observations_v3(
                state_observations, traces, website_url,
                global_reasoning=global_reasoning,
                loaded_route_tree=loaded_route_tree,
                loaded_site_graph=loaded_site_graph,
                output_dir=output_dir
            )
            if output_dir:
                self._save_parallel_summary(self._build_summary(
                    traces, jobs, state_observations, site_model
                ), output_dir)
            return site_model


        page_context_map = self._build_page_context_map(global_reasoning)
        skipped_step_keys = self._skipped_step_keys(global_reasoning)
        if self.logger:
            self.logger.log_info(
                f"[SM-P2→SM-P4] Built page context for {len(page_context_map)} observations"
            )


        step_labels = global_reasoning.get("step_labels", [])
        canonical_pages = global_reasoning.get("canonical_pages", [])
        page_info_map = {
            str(p.get("page_cluster_id", "")).strip(): p
            for p in canonical_pages if isinstance(p, dict) and str(p.get("page_cluster_id", "")).strip()
        }


        state_obs_map: Dict[str, FrameObservation] = {}
        for obs in state_observations:
            state_obs_map[f"{obs.trace_id}:{obs.step_index}"] = obs


        clusters: Dict[str, List[FrameObservation]] = {}
        obs_cluster: Dict[str, str] = {}
        structure_unique_keys: set = set()
        for label in step_labels:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            obs_key = f"{trace_id}:{step_index}"
            if obs_key in skipped_step_keys:
                continue
            obs = state_obs_map.get(obs_key)
            if obs is None:
                continue
            cluster_key = str(label.get("page_cluster_id", "")).strip()
            if not cluster_key:
                cluster_key = self._build_signature_cluster_key(obs)
            clusters.setdefault(cluster_key, []).append(obs)
            obs_cluster[obs_key] = cluster_key

            if label.get("has_unique_page_structure", True):
                structure_unique_keys.add(obs_key)


        for obs in state_observations:
            obs_key = f"{obs.trace_id}:{obs.step_index}"
            if obs_key in obs_cluster:
                continue
            if obs_key in skipped_step_keys:
                continue
            cluster_key = self._build_signature_cluster_key(obs)
            clusters.setdefault(cluster_key, []).append(obs)
            obs_cluster[obs_key] = cluster_key


        cluster_structures: Dict[str, Dict[str, Any]] = {k: {} for k in clusters}
        if self.logger:
            self.logger.log_info(
                "[SM-P3a] Skipped legacy visual layout/components pre-extraction "
                f"for {len(clusters)} General Page clusters; consolidation still "
                "runs and later materializes route/content/runtime structure fields"
            )


        if self.logger:
            self.logger.log_info(f"[SM-P3.5] Reference trace semantics for {len(clusters)} General Page clusters...")

        cluster_content_obs: Dict[str, Dict[str, Any]] = {}
        existing_instance_planning = self._load_existing_instance_planning(output_dir or "")
        reference_trace_semantics = await self._extract_reference_trace_semantics(
            clusters=clusters,
            traces=traces,
            page_info_map=page_info_map,
            output_dir=output_dir or "",
        )
        runtime_state_observations = self._build_runtime_state_observations(
            clusters=clusters,
            traces=traces,
            page_info_map=page_info_map,
            semantic_artifact=reference_trace_semantics,
        )
        instance_split_state_authority_by_obs_key = (
            self._instance_split_state_authority_by_obs_key(
                reference_trace_semantics,
            )
        )
        content_lifecycle_plan = runtime_state_observations.get("derived", {}).get("content_lifecycle_plan", {})
        if output_dir:
            self._save_debug_global_reasoning_stage(
                "reference_trace_semantics.json",
                reference_trace_semantics,
                output_dir,
            )
            self._save_debug_global_reasoning_stage(
                "runtime_state_observations.json",
                runtime_state_observations,
                output_dir,
            )
            self._save_debug_global_reasoning_stage(
                "content_lifecycle_plan.json",
                content_lifecycle_plan,
                output_dir,
            )

        existing_instance_split_producer_candidates = {
            cluster_key: candidate
            for cluster_key in clusters
            if isinstance(
                candidate := self._load_existing_instance_split_producer_candidate(
                    output_dir or "",
                    cluster_key,
                ),
                dict,
            )
        }
        instance_split_audit_repair_constraints = (
            self._load_instance_split_audit_repair_constraints(
                output_dir or "",
                clusters,
            )
        )
        producer_boundary_preservation_constraints = {
            cluster_key: constraint
            for cluster_key, candidate in (
                existing_instance_split_producer_candidates.items()
            )
            if (
                constraint
                := self._derive_instance_split_boundary_preservation_constraint(
                    candidate,
                    clusters[cluster_key],
                )
            )
        }
        boundary_preservation_constraints = {
            **producer_boundary_preservation_constraints,
            **instance_split_audit_repair_constraints,
        }
        (
            reusable_instance_planning,
            rejected_instance_planning,
        ) = self._admit_reusable_instance_planning(
            existing_instance_planning,
            clusters,
            state_authority_by_obs_key=(
                instance_split_state_authority_by_obs_key
            ),
            traces=traces,
            producer_candidates_by_cluster=(
                existing_instance_split_producer_candidates
            ),
            boundary_preservation_constraints_by_cluster=(
                boundary_preservation_constraints
            ),
        )
        self._save_instance_plan_seed_admission(
            output_dir or "",
            reusable_instance_planning,
            rejected_instance_planning,
            clusters,
        )
        if rejected_instance_planning and self.logger:
            self.logger.log_warning(
                "[SM-P4] Rejected stale instance-plan seed clusters under current contracts: "
                + ", ".join(sorted(rejected_instance_planning))
            )
        missing_instance_clusters = {
            cluster_key: cluster_obs
            for cluster_key, cluster_obs in clusters.items()
            if cluster_key not in reusable_instance_planning
        }
        if reusable_instance_planning and not missing_instance_clusters:
            instance_planning_debug = reusable_instance_planning
            if self.logger:
                self.logger.log_info(
                    f"[SM-P4] Reusing complete formal instance split plan: "
                    f"{len(instance_planning_debug)} clusters"
                )
        else:
            if reusable_instance_planning and self.logger:
                self.logger.log_info(
                    "[SM-P4] Resuming partial formal instance split plan: "
                    f"reused {len(reusable_instance_planning)} clusters, "
                    f"producing {len(missing_instance_clusters)} missing clusters"
                )
            instance_planning_debug = await self._build_instance_splitting_phase(
                clusters=missing_instance_clusters or clusters,
                traces=traces,
                page_info_map=page_info_map,
                content_lifecycle_plan=content_lifecycle_plan,
                reference_trace_semantics=reference_trace_semantics,
                global_reasoning=global_reasoning,
                output_dir=output_dir or "",
                initial_instance_planning=reusable_instance_planning,
                all_clusters=clusters,
                state_authority_by_obs_key=(
                    instance_split_state_authority_by_obs_key
                ),
                initial_formal_validation_errors_by_cluster=(
                    rejected_instance_planning
                ),
                boundary_preservation_constraints_by_cluster=(
                    boundary_preservation_constraints
                ),
            )
        self._require_complete_instance_plan_for_content_extraction(instance_planning_debug, clusters)
        if self.stop_after_instance_splitting:
            raise InstanceSplittingStopAfterReached()

        selected_instance_compile_binding = (
            self._load_selected_instance_compile_binding(
                instance_planning_debug,
                clusters=clusters,
            )
        )
        content_planning_clusters = clusters
        if selected_instance_compile_binding:
            selected_cluster_ids = set(
                selected_instance_compile_binding["selected_cluster_ids"]
            )
            content_planning_clusters = {
                cluster_id: cluster_obs
                for cluster_id, cluster_obs in clusters.items()
                if cluster_id in selected_cluster_ids
            }
            if set(content_planning_clusters) != selected_cluster_ids:
                raise ValueError(
                    "selected-instance compile cluster denominator is not present in current observations"
                )


        if self.logger:
            self.logger.log_info(
                "[SM-P5] Content extraction for "
                f"{len(content_planning_clusters)} Materialized Page clusters..."
            )

        def _merge_instance_content(accumulator: Dict[str, Any], job: Dict[str, Any], group_content: Dict[str, Any], content_file: str) -> None:
            group = job.get("instance_group", {})
            accumulator["instance_candidates"].append({
                "instance_order": job.get("instance_order", 0),
                "instance_key": group.get("instance_key", ""),
                "instance_label": group.get("instance_label", ""),
                "obs_keys": group.get("obs_keys", []),
                "content_file": content_file,
                "content_data": group_content,
            })
            for entity_name, items in (group_content.get("extracted_items", {}) if isinstance(group_content, dict) else {}).items():
                if entity_name == "_metadata":
                    continue
                if isinstance(items, list):
                    accumulator["merged_extracted_items"].setdefault(entity_name, []).extend(items)
            for mapping in group_content.get("entity_component_mapping", []) if isinstance(group_content, dict) else []:
                if isinstance(mapping, dict) and mapping not in accumulator["merged_mapping"]:
                    accumulator["merged_mapping"].append(mapping)
            relevance = group_content.get("entity_task_relevance", {}) if isinstance(group_content, dict) else {}
            if isinstance(relevance, dict):
                for entity_name, rel in relevance.items():
                    if not isinstance(rel, dict):
                        continue
                    current = accumulator["merged_relevance"].setdefault(entity_name, dict(rel))
                    current["task_related"] = bool(current.get("task_related")) or bool(rel.get("task_related"))
                    for key in ("evidence_steps", "task_instruction_refs"):
                        values = current.setdefault(key, [])
                        source_values = rel.get(key, [])
                        if not isinstance(source_values, list):
                            continue
                        for item in source_values:
                            if item not in values:
                                values.append(item)
            runtime_items = group_content.get("runtime_items", {}) if isinstance(group_content, dict) else {}
            if isinstance(runtime_items, dict):
                for entity_name, items in runtime_items.items():
                    if isinstance(items, list):
                        accumulator["merged_runtime_items"].setdefault(entity_name, []).extend(items)
            for key in ("excluded_from_initial", "item_lifecycle"):
                values = accumulator.setdefault(f"merged_{key}", [])
                for item in group_content.get(key, []) if isinstance(group_content, dict) else []:
                    if isinstance(item, dict) and item not in values:
                        values.append(item)

        def _finalize_instance_cluster(cluster_key: str, accumulator: Dict[str, Any]) -> Dict[str, Any]:
            instance_candidates = sorted(
                accumulator["instance_candidates"],
                key=lambda candidate: int(candidate.get("instance_order", 0)),
            )
            for candidate in instance_candidates:
                candidate.pop("instance_order", None)
            return {
                "extracted_items": accumulator["merged_extracted_items"],
                "entity_component_mapping": accumulator["merged_mapping"],
                "entity_task_relevance": accumulator["merged_relevance"],
                "runtime_items": accumulator.get("merged_runtime_items", {}),
                "excluded_from_initial": accumulator.get("merged_excluded_from_initial", []),
                "item_lifecycle": accumulator.get("merged_item_lifecycle", []),
                "instance_content_candidates": instance_candidates,
                "cluster_id": cluster_key,
                "merged_from_count": accumulator["source_cluster_count"],
            }

        def _record_instance_content_summaries(cluster_key: str, content: Dict[str, Any]) -> None:


            return

        cached_content_clusters: set[str] = set()

        def _save_completed_cluster(cluster_key: str, content: Dict[str, Any]) -> None:
            cluster_content_obs[cluster_key] = content
            _record_instance_content_summaries(cluster_key, content)
            if output_dir:
                instance_plan_entry = instance_planning_debug.get(cluster_key, {})
                self._save_debug_content_extraction_cluster(
                    output_dir,
                    cluster_key,
                    content,
                    instance_plan_entry,
                    instance_plan_entry_fingerprint=self._instance_plan_entry_fingerprint(instance_plan_entry),
                )

        async def _plan_content_extraction_jobs(cluster_key: str, cluster_obs: List[FrameObservation]) -> List[Dict[str, Any]]:
            planning_entry = instance_planning_debug.get(cluster_key, {})
            has_existing_planning = isinstance(planning_entry, dict) and bool(planning_entry)
            expected_plan_fingerprint = (
                self._instance_plan_entry_fingerprint(planning_entry)
                if has_existing_planning
                else None
            )
            if output_dir:
                resumed = self._load_debug_content_extraction_cluster(
                    output_dir,
                    cluster_key,
                    expected_instance_plan_entry_fingerprint=expected_plan_fingerprint,
                    require_instance_plan_fingerprint=True,
                )
                if resumed:
                    cluster_content_obs[cluster_key] = resumed.get("content", {})
                    cached_content_clusters.add(cluster_key)
                    instance_planning_debug[cluster_key] = planning_entry
                    if self.logger:
                        self.logger.log_info(f"[SM-P5] Reused content extraction for {cluster_key}")
                    return []

            page_meta = page_info_map.get(cluster_key, {})
            page_context = {
                "page_cluster_id": cluster_key,
                "page_name": str(page_meta.get("page_name", "")).strip(),
                "page_type_hint": str(page_meta.get("page_type_hint", "")).strip(),
                "description": str(page_meta.get("description", "")).strip(),
                "route_path": str(page_meta.get("route_path", "")).strip(),
            }
            lifecycle_context = self._build_content_lifecycle_context(
                cluster_key,
                cluster_obs,
                content_lifecycle_plan,
            )

            instance_groups = self._build_instance_groups_from_planning(
                cluster_key,
                cluster_obs,
                planning_entry,
            )
            if instance_groups:
                return [
                    {
                        "job_type": "instance",
                        "cluster_key": cluster_key,
                        "instance_order": instance_order,
                        "instance_group": group,
                        "page_context": page_context,
                        "cluster_obs": group.get("cluster_obs", []),
                        "lifecycle_context": self._build_content_lifecycle_context(
                            cluster_key,
                            group.get("cluster_obs", []),
                            content_lifecycle_plan,
                        ),
                        "source_cluster_count": len(cluster_obs),
                    }
                    for instance_order, group in enumerate(instance_groups)
                ]

            return [{
                "job_type": "cluster",
                "cluster_key": cluster_key,
                "page_context": page_context,
                "cluster_obs": cluster_obs,
                "lifecycle_context": lifecycle_context,
                "source_cluster_count": len(cluster_obs),
            }]

        planned_job_batches = await asyncio.gather(*[
            _plan_content_extraction_jobs(ck, co)
            for ck, co in content_planning_clusters.items()
        ])
        content_jobs = [
            job
            for batch in planned_job_batches
            for job in batch
        ]
        content_jobs = self._filter_selected_instance_content_jobs(
            content_jobs,
            selected_instance_compile_binding,
            completed_cluster_ids=cached_content_clusters,
        )
        if self.logger:
            instance_job_count = sum(1 for job in content_jobs if job["job_type"] == "instance")
            cluster_job_count = sum(1 for job in content_jobs if job["job_type"] == "cluster")
            self.logger.log_info(
                self._format_content_extraction_planning_log(
                    total_clusters=len(content_planning_clusters),
                    cached_clusters=len(cached_content_clusters),
                    planned_jobs=len(content_jobs),
                    instance_jobs=instance_job_count,
                    cluster_jobs=cluster_job_count,
                )
            )

        task_actions_by_cluster = self._build_task_actions_by_cluster(
            global_reasoning, content_planning_clusters, state_observations,
        )
        page_label_groups = self._build_screenshot_render_classification_groups(
            clusters=content_planning_clusters,
            content_jobs=content_jobs,
            instance_planning_debug=instance_planning_debug,
            page_info_map=page_info_map,
            task_actions_by_cluster=task_actions_by_cluster,
        )


        (
            _unused_gr,
            structure_unique_keys,
            cluster_label_debug,
            render_role_by_step,
            initial_authority_kind_by_step,
            screenshot_lifecycle_by_step,
        ) = (
            await self._apply_screenshot_render_classification_to_materialized_page_groups(
                global_reasoning,
                state_observations,
                page_label_groups,
                output_dir or "",
            )
        )
        runtime_state_observations.setdefault("derived", {})[
            "materialized_semantic_view"
        ] = self._build_materialized_semantic_view(
            runtime_state_observations,
            global_reasoning,
        )
        runtime_state_observations.setdefault("derived", {})[
            "runtime_state_obligations"
        ] = self._build_runtime_state_obligations(runtime_state_observations)
        if output_dir:
            self._save_debug_global_reasoning_stage(
                "screenshot_render_classification.json",
                cluster_label_debug,
                output_dir,
            )
            self._save_debug_global_reasoning_stage(
                "runtime_state_observations.json",
                runtime_state_observations,
                output_dir,
            )
            self._save_debug_global_reasoning_stage(
                "runtime_state_obligations.json",
                runtime_state_observations.get("derived", {}).get("runtime_state_obligations", {}),
                output_dir,
            )

        content_jobs = self._filter_l0_content_extraction_jobs(
            content_jobs,
            structure_unique_keys=structure_unique_keys,
            render_role_by_step=render_role_by_step,
            output_dir=output_dir or "",
        )

        pending_instance_counts: Dict[str, int] = {}
        instance_accumulators: Dict[str, Dict[str, Any]] = {}
        for job in content_jobs:
            if job["job_type"] != "instance":
                continue
            cluster_key = job["cluster_key"]
            pending_instance_counts[cluster_key] = pending_instance_counts.get(cluster_key, 0) + 1
            instance_accumulators.setdefault(cluster_key, {
                "source_cluster_count": job.get("source_cluster_count", 0),
                "instance_candidates": [],
                "merged_extracted_items": {"_metadata": {}},
                "merged_runtime_items": {},
                "merged_excluded_from_initial": [],
                "merged_item_lifecycle": [],
                "merged_mapping": [],
                "merged_relevance": {},
            })

        def _content_parent_job_key(job: Dict[str, Any]) -> str:
            if job.get("job_type") == "instance":
                return f"{job.get('cluster_key', '')}:instance:{int(job.get('instance_order', 0))}"
            return f"{job.get('cluster_key', '')}:cluster"

        completed_parent_jobs: set[str] = set()
        content_jobs_for_calls: List[Dict[str, Any]] = []
        for job in content_jobs:
            if job.get("job_type") != "instance" or not output_dir:
                content_jobs_for_calls.append(job)
                continue
            cluster_key = job["cluster_key"]
            group = job.get("instance_group", {})
            loaded_instance = self._load_debug_content_extraction_instance(
                output_dir,
                cluster_key,
                str(group.get("instance_key", "") or ""),
                int(job.get("instance_order", 0)),
            )
            if not loaded_instance:
                content_jobs_for_calls.append(job)
                continue

            content = self._apply_content_lifecycle_filters(
                loaded_instance.get("content", {}),
                job.get("lifecycle_context", {}),
            )
            _merge_instance_content(
                instance_accumulators[cluster_key],
                job,
                content,
                str(loaded_instance.get("content_file", "") or ""),
            )
            parent_key = _content_parent_job_key(job)
            completed_parent_jobs.add(parent_key)
            pending_instance_counts[cluster_key] -= 1
            if self.logger:
                self.logger.log_info(
                    f"[SM-P5] Reused content extraction instance {parent_key}"
                )
            if pending_instance_counts[cluster_key] == 0:
                _save_completed_cluster(
                    cluster_key,
                    _finalize_instance_cluster(cluster_key, instance_accumulators[cluster_key]),
                )

        content_jobs = content_jobs_for_calls

        content_call_jobs: List[Dict[str, Any]] = []
        for job in content_jobs:
            chunks = self._chunk_content_extraction_observations(job.get("cluster_obs", []))
            if not chunks:
                chunks = [[]]
            parent_job_key = _content_parent_job_key(job)
            chunk_count = len(chunks)
            for chunk_order, chunk_obs in enumerate(chunks):
                call_job = dict(job)
                call_job["parent_job_key"] = parent_job_key
                call_job["chunk_order"] = chunk_order
                call_job["chunk_count"] = chunk_count
                call_job["cluster_obs"] = chunk_obs
                content_call_jobs.append(call_job)

        if self.logger:
            split_job_count = sum(1 for job in content_jobs if len(self._chunk_content_extraction_observations(job.get("cluster_obs", []))) > 1)
            self.logger.log_info(
                f"[SM-P5] Expanded content extraction into {len(content_call_jobs)} calls "
                f"with max {self.CONTENT_EXTRACTION_MAX_SCREENSHOTS} screenshots per call "
                f"({split_job_count} logical jobs split)"
            )

        parent_jobs: Dict[str, Dict[str, Any]] = {}
        pending_chunk_counts: Dict[str, int] = {}
        chunk_payloads: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
        failed_content_jobs: List[Dict[str, Any]] = []
        completed_call_jobs = 0
        successful_call_jobs: set[str] = set()
        for call_job in content_call_jobs:
            parent_key = call_job["parent_job_key"]
            parent_jobs.setdefault(parent_key, {
                key: value
                for key, value in call_job.items()
                if key not in {"chunk_order", "chunk_count", "parent_job_key"}
            })
            pending_chunk_counts[parent_key] = int(call_job.get("chunk_count", 1))
            chunk_payloads.setdefault(parent_key, [])

        async def _run_content_extraction_job(job: Dict[str, Any]):
            content = await self._extract_cluster_content(
                job.get("cluster_obs", []),
                job["page_context"],
                structure_data=cluster_structures.get(job["cluster_key"], {}),
                task_trace_context=self._build_cluster_task_trace_context(
                    job.get("cluster_obs", []),
                    traces,
                ),
                lifecycle_context=job.get("lifecycle_context", {}),
            )
            content = content if isinstance(content, dict) else {}
            content = self._apply_content_lifecycle_filters(
                content,
                job.get("lifecycle_context", {}),
            )
            return job, content

        async def _run_content_extraction_job_checked(job: Dict[str, Any]):
            try:
                result_job, content = await _run_content_extraction_job(job)
                return result_job, content, None
            except Exception as exc:
                return job, {}, exc

        async for job, content, error, attempt in self._iter_content_extraction_call_results_with_retries(
            content_call_jobs,
            _run_content_extraction_job_checked,
        ):
            parent_key = job.get("parent_job_key", _content_parent_job_key(job))
            call_job_key = self._content_call_job_key(job)
            if error is not None:
                failed_content_jobs.append({
                    "parent_job_key": parent_key,
                    "cluster_key": job.get("cluster_key", ""),
                    "job_type": job.get("job_type", ""),
                    "chunk_order": int(job.get("chunk_order", 0)),
                    "chunk_count": int(job.get("chunk_count", 1)),
                    "attempt_count": int(attempt),
                    "error": str(error),
                })
                continue
            successful_call_jobs.add(call_job_key)
            completed_call_jobs = len(successful_call_jobs)
            chunk_payloads.setdefault(parent_key, []).append((int(job.get("chunk_order", 0)), content))
            pending_chunk_counts[parent_key] = pending_chunk_counts.get(parent_key, 1) - 1
            if pending_chunk_counts[parent_key] > 0:
                continue

            parent_job = parent_jobs.get(parent_key, job)
            content = await self._reduce_content_extraction_payloads(
                [
                    payload
                    for _chunk_order, payload in sorted(
                        chunk_payloads.get(parent_key, []),
                        key=lambda item: item[0],
                    )
                ],
                parent_job.get("page_context", {}),
                parent_job_key=parent_key,
                lifecycle_context=parent_job.get("lifecycle_context", {}),
            )
            content = self._apply_content_lifecycle_filters(
                content,
                parent_job.get("lifecycle_context", {}),
            )
            job = parent_job
            cluster_key = job["cluster_key"]
            if job["job_type"] == "cluster":
                _save_completed_cluster(cluster_key, content)
                completed_parent_jobs.add(parent_key)
                continue

            group = job.get("instance_group", {})
            content_file = ""
            if output_dir:
                content_file = self._save_debug_content_extraction_instance(
                    output_dir=output_dir,
                    cluster_key=cluster_key,
                    instance_key=group.get("instance_key", ""),
                    instance_label=group.get("instance_label", ""),
                    trace_ids=group.get("trace_ids", []),
                    obs_keys=group.get("obs_keys", []),
                    content=content,
                    instance_order=int(job.get("instance_order", 0)),
                )
            _merge_instance_content(
                instance_accumulators[cluster_key],
                job,
                content,
                content_file,
            )
            pending_instance_counts[cluster_key] -= 1
            completed_parent_jobs.add(parent_key)
            if pending_instance_counts[cluster_key] == 0:
                _save_completed_cluster(
                    cluster_key,
                    _finalize_instance_cluster(cluster_key, instance_accumulators[cluster_key]),
                )

        if self.logger:
            populated = sum(1 for c in cluster_content_obs.values() if c)
            self.logger.log_info(
                f"[SM-P5] Done: {populated}/{len(content_planning_clusters)} "
                "selected clusters have content"
            )

        expected_parent_jobs = set(parent_jobs) | set(completed_parent_jobs)
        try:
            self._validate_content_extraction_phase_complete(
                expected_clusters=set(content_planning_clusters),
                cluster_content_obs=cluster_content_obs,
                failed_jobs=failed_content_jobs,
                expected_parent_jobs=expected_parent_jobs,
                completed_parent_jobs=completed_parent_jobs,
                expected_call_jobs=len(content_call_jobs),
                completed_call_jobs=completed_call_jobs,
            )
        except ContentExtractionPhaseError as exc:
            if output_dir:
                failure_report = {
                    "format": "content_extraction_failure_report",
                    "status": "failed",
                    "expected_clusters": len(content_planning_clusters),
                    "completed_clusters": len(cluster_content_obs),
                    "expected_parent_jobs": len(expected_parent_jobs),
                    "completed_parent_jobs": len(completed_parent_jobs),
                    "expected_call_jobs": len(content_call_jobs),
                    "completed_call_jobs": completed_call_jobs,
                    "failed_jobs": failed_content_jobs,
                    "error": str(exc),
                }
                self.pom.write_json(
                    "content_extraction",
                    "debug",
                    "content_extraction_failure_report.json",
                    failure_report,
                    artifact_id="content_extraction.failure_report",
                    artifact_kind="validation_report",
                    producer="AppSpecBuilder.content_extraction",
                )
            raise


        if output_dir:
            self._save_debug_page_structures(cluster_structures, output_dir)
            self._save_debug_content_extraction_index(output_dir, cluster_content_obs, len(clusters))
            self._save_debug_schema_context_content_obs(
                output_dir,
                cluster_content_obs,
                instance_planning_debug,
            )
            self._write_phase_manifest(
                output_dir,
                "content_extraction",
                "manifest.json",
                inputs={
                    "instance_plan_hash": self._stable_json_fingerprint(instance_planning_debug),
                    "screenshot_render_classification_hash": self._stable_json_fingerprint(cluster_label_debug),
                    "content_lifecycle_plan_hash": self._stable_json_fingerprint(content_lifecycle_plan),
                },
                outputs={
                    "extracted_content_hash": self._stable_json_fingerprint(cluster_content_obs),
                    "cluster_count": len(cluster_content_obs),
                    "expected_cluster_count": len(content_planning_clusters),
                    "expected_parent_job_count": len(expected_parent_jobs),
                    "completed_parent_job_count": len(completed_parent_jobs),
                    "expected_call_job_count": len(content_call_jobs),
                    "completed_call_job_count": completed_call_jobs,
                    "status": "complete",
                },
                producer="AppSpecBuilder.content_extraction",
            )
            if selected_instance_compile_binding:
                self.pom.write_json(
                    "content_extraction",
                    "debug",
                    "selected_instance_compile_receipt.json",
                    {
                        "format": "ghostweb.selected-instance-compile-receipt.v1",
                        "status": "content_extraction_complete",
                        "manifest_path": selected_instance_compile_binding[
                            "manifest_path"
                        ],
                        "manifest_sha256": selected_instance_compile_binding[
                            "manifest_sha256"
                        ],
                        "instance_plan_path": selected_instance_compile_binding[
                            "instance_plan_path"
                        ],
                        "instance_plan_sha256": selected_instance_compile_binding[
                            "instance_plan_sha256"
                        ],
                        "full_cluster_count": selected_instance_compile_binding[
                            "full_cluster_count"
                        ],
                        "full_instance_count": selected_instance_compile_binding[
                            "full_instance_count"
                        ],
                        "selected_instance_count": selected_instance_compile_binding[
                            "selected_instance_count"
                        ],
                        "selected_observation_count": len(
                            selected_instance_compile_binding["selected_obs_keys"]
                        ),
                        "preserved_instance_count": selected_instance_compile_binding[
                            "preserved_instance_count"
                        ],
                        "selected_cluster_default_count": (
                            selected_instance_compile_binding[
                                "selected_cluster_default_count"
                            ]
                        ),
                        "preserved_cluster_default_count": (
                            selected_instance_compile_binding[
                                "preserved_cluster_default_count"
                            ]
                        ),
                        "selected_instances": [
                            {"cluster_id": cluster_id, "instance_key": instance_key}
                            for cluster_id, instance_key in sorted(
                                selected_instance_compile_binding["selected_pairs"]
                            )
                        ],
                        "completed_cluster_ids": sorted(cluster_content_obs),
                        "failure_policy": "hard_fail",
                        "unselected_members_mutated": False,
                    },
                    artifact_id="content_extraction.selected_instance_compile_receipt",
                    artifact_kind="validation_report",
                    producer="AppSpecBuilder.content_extraction",
                )

        if (
            selected_instance_compile_binding
            and selected_instance_compile_binding.get("stop_after")
            == "content_extraction"
        ):
            raise SelectedInstanceCompileStopAfterReached()


        consolidation_observations = state_observations
        consolidation_traces = traces
        consolidation_global_reasoning = global_reasoning
        consolidation_cluster_structures = cluster_structures
        consolidation_structure_unique_keys = structure_unique_keys
        consolidation_render_role_by_step = render_role_by_step
        consolidation_initial_authority_kind_by_step = initial_authority_kind_by_step
        consolidation_screenshot_lifecycle_by_step = screenshot_lifecycle_by_step
        if selected_instance_compile_binding:
            selected_obs_keys = set(
                selected_instance_compile_binding["selected_obs_keys"]
            )


            selected_obs_keys_canonical = {
                canonical_step_key(key)
                for key in selected_obs_keys
                if canonical_step_key(key)
            }
            selected_trace_ids = {
                str(obs.trace_id)
                for obs in state_observations
                if self._obs_key(obs) in selected_obs_keys
            }
            consolidation_observations = [
                obs
                for obs in state_observations
                if self._obs_key(obs) in selected_obs_keys
            ]
            consolidation_traces = [
                TraceData(
                    trace_id=trace.trace_id,
                    task=trace.task,
                    website_url=trace.website_url,
                    level=trace.level,
                    steps=[
                        step
                        for step in trace.steps
                        if canonical_step_key(
                            f"{trace.trace_id}:{int(step.step_index)}"
                        ) in selected_obs_keys_canonical
                    ],
                    skipped_steps=[],
                    step_dispositions=[
                        row
                        for row in trace.step_dispositions
                        if canonical_step_key(
                            f"{trace.trace_id}:{int(row.get('step_index', -1))}"
                        ) in selected_obs_keys_canonical
                    ],
                    terminal_observations=[],
                    terminal_contract={},
                )
                for trace in traces
                if trace.trace_id in selected_trace_ids
            ]
            consolidation_global_reasoning = deepcopy(global_reasoning)
            consolidation_global_reasoning["step_labels"] = [
                row
                for row in global_reasoning.get("step_labels", []) or []
                if isinstance(row, dict)
                and canonical_step_key(
                    f"{row.get('trace_id', '')}:{int(row.get('step_index', -1))}"
                ) in selected_obs_keys_canonical
            ]
            consolidation_global_reasoning["canonical_pages"] = [
                row
                for row in global_reasoning.get("canonical_pages", []) or []
                if isinstance(row, dict)
                and str(row.get("page_cluster_id", "") or "")
                in selected_instance_compile_binding["selected_cluster_ids"]
            ]
            consolidation_global_reasoning["canonical_routes"] = [
                row
                for row in global_reasoning.get("canonical_routes", []) or []
                if isinstance(row, dict)
                and str(row.get("from_page_cluster_id", "") or "")
                in selected_instance_compile_binding["selected_cluster_ids"]
                and str(row.get("to_page_cluster_id", "") or "")
                in selected_instance_compile_binding["selected_cluster_ids"]
            ]
            consolidation_cluster_structures = {
                cluster_id: value
                for cluster_id, value in cluster_structures.items()
                if cluster_id
                in selected_instance_compile_binding["selected_cluster_ids"]
            }
            consolidation_structure_unique_keys = {
                key
                for key in structure_unique_keys
                if canonical_step_key(key) in selected_obs_keys_canonical
            }
            consolidation_render_role_by_step = {
                key: value
                for key, value in render_role_by_step.items()
                if canonical_step_key(key) in selected_obs_keys_canonical
            }
            consolidation_initial_authority_kind_by_step = {
                key: value
                for key, value in initial_authority_kind_by_step.items()
                if canonical_step_key(key) in selected_obs_keys_canonical
            }
            consolidation_screenshot_lifecycle_by_step = {
                key: value
                for key, value in screenshot_lifecycle_by_step.items()
                if canonical_step_key(key) in selected_obs_keys_canonical
            }
        site_model = await self._consolidate_observations_v3(
            consolidation_observations, consolidation_traces, website_url,
            global_reasoning=consolidation_global_reasoning,
            loaded_route_tree=loaded_route_tree,
            loaded_site_graph=loaded_site_graph,
            output_dir=output_dir,
            cluster_structures=consolidation_cluster_structures,
            cluster_content_obs=cluster_content_obs,
            structure_unique_keys=consolidation_structure_unique_keys,
            render_role_by_step=consolidation_render_role_by_step,
            initial_authority_kind_by_step=consolidation_initial_authority_kind_by_step,
            screenshot_lifecycle_by_step=consolidation_screenshot_lifecycle_by_step,
            render_role_reasons=self._extract_render_role_reasons(cluster_label_debug),
            runtime_state_observations=runtime_state_observations,
            boundary_preservation_constraints_by_cluster=(
                boundary_preservation_constraints
            ),
        )

        if selected_instance_compile_binding and self.pom:
            self.pom.write_json(
                "consolidation",
                "debug",
                "selected_instance_compile_receipt.json",
                self._build_selected_instance_site_model_compile_receipt(
                    selected_instance_compile_binding,
                    site_model.structure.page_prototypes,
                ),
                artifact_id="consolidation.selected_instance_compile_receipt",
                artifact_kind="validation_report",
                producer="AppSpecBuilder.consolidation",
            )
        if self.logger:
            self.logger.log_info(
                "[SM-P5] Core AppSpec assembly complete; "
                "phase_consolidation finalizer outputs still pending"
            )


        site_model.cluster_assignments = {
            "obs_to_cluster": dict(sorted(obs_cluster.items())),
            "step_labels": global_reasoning.get("step_labels", []) if isinstance(global_reasoning, dict) else [],
        }
        site_model.runtime_state_observations = runtime_state_observations or {}
        site_model.content_lifecycle_plan = content_lifecycle_plan or {}
        site_model.instance_plan = instance_planning_debug or {}

        if output_dir:
            self._save_parallel_summary(
                self._build_summary(traces, jobs, state_observations, site_model), output_dir
            )

        if self.logger:
            self.logger.log_info(
                f"AppSpec V3 complete: {len(site_model.structure.page_prototypes)} pages, "
                f"{len(site_model.structure.site_graph.edges)} edges"
            )
        return site_model

    def _build_summary(self, traces, jobs, observations, site_model) -> Dict[str, Any]:
        return {
            "pipeline": "layered_progressive_v3_chained",
            "total_traces": len(traces),
            "total_jobs": len(jobs),
            "frame_observations": len(observations),
            "api_calls_estimate": len(jobs),
            "pages": len(site_model.structure.page_prototypes),
            "edges": len(site_model.structure.site_graph.edges)
        }
