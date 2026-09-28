import asyncio
import json
import hashlib
import math
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse

from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.app_spec.model import FrameObservation
from trace2world_pipeline.app_spec.trace_loader import TraceData
from trace2world_pipeline.app_spec.reconcile.screenshot_render_classification import (
    ScreenshotRenderClassificationError,
    classification_json_schema_for_payload,
)
from trace2world_pipeline.app_spec.reconcile.prompts import GLOBAL_REASONING_FIX_PROMPT
from trace2world_pipeline.app_spec.reconcile.compact_gr_producer_chunking import (
    build_authority_namespace_budget_producer_chunks,
    build_phase1_semantic_balanced_chunks_for_gr,
)
from trace2world_pipeline.app_spec.reconcile.global_reasoning_authority import (
    attach_page_identity_contracts_to_output,
    compile_authority_verdict,
)
from trace2world_pipeline.app_spec.step_disposition import is_hard_keep_disposition
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async

VALID_TRANSITION_KINDS = {"navigation", "query_update", "filter_update", "state_only"}


def _detect_schema_issues(result: Dict[str, Any]) -> List[str]:
    issues: List[str] = []

    if "app_type" not in result and "website_type" not in result:
        issues.append("missing required top-level field: app_type")


    for i, s in enumerate(result.get("step_labels", [])):
        cid = s.get("page_cluster_id", "")
        if cid and not cid.startswith("c_"):
            issues.append(f"step_labels[{i}].page_cluster_id='{cid}' must use c_ prefix (e.g. c_{cid})")
        if "is_page_transition" not in s:
            issues.append(f"step_labels[{i}] missing required field: is_page_transition")
        if "confidence" not in s:
            issues.append(f"step_labels[{i}] missing required field: confidence")


    required_page_fields = [
        "page_cluster_id", "page_type_hint", "page_name",
        "description", "primary_functions", "route_path",
        "parent_route_path", "route_level"
    ]
    for i, p in enumerate(result.get("canonical_pages", [])):
        cid = p.get("page_cluster_id", "")
        if cid and not cid.startswith("c_"):
            issues.append(f"canonical_pages[{i}].page_cluster_id='{cid}' must use c_ prefix")
        for field in required_page_fields:
            if field not in p or p[field] is None:
                issues.append(f"canonical_pages[{i}] missing required field: {field}")

        if "page_name" not in p and ("name" in p or "page_cluster_name" in p):
            found = [k for k in ("name", "page_cluster_name") if k in p]
            issues.append(f"canonical_pages[{i}] uses wrong field name for page_name (found: {found})")
        if "page_type_hint" not in p and "page_type" in p:
            issues.append(f"canonical_pages[{i}] uses wrong field name for page_type_hint (found: page_type)")


    required_route_fields = [
        "from_page_cluster_id", "to_page_cluster_id", "trigger",
        "evidence_steps", "confidence"
    ]
    for i, r in enumerate(result.get("canonical_routes", [])):
        for field in required_route_fields:
            if field not in r:
                issues.append(f"canonical_routes[{i}] missing required field: {field}")
        from_cid = r.get("from_page_cluster_id", "")
        to_cid = r.get("to_page_cluster_id", "")
        if from_cid and not from_cid.startswith("c_"):
            issues.append(f"canonical_routes[{i}].from_page_cluster_id='{from_cid}' must use c_ prefix")
        if to_cid and not to_cid.startswith("c_"):
            issues.append(f"canonical_routes[{i}].to_page_cluster_id='{to_cid}' must use c_ prefix")


    step_cids = {s.get("page_cluster_id") for s in result.get("step_labels", []) if s.get("page_cluster_id")}
    page_cids = {p.get("page_cluster_id") for p in result.get("canonical_pages", []) if p.get("page_cluster_id")}
    orphan = step_cids - page_cids
    if orphan:
        issues.append(f"step_labels reference cluster_ids not in canonical_pages: {orphan}")


    for i, r in enumerate(result.get("canonical_routes", [])):
        fc = r.get("from_page_cluster_id")
        tc = r.get("to_page_cluster_id")
        if fc and fc not in page_cids:
            issues.append(f"canonical_routes[{i}].from_page_cluster_id='{fc}' not found in canonical_pages")
        if tc and tc not in page_cids:
            issues.append(f"canonical_routes[{i}].to_page_cluster_id='{tc}' not found in canonical_pages")

    return issues


