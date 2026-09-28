
import copy
import json
import os
from typing import Dict, Any, List, Tuple

from trace2world_pipeline.app_spec.reconcile.phase1_semantic_chunking import (
    build_phase1_semantic_balanced_producer_chunks,
    ProducerChunk,
)


def build_authority_namespace_budget_producer_chunks(
    compact_ir: Dict[str, Any],
    chunk_size: int = 4,
    website_url: str = "",
    observations_by_key: Dict[str, Any] | None = None,
    target_input_tokens: int | None = None,
    hard_skips: List[Dict[str, Any]] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    trace_payloads = compact_ir.get("traces", [])
    if not isinstance(trace_payloads, list):
        trace_payloads = []

    chunk_payloads: List[Dict[str, Any]] = []
    for idx in range(0, len(trace_payloads), chunk_size):
        chunk_id = f"chunk_{idx // chunk_size:02d}"
        chunk_payload = {
            "format": "compact_trace_ir_chunk_v1",
            "chunk_id": chunk_id,
            "website_url": website_url,
            "traces": trace_payloads[idx:idx + chunk_size],
            "hard_skips": hard_skips if hard_skips is not None else compact_ir.get("hard_skips", []),
        }
        chunk_payloads.append(chunk_payload)

    total_segments = sum(
        len(tp.get("segments", []))
        for tp in trace_payloads
        if isinstance(tp, dict)
    )

    build_report = {
        "chunker": "authority_namespace_budget",
        "field_strategy": "namespace_sequential",
        "chunk_count": len(chunk_payloads),
        "total_segments": total_segments,
        "chunk_size_traces": chunk_size,
        "target_input_tokens": target_input_tokens,
    }

    return chunk_payloads, build_report


def estimate_payload_tokens(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, len(text) // 4)


def _strip_prompt_redundant_manifests(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(payload)
    for trace in out.get("traces", []) or []:
        if not isinstance(trace, dict):
            continue
        for segment in trace.get("segments", []) or []:
            if not isinstance(segment, dict):
                continue
            for key in (
                "raw_screenshot_base64",
                "screenshot_base64",
                "image_base64",
                "ocr_raw_blocks",
            ):
                segment.pop(key, None)
    return out


def _payload_with_added_trace_segment(
    payload: Dict[str, Any],
    compact_trace: Dict[str, Any],
    compact_segment: Dict[str, Any],
) -> Dict[str, Any]:
    out = copy.deepcopy(payload)
    trace_id = str(compact_trace.get("trace_id", "")).strip()
    if not trace_id:
        raise RuntimeError("cannot add segment without trace_id")
    traces = out.setdefault("traces", [])
    target_trace = None
    for trace in traces:
        if isinstance(trace, dict) and str(trace.get("trace_id", "")).strip() == trace_id:
            target_trace = trace
            break
    if target_trace is None:
        target_trace = {
            "trace_id": trace_id,
            "task": compact_trace.get("task", ""),
            "segments": [],
        }
        traces.append(target_trace)
    target_trace.setdefault("segments", []).append(copy.deepcopy(compact_segment))
    return out


def build_phase1_semantic_balanced_chunks_for_gr(
    compact_ir: Dict[str, Any],
    frame_observations: List[Any],
    target_budget_tokens: int = 12000,
    embedding_model: str = "BAAI/bge-small-zh-v1.5",
    similarity_threshold: float = 0.95,
    website_url: str = "",
    hard_skips: List[Dict[str, Any]] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:

    producer_chunks, build_report = build_phase1_semantic_balanced_producer_chunks(
        compact_ir=compact_ir,
        frame_observations=frame_observations,
        target_budget_tokens=target_budget_tokens,
        embedding_model=embedding_model,
        similarity_threshold=similarity_threshold,
        website_url=website_url,
        hard_skips=hard_skips if hard_skips is not None else compact_ir.get("hard_skips", []),
    )

    if not producer_chunks:
        raise RuntimeError(
            "Phase1 semantic balanced chunking produced 0 producer chunks. "
            "Cannot proceed with GR."
        )


    segment_lookup: Dict[str, Dict[str, Any]] = {}
    trace_lookup: Dict[str, Dict[str, Any]] = {}
    for trace_payload in compact_ir.get("traces", []):
        if not isinstance(trace_payload, dict):
            continue
        trace_id = str(trace_payload.get("trace_id", "")).strip()
        trace_lookup[trace_id] = trace_payload
        for segment in trace_payload.get("segments", []):
            if not isinstance(segment, dict):
                continue
            seg_id = str(segment.get("segment_id", "")).strip()
            if seg_id:
                segment_lookup[seg_id] = segment

    chunk_payloads: List[Dict[str, Any]] = []
    for pc in producer_chunks:

        trace_segments: Dict[str, List[Dict[str, Any]]] = {}
        for seg_id in pc.segment_ids:
            segment = segment_lookup.get(seg_id)
            if segment is None:
                continue

            parts = seg_id.rsplit(":s_", 1)
            trace_id = parts[0] if len(parts) == 2 else ""
            if trace_id:
                trace_segments.setdefault(trace_id, []).append(segment)


        chunk_traces: List[Dict[str, Any]] = []
        for trace_id, segments in trace_segments.items():
            trace_info = trace_lookup.get(trace_id, {})
            chunk_traces.append({
                "trace_id": trace_id,
                "task": trace_info.get("task", ""),
                "segments": segments,
            })

        chunk_payload = {
            "format": "compact_trace_ir_chunk_v1",
            "chunk_build_mode": "phase1_semantic_balanced",
            "chunk_id": pc.chunk_id,
            "website_url": website_url,
            "traces": chunk_traces,
            "hard_skips": hard_skips if hard_skips is not None else compact_ir.get("hard_skips", []),
            "context_optimization": {
                "producer_chunker": "phase1_semantic_balanced",
                "source_chunks": ["compact_trace_ir", "phase1_frame_observations"],
                "semantic_similarity_scope": "producer_semantic_family_discovery_only",
                "semantic_family_ids": pc.source_families,
                "split_family_ids": pc.split_families,
            },
        }
        chunk_payloads.append(chunk_payload)


    all_chunk_segments = set()
    for pc in producer_chunks:
        all_chunk_segments.update(pc.segment_ids)

    all_ir_segments = set()
    for trace_payload in compact_ir.get("traces", []):
        if not isinstance(trace_payload, dict):
            continue
        for segment in trace_payload.get("segments", []):
            if isinstance(segment, dict):
                seg_id = str(segment.get("segment_id", "")).strip()
                if seg_id:
                    all_ir_segments.add(seg_id)

    uncovered = all_ir_segments - all_chunk_segments
    if uncovered:
        raise RuntimeError(
            f"Phase1 semantic balanced chunking coverage gap: "
            f"{len(uncovered)} segments not assigned to any producer chunk. "
            f"First 5: {sorted(uncovered)[:5]}"
        )

    build_report["chunk_payloads_count"] = len(chunk_payloads)
    return chunk_payloads, build_report


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
