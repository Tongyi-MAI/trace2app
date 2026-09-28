import os
import json
import asyncio
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from trace2world_pipeline.app_spec.model import FrameObservation
from trace2world_pipeline.app_spec.trace_loader import TraceData
from trace2world_pipeline.core.llm_caller import (
    encode_image,
    call_openai_api_json_async,
    estimate_llm_stage_parallelism,
)
from trace2world_pipeline.app_spec.reconcile.content_extraction_contract import normalize_content_extraction_payload
from trace2world_pipeline.app_spec.reconcile.data_models import FrameJob


class FrameAnalysisMixin:

    async def _extract_cluster_content(
        self,
        cluster_obs: List[FrameObservation],
        page_context: Dict[str, str],
        structure_data: Dict[str, Any] = None,
        task_trace_context: Dict[str, Any] = None,
        lifecycle_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        seen_paths: set = set()
        screenshot_paths: List[str] = []
        for obs in cluster_obs:
            if not obs.screenshot_path or obs.screenshot_path in seen_paths:
                continue
            seen_paths.add(obs.screenshot_path)
            screenshot_paths.append(obs.screenshot_path)

        if not screenshot_paths:
            return {}

        prompt = self._build_cluster_content_extraction_prompt(
            page_context,
            len(screenshot_paths),
            structure_data=structure_data,
            task_trace_context=task_trace_context,
            lifecycle_context=lifecycle_context,
        )

        try:
            content = [{"type": "input_text", "text": prompt}]
            for path in screenshot_paths:
                content.append({"type": "input_image", "image_url": encode_image(path)})
            messages = [{"role": "user", "content": content}]

            result, _usage = await call_openai_api_json_async(
                messages,
                max_tokens=4096,
                reasoning_effort=self.reasoning_effort,
                model=self.model,
                stage="cluster_content_extraction",
            )
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                raise RuntimeError(
                    "cluster_content_extraction returned non-dict payload "
                    f"for cluster {page_context.get('page_cluster_id', '?')}"
                )
            return normalize_content_extraction_payload(result)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(
                    f"Content extraction failed for cluster {page_context.get('page_cluster_id', '?')}: {e}"
                )
            raise

    async def _analyze_frames_parallel_v3(self, jobs: List[FrameJob]) -> List[FrameObservation]:
        if not jobs:
            return []


        first_pass_results: Dict[str, Tuple[FrameObservation, bool]] = {}

        async def _first_pass(job: FrameJob):
            obs, needs_detailed = await self._analyze_frame_first_pass(
                job,
                run_quality_assessment=self.frame_quality_refinement_enabled
            )
            first_pass_results[job.job_key] = (obs, needs_detailed)

        if self.logger:
            self.logger.log_info(f"SM-P1.1: First pass analysis for {len(jobs)} frames...")

        await asyncio.gather(*[_first_pass(job) for job in jobs])


        frames_needing_detail: List[str] = []
        if self.frame_quality_refinement_enabled:
            frames_needing_detail = [k for k, (_, needs) in first_pass_results.items() if needs]
        if self.logger:
            self.logger.log_info(f"SM-P1.1 done: {len(frames_needing_detail)}/{len(jobs)} frames need detailed analysis")


        if frames_needing_detail:
            async def _second_pass(job: FrameJob):
                if job.job_key not in frames_needing_detail:
                    return
                detailed_obs = await self._analyze_frame_detailed(job)

                first_pass_results[job.job_key] = (detailed_obs, True)

            if self.logger:
                self.logger.log_info(f"SM-P1.2: Detailed analysis for {len(frames_needing_detail)} key frames...")

            await asyncio.gather(*[_second_pass(job) for job in jobs if job.job_key in frames_needing_detail])
        elif self.logger:
            self.logger.log_info("SM-P1.2 skipped: refinement is disabled")


        results = [obs for obs, _ in first_pass_results.values()]
        results.sort(key=lambda x: (x.trace_id, x.step_index))

        if self.logger:
            self.logger.log_info(f"SM-P1 detail pass complete: {len(results)} frames analyzed")

        return results

    async def _analyze_state_descriptions_parallel(
        self,
        jobs: List[FrameJob],
        cache_map: Optional[Dict[str, Dict[str, Any]]] = None,
        output_dir: Optional[str] = None,
    ) -> List[FrameObservation]:
        if not jobs:
            return []
        results: Dict[str, FrameObservation] = {}
        cache_map = cache_map or {}

        for job in jobs:
            cached = cache_map.get(job.job_key)
            if cached:
                obs = self._frame_observation_from_cached_state(job, cached)
                results[job.job_key] = obs
                if output_dir:
                    self._save_debug_frame_observation(obs, output_dir)

        missing_jobs = [job for job in jobs if job.job_key not in results]
        max_parallel_default = max(1, int(getattr(self, "max_concurrent", 16) or 16))
        sample_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Estimate frame state description routing."},
                    {"type": "input_image", "image_url": "data:image/png;base64,"},
                ],
            }
        ]

        def _estimate_parallelism(remaining_count: int) -> int:
            if remaining_count <= 0:
                return 0
            estimated = estimate_llm_stage_parallelism(
                self.model,
                "frame_state_description",
                {"type": "json_object"},
                sample_messages,
                default=max_parallel_default,
            )
            return max(1, min(remaining_count, max_parallel_default, int(estimated)))

        async def _describe(job: FrameJob):
            obs = await self._describe_frame_state(job)
            results[job.job_key] = obs
            if output_dir:
                self._save_debug_frame_observation(obs, output_dir)

        if self.logger:
            initial_parallel = _estimate_parallelism(len(missing_jobs)) if missing_jobs else 0
            self.logger.log_info(
                f"SM-P1(state descriptions): cache hit {len(jobs)-len(missing_jobs)}/{len(jobs)}, "
                f"describing {len(missing_jobs)} missing frames with initial_producer_concurrency={initial_parallel}, "
                f"max_producer_concurrency={max_parallel_default}..."
            )
        if missing_jobs:
            remaining = list(missing_jobs)
            pending: Dict[asyncio.Task, FrameJob] = {}
            previous_parallel = None
            completed_count = 0

            def _log_parallelism(current_parallel: int) -> None:
                nonlocal previous_parallel
                if self.logger and current_parallel != previous_parallel:
                    self.logger.log_info(
                        "SM-P1(state descriptions): adaptive producer_concurrency={} "
                        "at completed={}/{} remaining={}".format(
                            current_parallel,
                            completed_count,
                            len(missing_jobs),
                            len(remaining),
                        )
                    )
                previous_parallel = current_parallel

            def _fill_window() -> None:
                current_parallel = _estimate_parallelism(len(remaining) + len(pending))
                _log_parallelism(current_parallel)
                while remaining and len(pending) < current_parallel:
                    job = remaining.pop(0)
                    pending[asyncio.create_task(_describe(job))] = job

            _fill_window()
            while pending:
                done, _ = await asyncio.wait(
                    pending.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    pending.pop(task, None)
                    try:
                        await task
                    except BaseException:
                        for pending_task in pending:
                            pending_task.cancel()
                        if pending:
                            await asyncio.gather(*pending.keys(), return_exceptions=True)
                        raise
                    completed_count += 1
                if remaining:
                    _fill_window()
        ordered = [results[k.job_key] for k in jobs if k.job_key in results]
        ordered.sort(key=lambda x: (x.trace_id, x.step_index))
        return ordered

    def _load_state_description_cache(
        self,
        cache_path: str,
        output_dir: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        path = str(cache_path or "").strip()
        if not path:
            if output_dir:
                pom = getattr(self, "pom", None)
                if pom:
                    phase_dir = str(pom.formal("frame_analysis", "frame_observations"))
                else:
                    phase_dir = os.path.join(
                        output_dir, "phase_frame_analysis", "frame_observations"
                    )
                if os.path.isdir(phase_dir):
                    path = phase_dir
                else:
                    return {}
            else:
                return {}
        if not os.path.exists(path):
            if self.logger:
                self.logger.log_warning(f"State description cache not found: {path}")
            return {}
        rows: List[Dict[str, Any]] = []
        try:
            if os.path.isdir(path):
                for item_path in sorted(os.listdir(path)):
                    if not item_path.endswith(".json"):
                        continue
                    payload = self._read_json_file_safely(os.path.join(path, item_path), {})
                    if isinstance(payload, dict):
                        rows.append(payload)
            elif path.endswith(".jsonl"):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        item = json.loads(line)
                        if isinstance(item, dict):
                            rows.append(item)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    rows = [x for x in payload if isinstance(x, dict)]
                elif isinstance(payload, dict):
                    if isinstance(payload.get("steps"), list):
                        rows = [x for x in payload["steps"] if isinstance(x, dict)]
                    elif isinstance(payload.get("records"), list):
                        rows = [x for x in payload["records"] if isinstance(x, dict)]
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"Failed loading state description cache {path}: {e}")
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        for item in rows:
            tid = str(item.get("trace_id", "")).strip()
            step = item.get("step_index", -1)
            try:
                step = int(step)
            except Exception:
                continue
            if not tid or step < 0:
                continue
            out[f"{tid}:{step}"] = item
        return out

    def _frame_observation_from_cached_state(self, job: FrameJob, item: Dict[str, Any]) -> FrameObservation:
        description = str(item.get("description", "") or "").strip()
        structure_signature = str(item.get("structure_signature", "")).strip()
        content_signature = str(item.get("content_signature", "")).strip()
        route_hints = item.get("route_hints", {})
        if not isinstance(route_hints, dict):
            route_hints = {}
        page_state = item.get("page_state", {}) if isinstance(item.get("page_state", {}), dict) else {}
        external_ui_regions = item.get("external_ui_regions", page_state.get("external_ui_regions", []))
        if not isinstance(external_ui_regions, list):
            external_ui_regions = []
        obs = FrameObservation(
            trace_id=job.trace_id,
            step_index=job.step_index,
            screenshot_path=job.screenshot_path,
            description=description,
            brand_identity={},
            layout_structure={},
            components=item.get("components", []) if isinstance(item.get("components", []), list) else [],
            extracted_items={},
            page_state=page_state,
            identified_resources={},
            confidence=float(item.get("confidence", 0.6)),
            observed_url="",
            url_source="unknown",
            normalized_url="",
            url_prefix="",
            url_confidence=0.0,
            fallback_cluster_id=hashlib.md5(
                f"{job.trace_id}:{job.step_index}:{description[:80]}".encode("utf-8")
            ).hexdigest()[:12],
            structure_signature=structure_signature,
            content_signature=content_signature,
            route_hints=route_hints,
            header_pattern=str(item.get("header_pattern", "") or "unknown").strip(),
            shell_section=str(item.get("shell_section", "") or "unknown").strip(),
            body_pattern=str(item.get("body_pattern", "") or "unknown").strip(),
            sticky_action_pattern=str(item.get("sticky_action_pattern", "") or "unknown").strip(),
            identity_confidence=float(item.get("identity_confidence", 0.0) or 0.0),
            transition_from_previous=item.get("transition_from_previous"),
            transition_kind=str(item.get("transition_kind", "") or "").strip(),
            transition_evidence=[
                str(x).strip()
                for x in item.get("transition_evidence", [])
                if isinstance(x, str) and str(x).strip()
            ] if isinstance(item.get("transition_evidence", []), list) else [],
            action=job.action,
            thought=job.thought,
        )
        obs.page_state["quality_assessed"] = False
        obs.page_state["needs_refinement"] = False
        obs.page_state["last_action"] = str(item.get("action", "") or job.action)
        obs.page_state["previous_action"] = str(item.get("previous_action", "") or job.previous_action)
        obs.page_state["transition_from_previous"] = item.get("transition_from_previous")
        obs.page_state["transition_kind"] = str(item.get("transition_kind", "") or "").strip()
        obs.page_state["transition_evidence"] = list(obs.transition_evidence)
        obs.page_state["external_ui_regions"] = [
            region for region in external_ui_regions if isinstance(region, dict)
        ]
        obs.page_state["observation_role"] = job.observation_role
        obs.page_state["action_disposition"] = job.action_disposition
        obs.page_state["observation_disposition"] = job.observation_disposition
        obs.page_state["semantic_effect"] = job.semantic_effect
        if not obs.structure_signature:
            obs.structure_signature = self._compute_structure_signature({}, obs.components)
        if not obs.content_signature:
            obs.content_signature = self._compute_content_signature({}, obs.page_state, obs.description)
        return obs

    def _read_json_file_safely(self, path: str, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


    async def _describe_frame_state(self, job: FrameJob) -> FrameObservation:
        prompt = self._build_state_only_description_prompt(job.action, job.thought, job.previous_action)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            result, _usage = await call_openai_api_json_async(
                messages,
                reasoning_effort=getattr(self, "frame_observation_reasoning_effort", self.reasoning_effort),
                stage="frame_state_description",
                model=self.model
            )
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                raise ValueError(
                    f"frame_state_description returned {type(result).__name__}, expected object"
                )

            description = str(result.get("description", "")).strip()
            body_pattern = str(result.get("body_pattern", "") or "unknown").strip()
            header_pattern = str(result.get("header_pattern", "") or "unknown").strip()
            shell_section = str(result.get("shell_section", "") or "unknown").strip()
            sticky_action_pattern = str(result.get("sticky_action_pattern", "") or "unknown").strip()
            try:
                identity_confidence = float(result.get("identity_confidence", 0.0))
            except Exception:
                identity_confidence = 0.0
            transition_from_previous = result.get("transition_from_previous")
            if not isinstance(transition_from_previous, bool):
                transition_from_previous = None
            transition_kind = str(result.get("transition_kind", "") or "").strip()
            transition_evidence = [
                str(item).strip()
                for item in result.get("transition_evidence", [])
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(result.get("transition_evidence", []), list) else []
            external_ui_regions = result.get("external_ui_regions", [])
            if not isinstance(external_ui_regions, list):
                external_ui_regions = []

            obs = FrameObservation(
                trace_id=job.trace_id,
                step_index=job.step_index,
                screenshot_path=job.screenshot_path,
                description=description,
                confidence=0.6,
                header_pattern=header_pattern,
                shell_section=shell_section,
                body_pattern=body_pattern,
                sticky_action_pattern=sticky_action_pattern,
                identity_confidence=identity_confidence,
                transition_from_previous=transition_from_previous,
                transition_kind=transition_kind,
                transition_evidence=transition_evidence,
                action=job.action,
                thought=job.thought,
            )
            obs.page_state["previous_action"] = job.previous_action
            obs.page_state["transition_from_previous"] = transition_from_previous
            obs.page_state["transition_kind"] = transition_kind
            obs.page_state["transition_evidence"] = transition_evidence
            obs.page_state["external_ui_regions"] = [
                region for region in external_ui_regions if isinstance(region, dict)
            ]
            obs.page_state["observation_role"] = job.observation_role
            obs.page_state["action_disposition"] = job.action_disposition
            obs.page_state["observation_disposition"] = job.observation_disposition
            obs.page_state["semantic_effect"] = job.semantic_effect
            interaction_surface_role = str(result.get("interaction_surface_role", "") or "none").strip()
            obs.page_state["interaction_surface_role"] = interaction_surface_role
            host_page_still_visible = result.get("host_page_still_visible")
            if isinstance(host_page_still_visible, bool):
                obs.page_state["host_page_still_visible"] = host_page_still_visible
            obs.page_state["host_page_identity"] = str(result.get("host_page_identity", "") or "").strip()
            obs.page_state["opened_surface_identity"] = str(result.get("opened_surface_identity", "") or "").strip()
            obs.page_state["standalone_page_evidence"] = [
                str(item).strip()
                for item in result.get("standalone_page_evidence", [])
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(result.get("standalone_page_evidence", []), list) else []
            obs.page_state["host_page_evidence"] = [
                str(item).strip()
                for item in result.get("host_page_evidence", [])
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(result.get("host_page_evidence", []), list) else []
            return obs
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"State-only frame describe failed for {job.job_key}: {e}")
            raise RuntimeError(
                f"State-only frame description failed for {job.job_key}; "
                "phase_frame_analysis cannot continue with fallback observations"
            ) from e

    async def _extract_page_structure(
        self,
        cluster_obs: List[FrameObservation],
        page_context: Dict[str, str],
        structure_unique_keys: Optional[set] = None,
    ) -> Dict[str, Any]:

        seen_paths = set()
        screenshot_paths = []
        for obs in cluster_obs:
            if not obs.screenshot_path or obs.screenshot_path in seen_paths:
                continue

            if structure_unique_keys is not None:
                obs_key = f"{obs.trace_id}:{obs.step_index}"
                if obs_key not in structure_unique_keys:
                    continue
            seen_paths.add(obs.screenshot_path)
            screenshot_paths.append(obs.screenshot_path)


        if not screenshot_paths:
            seen_paths.clear()
            for obs in cluster_obs:
                if obs.screenshot_path and obs.screenshot_path not in seen_paths:
                    seen_paths.add(obs.screenshot_path)
                    screenshot_paths.append(obs.screenshot_path)

        if not screenshot_paths:
            return {}

        first_path = screenshot_paths[0] if screenshot_paths else None

        async def _call_visual_layout_text():
            prompt = self._build_visual_layout_text_only_prompt(page_context, len(screenshot_paths), screenshot_path=first_path)
            content = [{"type": "input_text", "text": prompt}]
            for path in screenshot_paths:
                content.append({"type": "input_image", "image_url": encode_image(path)})
            messages = [{"role": "user", "content": content}]
            result, _usage = await call_openai_api_json_async(
                messages, max_tokens=2048,
                reasoning_effort=self.reasoning_effort, model=self.model,
                stage="structure_extraction_visual_layout"
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}

        async def _call_components_layout():
            prompt = self._build_components_layout_prompt(page_context, len(screenshot_paths), screenshot_path=first_path)
            content = [{"type": "input_text", "text": prompt}]
            for path in screenshot_paths:
                content.append({"type": "input_image", "image_url": encode_image(path)})
            messages = [{"role": "user", "content": content}]
            result, _usage = await call_openai_api_json_async(
                messages, max_tokens=4096,
                reasoning_effort=self.reasoning_effort, model=self.model,
                stage="structure_extraction_components"
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}

        async def _call_metadata():
            prompt = self._build_layout_metadata_prompt(page_context, len(screenshot_paths), screenshot_path=first_path)
            content = [{"type": "input_text", "text": prompt}]
            for path in screenshot_paths:
                content.append({"type": "input_image", "image_url": encode_image(path)})
            messages = [{"role": "user", "content": content}]
            result, _usage = await call_openai_api_json_async(
                messages, max_tokens=2048,
                reasoning_effort=self.reasoning_effort, model=self.model,
                stage="structure_extraction_metadata"
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}

        try:
            vlt_result, components_result, metadata_result = await asyncio.gather(
                _call_visual_layout_text(), _call_components_layout(), _call_metadata()
            )


            merged = {**metadata_result, **components_result, **vlt_result}
            return merged
        except Exception as e:
            if self.logger:
                self.logger.log_warning(
                    f"Structure extraction failed for cluster {page_context.get('page_cluster_id', '?')}: {e}"
                )
            return {}

    async def _analyze_frame_first_pass(self, job: FrameJob, page_context: Dict[str, str] = None, run_quality_assessment: bool = True) -> Tuple[FrameObservation, bool]:
        prompt = self._build_first_pass_prompt(job.action, job.thought, page_context=page_context, screenshot_path=job.screenshot_path)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            result, _usage = await call_openai_api_json_async(
                messages,
                reasoning_effort=self.reasoning_effort,
                stage="frame_first_pass",
                model=self.model
            )
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                return self._fallback_frame_observation(job), False


            if self.use_url_signals:
                url_meta = self._extract_url_meta(result, job.website_url, job)
            else:
                url_meta = {
                    "observed_url": "",
                    "url_source": "unknown",
                    "normalized_url": "",
                    "url_prefix": "",
                    "url_confidence": 0.0,
                    "fallback_cluster_id": hashlib.md5(
                        f"{job.trace_id}:{job.step_index}:{result.get('description', '')[:80]}".encode("utf-8")
                    ).hexdigest()[:12],
                }

            description = str(result.get("description", "") or "").strip()
            structure_signature = str(result.get("structure_signature", "") or "").strip()
            content_signature = str(result.get("content_signature", "") or "").strip()
            route_hints = result.get("route_hints", {})
            if not isinstance(route_hints, dict):
                route_hints = {}
            if "visible_navigation_cues" not in route_hints:
                route_hints["visible_navigation_cues"] = result.get("visible_navigation_cues", [])
            if "interaction_state" not in route_hints:
                route_hints["interaction_state"] = result.get("interaction_state", {})
            if not structure_signature:
                structure_signature = self._compute_structure_signature(
                    result.get("layout_structure", {}),
                    result.get("components", [])
                )
            if not content_signature:
                content_signature = self._compute_content_signature(
                    result.get("extracted_items", {}),
                    result.get("page_state", {}),
                    description
                )

            obs = FrameObservation(
                trace_id=job.trace_id,
                step_index=job.step_index,
                screenshot_path=job.screenshot_path,
                description=description,
                brand_identity=result.get("brand_identity", {}),
                layout_structure=result.get("layout_structure", {}),
                components=result.get("components", []),
                extracted_items=result.get("extracted_items", {}),
                page_state=result.get("page_state", {}),
                identified_resources=result.get("identified_resources", {}),
                confidence=float(result.get("confidence", 0.6)),
                observed_url=url_meta["observed_url"],
                url_source=url_meta["url_source"],
                normalized_url=url_meta["normalized_url"],
                url_prefix=url_meta["url_prefix"],
                url_confidence=url_meta["url_confidence"],
                fallback_cluster_id=url_meta["fallback_cluster_id"],
                structure_signature=structure_signature,
                content_signature=content_signature,
                route_hints=route_hints,
                header_pattern=str(result.get("header_pattern", "") or "unknown").strip(),
                shell_section=str(result.get("shell_section", "") or "unknown").strip(),
                body_pattern=str(result.get("body_pattern", "") or "unknown").strip(),
                sticky_action_pattern=str(result.get("sticky_action_pattern", "") or "unknown").strip(),
                identity_confidence=float(result.get("identity_confidence", 0.0) or 0.0),
                action=job.action,
                thought=job.thought,
            )


            if result.get("design_system"):
                obs.layout_structure["design_system"] = result["design_system"]
            if result.get("navigation_elements"):
                obs.layout_structure["navigation_elements"] = result["navigation_elements"]


            needs_detailed = False
            if run_quality_assessment:
                needs_detailed = await self._assess_extraction_quality(job, result)


            obs.page_state["quality_assessed"] = bool(run_quality_assessment)
            obs.page_state["needs_refinement"] = needs_detailed
            obs.page_state["last_action"] = job.action

            return obs, needs_detailed

        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"First pass failed for {job.job_key}: {e}")
            return self._fallback_frame_observation(job), False

    async def _assess_extraction_quality(self, job: FrameJob, extraction_result: Dict[str, Any]) -> bool:

        confidence = extraction_result.get("confidence", 0.6)
        if confidence >= 0.9:
            if self.logger:
                self.logger.log_debug(f"High confidence ({confidence}), skipping quality assessment for {job.job_key}")
            return False


        components = extraction_result.get("components", [])
        if len(components) <= 2:
            if self.logger:
                self.logger.log_debug(f"Simple page ({len(components)} components), skipping refinement for {job.job_key}")
            return False

        prompt = self._build_quality_assessment_prompt(extraction_result)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            assessment, _usage = await call_openai_api_json_async(
                messages,
                reasoning_effort="medium",
                stage="frame_quality_assessment",
                model=self.model
            )
            if isinstance(assessment, str):
                assessment = json.loads(assessment)
            if not isinstance(assessment, dict):
                return False

            needs_refinement = assessment.get("needs_refinement", False)
            quality_score = assessment.get("quality_score", 0.8)

            if self.logger:
                missing = assessment.get("missing_elements", [])
                focus = assessment.get("refinement_focus", "none")
                self.logger.log_debug(
                    f"Quality assessment for {job.job_key}: "
                    f"score={quality_score:.2f}, needs_refinement={needs_refinement}, "
                    f"focus={focus}, missing={len(missing)} elements"
                )

            return needs_refinement

        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"Quality assessment failed for {job.job_key}: {e}")
            return False

    def _extract_url_meta(self, result: Dict[str, Any], website_url: str, job: FrameJob) -> Dict[str, Any]:
        url_evidence = result.get("url_evidence", {})
        observed_url = ""
        url_source = "unknown"
        url_confidence = 0.0
        if isinstance(url_evidence, dict):
            observed_url = str(url_evidence.get("observed_url", "") or "").strip()
            url_source = str(url_evidence.get("url_source", "unknown") or "unknown").strip().lower()
            try:
                url_confidence = float(url_evidence.get("confidence", 0.0))
            except Exception:
                url_confidence = 0.0

        if not observed_url:
            observed_url = str(result.get("observed_url", "") or "").strip()
        if not observed_url or observed_url.lower() in {"unknown", "n/a", "none"}:
            observed_url = ""
            url_source = "unknown"

        normalized_url = self._normalize_observed_url(observed_url, website_url)
        url_prefix = self._extract_url_prefix(normalized_url)
        fallback_seed = f"{job.trace_id}:{job.step_index}:{result.get('description', '')[:80]}"
        fallback_cluster_id = hashlib.md5(fallback_seed.encode("utf-8")).hexdigest()[:12]
        return {
            "observed_url": observed_url,
            "url_source": url_source,
            "normalized_url": normalized_url,
            "url_prefix": url_prefix,
            "url_confidence": max(0.0, min(1.0, url_confidence)),
            "fallback_cluster_id": fallback_cluster_id,
        }

    def _normalize_observed_url(self, observed_url: str, website_url: str) -> str:
        if not observed_url:
            return ""
        parsed = urlparse(observed_url)
        if not parsed.scheme or not parsed.netloc:
            base = urlparse(website_url or "")
            if base.scheme and base.netloc:
                path = observed_url if observed_url.startswith("/") else f"/{observed_url}"
                parsed = urlparse(f"{base.scheme}://{base.netloc}{path}")
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, parsed.fragment))

    def _extract_url_prefix(self, normalized_url: str) -> str:
        if not normalized_url:
            return ""
        parsed = urlparse(normalized_url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))

    def _compute_structure_signature(self, layout_structure: Dict[str, Any], components: List[Dict[str, Any]]) -> str:
        comp_types = sorted({
            str(c.get("type", "")).strip().lower()
            for c in (components or [])
            if isinstance(c, dict) and str(c.get("type", "")).strip()
        })
        structure_key = {
            "overall_layout": str((layout_structure or {}).get("overall_layout", "")).strip().lower(),
            "grid_columns": str((layout_structure or {}).get("grid_columns", "")).strip().lower(),
            "component_types": comp_types[:30]
        }
        digest = hashlib.md5(
            json.dumps(structure_key, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"st_{digest}"

    def _compute_content_signature(
        self,
        extracted_items: Dict[str, Any],
        page_state: Dict[str, Any],
        description: str
    ) -> str:
        entity_names = sorted([
            str(name).strip().lower()
            for name in (extracted_items or {}).keys()
            if str(name).strip()
        ])[:20]
        query = ""
        active_tab = ""
        if isinstance(page_state, dict):
            query = str(page_state.get("current_query", "")).strip().lower()
            active_tab = str(page_state.get("active_tab", "")).strip().lower()
        key = {
            "entities": entity_names,
            "current_query": query,
            "active_tab": active_tab,
            "description": str(description or "")[:200].strip().lower()
        }
        digest = hashlib.md5(
            json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"ct_{digest}"

    async def _analyze_frame_detailed(self, job: FrameJob) -> FrameObservation:
        layout_task = self._extract_layout(job)
        style_task = self._extract_style(job)
        content_task = self._extract_content(job)

        layout_result, style_result, content_result = await asyncio.gather(
            layout_task, style_task, content_task
        )

        return self._merge_detailed_results(job, layout_result, style_result, content_result)

    async def _extract_layout(self, job: FrameJob) -> Dict[str, Any]:
        prompt = self._build_layout_extraction_prompt(job.action, job.thought, screenshot_path=job.screenshot_path)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            result, _ = await call_openai_api_json_async(
                messages,
                reasoning_effort=self.reasoning_effort,
                stage="frame_detailed_layout",
                model=self.model
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"Layout extraction failed for {job.job_key}: {e}")
            return {}

    async def _extract_style(self, job: FrameJob) -> Dict[str, Any]:
        prompt = self._build_style_extraction_prompt(job.action, job.thought)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            result, _ = await call_openai_api_json_async(
                messages,
                reasoning_effort=self.reasoning_effort,
                stage="frame_detailed_style",
                model=self.model
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"Style extraction failed for {job.job_key}: {e}")
            return {}

    async def _extract_content(self, job: FrameJob) -> Dict[str, Any]:
        prompt = self._build_content_extraction_prompt(job.action, job.thought)
        try:
            image_url = encode_image(job.screenshot_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            result, _ = await call_openai_api_json_async(
                messages,
                reasoning_effort=self.reasoning_effort,
                stage="frame_detailed_content",
                model=self.model
            )
            if isinstance(result, str):
                result = json.loads(result)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"Content extraction failed for {job.job_key}: {e}")
            return {}

    def _merge_detailed_results(self, job: FrameJob,
                                 layout: Dict[str, Any],
                                 style: Dict[str, Any],
                                 content: Dict[str, Any]) -> FrameObservation:

        description = layout.get("description", content.get("description", ""))
        layout_structure = layout.get("layout_structure", {})
        components = layout.get("components", [])


        brand_identity = style.get("brand_identity", {})
        design_system = style.get("design_system", {})
        identified_resources = style.get("identified_resources", {})


        extracted_items = content.get("extracted_items", {})
        page_state = content.get("page_state", {})
        navigation_elements = content.get("navigation_elements", {})


        if design_system:
            layout_structure["design_system"] = design_system


        if navigation_elements:
            page_state["navigation_elements"] = navigation_elements

        page_state["detailed_analysis"] = True
        page_state["extraction_method"] = "parallel_3_pass"
        page_state["last_action"] = job.action

        if self.use_url_signals:
            merged_url_result = {
                "url_evidence": layout.get("url_evidence") or style.get("url_evidence") or content.get("url_evidence", {}),
                "observed_url": layout.get("observed_url") or style.get("observed_url") or content.get("observed_url", "")
            }
            url_meta = self._extract_url_meta(merged_url_result, job.website_url, job)
        else:
            url_meta = {
                "observed_url": "",
                "url_source": "unknown",
                "normalized_url": "",
                "url_prefix": "",
                "url_confidence": 0.0,
                "fallback_cluster_id": hashlib.md5(
                    f"{job.trace_id}:{job.step_index}:{job.action}".encode("utf-8")
                ).hexdigest()[:12],
            }

        description = str(
            content.get("description", "")
            or layout.get("description", "")
            or ""
        ).strip()
        structure_signature = str(layout.get("structure_signature", "") or "").strip()
        if not structure_signature:
            structure_signature = self._compute_structure_signature(layout_structure, components)
        content_signature = str(content.get("content_signature", "") or "").strip()
        if not content_signature:
            content_signature = self._compute_content_signature(extracted_items, page_state, description)
        route_hints = content.get("route_hints", {}) if isinstance(content.get("route_hints"), dict) else {}
        if "visible_navigation_cues" not in route_hints:
            route_hints["visible_navigation_cues"] = content.get("visible_navigation_cues", [])
        if "interaction_state" not in route_hints:
            route_hints["interaction_state"] = content.get("interaction_state", {})

        return FrameObservation(
            trace_id=job.trace_id,
            step_index=job.step_index,
            screenshot_path=job.screenshot_path,
            description=description,
            brand_identity=brand_identity,
            layout_structure=layout_structure,
            components=components,
            extracted_items=extracted_items,
            page_state=page_state,
            identified_resources=identified_resources,
            confidence=0.9,
            observed_url=url_meta["observed_url"],
            url_source=url_meta["url_source"],
            normalized_url=url_meta["normalized_url"],
            url_prefix=url_meta["url_prefix"],
            url_confidence=url_meta["url_confidence"],
            fallback_cluster_id=url_meta["fallback_cluster_id"],
            structure_signature=structure_signature,
            content_signature=content_signature,
            route_hints=route_hints,
            action=job.action,
            thought=job.thought,
        )

    def _fallback_frame_observation(self, job: FrameJob) -> FrameObservation:
        return FrameObservation(
            trace_id=job.trace_id,
            step_index=job.step_index,
            screenshot_path=job.screenshot_path,
            description=(job.thought or job.action or "frame observation")[:200],
            brand_identity={},
            layout_structure={},
            components=[],
            extracted_items={},
            page_state={
                "last_action": job.action,
                "previous_action": job.previous_action,
                "transition_from_previous": None,
                "transition_kind": "unknown",
                "transition_evidence": [],
            },
            identified_resources={},
            confidence=0.3,
            fallback_cluster_id=hashlib.md5(
                f"{job.trace_id}:{job.step_index}:{job.action}".encode("utf-8")
            ).hexdigest()[:12],
            structure_signature="",
            content_signature="",
            route_hints={
                "last_action": job.action,
                "previous_action": job.previous_action,
                "transition_from_previous": None,
                "transition_kind": "unknown",
                "from_fallback": True
            },
            transition_from_previous=None,
            transition_kind="unknown",
            transition_evidence=[],
            action=job.action,
            thought=job.thought,
        )
