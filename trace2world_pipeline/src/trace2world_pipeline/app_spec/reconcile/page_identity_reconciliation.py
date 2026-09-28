
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class PageIdentityIndex:
    pages: list[dict[str, Any]]
    page_by_id: dict[str, dict[str, Any]]
    route_neighbors: dict[str, list[str]]
    retrieval_mode: str
    page_texts: dict[str, str]
    embeddings: Any = None
    embed_model: Any = None


@dataclass
class ContextBatch:
    offset: int
    count: int
    items: list[dict[str, Any]]
    optimized_context_estimate: int
    baseline_context_estimate: int
    optimized_candidate_count: int
    baseline_candidate_count: int
    context_reduction_ratio: float


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def estimate_tokens(value: Any) -> int:
    return max(1, math.ceil(_json_size(value) / 4))


def _page_id(page: dict[str, Any]) -> str:
    return str(page.get("page_cluster_id") or page.get("id") or "").strip()


def compact_identity_page(page: dict[str, Any]) -> dict[str, Any]:
    page_id = _page_id(page)
    return {
        "page_cluster_id": page_id,
        "page_name": page.get("name") or page.get("page_name"),
        "description": page.get("description", ""),
        "route_path": page.get("routePath") or page.get("route_path", ""),
        "parent_route_path": page.get("parentRoutePath") or page.get("parent_route_path", ""),
        "route_level": page.get("routeLevel") or page.get("route_level", ""),
        "page_type_hint": page.get("pageTypeHint") or page.get("page_type_hint", ""),
        "domain": page.get("domain", ""),
        "primary_functions": page.get("primary_functions", [])[:5],
        "step_count": page.get("stepCount") or page.get("step_count", 0),
        "trace_count": page.get("traceCount") or page.get("trace_count", 0),
        "signatures": page.get("signatures", [])[:5],
    }


def candidate_card(page: dict[str, Any], reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "page_cluster_id": page.get("page_cluster_id") or page.get("id"),
        "page_name": page.get("page_name") or page.get("name"),
        "route_path": page.get("route_path") or page.get("routePath", ""),
        "page_type_hint": page.get("page_type_hint") or page.get("pageTypeHint", ""),
        "stable_identity": page.get("description", ""),
        "visual_signatures": page.get("signatures", [])[:3],
        "retrieval_reasons": reasons or [],
    }


def _page_text(page: dict[str, Any]) -> str:
    values = [
        page.get("page_name") or page.get("name") or "",
        page.get("route_path") or page.get("routePath") or "",
        page.get("page_type_hint") or page.get("pageTypeHint") or "",
        page.get("description") or "",
        " ".join(
            str(sig.get("signature", ""))
            for sig in page.get("signatures", [])[:5]
            if isinstance(sig, dict)
        ),
    ]
    return " ".join(str(value) for value in values if str(value).strip())


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", lowered)
    return {part for part in parts if part.strip()}


def _lexical_score(query: str, text: str) -> float:
    q = _tokens(query)
    t = _tokens(text)
    if not q or not t:
        return 0.0
    return len(q & t) / math.sqrt(len(q) * len(t))


def _route_neighbors(data: dict[str, Any]) -> dict[str, list[str]]:
    neighbors: dict[str, set[str]] = {}
    for edge in data.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or edge.get("from_page_cluster_id") or "").strip()
        target = str(edge.get("to") or edge.get("to_page_cluster_id") or "").strip()
        if not source or not target:
            continue
        neighbors.setdefault(source, set()).add(target)
        neighbors.setdefault(target, set()).add(source)
    return {key: sorted(value) for key, value in neighbors.items()}


def build_page_identity_index(data: dict[str, Any], retrieval_mode: str = "embedding") -> PageIdentityIndex:
    pages = [compact_identity_page(page) for page in data.get("nodes", []) if isinstance(page, dict)]
    page_by_id = {_page_id(page): page for page in pages if _page_id(page)}
    if not page_by_id:
        raise ValueError("PageIdentityIndex cannot be built without pages")
    page_texts = {pid: _page_text(page) for pid, page in page_by_id.items()}
    mode = retrieval_mode.strip().lower()
    embeddings = None
    embed_model = None
    if mode == "embedding":
        from sentence_transformers import SentenceTransformer

        embed_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        ordered_ids = list(page_by_id)
        embeddings = {
            "ids": ordered_ids,
            "vectors": embed_model.encode([page_texts[pid] for pid in ordered_ids], normalize_embeddings=True),
        }
    elif mode != "lexical":
        raise ValueError(f"unsupported retrieval_mode: {retrieval_mode}")
    return PageIdentityIndex(
        pages=pages,
        page_by_id=page_by_id,
        route_neighbors=_route_neighbors(data),
        retrieval_mode=mode,
        page_texts=page_texts,
        embeddings=embeddings,
        embed_model=embed_model,
    )