def _is_unassigned_step_label(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    for key in ("assign_to_page", "include_in_site_model", "materialize_page"):
        if key in item and item.get(key) is False:
            return True
    if item.get("skipped") is True or item.get("skip") is True:
        return True
    cluster_id = str(item.get("page_cluster_id", "")).strip().lower()
    return cluster_id in {"", "none", "null", "skip", "skipped", "unassigned", "no_page"}


class GlobalReasoningMixin:

    def _enforce_state_only_cluster_continuity(
        self,
        normalized: Dict[str, Any],
        traces: List[TraceData],
        observations: Optional[List[FrameObservation]] = None,
    ) -> Dict[str, Any]:
        if not observations:
            return deepcopy(normalized)

        working = deepcopy(normalized)
        obs_map = {self._obs_key(obs): obs for obs in observations}
        labels_by_trace = self._group_step_labels_by_trace(working)

        for trace in traces:
            ordered = labels_by_trace.get(str(trace.trace_id).strip(), [])
            for idx in range(1, len(ordered)):
                previous = ordered[idx - 1]
                current = ordered[idx]
                obs = obs_map.get(f"{trace.trace_id}:{int(current.get('step_index', -1))}")
                previous_obs = obs_map.get(f"{trace.trace_id}:{int(previous.get('step_index', -1))}")
                if obs is None:
                    continue
                previous_cluster = str(previous.get("page_cluster_id", "")).strip()
                current_cluster = str(current.get("page_cluster_id", "")).strip()
                if not previous_cluster or not current_cluster:
                    continue
                transition_kind = str(
                    obs.transition_kind or obs.page_state.get("transition_kind", "") or ""
                ).strip().lower()
                if transition_kind == "same_page_scroll":
                    if self._preserve_route_level_transition(previous, current, previous_obs, obs):
                        continue
                    current["page_cluster_id"] = previous_cluster
                    current["is_page_transition"] = False
                    current["has_unique_page_structure"] = False
                interaction_role = str(
                    obs.page_state.get("interaction_surface_role", "") or "none"
                ).strip().lower()
                host_visible = bool(obs.page_state.get("host_page_still_visible", False))
                if interaction_role in {"opened_overlay", "opened_drawer", "opened_action_sheet", "opened_modal"} and host_visible:
                    current["page_cluster_id"] = previous_cluster
                    current["is_page_transition"] = False
                    current["has_unique_page_structure"] = False
                    current["interaction_surface_role"] = interaction_role
                    current["opened_surface_identity"] = str(
                        obs.page_state.get("opened_surface_identity", "") or ""
                    ).strip()

        working["canonical_routes"] = self._derive_routes_from_step_labels(
            working.get("step_labels", []),
            traces,
            skipped_steps=working.get("skipped_steps", []),
        )
        return working

    def _preserve_route_level_transition(
        self,
        previous_label: Dict[str, Any],
        current_label: Dict[str, Any],
        previous_obs: Optional[FrameObservation],
        current_obs: Optional[FrameObservation],
    ) -> bool:
        if current_obs is None:
            return False
        if not bool(current_label.get("has_unique_page_structure", True)):
            return False

        previous_sticky = str(getattr(previous_obs, "sticky_action_pattern", "") or "").strip().lower()
        current_sticky = str(getattr(current_obs, "sticky_action_pattern", "") or "").strip().lower()
        route_level_sticky_patterns = {"detail_action_bar"}
        if current_sticky in route_level_sticky_patterns and current_sticky != previous_sticky:
            return True

        previous_header = str(getattr(previous_obs, "header_pattern", "") or "").strip().lower()
        current_header = str(getattr(current_obs, "header_pattern", "") or "").strip().lower()
        route_level_header_patterns = {"back_title_actions_header"}
        if current_header in route_level_header_patterns and current_header != previous_header:
            return True
        if not bool(current_label.get("is_page_transition")):
            return False
        return False

    def _page_cluster_map(self, normalized: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            str(page.get("page_cluster_id", "")).strip(): page
            for page in normalized.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
        }

    def _is_state_only_transition_signal(self, obs: FrameObservation) -> bool:
        transition_kind = str(
            obs.transition_kind or obs.page_state.get("transition_kind", "") or ""
        ).strip().lower()
        return transition_kind == "same_page_scroll"

    def _route_surface_role(self, page: Dict[str, Any]) -> str:
        page_type_hint = str(page.get("page_type_hint", "") or "").strip().lower()
        route_path = str(page.get("route_path", "") or "").strip().lower()
        if "search_results" in page_type_hint or "search/results" in route_path:
            return "search_results"
        if self._route_path_is_search_entry(route_path):
            return "form"
        if self._surface_pattern_role(page_type_hint) == "collection":
            return "listing"
        hinted_role = self._surface_pattern_role(page_type_hint)
        if hinted_role in {"detail", "form", "checkout"}:
            return hinted_role

        route_role = self._surface_pattern_role(route_path)
        if route_role == "collection":
            return "listing"
        if route_role in {"detail", "form", "checkout"}:
            return route_role
        if self._route_path_has_entity_instance_shape(route_path):
            return "detail"
        return "unknown"

    def _route_path_is_search_entry(self, route_path: str) -> bool:
        path = str(route_path or "").strip().lower()
        if not path:
            return False
        segments = [segment for segment in path.split("/") if segment]
        return bool(segments) and segments[-1] in {"search", "query", "find"} and "results" not in segments

    def _surface_pattern_role(self, value: str) -> str:
        text = str(value or "").strip().lower()
        if not text or text in {"none", "unknown", "n/a"}:
            return "unknown"
        role_tokens = {
            "checkout": (
                "checkout", "payment", "pay", "cart", "basket", "order_confirm",
                "submit_order",
            ),
            "form": (
                "form", "edit", "create", "compose", "new", "wizard", "input",
                "settings", "save",
            ),
            "detail": (
                "detail", "profile", "article", "record", "document", "product",
                "item", "entity", "storefront", "content_view", "show", "viewer",
            ),
            "collection": (
                "listing", "list", "feed", "grid", "results", "search", "collection",
                "catalog", "browse", "home", "homepage", "index",
            ),
        }
        for role, tokens in role_tokens.items():
            if any(token in text for token in tokens):
                return role
        return "unknown"

    def _route_path_has_entity_instance_shape(self, route_path: str) -> bool:
        path = str(route_path or "").strip().lower()
        if not path:
            return False
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return False
        if any(segment.startswith(":") or segment.startswith("{") or segment.startswith("<") for segment in segments):
            return True
        leaf = segments[-1]
        return bool(re.fullmatch(r"[0-9a-f]{6,}|[0-9]{2,}", leaf))

    def _is_route_level_header_pattern(self, pattern: str) -> bool:
        text = str(pattern or "").strip().lower()
        if not text or text in {"none", "unknown", "n/a"}:
            return False
        if "back" in text and any(token in text for token in ("title", "action", "toolbar", "nav", "header")):
            return True
        return self._surface_pattern_role(text) in {"form", "checkout"}

    def _is_route_level_sticky_pattern(self, pattern: str) -> bool:
        text = str(pattern or "").strip().lower()
        if not text or text in {"none", "unknown", "n/a"}:
            return False
        if any(token in text for token in ("action_bar", "submit", "primary_action", "floating_action")):
            return True
        return self._surface_pattern_role(text) in {"checkout", "form"}

    def _is_input_entry_header_pattern(self, pattern: str) -> bool:
        text = str(pattern or "").strip().lower()
        if not text or text in {"none", "unknown", "n/a"}:
            return False
        return any(token in text for token in ("search", "input", "query", "compose", "form"))

    def _known_surface_value(self, value: str) -> str:
        text = str(value or "").strip().lower()
        if text in {"", "none", "unknown", "n/a"}:
            return ""
        return text

    def _surface_patterns_compatible(
        self,
        previous_obs: Optional[FrameObservation],
        current_obs: Optional[FrameObservation],
    ) -> bool:
        if previous_obs is None or current_obs is None:
            return False

        score = 0
        previous_header = self._known_surface_value(getattr(previous_obs, "header_pattern", ""))
        current_header = self._known_surface_value(getattr(current_obs, "header_pattern", ""))
        if previous_header and current_header and previous_header == current_header:
            score += 1

        previous_body = self._known_surface_value(getattr(previous_obs, "body_pattern", ""))
        current_body = self._known_surface_value(getattr(current_obs, "body_pattern", ""))
        previous_body_role = self._surface_pattern_role(previous_body)
        current_body_role = self._surface_pattern_role(current_body)
        if previous_body and current_body and previous_body == current_body:
            score += 2
        elif (
            previous_body_role
            and current_body_role
            and previous_body_role != "unknown"
            and previous_body_role == current_body_role
        ):
            score += 1

        previous_sticky = self._known_surface_value(getattr(previous_obs, "sticky_action_pattern", ""))
        current_sticky = self._known_surface_value(getattr(current_obs, "sticky_action_pattern", ""))
        if previous_sticky and current_sticky and previous_sticky == current_sticky:
            score += 1

        previous_shell = self._known_surface_value(getattr(previous_obs, "shell_section", ""))
        current_shell = self._known_surface_value(getattr(current_obs, "shell_section", ""))
        if previous_shell and current_shell and previous_shell == current_shell:
            score += 1

        return score >= 2

    def _detect_recent_cluster_reentry(
        self,
        current_cluster: str,
        current_obs: Optional[FrameObservation],
        ordered_labels: List[Dict[str, Any]],
        current_index: int,
        obs_map: Dict[str, FrameObservation],
        trace_id: str,
        lookback_window: int = 12,
    ) -> Dict[str, Any]:
        if not current_cluster or current_obs is None or current_index < 2:
            return {"preserve": False, "evidence": []}

        lower_bound = max(0, current_index - lookback_window)
        for prior_index in range(current_index - 2, lower_bound - 1, -1):
            prior_label = ordered_labels[prior_index]
            prior_cluster = str(prior_label.get("page_cluster_id", "")).strip()
            if prior_cluster != current_cluster:
                continue
            prior_step = int(prior_label.get("step_index", -1))
            prior_obs = obs_map.get(f"{trace_id}:{prior_step}")
            if self._surface_patterns_compatible(prior_obs, current_obs):
                return {
                    "preserve": True,
                    "evidence": [f"recent_cluster_reentry:{current_cluster}@{prior_step}"],
                    "prior_step_index": prior_step,
                }

        return {"preserve": False, "evidence": []}

    def _detect_route_surface_emergence(
        self,
        previous_label: Dict[str, Any],
        current_label: Dict[str, Any],
        previous_obs: Optional[FrameObservation],
        current_obs: Optional[FrameObservation],
        page_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        previous_cluster = str(previous_label.get("page_cluster_id", "")).strip()
        current_cluster = str(current_label.get("page_cluster_id", "")).strip()
        if not current_obs or not previous_cluster or not current_cluster or previous_cluster == current_cluster:
            return {"preserve": False, "score": 0, "evidence": []}

        current_page = page_map.get(current_cluster, {})
        previous_page = page_map.get(previous_cluster, {})
        current_role = self._route_surface_role(current_page)
        previous_page_type = str(previous_page.get("page_type_hint", "") or "").strip().lower()
        score = 0
        evidence: List[str] = []

        if current_role in {"detail", "form", "checkout", "search_results"}:
            score += 2
            evidence.append(f"candidate_page_role:{current_role}")

        previous_sticky = str(getattr(previous_obs, "sticky_action_pattern", "") or "").strip().lower()
        current_sticky = str(getattr(current_obs, "sticky_action_pattern", "") or "").strip().lower()
        structural_signal_count = 0
        if self._is_route_level_sticky_pattern(current_sticky) and current_sticky != previous_sticky:
            score += 2
            structural_signal_count += 1
            evidence.append(f"persistent_action_surface:{current_sticky}")

        previous_header = str(getattr(previous_obs, "header_pattern", "") or "").strip().lower()
        current_header = str(getattr(current_obs, "header_pattern", "") or "").strip().lower()
        if self._is_route_level_header_pattern(current_header) and current_header != previous_header:
            score += 1
            structural_signal_count += 1
            evidence.append(f"navigation_affordance_changed:{previous_header}->{current_header}")
        elif (
            current_role == "form"
            and current_header != previous_header
            and self._is_input_entry_header_pattern(current_header)
        ):
            score += 1
            structural_signal_count += 1
            evidence.append(f"input_entry_header_changed:{previous_header}->{current_header}")

        transition_from_previous = (
            current_obs.transition_from_previous
            if current_obs.transition_from_previous is not None
            else current_obs.page_state.get("transition_from_previous")
        )
        if current_role == "search_results" and transition_from_previous is True:
            score += 1
            structural_signal_count += 1
            evidence.append("search_results_transition_acknowledged")

        previous_body_role = self._surface_pattern_role(getattr(previous_obs, "body_pattern", ""))
        current_body_role = self._surface_pattern_role(getattr(current_obs, "body_pattern", ""))
        if current_body_role in {"detail", "form", "checkout"} and current_body_role != previous_body_role:
            score += 1
            structural_signal_count += 1
            evidence.append(f"body_surface_role_changed:{previous_body_role}->{current_body_role}")

        if previous_sticky == "bottom_tab_bar" and current_sticky != "bottom_tab_bar":
            score += 1
            structural_signal_count += 1
            evidence.append("top_level_shell_changed")

        if (
            current_role == "listing"
            and "home" in previous_page_type
            and self._is_route_level_header_pattern(current_header)
            and previous_sticky == "bottom_tab_bar"
            and current_sticky != "bottom_tab_bar"
        ):
            score += 1
            structural_signal_count += 1
            evidence.append("nested_collection_frame_changed")

        previous_shell = str(getattr(previous_obs, "shell_section", "") or "").strip().lower()
        current_shell = str(getattr(current_obs, "shell_section", "") or "").strip().lower()
        if (
            current_shell
            and previous_shell
            and current_shell not in {"unknown", "none", "n/a"}
            and current_shell != previous_shell
        ):
            score += 1
            structural_signal_count += 1
            evidence.append(f"shell_section_changed:{previous_shell}->{current_shell}")

        return {
            "preserve": score >= 3 and (
                current_role in {"detail", "form", "checkout", "search_results"} or structural_signal_count >= 2
            ),
            "score": score,
            "evidence": evidence,
            "candidate_role": current_role,
        }

    def _page_surface_text(self, page: Dict[str, Any]) -> str:
        page_text = " ".join(
            str(page.get(key, "") or "")
            for key in ("page_cluster_id", "page_type_hint", "page_name", "description", "route_path")
        ).lower()
        return page_text

    def _observation_surface_text(self, obs: Optional[FrameObservation]) -> str:
        if obs is None:
            return ""
        surface_text = " ".join(
            str(value or "")
            for value in (
                obs.description,
                getattr(obs, "header_pattern", ""),
                getattr(obs, "body_pattern", ""),
                getattr(obs, "sticky_action_pattern", ""),
                getattr(obs, "shell_section", ""),
            )
        ).lower()
        return surface_text

    def _previous_action_text(self, obs: Optional[FrameObservation]) -> str:
        if obs is None:
            return ""
        page_state = obs.page_state if isinstance(obs.page_state, dict) else {}
        return str(page_state.get("previous_action", "") or "").strip().lower()

    def _classify_route_action_intent(self, action_text: str) -> str:
        text = str(action_text or "").strip().lower()
        if not text:
            return "unknown"
        if any(token in text for token in ("scroll", "swipe", "drag", "上滑", "下滑", "滑动", "滚动")):
            return "state_update"
        if any(token in text for token in ("type", "input", "focus", "keyboard", "输入", "清空", "编辑", "退格")):
            return "state_update"
        if any(token in text for token in ("filter", "sort", "筛选", "排序", "销量", "价格")):
            return "state_update"
        if any(token in text for token in ("add to cart", "加购", "加入购物车", "黄色加号", "选规格")):
            return "state_update"

        submit_cues = ("submit search", "execute search", "search button", "tap search", "点击搜索", "搜索按钮")
        if any(cue in text for cue in submit_cues) or (
            any(token in text for token in ("search", "搜索"))
            and any(token in text for token in ("click", "tap", "submit", "execute", "点击", "按钮"))
        ):
            return "submit_search"

        open_cues = (
            "open",
            "enter",
            "click",
            "tap",
            "进入",
            "点击",
            "打开",
        )
        collection_cues = (
            "results",
            "result list",
            "listing",
            "collection",
            "feed",
            "list",
            "browse",
            "结果",
            "列表",
        )
        if any(cue in text for cue in open_cues) and any(cue in text for cue in collection_cues):
            return "open_collection"

        entity_cues = (
            "result",
            "card",
            "entity",
            "detail",
            "merchant",
            "store",
            "shop",
            "item",
            "product",
            "列表",
            "卡片",
            "详情",
            "商家",
            "店铺",
            "门店",
            "商品",
        )
        if any(cue in text for cue in open_cues) and any(cue in text for cue in entity_cues):
            return "open_entity"
        return "unknown"

    def _derive_surface_evidence(
        self,
        current_page: Dict[str, Any],
        current_obs: Optional[FrameObservation],
    ) -> Dict[str, bool]:
        page_text = self._page_surface_text(current_page)
        surface_text = self._observation_surface_text(current_obs)
        text = f"{page_text} {surface_text}"
        result_cues = (
            "search results",
            "query results",
            "results page",
            "results list",
            "result cards",
            "product search results",
            "商品搜索结果",
            "搜索结果",
            "结果列表",
            "商品列表",
            "feed_list",
            "grid",
        )
        entity_cues = (
            "single entity",
            "entity detail",
            "detail page",
            "entity name",
            "profile header",
            "record surface",
            "product detail",
            "storefront page",
            "merchant storefront",
            "store page",
            "pharmacy store page",
            "store header",
            "store identity",
            "shop title",
            "shop’s header",
            "rating",
            "summary metrics",
            "店铺",
            "门店",
            "商家",
            "药店",
            "详情",
        )
        route_layout_cues = (
            "tabs",
            "section tabs",
            "toolbar",
            "title_toolbar",
            "back_title",
            "detail_action_bar",
            "filter row",
            "sort",
            "category rail",
            "category sidebar",
            "left sidebar",
            "two-pane",
            "dish ordering",
            "menu list",
            "dish cards",
            "item list",
            "content list",
            "checkout",
            "submit order",
            "form fields",
            "save button",
            "点菜",
            "评价",
            "商家",
            "菜品",
            "选规格",
            "全部商品",
            "所搜商品",
            "购物车",
        )
        input_only_cues = (
            "autocomplete",
            "suggestion list",
            "search suggestions",
            "history/suggestions",
            "keyboard is open",
            "text-entry mode",
            "no actual search results",
            "联想",
            "键盘",
            "输入法",
        )
        modal_cues = (
            "bottom-sheet",
            "bottom sheet",
            "modal",
            "dialog",
            "overlay modal",
            "弹窗",
            "浮层",
        )
        return {
            "result_list": any(cue in text for cue in result_cues),
            "entity_identity": any(cue in text for cue in entity_cues),
            "route_layout": any(cue in text for cue in route_layout_cues),
            "input_only": any(cue in text for cue in input_only_cues),
            "modal_overlay": any(cue in text for cue in modal_cues),
        }

    def _should_preserve_candidate_boundary(
        self,
        previous_role: str,
        current_role: str,
        current_page: Dict[str, Any],
        current_obs: Optional[FrameObservation],
    ) -> bool:
        if current_obs is None:
            return False
        evidence = self._derive_surface_evidence(current_page, current_obs)
        if evidence["modal_overlay"]:
            return False
        if evidence["input_only"]:
            return (
                current_role == "form"
                and previous_role != "form"
                and self._route_path_is_search_entry(str(current_page.get("route_path", "") or ""))
            )

        intent = self._classify_route_action_intent(self._previous_action_text(current_obs))
        if intent == "submit_search" and current_role == "search_results" and evidence["result_list"]:
            return True

        if (
            previous_role == "form"
            and current_role == "listing"
            and evidence["result_list"]
            and evidence["route_layout"]
        ):
            return True

        if (
            intent in {"submit_search", "open_collection"}
            and current_role == "listing"
            and evidence["result_list"]
            and evidence["route_layout"]
        ):
            return True

        role_changed = previous_role != current_role and current_role != "unknown"
        route_surface_roles = {"detail", "checkout", "form", "search_results"}
        if (
            role_changed
            and current_role in route_surface_roles
            and evidence["route_layout"]
            and (evidence["entity_identity"] or evidence["result_list"] or current_role in {"checkout", "form"})
        ):
            return True

        if (
            intent == "open_entity"
            and current_role in {"detail", "checkout", "form"}
            and evidence["route_layout"]
            and evidence["entity_identity"]
        ):
            return True

        return False

    def _resolve_state_only_transitions_with_admissibility(
        self,
        normalized: Dict[str, Any],
        traces: List[TraceData],
        observations: List[FrameObservation],
    ) -> Dict[str, Any]:
        working = deepcopy(normalized)
        obs_map = {self._obs_key(obs): obs for obs in observations}
        labels_by_trace = self._group_step_labels_by_trace(working)
        page_map = self._page_cluster_map(working)

        for trace in traces:
            trace_id = str(trace.trace_id).strip()
            ordered = labels_by_trace.get(trace_id, [])
            for idx in range(1, len(ordered)):
                previous = ordered[idx - 1]
                current = ordered[idx]
                obs = obs_map.get(f"{trace_id}:{int(current.get('step_index', -1))}")
                previous_obs = obs_map.get(f"{trace_id}:{int(previous.get('step_index', -1))}")
                if obs is None or not self._is_state_only_transition_signal(obs):
                    continue
                previous_cluster = str(previous.get("page_cluster_id", "")).strip()
                current_cluster = str(current.get("page_cluster_id", "")).strip()
                if not previous_cluster or not current_cluster:
                    continue
                if current_cluster != previous_cluster:
                    previous_role = self._route_surface_role(page_map.get(previous_cluster, {}))
                    current_role = self._route_surface_role(page_map.get(current_cluster, {}))
                    transition_from_previous = (
                        obs.transition_from_previous
                        if obs.transition_from_previous is not None
                        else obs.page_state.get("transition_from_previous")
                    )
                    stale_results_reentry = (
                        previous_role == "form"
                        and current_role == "search_results"
                        and transition_from_previous is False
                    )
                    intent = self._classify_route_action_intent(self._previous_action_text(obs))
                    if stale_results_reentry and intent == "state_update":
                        current["page_cluster_id"] = previous_cluster
                        current["is_page_transition"] = False
                        current["has_unique_page_structure"] = False
                        continue
                    if self._should_preserve_candidate_boundary(
                        previous_role,
                        current_role,
                        page_map.get(current_cluster, {}),
                        obs,
                    ):
                        current["is_page_transition"] = True
                        current["has_unique_page_structure"] = True
                        continue
                    if not stale_results_reentry and bool(current.get("has_unique_page_structure", True)):
                        reentry = self._detect_recent_cluster_reentry(
                            current_cluster,
                            obs,
                            ordered,
                            idx,
                            obs_map,
                            trace_id,
                        )
                        if reentry.get("preserve"):
                            current["is_page_transition"] = True
                            current["has_unique_page_structure"] = True
                            continue
                emergence = self._detect_route_surface_emergence(
                    previous,
                    current,
                    previous_obs,
                    obs,
                    page_map,
                )
                if emergence.get("preserve"):
                    current["is_page_transition"] = True
                    current["has_unique_page_structure"] = True
                    continue
                current["page_cluster_id"] = previous_cluster
                current["is_page_transition"] = False
                current["has_unique_page_structure"] = False

        working["canonical_routes"] = self._derive_routes_from_step_labels(
            working.get("step_labels", []),
            traces,
            skipped_steps=working.get("skipped_steps", []),
        )
        return working

    def _reconcile_global_reasoning_catalog(
        self,
        normalized: Dict[str, Any],
    ) -> Dict[str, Any]:
        working = deepcopy(normalized)
        supported_clusters = {
            str(label.get("page_cluster_id", "")).strip()
            for label in working.get("step_labels", [])
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }

        canonical_pages = [
            deepcopy(page)
            for page in working.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip() in supported_clusters
        ]
        valid_page_clusters = {
            str(page.get("page_cluster_id", "")).strip()
            for page in canonical_pages
            if isinstance(page, dict)
        }

        canonical_routes = [
            deepcopy(route)
            for route in working.get("canonical_routes", [])
            if isinstance(route, dict)
            and str(route.get("from_page_cluster_id", "")).strip() in valid_page_clusters
            and str(route.get("to_page_cluster_id", "")).strip() in valid_page_clusters
        ]

        task_related_elements = {}
        for page_cluster_id, elements in working.get("task_related_elements", {}).items():
            page_cluster_id = str(page_cluster_id).strip()
            if page_cluster_id not in valid_page_clusters or not isinstance(elements, list):
                continue
            task_related_elements[page_cluster_id] = deepcopy(elements)

        working["canonical_pages"] = canonical_pages
        working["canonical_routes"] = canonical_routes
        working["task_related_elements"] = task_related_elements
        return {
            key: working[key]
            for key in (
                "app_type",
                "website_type",
                "step_labels",
                "skipped_steps",
                "canonical_pages",
                "canonical_routes",
                "task_related_elements",
                "global_reasoning_logic_flow",
                "step_coverage",
            )
            if key in working
        }

    def _apply_global_reasoning_constraint_pass(
        self,
        normalized: Dict[str, Any],
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        step_support_before = {
            str(label.get("page_cluster_id", "")).strip()
            for label in normalized.get("step_labels", [])
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        continuity_resolved = self._enforce_state_only_cluster_continuity(
            normalized,
            traces,
            observations=observations,
        )
        reconciled = self._reconcile_global_reasoning_catalog(continuity_resolved)
        reconciled = self._ensure_global_reasoning_step_coverage(
            reconciled,
            observations,
            traces,
        )
        step_support_after = {
            str(label.get("page_cluster_id", "")).strip()
            for label in reconciled.get("step_labels", [])
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        return {
            "resolved": reconciled,
            "dropped_pages": sorted(step_support_before - step_support_after),
            "resolved_page_count": len(reconciled.get("canonical_pages", [])),
            "resolved_route_count": len(reconciled.get("canonical_routes", [])),
        }

    def _apply_global_reasoning_transition_resolver_pass(
        self,
        normalized: Dict[str, Any],
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        step_support_before = {
            str(label.get("page_cluster_id", "")).strip()
            for label in normalized.get("step_labels", [])
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        transition_resolved = self._resolve_state_only_transitions_with_admissibility(
            normalized,
            traces,
            observations,
        )
        reconciled = self._reconcile_global_reasoning_catalog(transition_resolved)
        reconciled = self._ensure_global_reasoning_step_coverage(
            reconciled,
            observations,
            traces,
        )
        step_support_after = {
            str(label.get("page_cluster_id", "")).strip()
            for label in reconciled.get("step_labels", [])
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        return {
            "resolved": reconciled,
            "dropped_pages": sorted(step_support_before - step_support_after),
            "resolved_page_count": len(reconciled.get("canonical_pages", [])),
            "resolved_route_count": len(reconciled.get("canonical_routes", [])),
        }

    @staticmethod
    def _preserve_retained_observation_labels(
        source: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        working = deepcopy(candidate)
        source_labels = {
            (
                str(label.get("trace_id", "")).strip(),
                int(label.get("step_index", -1)),
            ): deepcopy(label)
            for label in source.get("step_labels", [])
            if isinstance(label, dict)
            and str(label.get("observation_role", "")).strip()
            == "terminal_observation"
        }
        if not source_labels:
            return working

        working["skipped_steps"] = [
            item
            for item in working.get("skipped_steps", [])
            if not (
                isinstance(item, dict)
                and (
                    str(item.get("trace_id", "")).strip(),
                    int(item.get("step_index", -1)),
                )
                in source_labels
            )
        ]
        candidate_labels = [
            deepcopy(label)
            for label in working.get("step_labels", [])
            if isinstance(label, dict)
        ]
        candidate_by_key = {
            (
                str(label.get("trace_id", "")).strip(),
                int(label.get("step_index", -1)),
            ): label
            for label in candidate_labels
        }
        for key, source_label in source_labels.items():
            if key in candidate_by_key:
                candidate_by_key[key]["observation_role"] = "terminal_observation"
                continue
            candidate_labels.append(source_label)
        working["step_labels"] = candidate_labels

        candidate_page_ids = {
            str(page.get("page_cluster_id", "")).strip()
            for page in working.get("canonical_pages", [])
            if isinstance(page, dict)
        }
        source_pages = {
            str(page.get("page_cluster_id", "")).strip(): page
            for page in source.get("canonical_pages", [])
            if isinstance(page, dict)
        }
        for label in source_labels.values():
            cluster_id = str(label.get("page_cluster_id", "")).strip()
            if (
                cluster_id
                and cluster_id not in candidate_page_ids
                and cluster_id in source_pages
            ):
                working.setdefault("canonical_pages", []).append(
                    deepcopy(source_pages[cluster_id])
                )
                candidate_page_ids.add(cluster_id)
        return working

    async def _run_global_reasoning_constraint_flow(
        self,
        payload: Dict[str, Any],
        proposal_result: Dict[str, Any],
        observations: List[FrameObservation],
        traces: List[TraceData],
        stage_suffix: str,
        output_dir: str = "",
        run_identity_fix: bool = True,
    ) -> Dict[str, Any]:
        pass_fn = self._apply_global_reasoning_transition_resolver_pass
        proposal_normalized = self._normalize_global_reasoning_result(proposal_result, observations)
        if output_dir:
            self._save_debug_global_reasoning_stage(
                "global_reasoning_proposal.json",
                proposal_normalized,
                output_dir,
            )

        constraint_result = pass_fn(
            proposal_normalized,
            observations,
            traces,
        )
        resolved = constraint_result["resolved"]
        if output_dir:
            self._save_debug_global_reasoning_stage(
                "global_reasoning_resolved.json",
                resolved,
                output_dir,
            )
            flow_diagnostics = {
                "stage_suffix": stage_suffix,
                "dropped_pages": constraint_result.get("dropped_pages", []),
                "resolved_page_count": constraint_result.get("resolved_page_count", 0),
                "resolved_route_count": constraint_result.get("resolved_route_count", 0),
                "identity_fix_enabled": bool(run_identity_fix),
            }
            self._save_debug_global_reasoning_stage(
                "global_reasoning_transition_resolver_flow.json",
                flow_diagnostics,
                output_dir,
            )

        final_result = resolved


        if output_dir:
            preliminary_final = self._ensure_global_reasoning_step_coverage(
                self._normalize_global_reasoning_result(final_result, observations),
                observations,
                traces,
            )
            page_level_fixed = await self._run_page_level_reasoning_fix(
                preliminary_final,
                output_dir,
            )
            if page_level_fixed is not None:
                final_result = self._preserve_retained_observation_labels(
                    preliminary_final,
                    page_level_fixed,
                )


            merge_fixed = await self._run_sprite_merge_admission(
                final_result,
                output_dir,
                observations,
            )
            if merge_fixed is not None:
                final_result = self._preserve_retained_observation_labels(
                    preliminary_final,
                    merge_fixed,
                )


        if run_identity_fix and final_result.get("canonical_pages") and final_result.get("step_labels"):
            fixed = await self._run_trace_level_reasoning_fix(
                payload,
                final_result,
                observations=observations,
                stage_suffix=f"{stage_suffix}_constraint_flow",
                output_dir=output_dir,
                traces=traces,
            )
            fixed = self._preserve_retained_observation_labels(
                final_result,
                fixed,
            )
            fixed_normalized = self._normalize_global_reasoning_result(fixed, observations)
            if output_dir:
                self._save_debug_global_reasoning_stage(
                    "global_reasoning_trace_level_fixed.json",
                    fixed_normalized,
                    output_dir,
                )
            post_fix_constraint = pass_fn(
                fixed_normalized,
                observations,
                traces,
            )
            post_fix_candidate = self._reconcile_global_reasoning_catalog(post_fix_constraint["resolved"])
            guarded_post_fix = self._apply_post_fix_constraint_guard(
                fixed_normalized,
                post_fix_candidate,
                traces,
            )
            post_fix_resolved = guarded_post_fix["resolved"]
            if output_dir:
                self._save_debug_global_reasoning_stage(
                    "global_reasoning_post_fix_constraint_candidate.json",
                    post_fix_candidate,
                    output_dir,
                )
                self._save_debug_global_reasoning_stage(
                    "global_reasoning_post_fix_constraint_resolved.json",
                    post_fix_resolved,
                    output_dir,
                )
                post_fix_candidate_diff = self._build_post_fix_constraint_diff(
                    fixed_normalized,
                    post_fix_candidate,
                )
                post_fix_applied_diff = self._build_post_fix_constraint_diff(
                    fixed_normalized,
                    post_fix_resolved,
                )
                post_fix_applied_diff["candidate_step_label_change_count"] = post_fix_candidate_diff.get(
                    "step_label_change_count", 0
                )
                post_fix_applied_diff["candidate_step_label_changes"] = post_fix_candidate_diff.get(
                    "step_label_changes", []
                )
                post_fix_applied_diff["accepted_metadata_change_count"] = guarded_post_fix.get(
                    "accepted_metadata_change_count", 0
                )
                post_fix_applied_diff["accepted_metadata_changes"] = guarded_post_fix.get(
                    "accepted_metadata_changes", []
                )
                post_fix_applied_diff["rejected_step_label_change_count"] = guarded_post_fix.get(
                    "rejected_step_label_change_count", 0
                )
                post_fix_applied_diff["rejected_step_label_changes"] = guarded_post_fix.get(
                    "rejected_step_label_changes", []
                )
                self._save_debug_global_reasoning_stage(
                    "global_reasoning_post_fix_constraint_diff.json",
                    post_fix_applied_diff,
                    output_dir,
                )
            final_result = post_fix_resolved

        final_result = self._ensure_global_reasoning_step_coverage(
            self._normalize_global_reasoning_result(final_result, observations),
            observations,
            traces,
        )
        if output_dir:
            self._save_debug_global_reasoning_stage(
                "global_reasoning_final.json",
                final_result,
                output_dir,
            )
        return final_result


    def _build_page_context_map(
        self, global_reasoning: Dict[str, Any]
    ) -> Dict[str, Dict[str, str]]:
        step_labels = global_reasoning.get("step_labels", [])
        canonical_pages = global_reasoning.get("canonical_pages", [])


        page_info: Dict[str, Dict[str, Any]] = {}
        for p in canonical_pages:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("page_cluster_id", "")).strip()
            if pid:
                page_info[pid] = p


        context_map: Dict[str, Dict[str, str]] = {}
        for label in step_labels:
            if not isinstance(label, dict):
                continue
            if _is_unassigned_step_label(label):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            page_cluster_id = str(label.get("page_cluster_id", "")).strip()
            if not page_cluster_id:
                continue
            obs_key = f"{trace_id}:{step_index}"

            page = page_info.get(page_cluster_id, {})
            context_map[obs_key] = {
                "page_cluster_id": page_cluster_id,
                "page_name": str(page.get("page_name", "")).strip(),
                "page_type_hint": str(page.get("page_type_hint", "")).strip(),
                "description": str(page.get("description", "")).strip(),
                "route_path": str(page.get("route_path", "")).strip(),
            }

        return context_map

    @staticmethod
    def _skipped_step_keys(global_reasoning: Dict[str, Any]) -> set:
        skipped = set()
        if not isinstance(global_reasoning, dict):
            return skipped
        for item in global_reasoning.get("skipped_steps", []):
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except Exception:
                continue
            if trace_id and step_index >= 0:
                skipped.add(f"{trace_id}:{step_index}")
        for item in global_reasoning.get("step_labels", []):
            if not isinstance(item, dict) or not _is_unassigned_step_label(item):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except Exception:
                continue
            if trace_id and step_index >= 0:
                skipped.add(f"{trace_id}:{step_index}")
        return skipped

    def _estimate_tokens_for_payload(self, payload: Dict[str, Any]) -> int:

        return max(1, int(len(json.dumps(payload, ensure_ascii=False)) / 4))

    def _action_class(self, action: str) -> str:
        action_text = str(action or "").strip().lower()
        if not action_text:
            return "unknown"
        if any(token in action_text for token in ("swipe", "scroll", "drag", "上滑", "下滑", "滑动", "滚动")):
            return "swipe"
        if any(token in action_text for token in ("type", "input", "enter", "输入")):
            return "type"
        if any(token in action_text for token in ("click", "tap", "press", "点击", "选择")):
            return "click"
        if any(token in action_text for token in ("open_app", "open app", "启动", "打开")):
            return "open_app"
        if any(token in action_text for token in ("wait", "sleep", "等待", "稍等")):
            return "wait"
        if "finished" in action_text or "完成" in action_text:
            return "finished"
        if "takeover" in action_text:
            return "takeover"
        return "other"

    def _clip_compact_text(self, value: str, limit: int = 160) -> str:
        text = " ".join(str(value or "").strip().split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _action_blocks_route_transition(self, action: str) -> bool:
        return self._action_class(action) in {"swipe", "wait", "open_app", "finished", "takeover"}

    def _log_global_reasoning_info(self, message: str) -> None:
        if not self.logger:
            return
        if hasattr(self.logger, "log_info"):
            self.logger.log_info(message)
        elif hasattr(self.logger, "info"):
            self.logger.info(message)

    def _hard_skip_reason(self, step: Any, obs: Optional[FrameObservation]) -> str:
        summary = ""
        if obs is not None:
            summary = " ".join([
                str(getattr(obs, "description", "") or ""),
                str(getattr(obs, "shell_section", "") or ""),
                str(getattr(obs, "header_pattern", "") or ""),
                str(getattr(obs, "body_pattern", "") or ""),
            ]).lower()
        launcher_terms = (
            "phone launcher", "system launcher", "android launcher",
            "phone home", "device home", "手机桌面", "系统桌面",
            "启动器", "桌面",
        )
        if any(term in summary for term in launcher_terms):
            return "phone_home"
        stable_surface_terms = (
            "home page", "homepage", "bottom tab", "tab bar", "feed", "list",
            "merchant", "product", "search", "result", "detail", "form",
            "checkout", "order", "首页", "底部导航", "列表", "商家", "商品",
            "搜索", "结果", "详情", "表单", "结算", "订单", "发票", "地址",
        )
        launch_gate_terms = (
            "splash ad", "startup advertisement", "splash advertisement",
            "splash poster", "interstitial ad", "开屏广告", "启动广告",
            "splash_ad", "splash ad poster",
        )
        if any(term in summary for term in launch_gate_terms) and not any(
            term in summary for term in stable_surface_terms
        ):
            return "transient_launch_gate"
        loading_terms = (
            "app loading", "loading screen", "launch screen", "splash screen",
            "loading spinner", "loading indicator", "正在加载", "加载中",
            "启动页", "开屏页",
        )
        if any(term in summary for term in loading_terms) and not any(
            term in summary for term in stable_surface_terms
        ):
            return "loading_wait"
        return ""

    def _unassigned_step_skip_reason(self, step: Any, obs: Optional[FrameObservation]) -> str:
        hard_reason = self._hard_skip_reason(step, obs)
        if hard_reason:
            return hard_reason
        action_class = self._action_class(getattr(step, "action", ""))
        if action_class == "wait":
            return "loading_wait"
        if action_class == "open_app":
            return "system_launcher"
        if action_class in {"swipe", "type"}:
            return "runtime_state_bridge"
        return "unassigned_step"

    @staticmethod
    def _skip_blocks_trace_route(reason: str) -> bool:
        return str(reason or "").strip().lower() in {
            "phone_home",
            "system_launcher",
            "non_target_app",
            "non_target_app_state",
            "external_app",
            "external_system",
            "system_chrome",
            "unassigned_step",
        }

    @staticmethod
    def _skip_allows_trace_route_bridge(reason: str) -> bool:
        return str(reason or "").strip().lower() in {
            "loading_wait",
            "noop_retry",
            "transient_wait",
            "transient_launch_gate",
        }

    def _ensure_global_reasoning_step_coverage(
        self,
        normalized: Dict[str, Any],
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        working = deepcopy(normalized)
        obs_map = {self._obs_key(o): o for o in observations}
        def item_key(item: Dict[str, Any]) -> str:
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except Exception:
                return ""
            return f"{trace_id}:{step_index}" if trace_id and step_index >= 0 else ""

        labeled_keys = {
            key for key in (item_key(label) for label in working.get("step_labels", []) if isinstance(label, dict))
            if key
        }
        skipped_keys = {
            key for key in (item_key(item) for item in working.get("skipped_steps", []) if isinstance(item, dict))
            if key
        }
        repaired: List[Dict[str, Any]] = []
        unassigned_executable: List[Dict[str, Any]] = []
        unassigned_retained_observations: List[Dict[str, Any]] = []
        skipped_steps = list(working.get("skipped_steps", []))
        for trace in traces:
            executable_by_index = {
                int(step.step_index): step for step in trace.steps
            }
            for unit in self._trace_observation_units(trace):
                step_index = int(unit["step_index"])
                key = f"{trace.trace_id}:{step_index}"
                if (
                    unit.get("observation_role") == "terminal_observation"
                    and key in skipped_keys
                ):
                    unassigned_retained_observations.append({
                        "trace_id": trace.trace_id,
                        "step_index": step_index,
                        "observation_role": "terminal_observation",
                        "reason": "retained_observation_was_skipped",
                    })
                    continue
                if key in labeled_keys or key in skipped_keys:
                    continue
                if unit.get("observation_role") == "terminal_observation":
                    unassigned_retained_observations.append({
                        "trace_id": trace.trace_id,
                        "step_index": step_index,
                        "observation_role": "terminal_observation",
                    })
                    continue
                step = executable_by_index[step_index]
                reason = self._unassigned_step_skip_reason(step, obs_map.get(key))
                if reason == "unassigned_step":
                    unassigned_executable.append({
                        "trace_id": trace.trace_id,
                        "step_index": step.step_index,
                        "action": getattr(step, "action", ""),
                        "screenshot_path": getattr(step, "screenshot_path", ""),
                    })
                    continue
                item = {
                    "trace_id": trace.trace_id,
                    "step_index": step.step_index,
                    "skip_reason": reason,
                    "coverage_repair": True,
                }
                skipped_steps.append(item)
                skipped_keys.add(key)
                repaired.append(item)

        if unassigned_executable:
            sample = unassigned_executable[:20]
            raise RuntimeError(
                "Global reasoning step coverage hard gate failed: "
                "executable steps missing page assignment; "
                f"missing={json.dumps(sample, ensure_ascii=False)}; "
                f"missing_count={len(unassigned_executable)}"
            )

        if unassigned_retained_observations:
            raise RuntimeError(
                "Global reasoning observation coverage hard gate failed: retained "
                "terminal observations require page assignments; "
                f"missing={json.dumps(unassigned_retained_observations[:20], ensure_ascii=False)}; "
                f"missing_count={len(unassigned_retained_observations)}"
            )

        working["skipped_steps"] = sorted(
            [
                item for item in skipped_steps
                if isinstance(item, dict)
                and str(item.get("trace_id", "")).strip()
                and str(item.get("step_index", "")).strip() != ""
            ],
            key=lambda item: (str(item.get("trace_id", "")), int(item.get("step_index", -1))),
        )
        working["canonical_routes"] = self._derive_routes_from_step_labels(
            working.get("step_labels", []),
            traces,
            skipped_steps=working.get("skipped_steps", []),
        )
        total_steps = sum(len(self._trace_observation_units(trace)) for trace in traces)
        working["step_coverage"] = {
            "total_steps": total_steps,
            "assigned_steps": len(labeled_keys),
            "skipped_steps": len(skipped_keys),
            "coverage_repair_count": len(repaired),
            "coverage_repaired_steps": repaired[:200],
            "complete": (len(labeled_keys | skipped_keys) == total_steps),
        }
        return working

    def _build_hard_skip_map(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Dict[str, Any]]:
        obs_map = {self._obs_key(o): o for o in observations}
        hard_skips: Dict[str, Dict[str, Any]] = {}
        for trace in traces:
            for disposition in getattr(trace, "step_dispositions", []) or []:
                if not isinstance(disposition, dict):
                    continue
                verdict = str(disposition.get("verdict", "")).strip().lower()
                if verdict != "skip":
                    continue
                skip_reason = str(
                    disposition.get("skip_reason", "")
                    or disposition.get("reason", "")
                    or ""
                ).strip().lower()
                raw_action_type = str(
                    disposition.get("raw_action_type", "") or ""
                ).strip().lower()
                if skip_reason == "terminal_observation" or raw_action_type == "terminate":
                    continue
                if str(
                    disposition.get("observation_disposition", "") or ""
                ).strip().lower() == "retain":
                    continue
                trace_id = str(disposition.get("trace_id", trace.trace_id)).strip()
                try:
                    step_index = int(disposition.get("step_index", -1))
                except (TypeError, ValueError):
                    continue
                if step_index < 0:
                    continue
                key = f"{trace_id}:{step_index}"
                hard_skips[key] = {
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "skip_reason": str(disposition.get("skip_reason", "dropped_by_preprocess")),
                    "authority": str(disposition.get("authority", "preprocessing")),
                    "bridge_policy": str(disposition.get("bridge_policy", "none")),
                }
            for step in trace.steps:
                key = f"{trace.trace_id}:{step.step_index}"
                if key in hard_skips:
                    continue
                reason = self._hard_skip_reason(step, obs_map.get(key))
                if not reason:
                    continue
                hard_skips[key] = {
                    "trace_id": trace.trace_id,
                    "step_index": step.step_index,
                    "skip_reason": reason,
                }
        return hard_skips

    def _build_hard_keep_map(
        self,
        traces: List[TraceData],
    ) -> Dict[str, Dict[str, Any]]:
        hard_keeps: Dict[str, Dict[str, Any]] = {}
        for trace in traces:
            for disposition in getattr(trace, "step_dispositions", []) or []:
                if not isinstance(disposition, dict):
                    continue
                if not is_hard_keep_disposition(disposition):
                    continue
                trace_id = str(disposition.get("trace_id", trace.trace_id)).strip()
                try:
                    step_index = int(disposition.get("step_index", -1))
                except (TypeError, ValueError):
                    continue
                if not trace_id or step_index < 0:
                    continue
                key = f"{trace_id}:{step_index}"
                hard_keeps[key] = {
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "keep_reason": str(disposition.get("keep_reason", "")),
                    "authority": str(disposition.get("authority", "")),
                    "evidence": str(disposition.get("evidence", "") or disposition.get("reason", "")),
                }
            for terminal in getattr(trace, "terminal_observations", []) or []:
                if not isinstance(terminal, dict):
                    continue
                try:
                    step_index = int(terminal.get("step_index", -1))
                except (TypeError, ValueError):
                    continue
                if step_index < 0:
                    continue
                key = f"{trace.trace_id}:{step_index}"
                hard_keeps[key] = {
                    "trace_id": trace.trace_id,
                    "step_index": step_index,
                    "keep_reason": "retained_terminal_observation_page_evidence",
                    "authority": "terminal_observation_retention_contract",
                    "evidence": str(terminal.get("screenshot_path", "") or ""),
                    "observation_role": "terminal_observation",
                }
        return hard_keeps

    def _segment_signature(self, step: Any, obs: FrameObservation) -> str:
        page_state = obs.page_state if isinstance(obs.page_state, dict) else {}
        parts = [
            str(getattr(obs, "header_pattern", "") or "").strip().lower(),
            str(getattr(obs, "shell_section", "") or "").strip().lower(),
            str(getattr(obs, "body_pattern", "") or "").strip().lower(),
            str(getattr(obs, "sticky_action_pattern", "") or "").strip().lower(),
            str(page_state.get("interaction_surface_role", "") or "none").strip().lower(),
            str(page_state.get("opened_surface_identity", "") or "").strip().lower(),
            self._action_class(getattr(step, "action", "")),
        ]
        return "|".join(parts)

    def _build_compact_trace_ir(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData],
        hard_skips: Dict[str, Dict[str, Any]],
        hard_keeps: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(o): o for o in observations}
        trace_payloads: List[Dict[str, Any]] = []
        for trace in traces:
            segments: List[Dict[str, Any]] = []
            current: Optional[Dict[str, Any]] = None
            segment_index = 0
            for unit in self._trace_observation_units(trace):
                step_index = int(unit["step_index"])
                obs_key = f"{trace.trace_id}:{step_index}"
                obs = obs_map.get(obs_key)
                if obs is None or obs_key in hard_skips:
                    continue
                action_text = str(unit.get("action", "") or "")
                thought_text = str(unit.get("thought", "") or "")
                observation_role = str(
                    unit.get("observation_role", "executable_step")
                )
                action_class = (
                    "observation_only"
                    if observation_role == "terminal_observation"
                    else self._action_class(action_text)
                )
                page_state = obs.page_state if isinstance(obs.page_state, dict) else {}
                signature = "|".join([
                    str(getattr(obs, "header_pattern", "") or "").strip().lower(),
                    str(getattr(obs, "shell_section", "") or "").strip().lower(),
                    str(getattr(obs, "body_pattern", "") or "").strip().lower(),
                    str(getattr(obs, "sticky_action_pattern", "") or "").strip().lower(),
                    str(page_state.get("interaction_surface_role", "") or "none").strip().lower(),
                    str(page_state.get("opened_surface_identity", "") or "").strip().lower(),
                    action_class,
                ])
                action_summary = self._clip_compact_text(action_text, 80)
                if current is None or current.get("visual_signature") != signature:
                    segment_id = f"{trace.trace_id}:s_{segment_index:03d}"
                    segment_index += 1
                    current = {
                        "segment_id": segment_id,
                        "trace_id": trace.trace_id,
                        "steps": [],
                        "step_range": [step_index, step_index],
                        "representative_steps": [step_index],
                        "representative_actions": [action_summary],
                        "representative_thoughts": [self._clip_compact_text(thought_text, 100)],
                        "observation_role": observation_role,
                        "visual_signature": signature,
                        "description": self._clip_compact_text(obs.description, 220),
                        "header_pattern": obs.header_pattern,
                        "shell_section": obs.shell_section,
                        "body_pattern": obs.body_pattern,
                        "sticky_action_pattern": obs.sticky_action_pattern,
                        "transition_kind": str(
                            obs.transition_kind
                            or page_state.get("transition_kind", "")
                            or ""
                        ).strip(),
                        "interaction_surface_hint": {
                            "role": str(
                                page_state.get("interaction_surface_role", "")
                                or "none"
                            ).strip(),
                            "host_page_still_visible": bool(
                                page_state.get("host_page_still_visible", False)
                            ),
                            "host_page_identity": self._clip_compact_text(
                                str(page_state.get("host_page_identity", "") or ""),
                                100,
                            ),
                            "opened_surface_identity": self._clip_compact_text(
                                str(page_state.get("opened_surface_identity", "") or ""),
                                100,
                            ),
                            "evidence": [
                                self._clip_compact_text(str(item), 120)
                                for item in page_state.get("transition_evidence", [])
                                if str(item).strip()
                            ][:4],
                            "is_ground_truth": False,
                        },
                        "external_ui_regions": obs.page_state.get("external_ui_regions", [])
                        if isinstance(obs.page_state.get("external_ui_regions", []), list)
                        else [],
                        "identity_confidence": obs.identity_confidence,
                        "action_classes": [],
                    }
                    segments.append(current)
                current["steps"].append(step_index)
                current["step_range"][1] = step_index
                if action_class not in current["action_classes"]:
                    current["action_classes"].append(action_class)
                if step_index not in current["representative_steps"]:
                    current["representative_steps"] = [
                        current["steps"][0],
                        current["steps"][-1],
                    ]
                    current["representative_actions"] = [
                        current["representative_actions"][0],
                        action_summary,
                    ]
                    first_thought = self._clip_compact_text(
                        thought_text if len(current["steps"]) == 1 else "",
                        100,
                    )
                    last_thought = self._clip_compact_text(thought_text, 100)
                    if first_thought:
                        current["representative_thoughts"] = [first_thought, last_thought]
                    elif current.get("representative_thoughts"):
                        current["representative_thoughts"] = [
                            current["representative_thoughts"][0],
                            last_thought,
                        ]
            trace_payloads.append({
                "trace_id": trace.trace_id,
                "task": trace.task,
                "segments": segments,
            })
        return {
            "format": "compact_trace_ir_v1",
            "traces": trace_payloads,
            "hard_skips": list(hard_skips.values()),
            "hard_keeps": list((hard_keeps or {}).values()),
        }

    def _save_compact_global_reasoning_debug(self, name: str, payload: Dict[str, Any]):
        output_dir = getattr(self, "_current_output_dir", "")
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        debug_dir = str(pom.debug("global_reasoning", "global_reasoning_compact_hierarchical"))
        try:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            if self.logger:
                self.logger.log_warning(f"[Debug] Failed to save compact global reasoning {name}: {exc}")

    def _apply_contrast_admission(
        self,
        partial_results: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
        observations: List[FrameObservation],
        compact_ir: Dict[str, Any],
    ) -> Dict[str, Any]:
        merge_result = deepcopy(merge_result)
        local_to_global = merge_result.get("local_to_global", {})
        canonical_pages = merge_result.get("canonical_pages", [])
        if not isinstance(local_to_global, dict) or not isinstance(canonical_pages, list):
            return merge_result

        obs_map: Dict[str, FrameObservation] = {self._obs_key(o): o for o in observations}


        segment_steps: Dict[str, List[int]] = {}
        segment_trace: Dict[str, str] = {}
        for trace_payload in compact_ir.get("traces", []):
            if not isinstance(trace_payload, dict):
                continue
            trace_id = str(trace_payload.get("trace_id", "")).strip()
            for segment in trace_payload.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                seg_id = str(segment.get("segment_id", "")).strip()
                segment_trace[seg_id] = trace_id
                segment_steps[seg_id] = [
                    int(s) if not isinstance(s, dict) else int(s.get("step_index", -1))
                    for s in segment.get("steps", [])
                ]


        global_signatures: Dict[str, Dict[str, int]] = {}
        global_evidence_count: Dict[str, int] = {}

        for partial in partial_results:
            chunk_id = str(partial.get("chunk_id", "")).strip()
            for assignment in partial.get("segment_assignments", []):
                if not isinstance(assignment, dict):
                    continue
                local_page_id = str(assignment.get("local_page_id", "")).strip()
                seg_id = str(assignment.get("segment_id", "")).strip()
                if not local_page_id or not seg_id:
                    continue
                qualified = f"{chunk_id}:{local_page_id}"
                global_page_id = str(
                    local_to_global.get(qualified, local_to_global.get(local_page_id, ""))
                ).strip()
                if not global_page_id:
                    continue

                trace_id = segment_trace.get(seg_id, "")
                for step_idx in segment_steps.get(seg_id, []):
                    if step_idx < 0:
                        continue
                    obs = obs_map.get(f"{trace_id}:{step_idx}")
                    if obs is None:
                        continue
                    shell = str(getattr(obs, "shell_section", "") or "").strip().lower()
                    header = str(getattr(obs, "header_pattern", "") or "").strip().lower()
                    body = str(getattr(obs, "body_pattern", "") or "").strip().lower()
                    if not shell and not header:
                        continue
                    sig = f"{shell}|{header}|{body}"
                    global_signatures.setdefault(global_page_id, {})
                    global_signatures[global_page_id][sig] = global_signatures[global_page_id].get(sig, 0) + 1
                    global_evidence_count[global_page_id] = global_evidence_count.get(global_page_id, 0) + 1


        page_dominant_sig: Dict[str, str] = {}
        for gid, sigs in global_signatures.items():
            if sigs:
                page_dominant_sig[gid] = max(sigs, key=sigs.get)


        sig_to_pages: Dict[str, List[str]] = {}
        for gid, sig in page_dominant_sig.items():
            shell_part = sig.split("|")[0] if "|" in sig else ""
            if shell_part in ("", "unknown", "none"):
                continue
            sig_to_pages.setdefault(sig, []).append(gid)

        remap: Dict[str, str] = {}
        admission_log: List[Dict[str, Any]] = []

        for sig, pages in sig_to_pages.items():
            if len(pages) < 2:
                continue
            pages_sorted = sorted(pages, key=lambda p: global_evidence_count.get(p, 0), reverse=True)
            survivor = pages_sorted[0]
            for victim in pages_sorted[1:]:
                remap[victim] = survivor
                admission_log.append({
                    "rule": "contrast_admission_merge",
                    "victim": victim,
                    "survivor": survivor,
                    "shared_signature": sig,
                    "victim_evidence": global_evidence_count.get(victim, 0),
                    "survivor_evidence": global_evidence_count.get(survivor, 0),
                })

        if not remap:
            merge_result["_contrast_admission"] = {"remaps": [], "status": "no_merges_needed"}
            return merge_result

        for key, val in local_to_global.items():
            if val in remap:
                local_to_global[key] = remap[val]

        victims = set(remap.keys())
        canonical_pages = [
            p for p in canonical_pages
            if str(p.get("page_cluster_id", "")).strip() not in victims
        ]
        merge_result["canonical_pages"] = canonical_pages
        merge_result["local_to_global"] = local_to_global
        merge_result["_contrast_admission"] = {
            "remaps": [{"from": k, "to": v} for k, v in remap.items()],
            "log": admission_log,
            "status": "applied",
            "pages_before": len(canonical_pages) + len(victims),
            "pages_after": len(canonical_pages),
        }

        if self.logger:
            for entry in admission_log:
                self.logger.log_info(
                    f"[Contrast Admission] Merged {entry['victim']} → {entry['survivor']} "
                    f"(shared: {entry['shared_signature']})"
                )

        return merge_result

    def _attach_structure_fingerprints(
        self,
        chunk_result: Dict[str, Any],
        segment_fingerprints: Dict[str, str],
    ) -> None:
        assignments = chunk_result.get("segment_assignments", [])
        page_fingerprints: Dict[str, Dict[str, int]] = {}
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            seg_id = str(assignment.get("segment_id", "")).strip()
            local_page_id = str(assignment.get("local_page_id", "")).strip()
            fp = segment_fingerprints.get(seg_id, "")
            if local_page_id and fp:
                page_fingerprints.setdefault(local_page_id, {})
                page_fingerprints[local_page_id][fp] = page_fingerprints[local_page_id].get(fp, 0) + 1
        for page in chunk_result.get("page_candidates", chunk_result.get("canonical_pages", [])):
            if not isinstance(page, dict):
                continue
            pid = (
                str(page.get("local_page_id", "")).strip()
                or str(page.get("page_cluster_id", "")).strip()
            )
            local_pid = pid.replace("c_", "p_", 1) if pid.startswith("c_") else pid
            fps = page_fingerprints.get(local_pid, page_fingerprints.get(pid, {}))
            if fps:
                page["structure_fingerprint"] = max(fps, key=fps.get)

    @staticmethod
    def _compact_trace_step_load(trace_payload: Dict[str, Any]) -> int:
        if not isinstance(trace_payload, dict):
            return 0
        load = 0
        for segment in trace_payload.get("segments", []):
            if not isinstance(segment, dict):
                continue
            steps = segment.get("steps", [])
            if isinstance(steps, list):
                load += len(steps)
        return load

    @staticmethod
    def _compact_trace_segment_count(trace_payload: Dict[str, Any]) -> int:
        if not isinstance(trace_payload, dict):
            return 0
        segments = trace_payload.get("segments", [])
        return len(segments) if isinstance(segments, list) else 0

    def _balance_compact_trace_chunks_by_steps(
        self,
        trace_payloads: List[Dict[str, Any]],
        target_trace_count: int,
    ) -> List[List[Dict[str, Any]]]:
        valid_traces = [trace for trace in trace_payloads if isinstance(trace, dict)]
        if not valid_traces:
            return []

        target_trace_count = max(1, int(target_trace_count or 1))
        chunk_count = max(1, math.ceil(len(valid_traces) / target_trace_count))
        chunks: List[Dict[str, Any]] = [
            {"step_load": 0, "trace_count": 0, "traces": []}
            for _ in range(chunk_count)
        ]

        indexed_traces = list(enumerate(valid_traces))
        indexed_traces.sort(
            key=lambda item: (
                self._compact_trace_step_load(item[1]),
                self._compact_trace_segment_count(item[1]),
            ),
            reverse=True,
        )

        for _, trace_payload in indexed_traces:
            target = min(
                chunks,
                key=lambda chunk: (
                    int(chunk["step_load"]),
                    int(chunk["trace_count"]),
                ),
            )
            step_load = self._compact_trace_step_load(trace_payload)
            target["traces"].append(trace_payload)
            target["step_load"] = int(target["step_load"]) + step_load
            target["trace_count"] = int(target["trace_count"]) + 1

        return [
            list(chunk["traces"])
            for chunk in chunks
            if chunk.get("traces")
        ]

    def _compact_trace_chunk_balance_report(
        self,
        chunks: List[List[Dict[str, Any]]],
        target_trace_count: int,
    ) -> Dict[str, Any]:
        chunk_reports: List[Dict[str, Any]] = []
        for index, chunk_traces in enumerate(chunks):
            trace_reports: List[Dict[str, Any]] = []
            for trace_payload in chunk_traces:
                trace_reports.append({
                    "trace_id": str(trace_payload.get("trace_id", "")).strip(),
                    "step_load": self._compact_trace_step_load(trace_payload),
                    "segment_count": self._compact_trace_segment_count(trace_payload),
                })
            chunk_reports.append({
                "chunk_id": f"chunk_{index:02d}",
                "trace_count": len(chunk_traces),
                "step_load": sum(item["step_load"] for item in trace_reports),
                "segment_count": sum(item["segment_count"] for item in trace_reports),
                "traces": trace_reports,
            })
        loads = [int(item["step_load"]) for item in chunk_reports]
        return {
            "format": "compact_trace_chunk_balance_report_v1",
            "strategy": "greedy_step_load_balancing",
            "target_trace_count": max(1, int(target_trace_count or 1)),
            "chunk_count": len(chunk_reports),
            "total_step_load": sum(loads),
            "min_step_load": min(loads) if loads else 0,
            "max_step_load": max(loads) if loads else 0,
            "step_load_spread": (max(loads) - min(loads)) if loads else 0,
            "chunks": chunk_reports,
        }

    @staticmethod
    def _compact_input_segment_ids(chunk_payload: Dict[str, Any]) -> set[str]:
        segment_ids: set[str] = set()
        if not isinstance(chunk_payload, dict):
            return segment_ids
        for trace_payload in chunk_payload.get("traces", []):
            if not isinstance(trace_payload, dict):
                continue
            for segment in trace_payload.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                segment_id = str(segment.get("segment_id", "")).strip()
                if segment_id:
                    segment_ids.add(segment_id)
        return segment_ids

    def _validate_compact_chunk_segment_coverage(
        self,
        chunk_result: Dict[str, Any],
        chunk_payload: Dict[str, Any],
        chunk_id: str,
    ) -> None:
        expected = self._compact_input_segment_ids(chunk_payload)
        if not expected:
            return

        seen: Dict[str, str] = {}
        duplicates: List[str] = []
        invalid: List[str] = []
        for collection_name in ("segment_assignments", "skip_segments"):
            collection = chunk_result.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                segment_id = str(item.get("segment_id", "")).strip()
                if not segment_id:
                    continue
                if segment_id not in expected:
                    invalid.append(segment_id)
                    continue
                if segment_id in seen:
                    duplicates.append(segment_id)
                    continue
                seen[segment_id] = collection_name

        missing = sorted(expected - set(seen))
        overlay_rows = chunk_result.get("state_overlays", [])
        if not isinstance(overlay_rows, list):
            overlay_rows = []
        assignments_by_segment = {
            str(item.get("segment_id", "")).strip(): str(
                item.get("local_page_id", "")
            ).strip()
            for item in chunk_result.get("segment_assignments", [])
            if isinstance(item, dict)
            and str(item.get("segment_id", "")).strip()
        }
        overlay_by_segment: Dict[str, Dict[str, Any]] = {}
        overlay_contract_errors: List[str] = []
        surface_contract_by_id: Dict[str, Tuple[str, str]] = {}
        for index, row in enumerate(overlay_rows):
            if not isinstance(row, dict):
                overlay_contract_errors.append(
                    f"state_overlays[{index}] must be an object"
                )
                continue
            segment_id = str(row.get("segment_id", "") or "").strip()
            underlying_page = str(
                row.get("underlying_local_page_id", "") or ""
            ).strip()
            surface_id = str(row.get("surface_id", "") or "").strip()
            surface_identity = str(
                row.get("opened_surface_identity", "") or ""
            ).strip()
            state_kind = str(row.get("state_kind", "") or "").strip()
            if not segment_id or segment_id not in expected:
                overlay_contract_errors.append(
                    f"state_overlays[{index}] has invalid segment_id={segment_id!r}"
                )
                continue
            if segment_id in overlay_by_segment:
                overlay_contract_errors.append(
                    f"state_overlays contains duplicate segment_id={segment_id}"
                )
            overlay_by_segment[segment_id] = row
            if not underlying_page:
                overlay_contract_errors.append(
                    f"state_overlays[{index}] missing underlying_local_page_id"
                )
            if not surface_id:
                overlay_contract_errors.append(
                    f"state_overlays[{index}] missing surface_id"
                )
            if not surface_identity:
                overlay_contract_errors.append(
                    f"state_overlays[{index}] missing opened_surface_identity"
                )
            assigned_page = assignments_by_segment.get(segment_id, "")
            if assigned_page and underlying_page and assigned_page != underlying_page:
                overlay_contract_errors.append(
                    f"overlay segment {segment_id} is assigned to {assigned_page}, "
                    f"not its underlying page {underlying_page}"
                )
            if underlying_page and surface_id and state_kind:
                surface_contract = (underlying_page, state_kind)
                previous_contract = surface_contract_by_id.setdefault(
                    surface_id,
                    surface_contract,
                )
                if previous_contract != surface_contract:
                    overlay_contract_errors.append(
                        "one surface_id maps to multiple host/role contracts: "
                        f"{surface_id} -> {previous_contract!r}, {surface_contract!r}"
                    )

        for page_index, page in enumerate(chunk_result.get("page_candidates", [])):
            if not isinstance(page, dict):
                continue
            local_page_id = str(page.get("local_page_id", "") or "").strip()
            evidence_segments = [
                str(item).strip()
                for item in page.get("evidence_segments", [])
                if str(item).strip()
            ]
            overlay_evidence = [
                overlay_by_segment[item]
                for item in evidence_segments
                if item in overlay_by_segment
            ]
            if evidence_segments and len(overlay_evidence) == len(evidence_segments):
                underlying_pages = {
                    str(row.get("underlying_local_page_id", "") or "").strip()
                    for row in overlay_evidence
                }
                if local_page_id and local_page_id not in underlying_pages:
                    overlay_contract_errors.append(
                        f"page_candidates[{page_index}] {local_page_id} is supported "
                        "only by transient overlay segments"
                    )

        if invalid or duplicates or missing or overlay_contract_errors:
            parts: List[str] = []
            if invalid:
                parts.append(f"invalid segment ids={sorted(set(invalid))[:20]}")
            if duplicates:
                parts.append(f"duplicate segment ids={sorted(set(duplicates))[:20]}")
            if missing:
                parts.append(f"missing segment ids={missing[:20]}")
            if overlay_contract_errors:
                parts.append(
                    f"invalid overlay boundary contract={overlay_contract_errors[:20]}"
                )
            raise RuntimeError(
                f"Global reasoning compact chunk {chunk_id} segment coverage hard gate failed: "
                + "; ".join(parts)
            )

    async def _run_compact_global_reasoning_chunk(
        self,
        chunk_payload: Dict[str, Any],
        chunk_id: str,
    ) -> Dict[str, Any]:
        prompt = self._build_compact_global_reasoning_chunk_prompt(chunk_payload)
        result, _usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage=f"site_model_global_reasoning_compact_{chunk_id}",
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            return {}
        try:
            self._validate_compact_chunk_segment_coverage(result, chunk_payload, chunk_id)
        except RuntimeError as exc:


            self._save_compact_global_reasoning_debug(
                f"{chunk_id}_rejected_segment_coverage",
                {
                    "schema": "ghostweb.compact_global_reasoning_rejected_chunk.v1",
                    "chunk_id": chunk_id,
                    "reason": str(exc),
                    "input_segment_ids": sorted(
                        self._compact_input_segment_ids(chunk_payload)
                    ),
                    "rejected_output": result,
                },
            )
            raise
        result["chunk_id"] = chunk_id
        return result

    async def _run_compact_global_reasoning_merge(
        self,
        partial_results: List[Dict[str, Any]],
        stage_suffix: str,
    ) -> Dict[str, Any]:
        merge_input = {
            "format": "compact_global_reasoning_merge_v1",
            "partials": [
                self._compact_partial_for_merge(partial)
                for partial in partial_results
                if isinstance(partial, dict)
            ],
        }
        self._save_compact_global_reasoning_debug(f"{stage_suffix}_input", merge_input)
        prompt = self._build_compact_global_reasoning_merge_prompt(merge_input)
        result, _usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage=f"site_model_global_reasoning_compact_merge_{stage_suffix}",
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            return {}
        self._save_compact_global_reasoning_debug(stage_suffix, result)
        return result

    def _compact_partial_for_merge(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        chunk_id = str(partial.get("chunk_id", "")).strip()
        candidates = []
        for page in partial.get("page_candidates", []):
            if not isinstance(page, dict):
                continue
            local_page_id = str(page.get("local_page_id", "")).strip()
            if not local_page_id:
                continue
            item = dict(page)
            item["qualified_local_page_id"] = f"{chunk_id}:{local_page_id}" if chunk_id else local_page_id
            if "source_local_page_ids" not in item:
                item["source_local_page_ids"] = [item["qualified_local_page_id"]]
            candidates.append(item)
        return {
            "chunk_id": chunk_id,
            "app_type": partial.get("app_type", ""),
            "page_candidates": candidates,
            "state_overlays": partial.get("state_overlays", []),
        }

    def _candidate_sources_by_key(self, partials: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        sources: Dict[str, List[str]] = {}
        for partial in partials:
            if not isinstance(partial, dict):
                continue
            chunk_id = str(partial.get("chunk_id", "")).strip()
            for candidate in partial.get("page_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                local_page_id = str(candidate.get("local_page_id", "")).strip()
                if not local_page_id:
                    continue
                qualified = f"{chunk_id}:{local_page_id}" if chunk_id else local_page_id
                candidate_sources = candidate.get("source_local_page_ids", [])
                if not isinstance(candidate_sources, list) or not candidate_sources:
                    candidate_sources = [qualified]
                candidate_sources = [str(item).strip() for item in candidate_sources if str(item).strip()]
                sources[qualified] = candidate_sources
                sources.setdefault(local_page_id, candidate_sources)
        return sources

    def _expand_compact_merge_mapping(
        self,
        input_partials: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
    ) -> Dict[str, str]:
        candidate_sources = self._candidate_sources_by_key(input_partials)
        expanded: Dict[str, str] = {}
        raw_mapping = merge_result.get("local_to_global", {})
        if isinstance(raw_mapping, dict):
            mapping_items = list(raw_mapping.items())
        elif isinstance(raw_mapping, list):
            mapping_items = []
            for item in raw_mapping:
                if not isinstance(item, dict):
                    continue
                local_key = (
                    str(
                        item.get("local_page_id")
                        or item.get("source_local_page_id")
                        or item.get("qualified_local_page_id")
                        or ""
                    ).strip()
                )
                global_id = (
                    str(
                        item.get("global_page_id")
                        or item.get("target_canonical_page_id")
                        or item.get("page_cluster_id")
                        or ""
                    ).strip()
                )
                if local_key and global_id:
                    mapping_items.append((local_key, global_id))
        else:
            mapping_items = []
        for local_key, global_id in mapping_items:
            normalized_global = str(global_id).strip()
            if not normalized_global:
                continue
            if not normalized_global.startswith("c_"):
                normalized_global = f"c_{normalized_global}"
            for source in candidate_sources.get(str(local_key).strip(), [str(local_key).strip()]):
                if source:
                    expanded[source] = normalized_global
        return expanded

    def _source_ids_for_global_page(
        self,
        input_partials: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
        global_page_id: str,
    ) -> List[str]:
        expanded = merge_result.get("_expanded_local_to_global", {})
        if not isinstance(expanded, dict):
            expanded = self._expand_compact_merge_mapping(input_partials, merge_result)
        sources = [
            str(local_key).strip()
            for local_key, mapped_global in expanded.items()
            if str(mapped_global).strip() == global_page_id and str(local_key).strip()
        ]
        return sorted(set(sources))

    def _compact_merge_mapping_coverage(
        self,
        input_partials: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        expected: set[str] = set()
        for partial in input_partials:
            if not isinstance(partial, dict):
                continue
            chunk_id = str(partial.get("chunk_id", "") or "").strip()
            for page in partial.get("page_candidates", []):
                if not isinstance(page, dict):
                    continue
                local_page_id = str(page.get("local_page_id", "") or "").strip()
                if local_page_id:
                    expected.add(f"{chunk_id}:{local_page_id}" if chunk_id else local_page_id)
        expanded = self._expand_compact_merge_mapping(input_partials, merge_result)
        mapped = {str(key).strip() for key in expanded if str(key).strip()}
        missing = sorted(expected - mapped)
        extra = sorted(mapped - expected)
        return {
            "expected_mapping_count": len(expected),
            "mapped_count": len(expected & mapped),
            "missing_mapping_count": len(missing),
            "extra_mapping_count": len(extra),
            "missing_mappings": missing[:50],
            "extra_mappings": extra[:50],
        }

    def _compact_authority_report(
        self,
        *,
        compact_ir: Dict[str, Any],
        producer_chunks: List[Dict[str, Any]],
        partial_results: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        expected_segment_count = 0
        for trace in compact_ir.get("traces", []):
            if isinstance(trace, dict) and isinstance(trace.get("segments"), list):
                expected_segment_count += len(trace.get("segments", []))
        producer_chunk_summaries = [
            {
                "chunk_id": chunk.get("chunk_id", ""),
                "segment_count": sum(
                    len(trace.get("segments", []))
                    for trace in chunk.get("traces", [])
                    if isinstance(trace, dict) and isinstance(trace.get("segments"), list)
                ),
                "chunk_authority_manifest": chunk.get("chunk_authority_manifest", {}),
            }
            for chunk in producer_chunks
            if isinstance(chunk, dict)
        ]
        api_results = [
            {
                "chunk_id": partial.get("chunk_id", ""),
                "ok": True,
                "coverage": {"ok": True},
                "output": {
                    "page_candidates": partial.get("page_candidates", []),
                    "segment_assignments": partial.get("segment_assignments", []),
                    "skip_segments": partial.get("skip_segments", []),
                    "state_overlays": partial.get("state_overlays", []),
                },
            }
            for partial in partial_results
            if isinstance(partial, dict)
        ]
        mapping_coverage = self._compact_merge_mapping_coverage(partial_results, merge_result)
        return {
            "format": "compact_gr_pipeline_authority_report_v1",
            "chunk_build_mode": str(getattr(self, "global_reasoning_logic_flow", "")),
            "source_paths": [],
            "phase1_authority": {"segment_count": expected_segment_count},
            "producer_chunks": producer_chunk_summaries,
            "api_results": api_results,
            "hierarchical_merge": {
                "ok": True,
                "mapping_coverage": mapping_coverage,
                "rounds": merge_result.get("_audit_rounds", []),
            },
        }

    def _local_page_to_global(
        self,
        local_to_global: Dict[str, Any],
        chunk_id: str,
        local_page_id: str,
    ) -> str:
        qualified = f"{chunk_id}:{local_page_id}" if chunk_id else local_page_id
        for key in (qualified, local_page_id):
            value = str(local_to_global.get(key, "")).strip()
            if value:
                return value if value.startswith("c_") else f"c_{value}"
        local = local_page_id[2:] if local_page_id.startswith("p_") else local_page_id
        return f"c_{local}"

    def _page_from_candidate(self, cluster_id: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
        route_path = str(candidate.get("route_path", "")).strip()
        if not route_path:
            route_path = f"/page/{cluster_id[2:] if cluster_id.startswith('c_') else cluster_id}"
        if not route_path.startswith("/"):
            route_path = "/" + route_path
        parent_route_path = str(candidate.get("parent_route_path", "")).strip()
        if not parent_route_path:
            parts = [p for p in route_path.split("/") if p]
            parent_route_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        try:
            route_level = int(candidate.get("route_level", 0))
        except Exception:
            route_level = 0
        if route_level <= 0:
            route_level = max(1, len([p for p in route_path.split("/") if p]))
        return {
            "page_cluster_id": cluster_id,
            "page_type_hint": str(
                candidate.get("page_type_hint", "")
                or candidate.get("page_kind", "")
                or "utility"
            ).strip(),
            "page_name": str(candidate.get("page_name", "") or cluster_id).strip(),
            "description": str(candidate.get("description", "") or candidate.get("page_name", "") or cluster_id).strip(),
            "primary_functions": [
                str(x).strip()
                for x in candidate.get("primary_functions", [])
                if isinstance(x, str) and str(x).strip()
            ],
            "route_path": route_path,
            "parent_route_path": parent_route_path,
            "route_level": route_level,
        }

    @staticmethod
    def _trace_observation_units(trace: TraceData) -> List[Dict[str, Any]]:
        units: List[Dict[str, Any]] = [
            {
                "step_index": int(step.step_index),
                "action": str(step.action or ""),
                "thought": str(step.thought or ""),
                "observation_role": "executable_step",
            }
            for step in trace.steps
        ]
        for item in getattr(trace, "terminal_observations", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                step_index = int(item.get("step_index", -1))
            except (TypeError, ValueError):
                continue
            if step_index < 0:
                continue
            units.append({
                "step_index": step_index,
                "action": "",
                "thought": str(
                    item.get("summary") or item.get("detail") or item.get("status") or ""
                ),
                "observation_role": "terminal_observation",
            })
        return sorted(units, key=lambda item: int(item["step_index"]))

    def _derive_routes_from_step_labels(
        self,
        step_labels: List[Dict[str, Any]],
        traces: List[TraceData],
        skipped_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        label_index = {
            (str(label.get("trace_id", "")), int(label.get("step_index", -1))): label
            for label in step_labels
            if isinstance(label, dict)
        }
        skip_index = {
            (str(item.get("trace_id", "")), int(item.get("step_index", -1))): item
            for item in (skipped_steps or [])
            if isinstance(item, dict)
        }
        routes: List[Dict[str, Any]] = []
        routes_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for trace in traces:
            units = self._trace_observation_units(trace)
            previous_unit = None
            previous_label = None
            bridge_skips: List[Dict[str, Any]] = []
            for unit in units:
                step_index = int(unit["step_index"])
                step_key = (trace.trace_id, step_index)
                label = label_index.get(step_key)
                skip = skip_index.get(step_key)
                if not label:
                    if previous_label and skip:
                        bridge_skips.append(skip)
                    continue
                if not previous_label:
                    previous_unit = unit
                    previous_label = label
                    bridge_skips = []
                    continue
                from_cluster = str(previous_label.get("page_cluster_id", "")).strip()
                to_cluster = str(label.get("page_cluster_id", "")).strip()
                if not from_cluster or not to_cluster or from_cluster == to_cluster:
                    previous_unit = unit
                    previous_label = label
                    bridge_skips = []
                    continue
                if any(
                    self._skip_blocks_trace_route(str(item.get("skip_reason", "")))
                    for item in bridge_skips
                ):
                    previous_unit = unit
                    previous_label = label
                    bridge_skips = []
                    continue
                if bridge_skips and not all(
                    self._skip_allows_trace_route_bridge(str(item.get("skip_reason", "")))
                    for item in bridge_skips
                ):
                    previous_unit = unit
                    previous_label = label
                    bridge_skips = []
                    continue
                trigger = str((previous_unit or {}).get("action", "") or "navigate")
                if self._action_blocks_route_transition(trigger):
                    previous_unit = unit
                    previous_label = label
                    bridge_skips = []
                    continue
                key = (from_cluster, to_cluster, trigger)
                evidence_steps = [
                    f"{trace.trace_id}:{int((previous_unit or {})['step_index'])}"
                ]
                bridge_refs = [
                    f"{str(item.get('trace_id', trace.trace_id))}:{int(item.get('step_index', -1))}"
                    for item in bridge_skips
                    if str(item.get("step_index", "")).strip() != ""
                ]
                existing = routes_by_key.get(key)
                if existing is None:
                    existing = {
                        "from_page_cluster_id": from_cluster,
                        "to_page_cluster_id": to_cluster,
                        "trigger": trigger,
                        "evidence_steps": evidence_steps,
                        "bridge_steps": bridge_refs,
                        "route_source": "trace_transition_closure" if bridge_refs else "adjacent_step_labels",
                        "confidence": 0.78 if bridge_refs else 0.75,
                    }
                    routes_by_key[key] = existing
                    routes.append(existing)
                else:


                    for occurrence in evidence_steps:
                        if occurrence not in existing["evidence_steps"]:
                            existing["evidence_steps"].append(occurrence)
                    for bridge_ref in bridge_refs:
                        if bridge_ref not in existing["bridge_steps"]:
                            existing["bridge_steps"].append(bridge_ref)
                    if existing["bridge_steps"]:
                        existing["route_source"] = "trace_transition_closure"
                        existing["confidence"] = max(
                            float(existing.get("confidence", 0.0) or 0.0), 0.78
                        )
                previous_unit = unit
                previous_label = label
                bridge_skips = []
        return routes

    def _project_compact_global_reasoning(
        self,
        compact_ir: Dict[str, Any],
        partial_results: List[Dict[str, Any]],
        merge_result: Dict[str, Any],
        observations: List[FrameObservation],
        traces: List[TraceData],
        hard_skips: Dict[str, Dict[str, Any]],
        hard_keeps: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(o): o for o in observations}
        obs_keys = set(obs_map)
        segment_steps: Dict[str, List[int]] = {}
        segment_trace: Dict[str, str] = {}
        for trace_payload in compact_ir.get("traces", []):
            if not isinstance(trace_payload, dict):
                continue
            trace_id = str(trace_payload.get("trace_id", "")).strip()
            for segment in trace_payload.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                segment_id = str(segment.get("segment_id", "")).strip()
                if not segment_id:
                    continue
                segment_trace[segment_id] = trace_id
                step_indices: List[int] = []
                for step in segment.get("steps", []):
                    try:
                        step_index = int(step.get("step_index", -1)) if isinstance(step, dict) else int(step)
                    except Exception:
                        continue
                    if step_index >= 0:
                        step_indices.append(step_index)
                segment_steps[segment_id] = step_indices

        local_to_global = merge_result.get("local_to_global", {})
        if not isinstance(local_to_global, dict):
            local_to_global = {}

        candidate_by_qualified: Dict[str, Dict[str, Any]] = {}
        for partial in partial_results:
            chunk_id = str(partial.get("chunk_id", "")).strip()
            for candidate in partial.get("page_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                local_page_id = str(candidate.get("local_page_id", "")).strip()
                if not local_page_id:
                    continue
                candidate_by_qualified[f"{chunk_id}:{local_page_id}"] = candidate
                candidate_by_qualified.setdefault(local_page_id, candidate)

        skipped_steps = list(hard_skips.values())
        skipped_keys = {f"{item['trace_id']}:{item['step_index']}" for item in skipped_steps}
        hard_keep_keys = set((hard_keeps or {}).keys())
        for partial in partial_results:
            chunk_id = str(partial.get("chunk_id", "")).strip()
            for skip_seg in partial.get("skip_segments", []):
                if not isinstance(skip_seg, dict):
                    continue
                segment_id = str(skip_seg.get("segment_id", "")).strip()
                if not segment_id:
                    continue
                trace_id = segment_trace.get(segment_id, "")
                skip_reason = str(skip_seg.get("skip_reason", "other")).strip()
                for step_index in segment_steps.get(segment_id, []):
                    key = f"{trace_id}:{step_index}"
                    if key in hard_keep_keys:
                        raise RuntimeError(
                            "Global reasoning hard-keep violation: "
                            f"{key} cannot be skipped as {skip_reason}; "
                            f"authority={(hard_keeps or {}).get(key, {}).get('authority', '')}"
                        )
                    if key in skipped_keys or key not in obs_keys:
                        continue
                    skipped_steps.append({
                        "trace_id": trace_id,
                        "step_index": step_index,
                        "skip_reason": skip_reason,
                        "authority": "global_reasoning_chunk",
                    })
                    skipped_keys.add(key)
        step_labels: List[Dict[str, Any]] = []
        seen_labels = set()
        for partial in partial_results:
            chunk_id = str(partial.get("chunk_id", "")).strip()
            for assignment in partial.get("segment_assignments", []):
                if not isinstance(assignment, dict):
                    continue
                segment_id = str(assignment.get("segment_id", "")).strip()
                local_page_id = str(assignment.get("local_page_id", "")).strip()
                if not segment_id or not local_page_id:
                    continue
                trace_id = segment_trace.get(segment_id, "")
                cluster_id = self._local_page_to_global(local_to_global, chunk_id, local_page_id)
                try:
                    confidence = float(assignment.get("confidence", 0.75))
                except Exception:
                    confidence = 0.75
                for offset, step_index in enumerate(segment_steps.get(segment_id, [])):
                    key = f"{trace_id}:{step_index}"
                    if key not in obs_keys or key in skipped_keys or key in seen_labels:
                        continue
                    step_labels.append({
                        "trace_id": trace_id,
                        "step_index": step_index,
                        "page_cluster_id": cluster_id,
                        "is_page_transition": bool(assignment.get("is_page_transition", offset == 0)),
                        "confidence": max(0.0, min(1.0, confidence)),
                        "observation_role": str(
                            obs_map[key].page_state.get(
                                "observation_role", "executable_step"
                            )
                            if isinstance(obs_map[key].page_state, dict)
                            else "executable_step"
                        ),
                    })
                    seen_labels.add(key)

        canonical_pages = []
        seen_pages = set()
        for page in merge_result.get("canonical_pages", []):
            if not isinstance(page, dict):
                continue
            cluster_id = str(page.get("page_cluster_id", "")).strip()
            if not cluster_id:
                continue
            cluster_id = cluster_id if cluster_id.startswith("c_") else f"c_{cluster_id}"
            if cluster_id in seen_pages:
                continue
            page = dict(page)
            page["page_cluster_id"] = cluster_id
            canonical_pages.append(self._page_from_candidate(cluster_id, page))
            seen_pages.add(cluster_id)

        assigned_cluster_ids = {
            str(label.get("page_cluster_id", "")).strip()
            for label in step_labels
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        for cluster_id in sorted(assigned_cluster_ids - seen_pages):
            candidate = {}
            for qualified, global_id in local_to_global.items():
                normalized_global = str(global_id).strip()
                if normalized_global and not normalized_global.startswith("c_"):
                    normalized_global = f"c_{normalized_global}"
                if normalized_global == cluster_id:
                    candidate = candidate_by_qualified.get(str(qualified), {})
                    break
            canonical_pages.append(self._page_from_candidate(cluster_id, candidate))
            seen_pages.add(cluster_id)

        result = {
            "app_type": str(merge_result.get("app_type", "")).strip(),
            "step_labels": step_labels,
            "skipped_steps": skipped_steps,
            "canonical_pages": canonical_pages,
            "canonical_routes": self._derive_routes_from_step_labels(
                step_labels,
                traces,
                skipped_steps=skipped_steps,
            ),
            "task_related_elements": merge_result.get("task_related_elements", {})
            if isinstance(merge_result.get("task_related_elements", {}), dict) else {},
            "global_reasoning_logic_flow": str(
                getattr(self, "global_reasoning_logic_flow", "compact_transition_resolver")
            ),
        }
        return self._normalize_global_reasoning_result(result, observations)

    def _build_global_reasoning_payload(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(o): o for o in observations}
        trace_payloads: List[Dict[str, Any]] = []
        for trace in traces:
            steps = []
            for unit in self._trace_observation_units(trace):
                step_index = int(unit["step_index"])
                obs = obs_map.get(f"{trace.trace_id}:{step_index}")
                if obs is None:
                    continue
                steps.append({
                    "step_index": step_index,
                    "action": str(unit.get("action", "") or ""),
                    "thought": str(unit.get("thought", "") or ""),
                    "observation_role": str(
                        unit.get("observation_role", "executable_step")
                    ),
                    "description": obs.description,
                    "header_pattern": obs.header_pattern,
                    "shell_section": obs.shell_section,
                        "body_pattern": obs.body_pattern,
                        "sticky_action_pattern": obs.sticky_action_pattern,
                        "external_ui_regions": obs.page_state.get("external_ui_regions", [])
                        if isinstance(obs.page_state.get("external_ui_regions", []), list)
                        else [],
                        "identity_confidence": obs.identity_confidence,
                    "transition_from_previous": obs.transition_from_previous if obs.transition_from_previous is not None else obs.page_state.get("transition_from_previous"),
                    "transition_kind": obs.transition_kind or str(obs.page_state.get("transition_kind", "") or "").strip(),
                    "transition_evidence": obs.transition_evidence or (
                        [
                            str(item).strip()
                            for item in obs.page_state.get("transition_evidence", [])
                            if isinstance(item, str) and str(item).strip()
                        ]
                        if isinstance(obs.page_state.get("transition_evidence", []), list)
                        else []
                    ),
                })
            trace_payloads.append({
                "trace_id": trace.trace_id,
                "task": trace.task,
                "steps": steps
            })
        return {
            "traces": trace_payloads
        }

    def _select_identity_judge_trace_ids(self, normalized: Dict[str, Any]) -> List[str]:
        labels_by_trace: Dict[str, int] = {}
        for item in normalized.get("step_labels", []):
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            if not trace_id:
                continue
            labels_by_trace[trace_id] = labels_by_trace.get(trace_id, 0) + 1
        return sorted(trace_id for trace_id, count in labels_by_trace.items() if count >= 2)

    def _compact_identity_page(self, page: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "page_cluster_id": str(page.get("page_cluster_id", "")).strip(),
            "page_type_hint": str(page.get("page_type_hint", "")).strip(),
            "page_name": str(page.get("page_name", "")).strip(),
            "description": str(page.get("description", "")).strip(),
            "primary_functions": [
                str(item).strip()
                for item in page.get("primary_functions", [])
                if isinstance(item, str) and str(item).strip()
            ],
            "route_path": str(page.get("route_path", "")).strip(),
            "parent_route_path": str(page.get("parent_route_path", "")).strip(),
            "route_level": page.get("route_level", 0),
        }

    def _identity_candidate_pages_for_trace(
        self,
        trace_labels: List[Dict[str, Any]],
        canonical_pages: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        trace_page_ids = {
            str(label.get("page_cluster_id", "")).strip()
            for label in trace_labels
            if isinstance(label, dict) and str(label.get("page_cluster_id", "")).strip()
        }
        candidates: Dict[str, Dict[str, Any]] = {}
        for page in canonical_pages:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("page_cluster_id", "")).strip()
            if not page_id or page_id not in trace_page_ids:
                continue
            candidates[page_id] = self._compact_identity_page(page)
        return candidates

    def _build_trace_identity_judge_payload(
        self,
        trace_id: str,
        normalized: Dict[str, Any],
        observations: List[FrameObservation],
        traces: Optional[List[TraceData]] = None,
        trace_tasks: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(obs): obs for obs in observations}
        trace_by_id = {trace.trace_id: trace for trace in traces or []}
        trace = trace_by_id.get(trace_id)
        trace_task = trace.task if trace else str((trace_tasks or {}).get(trace_id, "")).strip()
        trace_labels = sorted(
            [
                item
                for item in normalized.get("step_labels", [])
                if isinstance(item, dict) and str(item.get("trace_id", "")).strip() == trace_id
            ],
            key=lambda item: int(item.get("step_index", -1)),
        )
        ordered_steps: List[Dict[str, Any]] = []
        for label in trace_labels:
            step_index = int(label.get("step_index", -1))
            obs = obs_map.get(f"{trace_id}:{step_index}")
            if not obs:
                continue
            confidence = float(getattr(obs, "identity_confidence", 0.0) or 0.0)
            ordered_steps.append({
                "step_index": step_index,
                "assigned_cluster_id": str(label.get("page_cluster_id", "")).strip(),
                "is_page_transition": bool(label.get("is_page_transition", True)),
                "has_unique_page_structure": bool(label.get("has_unique_page_structure", True)),
                "assignment_confidence": float(label.get("confidence", 0.0) or 0.0),
                "description": getattr(obs, "description", ""),
                "header_pattern": getattr(obs, "header_pattern", ""),
                "shell_section": getattr(obs, "shell_section", ""),
                "body_pattern": getattr(obs, "body_pattern", ""),
                "sticky_action_pattern": getattr(obs, "sticky_action_pattern", ""),
                "identity_confidence": confidence,
                "transition_from_previous": (
                    obs.transition_from_previous
                    if obs.transition_from_previous is not None
                    else obs.page_state.get("transition_from_previous")
                ),
                "transition_kind": obs.transition_kind or str(obs.page_state.get("transition_kind", "") or "").strip(),
                "transition_evidence": obs.transition_evidence or (
                    [
                        str(item).strip()
                        for item in obs.page_state.get("transition_evidence", [])
                        if isinstance(item, str) and str(item).strip()
                    ]
                    if isinstance(obs.page_state.get("transition_evidence", []), list)
                    else []
                ),
                "action": getattr(obs, "action", ""),
                "thought": getattr(obs, "thought", ""),
            })

        trace_pages = self._identity_candidate_pages_for_trace(
            trace_labels,
            normalized.get("canonical_pages", []),
        )
        return {
            "task": "Trace-wise page-cluster assignment audit with no preselected suspect step.",
            "rules": [
                "Audit every ordered step in order. Do not assume any specific step is wrong.",
                "Use CURRENT screenshot/observation identity as primary evidence; action describes the next action from the current page.",
                "First ask whether the CURRENT screenshot can still be explained as the previous page after an in-page state change such as scroll, filter, query/value change, or same-shell feed/list update.",
                "Same-page explanation is the default decision. Only if the CURRENT screenshot cannot be reasonably explained as an in-page state change should you recommend a different cluster.",
                "A step is misassigned only if its current page identity, vertical, top-level app section, or standalone route-level page frame clearly differs from its assigned canonical page cluster.",
                "Only recommend a cluster listed in trace_pages. Do not search or recommend page clusters that are not present in this trace.",
                "Do not flag normal same-page form filling, typing, keyboard-open states, scrolling, or repeated list/detail states.",
                "If the current observation or assignment context indicates transition_from_previous=false, treat it as same-page evidence unless the screenshot clearly shows a different top-level app section or a new standalone route-level page frame that cannot plausibly exist as an in-page state of the previous cluster.",
                "same_page_scroll is the only hard same-page signal: do not infer a separate listing/search-results page from scroll alone, and let scroll override generic listing/search-results heuristics and other looks-like-a-new-page heuristics.",
                "same_page_filter, same_page_other, and transition_from_previous=false are state evidence to evaluate against the visible stable identity, not automatic cluster identity rules.",
                "Do not treat back+search headers, tab rows, filter chips, denser merchant/product feeds, search-like controls, or longer listing content by themselves as proof of a different top-level or route-level surface. These may still be in-page states within the same shell.",
                "To recommend a new cluster, identify the new standalone route-level page frame or different top-level app section that appears and explain why it cannot be explained by scroll/filter/query refinement within the previous shell.",
                "Return strict JSON only.",
            ],
            "trace_id": trace_id,
            "trace_task": trace_task,
            "trace_pages": trace_pages,
            "ordered_steps": ordered_steps,
            "required_output_schema": {
                "trace_id": trace_id,
                "mismatches": [{
                    "step_index": "int",
                    "current_cluster_id": "string",
                    "recommended_cluster_id": "string, must be one of trace_pages",
                    "severity": "fixloop|diagnostic",
                    "reason_code": "string",
                    "confidence": "number 0..1",
                    "evidence": ["short strings"],
                    "route_implication": "string",
                }],
                "non_mismatch_notes": ["optional short strings for ambiguous but acceptable assignments"],
            },
        }

    def _normalize_trace_identity_judge_result(
        self,
        trace_id: str,
        raw_result: Dict[str, Any],
        valid_step_indices: set,
        valid_cluster_ids: set,
    ) -> Dict[str, Any]:
        normalized = {
            "trace_id": trace_id,
            "status": "ok",
            "mismatches": [],
            "invalid_mismatches": [],
            "non_mismatch_notes": [],
            "usage": {},
        }
        if not isinstance(raw_result, dict):
            normalized["status"] = "failed"
            normalized["error"] = "judge_result_not_object"
            return normalized

        usage = raw_result.get("usage", {})
        if isinstance(usage, dict):
            normalized["usage"] = usage

        notes = raw_result.get("non_mismatch_notes", [])
        if isinstance(notes, list):
            for note in notes:
                if isinstance(note, str) and note.strip():
                    normalized["non_mismatch_notes"].append(note.strip())

        mismatches = raw_result.get("mismatches", [])
        if not isinstance(mismatches, list):
            normalized["invalid_mismatches"].append(mismatches)
            return normalized

        for item in mismatches:
            if not isinstance(item, dict):
                normalized["invalid_mismatches"].append(item)
                continue
            try:
                step_index = int(item.get("step_index", -1))
                confidence = float(item.get("confidence", 0.0) or 0.0)
            except Exception:
                normalized["invalid_mismatches"].append(item)
                continue
            current_cluster_id = str(item.get("current_cluster_id", "")).strip()
            recommended_cluster_id = str(item.get("recommended_cluster_id", "")).strip()
            severity = str(item.get("severity", "diagnostic")).strip()
            if (
                step_index not in valid_step_indices
                or current_cluster_id not in valid_cluster_ids
                or recommended_cluster_id not in valid_cluster_ids
                or not recommended_cluster_id
                or not math.isfinite(confidence)
            ):
                normalized["invalid_mismatches"].append(item)
                continue
            if severity not in {"fixloop", "diagnostic"}:
                severity = "diagnostic"
            evidence_items = item.get("evidence", [])
            if not isinstance(evidence_items, list):
                evidence_items = []
            normalized["mismatches"].append({
                "trace_id": trace_id,
                "step_index": step_index,
                "current_cluster_id": current_cluster_id,
                "recommended_cluster_id": recommended_cluster_id,
                "severity": severity,
                "reason_code": str(item.get("reason_code", "identity_mismatch")).strip() or "identity_mismatch",
                "confidence": max(0.0, min(1.0, confidence)),
                "evidence": [
                    str(evidence).strip()
                    for evidence in evidence_items
                    if isinstance(evidence, str) and str(evidence).strip()
                ],
                "route_implication": str(item.get("route_implication", "")).strip(),
            })
        return normalized

    def _aggregate_trace_identity_judge_results(
        self,
        results: List[Dict[str, Any]],
        confidence_threshold: float = 0.85,
    ) -> Dict[str, Any]:
        summary = {
            "judged_trace_count": 0,
            "failed_trace_count": 0,
            "mismatch_count": 0,
            "fixloop_mismatch_count": 0,
            "diagnostic_mismatch_count": 0,
            "fixloop_mismatches": [],
            "diagnostic_mismatches": [],
            "token_usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        }
        for result in results:
            if not isinstance(result, dict):
                summary["failed_trace_count"] += 1
                continue
            if result.get("status") == "failed":
                summary["failed_trace_count"] += 1
                continue
            summary["judged_trace_count"] += 1
            usage = result.get("usage", {}) if isinstance(result.get("usage", {}), dict) else {}
            for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
                try:
                    summary["token_usage"][key] += int(usage.get(key, 0) or 0)
                except Exception:
                    pass
            for mismatch in result.get("mismatches", []):
                if not isinstance(mismatch, dict):
                    continue
                summary["mismatch_count"] += 1
                try:
                    confidence = float(mismatch.get("confidence", 0.0) or 0.0)
                except Exception:
                    confidence = 0.0
                fixloop_threshold = min(confidence_threshold, 0.75)
                if mismatch.get("severity") == "fixloop" and confidence >= fixloop_threshold:
                    summary["fixloop_mismatches"].append(mismatch)
                    summary["fixloop_mismatch_count"] += 1
                else:
                    summary["diagnostic_mismatches"].append(mismatch)
                    summary["diagnostic_mismatch_count"] += 1
        return summary

    def _failed_trace_identity_judge_result(self, trace_id: str, error: BaseException) -> Dict[str, Any]:
        return {
            "trace_id": trace_id,
            "status": "failed",
            "error": str(error),
            "mismatches": [],
            "invalid_mismatches": [],
            "non_mismatch_notes": [],
            "usage": {},
        }

    async def _run_trace_identity_judge(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        trace_id = str(payload.get("trace_id", "")).strip()
        messages = [{
            "role": "user",
            "content": (
                "You are a strict trace-wise page identity auditor for trace-based app modeling. "
                "Find all incorrectly assigned steps, if any. Return strict JSON only.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            ),
        }]
        result, usage = await call_openai_api_json_async(
            messages,
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage="site_model_global_reasoning_trace_identity_judge",
        )
        if isinstance(result, str):
            result = json.loads(result)

        valid_step_indices = set()
        valid_cluster_ids = set()
        for item in payload.get("ordered_steps", []):
            if not isinstance(item, dict):
                continue
            try:
                valid_step_indices.add(int(item.get("step_index", -1)))
            except Exception:
                pass
            cluster_id = str(item.get("assigned_cluster_id", "")).strip()
            if cluster_id:
                valid_cluster_ids.add(cluster_id)
        trace_pages = payload.get("trace_pages", {})
        if isinstance(trace_pages, dict):
            for cluster_id in trace_pages:
                cluster_id = str(cluster_id).strip()
                if cluster_id:
                    valid_cluster_ids.add(cluster_id)

        normalized = self._normalize_trace_identity_judge_result(
            trace_id=trace_id,
            raw_result=result,
            valid_step_indices=valid_step_indices,
            valid_cluster_ids=valid_cluster_ids,
        )
        normalized["usage"] = usage if isinstance(usage, dict) else {}
        return normalized

    def _save_trace_identity_judge_debug(
        self,
        output_dir: str,
        results: List[Dict[str, Any]],
        summary: Dict[str, Any],
        payloads: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        debug_dir = str(pom.debug("global_reasoning", "trace_wise_judge"))
        os.makedirs(debug_dir, exist_ok=True)
        for result in results:
            trace_id = str(result.get("trace_id", "unknown")).replace("/", "_").replace(":", "_")
            path = os.path.join(debug_dir, f"trace_{trace_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            trace_id = str(payload.get("trace_id", "unknown")).replace("/", "_").replace(":", "_")
            path = os.path.join(debug_dir, f"trace_{trace_id}_payload.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(debug_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

    def _trace_tasks_from_payload(self, payload: Dict[str, Any]) -> Dict[str, str]:
        trace_tasks: Dict[str, str] = {}
        for trace in payload.get("traces", []):
            if not isinstance(trace, dict):
                continue
            trace_id = str(trace.get("trace_id", "")).strip()
            if trace_id:
                trace_tasks[trace_id] = str(trace.get("task", "")).strip()
        return trace_tasks

    def _group_step_labels_by_trace(
        self,
        normalized: Dict[str, Any],
    ) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for label in normalized.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            if not trace_id or step_index < 0:
                continue
            grouped.setdefault(trace_id, []).append(label)
        for trace_id in grouped:
            grouped[trace_id] = sorted(grouped[trace_id], key=lambda item: int(item.get("step_index", -1)))
        return grouped

    def _trace_action_lookup(self, traces: Optional[List[TraceData]]) -> Dict[Tuple[str, int], str]:
        action_lookup: Dict[Tuple[str, int], str] = {}
        for trace in traces or []:
            for step in trace.steps:
                try:
                    action_lookup[(str(trace.trace_id).strip(), int(step.step_index))] = str(step.action or "")
                except Exception:
                    continue
        return action_lookup

    def _apply_identity_operations(
        self,
        normalized: Dict[str, Any],
        operations: List[Dict[str, Any]],
        *,
        require_from_match: bool,
    ) -> Dict[str, Any]:
        patched = deepcopy(normalized)
        by_key = {
            (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1))): label
            for label in patched.get("step_labels", [])
            if isinstance(label, dict)
        }
        page_ids = {
            str(page.get("page_cluster_id", "")).strip()
            for page in patched.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
        }
        applied: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            trace_id = str(operation.get("trace_id", "")).strip()
            try:
                step_index = int(operation.get("step_index", -1))
            except Exception:
                continue
            current = str(operation.get("from_cluster", operation.get("current_cluster_id", ""))).strip()
            recommended = str(operation.get("to_cluster", operation.get("recommended_cluster_id", ""))).strip()
            label = by_key.get((trace_id, step_index))
            if not label or not recommended:
                skipped.append({"operation": operation, "reason": "missing_label_or_target"})
                continue
            if recommended not in page_ids:
                skipped.append({
                    "operation": operation,
                    "reason": "recommended_cluster_missing_from_canonical_pages",
                })
                continue
            actual_cluster = str(label.get("page_cluster_id", "")).strip()
            if require_from_match and current and actual_cluster != current:
                skipped.append({
                    "operation": operation,
                    "reason": "current_cluster_changed",
                    "actual_cluster": actual_cluster,
                })
                continue
            label["page_cluster_id"] = recommended
            label["is_page_transition"] = True
            label["has_unique_page_structure"] = True
            label["confidence"] = max(
                float(label.get("confidence", 0.0) or 0.0),
                float(operation.get("confidence", 0.0) or 0.0),
            )
            applied.append({
                "op": "reassign_step",
                "trace_id": trace_id,
                "step_index": step_index,
                "from_cluster": actual_cluster,
                "to_cluster": recommended,
                "reason_code": operation.get("reason_code", ""),
                "confidence": operation.get("confidence", 0.0),
            })
        return {"patched": patched, "operations": applied, "skipped": skipped}

    def _apply_identity_judge_reassignments(
        self,
        normalized: Dict[str, Any],
        mismatches: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        operations: List[Dict[str, Any]] = []
        for mismatch in mismatches:
            if not isinstance(mismatch, dict):
                continue
            if mismatch.get("severity") != "fixloop":
                continue
            operations.append({
                "trace_id": str(mismatch.get("trace_id", "")).strip(),
                "step_index": mismatch.get("step_index", -1),
                "from_cluster": str(mismatch.get("current_cluster_id", "")).strip(),
                "to_cluster": str(mismatch.get("recommended_cluster_id", "")).strip(),
                "reason_code": mismatch.get("reason_code", ""),
                "confidence": mismatch.get("confidence", 0.0),
            })
        return self._apply_identity_operations(
            normalized,
            operations,
            require_from_match=True,
        )

    def _derive_identity_operations_from_diff(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        before_map = {
            (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1))): str(label.get("page_cluster_id", "")).strip()
            for label in before.get("step_labels", [])
            if isinstance(label, dict)
        }
        operations: List[Dict[str, Any]] = []
        for label in after.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            try:
                key = (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1)))
            except Exception:
                continue
            before_cluster = before_map.get(key, "")
            after_cluster = str(label.get("page_cluster_id", "")).strip()
            if before_cluster and after_cluster and before_cluster != after_cluster:
                operations.append({
                    "trace_id": key[0],
                    "step_index": key[1],
                    "from_cluster": before_cluster,
                    "to_cluster": after_cluster,
                    "reason_code": "identity_fix_lock",
                    "confidence": float(label.get("confidence", 0.0) or 0.0),
                })
        return operations

    def _build_post_fix_constraint_diff(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
    ) -> Dict[str, Any]:
        before_labels = {
            (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1))): label
            for label in before.get("step_labels", [])
            if isinstance(label, dict)
        }
        after_labels = {
            (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1))): label
            for label in after.get("step_labels", [])
            if isinstance(label, dict)
        }
        step_label_changes: List[Dict[str, Any]] = []
        for key in sorted(set(before_labels) & set(after_labels)):
            before_label = before_labels[key]
            after_label = after_labels[key]
            before_page = str(before_label.get("page_cluster_id", "")).strip()
            after_page = str(after_label.get("page_cluster_id", "")).strip()
            before_transition = bool(before_label.get("is_page_transition", False))
            after_transition = bool(after_label.get("is_page_transition", False))
            before_unique = bool(before_label.get("has_unique_page_structure", False))
            after_unique = bool(after_label.get("has_unique_page_structure", False))
            before_confidence = before_label.get("confidence", 0.0)
            after_confidence = after_label.get("confidence", 0.0)
            if (
                before_page == after_page
                and before_transition == after_transition
                and before_unique == after_unique
                and before_confidence == after_confidence
            ):
                continue
            trace_id, step_index = key
            step_label_changes.append({
                "key": f"{trace_id}:{step_index}",
                "trace_id": trace_id,
                "step_index": step_index,
                "from_page_cluster_id": before_page,
                "to_page_cluster_id": after_page,
                "from_is_page_transition": before_transition,
                "to_is_page_transition": after_transition,
                "from_has_unique_page_structure": before_unique,
                "to_has_unique_page_structure": after_unique,
                "from_confidence": before_confidence,
                "to_confidence": after_confidence,
            })

        before_page_ids = {
            str(page.get("page_cluster_id", "")).strip()
            for page in before.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
        }
        after_page_ids = {
            str(page.get("page_cluster_id", "")).strip()
            for page in after.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
        }
        return {
            "format": "global_reasoning_post_fix_constraint_diff_v1",
            "before_summary": {
                "pages": len(before.get("canonical_pages", [])),
                "routes": len(before.get("canonical_routes", [])),
                "step_labels": len(before.get("step_labels", [])),
            },
            "after_summary": {
                "pages": len(after.get("canonical_pages", [])),
                "routes": len(after.get("canonical_routes", [])),
                "step_labels": len(after.get("step_labels", [])),
            },
            "step_label_change_count": len(step_label_changes),
            "step_label_changes": step_label_changes,
            "dropped_page_cluster_ids": sorted(before_page_ids - after_page_ids),
            "added_page_cluster_ids": sorted(after_page_ids - before_page_ids),
        }

    def _apply_post_fix_constraint_guard(
        self,
        trace_fixed: Dict[str, Any],
        post_candidate: Dict[str, Any],
        traces: List[TraceData],
    ) -> Dict[str, Any]:
        guarded = deepcopy(trace_fixed)
        candidate_labels = {
            (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1))): label
            for label in post_candidate.get("step_labels", [])
            if isinstance(label, dict)
        }
        rejected_changes: List[Dict[str, Any]] = []
        accepted_metadata_changes: List[Dict[str, Any]] = []
        for label in guarded.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            try:
                key = (str(label.get("trace_id", "")).strip(), int(label.get("step_index", -1)))
            except Exception:
                continue
            candidate = candidate_labels.get(key)
            if not candidate:
                continue
            before_page = str(label.get("page_cluster_id", "")).strip()
            candidate_page = str(candidate.get("page_cluster_id", "")).strip()
            if before_page != candidate_page:
                rejected_changes.append({
                    "key": f"{key[0]}:{key[1]}",
                    "trace_id": key[0],
                    "step_index": key[1],
                    "from_page_cluster_id": before_page,
                    "to_page_cluster_id": candidate_page,
                    "reason": "post_fix_constraint_cannot_override_page_assignment",
                })
                continue

            metadata_change: Dict[str, Any] = {
                "key": f"{key[0]}:{key[1]}",
                "trace_id": key[0],
                "step_index": key[1],
            }
            changed = False
            for field in ("is_page_transition", "has_unique_page_structure", "confidence"):
                if field not in candidate:
                    continue
                before_value = label.get(field)
                after_value = candidate.get(field)
                if before_value == after_value:
                    continue
                metadata_change[f"from_{field}"] = before_value
                metadata_change[f"to_{field}"] = after_value
                label[field] = after_value
                changed = True
            if changed:
                accepted_metadata_changes.append(metadata_change)

        guarded["canonical_routes"] = self._derive_routes_from_step_labels(
            guarded.get("step_labels", []),
            traces,
            skipped_steps=guarded.get("skipped_steps", []),
        )
        resolved = self._reconcile_global_reasoning_catalog(guarded)
        return {
            "resolved": resolved,
            "accepted_metadata_change_count": len(accepted_metadata_changes),
            "accepted_metadata_changes": accepted_metadata_changes,
            "rejected_step_label_change_count": len(rejected_changes),
            "rejected_step_label_changes": rejected_changes,
        }

    def _action_for_trace_step(
        self,
        trace_actions: Dict[Tuple[str, int], str],
        trace_id: str,
        step_index: int,
    ) -> str:
        return trace_actions.get((trace_id, step_index), "navigate")

    def _local_transition_routes_for_identity_operation(
        self,
        normalized: Dict[str, Any],
        operation: Dict[str, Any],
        trace_actions: Dict[Tuple[str, int], str],
    ) -> List[Dict[str, Any]]:
        trace_id = str(operation.get("trace_id", "")).strip()
        try:
            patched_step_index = int(operation.get("step_index", -1))
        except Exception:
            return []
        if not trace_id or patched_step_index < 0:
            return []
        ordered = self._group_step_labels_by_trace(normalized).get(trace_id, [])
        current_pos = next(
            (idx for idx, label in enumerate(ordered) if int(label.get("step_index", -1)) == patched_step_index),
            -1,
        )
        if current_pos < 0:
            return []
        routes = []
        for from_pos, to_pos in ((current_pos - 1, current_pos), (current_pos, current_pos + 1)):
            if from_pos < 0 or to_pos >= len(ordered):
                continue
            from_label = ordered[from_pos]
            to_label = ordered[to_pos]
            from_cluster = str(from_label.get("page_cluster_id", "")).strip()
            to_cluster = str(to_label.get("page_cluster_id", "")).strip()
            if not from_cluster or not to_cluster or from_cluster == to_cluster:
                continue
            evidence_step_index = int(from_label.get("step_index", -1))
            trigger = self._action_for_trace_step(trace_actions, trace_id, evidence_step_index)
            if self._action_blocks_route_transition(trigger):
                continue
            routes.append({
                "from_page_cluster_id": from_cluster,
                "to_page_cluster_id": to_cluster,
                "trigger": trigger,
                "evidence_steps": [f"{trace_id}:{evidence_step_index}"],
                "confidence": 0.75,
            })
        return routes

    def _route_evidence_keys(self, route: Dict[str, Any]) -> set:
        evidence_steps = route.get("evidence_steps", [])
        if not isinstance(evidence_steps, list):
            return set()
        return {str(item).strip() for item in evidence_steps if str(item).strip()}

    def _patch_identity_routes_locally(
        self,
        source_normalized: Dict[str, Any],
        patched_normalized: Dict[str, Any],
        operations: List[Dict[str, Any]],
        traces: Optional[List[TraceData]] = None,
    ) -> Dict[str, Any]:
        source_labels = self._group_step_labels_by_trace(source_normalized)
        patched_labels = self._group_step_labels_by_trace(patched_normalized)
        trace_actions = self._trace_action_lookup(traces)
        affected_triplets = set()
        for operation in operations:
            trace_id = str(operation.get("trace_id", "")).strip()
            try:
                step_index = int(operation.get("step_index", -1))
            except Exception:
                continue
            before = source_labels.get(trace_id, [])
            current_pos = next(
                (idx for idx, label in enumerate(before) if int(label.get("step_index", -1)) == step_index),
                -1,
            )
            if current_pos < 0:
                continue
            for from_pos, to_pos in ((current_pos - 1, current_pos), (current_pos, current_pos + 1)):
                if from_pos < 0 or to_pos >= len(before):
                    continue
                from_label = before[from_pos]
                to_label = before[to_pos]
                from_cluster = str(from_label.get("page_cluster_id", "")).strip()
                to_cluster = str(to_label.get("page_cluster_id", "")).strip()
                if not from_cluster or not to_cluster:
                    continue
                evidence_step = int(from_label.get("step_index", -1))
                affected_triplets.add((trace_id, evidence_step, from_cluster, to_cluster))

        retained = []
        removed = []
        for route in source_normalized.get("canonical_routes", []):
            if not isinstance(route, dict):
                continue
            evidence = sorted(self._route_evidence_keys(route))
            evidence_step = evidence[0] if evidence else ""
            trace_id = evidence_step.rsplit(":", 1)[0] if ":" in evidence_step else ""
            key = (
                trace_id,
                int(evidence_step.rsplit(":", 1)[1]) if ":" in evidence_step else -1,
                str(route.get("from_page_cluster_id", "")).strip(),
                str(route.get("to_page_cluster_id", "")).strip(),
            )
            if key in affected_triplets:
                removed.append(route)
            else:
                retained.append(deepcopy(route))

        added = []
        seen = {
            (
                str(route.get("from_page_cluster_id", "")).strip(),
                str(route.get("to_page_cluster_id", "")).strip(),
                tuple(sorted(self._route_evidence_keys(route))),
            )
            for route in retained
        }
        for operation in operations:
            for route in self._local_transition_routes_for_identity_operation(
                patched_normalized,
                operation,
                trace_actions,
            ):
                key = (
                    str(route.get("from_page_cluster_id", "")).strip(),
                    str(route.get("to_page_cluster_id", "")).strip(),
                    tuple(sorted(self._route_evidence_keys(route))),
                )
                if key in seen:
                    continue
                retained.append(route)
                added.append(route)
                seen.add(key)

        patched_normalized["canonical_routes"] = retained
        return {
            "removed_routes": removed,
            "added_routes": added,
            "affected_triplets": [
                {
                    "trace_id": trace_id,
                    "evidence_step_index": step_index,
                    "from_cluster": from_cluster,
                    "to_cluster": to_cluster,
                }
                for trace_id, step_index, from_cluster, to_cluster in sorted(affected_triplets)
            ],
        }

    def _build_identity_fix_prompt_from_judge_mismatches(
        self,
        mismatches: List[Dict[str, Any]],
    ) -> str:
        return (
            "You previously returned a global reasoning result that has high-confidence "
            "trace-wise page-identity mismatches. Regenerate a COMPLETE corrected global "
            "reasoning result, not a partial patch. You must return a full JSON object "
            "with app_type, step_labels, canonical_pages, canonical_routes, "
            "and task_related_elements. Preserve all steps, but you may "
            "reassign clusters, rebuild canonical_pages, and rebuild canonical_routes so "
            "the full result is internally consistent. A step whose current screenshot "
            "identity clearly indicates a different stable page must not remain in the "
            "old cluster. Use the trace_task and evidence fields as context for the "
            "current page identity, but do not add deterministic rule-based assumptions.\n\n"
            f"Trace-wise judge mismatches:\n{json.dumps(mismatches, ensure_ascii=False, indent=2)}"
        )

    def _detect_identity_mismatch_steps(
        self,
        normalized: Dict[str, Any],
        observations: List[FrameObservation],
    ) -> List[Dict[str, Any]]:
        obs_map = {self._obs_key(o): o for o in observations}
        page_map = {
            str(page.get("page_cluster_id", "")).strip(): page
            for page in normalized.get("canonical_pages", [])
            if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
        }
        mismatches: List[Dict[str, Any]] = []
        for label in normalized.get("step_labels", []):
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            cluster_id = str(label.get("page_cluster_id", "")).strip()
            obs = obs_map.get(f"{trace_id}:{step_index}")
            page = page_map.get(cluster_id, {})
            if not obs or not page:
                continue
            if float(getattr(obs, "identity_confidence", 0.0) or 0.0) < 0.75:
                continue
            obs_shell = str(getattr(obs, "shell_section", "") or "").strip().lower()
            obs_header = str(getattr(obs, "header_pattern", "") or "").strip().lower()
            obs_body = str(getattr(obs, "body_pattern", "") or "").strip().lower()
            obs_sticky = str(getattr(obs, "sticky_action_pattern", "") or "").strip().lower()
            page_identity = " ".join([
                str(page.get("page_name", "")),
                str(page.get("description", "")),
                str(page.get("route_path", "")),
            ]).lower()
            page_is_home = any(token in page_identity for token in ("推荐", "首页", "feed", "home", "/home"))
            me_signature = (
                obs_shell == "me"
                and obs_body == "profile_dashboard"
                and obs_sticky == "bottom_tab_bar"
                and obs_header == "avatar_stats_header"
            )
            if me_signature and page_is_home:
                mismatches.append({
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "current_cluster_id": cluster_id,
                    "obs_header_pattern": obs_header,
                    "obs_shell_section": obs_shell,
                    "obs_body_pattern": obs_body,
                    "obs_sticky_action_pattern": obs_sticky,
                    "obs_identity_confidence": getattr(obs, "identity_confidence", 0.0),
                    "page_name": page.get("page_name", ""),
                    "page_description": page.get("description", ""),
                })
        return mismatches

    async def _run_page_level_reasoning_fix(
        self,
        result: Dict[str, Any],
        output_dir: str,
    ) -> Dict[str, Any]:
        config_manager = getattr(self, "config_manager", None)
        if not output_dir or config_manager is None:
            raise RuntimeError(
                "[Global Reasoning] page-level fix cannot run: output_dir/config_manager missing"
            )

        output_dir_path = Path(output_dir).resolve()
        debug_dir = Path(str(self.pom.debug("global_reasoning")))
        trace_input_dir = str(config_manager.get("trace_input_dir", "") or "").strip()
        if not trace_input_dir:
            raise RuntimeError(
                "[Global Reasoning] page-level fix cannot run: trace_input_dir not configured"
            )
        trace_root = Path(trace_input_dir).expanduser().resolve()
        if not trace_root.exists():
            raise RuntimeError(
                f"[Global Reasoning] page-level fix cannot run: trace_input_dir does not exist: {trace_root}"
            )

        primary_path = debug_dir / "page_level_fix.json"
        legacy_path = debug_dir / "global_reasoning_page_assignment_audited.json"
        reports_dir = Path(str(self.pom.debug("global_reasoning", "page_assignment_audit")))
        reports_dir.mkdir(parents=True, exist_ok=True)

        model = str(
            config_manager.get(
                "global_reasoning_page_assignment_audit_model",
                config_manager.get("model", ""),
            )
            or ""
        ).strip() or None
        reasoning_effort = str(
            config_manager.get(
                "global_reasoning_page_assignment_audit_reasoning_effort",
                config_manager.get("global_reasoning_reasoning_effort", "medium"),
            )
            or "medium"
        ).strip()
        batch_size = int(
            config_manager.get("global_reasoning_page_assignment_audit_batch_size", 32)
            or 32
        )
        pair_budget = int(
            config_manager.get("global_reasoning_page_assignment_audit_pair_budget", 256)
            or 256
        )
        max_concurrent = int(config_manager.get("max_concurrent", 1) or 1)
        evidence_mode = str(
            config_manager.get(
                "global_reasoning_page_assignment_audit_evidence_mode",
                "phase1a_obs",
            )
            or "phase1a_obs"
        ).strip()
        if evidence_mode not in {"phase1a_obs", "image"}:
            raise RuntimeError(
                "[Global Reasoning] page-level fix cannot run: "
                f"unsupported global_reasoning_page_assignment_audit_evidence_mode={evidence_mode!r}"
            )
        page_identity_enabled = bool(
            config_manager.get("global_reasoning_page_identity_reconciliation_enabled", True)
        )
        page_identity_retrieval_mode = str(
            config_manager.get("global_reasoning_page_identity_retrieval_mode", "embedding")
            or "embedding"
        ).strip()
        page_identity_max_candidates = int(
            config_manager.get("global_reasoning_page_identity_max_candidates", 8)
            or 8
        )
        page_identity_top_k = int(
            config_manager.get("global_reasoning_page_identity_top_k", 6)
            or 6
        )
        page_identity_context_budget = int(
            config_manager.get("global_reasoning_page_identity_context_budget", 24000)
            or 24000
        )
        page_identity_image_token_cost = int(
            config_manager.get("global_reasoning_page_identity_image_token_cost", 1800)
            or 1800
        )
        page_identity_output_tokens_per_item = int(
            config_manager.get("global_reasoning_page_identity_output_tokens_per_item", 220)
            or 220
        )
        page_identity_merge_similarity_threshold = float(
            config_manager.get("global_reasoning_page_identity_merge_similarity_threshold", 0.83)
            or 0.83
        )
        page_identity_merge_composite_threshold = float(
            config_manager.get("global_reasoning_page_identity_merge_composite_threshold", 0.68)
            or 0.68
        )

        if self.logger:
            self.logger.log_info(
                "[Global Reasoning] Stage 3 (Page Identity Reconciliation): "
                f"running StepPageAssignmentReview evidence_mode={evidence_mode} "
                f"shared_index_enabled={page_identity_enabled} -> {legacy_path}"
            )
        try:
            from trace2world_pipeline.app_spec.reconcile.page_assignment_audit import (
                run_page_assignment_audit,
            )

            audit_result = await run_page_assignment_audit(
                result_root=output_dir_path,
                trace_root=trace_root,
                reports_dir=reports_dir,
                reasoning=result,
                all_pages=True,
                batch_size=max(1, batch_size),
                pair_budget=max(0, pair_budget),
                max_concurrent=max_concurrent,
                reasoning_effort=reasoning_effort,
                model=model or str(
                    config_manager.get("model", "")
                    or config_manager.get("deployment", "")
                    or "gpt-5.2-2025-12-11"
                ),
                evidence_mode=evidence_mode,
                enable_page_identity_reconciliation=page_identity_enabled,
                page_identity_retrieval_mode=page_identity_retrieval_mode,
                page_identity_max_candidates=page_identity_max_candidates,
                page_identity_top_k=page_identity_top_k,
                page_identity_context_budget=page_identity_context_budget,
                page_identity_image_token_cost=page_identity_image_token_cost,
                page_identity_output_tokens_per_item=page_identity_output_tokens_per_item,
                page_identity_merge_similarity_threshold=page_identity_merge_similarity_threshold,
                page_identity_merge_composite_threshold=page_identity_merge_composite_threshold,
                patched_reasoning_json=legacy_path,
                reasoning_label="<in_memory:global_reasoning_page_assignment_input>",
            )
        except Exception as exc:
            raise RuntimeError(f"[Global Reasoning] page-level fix failed: {exc}") from exc

        patched = audit_result.get("patched_reasoning") if isinstance(audit_result, dict) else None
        if not isinstance(patched, dict):
            raise RuntimeError(
                "[Global Reasoning] page-level fix produced no patched reasoning"
            )
        patch_report = (
            audit_result.get("patch_report", {})
            if isinstance(audit_result, dict)
            else {}
        )
        retained_observation_blocks = [
            item
            for item in patch_report.get("blocked", [])
            if isinstance(item, dict)
            and item.get("reason") == "retained_terminal_observation_skip_forbidden"
        ]
        if retained_observation_blocks:
            raise RuntimeError(
                "[Global Reasoning] page-level fix attempted to skip retained terminal "
                f"observations: {json.dumps(retained_observation_blocks, ensure_ascii=False)}"
            )

        try:
            primary_path.parent.mkdir(parents=True, exist_ok=True)
            primary_path.write_text(
                json.dumps(patched, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            raise RuntimeError(
                f"[Global Reasoning] page-level fix failed to write {primary_path}: {exc}"
            ) from exc

        diagnostics = {
            "stage_suffix": "page_level_fix",
            "source_path": "<in_memory:global_reasoning_page_assignment_input>",
            "patched_path": str(primary_path),
            "legacy_path": str(legacy_path),
            "reports_dir": str(reports_dir),
            "pre_fix_summary": {
                "pages": len(result.get("canonical_pages", []) or []),
                "routes": len(result.get("canonical_routes", []) or []),
                "step_labels": len(result.get("step_labels", []) or []),
            },
            "post_fix_summary": {
                "pages": len(patched.get("canonical_pages", []) or []),
                "routes": len(patched.get("canonical_routes", []) or []),
                "step_labels": len(patched.get("step_labels", []) or []),
            },
        }
        if hasattr(self, "_save_debug_page_level_fix"):
            self._save_debug_page_level_fix(diagnostics, output_dir)

        if self.logger:
            self.logger.log_info(
                f"[Global Reasoning] Stage 3 done: pages "
                f"{diagnostics['pre_fix_summary']['pages']} -> {diagnostics['post_fix_summary']['pages']}, "
                f"routes {diagnostics['pre_fix_summary']['routes']} -> {diagnostics['post_fix_summary']['routes']}"
            )
        return patched

    async def _run_identity_fix_loop(
        self,
        payload: Dict[str, Any],
        result: Dict[str, Any],
        observations: List[FrameObservation],
        stage_suffix: str,
        output_dir: str = "",
        traces: Optional[List[TraceData]] = None,
    ) -> Dict[str, Any]:
        return await self._run_trace_level_reasoning_fix(
            payload,
            result,
            observations,
            stage_suffix,
            output_dir=output_dir,
            traces=traces,
        )

    async def _run_sprite_merge_admission(
        self,
        result: Dict[str, Any],
        output_dir: str,
        observations: List[Any],
    ) -> Optional[Dict[str, Any]]:
        from PIL import Image, ImageDraw, ImageFont
        from trace2world_pipeline.core.llm_caller import call_openai_api_json_async, encode_image
        from collections import defaultdict as _defaultdict

        canonical_pages = result.get("canonical_pages", [])
        step_labels = result.get("step_labels", [])
        canonical_routes = result.get("canonical_routes", [])
        if not canonical_pages or len(canonical_pages) <= 1:
            return None

        config_manager = getattr(self, "config_manager", None)
        trace_input_dir = ""
        if config_manager:
            trace_input_dir = str(config_manager.get("trace_input_dir", "") or "").strip()


        page_steps: Dict[str, List[Dict[str, Any]]] = {}
        for label in step_labels:
            pid = str(label.get("page_cluster_id", "")).strip()
            if pid:
                page_steps.setdefault(pid, []).append(label)

        inbound_count: Dict[str, int] = {}
        inbound_set: Dict[str, set] = _defaultdict(set)
        outbound_set: Dict[str, set] = _defaultdict(set)
        for route in canonical_routes:
            fr = str(route.get("from_page_cluster_id", route.get("from", ""))).strip()
            to = str(route.get("to_page_cluster_id", route.get("to", ""))).strip()
            if fr and to:
                inbound_count[to] = inbound_count.get(to, 0) + 1
                inbound_set[to].add(fr)
                outbound_set[fr].add(to)

        pages_map: Dict[str, Dict[str, Any]] = {}
        page_info: List[Dict[str, Any]] = []
        for page in canonical_pages:
            pid = str(page.get("page_cluster_id", "")).strip()
            if not pid:
                continue
            steps = page_steps.get(pid, [])
            screenshot_path = ""
            if steps and trace_input_dir:
                first = steps[0]
                tid = str(first.get("trace_id", "")).strip()
                si = int(first.get("step_index", 0))
                for stem in (f"action_{si:04d}", f"step_{si:04d}"):
                    for ext in (".jpg", ".jpeg", ".png"):
                        candidate = os.path.join(trace_input_dir, tid, "screenshots", stem + ext)
                        if os.path.exists(candidate):
                            screenshot_path = candidate
                            break
                    if screenshot_path:
                        break
            info = {
                "page_cluster_id": pid,
                "page_name": str(page.get("page_name", pid)),
                "description": str(page.get("description", "")),
                "page_type_hint": str(page.get("page_type_hint", "other")),
                "evidence": len(steps),
                "inbound_routes": inbound_count.get(pid, 0),
                "screenshot_path": screenshot_path,
            }
            page_info.append(info)
            pages_map[pid] = info

        if len(page_info) < 2:
            return None


        page_identity_debug = (
            result.get("debug", {}).get("page_identity_reconciliation", {})
            if isinstance(result.get("debug", {}), dict)
            else {}
        )
        shared_merge_clusters = page_identity_debug.get("merge_candidate_clusters", {})
        shared_index_id = str(page_identity_debug.get("shared_index_id", "") or "").strip()
        edges = []
        if isinstance(shared_merge_clusters, dict) and shared_index_id:
            for edge in shared_merge_clusters.get("edges", []) or []:
                if not isinstance(edge, dict):
                    continue
                a = str(edge.get("from", "") or "").strip()
                b = str(edge.get("to", "") or "").strip()
                composite = float(edge.get("composite", 0.0) or 0.0)
                if a and b:
                    edges.append((a, b, composite))
            if self.logger:
                self.logger.log_info(
                    f"[Sprite Merge] Using shared PageIdentityIndex {shared_index_id} "
                    f"candidate_edges={len(edges)}"
                )
        else:

            try:
                from sentence_transformers import SentenceTransformer
                import numpy as np
                embed_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
                texts = [f"{p['page_name']}：{p['description']}" for p in page_info]
                embeddings = embed_model.encode(texts, normalize_embeddings=True)
                sim_matrix = embeddings @ embeddings.T
            except Exception as exc:
                if self.logger:
                    self.logger.log_warning(f"[Sprite Merge] Embedding failed: {exc}, skipping")
                return None

            _TYPE_EQUIV = {"homepage": 0, "listing": 0, "landing": 0, "detail": 1, "form": 2,
                           "search_results": 3, "search_entry": 4}
            _EMB_THRESHOLD = 0.83
            _COMPOSITE_THRESHOLD = 0.6

            n = len(page_info)
            for i in range(n):
                for j in range(i + 1, n):
                    emb_sim = float(sim_matrix[i][j])
                    if emb_sim < _EMB_THRESHOLD:
                        continue
                    a, b = page_info[i]["page_cluster_id"], page_info[j]["page_cluster_id"]
                    if b in outbound_set.get(a, set()) or a in outbound_set.get(b, set()):
                        continue
                    ia, ib = inbound_set.get(a, set()), inbound_set.get(b, set())
                    inb_overlap = len(ia & ib) / len(ia | ib) if ia | ib else 0.0
                    ta = _TYPE_EQUIV.get(page_info[i]["page_type_hint"], -1)
                    tb = _TYPE_EQUIV.get(page_info[j]["page_type_hint"], -2)
                    type_eq = 1.0 if (ta == tb and ta >= 0) else 0.0
                    composite = emb_sim * 0.5 + inb_overlap * 0.3 + type_eq * 0.2
                    if composite >= _COMPOSITE_THRESHOLD:
                        edges.append((a, b, composite))

        if not edges:
            if self.logger:
                self.logger.log_info("[Sprite Merge] No candidate pairs above threshold")
            return None


        adj: Dict[str, set] = _defaultdict(set)
        for a, b, _ in edges:
            adj[a].add(b)
            adj[b].add(a)
        visited: set = set()
        clusters: List[List[str]] = []
        def _dfs(node, comp):
            if node in visited:
                return
            visited.add(node)
            comp.append(node)
            for nb in adj[node]:
                _dfs(nb, comp)
        for p in set(adj.keys()):
            if p not in visited:
                comp: list = []
                _dfs(p, comp)
                clusters.append(sorted(comp))
        clusters.sort(key=len, reverse=True)

        if self.logger:
            self.logger.log_info(
                f"[Sprite Merge] {len(edges)} candidate edges → {len(clusters)} clusters "
                f"(sizes: {[len(c) for c in clusters]}), {len(page_info)} total pages"
            )


        _CELL_W, _CELL_H, _LABEL_H, _PAD, _COLS = 360, 780, 50, 8, 5

        def _get_font(size):
            for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                       "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]:
                try:
                    return ImageFont.truetype(fp, size)
                except (OSError, IOError):
                    continue
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()

        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        debug_dir = str(pom.debug("global_reasoning", "page_wise_judge"))
        os.makedirs(debug_dir, exist_ok=True)

        def _build_cluster_sprite(cluster_pids, idx):
            infos = [pages_map[pid] for pid in cluster_pids if pid in pages_map]
            nc = len(infos)
            cols = min(nc, _COLS)
            rows = (nc + cols - 1) // cols
            cw = cols * (_CELL_W + _PAD) + _PAD
            ch = rows * (_CELL_H + _LABEL_H + _PAD) + _PAD
            canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
            draw = ImageDraw.Draw(canvas)
            font = _get_font(20)
            for pos, info in enumerate(infos):
                col = pos % cols
                row = pos // cols
                x = _PAD + col * (_CELL_W + _PAD)
                y = _PAD + row * (_CELL_H + _LABEL_H + _PAD)
                if info["screenshot_path"] and os.path.exists(info["screenshot_path"]):
                    img = Image.open(info["screenshot_path"]).convert("RGB")
                    scale = _CELL_W / img.width
                    img = img.resize((_CELL_W, max(1, int(round(img.height * scale)))), Image.LANCZOS)
                    if img.height > _CELL_H:
                        img = img.crop((0, 0, _CELL_W, _CELL_H))
                    canvas.paste(img, (x + (_CELL_W - img.width) // 2, y + (_CELL_H - img.height) // 2))
                draw.text((x + 4, y + _CELL_H + 4), info["page_cluster_id"][:30], fill=(0, 0, 0), font=font)
                draw.text((x + 4, y + _CELL_H + 26), f"ev={info['evidence']} in={info['inbound_routes']}", fill=(80, 80, 80), font=font)
            path = os.path.join(debug_dir, f"cluster_{idx:02d}.jpg")
            canvas.save(path, "JPEG", quality=85)
            return path, infos

        sprite_data = [_build_cluster_sprite(cl, i) for i, cl in enumerate(clusters)]


        _MERGE_PROMPT = """You are reviewing canonical pages from a mobile app AppSpec for over-split errors.
The sprite shows one representative screenshot per page, labeled with page_cluster_id.

Page list:
{page_list}

Find pages that should be MERGED (they are the same page split under different names).

Merge criteria:
- DUPLICATE: Same page structure, header, content layout — just different names from different reasoning chunks
- OVERLAY: One page is a bottom sheet/modal over another
- TAB STATE: Same page shell, different active tab
- SCROLL STATE: Same page at different scroll positions

Do NOT merge pages that are genuinely different (different headers, different content types, different route-level purposes).

Merge direction: victim must have FEWER evidence steps than target.

Return JSON: {{"merge_proposals": [{{"victim_page_id": "...", "target_page_id": "...", "reason": "...", "variant_type": "..."}}], "reasoning": "..."}}
If no merges needed, return {{"merge_proposals": [], "reasoning": "..."}}"""

        model = None
        reasoning_effort = "medium"
        if config_manager:
            model = str(config_manager.get("global_reasoning_page_assignment_audit_model", config_manager.get("model", "")) or "").strip() or None
            reasoning_effort = str(config_manager.get("global_reasoning_page_assignment_audit_reasoning_effort", "medium") or "medium")

        async def _call_cluster(sprite_path, infos, idx):
            page_list = "\n".join(
                f"- {p['page_cluster_id']}: {p['page_name']} (evidence={p['evidence']}, inbound={p['inbound_routes']})"
                for p in infos
            )
            prompt = _MERGE_PROMPT.format(page_list=page_list)
            messages = [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": encode_image(sprite_path)},
            ]}]
            try:
                reply, usage = await call_openai_api_json_async(
                    messages, model=model, reasoning_effort=reasoning_effort,
                    stage="sprite_page_merge_admission", max_tokens=4000,
                )
            except Exception as exc:
                if self.logger:
                    self.logger.log_warning(f"[Sprite Merge] Cluster {idx} LLM failed: {exc}")
                return []
            if not reply:
                return []
            try:
                return json.loads(reply).get("merge_proposals", [])
            except (json.JSONDecodeError, TypeError):
                return []

        all_tasks = [_call_cluster(sp, infos, i) for i, (sp, infos) in enumerate(sprite_data)]
        cluster_results = await asyncio.gather(*all_tasks)
        merge_proposals = [p for batch in cluster_results for p in batch]


        debug_payload = {
            "format": "sprite_page_merge_admission_v2",
            "page_count": len(page_info),
            "cluster_count": len(clusters),
            "cluster_sizes": [len(c) for c in clusters],
            "candidate_edge_count": len(edges),
            "merge_proposals": merge_proposals,
        }
        try:
            with open(os.path.join(debug_dir, "page_merge_admission.json"), "w", encoding="utf-8") as f:
                json.dump(debug_payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        if not merge_proposals:
            if self.logger:
                self.logger.log_info("[Sprite Merge] No merges proposed")
            return None


        patched = deepcopy(result)
        victims = set()
        remap: Dict[str, str] = {}
        for proposal in merge_proposals:
            victim = str(proposal.get("victim_page_id", "")).strip()
            target = str(proposal.get("target_page_id", "")).strip()
            if not victim or not target or victim == target:
                continue
            victim_exists = any(str(p.get("page_cluster_id", "")).strip() == victim for p in patched["canonical_pages"])
            target_exists = any(str(p.get("page_cluster_id", "")).strip() == target for p in patched["canonical_pages"])
            if not victim_exists or not target_exists:
                continue
            victims.add(victim)
            remap[victim] = target

        if not remap:
            return None

        for label in patched.get("step_labels", []):
            pid = str(label.get("page_cluster_id", "")).strip()
            if pid in remap:
                label["page_cluster_id"] = remap[pid]

        patched["canonical_pages"] = [
            p for p in patched["canonical_pages"]
            if str(p.get("page_cluster_id", "")).strip() not in victims
        ]

        new_routes = []
        for route in patched.get("canonical_routes", []):
            from_id = str(route.get("from_page_cluster_id", route.get("from", ""))).strip()
            to_id = str(route.get("to_page_cluster_id", route.get("to", ""))).strip()
            if from_id in victims:
                continue
            if to_id in remap:
                route = dict(route)
                route["to_page_cluster_id"] = remap[to_id]
                if "to" in route:
                    route["to"] = remap[to_id]
            new_routes.append(route)
        patched["canonical_routes"] = new_routes

        if self.logger:
            for proposal in merge_proposals:
                v = proposal.get("victim_page_id", "")
                t = proposal.get("target_page_id", "")
                vt = proposal.get("variant_type", "")
                if v in remap:
                    self.logger.log_info(f"[Sprite Merge] Merged {v} → {t} ({vt})")
            self.logger.log_info(
                f"[Sprite Merge] {len(remap)} merges applied, "
                f"{len(patched['canonical_pages'])} pages remaining"
            )

        return patched

    async def _run_trace_level_reasoning_fix(
        self,
        payload: Dict[str, Any],
        result: Dict[str, Any],
        observations: List[FrameObservation],
        stage_suffix: str,
        output_dir: str = "",
        traces: Optional[List[TraceData]] = None,
    ) -> Dict[str, Any]:
        normalized = self._normalize_global_reasoning_result(result, observations)
        diagnostics = {
            "stage_suffix": stage_suffix,
            "identity_fix_triggered": False,
            "identity_mismatches": [],
            "identity_judge_summary": {},
            "pre_fix_summary": {
                "pages": len(normalized.get("canonical_pages", [])),
                "routes": len(normalized.get("canonical_routes", [])),
                "step_labels": len(normalized.get("step_labels", [])),
            },
        }
        if not normalized.get("canonical_pages") or not normalized.get("step_labels"):
            diagnostics["skip_reason"] = "empty_normalized_result"
            if output_dir:
                self._save_debug_global_reasoning_fixloop(diagnostics, output_dir)
            return result

        trace_ids = self._select_identity_judge_trace_ids(normalized)
        if not trace_ids:
            diagnostics["skip_reason"] = "no_identity_judge_traces"
            if output_dir:
                self._save_trace_identity_judge_debug(output_dir, [], {
                    "judged_trace_count": 0,
                    "failed_trace_count": 0,
                    "mismatch_count": 0,
                    "fixloop_mismatch_count": 0,
                    "diagnostic_mismatch_count": 0,
                    "fixloop_mismatches": [],
                    "diagnostic_mismatches": [],
                    "token_usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
                    "skip_reason": "no_identity_judge_traces",
                })
                self._save_debug_global_reasoning_fixloop(diagnostics, output_dir)
            return result

        trace_tasks = self._trace_tasks_from_payload(payload)
        judge_payloads = [
            self._build_trace_identity_judge_payload(
                trace_id=trace_id,
                normalized=normalized,
                observations=observations,
                traces=traces,
                trace_tasks=trace_tasks,
            )
            for trace_id in trace_ids
        ]
        tasks = [self._run_trace_identity_judge(judge_payload) for judge_payload in judge_payloads]
        raw_judge_results = await asyncio.gather(*tasks, return_exceptions=True)
        judge_results: List[Dict[str, Any]] = []
        for trace_id, judge_result in zip(trace_ids, raw_judge_results):
            if isinstance(judge_result, BaseException):
                judge_results.append(self._failed_trace_identity_judge_result(trace_id, judge_result))
            elif isinstance(judge_result, dict):
                judge_results.append(judge_result)
            else:
                judge_results.append(
                    self._failed_trace_identity_judge_result(
                        trace_id,
                        TypeError(f"unexpected judge result type: {type(judge_result).__name__}"),
                    )
                )

        judge_summary = self._aggregate_trace_identity_judge_results(judge_results)
        diagnostics["identity_judge_summary"] = judge_summary
        mismatches = judge_summary.get("fixloop_mismatches", [])
        diagnostics["identity_mismatches"] = mismatches
        if output_dir:
            self._save_trace_identity_judge_debug(output_dir, judge_results, judge_summary, judge_payloads)
        if not mismatches:
            diagnostics["skip_reason"] = "no_high_confidence_identity_judge_mismatches"
            if output_dir:
                self._save_debug_global_reasoning_fixloop(diagnostics, output_dir)
            return result
        diagnostics["identity_fix_triggered"] = True
        if self.logger:
            if hasattr(self.logger, "log_info"):
                self.logger.log_info(f"[Global Reasoning] Identity patch fix triggered: {len(mismatches)} mismatches")
            elif hasattr(self.logger, "info"):
                self.logger.info(f"[Global Reasoning] Identity patch fix triggered: {len(mismatches)} mismatches")
        patch_result = self._apply_identity_judge_reassignments(normalized, mismatches)
        fixed_normalized = patch_result["patched"]
        route_patch = self._patch_identity_routes_locally(
            normalized,
            fixed_normalized,
            patch_result["operations"],
            traces=traces,
        )
        diagnostics["patch_operations"] = patch_result["operations"]
        diagnostics["patch_skipped"] = patch_result["skipped"]
        diagnostics["route_patch"] = {
            "removed_route_count": len(route_patch["removed_routes"]),
            "added_route_count": len(route_patch["added_routes"]),
            "affected_triplets": route_patch["affected_triplets"],
        }
        issues = _detect_schema_issues(fixed_normalized)
        diagnostics["schema_fix_issues_after_identity_fix"] = issues
        if issues:
            diagnostics["result_source"] = "original_due_to_patch_schema_issues"
            if output_dir:
                self._save_debug_global_reasoning_fixloop(diagnostics, output_dir)
            return result
        diagnostics["post_fix_summary"] = {
            "pages": len(fixed_normalized.get("canonical_pages", [])),
            "routes": len(fixed_normalized.get("canonical_routes", [])),
            "step_labels": len(fixed_normalized.get("step_labels", [])),
        }
        diagnostics["result_source"] = "identity_patch_fix"
        if output_dir:
            self._save_debug_global_reasoning_fixloop(diagnostics, output_dir)
        return fixed_normalized

    async def _run_global_reasoning(
        self, payload: Dict[str, Any], observations: List[FrameObservation], stage_suffix: str = "full",
        max_fix_rounds: int = 2, output_dir: str = "", traces: Optional[List[TraceData]] = None,
        run_identity_fix: bool = True,
    ) -> Dict[str, Any]:
        prompt = self._build_global_reasoning_prompt(payload)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        result, _usage = await call_openai_api_json_async(
            messages,
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage=f"site_model_global_reasoning_{stage_suffix}"
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            return {}


        for round_num in range(max_fix_rounds):
            issues = _detect_schema_issues(result)
            if not issues:
                break
            self._log_global_reasoning_info(
                f"[Global Reasoning] Fix round {round_num + 1}: found {len(issues)} schema issues"
            )
            fix_messages = [
                {"role": "user", "content": messages[0]["content"]},
                {"role": "assistant", "content": json.dumps(result, ensure_ascii=False, indent=2)},
                {"role": "user", "content": GLOBAL_REASONING_FIX_PROMPT.format(
                    issues="\n".join(f"- {i}" for i, issue in enumerate(issues, 1))
                )}
            ]
            result, _ = await call_openai_api_json_async(
                fix_messages,
                reasoning_effort=self.reasoning_effort,
                model=self.model,
                stage=f"site_model_global_reasoning_{stage_suffix}_fix_{round_num + 1}"
            )
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                return {}

        if run_identity_fix and result.get("canonical_pages") and result.get("step_labels"):
            result = await self._run_identity_fix_loop(
                payload,
                result,
                observations=observations,
                stage_suffix=stage_suffix,
                output_dir=output_dir,
                traces=traces,
            )
        return result

    async def _run_global_reasoning_reduce(
        self,
        partial_results: List[Dict[str, Any]],
        payload: Dict[str, Any],
        max_fix_rounds: int = 2
    ) -> Dict[str, Any]:
        prompt = self._build_global_reasoning_reduce_prompt(partial_results, payload)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        result, _usage = await call_openai_api_json_async(
            messages,
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage="site_model_global_reasoning_reduce"
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            return {}


        for round_num in range(max_fix_rounds):
            issues = _detect_schema_issues(result)
            if not issues:
                break
            self._log_global_reasoning_info(
                f"[Global Reasoning reduce] Fix round {round_num + 1}: found {len(issues)} schema issues"
            )
            fix_messages = [
                {"role": "user", "content": messages[0]["content"]},
                {"role": "assistant", "content": json.dumps(result, ensure_ascii=False, indent=2)},
                {"role": "user", "content": GLOBAL_REASONING_FIX_PROMPT.format(
                    issues="\n".join(f"- {i}" for i, issue in enumerate(issues, 1))
                )}
            ]
            result, _ = await call_openai_api_json_async(
                fix_messages,
                reasoning_effort=self.reasoning_effort,
                model=self.model,
                stage=f"site_model_global_reasoning_reduce_fix_{round_num + 1}"
            )
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                return {}

        return result

    async def _merge_compact_partials_batched(
        self,
        partial_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        current = list(partial_results)
        round_index = 0
        audit_rounds: List[Dict[str, Any]] = []
        while len(current) > 1:

            merge_batches: List[Tuple[str, List[Dict[str, Any]]]] = []
            pass_through: List[Dict[str, Any]] = []
            for batch_index in range(0, len(current), self.global_reasoning_merge_batch_size):
                batch = current[batch_index:batch_index + self.global_reasoning_merge_batch_size]
                if len(batch) == 1:
                    pass_through.append(batch[0])
                else:
                    stage_suffix = f"round_{round_index:02d}_batch_{batch_index // self.global_reasoning_merge_batch_size:02d}"
                    merge_batches.append((stage_suffix, batch))


            next_round: List[Dict[str, Any]] = list(pass_through)
            round_audit: Dict[str, Any] = {"round_index": round_index, "batches": []}
            if merge_batches:
                merge_coros = [
                    self._run_compact_global_reasoning_merge(batch, stage_suffix)
                    for stage_suffix, batch in merge_batches
                ]
                raw_merges = await asyncio.gather(*merge_coros, return_exceptions=True)
                failed_merges: List[str] = []
                for (stage_suffix, batch), result in zip(merge_batches, raw_merges):
                    if isinstance(result, BaseException):
                        self._log_global_reasoning_info(
                            f"[Global Reasoning] merge {stage_suffix} failed: {result}"
                        )
                        failed_merges.append(f"{stage_suffix}: {result}")
                        round_audit["batches"].append({
                            "stage_suffix": stage_suffix,
                            "error": str(result),
                            "raw_merge_result": {},
                            "merge_result": {},
                            "mapping_coverage": {
                                "expected_mapping_count": 0,
                                "mapped_count": 0,
                                "missing_mapping_count": 0,
                                "extra_mapping_count": 0,
                            },
                        })
                        continue
                    merged = result
                    expanded_mapping = self._expand_compact_merge_mapping(batch, merged)
                    merged["_expanded_local_to_global"] = expanded_mapping
                    mapping_coverage = self._compact_merge_mapping_coverage(batch, merged)
                    round_audit["batches"].append({
                        "stage_suffix": stage_suffix,
                        "raw_merge_result": deepcopy(result),
                        "merge_result": deepcopy(merged),
                        "mapping_coverage": mapping_coverage,
                    })
                    source_fingerprints: Dict[str, str] = {}
                    for partial in batch:
                        if not isinstance(partial, dict):
                            continue
                        for cand in partial.get("page_candidates", []):
                            if isinstance(cand, dict) and cand.get("structure_fingerprint"):
                                qid = str(cand.get("qualified_local_page_id") or cand.get("local_page_id") or "")
                                lid = str(cand.get("local_page_id", "")).strip()
                                fp = str(cand["structure_fingerprint"])
                                if qid:
                                    source_fingerprints[qid] = fp
                                if lid:
                                    source_fingerprints[lid] = fp

                    merged_candidates = []
                    for page in merged.get("canonical_pages", []):
                        if not isinstance(page, dict) or not str(page.get("page_cluster_id", "")).strip():
                            continue
                        source_ids = self._source_ids_for_global_page(
                            batch, merged, str(page.get("page_cluster_id", "")).strip(),
                        )
                        fp = ""
                        for sid in source_ids:
                            fp = source_fingerprints.get(sid, "")
                            if fp:
                                break
                        candidate: Dict[str, Any] = {
                            "local_page_id": str(page.get("page_cluster_id", "")).replace("c_", "p_", 1),
                            "page_kind": page.get("page_type_hint", "utility"),
                            "page_name": page.get("page_name", ""),
                            "description": page.get("description", ""),
                            "primary_functions": page.get("primary_functions", []),
                            "route_path": page.get("route_path", ""),
                            "include_as_page": True,
                            "evidence_segments": [],
                            "state_variants": [],
                            "source_local_page_ids": source_ids,
                        }
                        if fp:
                            candidate["structure_fingerprint"] = fp
                        merged_candidates.append(candidate)

                    merged_partial = {
                        "chunk_id": f"merge_{stage_suffix}",
                        "app_type": merged.get("app_type", ""),
                        "page_candidates": merged_candidates,
                        "_merged_result": merged,
                    }
                    next_round.append(merged_partial)
                if failed_merges:
                    raise RuntimeError(
                        "global reasoning compact merge aborted: "
                        f"{len(failed_merges)}/{len(merge_batches)} merge batch(es) failed "
                        f"({'; '.join(failed_merges)}); cannot continue with pass-through partial results"
                    )
            current = next_round
            audit_rounds.append(round_audit)
            round_index += 1
        if len(partial_results) == 1:
            partial = partial_results[0]
            local_to_global = {}
            for page in partial.get("page_candidates", []):
                if not isinstance(page, dict):
                    continue
                local_page_id = str(page.get("local_page_id", "")).strip()
                if not local_page_id:
                    continue
                local_to_global[f"{partial.get('chunk_id', '')}:{local_page_id}"] = self._local_page_to_global({}, "", local_page_id)
            result = {
                "app_type": partial.get("app_type", ""),
                "local_to_global": local_to_global,
                "canonical_pages": [
                    self._page_from_candidate(global_id, page)
                    for page in partial.get("page_candidates", [])
                    if isinstance(page, dict)
                    for global_id in [self._local_page_to_global({}, "", str(page.get("local_page_id", "")).strip())]
                ],
                "task_related_elements": {},
            }
            result["_audit_rounds"] = []
            return result
        final_partial = current[0] if current else {}
        if isinstance(final_partial, dict) and isinstance(final_partial.get("_merged_result"), dict):
            merged = final_partial["_merged_result"]
            if isinstance(merged.get("_expanded_local_to_global"), dict):
                merged["local_to_global"] = merged["_expanded_local_to_global"]
            merged["_audit_rounds"] = audit_rounds
            return merged
        if current:
            merged = await self._run_compact_global_reasoning_merge(current, "final")
            expanded_mapping = self._expand_compact_merge_mapping(current, merged)
            if expanded_mapping:
                merged["local_to_global"] = expanded_mapping
            merged["_audit_rounds"] = audit_rounds
            return merged
        return {}

    async def _infer_global_reasoning_compact_hierarchical(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData],
        website_url: str
    ) -> Dict[str, Any]:
        hard_skips = self._build_hard_skip_map(observations, traces)
        hard_keeps = self._build_hard_keep_map(traces)
        compact_ir = self._build_compact_trace_ir(observations, traces, hard_skips, hard_keeps)
        self._save_compact_global_reasoning_debug("compact_trace_ir", compact_ir)
        trace_payloads = compact_ir.get("traces", [])
        if not isinstance(trace_payloads, list) or not trace_payloads:
            return self._fallback_global_reasoning(observations, traces)

        chunk_tasks: List[Tuple[str, Any]] = []
        chunk_payload_by_id: Dict[str, Dict[str, Any]] = {}
        chunk_max_concurrent = max(
            1,
            int(getattr(self, "max_concurrent", 1)),
        )
        chunk_retry_attempts = max(
            0,
            int(getattr(self, "global_reasoning_compact_chunk_retry_attempts", 1)),
        )
        chunk_semaphore = asyncio.Semaphore(chunk_max_concurrent)
        use_authority_namespace_budget = (
            getattr(self, "global_reasoning_logic_flow", "") == "compact_authority_namespace_budget"
        )
        use_phase1_semantic_balanced = (
            getattr(self, "global_reasoning_logic_flow", "") == "compact_phase1_semantic_balanced"
        )
        if use_phase1_semantic_balanced:
            target_budget = int(getattr(self, "global_reasoning_token_budget", 12000) or 12000)
            self._log_global_reasoning_info(
                f"[Global Reasoning] compact_phase1_semantic_balanced: "
                f"building producer chunks with token_budget={target_budget}"
            )
            try:
                producer_chunks, phase1_report = build_phase1_semantic_balanced_chunks_for_gr(
                    compact_ir=compact_ir,
                    frame_observations=observations,
                    target_budget_tokens=target_budget,
                    website_url=website_url,
                    hard_skips=compact_ir.get("hard_skips", []),
                )
            except RuntimeError as e:
                self._log_global_reasoning_info(
                    f"[Global Reasoning] compact_phase1_semantic_balanced HARD FAIL: {e}"
                )
                raise
            phase1_report.update({
                "compact_chunk_max_concurrent": chunk_max_concurrent,
                "compact_chunk_retry_attempts": chunk_retry_attempts,
            })
            self._save_compact_global_reasoning_debug(
                "producer_chunk_build_report",
                phase1_report,
            )
        elif use_authority_namespace_budget:
            observations_by_key = {self._obs_key(obs): obs for obs in observations}
            producer_chunks, producer_chunk_report = build_authority_namespace_budget_producer_chunks(
                compact_ir=compact_ir,
                observations_by_key=observations_by_key,
                website_url=website_url,
                target_input_tokens=int(getattr(self, "global_reasoning_token_budget", 60000) or 60000),
                hard_skips=compact_ir.get("hard_skips", []),
            )
            producer_chunk_report.update({
                "compact_chunk_max_concurrent": chunk_max_concurrent,
                "compact_chunk_retry_attempts": chunk_retry_attempts,
            })
            self._save_compact_global_reasoning_debug(
                "producer_chunk_build_report",
                producer_chunk_report,
            )
        else:
            balanced_trace_chunks = self._balance_compact_trace_chunks_by_steps(
                trace_payloads,
                self.global_reasoning_chunk_size,
            )
            producer_chunks = []
            for chunk_index, chunk_traces in enumerate(balanced_trace_chunks):
                chunk_id = f"chunk_{chunk_index:02d}"
                producer_chunks.append({
                    "format": "compact_trace_ir_chunk_v1",
                    "chunk_id": chunk_id,
                    "website_url": website_url,
                    "traces": chunk_traces,
                    "hard_skips": compact_ir.get("hard_skips", []),
                })
            self._save_compact_global_reasoning_debug(
                "chunk_balance_report",
                {
                    **self._compact_trace_chunk_balance_report(
                        balanced_trace_chunks,
                        self.global_reasoning_chunk_size,
                    ),
                    "compact_chunk_max_concurrent": chunk_max_concurrent,
                    "compact_chunk_retry_attempts": chunk_retry_attempts,
                },
            )
        async def run_chunk_payload(
            payload: Dict[str, Any],
            current_chunk_id: str,
        ) -> Tuple[str, Any]:
            async with chunk_semaphore:
                try:
                    result = await self._run_compact_global_reasoning_chunk(
                        payload,
                        current_chunk_id,
                    )
                    return current_chunk_id, result
                except BaseException as exc:
                    return current_chunk_id, exc

        for chunk_payload in producer_chunks:
            chunk_id = str(chunk_payload.get("chunk_id", "") or f"chunk_{len(chunk_payload_by_id):02d}")
            self._save_compact_global_reasoning_debug(f"{chunk_id}_input", chunk_payload)
            chunk_payload_by_id[chunk_id] = chunk_payload
            chunk_tasks.append((chunk_id, asyncio.create_task(run_chunk_payload(chunk_payload, chunk_id))))
        segment_fingerprints: Dict[str, str] = {}
        for trace_payload in trace_payloads:
            if not isinstance(trace_payload, dict):
                continue
            for seg in trace_payload.get("segments", []):
                if not isinstance(seg, dict):
                    continue
                seg_id = str(seg.get("segment_id", "")).strip()
                if seg_id:
                    hp = str(seg.get("header_pattern", "")).strip()
                    bp = str(seg.get("body_pattern", "")).strip()
                    segment_fingerprints[seg_id] = f"{hp}|{bp}" if hp or bp else ""

        partial_results: List[Dict[str, Any]] = []

        def accept_chunk_result(chunk_id: str, result: Any) -> bool:
            if isinstance(result, BaseException):
                self._log_global_reasoning_info(
                    f"[Global Reasoning] chunk {chunk_id} failed: {result}"
                )
                return False
            self._save_compact_global_reasoning_debug(chunk_id, result)
            if not result or not isinstance(result, dict):
                self._log_global_reasoning_info(
                    f"[Global Reasoning] chunk {chunk_id} returned empty/invalid result"
                )
                return False
            result = attach_page_identity_contracts_to_output(
                result,
                chunk_payload_by_id.get(chunk_id, {}),
            )
            assignments = result.get("segment_assignments", [])
            pages = result.get("page_candidates", [])
            if not assignments and not pages:
                self._log_global_reasoning_info(
                    f"[Global Reasoning] chunk {chunk_id} returned no pages and no assignments; treating as failed"
                )
                return False
            self._attach_structure_fingerprints(result, segment_fingerprints)
            partial_results.append(result)
            return True

        failed_chunks: List[str] = []
        for completed in asyncio.as_completed([task for _, task in chunk_tasks]):
            chunk_id, result = await completed
            if not accept_chunk_result(chunk_id, result):
                failed_chunks.append(chunk_id)

        for retry_index in range(1, chunk_retry_attempts + 1):
            if not failed_chunks:
                break
            retry_chunk_ids = list(failed_chunks)
            failed_chunks = []
            self._log_global_reasoning_info(
                f"[Global Reasoning] retrying failed compact chunk(s), "
                f"attempt {retry_index}/{chunk_retry_attempts}: {', '.join(retry_chunk_ids)}"
            )
            retry_results: List[Tuple[str, Any]] = []
            for chunk_id in retry_chunk_ids:
                if chunk_id not in chunk_payload_by_id:
                    continue
                try:
                    retry_results.append(await run_chunk_payload(chunk_payload_by_id[chunk_id], chunk_id))
                except BaseException as exc:
                    retry_results.append((chunk_id, exc))
            for chunk_id, result in retry_results:
                if not accept_chunk_result(chunk_id, result):
                    failed_chunks.append(chunk_id)
        if failed_chunks:
            raise RuntimeError(
                f"global reasoning aborted: {len(failed_chunks)}/{len(chunk_tasks)} chunk(s) failed "
                f"({', '.join(failed_chunks)}); cannot produce a complete AppSpec from partial results"
            )
        if not partial_results:
            raise RuntimeError(
                "global reasoning aborted: no compact producer partial results; "
                "fallback is disabled for compact hierarchical GR"
            )

        merge_result = await self._merge_compact_partials_batched(partial_results)
        self._save_compact_global_reasoning_debug("final_merge", merge_result)
        if not merge_result:
            raise RuntimeError(
                "global reasoning aborted: compact merge returned empty result; "
                "fallback is disabled for compact hierarchical GR"
            )
        if use_authority_namespace_budget or use_phase1_semantic_balanced:
            authority_report = self._compact_authority_report(
                compact_ir=compact_ir,
                producer_chunks=producer_chunks,
                partial_results=partial_results,
                merge_result=merge_result,
            )
            authority_verdict = compile_authority_verdict(authority_report)
            authority_report["authority_verdict"] = authority_verdict
            output_dir = getattr(self, "_current_output_dir", "")
            if output_dir:
                pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
                authority_dir = Path(str(pom.debug("global_reasoning", "global_reasoning_authority")))
                authority_dir.mkdir(parents=True, exist_ok=True)
                with open(authority_dir / "authority_verdict.json", "w", encoding="utf-8") as f:
                    json.dump(authority_report, f, ensure_ascii=False, indent=2)
            if authority_verdict.get("summary", {}).get("overall_status") != "pass":
                raise RuntimeError(
                    "global reasoning authority hard gate blocked: "
                    + str(authority_verdict.get("summary", {}).get("blocked_reason", "unknown"))
                )
        projected = self._project_compact_global_reasoning(
            compact_ir=compact_ir,
            partial_results=partial_results,
            merge_result=merge_result,
            observations=observations,
            traces=traces,
            hard_skips=hard_skips,
            hard_keeps=hard_keeps,
        )
        if self.global_reasoning_constraint_flow_enabled:
            fix_payload = self._build_global_reasoning_payload(observations, traces)
            return await self._run_global_reasoning_constraint_flow(
                fix_payload,
                projected,
                observations=observations,
                traces=traces,
                stage_suffix="compact_hierarchical",
                output_dir=getattr(self, "_current_output_dir", ""),
                run_identity_fix=True,
            )
        if projected.get("canonical_pages") and projected.get("step_labels"):
            fix_payload = self._build_global_reasoning_payload(observations, traces)
            fixed = await self._run_identity_fix_loop(
                fix_payload,
                projected,
                observations=observations,
                stage_suffix="compact_hierarchical",
                output_dir=getattr(self, "_current_output_dir", ""),
                traces=traces,
            )
            return self._ensure_global_reasoning_step_coverage(
                self._normalize_global_reasoning_result(fixed, observations),
                observations,
                traces,
            )
        return self._ensure_global_reasoning_step_coverage(
            self._normalize_global_reasoning_result(projected, observations),
            observations,
            traces,
        )

    def _normalize_global_reasoning_result(
        self,
        result: Dict[str, Any],
        observations: List[FrameObservation]
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(o): o for o in observations}
        step_labels: List[Dict[str, Any]] = []
        skipped_steps: List[Dict[str, Any]] = []
        seen_steps = set()
        seen_skipped = set()

        def add_skipped(item: Dict[str, Any], key: str):
            if key in seen_skipped:
                return
            trace_id, step_index_str = key.rsplit(":", 1)
            skipped_steps.append({
                "trace_id": trace_id,
                "step_index": int(step_index_str),
                "skip_reason": str(
                    item.get("skip_reason", "")
                    or item.get("reason", "")
                    or "non_target_app_state"
                ).strip(),
            })
            seen_skipped.add(key)

        for item in result.get("skipped_steps", []):
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except Exception:
                continue
            key = f"{trace_id}:{step_index}"
            if key in obs_map:
                add_skipped(item, key)

        for item in result.get("step_labels", []):
            if not isinstance(item, dict):
                continue
            trace_id = str(item.get("trace_id", "")).strip()
            try:
                step_index = int(item.get("step_index", -1))
            except Exception:
                continue
            key = f"{trace_id}:{step_index}"
            if key not in obs_map or key in seen_steps or key in seen_skipped:
                continue
            if _is_unassigned_step_label(item):
                add_skipped(item, key)
                seen_steps.add(key)
                continue
            cluster_id = str(item.get("page_cluster_id", "")).strip() or self._build_signature_cluster_key(obs_map[key])
            try:
                confidence = float(item.get("confidence", 0.5))
            except Exception:
                confidence = 0.5
            step_labels.append({
                "trace_id": trace_id,
                "step_index": step_index,
                "page_cluster_id": cluster_id,
                "is_page_transition": bool(item.get("is_page_transition", True)),
                "has_unique_page_structure": bool(item.get("has_unique_page_structure", True)),
                "confidence": max(0.0, min(1.0, confidence)),
                "observation_role": str(
                    obs_map[key].page_state.get(
                        "observation_role", "executable_step"
                    )
                    if isinstance(obs_map[key].page_state, dict)
                    else "executable_step"
                ),
            })
            seen_steps.add(key)

        canonical_pages = []
        seen_page_clusters = set()
        for item in result.get("canonical_pages", []):
            if not isinstance(item, dict):
                continue
            cluster_id = str(item.get("page_cluster_id", "")).strip()
            if not cluster_id or cluster_id in seen_page_clusters:
                continue
            route_path = str(item.get("route_path", "")).strip()
            parent_route_path = str(item.get("parent_route_path", "")).strip()
            if route_path and not route_path.startswith("/"):
                route_path = "/" + route_path
            if route_path and not parent_route_path:
                parts = [p for p in route_path.split("/") if p]
                if len(parts) > 1:
                    parent_route_path = "/" + "/".join(parts[:-1])
            try:
                route_level = int(item.get("route_level", 0))
            except Exception:
                route_level = max(0, len([p for p in route_path.split("/") if p]) - 1) if route_path else 0
            canonical_pages.append({
                "page_cluster_id": cluster_id,
                "page_type_hint": str(item.get("page_type_hint", "utility")).strip() or "utility",
                "page_name": str(item.get("page_name", "")).strip(),
                "description": str(item.get("description", "")).strip(),
                "primary_functions": [
                    str(f).strip() for f in item.get("primary_functions", [])
                    if isinstance(f, str) and str(f).strip()
                ],
                "route_path": route_path,
                "parent_route_path": parent_route_path,
                "route_level": max(0, route_level)
            })
            seen_page_clusters.add(cluster_id)

        canonical_routes = []
        for item in result.get("canonical_routes", []):
            if not isinstance(item, dict):
                continue
            evidence_steps = item.get("evidence_steps", [])
            if not isinstance(evidence_steps, list):
                evidence_steps = []
            try:
                confidence = float(item.get("confidence", 0.5))
            except Exception:
                confidence = 0.5
            canonical_routes.append({
                "from_page_cluster_id": str(item.get("from_page_cluster_id", "")).strip(),
                "to_page_cluster_id": str(item.get("to_page_cluster_id", "")).strip(),
                "trigger": str(item.get("trigger", "navigate")).strip() or "navigate",
                "evidence_steps": [str(x) for x in evidence_steps if str(x).strip()],
                "bridge_steps": [
                    str(x) for x in item.get("bridge_steps", [])
                    if isinstance(item.get("bridge_steps", []), list) and str(x).strip()
                ],
                "route_source": str(item.get("route_source", "")).strip(),
                "confidence": max(0.0, min(1.0, confidence))
            })


        task_related_elements = {}
        raw_te = result.get("task_related_elements", {})
        if isinstance(raw_te, dict):
            for page_id, elements in raw_te.items():
                if not isinstance(elements, list):
                    continue
                normalized = []
                for el in elements:
                    if not isinstance(el, dict):
                        continue
                    name = str(el.get("element_name", "")).strip()
                    etype = str(el.get("element_type", "")).strip()
                    if not name:
                        continue
                    normalized.append({
                        "element_name": name,
                        "element_type": etype,
                        "display_text": str(el.get("display_text", "")).strip(),
                        "task_step_refs": el.get("task_step_refs", []) if isinstance(el.get("task_step_refs"), list) else []
                    })
                if normalized:
                    task_related_elements[str(page_id).strip()] = normalized

        normalized = {
            "app_type": str(result.get("app_type", result.get("website_type", ""))).strip(),
            "step_labels": step_labels,
            "skipped_steps": skipped_steps,
            "canonical_pages": canonical_pages,
            "canonical_routes": canonical_routes,
            "task_related_elements": task_related_elements,
        }
        if isinstance(result.get("step_coverage"), dict):
            normalized["step_coverage"] = deepcopy(result["step_coverage"])
        if not getattr(self, "screenshot_render_classification_enabled", True):
            incoming_unique_count = sum(
                1 for label in step_labels
                if bool(label.get("has_unique_page_structure", True))
            )
            duplicate_count = sum(
                1 for label in step_labels
                if not bool(label.get("has_unique_page_structure", True))
            )
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
                    "cluster_count": len(canonical_pages),
                    "materialized_page_count": len(canonical_pages),
                    "general_page_count": len(canonical_pages),
                    "step_label_count": len(step_labels),
                    "incoming_unique_count": incoming_unique_count,
                    "gr_p4_unique_count": incoming_unique_count,
                    "gr_p4_duplicate_count": duplicate_count,
                    "phase4_unique_count": incoming_unique_count,
                    "phase4_duplicate_count": duplicate_count,
                    "materialized_unique_count": incoming_unique_count,
                    "materialized_duplicate_count": duplicate_count,
                    "unique_delta_from_incoming": 0,
                    "materialized_page_group_count": len(canonical_pages),
                },
                "clusters": {},
            }
            setattr(self, "_last_screenshot_render_classification_debug", debug)
        return normalized

    async def _run_screenshot_render_classification_judge(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = [{
            "role": "user",
            "content": (
                "You are a strict page-structure auditor for trace-based app modeling. "
                "For the provided existing Materialized Page, mark every ordered step as either "
                "a unique page structure representative or a duplicate of another structure "
                "already present in this same Materialized Page. Treat a step as unique when it "
                "introduces a new component, component layout, or component composition/relationship. "
                "For home/feed pages, a different named above-the-fold campaign/module family or "
                "module stack is a component variant and should be unique. "
                "Return classifications keyed by exact ordered_steps step_key values. "
                "Use payload.classification_skeleton.classifications as the complete key set: "
                "do not create, delete, rename, or reorder keys. "
                "Every ordered step must appear exactly once under classifications with "
                "classification=unique or classification=duplicate. "
                "For duplicate items, duplicate_of must reference a classification=unique step. "
                "For unique items, render_role must be one of canonical_layout, "
                "post_interaction_overlay, interaction_state_change, runtime_status. "
                "The JSON schema requires duplicate_of and render_role on every item: "
                "for unique items set duplicate_of to an empty string; for duplicate items set "
                "render_role to the duplicated representative's render role when known. "
                "Include a parseable reason on every classifications item. Write cluster_rationale and all reason fields "
                "in Chinese. Return strict JSON only. Do not create "
                "or rename General Pages or Materialized Pages.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            ),
        }]
        result, _usage = await call_openai_api_json_async(
            messages,
            reasoning_effort=self.reasoning_effort,
            model=self.model,
            stage="site_model_global_reasoning_screenshot_render_classification",
            json_schema=classification_json_schema_for_payload(payload),
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ScreenshotRenderClassificationError(
                "Cluster unique labeling judge returned non-object result"
            )
        return result

    async def _infer_global_reasoning(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData],
        website_url: str
    ) -> Dict[str, Any]:
        if bool(getattr(self, "global_reasoning_is_compact", False)):
            return await self._infer_global_reasoning_compact_hierarchical(
                observations, traces, website_url
            )

        payload = self._build_global_reasoning_payload(observations, traces)
        token_estimate = self._estimate_tokens_for_payload(payload)
        if token_estimate <= self.global_reasoning_token_budget:
            result = await self._run_global_reasoning(
                payload,
                observations,
                stage_suffix="full",
                output_dir=getattr(self, "_current_output_dir", ""),
                traces=traces,
                run_identity_fix=not self.global_reasoning_constraint_flow_enabled,
            )
            if self.global_reasoning_constraint_flow_enabled:
                return await self._run_global_reasoning_constraint_flow(
                    payload,
                    result,
                    observations=observations,
                    traces=traces,
                    stage_suffix="full",
                    output_dir=getattr(self, "_current_output_dir", ""),
                    run_identity_fix=True,
                )
            normalized = self._normalize_global_reasoning_result(result, observations)
            return self._ensure_global_reasoning_step_coverage(normalized, observations, traces)

        if self.global_reasoning_overflow_strategy == "fail_fast":
            raise RuntimeError(
                f"Global reasoning input too large: {token_estimate} > {self.global_reasoning_token_budget}"
            )


        traces_payload = payload.get("traces", [])
        if not isinstance(traces_payload, list) or not traces_payload:
            return self._fallback_global_reasoning(observations, traces)
        budget_chars = self.global_reasoning_token_budget * 4
        chunks: List[List[Dict[str, Any]]] = []
        current_chunk: List[Dict[str, Any]] = []
        current_size = 0
        for tr in traces_payload:
            tr_size = len(json.dumps(tr, ensure_ascii=False))
            if current_chunk and (current_size + tr_size > budget_chars):
                chunks.append(current_chunk)
                current_chunk = [tr]
                current_size = tr_size
            else:
                current_chunk.append(tr)
                current_size += tr_size
        if current_chunk:
            chunks.append(current_chunk)

        partial_results = []
        for idx, chunk in enumerate(chunks):
            chunk_payload = {
                "traces": chunk
            }
            partial = await self._run_global_reasoning(
                chunk_payload,
                observations,
                stage_suffix=f"chunk_{idx:03d}",
                output_dir=getattr(self, "_current_output_dir", ""),
                traces=traces,
                run_identity_fix=False,
            )
            partial_results.append(partial)

        reduced = await self._run_global_reasoning_reduce(partial_results, payload)
        if self.global_reasoning_constraint_flow_enabled:
            return await self._run_global_reasoning_constraint_flow(
                payload,
                reduced,
                observations=observations,
                traces=traces,
                stage_suffix="reduce",
                output_dir=getattr(self, "_current_output_dir", ""),
                run_identity_fix=True,
            )
        if reduced.get("canonical_pages") and reduced.get("step_labels"):
            reduced = await self._run_identity_fix_loop(
                payload,
                reduced,
                observations=observations,
                stage_suffix="reduce",
                output_dir=getattr(self, "_current_output_dir", ""),
                traces=traces,
            )
        normalized = self._normalize_global_reasoning_result(reduced, observations)
        return self._ensure_global_reasoning_step_coverage(normalized, observations, traces)

    def _fallback_global_reasoning(
        self,
        observations: List[FrameObservation],
        traces: List[TraceData]
    ) -> Dict[str, Any]:
        obs_map = {self._obs_key(o): o for o in observations}
        hard_skips = self._build_hard_skip_map(observations, traces)
        step_labels: List[Dict[str, Any]] = []
        for obs in observations:
            if self._obs_key(obs) in hard_skips:
                continue
            cluster_id = self._build_signature_cluster_key(obs)
            action = ""
            if isinstance(obs.page_state, dict):
                action = str(obs.page_state.get("last_action", "")).strip()
            step_labels.append({
                "trace_id": obs.trace_id,
                "step_index": obs.step_index,
                "page_cluster_id": cluster_id,
                "is_page_transition": True,
                "confidence": 0.45
            })

        page_map: Dict[str, Dict[str, Any]] = {}
        for label in step_labels:
            key = f"{label['trace_id']}:{label['step_index']}"
            obs = obs_map.get(key)
            if not obs:
                continue
            cluster_id = label["page_cluster_id"]
            if cluster_id in page_map:
                continue
            page_map[cluster_id] = {
                "page_cluster_id": cluster_id,
                "page_type_hint": "utility",
                "page_name": self._infer_page_name("utility", obs.description),
                "description": obs.description,
                "route_path": f"/page/{cluster_id[-6:]}",
                "parent_route_path": "/page",
                "route_level": 1
            }

        route_seen = set()
        routes = []
        label_index = {(x["trace_id"], x["step_index"]): x for x in step_labels}
        for trace in traces:
            steps = sorted(trace.steps, key=lambda s: s.step_index)
            for i in range(len(steps) - 1):
                curr = steps[i]
                nxt = steps[i + 1]
                c_label = label_index.get((trace.trace_id, curr.step_index))
                n_label = label_index.get((trace.trace_id, nxt.step_index))
                if not c_label or not n_label:
                    continue
                from_cluster = c_label["page_cluster_id"]
                to_cluster = n_label["page_cluster_id"]
                if not from_cluster or not to_cluster or from_cluster == to_cluster:
                    continue
                route_key = (from_cluster, to_cluster, curr.action)
                if route_key in route_seen:
                    continue
                route_seen.add(route_key)
                routes.append({
                    "from_page_cluster_id": from_cluster,
                    "to_page_cluster_id": to_cluster,
                    "trigger": curr.action or "navigate",
                    "evidence_steps": [f"{trace.trace_id}:{curr.step_index}"],
                    "confidence": 0.45
                })

        normalized = {
            "app_type": "",
            "step_labels": step_labels,
            "skipped_steps": list(hard_skips.values()),
            "canonical_pages": list(page_map.values()),
            "canonical_routes": routes,
        }
        return self._ensure_global_reasoning_step_coverage(normalized, observations, traces)

    def _most_common_text(self, values: List[str], fallback: str = "") -> str:
        counts: Dict[str, int] = {}
        for v in values:
            key = (v or "").strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return fallback
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def _collect_primary_functions(self, observations: List[FrameObservation], structure_data=None, page_type: str = "", gr_primary_functions: List[str] = None) -> List[str]:

        if gr_primary_functions:
            return list(gr_primary_functions)[:10]

        _COMPONENT_TYPE_MAP = {
            "search-form": "Search and filtering",
            "search": "Search and filtering",
            "card-list": "Content browsing and listing",
            "item-card-list": "Content browsing and listing",
            "grid": "Content browsing and listing",
            "tabs": "Tabbed content navigation",
            "nav": "Site navigation",
            "header": "Site navigation",
            "form": "Form submission",
            "input": "Form submission",
            "feature-grid": "Feature showcase",
            "carousel": "Featured content browsing",
            "slider": "Featured content browsing",
            "footer": "Footer information",
        }
        _PAGE_TYPE_FALLBACKS = {
            "homepage": ["Site overview and navigation hub"],
            "home": ["Site overview and navigation hub"],
            "detail": ["Detailed content viewing"],
            "listing": ["Content browsing and filtering"],
            "list": ["Content browsing and filtering"],
            "search": ["Search and filtering"],
            "login": ["User authentication"],
            "register": ["User registration"],
            "profile": ["User profile management"],
            "settings": ["User settings management"],
            "cart": ["Shopping cart management"],
            "checkout": ["Order checkout"],
        }

        funcs: List[str] = []


        if isinstance(structure_data, dict):
            for comp in structure_data.get("components", []):
                comp_type = (comp.get("type", "") if isinstance(comp, dict) else "").strip().lower()
                label = _COMPONENT_TYPE_MAP.get(comp_type)
                if label and label not in funcs:
                    funcs.append(label)


            nav_elements = structure_data.get("navigation_elements", {})
            if isinstance(nav_elements, dict):
                for btn in nav_elements.get("cta_buttons", []):
                    text = (btn.get("text", "") if isinstance(btn, dict) else "").strip()
                    if text:
                        action_label = f"Action: {text}"
                        if action_label not in funcs:
                            funcs.append(action_label)


        for obs in observations:
            action = obs.page_state.get("last_action") if isinstance(obs.page_state, dict) else None
            if isinstance(action, str) and action and action not in funcs:
                funcs.append(action)


        if not funcs and page_type:
            pt = page_type.strip().lower()
            fallbacks = _PAGE_TYPE_FALLBACKS.get(pt)
            if fallbacks:
                funcs.extend(fallbacks)
            else:
                funcs.append(f"{page_type.strip()} page functionality")

        return funcs[:10]

    def _infer_filename_from_cluster(self, cluster_key: str, page_type: str, idx: int, route_path: str = "") -> str:
        if (page_type or "").lower() == "homepage":
            if route_path and route_path != "/":
                segments = [s for s in route_path.strip("/").split("/") if s]
                if segments:
                    if segments[-1] in {"home", "homepage", "index", "default", "main", "inbox"} and len(segments) >= 2:
                        segments = segments[:-1]
                    slug = "-".join(segments)
                    safe = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug).strip("-").lower()
                    if safe:
                        return f"{safe}.html"
            self._homepage_counter = getattr(self, '_homepage_counter', 0) + 1
            if self._homepage_counter == 1:
                return "homepage.html"
            return f"homepage-{self._homepage_counter}.html"
        if self.use_url_signals and cluster_key and cluster_key.startswith("http"):
            parsed = urlparse(cluster_key)
            tail = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
            if tail:
                tail = tail.replace(".php", "").replace(".html", "").strip()
                safe = "".join(ch if ch.isalnum() else "-" for ch in tail).strip("-")
                if safe:
                    return f"{safe}.html"

        if route_path and route_path != "/":
            segments = [s for s in route_path.strip("/").split("/") if s]
            if segments:
                slug = "-".join(segments)
                safe = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug).strip("-").lower()
                if safe and safe != "page":
                    return f"{safe}.html"

        if cluster_key and not cluster_key.startswith("http"):
            clean = cluster_key.lstrip("c_").replace("_", "-")
            safe = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in clean).strip("-").lower()
            if safe and len(safe) > 2:
                return f"{safe}.html"
        base = "".join(ch if ch.isalnum() else "-" for ch in (page_type or "page")).strip("-").lower() or "page"
        return f"{base}-{idx+1}.html"
