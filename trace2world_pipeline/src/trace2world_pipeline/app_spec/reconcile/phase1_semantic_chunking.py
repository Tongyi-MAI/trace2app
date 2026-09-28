
import math
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

import numpy as np


def _compact_field_text(value: Any, max_chars: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _append_labeled(parts: List[str], label: str, value: Any) -> None:
    text = _compact_field_text(value)
    if text:
        parts.append(f"{label}: {text}")


@dataclass
class SegmentAuthorityRecord:
    segment_id: str
    trace_id: str
    step_range: List[int] = field(default_factory=list)
    authority_text: str = ""
    token_estimate: int = 0
    phase1_refs: List[str] = field(default_factory=list)
    visual_signature: str = ""
    compact_trace: Dict[str, Any] = field(default_factory=dict)
    compact_segment: Dict[str, Any] = field(default_factory=dict)
    global_ordinal: int = 0


@dataclass
class SemanticFamily:
    family_id: str
    segment_ids: List[str] = field(default_factory=list)
    centroid: Optional[Any] = None
    total_tokens: int = 0


@dataclass
class ProducerChunk:
    chunk_id: str
    segment_ids: List[str] = field(default_factory=list)
    total_tokens: int = 0
    source_families: List[str] = field(default_factory=list)
    split_families: List[str] = field(default_factory=list)


def compile_segment_authority_records(
    compact_ir: Dict[str, Any],
    frame_observations: List[Any],
) -> Tuple[List[SegmentAuthorityRecord], List[str]]:
    obs_map: Dict[str, Any] = {}
    for obs in frame_observations:
        key = f"{obs.trace_id}:{obs.step_index}"
        obs_map[key] = obs

    records: List[SegmentAuthorityRecord] = []
    blocked_segments: List[str] = []

    global_ordinal = 0
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

            steps = segment.get("steps", [])
            step_range = segment.get("step_range", [])


            phase1_refs: List[str] = []
            authority_parts: List[str] = []
            has_any_obs = False

            all_step_indices = steps if isinstance(steps, list) else []


            obs_for_segment: List[Any] = []
            first_step_candidates = []
            if all_step_indices:
                first_step_candidates.append(all_step_indices[0])
            if step_range:
                first_step_candidates.append(step_range[0])
            for step_idx in first_step_candidates:
                try:
                    idx = int(step_idx)
                except (TypeError, ValueError):
                    continue
                obs_key = f"{trace_id}:{idx}"
                obs = obs_map.get(obs_key)
                if obs is not None:
                    obs_for_segment.append(obs)
                    phase1_refs.append(obs_key)
                    has_any_obs = True

            if not has_any_obs:

                for step_idx in all_step_indices:
                    try:
                        idx = int(step_idx)
                    except (TypeError, ValueError):
                        continue
                    obs_key = f"{trace_id}:{idx}"
                    obs = obs_map.get(obs_key)
                    if obs is not None:
                        obs_for_segment.append(obs)
                        phase1_refs.append(obs_key)
                        has_any_obs = True
                        break

            if not has_any_obs:
                blocked_segments.append(segment_id)
                continue


            primary_obs = obs_for_segment[0]


            desc = str(
                getattr(primary_obs, "description", "")
                or segment.get("description", "")
                or ""
            ).strip()
            state_summary = str(getattr(primary_obs, "state_summary", "") or "").strip()
            _append_labeled(authority_parts, "description", desc or state_summary)


            header = str(getattr(primary_obs, "header_pattern", "") or segment.get("header_pattern", "") or "").strip()
            shell = str(getattr(primary_obs, "shell_section", "") or segment.get("shell_section", "") or "").strip()
            body = str(getattr(primary_obs, "body_pattern", "") or segment.get("body_pattern", "") or "").strip()
            sticky = str(getattr(primary_obs, "sticky_action_pattern", "") or segment.get("sticky_action_pattern", "") or "").strip()
            _append_labeled(authority_parts, "header_pattern", header)
            _append_labeled(authority_parts, "shell_section", shell)
            _append_labeled(authority_parts, "body_pattern", body)
            _append_labeled(authority_parts, "sticky_action_pattern", sticky)


            page_state = getattr(primary_obs, "page_state", {}) or {}
            if not isinstance(page_state, dict):
                page_state = {}
            for key in (
                "interaction_surface_role",
                "host_page_identity",
                "opened_surface_identity",
            ):
                value = str(page_state.get(key, "") or "").strip()
                _append_labeled(authority_parts, key, value)
            for key in ("standalone_page_evidence", "host_page_evidence"):
                value = page_state.get(key, [])
                _append_labeled(authority_parts, key, value)

            transition_kind = str(
                getattr(primary_obs, "transition_kind", "") or
                primary_obs.page_state.get("transition_kind", "") if hasattr(primary_obs, "page_state") else ""
            ).strip()
            transition_evidence = getattr(primary_obs, "transition_evidence", []) or []
            _append_labeled(authority_parts, "transition_kind", transition_kind)
            _append_labeled(authority_parts, "transition_evidence", transition_evidence)


            extracted = getattr(primary_obs, "extracted_items", {}) or {}
            _append_labeled(authority_parts, "extracted_items", extracted)

            vis_sig = str(segment.get("visual_signature", "")).strip()

            authority_text = "\n".join(authority_parts)
            token_estimate = max(1, len(authority_text) // 4)

            records.append(SegmentAuthorityRecord(
                segment_id=segment_id,
                trace_id=trace_id,
                step_range=step_range if isinstance(step_range, list) else [],
                authority_text=authority_text,
                token_estimate=token_estimate,
                phase1_refs=phase1_refs,
                visual_signature=vis_sig,
                compact_trace=trace_payload,
                compact_segment=segment,
                global_ordinal=global_ordinal,
            ))
            global_ordinal += 1

    return records, blocked_segments


def build_semantic_families(
    records: List[SegmentAuthorityRecord],
    model_name: str = "BAAI/bge-small-zh-v1.5",
    similarity_threshold: float = 0.95,
) -> List[SemanticFamily]:
    if not records:
        return []


    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise RuntimeError(
            f"sentence_transformers is required for phase1 semantic chunking but not installed: {e}"
        ) from e

    try:
        model = SentenceTransformer(model_name)
    except Exception as e:
        raise RuntimeError(
            f"Failed to load BGE embedding model '{model_name}': {e}. "
            f"No fallback to keyword/namespace rules is allowed."
        ) from e


    texts = [r.authority_text for r in records]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    embeddings = np.array(embeddings, dtype=np.float32)

    try:
        from sklearn.cluster import AgglomerativeClustering
    except ImportError as e:
        raise RuntimeError(
            f"sklearn is required for phase1 semantic chunking but not installed: {e}"
        ) from e

    distance_threshold = 1.0 - float(similarity_threshold)
    kwargs = {
        "n_clusters": None,
        "distance_threshold": distance_threshold,
        "linkage": "average",
    }
    try:
        clusterer = AgglomerativeClustering(metric="cosine", **kwargs)
    except TypeError:
        clusterer = AgglomerativeClustering(affinity="cosine", **kwargs)
    labels = np.asarray(clusterer.fit_predict(embeddings), dtype=np.int64)

    grouped: Dict[int, List[int]] = {}
    for index, label in enumerate(labels.tolist()):
        grouped.setdefault(int(label), []).append(index)

    families: List[SemanticFamily] = []
    for family_idx, label in enumerate(sorted(grouped, key=lambda key: (min(grouped[key]), key))):
        family_members = grouped[label]
        centroid = np.mean(embeddings[family_members], axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        total_tokens = sum(records[m].token_estimate for m in family_members)
        families.append(SemanticFamily(
            family_id=f"sf_{family_idx:03d}",
            segment_ids=[records[m].segment_id for m in family_members],
            centroid=centroid,
            total_tokens=total_tokens,
        ))

    return families


def _payload_for_records(
    records: List[SegmentAuthorityRecord],
    chunk_id: str,
    build_mode: str,
    website_url: str = "",
    hard_skips: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    from trace2world_pipeline.app_spec.reconcile.compact_gr_producer_chunking import (
        _payload_with_added_trace_segment,
        _strip_prompt_redundant_manifests,
    )

    payload: Dict[str, Any] = {
        "format": "compact_trace_ir_chunk_v1",
        "chunk_build_mode": build_mode,
        "chunk_id": chunk_id,
        "website_url": website_url,
        "traces": [],
        "hard_skips": hard_skips or [],
        "apk_route_priors": {},
        "context_optimization": {
            "producer_chunker": build_mode,
            "source_chunks": ["compact_trace_ir", "phase1_frame_observations"],
            "semantic_similarity_scope": "producer_semantic_family_discovery_only",
        },
    }
    for record in sorted(records, key=lambda item: int(item.global_ordinal)):
        payload = _payload_with_added_trace_segment(
            payload,
            record.compact_trace,
            record.compact_segment,
        )
    return _strip_prompt_redundant_manifests(payload)


def _payload_tokens_for_records(
    records: List[SegmentAuthorityRecord],
    chunk_id: str,
    build_mode: str,
    website_url: str = "",
    hard_skips: List[Dict[str, Any]] | None = None,
) -> int:
    from trace2world_pipeline.app_spec.reconcile.compact_gr_producer_chunking import estimate_payload_tokens

    return estimate_payload_tokens(
        _payload_for_records(
            records,
            chunk_id=chunk_id,
            build_mode=build_mode,
            website_url=website_url,
            hard_skips=hard_skips,
        )
    )


def build_token_balanced_chunks(
    families: List[SemanticFamily],
    records: List[SegmentAuthorityRecord],
    target_budget_tokens: int = 12000,
    build_mode: str = "phase1_semantic_balanced",
    website_url: str = "",
    hard_skips: List[Dict[str, Any]] | None = None,
) -> Tuple[List[ProducerChunk], Dict[str, Any]]:
    if not families or not records:
        return [], {"error": "empty_input"}

    record_map = {r.segment_id: r for r in records}


    sorted_families = sorted(families, key=lambda f: f.total_tokens, reverse=True)

    chunks: List[ProducerChunk] = []
    split_count = 0
    chunk_idx = 0


    bin_items: List[Tuple[str, List[str], int]] = []

    soft_family_budget = max(1, int(target_budget_tokens * 0.75))

    for family in sorted_families:
        family_records = [record_map[seg_id] for seg_id in family.segment_ids if seg_id in record_map]
        family_tokens = _payload_tokens_for_records(
            family_records,
            family.family_id,
            build_mode,
            website_url=website_url,
            hard_skips=hard_skips,
        )
        if family_tokens <= soft_family_budget:
            bin_items.append((family.family_id, family.segment_ids, family_tokens))
        else:

            split_count += 1
            current_group: List[str] = []
            current_tokens = 0
            sub_idx = 0
            for seg_id in family.segment_ids:
                candidate_group = current_group + [seg_id]
                candidate_records = [record_map[item] for item in candidate_group if item in record_map]
                candidate_tokens = _payload_tokens_for_records(
                    candidate_records,
                    f"{family.family_id}_split_{sub_idx}",
                    build_mode,
                    website_url=website_url,
                    hard_skips=hard_skips,
                )
                if current_group and candidate_tokens > soft_family_budget:
                    bin_items.append(
                        (f"{family.family_id}_split_{sub_idx}", current_group, current_tokens)
                    )
                    sub_idx += 1
                    current_group = [seg_id]
                    current_tokens = _payload_tokens_for_records(
                        [record_map[seg_id]],
                        f"{family.family_id}_split_{sub_idx}",
                        build_mode,
                        website_url=website_url,
                        hard_skips=hard_skips,
                    )
                    if current_tokens > target_budget_tokens:
                        raise RuntimeError(
                            f"single segment exceeds producer token budget: "
                            f"{seg_id} tokens={current_tokens} budget={target_budget_tokens}"
                        )
                else:
                    current_group = candidate_group
                    current_tokens = candidate_tokens
            if current_group:
                bin_items.append(
                    (f"{family.family_id}_split_{sub_idx}", current_group, current_tokens)
                )


    bin_items.sort(key=lambda x: x[2], reverse=True)

    def try_assign(bin_count: int) -> List[Tuple[List[str], List[str], int]] | None:
        bins: List[Tuple[List[str], List[str], int]] = [
            ([], [], 0) for _ in range(max(1, int(bin_count)))
        ]
        for item_family_id, item_segments, _item_tokens in bin_items:
            candidate_order = sorted(
                range(len(bins)),
                key=lambda idx: (bins[idx][2], len(bins[idx][0]), idx),
            )
            placed = False
            for bin_idx in candidate_order:
                bin_segs, bin_families, _bin_tokens = bins[bin_idx]
                preview_segments = bin_segs + item_segments
                preview_records = [
                    record_map[seg_id]
                    for seg_id in preview_segments
                    if seg_id in record_map
                ]
                actual_tokens = _payload_tokens_for_records(
                    preview_records,
                    f"producer_chunk_{bin_idx:02d}",
                    build_mode,
                    website_url=website_url,
                    hard_skips=hard_skips,
                )
                if actual_tokens <= target_budget_tokens:
                    bins[bin_idx] = (
                        list(preview_segments),
                        bin_families + [item_family_id],
                        actual_tokens,
                    )
                    placed = True
                    break
            if not placed:
                return None
        return [item for item in bins if item[0]]

    min_bins = max(
        1,
        math.ceil(sum(item[2] for item in bin_items) / max(1, target_budget_tokens)),
    )
    best_bins: List[Tuple[List[str], List[str], int]] | None = None
    best_score = float("inf")
    for bin_count in range(min_bins, len(bin_items) + 1):
        candidate = try_assign(bin_count)
        if not candidate:
            continue
        token_values = [item[2] for item in candidate]
        segment_values = [len(item[0]) for item in candidate]
        token_imbalance = (max(token_values) - min(token_values)) / max(1, target_budget_tokens)
        segment_imbalance = (max(segment_values) - min(segment_values)) / max(1, max(segment_values))


        score = token_imbalance + 0.25 * segment_imbalance + 0.01 * len(candidate)
        if score < best_score:
            best_score = score
            best_bins = candidate
        if token_imbalance <= 0.15 and segment_imbalance <= 0.25:
            best_bins = candidate
            break
    if best_bins is None:
        raise RuntimeError("unable to construct phase1 semantic producer chunks within token budget")
    chunk_bins = best_bins


    for idx, (seg_ids, source_fams, total_toks) in enumerate(chunk_bins):
        split_fams = [f for f in source_fams if "_split_" in f]
        chunks.append(ProducerChunk(
            chunk_id=f"producer_chunk_{idx:02d}",
            segment_ids=seg_ids,
            total_tokens=total_toks,
            source_families=[f.split("_split_")[0] for f in source_fams],
            split_families=split_fams,
        ))


    token_counts = [c.total_tokens for c in chunks]
    segment_counts = [len(c.segment_ids) for c in chunks]

    token_max = max(token_counts) if token_counts else 0
    token_min = min(token_counts) if token_counts else 0
    token_ratio = round(token_max / token_min, 2) if token_min > 0 else float("inf")

    seg_max = max(segment_counts) if segment_counts else 0
    seg_min = min(segment_counts) if segment_counts else 0
    seg_ratio = round(seg_max / seg_min, 2) if seg_min > 0 else float("inf")

    build_stats = {
        "chunk_count": len(chunks),
        "semantic_cluster_count": len(families),
        "split_semantic_cluster_count": split_count,
        "target_input_tokens": target_budget_tokens,
        "token_balance_stats": {
            "max": token_max,
            "min": token_min,
            "mean": round(sum(token_counts) / len(token_counts), 1) if token_counts else 0,
            "ratio": token_ratio,
        },
        "segment_balance_stats": {
            "max": seg_max,
            "min": seg_min,
            "mean": round(sum(segment_counts) / len(segment_counts), 1) if segment_counts else 0,
            "ratio": seg_ratio,
        },
    }

    return chunks, build_stats


def build_phase1_semantic_balanced_producer_chunks(
    compact_ir: Dict[str, Any],
    frame_observations: List[Any],
    target_budget_tokens: int = 12000,
    embedding_model: str = "BAAI/bge-small-zh-v1.5",
    similarity_threshold: float = 0.95,
    website_url: str = "",
    hard_skips: List[Dict[str, Any]] | None = None,
) -> Tuple[List[ProducerChunk], Dict[str, Any]]:

    records, blocked_segments = compile_segment_authority_records(
        compact_ir, frame_observations
    )

    if blocked_segments:
        raise RuntimeError(
            f"Phase1 semantic chunking hard fail: {len(blocked_segments)} segments "
            f"have no Phase1 frame observations. Blocked: {blocked_segments[:10]}"
        )

    if not records:
        raise RuntimeError(
            "Phase1 semantic chunking hard fail: no authority records compiled. "
            "compact_ir may be empty or all segments blocked."
        )


    families = build_semantic_families(
        records,
        model_name=embedding_model,
        similarity_threshold=similarity_threshold,
    )


    chunks, build_stats = build_token_balanced_chunks(
        families,
        records,
        target_budget_tokens=target_budget_tokens,
        build_mode="phase1_semantic_balanced",
        website_url=website_url,
        hard_skips=hard_skips,
    )


    build_report = {
        "chunker": "phase1_semantic_balanced",
        "field_strategy": "phase1_authority_no_action",
        "target_input_tokens": target_budget_tokens,
        "embedding_model": embedding_model,
        "similarity_threshold": similarity_threshold,
        "total_segments": len(records),
        "blocked_segments": len(blocked_segments),
        **build_stats,
    }

    return chunks, build_report


def save_producer_chunk_build_report(
    build_report: Dict[str, Any],
    output_dir: str,
) -> str:
    if not output_dir:
        return ""
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "producer_chunk_build_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(build_report, f, ensure_ascii=False, indent=2)
    return report_path