def page_identity_index_id(index: PageIdentityIndex) -> str:
    payload = {
        "retrieval_mode": index.retrieval_mode,
        "page_ids": sorted(index.page_by_id),
        "route_neighbors": {key: sorted(value) for key, value in sorted(index.route_neighbors.items())},
        "page_texts": {key: index.page_texts[key] for key in sorted(index.page_texts)},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "page_identity_index_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _item_text(items: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for item in items:
        values.extend(
            [
                str(item.get("task_instruction", "")),
                str(item.get("previous_action", "")),
                str(item.get("current_action", "")),
                str(item.get("previous_thought", "")),
                str(item.get("current_thought", "")),
                json.dumps(item.get("transition_hint", {}), ensure_ascii=False),
                json.dumps(item.get("phase1a_observation", {}), ensure_ascii=False),
            ]
        )
    return " ".join(value for value in values if value.strip())


def _embedding_scores(index: PageIdentityIndex, query: str) -> dict[str, float]:
    if index.retrieval_mode != "embedding":
        return {}
    if index.embed_model is None or index.embeddings is None:
        raise RuntimeError("embedding retrieval requires initialized embeddings")
    query_vec = index.embed_model.encode([query], normalize_embeddings=True)[0]
    vectors = index.embeddings["vectors"]
    ids = index.embeddings["ids"]
    sims = vectors @ query_vec
    return {pid: float(score) for pid, score in zip(ids, sims)}


def select_candidates_for_items(
    *,
    index: PageIdentityIndex,
    target_page_id: str,
    items: list[dict[str, Any]],
    max_candidates: int = 8,
    top_k: int = 6,
) -> list[dict[str, Any]]:
    if target_page_id not in index.page_by_id:
        raise ValueError(f"target page missing from identity index: {target_page_id}")
    if not items:
        raise ValueError(f"cannot select candidates for target={target_page_id} without items")
    query = _item_text(items) + " " + index.page_texts.get(target_page_id, "")
    mandatory: dict[str, list[str]] = {target_page_id: ["target_page"]}
    for neighbor in index.route_neighbors.get(target_page_id, []):
        if neighbor in index.page_by_id:
            mandatory.setdefault(neighbor, []).append("route_neighbor")
    for item in items:
        current = item.get("current_assignment", {})
        pid = str(current.get("page_cluster_id", "") or "").strip()
        if pid and pid in index.page_by_id:
            mandatory.setdefault(pid, []).append("current_assignment")
    scores = (
        _embedding_scores(index, query)
        if index.retrieval_mode == "embedding"
        else {pid: _lexical_score(query, text) for pid, text in index.page_texts.items()}
    )
    ranked = sorted(
        (pid for pid in index.page_by_id if pid not in mandatory),
        key=lambda pid: (-scores.get(pid, 0.0), pid),
    )
    selected_ids = list(mandatory)
    for pid in ranked[: max(0, top_k)]:
        if len(selected_ids) >= max(max_candidates, len(mandatory)):
            break
        selected_ids.append(pid)
    return [
        candidate_card(
            index.page_by_id[pid],
            list(dict.fromkeys(mandatory.get(pid, []) + (["retrieved"] if pid not in mandatory else []))),
        )
        for pid in selected_ids
    ]


def estimate_prompt_context(
    *,
    target_page: dict[str, Any],
    candidates: list[dict[str, Any]],
    items: list[dict[str, Any]],
    image_token_cost: int,
    output_tokens_per_item: int,
) -> int:
    fixed = 1250
    schema = 900 + output_tokens_per_item * max(1, len(items))
    target = estimate_tokens(target_page)
    candidate_tokens = estimate_tokens(candidates)
    item_metadata = estimate_tokens([
        {key: value for key, value in item.items() if key not in {"image_path", "image_rel"}}
        for item in items
    ])
    image_tokens = image_token_cost * len(items)
    return fixed + schema + target + candidate_tokens + item_metadata + image_tokens


def plan_context_budgeted_batches(
    *,
    target_page: dict[str, Any],
    candidates: list[dict[str, Any]],
    items: list[dict[str, Any]],
    context_budget: int,
    image_token_cost: int = 1800,
    output_tokens_per_item: int = 220,
    baseline_candidates: list[dict[str, Any]] | None = None,
) -> list[ContextBatch]:
    if context_budget <= 0:
        raise ValueError("context_budget must be positive")
    baseline = baseline_candidates or candidates
    batches: list[ContextBatch] = []
    offset = 0
    while offset < len(items):
        best_count = 0
        best_estimate = 0
        for count in range(1, len(items) - offset + 1):
            candidate_items = items[offset: offset + count]
            estimate = estimate_prompt_context(
                target_page=target_page,
                candidates=candidates,
                items=candidate_items,
                image_token_cost=image_token_cost,
                output_tokens_per_item=output_tokens_per_item,
            )
            if estimate > context_budget and count > 1:
                break
            if estimate > context_budget and count == 1:
                best_count = 1
                best_estimate = estimate
                break
            best_count = count
            best_estimate = estimate
        batch_items = items[offset: offset + best_count]
        baseline_estimate = estimate_prompt_context(
            target_page=target_page,
            candidates=baseline,
            items=batch_items,
            image_token_cost=image_token_cost,
            output_tokens_per_item=output_tokens_per_item,
        )
        ratio = 1.0 - (best_estimate / baseline_estimate) if baseline_estimate else 0.0
        batches.append(
            ContextBatch(
                offset=offset,
                count=best_count,
                items=batch_items,
                optimized_context_estimate=best_estimate,
                baseline_context_estimate=baseline_estimate,
                optimized_candidate_count=len(candidates),
                baseline_candidate_count=len(baseline),
                context_reduction_ratio=round(ratio, 6),
            )
        )
        offset += best_count
    return batches


def _signature_set(page: dict[str, Any]) -> set[str]:
    signatures = set()
    for item in page.get("signatures", []):
        if isinstance(item, dict) and str(item.get("signature", "")).strip():
            signatures.add(str(item["signature"]).strip())
    return signatures


def _route_inbound_sets(index: PageIdentityIndex) -> dict[str, set[str]]:
    inbound: dict[str, set[str]] = {pid: set() for pid in index.page_by_id}
    for source, neighbors in index.route_neighbors.items():
        for target in neighbors:
            inbound.setdefault(target, set()).add(source)
    return inbound


def build_merge_candidate_clusters(
    index: PageIdentityIndex,
    threshold: float = 0.83,
    composite_threshold: float = 0.68,
) -> dict[str, Any]:
    if index.retrieval_mode == "embedding":
        ids = index.embeddings["ids"]
        vectors = index.embeddings["vectors"]
        score = lambda a, b: float(vectors[ids.index(a)] @ vectors[ids.index(b)])
    else:
        ids = list(index.page_by_id)
        score = lambda a, b: _lexical_score(index.page_texts[a], index.page_texts[b])
    inbound = _route_inbound_sets(index)
    edges = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if b in index.route_neighbors.get(a, []) or a in index.route_neighbors.get(b, []):
                continue
            sim = score(a, b)
            if sim < threshold:
                continue
            page_a = index.page_by_id[a]
            page_b = index.page_by_id[b]
            inbound_a = inbound.get(a, set())
            inbound_b = inbound.get(b, set())
            inbound_overlap = len(inbound_a & inbound_b) / len(inbound_a | inbound_b) if inbound_a | inbound_b else 0.0
            type_eq = 1.0 if page_a.get("page_type_hint") and page_a.get("page_type_hint") == page_b.get("page_type_hint") else 0.0
            sig_a = _signature_set(page_a)
            sig_b = _signature_set(page_b)
            signature_overlap = len(sig_a & sig_b) / len(sig_a | sig_b) if sig_a | sig_b else 0.0
            composite = sim * 0.5 + inbound_overlap * 0.25 + type_eq * 0.15 + signature_overlap * 0.10
            if composite >= composite_threshold:
                edges.append({
                    "from": a,
                    "to": b,
                    "similarity": round(sim, 4),
                    "inbound_overlap": round(inbound_overlap, 4),
                    "type_eq": type_eq,
                    "signature_overlap": round(signature_overlap, 4),
                    "composite": round(composite, 4),
                })
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["from"], set()).add(edge["to"])
        adjacency.setdefault(edge["to"], set()).add(edge["from"])
    clusters: list[list[str]] = []
    seen: set[str] = set()
    for node in sorted(adjacency):
        if node in seen:
            continue
        stack = [node]
        cluster: list[str] = []
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            cluster.append(cur)
            stack.extend(sorted(adjacency.get(cur, set()) - seen))
        clusters.append(sorted(cluster))
    clusters.sort(key=lambda item: (-len(item), item))
    return {"edge_count": len(edges), "edges": edges, "clusters": clusters}


def _non_route_page_pairs(index: PageIdentityIndex) -> list[dict[str, Any]]:
    ids = sorted(index.page_by_id)
    pairs: list[dict[str, Any]] = []
    for pos, source in enumerate(ids):
        for target in ids[pos + 1:]:
            if target in index.route_neighbors.get(source, []) or source in index.route_neighbors.get(target, []):
                continue
            pairs.append({"from": source, "to": target})
    return pairs


def _merge_pages_for_edges(index: PageIdentityIndex, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    page_ids = sorted(
        {
            str(edge.get("from", "") or "").strip()
            for edge in edges
            if str(edge.get("from", "") or "").strip()
        }
        | {
            str(edge.get("to", "") or "").strip()
            for edge in edges
            if str(edge.get("to", "") or "").strip()
        }
    )
    return [candidate_card(index.page_by_id[page_id]) for page_id in page_ids if page_id in index.page_by_id]


def estimate_oversplit_merge_context(
    *,
    index: PageIdentityIndex,
    merge_candidate_clusters: dict[str, Any],
    output_tokens_per_pair: int = 160,
) -> dict[str, Any]:
    baseline_pairs = _non_route_page_pairs(index)
    candidate_edges = [
        edge for edge in merge_candidate_clusters.get("edges", []) if isinstance(edge, dict)
    ]
    baseline_payload = {
        "pages": [candidate_card(page) for page in index.pages],
        "candidate_pairs": baseline_pairs,
    }
    optimized_payload = {
        "pages": _merge_pages_for_edges(index, candidate_edges),
        "candidate_edges": candidate_edges,
        "clusters": merge_candidate_clusters.get("clusters", []),
    }
    fixed_tokens = 900
    baseline_estimate = fixed_tokens + estimate_tokens(baseline_payload) + output_tokens_per_pair * max(1, len(baseline_pairs))
    optimized_estimate = fixed_tokens + estimate_tokens(optimized_payload) + output_tokens_per_pair * max(1, len(candidate_edges))
    return {
        "baseline_pair_count": len(baseline_pairs),
        "candidate_pair_count": len(candidate_edges),
        "baseline_context_estimate": baseline_estimate,
        "optimized_context_estimate": optimized_estimate,
        "naive_pair_context_reduction_ratio": round(
            1.0 - optimized_estimate / baseline_estimate if baseline_estimate else 0.0,
            6,
        ),
        "incremental_reduction_over_current_pipeline": None,
    }


def build_page_identity_reconciliation_report(
    *,
    index: PageIdentityIndex,
    target_item_map: dict[str, list[dict[str, Any]]],
    evidence_mode: str,
    max_candidates: int,
    top_k: int,
    context_budget: int,
    image_token_cost: int,
    output_tokens_per_item: int,
    merge_similarity_threshold: float,
    merge_composite_threshold: float,
) -> dict[str, Any]:
    if evidence_mode not in {"phase1a_obs", "image"}:
        raise ValueError(f"unsupported evidence_mode: {evidence_mode}")
    if not isinstance(target_item_map, dict):
        raise ValueError("target_item_map must be a mapping from page id to audit items")

    index_id = page_identity_index_id(index)
    baseline_pages = index.pages
    audit_targets: list[dict[str, Any]] = []
    audit_baseline_context = 0
    audit_optimized_context = 0
    audit_image_equivalent_context = 0
    for target_id, items in target_item_map.items():
        if target_id not in index.page_by_id:
            raise ValueError(f"target page missing from identity index: {target_id}")
        if not items:
            continue
        target_page = index.page_by_id[target_id]
        candidates = select_candidates_for_items(
            index=index,
            target_page_id=target_id,
            items=items,
            max_candidates=max_candidates,
            top_k=top_k,
        )
        batches = plan_context_budgeted_batches(
            target_page=target_page,
            candidates=candidates,
            baseline_candidates=baseline_pages,
            items=items,
            context_budget=context_budget,
            image_token_cost=0 if evidence_mode == "phase1a_obs" else image_token_cost,
            output_tokens_per_item=output_tokens_per_item,
        )
        baseline_context = sum(batch.baseline_context_estimate for batch in batches)
        optimized_context = sum(batch.optimized_context_estimate for batch in batches)
        image_equivalent_context = sum(
            estimate_prompt_context(
                target_page=target_page,
                candidates=candidates,
                items=batch.items,
                image_token_cost=image_token_cost,
                output_tokens_per_item=output_tokens_per_item,
            )
            for batch in batches
        )
        audit_baseline_context += baseline_context
        audit_optimized_context += optimized_context
        audit_image_equivalent_context += image_equivalent_context
        audit_targets.append(
            {
                "target_page_id": target_id,
                "item_count": len(items),
                "baseline_candidate_count": len(baseline_pages),
                "optimized_candidate_count": len(candidates),
                "baseline_context_estimate": baseline_context,
                "optimized_context_estimate": optimized_context,
                "image_equivalent_context_estimate": image_equivalent_context,
                "candidate_retrieval_context_reduction_ratio": round(
                    1.0 - optimized_context / baseline_context if baseline_context else 0.0,
                    6,
                ),
                "evidence_mode_context_reduction_ratio": round(
                    1.0 - optimized_context / image_equivalent_context if image_equivalent_context else 0.0,
                    6,
                ),
                "batches": [
                    asdict(batch) | {"items": [str(item.get("key") or item.get("tile_id") or "") for item in batch.items]}
                    for batch in batches
                ],
                "candidates": candidates,
            }
        )

    merge_clusters = build_merge_candidate_clusters(
        index,
        threshold=merge_similarity_threshold,
        composite_threshold=merge_composite_threshold,
    )
    oversplit_context = estimate_oversplit_merge_context(
        index=index,
        merge_candidate_clusters=merge_clusters,
        output_tokens_per_pair=output_tokens_per_item,
    )
    baseline_total = audit_baseline_context + oversplit_context["baseline_context_estimate"]
    optimized_total = audit_optimized_context + oversplit_context["optimized_context_estimate"]
    image_equivalent_total = audit_image_equivalent_context + oversplit_context["optimized_context_estimate"]
    return {
        "phase": "Page Identity Reconciliation",
        "shared_index": {
            "index_id": index_id,
            "retrieval_mode": index.retrieval_mode,
            "page_count": len(index.pages),
            "route_edge_count": sum(len(value) for value in index.route_neighbors.values()) // 2,
        },
        "consumers": {
            "StepPageAssignmentReview": {
                "consumer": "StepPageAssignmentReview",
                "legacy_name": "page_assignment_audit",
                "shared_index_id": index_id,
                "candidate_selection_policy": "mandatory_route_current_plus_embedding_or_lexical_topk",
                "targets": audit_targets,
                "baseline_context_estimate": audit_baseline_context,
                "optimized_context_estimate": audit_optimized_context,
                "candidate_retrieval_context_reduction_ratio": round(
                    1.0 - audit_optimized_context / audit_baseline_context if audit_baseline_context else 0.0,
                    6,
                ),
                "evidence_mode_context_reduction_ratio": round(
                    1.0 - audit_optimized_context / audit_image_equivalent_context if audit_image_equivalent_context else 0.0,
                    6,
                ),
            },
            "OverSplitPageMergeReview": {
                "consumer": "OverSplitPageMergeReview",
                "legacy_name": "sprite_merge",
                "shared_index_id": index_id,
                "candidate_selection_policy": "shared_identity_index_similarity_topology_clusters",
                "merge_candidate_clusters": merge_clusters,
                **oversplit_context,
            },
        },
        "context_benefit": {
            "baseline_context_estimate": baseline_total,
            "optimized_context_estimate": optimized_total,
            "image_equivalent_context_estimate": image_equivalent_total,
            "overall_context_reduction_ratio": round(
                1.0 - optimized_total / baseline_total if baseline_total else 0.0,
                6,
            ),
            "evidence_mode_context_reduction_ratio": round(
                1.0 - optimized_total / image_equivalent_total if image_equivalent_total else 0.0,
                6,
            ),
        },
    }
