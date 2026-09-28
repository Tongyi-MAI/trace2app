import json
import re
import hashlib
import os
import asyncio
from copy import deepcopy
from collections import Counter, defaultdict
from typing import Dict, Any, List, Optional, Tuple, Mapping
from urllib.parse import urlparse
from trace2world_pipeline.core.config_manager import resolve_page_environment_planning_mode
from trace2world_pipeline.core.llm_caller import LLMJsonResponseError, call_openai_api_json_async
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.core.step_keys import canonical_step_key

from trace2world_pipeline.app_spec.model import (
    AppSpec, SiteStructure, SiteContent, SiteGraph,
    PagePrototype, GraphEdge, FrameObservation
)
from trace2world_pipeline.app_spec.trace_loader import TraceData
from trace2world_pipeline.app_spec.provenance import Provenance, attach_provenance
from trace2world_pipeline.app_spec.global_execution_schema import (
    complete_global_schema_from_obligations,
    mark_unreferenced_global_fields_optional,
    normalize_canonical_global_execution_schema,
    validate_canonical_global_execution_schema,
)
from trace2world_pipeline.app_spec.schema_generation_context import build_schema_generation_context
from trace2world_pipeline.app_spec.schema_obligations import collect_schema_obligations


def _safe_schema_artifact_key(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = text.strip("._-")
    return text or "schema_inference_output"


def parse_schema_inference_output(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("schema inference output must be a JSON object")
    if not isinstance(raw.get("global_execution_schema"), dict):
        raise ValueError("schema inference output missing global_execution_schema")
    return raw


def merge_schema_inference_outputs(
    chunk_outputs: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    chunks = [
        dict(item)
        for item in chunk_outputs
        if isinstance(item, Mapping)
    ]
    return {
        "format": "schema_inference_chunk_outputs_v1",
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def apply_schema_operations(
    schemas: List[Dict[str, Any]],
    operations: List[Dict[str, Any]],
    existing_relationships: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    schema_map: Dict[str, Dict[str, Any]] = {
        s["name"]: s for s in schemas if isinstance(s, dict) and s.get("name")
    }
    new_relationships: List[Dict[str, Any]] = []

    for op in operations:
        if not isinstance(op, dict):
            continue
        kind = op.get("op", "")

        if kind == "create_entity":
            entity = op.get("entity", {})
            if not isinstance(entity, dict) or not entity.get("name"):
                continue
            name = entity["name"]
            if name not in schema_map:
                schema_map[name] = {
                    "name": name,
                    "storage_key": entity.get("storage_key", name.lower() + "s"),
                    "description": entity.get("description", ""),
                    "fields": entity.get("fields", []),
                    "data_pre_generation_num": entity.get("data_pre_generation_num", "few"),
                    "observed_item_count": 0,
                }

        elif kind == "add_field":
            schema = schema_map.get(op.get("entity_name", ""))
            if not schema:
                continue
            field = op.get("field", {})
            if not isinstance(field, dict) or not field.get("name"):
                continue
            existing_names = {f["name"] for f in schema.get("fields", []) if isinstance(f, dict)}
            if field["name"] not in existing_names:
                field_dict = {k: v for k, v in {
                    "name": field["name"],
                    "type": field.get("type", "string"),
                    "description": field.get("description", ""),
                    "observed_examples": [],
                    "domain_type": field.get("domain_type"),
                    "domain_constraints": field.get("domain_constraints"),
                }.items() if v is not None}
                schema.setdefault("fields", []).append(field_dict)

        elif kind == "update_field":
            schema = schema_map.get(op.get("entity_name", ""))
            if not schema:
                continue
            field_name = op.get("field_name", "")
            updates = op.get("updates", {})
            if not isinstance(updates, dict):
                continue
            for f in schema.get("fields", []):
                if isinstance(f, dict) and f.get("name") == field_name:
                    for k, v in updates.items():
                        if k != "name":
                            f[k] = v
                    break

        elif kind == "add_examples":
            schema = schema_map.get(op.get("entity_name", ""))
            if not schema:
                continue
            field_name = op.get("field_name", "")
            examples = op.get("examples", [])
            if not isinstance(examples, list):
                continue
            for f in schema.get("fields", []):
                if isinstance(f, dict) and f.get("name") == field_name:
                    existing = f.setdefault("observed_examples", [])
                    for ex in examples:
                        if ex not in existing and len(existing) < 10:
                            existing.append(ex)
                    break

        elif kind == "update_entity":
            schema = schema_map.get(op.get("entity_name", ""))
            if not schema:
                continue
            updates = op.get("updates", {})
            if not isinstance(updates, dict):
                continue
            for k, v in updates.items():
                if k not in ("name", "fields"):
                    schema[k] = v

        elif kind == "delete_entity":
            schema_map.pop(op.get("entity_name", ""), None)

        elif kind == "create_relationship":
            rel = op.get("relationship", {})
            if isinstance(rel, dict) and rel.get("from") and rel.get("to"):
                new_relationships.append(rel)

        elif kind == "split_entity":
            entity_name = op.get("entity_name", "")
            original = schema_map.get(entity_name)
            if not original:
                continue
            new_entities_spec = op.get("new_entities", [])
            if not isinstance(new_entities_spec, list) or len(new_entities_spec) < 2:
                continue

            orig_field_map = {
                f["name"]: f for f in original.get("fields", [])
                if isinstance(f, dict) and f.get("name")
            }

            created_names = []
            for ne_spec in new_entities_spec:
                if not isinstance(ne_spec, dict) or not ne_spec.get("name"):
                    continue
                ne_name = ne_spec["name"]
                field_names = ne_spec.get("field_names", [])
                if not isinstance(field_names, list):
                    continue
                ne_fields = []
                for fn in field_names:
                    if fn in orig_field_map:
                        ne_fields.append(dict(orig_field_map[fn]))
                    else:
                        ne_fields.append({"name": fn, "type": "string", "description": ""})
                schema_map[ne_name] = {
                    "name": ne_name,
                    "storage_key": ne_spec.get("storage_key", ne_name.lower() + "s"),
                    "description": ne_spec.get("description", original.get("description", "")),
                    "fields": ne_fields,
                    "data_pre_generation_num": original.get("data_pre_generation_num", "few"),
                    "observed_item_count": 0,
                }
                created_names.append(ne_name)
            if len(created_names) >= 2:

                if existing_relationships is not None:
                    created_field_sets = {
                        ne_spec["name"]: set(ne_spec.get("field_names", []))
                        for ne_spec in new_entities_spec
                        if isinstance(ne_spec, dict) and ne_spec.get("name") and ne_spec["name"] in created_names
                    }
                    for rel in existing_relationships:
                        if not isinstance(rel, dict):
                            continue
                        for key in ("from", "to"):
                            if rel.get(key) == entity_name:
                                fk_field = rel.get("field", "")
                                best_match = created_names[0]
                                best_score = -1
                                for cn, cn_fields in created_field_sets.items():
                                    score = len(cn_fields)
                                    if fk_field and fk_field in cn_fields:
                                        score += 100
                                    if score > best_score:
                                        best_score = score
                                        best_match = cn
                                rel[key] = best_match

                schema_map.pop(entity_name, None)

    schemas = validate_and_fix_types(list(schema_map.values()))
    return schemas, new_relationships


def validate_and_fix_types(schemas):
    for entity in schemas:
        if not isinstance(entity, dict):
            continue

        for field in entity.get("fields", []):
            if not isinstance(field, dict):
                continue

            ftype = field.get("type", "string")
            raw_examples = field.get("observed_examples", [])


            if ftype == "number" and raw_examples:
                def _parseable_str(ex):
                    try:
                        float(str(ex).strip().lstrip('¥$€£\uffe5').replace(',', '').rstrip('+'))
                        return True
                    except (ValueError, AttributeError):
                        return False

                has_string_examples = False
                all_pass = True
                for ex in raw_examples:
                    if ex is None or (isinstance(ex, str) and not ex.strip()):
                        continue
                    if isinstance(ex, (int, float)):
                        continue

                    has_string_examples = True
                    if not _parseable_str(ex):
                        all_pass = False
                        break
                if has_string_examples and not all_pass:
                    field["type"] = "string"


            elif ftype == "url" and raw_examples:
                examples = [str(e) for e in raw_examples if e is not None and str(e).strip()]
                if examples:
                    url_prefixes = ("http://", "https://", "/", "data:")
                    if not any(ex.startswith(url_prefixes) for ex in examples):
                        field["type"] = "string"


            elif ftype == "date" and raw_examples:
                examples = [str(e) for e in raw_examples if e is not None and str(e).strip()]
                if examples:
                    def _looks_like_date(ex):

                        if re.search(r'\d[\-/]\d', ex):
                            return True

                        try:
                            val = float(ex.strip())
                            if val >= 1e9:
                                return True
                        except (ValueError, AttributeError):
                            pass
                        return False
                    if not any(_looks_like_date(ex) for ex in examples):
                        field["type"] = "string"


        name = entity.get("name", "")
        storage_key = entity.get("storage_key", "")
        if name and storage_key:
            expected = _camel_to_snake(name) + "s"


            accepted = {expected}
            if expected.endswith("ys"):
                accepted.add(expected[:-2] + "ies")
            if storage_key not in accepted:
                entity["storage_key"] = expected

    return schemas


def dedup_relationships(
    relationships: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        key = (
            rel.get("from", ""),
            rel.get("to", ""),
            rel.get("type", ""),
            rel.get("field", ""),
        )
        if key not in seen:
            seen.add(key)
            result.append(rel)
    return result


def remove_self_references(
    relationships: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        rel for rel in relationships
        if isinstance(rel, dict) and rel.get("from") != rel.get("to")
    ]


def _camel_to_snake(name: str) -> str:
    s1 = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    s2 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s1)
    return s2.lower()


def normalize_fields_to_snake_case(
    schemas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        fields = schema.get("fields", [])
        seen_names: set = set()
        normalized_fields: list = []
        for field in fields:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            new_name = _camel_to_snake(field["name"])
            if new_name in seen_names:
                continue
            field["name"] = new_name
            seen_names.add(new_name)
            normalized_fields.append(field)
        schema["fields"] = normalized_fields
    return schemas


def normalize_content_sample_fields(
    content_samples: Dict[str, list],
) -> Dict[str, list]:
    for entity_name, items in content_samples.items():
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            new_item: Dict[str, Any] = {}
            for key, value in item.items():
                new_key = _camel_to_snake(key)
                if new_key not in new_item:
                    new_item[new_key] = value
            items[i] = new_item
    return content_samples


def materialize_fk_fields(
    schemas: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    schema_map = {s["name"]: s for s in schemas if isinstance(s, dict) and s.get("name")}

    for rel in relationships:
        from_entity = rel.get("from", "")
        fk_field = rel.get("field", "")
        to_entity = rel.get("to", "")
        if not from_entity or not fk_field or not to_entity:
            continue
        if not fk_field.endswith("_id"):
            continue
        schema = schema_map.get(from_entity)
        if not schema:
            continue
        existing_names = {f["name"] for f in schema.get("fields", []) if isinstance(f, dict)}
        if fk_field not in existing_names:
            schema.setdefault("fields", []).append({
                "name": fk_field,
                "type": "string",
                "description": f"Foreign key referencing {to_entity}",
                "source": "fk_materialized",
            })

    return schemas


class ConsolidationMixin:
    _SHELL_SHORTCUT_ENTITY_NAMES = (
        "ShortcutApp",
        "AppModule",
        "AppShortcut",
        "ServiceChannel",
        "ChannelService",
        "ChannelEntry",
        "CategoryChannel",
    )
    _SERVICE_ROUTE_ENTITY_NAMES = (
        "ServiceChannel",
        "ChannelService",
        "ChannelEntry",
        "CategoryChannel",
    )
    _FIRST_LEVEL_ROUTE_ENTITY_NAMES = (
        "ServiceChannel",
        "ChannelEntry",
        "CategoryChannel",
    )

    """Mixin containing consolidation and site model construction methods."""

    def _obs_key(self, obs: FrameObservation) -> str:
        return f"{obs.trace_id}:{obs.step_index}"

    def _infer_page_name(self, page_type: str, description: str) -> str:
        if description:

            name = description.split(".")[0].split("，")[0].strip()
            if len(name) > 60:
                name = name[:57] + "..."
            return name
        return page_type.replace("_", " ").title() if page_type else "Page"

    def _sanitize_text_soft(self, text: str, soft_limit: int) -> Tuple[str, str]:
        full_text = str(text or "").strip()
        if not full_text:
            return "", ""
        if len(full_text) <= soft_limit:
            return full_text, full_text
        return f"{full_text[:soft_limit].rstrip()}...", full_text

    @staticmethod
    def _clone_jsonish(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))

    def _slugify_instance_label(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        compact = re.sub(r"\s+", " ", raw)
        ascii_text = compact.encode("ascii", "ignore").decode("ascii")
        ascii_text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ascii_text)
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
        if slug:
            return slug[:48]
        return ""

    def _build_instance_filename(self, base_filename: str, instance_key: str) -> str:
        base = str(base_filename or "").strip() or "page.html"
        suffix = str(instance_key or "").strip() or "instance"
        if "." in base:
            stem, ext = base.rsplit(".", 1)
            return f"{stem}-{suffix}.{ext}"
        return f"{base}-{suffix}"

    def _score_text_match(self, query: str, candidate: str) -> float:
        q = self._normalize_task_label(query)
        c = self._normalize_task_label(candidate)
        if not q or not c:
            return 0.0
        if q == c:
            return 1.0
        if q in c or c in q:
            return 0.92
        q_tokens = [tok for tok in re.split(r"[\s:：,，!！·\-_./()\[\]\"'…]+", q) if tok]
        c_tokens = [tok for tok in re.split(r"[\s:：,，!！·\-_./()\[\]\"'…]+", c) if tok]
        if not q_tokens or not c_tokens:
            return 0.0
        overlap = len(set(q_tokens) & set(c_tokens))
        if overlap == 0:
            return 0.0
        return overlap / max(len(set(c_tokens)), 1)

    def _iter_interaction_candidate_texts(self, extracted_items: Dict[str, Any]) -> List[str]:
        texts: List[str] = []
        for entity_name, items in extracted_items.items():
            if entity_name == "_metadata" or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                for field in ("title", "name", "display_name"):
                    value = str(item.get(field, "")).strip()
                    if value:
                        texts.append(value)
                        break
        return texts

    def _page_navigation_aliases(self, page: PagePrototype) -> List[str]:
        aliases: List[str] = []
        for name in (getattr(page, "page_name", ""), getattr(page, "page_name_full", "")):
            if name and name not in aliases:
                aliases.append(name)
        route_path = ""
        if isinstance(getattr(page, "structure_data", None), dict):
            route_path = str(page.structure_data.get("route_path", "")).strip().lower()
        if "/mail/" in route_path and "邮箱" not in aliases:
            aliases.append("邮箱")
        if "/docs/" in route_path and "文档" not in aliases:
            aliases.append("文档")
        if "/meet" in route_path and "会议" not in aliases:
            aliases.append("会议")
        if "/messages/" in route_path and "消息" not in aliases:
            aliases.append("消息")
        if "/ai-listen" in route_path:
            for alias in ("AI 听记", "AI听记", "听记"):
                if alias not in aliases:
                    aliases.append(alias)
        return aliases

    def _resolve_shortcut_nav_target(self, display_text: str, page_prototypes: List[PagePrototype]) -> Optional[Dict[str, Any]]:
        target_key = self._normalize_task_label(display_text)
        if not target_key:
            return None
        candidates: List[Dict[str, Any]] = []
        for page in page_prototypes:
            aliases = self._page_navigation_aliases(page)
            if not any(self._normalize_task_label(alias) == target_key for alias in aliases):
                continue
            page_type = str(getattr(page, "page_type", "") or "").strip().lower()
            route_path = ""
            if isinstance(getattr(page, "structure_data", None), dict):
                route_path = str(page.structure_data.get("route_path", "") or "").strip().lower()
            priority = 50
            if page_type in {"homepage", "listing", "search_results"}:
                priority = 0
            elif page_type in {"utility", "form"}:
                priority = 10
            elif page_type == "detail":
                priority = 30
            if "/docs/hub" in route_path or route_path.endswith("/hub"):
                priority -= 5
            if "/document" in route_path or page_type == "detail":
                priority += 10
            candidates.append({
                "page_id": page.page_id,
                "page_name": page.page_name_full or page.page_name,
                "filename": page.filename,
                "priority": priority,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["priority"], item["filename"]))
        return {k: v for k, v in candidates[0].items() if k != "priority"}

    @staticmethod
    def _route_path_depth(route_path: str) -> int:
        return len([part for part in str(route_path or "").split("/") if part])

    def _is_first_level_alternative_route_source(self, page: Optional[PagePrototype]) -> bool:
        if page is None:
            return False
        page_type = str(getattr(page, "page_type", "") or "").strip().lower()
        route_path = ""
        if isinstance(getattr(page, "structure_data", None), dict):
            route_path = str(page.structure_data.get("route_path", "") or "").strip()
        if page_type != "homepage":
            return False
        return not route_path or route_path in {"/", "/home"} or self._route_path_depth(route_path) <= 1

    def _is_first_level_alternative_route_target(self, target: Dict[str, Any], page_prototypes: List[PagePrototype]) -> bool:
        target_page_id = str(target.get("page_id") or "").strip()
        page = next((item for item in page_prototypes if getattr(item, "page_id", "") == target_page_id), None)
        if page is None:
            return False
        page_type = str(getattr(page, "page_type", "") or "").strip().lower()
        if page_type not in {"homepage", "listing", "category", "category_listing"}:
            return False
        route_path = ""
        if isinstance(getattr(page, "structure_data", None), dict):
            route_path = str(page.structure_data.get("route_path", "") or "").strip()
        return not route_path or self._route_path_depth(route_path) <= 2

    def _infer_floating_component_for_page(
        self,
        page: Optional[PagePrototype],
        page_actions: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if page is None or not isinstance(page.content_data, dict):
            return None
        extracted_items = page.content_data.get("extracted_items", {}) or {}
        shortcut_items = self._collect_shortcut_item_names(extracted_items)
        has_more_action = any(
            "更多" in str(act.get("target_element", "")) or "更多" in str(act.get("description", ""))
            for act in (page_actions or [])
            if isinstance(act, dict)
        )
        if not shortcut_items:
            return None
        page_desc = " ".join([
            str(getattr(page, "page_name", "") or ""),
            str(getattr(page, "page_name_full", "") or ""),
            str(getattr(page, "description", "") or ""),
            " ".join(str(f) for f in getattr(page, "primary_functions", []) if isinstance(f, str)),
        ]).lower()
        overlay_keywords = ("overlay", "popover", "drawer", "modal", "panel", "overflow", "more menu", "更多", "浮层", "浮窗", "弹层", "弹窗", "抽屉", "覆盖", "悬浮")
        has_overlay_evidence = any(keyword in page_desc for keyword in overlay_keywords)
        if not (has_more_action and has_overlay_evidence):
            return None
        variant_id = self._infer_capability_variant(page)
        payload = {"items": shortcut_items}
        component_contract = {
            "capability_id": "more_menu",
            "component_type": "floating_component",
            "component_id": "more_overlay",
            "host_type": "fullscreen_overlay",
            "trigger_element_id": "btn_更多",
            "panel_alignment": "bottom_right",
            "offset": {"bottom": 70, "right": 8},
            "backdrop": True,
            "close_on_backdrop": True,
            "close_on_outside_click": False,
            "template_id": "fullscreen_overlay_floating_panel",
            "variant_id": variant_id,
            "payload": payload,
            "items": shortcut_items,
            "overlay_kind": "anchored_drawer",
            "overlay_anchor": "btn_更多",
            "overlay_position": "above_footer_right",
            "overlay_behavior": "overlays_content_without_reflow",
        }
        return component_contract

    @staticmethod
    def _merge_unique_texts(*groups: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for group in groups:
            for item in group or []:
                text = str(item).strip()
                if not text or text in seen:
                    continue
                merged.append(text)
                seen.add(text)
        return merged

    def _collect_shortcut_item_names(self, extracted_items: Dict[str, Any]) -> List[str]:
        item_names: List[str] = []
        if not isinstance(extracted_items, dict):
            return item_names
        for entity_name in self._SHELL_SHORTCUT_ENTITY_NAMES:
            items = extracted_items.get(entity_name, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if name and name not in item_names:
                    item_names.append(name)
        return item_names

    def _component_ref_for_entity(self, content_data: Dict[str, Any], entity_name: str) -> str:
        if not isinstance(content_data, dict):
            return ""
        mapping = content_data.get("entity_component_mapping", [])
        for item in mapping if isinstance(mapping, list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("entity") or "").strip() == entity_name:
                return str(item.get("source_component") or item.get("component_ref") or "").strip()
        return ""

    def _collect_shortcut_item_records(self, content_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(content_data, dict):
            return []
        extracted_items = content_data.get("extracted_items", {}) or {}
        if not isinstance(extracted_items, dict):
            return []
        records: List[Dict[str, Any]] = []
        seen_labels = set()
        for entity_name in self._SHELL_SHORTCUT_ENTITY_NAMES:
            items = extracted_items.get(entity_name, [])
            if not isinstance(items, list):
                continue
            component_ref = self._component_ref_for_entity(content_data, entity_name)
            for item in items:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("name", "")).strip()
                if not label or label in seen_labels:
                    continue
                seen_labels.add(label)
                records.append({
                    "label": label,
                    "entity": entity_name,
                    "component_ref": component_ref,
                    "component_role": "category_grid",
                })
        return records

    def _build_shell_component_navigation_entry(
        self,
        page: PagePrototype,
        target: Dict[str, Any],
        item: Dict[str, Any],
        target_resolution: str,
    ) -> Dict[str, Any]:
        label = str(item.get("label") or "").strip()
        component_ref = str(item.get("component_ref") or "").strip()
        entity = str(item.get("entity") or "").strip()
        binding = {
            "binding_kind": "category_entry",
            "labels": [label] if label else [],
            "surface": "category_grid",
            "source_scope": "shell_family_component",
            "match_policy": "exact_label_or_explicit_attribute",
            "confidence": "inferred",
        }
        if component_ref:
            binding["component_ref"] = component_ref
        if entity:
            binding["entity"] = entity
        return {
            "to_page_id": target["page_id"],
            "to_page": target["page_name"],
            "to_filename": target["filename"],
            "trigger": f"点击{label}",
            "kind": "navigation",
            "to_route": "",
            "to_route_pattern": "",
            "params_needed": [],
            "source": "shell_component_affordance",
            "target_source": "shell_component_affordance",
            "affordance_kind": "shell_component_navigation_affordance",
            "label": label,
            "labels": [label] if label else [],
            "component_ref": component_ref,
            "component_role": str(item.get("component_role") or "category_grid"),
            "entity": entity,
            "binding": binding,
            "provenance": {
                "source_page_id": getattr(page, "page_id", ""),
                "source_filename": getattr(page, "filename", ""),
                "target_resolution": target_resolution,
                "source_entity": entity,
                "source_component": component_ref,
            },
            "admission_reason": target_resolution,
            "trigger_visible_text_allowed": True,
        }

    def _infer_shortcut_navigation_out(
        self,
        page: Optional[PagePrototype],
        page_prototypes: List[PagePrototype],
    ) -> List[Dict[str, Any]]:
        if page is None or not isinstance(page.content_data, dict):
            return []
        shortcut_items = self._collect_shortcut_item_records(page.content_data)
        entries = []
        seen = set()
        for item in shortcut_items:
            entity = str(item.get("entity") or "").strip()
            if entity in self._SERVICE_ROUTE_ENTITY_NAMES:
                if entity not in self._FIRST_LEVEL_ROUTE_ENTITY_NAMES:
                    continue
                if not self._is_first_level_alternative_route_source(page):
                    continue
            name = str(item.get("label") or "").strip()
            target = (
                self._resolve_alternative_affordance_target(name, page_prototypes)
                if entity in self._FIRST_LEVEL_ROUTE_ENTITY_NAMES
                else self._resolve_shortcut_nav_target(name, page_prototypes)
            )
            if not target:
                continue
            if entity in self._FIRST_LEVEL_ROUTE_ENTITY_NAMES and not self._is_first_level_alternative_route_target(target, page_prototypes):
                continue
            if target["page_id"] == page.page_id:
                continue
            key = (name, target["filename"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                self._build_shell_component_navigation_entry(
                    page,
                    target,
                    item,
                    "exact_page_alias",
                )
            )
        return entries

    def _resolve_alternative_affordance_target(
        self,
        display_text: str,
        page_prototypes: List[PagePrototype],
    ) -> Optional[Dict[str, Any]]:
        target_key = self._normalize_task_label(display_text)
        if not target_key:
            return None
        candidates: List[Dict[str, Any]] = []
        for page in page_prototypes:
            aliases = self._page_navigation_aliases(page)
            scored_aliases = []
            for alias in aliases:
                alias_key = self._normalize_task_label(alias)
                if not alias_key:
                    continue
                score = self._score_text_match(target_key, alias_key)
                if score >= 0.92:
                    scored_aliases.append((score, alias_key))
            if not scored_aliases:
                continue
            page_type = str(getattr(page, "page_type", "") or "").strip().lower()
            route_path = ""
            if isinstance(getattr(page, "structure_data", None), dict):
                route_path = str(page.structure_data.get("route_path", "") or "").strip().lower()
            priority = 50
            if page_type in {"homepage", "listing", "search_results"}:
                priority = 0
            elif page_type in {"utility", "form"}:
                priority = 10
            elif page_type == "detail":
                priority = 30
            if any(word in route_path for word in ("document", "detail")) or page_type == "detail":
                priority += 10
            best_score = max(score for score, _alias in scored_aliases)
            candidates.append({
                "page_id": page.page_id,
                "page_name": page.page_name_full or page.page_name,
                "filename": page.filename,
                "priority": priority,
                "score": -best_score,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["priority"], item["score"], item["filename"]))
        return {k: v for k, v in candidates[0].items() if k not in {"priority", "score"}}

    def _infer_alternative_affordance_navigation_out(
        self,
        page: Optional[PagePrototype],
        page_prototypes: List[PagePrototype],
    ) -> List[Dict[str, Any]]:
        if page is None or not isinstance(page.content_data, dict):
            return []
        shortcut_items = self._collect_shortcut_item_records(page.content_data)
        entries = []
        seen = set()
        for item in shortcut_items:
            name = str(item.get("label") or "").strip()
            target = self._resolve_alternative_affordance_target(name, page_prototypes)
            if not target:
                continue
            if target["page_id"] == page.page_id:
                continue
            key = (name, target["filename"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                self._build_shell_component_navigation_entry(
                    page,
                    target,
                    item,
                    "reference_backed_alternative_affordance",
                )
            )
        return entries

    def _page_supports_more_menu(self, page_obj: Optional[PagePrototype]) -> bool:
        page_desc = " ".join([
            str(getattr(page_obj, "page_name", "") or ""),
            str(getattr(page_obj, "page_name_full", "") or ""),
            str(getattr(page_obj, "description", "") or ""),
            " ".join(str(f) for f in getattr(page_obj, "primary_functions", []) if isinstance(f, str)),
        ]).lower() if page_obj else ""
        overlay_keywords = ("overlay", "popover", "drawer", "modal", "panel", "overflow", "more menu", "更多", "浮层", "浮窗", "弹层", "弹窗", "抽屉", "覆盖", "悬浮")
        return any(keyword in page_desc for keyword in overlay_keywords)

    def _infer_capability_variant(self, page_obj: Optional[PagePrototype]) -> str:
        page_desc = " ".join([
            str(getattr(page_obj, "page_name", "") or ""),
            str(getattr(page_obj, "page_name_full", "") or ""),
            str(getattr(page_obj, "description", "") or ""),
            " ".join(str(f) for f in getattr(page_obj, "primary_functions", []) if isinstance(f, str)),
        ]).lower() if page_obj else ""
        if any(keyword in page_desc for keyword in ("bottom tab", "bottom navigation", "底部导航", "bottom tab bar")):
            return "bottom_tab"
        return "header_overflow"

    def _build_interaction_policy_for_page(
        self,
        page: Optional[PagePrototype],
        elements: List[Dict[str, Any]],
        continuations: List[Dict[str, Any]],
        navigation_out: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        extracted_items = {}
        if page and isinstance(page.content_data, dict):
            extracted_items = page.content_data.get("extracted_items", {}) or {}

        navigable_texts: List[str] = []
        inline_only_texts: List[str] = []
        allowed_queries: List[str] = []

        for item in continuations:
            if not isinstance(item, dict):
                continue
            text = str(item.get("display_text", "")).strip()
            if not text:
                continue
            if item.get("continuation_kind") == "page_navigation":
                navigable_texts.append(text)
            else:
                inline_only_texts.append(text)
            allowed_queries.append(text)

        for elem in elements:
            if not isinstance(elem, dict):
                continue
            text = str(elem.get("display_text", "")).strip()
            if text:
                allowed_queries.append(text)
                if text not in navigable_texts and text not in inline_only_texts:
                    navigable_texts.append(text)

        for nav in navigation_out or []:
            if not isinstance(nav, dict):
                continue
            if nav.get("trigger_visible_text_allowed") is not True:
                continue
            trigger = str(nav.get("trigger", "")).strip()
            if trigger.startswith("点击"):
                text = trigger.replace("点击", "", 1).strip()
                if text:
                    allowed_queries.append(text)
                    if text not in navigable_texts:
                        navigable_texts.append(text)

        non_navigable_texts: List[str] = []
        for text in self._iter_interaction_candidate_texts(extracted_items):
            if any(self._score_text_match(text, query) >= 0.55 for query in allowed_queries):
                continue
            if text not in non_navigable_texts:
                non_navigable_texts.append(text)

        return {
            "default_component_policy": "non_navigable",
            "strict_navigation": True,
            "navigable_texts": navigable_texts,
            "inline_only_texts": inline_only_texts,
            "non_navigable_texts": non_navigable_texts,
            "navigable_entries": [
                {"text": text, "source_kind": "visible_binding", "visible_text_allowed": True}
                for text in navigable_texts
            ],
            "non_navigable_entries": [
                {"text": text, "source_kind": "content_text", "visible_text_allowed": True}
                for text in non_navigable_texts
            ],
        }

    @staticmethod
    def _is_external_system_observation(obs: FrameObservation) -> bool:
        page_state = getattr(obs, "page_state", {}) or {}
        action_text = " ".join([
            str(getattr(obs, "action", "") or ""),
            str(getattr(obs, "thought", "") or ""),
            str(page_state.get("last_action", "") if isinstance(page_state, dict) else ""),
        ]).lower()
        text = " ".join([
            str(getattr(obs, "description", "") or ""),
            action_text,
        ]).lower()
        external_markers = [
            "android home screen",
            "home screen/launcher",
            "device home screen",
            "launcher rather than an in-app page",
            "system status bar",
            "app icon grid",
            "dock row",
            "system toast",
        ]
        if any(marker in text for marker in external_markers):
            return True
        transition_evidence = " ".join(str(item) for item in page_state.get("transition_evidence", [])).lower() if isinstance(page_state, dict) else ""
        if "not the meituan app interface" in transition_evidence or "device-level navigation" in text:
            return True

        components = getattr(obs, "components", []) or []
        extracted_items = getattr(obs, "extracted_items", {}) or {}
        has_stable_surface = bool(components) or bool(extracted_items)
        unknown_patterns = all(
            str(getattr(obs, field, "") or "").strip().lower() in {"", "unknown"}
            for field in ("header_pattern", "shell_section", "body_pattern", "sticky_action_pattern")
        )
        try:
            identity_confidence = float(getattr(obs, "identity_confidence", 0.0) or 0.0)
        except Exception:
            identity_confidence = 0.0
        open_app_intent = (
            "open app" in action_text
            or "open the app" in action_text
            or "open_app" in action_text
            or "打开app" in action_text.replace(" ", "")
            or "打开应用" in action_text
        )
        if open_app_intent and not has_stable_surface and unknown_patterns and identity_confidence <= 0.1:
            return True
        return False

    def _plan_entity_bound_instance_splits(
        self,
        cluster_key: str,
        cluster_obs: List[FrameObservation],
        content_data: Dict[str, Any],
        page_type: str,
        page_name_full: str,
        base_filename: str,
        route_path: str,
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        preextracted = content_data.get("instance_content_candidates", []) if isinstance(content_data, dict) else []
        if isinstance(preextracted, list) and len(preextracted) >= 2:
            obs_lookup = {self._obs_key(obs): obs for obs in cluster_obs}
            candidate_groups: List[Dict[str, Any]] = []
            for candidate in preextracted:
                if not isinstance(candidate, dict):
                    continue
                obs_keys = [str(key).strip() for key in candidate.get("obs_keys", []) if str(key).strip()]
                obs_list = [obs_lookup[key] for key in obs_keys if key in obs_lookup]
                candidate_groups.append({
                    "instance_key": str(candidate.get("instance_key", "")).strip(),
                    "instance_label": str(candidate.get("instance_label", "")).strip(),
                    "trace_ids": sorted(set(obs.trace_id for obs in obs_list)),
                    "obs_keys": [self._obs_key(obs) for obs in obs_list],
                    "cluster_obs": obs_list,
                    "_source_candidate": candidate,
                })
            collapsed_groups = self._merge_same_page_continuation_instance_groups(
                candidate_groups,
                boundary_preservation_constraint=boundary_preservation_constraint,
            )
            if len(collapsed_groups) < 2:
                return []
            candidates_by_key = {
                id(group.get("_source_candidate")): group
                for group in collapsed_groups
                if isinstance(group.get("_source_candidate"), dict)
            }
            planned_specs: List[Dict[str, Any]] = []
            for candidate in preextracted:
                if not isinstance(candidate, dict):
                    continue
                merged_group = candidates_by_key.get(id(candidate))
                if merged_group is None:
                    continue
                instance_key = str(candidate.get("instance_key", "")).strip() or self._slugify_instance_label(str(candidate.get("instance_label", ""))) or "instance"
                label = str(candidate.get("instance_label", "")).strip()
                obs_keys = [str(key).strip() for key in merged_group.get("obs_keys", []) if str(key).strip()]
                obs_list = [obs_lookup[key] for key in obs_keys if key in obs_lookup]
                if not obs_list:
                    continue
                instance_content = self._clone_jsonish(candidate.get("content_data", {}))
                instance_content["source_cluster_id"] = cluster_key
                instance_content["instance_key"] = instance_key
                planned_specs.append({
                    "instance_key": instance_key,
                    "page_name_full": f"{page_name_full} · {label}" if label else page_name_full,
                    "filename": self._build_instance_filename(base_filename, instance_key),
                    "route_path": self._normalize_route_pattern(route_path.rstrip("/") + f"/{instance_key}"),
                    "content_data": instance_content,
                    "cluster_obs": obs_list,
                    "main_entity_type": "",
                    "main_entity_label": label,
                })
            if len(planned_specs) >= 2:
                return planned_specs

        return []

    @staticmethod
    def _validate_consolidated_boundary_preservation(
        *,
        clusters: Dict[str, List[FrameObservation]],
        cluster_to_page_ids: Dict[str, List[str]],
        obs_to_page_id: Dict[str, str],
        boundary_preservation_constraints_by_cluster: Optional[
            Dict[str, Dict[str, Any]]
        ],
    ) -> None:
        constraints = boundary_preservation_constraints_by_cluster or {}
        for cluster_key, cluster_obs in clusters.items():
            constraint = constraints.get(cluster_key, {})
            if not isinstance(constraint, dict) or not constraint:
                continue

            minimum = int(constraint.get("minimum_instance_count", 0) or 0)
            materialized_page_ids = {
                str(page_id).strip()
                for page_id in cluster_to_page_ids.get(cluster_key, [])
                if str(page_id).strip()
            }
            errors: List[str] = []
            if minimum >= 2 and len(materialized_page_ids) < minimum:
                errors.append(
                    "materialized PagePrototype count dropped below the bound "
                    f"minimum: expected>={minimum}, got={len(materialized_page_ids)}"
                )

            cluster_obs_keys = {
                f"{obs.trace_id}:{int(obs.step_index)}"
                for obs in cluster_obs
            }
            separation_groups = constraint.get(
                "must_separate_obs_key_groups", []
            )
            group_owners: List[str] = []
            if isinstance(separation_groups, list):
                for group_index, raw_group in enumerate(separation_groups):
                    group = [
                        str(obs_key or "").strip()
                        for obs_key in raw_group
                        if str(obs_key or "").strip()
                    ] if isinstance(raw_group, list) else []
                    missing_from_cluster = sorted(set(group) - cluster_obs_keys)
                    owners = {obs_to_page_id.get(obs_key) for obs_key in group}
                    if missing_from_cluster or None in owners or len(owners) != 1:
                        errors.append(
                            "bound observation group must materialize on exactly one "
                            "PagePrototype: "
                            f"group={group_index}, obs_keys={group}, "
                            f"missing_from_cluster={missing_from_cluster}, "
                            f"owners={sorted(str(owner) for owner in owners)}"
                        )
                        continue
                    group_owners.append(next(iter(owners)))

            if (
                isinstance(separation_groups, list)
                and len(group_owners) == len(separation_groups)
                and len(set(group_owners)) != len(group_owners)
            ):
                errors.append(
                    "bound observation groups collapsed onto the same "
                    f"PagePrototype: owners={group_owners}"
                )

            if errors:
                raise ValueError(
                    "consolidation violated boundary preservation for "
                    f"{cluster_key}: " + "; ".join(errors)
                )

    def _map_task_elements_to_pages(
        self,
        raw_task_elements: Dict[str, List[Dict[str, Any]]],
        cluster_to_page_ids: Dict[str, List[str]],
        obs_to_page_id: Dict[str, str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        mapped: Dict[str, List[Dict[str, Any]]] = {}
        for cluster_or_page_id, elements in raw_task_elements.items():
            candidate_pages = cluster_to_page_ids.get(cluster_or_page_id, [])
            if not candidate_pages:
                mapped.setdefault(cluster_or_page_id, []).extend(self._clone_jsonish(elements))
                continue
            for element in elements if isinstance(elements, list) else []:
                refs = element.get("task_step_refs", []) if isinstance(element, dict) else []
                matched_pages = []
                for ref in refs if isinstance(refs, list) else []:
                    if not isinstance(ref, dict):
                        continue
                    trace_id = str(ref.get("trace_id", "")).strip()
                    try:
                        step_num = int(ref.get("step", 0))
                    except Exception:
                        continue
                    if not trace_id or step_num <= 0:
                        continue
                    page_id = obs_to_page_id.get(f"{trace_id}:{step_num - 1}", "")
                    if page_id and page_id not in matched_pages:
                        matched_pages.append(page_id)
                targets = matched_pages or candidate_pages[:1]
                for page_id in targets:
                    mapped.setdefault(page_id, []).append(self._clone_jsonish(element))
        return mapped

    def _build_signature_cluster_key(self, obs: FrameObservation) -> str:
        if obs.structure_signature:
            return f"sig:{obs.structure_signature}"
        raw = {
            "structure_signature": obs.structure_signature,
            "content_signature": obs.content_signature,
            "description": (obs.description or "")[:160],
            "component_types": sorted([
                str(c.get("type", ""))
                for c in (obs.components or [])
                if isinstance(c, dict)
            ])[:20]
        }
        digest = hashlib.md5(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return f"sig:{digest}"


    async def _consolidate_observations_v3(self,
                                           observations: List[FrameObservation],
                                           traces: List[TraceData],
                                           website_url: str,
                                           global_reasoning: Dict[str, Any] = None,
                                           loaded_route_tree: Dict[str, Any] = None,
                                           loaded_site_graph: Dict[str, Any] = None,
                                           output_dir: str = None,
                                           cluster_structures: Dict[str, Dict[str, Any]] = None,
                                           cluster_content_obs: Dict[str, Dict[str, Any]] = None,
                                           structure_unique_keys: set = None,
                                           render_role_by_step: Dict[str, str] = None,
                                           initial_authority_kind_by_step: Dict[str, str] = None,
                                           screenshot_lifecycle_by_step: Dict[str, Dict[str, Any]] = None,
                                           render_role_reasons: Dict[str, str] = None,
                                           runtime_state_observations: Dict[str, Any] = None,
                                           boundary_preservation_constraints_by_cluster: Optional[
                                               Dict[str, Dict[str, Any]]
                                           ] = None) -> AppSpec:
        if not observations:
            return AppSpec(source_website=website_url, website_type="app")

        if global_reasoning is None:
            global_reasoning = {}
        if loaded_route_tree is None:
            loaded_route_tree = {}
        if loaded_site_graph is None:
            loaded_site_graph = {}
        if cluster_structures is None:
            cluster_structures = {}
        if cluster_content_obs is None:
            cluster_content_obs = {}
        normalized_structure_unique_keys = {
            canonical_step_key(key)
            for key in (structure_unique_keys or set())
            if canonical_step_key(key)
        }

        obs_map: Dict[str, FrameObservation] = {self._obs_key(o): o for o in observations}

        step_labels = global_reasoning.get("step_labels", [])
        canonical_pages = global_reasoning.get("canonical_pages", [])
        canonical_routes = global_reasoning.get("canonical_routes", [])
        skipped_step_keys = self._skipped_step_keys(global_reasoning)
        page_info_map = {
            str(p.get("page_cluster_id", "")).strip(): p
            for p in canonical_pages if isinstance(p, dict) and str(p.get("page_cluster_id", "")).strip()
        }

        clusters: Dict[str, List[FrameObservation]] = {}
        obs_cluster: Dict[str, str] = {}
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
            obs = obs_map.get(obs_key)
            if obs is None:
                continue
            cluster_key = str(label.get("page_cluster_id", "")).strip()
            if not cluster_key:
                cluster_key = self._build_signature_cluster_key(obs)
            clusters.setdefault(cluster_key, []).append(obs)
            obs_cluster[obs_key] = cluster_key


        for obs in observations:
            obs_key = self._obs_key(obs)
            if obs_key in obs_cluster:
                continue
            if obs_key in skipped_step_keys:
                continue
            if self._is_external_system_observation(obs):
                skipped_step_keys.add(obs_key)
                continue
            cluster_key = self._build_signature_cluster_key(obs)
            clusters.setdefault(cluster_key, []).append(obs)
            obs_cluster[obs_key] = cluster_key


        if output_dir:
            self._save_debug_global_reasoning(
                global_reasoning, obs_cluster, clusters, output_dir
            )


        self._page_counter = 0
        max_existing_page_num = 0
        for cluster_key in clusters.keys():
            key = str(cluster_key)
            if not key.startswith("p_"):
                continue
            suffix = key[2:]
            if suffix.isdigit():
                max_existing_page_num = max(max_existing_page_num, int(suffix))
        self._page_counter = max_existing_page_num
        self._homepage_counter = 0

        cluster_to_page_ids: Dict[str, List[str]] = {}
        page_prototypes: List[PagePrototype] = []
        used_page_ids = set()
        obs_to_page_id: Dict[str, str] = {}
        split_clusters: set = set()
        for idx, cluster_key in enumerate(sorted(clusters.keys())):
            cluster_obs = clusters[cluster_key]
            if not cluster_obs:
                continue
            base_obs = self._random.choice(cluster_obs)
            pre_structure = cluster_structures.get(cluster_key, {})
            structure_data = dict(pre_structure)

            content_data = dict(cluster_content_obs.get(cluster_key, {})) if cluster_content_obs else {}
            page_meta = page_info_map.get(cluster_key, {})
            page_type = str(page_meta.get("page_type_hint", "")).strip()
            if not page_type:
                page_type = "utility"
            description_full = str(page_meta.get("description", "")).strip()
            if not description_full:
                description_full = base_obs.description or (cluster_obs[0].description if cluster_obs else "")
            page_name_full = str(page_meta.get("page_name", "")).strip()
            if not page_name_full:
                page_name_full = self._infer_page_name(page_type, description_full)
            page_name, page_name_full = self._sanitize_text_soft(page_name_full, self.page_name_soft_limit)
            description = description_full
            route_path = str(page_meta.get("route_path", "")).strip() or f"/{page_type}/{idx+1}"
            parent_route_path = str(page_meta.get("parent_route_path", "")).strip()
            if parent_route_path.lower() in {"none", "null"}:
                parent_route_path = ""
            try:
                route_level = int(page_meta.get("route_level", 0))
            except Exception:
                route_level = 0
            filename = self._infer_filename_from_cluster(cluster_key, page_type, idx, route_path=route_path)
            observed_urls = (
                sorted({o.normalized_url for o in cluster_obs if o.normalized_url})
                if self.use_url_signals else []
            )

            instance_specs = self._plan_entity_bound_instance_splits(
                cluster_key=cluster_key,
                cluster_obs=cluster_obs,
                content_data=content_data,
                page_type=page_type,
                page_name_full=page_name_full,
                base_filename=filename,
                route_path=route_path,
                boundary_preservation_constraint=(
                    boundary_preservation_constraints_by_cluster or {}
                ).get(cluster_key),
            )
            if instance_specs:
                split_clusters.add(cluster_key)
            else:
                instance_specs = [{
                    "instance_key": "",
                    "page_name_full": page_name_full,
                    "filename": filename,
                    "route_path": route_path,
                    "content_data": content_data,
                    "cluster_obs": cluster_obs,
                }]

            for spec_index, spec in enumerate(instance_specs):
                if str(cluster_key).startswith("p_") and str(cluster_key)[2:].isdigit() and cluster_key not in used_page_ids and spec_index == 0:
                    page_id = str(cluster_key)
                else:
                    page_id = self._next_page_id()
                    while page_id in used_page_ids:
                        page_id = self._next_page_id()
                used_page_ids.add(page_id)
                cluster_to_page_ids.setdefault(cluster_key, []).append(page_id)

                spec_route_path = str(spec.get("route_path", route_path)).strip() or route_path
                spec_page_name_full = str(spec.get("page_name_full", page_name_full)).strip() or page_name_full
                spec_page_name, spec_page_name_full = self._sanitize_text_soft(spec_page_name_full, self.page_name_soft_limit)
                spec_structure_data = dict(structure_data)
                spec_structure_data["cluster_id"] = cluster_key
                spec_structure_data["merged_from_count"] = len(cluster_obs)
                spec_structure_data["route_path"] = spec_route_path
                spec_structure_data["parent_route_path"] = parent_route_path
                spec_structure_data["route_level"] = max(0, route_level)
                if spec.get("instance_key"):
                    spec_structure_data["instance_key"] = spec["instance_key"]
                    spec_structure_data["source_cluster_id"] = cluster_key

                spec_content_data = dict(spec.get("content_data", content_data))
                spec_content_data["cluster_id"] = cluster_key
                spec_content_data["merged_from_count"] = len(cluster_obs)

                spec_cluster_obs = spec.get("cluster_obs", cluster_obs) or cluster_obs
                external_ui_regions: List[Dict[str, Any]] = []
                seen_external_regions = set()
                for obs in spec_cluster_obs:
                    regions = obs.page_state.get("external_ui_regions", []) if isinstance(obs.page_state, dict) else []
                    if not isinstance(regions, list):
                        continue
                    for region in regions:
                        if not isinstance(region, dict):
                            continue
                        key = (
                            str(region.get("region_type", "")).strip(),
                            str(region.get("location", "")).strip(),
                            str(region.get("description", "")).strip(),
                        )
                        if key in seen_external_regions:
                            continue
                        seen_external_regions.add(key)
                        external_ui_regions.append(dict(region))
                if external_ui_regions:
                    spec_structure_data["external_ui_regions"] = external_ui_regions

                all_screenshot_refs = sorted({o.screenshot_path for o in spec_cluster_obs})
                unique_screenshot_refs = sorted({
                    o.screenshot_path for o in spec_cluster_obs
                    if not normalized_structure_unique_keys
                    or canonical_step_key(f"{o.trace_id}:step{o.step_index}") in normalized_structure_unique_keys
                })
                spec_structure_data["resource_screenshot_refs"] = all_screenshot_refs
                if render_role_by_step:
                    _reasons = render_role_reasons or {}
                    _lifecycle = screenshot_lifecycle_by_step or {}
                    annotations: Dict[str, Dict[str, Any]] = {}
                    for o in spec_cluster_obs:
                        try:
                            sk = f"{o.trace_id}:step{int(o.step_index)}"
                        except Exception:
                            sk = f"{o.trace_id}:step{o.step_index}"
                        legacy_sk = f"{o.trace_id}:{o.step_index}"
                        render_role = (render_role_by_step or {}).get(sk) or (render_role_by_step or {}).get(legacy_sk)
                        if not render_role:
                            continue
                        lifecycle = dict(_lifecycle.get(sk) or _lifecycle.get(legacy_sk) or {})
                        annotation: Dict[str, Any] = {
                            "render_role": render_role,
                            "action": str(getattr(o, "action", "") or "").strip(),
                            "reason": _reasons.get(sk, "") or _reasons.get(legacy_sk, ""),
                        }
                        for field in (
                            "structural_role",
                            "lifecycle_role",
                            "initial_dom_eligible",
                            "action_host_authority",
                            "state_preconditions",
                        ):
                            if field in lifecycle:
                                annotation[field] = lifecycle[field]
                        annotations[sk] = annotation
                    spec_structure_data["screenshot_render_roles"] = {
                        sk: ann["render_role"] for sk, ann in annotations.items()
                    }
                    spec_structure_data["screenshot_render_annotations"] = annotations
                    lifecycle_for_page = {
                        sk: {
                            field: ann[field]
                            for field in (
                                "structural_role",
                                "lifecycle_role",
                                "initial_dom_eligible",
                                "action_host_authority",
                                "state_preconditions",
                            )
                            if field in ann
                        }
                        for sk, ann in annotations.items()
                        if any(
                            field in ann
                            for field in (
                                "structural_role",
                                "lifecycle_role",
                                "initial_dom_eligible",
                                "action_host_authority",
                                "state_preconditions",
                            )
                        )
                    }
                    if lifecycle_for_page:
                        spec_structure_data["screenshot_render_lifecycle"] = lifecycle_for_page
                    if initial_authority_kind_by_step:
                        iak_for_page = {}
                        for sk_ann in annotations:
                            iak = (initial_authority_kind_by_step or {}).get(sk_ann, "")
                            legacy_sk_ann = sk_ann.replace(":step", ":")
                            if not iak:
                                iak = (initial_authority_kind_by_step or {}).get(legacy_sk_ann, "")
                            if iak:
                                iak_for_page[sk_ann] = iak
                        if iak_for_page:
                            spec_structure_data["initial_authority_kind_by_step"] = iak_for_page

                page_prototypes.append(PagePrototype(
                    page_id=page_id,
                    page_type=page_type,
                    page_name=spec_page_name,
                    page_name_full=spec_page_name_full,
                    filename=str(spec.get("filename", filename)).strip() or filename,
                    description=description,
                    primary_functions=self._collect_primary_functions(spec_cluster_obs, structure_data=spec_structure_data, page_type=page_type, gr_primary_functions=page_meta.get("primary_functions")),
                    url_pattern=cluster_key if (self.use_url_signals and cluster_key.startswith("http")) else "",
                    observed_urls=observed_urls,
                    components=spec_structure_data.get("components", []),
                    screenshot_refs=unique_screenshot_refs,
                    confidence=max(o.confidence for o in spec_cluster_obs),
                    structure_data=spec_structure_data,
                    content_data=spec_content_data
                ))
                for obs in spec_cluster_obs:
                    obs_to_page_id[self._obs_key(obs)] = page_id

        unresolved_split_obs: List[Dict[str, str]] = []
        for obs in observations:
            obs_key = self._obs_key(obs)
            if obs_key in obs_to_page_id:
                continue
            if obs_key in skipped_step_keys:
                continue
            cluster_key = obs_cluster.get(obs_key, "")
            candidate_page_ids = cluster_to_page_ids.get(cluster_key, [])
            if not candidate_page_ids:
                continue
            if cluster_key in split_clusters and len(candidate_page_ids) > 1:


                unresolved_split_obs.append({
                    "observation_key": obs_key,
                    "cluster_key": cluster_key,
                    "candidate_page_ids": list(candidate_page_ids),
                    "reason": "obs_in_split_cluster_not_claimed_by_instance_split_group",
                })
                continue
            obs_to_page_id[obs_key] = candidate_page_ids[0]
        if unresolved_split_obs and self.logger:
            self.logger.log_warning(
                f"[InstanceSplit] {len(unresolved_split_obs)} observation(s) in split cluster(s) "
                "were not claimed by any spec_cluster_obs group; left out of obs_to_page_id. "
                "Their downstream canonical_route evidence will appear in "
                "canonical_route_materialization.json's unresolved_evidence_steps."
            )

        self._validate_consolidated_boundary_preservation(
            clusters=clusters,
            cluster_to_page_ids=cluster_to_page_ids,
            obs_to_page_id=obs_to_page_id,
            boundary_preservation_constraints_by_cluster=(
                boundary_preservation_constraints_by_cluster
            ),
        )

        (
            cluster_routes,
            materialized_routes,
            materialization_conflicts,
            unresolved_materialization_steps,
        ) = self._admit_canonical_routes_with_materialization(
            canonical_routes=canonical_routes,
            obs_to_page_id=obs_to_page_id,
            split_clusters=split_clusters,
        )
        canonical_route_materialization_payload = {
            "format": "canonical_route_materialization_v1",
            "schema_revision": 2,
            "canonical_route_count": len(canonical_routes),
            "cluster_admitted_count": len(cluster_routes),
            "materialized_admitted_count": len(materialized_routes),
            "materialized_routes": materialized_routes,
            "split_general_page_ids": sorted(str(cluster) for cluster in split_clusters),
            "multi_target_group_count": len(materialization_conflicts),
            "multi_target_groups": materialization_conflicts,
            "unresolved_evidence_step_count": len(unresolved_materialization_steps),
            "unresolved_evidence_steps": unresolved_materialization_steps,
            "notes": [
                "Multi-target groups are advisory only — single-trace runtime branching is by design.",
                "Real trace conflict bug detection lives in build_l0_transition_conflicts "
                "(requires multi-trace + multi-target on the same surface_key).",
            ],
        }
        if output_dir:
            self._save_task_interaction_debug(
                "canonical_route_materialization.json",
                canonical_route_materialization_payload,
                output_dir,
            )

        cluster_to_first_page_id = {
            cluster_key: page_ids[0]
            for cluster_key, page_ids in cluster_to_page_ids.items()
            if page_ids
        }
        edges = self._build_site_graph_from_canonical_and_materialized_routes(
            cluster_routes=cluster_routes,
            materialized_routes=materialized_routes,
            cluster_to_page_id=cluster_to_first_page_id,
        )
        if not edges and not canonical_routes:
            edges = self._build_site_graph_from_observations(traces, obs_to_page_id, obs_map=obs_map)
        nodes, route_hierarchy = self._build_site_graph_nodes_and_hierarchy(page_prototypes)
        edges = self._enrich_edges_with_page_context(edges, nodes)
        route_tree = loaded_route_tree if loaded_route_tree else self._build_route_tree(page_prototypes, website_url)
        global_navigation = self._aggregate_global_navigation(page_prototypes)
        visual_style = self._aggregate_visual_style_from_structures(page_prototypes)


        capture_actions = self._build_capture_actions_from_traces(
            traces=traces,
            step_labels=step_labels,
            obs_to_page_id=obs_to_page_id,
            cluster_to_page_ids=cluster_to_page_ids,
            obs_map=obs_map,
        )


        if output_dir:
            self._save_capture_actions(capture_actions, output_dir)
            try:
                from trace2world_pipeline.app_spec.source_trace_integrity import (
                    DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE,
                    build_source_trace_integrity_report,
                    should_block_source_trace_admission,
                    write_source_trace_integrity_artifacts,
                )

                integrity_report = build_source_trace_integrity_report(capture_actions)
                written = write_source_trace_integrity_artifacts(output_dir, integrity_report)
                blocked_trace_count = int(
                    integrity_report.get("summary", {}).get("blocked_trace_count", 0) or 0
                )
                if self.logger:
                    self.logger.log_info(
                        "[SourceTraceIntegrity] action-chain admission: "
                        f"blocked_traces={blocked_trace_count}, artifacts={written}"
                    )
                config_manager = getattr(self, "config_manager", None)
                mode = DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE
                if config_manager is not None:
                    mode = str(
                        config_manager.get(
                            "source_trace_action_chain_admission_mode",
                            config_manager.get(
                                "source_trace_action_chain_admission",
                                DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE,
                            ),
                        )
                        or DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE
                    ).strip().lower()
                if should_block_source_trace_admission(mode, integrity_report):
                    raise RuntimeError(
                        "source trace action-chain admission blocked "
                        f"{blocked_trace_count} trace(s); see {written.get('source_trace_integrity_report.json')}"
                    )
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith("source trace action-chain admission blocked"):
                    raise
                if self.logger:
                    self.logger.log_warning(
                        f"[SourceTraceIntegrity] failed to build action-chain admission report: {exc}"
                    )

        page_behaviors = self._build_page_behaviors(page_prototypes, edges, capture_actions, global_navigation)

        content = self._load_reusable_site_content(output_dir) if output_dir else None
        reused_site_content = content is not None
        if content is None:
            if self.logger:
                self.logger.log_info("[Schema] Building entity schemas from traces...")
            content = await self._build_schema_from_traces(
                traces=traces,
                obs_cluster=obs_cluster,
                cluster_structures=cluster_structures,
                cluster_content_obs=cluster_content_obs,
                canonical_pages=canonical_pages,
                page_info_map=page_info_map,
                runtime_state_observations=runtime_state_observations or {},
                output_dir=output_dir,
            )
        elif self.logger:
            self.logger.log_info(
                "[Schema] Reused existing SiteContent artifact: "
                f"{len(content.entity_schemas)} entities, {len(content.entity_relationships)} relationships"
            )
        if self.logger:
            self.logger.log_info(
                f"[Schema] Done: {len(content.entity_schemas)} entities, "
                f"{len(content.entity_relationships)} relationships"
            )

        if not reused_site_content:

            content = self._consolidate_entity_schemas(content)
            if self.logger:
                self.logger.log_info(
                    f"[Schema] After consolidation: {len(content.entity_schemas)} entities"
                )


            materialize_fk_fields(content.entity_schemas, content.entity_relationships)


            self._coerce_field_types(content.entity_schemas)

            content.entity_schemas = validate_and_fix_types(content.entity_schemas)
            if self.logger:
                type_counts = {}
                for s in content.entity_schemas:
                    for f in s.get("fields", []):
                        t = f.get("type", "string")
                        type_counts[t] = type_counts.get(t, 0) + 1
                self.logger.log_info(f"[Schema] Field types after coercion: {type_counts}")
            if output_dir:
                self._save_reusable_site_content(content, output_dir)
        elif self.logger:
            self.logger.log_info("[Schema] Skipped schema post-processing for restored SiteContent artifact")
        if output_dir:
            self._save_schema_evaluation_report(content, output_dir)
        if self._l0_schema_registry_flow_enabled():
            self._materialize_l0_schema_registry(content, output_dir)

        site_model = AppSpec(
            source_website=website_url,
            website_type=global_reasoning.get("app_type", global_reasoning.get("website_type", "")) or "app",
            trace_ids=[t.trace_id for t in traces],
            structure=SiteStructure(
                page_prototypes=page_prototypes,
                site_graph=SiteGraph(
                    graph_type="canonical_route_graph_v1",
                    edges=edges,
                    nodes=nodes,
                    route_hierarchy=route_hierarchy
                ),
                route_tree=route_tree,
                global_navigation=global_navigation,
                page_behaviors=page_behaviors
            ),
            visual_style=visual_style,
            content=content,
            metadata={
                "total_screenshots_analyzed": len(observations),
                "pipeline_version": "v3_1_global_reasoning",
                "reference_tasks": [
                    {
                        "id": t.trace_id,
                        "name": t.task,
                        "level": t.level,
                        "steps": [
                            s.action for s in t.steps
                            if s.action and s.action != "finished()"
                        ]
                    }
                    for t in traces
                ],
                "global_reasoning_enabled": self.global_reasoning_enabled,
                "global_reasoning_step_labels": len(step_labels),
                "global_reasoning_skipped_steps": len(skipped_step_keys),
                "global_reasoning_pages": len(page_prototypes),
                "global_reasoning_pages_raw": len(canonical_pages),
                "global_reasoning_pages_unmaterialized": max(0, len(canonical_pages) - len(page_prototypes)),
                "global_reasoning_routes": len(canonical_routes),
                "global_reasoning_routes_cluster_admitted": len(cluster_routes),
                "global_reasoning_routes_materialized_admitted": len(materialized_routes),
                "global_reasoning_route_materialization": {
                    "policy": "materialize_routes_touching_split_general_pages",
                    "conflict_count": len(materialization_conflicts),
                    "unresolved_evidence_step_count": len(unresolved_materialization_steps),
                },
                "use_url_signals": self.use_url_signals,
                "phase1_routing_artifacts_loaded": bool(loaded_site_graph)
            }
        )


        raw_te = global_reasoning.get("task_related_elements", {}) if global_reasoning else {}
        has_runtime_state_observations = bool(
            isinstance(runtime_state_observations, dict)
            and runtime_state_observations.get("observations")
        )
        if raw_te or capture_actions or has_runtime_state_observations:
            mapped_te = self._map_task_elements_to_pages(
                raw_task_elements=raw_te,
                cluster_to_page_ids=cluster_to_page_ids,
                obs_to_page_id=obs_to_page_id,
            )
            enriched_task_elements, enriched_behaviors = self._enrich_task_interactions(
                page_prototypes=page_prototypes,
                page_behaviors=page_behaviors,
                task_elements={
                    "elements_by_page": mapped_te,
                    "capture_actions": capture_actions,
                },
            )
            interaction_candidates = enriched_task_elements.get("interaction_candidates", [])
            if not isinstance(interaction_candidates, list):
                interaction_candidates = []
            runtime_candidates = self._build_runtime_state_interaction_candidates(
                runtime_state_observations or {},
                page_prototypes,
            )
            if runtime_candidates:
                existing_candidate_ids = {
                    str(candidate.get("candidate_id", "") or "")
                    for candidate in interaction_candidates
                    if isinstance(candidate, dict)
                }
                for candidate in runtime_candidates:
                    candidate_id = str(candidate.get("candidate_id", "") or "")
                    if candidate_id and candidate_id not in existing_candidate_ids:
                        interaction_candidates.append(candidate)
                        existing_candidate_ids.add(candidate_id)
                enriched_task_elements["interaction_candidates"] = interaction_candidates
            candidate_artifact = {
                "format": "interaction_candidates",
                "schema_revision": 1,
                "candidate_count": len(interaction_candidates),
                "candidates": interaction_candidates,
            }
            if output_dir:
                self._save_task_interaction_debug(
                    "interaction_candidates.json",
                    candidate_artifact,
                    output_dir,
                )


            config_manager = getattr(self, "config_manager", None)
            if config_manager is not None:
                task_interaction_planner_enabled = bool(
                    config_manager.get("task_interaction_contract_planner_enabled", False)
                )
                page_environment_planning_mode = resolve_page_environment_planning_mode(config_manager)
            else:
                task_interaction_planner_enabled = True
                page_environment_planning_mode = "legacy_stages"
            unified_page_environment_planning = page_environment_planning_mode == "unified"
            if task_interaction_planner_enabled and not unified_page_environment_planning:
                contract_artifact = await self._plan_task_interaction_contracts_with_model(
                    candidates=interaction_candidates,
                    page_prototypes=page_prototypes,
                    content=content,
                    output_dir=output_dir or "",
                    runtime_state_observations=runtime_state_observations or {},
                )
            else:
                if self.logger:
                    self.logger.log_info(
                        "[TaskInteraction] Skipped legacy planner stage "
                        f"(page_environment_planning_mode={page_environment_planning_mode} absorbs per-page interaction)"
                    )
                contract_artifact = {
                    "format": "task_interaction_contracts",
                    "schema_revision": 1,
                    "contracts": [],
                    "contracts_by_candidate_id": {},
                    "candidate_count": len(interaction_candidates),
                    "skipped_reason": "page_environment_planning_mode_unified",
                    "page_environment_planning_mode": page_environment_planning_mode,
                }
            site_model.task_interaction_contracts = contract_artifact
            canonicalization = self._canonicalize_l0_task_interaction_state_identity(site_model)
            if (
                output_dir
                and self._l0_schema_registry_flow_enabled()
                and canonicalization.get("created_runtime_state_count", 0)
            ):
                self._save_schema_registry_artifact(content.schema_registry, output_dir)
            if output_dir:
                self._save_task_interaction_debug(
                    "task_interaction_state_identity_canonicalization.json",
                    canonicalization,
                    output_dir,
                )
            validation = self._validate_task_interaction_contracts(
                contract_artifact.get("contracts", []),
                content,
            )
            if output_dir:
                self._save_task_interaction_debug(
                    "task_interaction_contract_validation.json",
                    validation,
                    output_dir,
                )
            if not validation.get("valid", False):
                raise RuntimeError(
                    "task_interaction_contract validation failed with {} error(s)".format(
                        validation.get("error_count", 0)
                    )
                )
            enriched_behaviors = self._apply_task_interaction_contracts(
                enriched_behaviors,
                contract_artifact.get("contracts_by_candidate_id", {}),
            )
            enriched_task_elements["continuations_by_page"] = {
                page_id: behavior.get("task_continuations", [])
                for page_id, behavior in enriched_behaviors.items()
                if isinstance(behavior, dict) and behavior.get("task_continuations")
            }
            enriched_task_elements["task_interaction_contracts"] = {
                "contract_count": len(contract_artifact.get("contracts", [])),
                "contract_ids_by_candidate_id": {
                    candidate_id: contract.get("contract_id", "")
                    for candidate_id, contract in contract_artifact.get("contracts_by_candidate_id", {}).items()
                },
            }
            site_model.structure.page_behaviors = enriched_behaviors
            site_model.task_elements = {
                "elements_by_page": mapped_te,
                "capture_actions": capture_actions,
                **enriched_task_elements,
            }

        site_model.page_signatures = self._build_page_signatures(
            page_prototypes,
            site_model.structure.page_behaviors,
        )
        site_model.canonical_route_materialization = canonical_route_materialization_payload

        return site_model

    def _load_reusable_site_content(self, output_dir: str) -> Optional["SiteContent"]:
        if not output_dir:
            return None
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        candidate_paths = [
            pom.formal("consolidation", "site_content", "site_content.json"),
            pom.formal("consolidation", "projections", "schema", "site_content.json"),


            pom.site_model_dir() / "content.json",
        ]
        decision: Dict[str, Any] = {
            "format": "schema_site_content_reuse_decision",
            "schema_revision": 1,
            "artifact_id": "consolidation.schema.site_content",
            "decision": "missing",
            "reused": False,
            "reason": "missing_candidate_artifacts",
            "would_call_llm": True,
            "would_recompute": True,
            "candidate_paths": [
                {
                    "path": os.path.relpath(str(path), output_dir),
                    "exists": os.path.exists(path),
                }
                for path in candidate_paths
            ],
            "formal_paths": [
                os.path.relpath(str(path), output_dir)
                for path in candidate_paths
            ],
            "source_path": "",
            "manual_invalidation_policy": "delete_or_deprecate_canonical_artifact_to_force_recompute",
        }
        content_path = next((path for path in candidate_paths if os.path.exists(path)), None)
        if content_path is None:
            self._save_schema_site_content_reuse_decision(decision, output_dir)
            return None
        decision["source_path"] = os.path.relpath(str(content_path), output_dir)
        try:
            with open(content_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                decision["reason"] = "invalid_json_shape"
                self._save_schema_site_content_reuse_decision(decision, output_dir)
                return None
            content = SiteContent.from_dict(raw)
            if not (
                content.entity_schemas
                or content.content_samples
                or content.task_state_contract
                or content.interaction_contract
            ):
                decision["reason"] = "empty_site_content"
                self._save_schema_site_content_reuse_decision(decision, output_dir)
                return None
            decision.update({
                "decision": "reuse",
                "reused": True,
                "reason": "canonical_artifact_present",
                "would_call_llm": False,
                "would_recompute": False,
                "entity_schema_count": len(content.entity_schemas),
                "content_sample_group_count": len(content.content_samples),
                "task_state_contract_present": bool(content.task_state_contract),
                "interaction_contract_present": bool(content.interaction_contract),
            })
            canonical_path = pom.formal("consolidation", "site_content", "site_content.json")
            if os.path.abspath(str(content_path)) != os.path.abspath(str(canonical_path)):
                self._save_reusable_site_content(content, output_dir)
            self._save_schema_site_content_reuse_decision(decision, output_dir)
            return content
        except Exception as e:
            decision["reason"] = "load_error"
            decision["error"] = str(e)
            self._save_schema_site_content_reuse_decision(decision, output_dir)
            if self.logger:
                self.logger.log_warning(f"[Schema] Failed to reuse existing SiteContent artifact: {e}")
            return None

    def _save_schema_site_content_reuse_decision(self, decision: Dict[str, Any], output_dir: str) -> None:
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        try:
            pom.write_json(
                "consolidation",
                "debug",
                "site_content/reuse_decision.json",
                decision,
                artifact_id="consolidation.site_content.reuse_decision",
                artifact_kind="debug_export",
                producer="site_model.builder.consolidation._load_reusable_site_content",
            )
            pom.write_json(
                "consolidation",
                "debug",
                "schema_site_content_reuse_decision.json",
                decision,
                artifact_id="debug.schema_site_content_reuse_decision",
                artifact_kind="debug_export",
                producer="site_model.builder.consolidation._load_reusable_site_content",
            )
        except Exception:
            pass

    def _save_reusable_site_content(self, content: "SiteContent", output_dir: str) -> None:
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        try:
            path = pom.write_json(
                "consolidation",
                "formal",
                "site_content/site_content.json",
                content.to_dict(),
                artifact_id="consolidation.site_content.site_content",
                artifact_kind="det_projection",
                producer="site_model.builder.consolidation._save_reusable_site_content",
            )
            pom.write_json(
                "consolidation",
                "formal",
                "projections/schema/site_content.json",
                content.to_dict(),
                artifact_id="projections.schema.site_content",
                artifact_kind="det_projection",
                producer="site_model.builder.consolidation._save_reusable_site_content",
                register=False,
            )
            if self.logger:
                self.logger.log_info(f"[Schema] Saved reusable SiteContent artifact: {path}")
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Schema] Failed to save reusable SiteContent artifact: {e}")

    async def _build_schema_from_traces(
        self,
        traces: List["TraceData"],
        obs_cluster: Dict[str, str],
        cluster_structures: Dict[str, Dict[str, Any]],
        cluster_content_obs: Dict[str, Dict[str, Any]],
        canonical_pages: List[Dict],
        page_info_map: Dict[str, Dict],
        runtime_state_observations: Dict[str, Any] = None,
        output_dir: str = "",
    ) -> "SiteContent":
        if not cluster_content_obs:
            return SiteContent()

        schema_candidate_ir = self._build_schema_candidate_ir(
            cluster_content_obs,
            page_info_map,
        )
        page_execution_obligations = self._build_trace_action_packs_for_schema_generation(
            traces,
            obs_cluster,
            page_info_map,
        )
        schema_obligations = self._build_schema_obligations_for_schema_generation(
            runtime_state_observations or {},
            page_execution_obligations=page_execution_obligations,
        )
        if schema_obligations:
            schema_candidate_ir["schema_obligations"] = schema_obligations
            schema_candidate_ir["schema_obligation_count"] = len(schema_obligations)
            schema_candidate_ir["schema_obligation_source"] = (
                "runtime_state_observations.derived.runtime_state_obligations+reference_trace_action_packs"
            )
        if output_dir:
            self._save_schema_candidate_ir(schema_candidate_ir, output_dir)
        entity_schemas: List[Dict[str, Any]] = []
        entity_relationships: List[Dict[str, Any]] = []
        rejected_candidates: List[Dict[str, Any]] = []
        local_to_global_entity: Dict[str, Any] = {}
        boundary_plan: Optional[Dict[str, Any]] = None
        global_execution_schema: Dict[str, Any] = {}
        schema_inference_raw: Dict[str, Any] = {}
        schema_inference_prompt = ""
        schema_inference_prompts: Dict[str, str] = {}
        schema_inference_raw_outputs: Dict[str, Dict[str, Any]] = {}
        routing_context = self._build_routing_context(canonical_pages)
        global_structure_context = self._build_global_structure_context(
            cluster_structures,
            page_info_map,
            cluster_content_obs=cluster_content_obs,
        )
        content_summary = self._build_content_summary(
            cluster_content_obs,
            page_info_map,
        )
        all_traces_context = self._build_schema_inference_trace_context(
            traces,
            obs_cluster,
            page_info_map,
            cluster_content_obs=cluster_content_obs,
            cluster_structures=cluster_structures,
        )

        entity_schemas, entity_relationships = self._build_candidate_seed_entity_schemas(
            schema_candidate_ir,
            rejected_candidates=[],
        )
        boundary_plan = await self._plan_schema_boundaries(
            schema_candidate_ir,
            entity_schemas,
            rejected_candidates,
            local_to_global_entity,
        )
        schema_projection = self._build_schema_candidate_projection(
            schema_candidate_ir,
            entity_schemas,
            rejected_candidates,
            local_to_global_entity,
            planned_decisions=boundary_plan.get("decisions", []),
            projection_plan=boundary_plan,
        )
        if self.logger:
            self.logger.log_info(
                f"[Schema] boundary_planner: {len(entity_schemas)} seed entities, "
                f"{len(schema_projection.get('decisions', []))} boundary decisions"
            )
        if schema_obligations:
            (
                global_execution_schema,
                schema_inference_prompt,
                schema_inference_raw,
                schema_inference_prompts,
                schema_inference_raw_outputs,
            ) = await self._infer_canonical_global_execution_schema(
                traces_context=all_traces_context,
                routing_context=routing_context,
                global_structure_context=global_structure_context,
                content_summary=content_summary,
                schema_candidate_ir=schema_candidate_ir,
            )

        entity_schemas, entity_relationships = self._filter_schemas_by_projection(
            entity_schemas,
            entity_relationships,
            schema_projection,
        )
        entity_schemas, entity_relationships = self._dedupe_entity_schemas_by_name(
            entity_schemas,
            entity_relationships,
        )
        if output_dir:
            self._save_schema_projection_artifacts(
                schema_projection,
                output_dir,
                boundary_plan=boundary_plan,
            )

        content_samples = self._aggregate_content_samples(cluster_content_obs)
        content_samples = self._project_content_samples(content_samples, schema_projection)
        runtime_state_samples = self._aggregate_runtime_state_samples(cluster_content_obs)
        runtime_state_samples = self._project_content_samples(runtime_state_samples, schema_projection)
        entity_schemas, entity_relationships = self._prune_unsupported_entity_schemas_after_projection(
            entity_schemas,
            entity_relationships,
            schema_projection,
            content_samples,
        )
        entity_schemas, entity_relationships = self._prune_low_durability_entity_schemas_after_projection(
            entity_schemas,
            entity_relationships,
            schema_projection,
            schema_candidate_ir,
        )
        content_constraints = self._extract_content_constraints(content_samples)
        if output_dir:
            self._save_global_execution_schema_artifacts(
                schema_candidate_ir=schema_candidate_ir,
                global_execution_schema=global_execution_schema,
                output_dir=output_dir,
                traces_context=all_traces_context,
                global_structure_context=global_structure_context,
                content_summary=content_summary,
                schema_inference_prompt=schema_inference_prompt,
                schema_inference_raw=schema_inference_raw,
                schema_inference_prompts=schema_inference_prompts,
                schema_inference_raw_outputs=schema_inference_raw_outputs,
            )


        entity_comp_sources: Dict[str, set] = {}
        for cluster_key, obs in cluster_content_obs.items():
            ecm = obs.get("entity_component_mapping", [])
            if not isinstance(ecm, list):
                continue
            for mapping in ecm:
                if not isinstance(mapping, dict):
                    continue
                ename = str(mapping.get("entity", "")).strip()
                comp = str(mapping.get("source_component", "")).strip()
                if ename and comp:
                    entity_comp_sources.setdefault(ename, set()).add(comp)
        entity_component_sources = {k: sorted(v) for k, v in entity_comp_sources.items()}

        return SiteContent(
            entity_schemas=entity_schemas,
            entity_relationships=entity_relationships,
            content_samples=content_samples,
            runtime_state_samples=runtime_state_samples,
            content_constraints=content_constraints,
            entity_component_sources=entity_component_sources,
            task_state_contract=schema_projection.get("task_state_contract", {}),
            interaction_contract=schema_projection.get("interaction_contract", {}),
            global_execution_schema=global_execution_schema,
            success_evidence_hints=schema_projection.get("success_evidence_hints", {}),
            schema_candidate_projection=schema_projection.get("decisions", []),
        )

    def _build_schema_inference_trace_context(
        self,
        traces: List["TraceData"],
        obs_cluster: Dict[str, str],
        page_info_map: Dict[str, Dict],
        *,
        cluster_content_obs: Optional[Mapping[str, Any]] = None,
        cluster_structures: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:

        all_traces_context: List[Dict[str, Any]] = []
        seen_clusters_global: set = set()
        content_by_cluster = cluster_content_obs or {}
        structures_by_cluster = cluster_structures or {}
        for trace in traces:
            trace_entry = {
                "trace_id": trace.trace_id,
                "task": trace.task,
                "steps": [],
            }
            for step in trace.steps:
                obs_key = f"{trace.trace_id}:{step.step_index}"
                cluster_key = obs_cluster.get(obs_key, "")
                if not cluster_key:
                    continue
                page_meta = page_info_map.get(cluster_key, {}) if cluster_key else {}
                step_entry: Dict[str, Any] = {
                    "step": step.step_index,
                    "page": page_meta.get("page_name", "") or cluster_key,
                    "page_type": page_meta.get("page_type_hint", ""),
                    "action": (step.action or "")[:240],
                }

                structure = (
                    structures_by_cluster.get(cluster_key, {})
                    if isinstance(structures_by_cluster, Mapping)
                    else {}
                )
                components = self._compact_schema_trace_components(structure)
                if components:
                    step_entry["components"] = components

                content_obs = (
                    content_by_cluster.get(cluster_key, {})
                    if isinstance(content_by_cluster, Mapping)
                    else {}
                )
                if cluster_key and cluster_key not in seen_clusters_global:
                    extracted_items = (
                        content_obs.get("extracted_items", {})
                        if isinstance(content_obs, Mapping)
                        else {}
                    )
                    compact_items = self._compact_schema_trace_extracted_items(extracted_items)
                    if compact_items:
                        step_entry["extracted_items"] = compact_items
                    seen_clusters_global.add(cluster_key)

                interactive_elements = getattr(step, "interactive_elements", None)
                compact_interactive = self._compact_schema_trace_interactive_elements(
                    interactive_elements
                )
                if compact_interactive:
                    step_entry["interactive_elements"] = compact_interactive

                trace_entry["steps"].append(step_entry)
            if trace_entry["steps"]:
                all_traces_context.append(trace_entry)
        return all_traces_context

    def _compact_schema_trace_components(self, structure: Any) -> List[Dict[str, Any]]:
        if not isinstance(structure, Mapping):
            return []
        components = structure.get("components", [])
        if not isinstance(components, list):
            return []
        compact: List[Dict[str, Any]] = []
        for component in components:
            if not isinstance(component, Mapping):
                continue
            if component.get("is_transient"):
                continue
            ctype = str(component.get("type") or component.get("component_type") or "").strip()
            if not ctype:
                continue
            entry: Dict[str, Any] = {"type": ctype[:80]}
            component_id = str(
                component.get("component_id") or component.get("id") or component.get("name") or ""
            ).strip()
            if component_id:
                entry["id"] = component_id[:80]
            label = str(component.get("label") or component.get("text") or component.get("name") or "").strip()
            if label and label != component_id:
                entry["label"] = label[:120]
            role = str(component.get("semantic_role") or component.get("role") or "").strip()
            if role:
                entry["role"] = role[:80]
            item_count = component.get("item_count")
            if isinstance(item_count, (int, float)) and item_count:
                entry["item_count"] = int(item_count)
            compact.append(entry)
            if len(compact) >= 10:
                break
        return compact

    def _compact_schema_trace_extracted_items(self, extracted_items: Any) -> Dict[str, List[Dict[str, Any]]]:
        if not isinstance(extracted_items, Mapping):
            return {}
        compact: Dict[str, List[Dict[str, Any]]] = {}
        for entity_name, items in extracted_items.items():
            if entity_name == "_metadata" or not isinstance(items, list):
                continue
            item_rows: List[Dict[str, Any]] = []
            for item in items[:4]:
                if not isinstance(item, Mapping):
                    continue
                row: Dict[str, Any] = {}
                for key, value in list(item.items())[:8]:
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        row[str(key)[:80]] = value if not isinstance(value, str) else value[:160]
                if row:
                    item_rows.append(row)
            if item_rows:
                compact[str(entity_name)[:80]] = item_rows
            if len(compact) >= 6:
                break
        return compact

    def _compact_schema_trace_interactive_elements(self, interactive_elements: Any) -> List[Dict[str, Any]]:
        if not isinstance(interactive_elements, list):
            return []
        compact: List[Dict[str, Any]] = []
        keep_keys = (
            "id",
            "component_id",
            "type",
            "role",
            "label",
            "text",
            "name",
            "placeholder",
            "value",
            "checked",
            "selected",
            "disabled",
        )
        for element in interactive_elements:
            if not isinstance(element, Mapping):
                continue
            row: Dict[str, Any] = {}
            for key in keep_keys:
                value = element.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    text_key = str(key)
                    row[text_key] = value if not isinstance(value, str) else value[:160]
            if row:
                compact.append(row)
            if len(compact) >= 6:
                break
        return compact

    def _schema_obligations_payload_from_candidate_ir(
        self,
        schema_candidate_ir: Dict[str, Any],
    ) -> Dict[str, Any]:
        obligations = (
            schema_candidate_ir.get("schema_obligations", [])
            if isinstance(schema_candidate_ir, dict)
            else []
        )
        obligations = [item for item in obligations if isinstance(item, dict)]
        return {
            "format": "schema_obligations_v1",
            "source": "site_model.builder.consolidation.schema_candidate_ir",
            "obligation_count": len(obligations),
            "obligations": obligations,
        }

    def _build_global_schema_generation_context(
        self,
        *,
        schema_candidate_ir: Dict[str, Any],
        traces_context: List[Dict[str, Any]],
        global_structure_context: str = "",
        content_summary: str = "",
    ) -> Dict[str, Any]:
        return build_schema_generation_context(
            schema_candidate_ir=schema_candidate_ir,
            schema_obligations=self._schema_obligations_payload_from_candidate_ir(schema_candidate_ir),
            traces_context=traces_context,
            global_structure_context=global_structure_context,
            content_summary=content_summary,
        )

    async def _infer_canonical_global_execution_schema(
        self,
        *,
        traces_context: List[Dict[str, Any]],
        routing_context: str,
        global_structure_context: str,
        content_summary: str,
        schema_candidate_ir: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any], Dict[str, str], Dict[str, Dict[str, Any]]]:
        schema_generation_context = self._build_global_schema_generation_context(
            schema_candidate_ir=schema_candidate_ir,
            traces_context=traces_context,
            global_structure_context=global_structure_context,
            content_summary=content_summary,
        )
        base_candidate_context = self._build_schema_inference_candidate_context(schema_candidate_ir)
        if isinstance(base_candidate_context, dict):
            base_candidate_context["schema_generation_context"] = schema_generation_context.get("prompt_view", {})
            base_candidate_context["schema_generation_context_artifact_format"] = schema_generation_context.get("format")
        chunks = self._schema_generation_chunks(schema_generation_context)
        if len(chunks) <= 1:
            prompt = self._build_schema_inference_prompt(
                traces_context=traces_context,
                routing_context=routing_context,
                global_structure_context=global_structure_context,
                content_summary=content_summary,
                schema_candidate_ir=base_candidate_context,
            )
            result = await self._call_schema_inference_json(
                prompt,
                stage="site_model_schema_inference",
            )
            global_schema = self._normalize_schema_inference_global_execution_schema(
                result,
                schema_obligations=schema_candidate_ir.get("schema_obligations", []),
                require_when_obligations=True,
            )
            return (
                global_schema,
                prompt,
                result,
                {"site_model_schema_inference": prompt},
                {"site_model_schema_inference": result},
            )

        prompts: Dict[str, str] = {}
        raw_outputs: Dict[str, Dict[str, Any]] = {}
        normalized_chunk_outputs: List[Dict[str, Any]] = []
        chunk_raw_outputs_for_merge: List[Dict[str, Any]] = []
        all_obligations = [
            item for item in schema_candidate_ir.get("schema_obligations", [])
            if isinstance(item, dict)
        ]
        for idx, chunk in enumerate(chunks):
            chunk_key = f"site_model_schema_inference_chunk_{idx:03d}"
            chunk_obligations = self._schema_obligations_for_chunk(all_obligations, chunk)
            chunk_candidate_context = self._build_schema_inference_chunk_candidate_context(
                base_candidate_context=base_candidate_context,
                schema_generation_context=schema_generation_context,
                chunk=chunk,
                chunk_obligations=chunk_obligations,
            )
            prompt = self._build_schema_inference_chunk_prompt(
                traces_context=traces_context,
                routing_context=routing_context,
                global_structure_context=global_structure_context,
                content_summary=content_summary,
                schema_candidate_ir=chunk_candidate_context,
                chunk=chunk,
                chunk_index=idx,
                chunk_count=len(chunks),
            )
            result = await self._call_schema_inference_json(
                prompt,
                stage="site_model_schema_inference",
            )
            self._normalize_schema_inference_global_execution_schema(
                result,
                schema_obligations=chunk_obligations,
                require_when_obligations=True,
            )
            prompts[chunk_key] = prompt
            raw_outputs[chunk_key] = result
            normalized_chunk_outputs.append({
                "chunk_id": chunk.get("chunk_id", ""),
                "family_id": chunk.get("family_id", ""),
                "field_paths": chunk.get("field_paths", []),
                "schema": result.get("global_execution_schema", {}),
            })
            chunk_raw_outputs_for_merge.append({
                "chunk_id": chunk.get("chunk_id", ""),
                "family_id": chunk.get("family_id", ""),
                "field_paths": chunk.get("field_paths", []),
                "raw_output": result,
            })

        merge_candidate_context = deepcopy(base_candidate_context)
        if isinstance(merge_candidate_context, dict):
            merge_candidate_context["schema_generation_context"] = schema_generation_context.get("prompt_view", {})
            merge_candidate_context["schema_generation_merge_context"] = {
                "format": "schema_inference_merge_context_v1",
                "chunk_count": len(chunks),
                "chunks": [
                    {
                        "chunk_id": chunk.get("chunk_id", ""),
                        "family_id": chunk.get("family_id", ""),
                        "field_paths": chunk.get("field_paths", []),
                    }
                    for chunk in chunks
                ],
            }
        merge_prompt = self._build_schema_inference_merge_prompt(
            traces_context=traces_context,
            routing_context=routing_context,
            global_structure_context=global_structure_context,
            content_summary=content_summary,
            schema_candidate_ir=merge_candidate_context,
            chunk_outputs=chunk_raw_outputs_for_merge,
        )
        merge_result = await self._call_schema_inference_json(
            merge_prompt,
            stage="site_model_schema_inference_merge",
        )
        global_schema = self._normalize_schema_inference_global_execution_schema(
            merge_result,
            schema_obligations=all_obligations,
            require_when_obligations=True,
        )
        prompts["site_model_schema_inference_merge"] = merge_prompt
        raw_outputs["site_model_schema_inference_merge"] = merge_result
        prompts["site_model_schema_inference"] = merge_prompt
        raw_outputs["site_model_schema_inference"] = merge_result
        raw_outputs["site_model_schema_inference_chunks"] = merge_schema_inference_outputs(
            normalized_chunk_outputs
        )
        return global_schema, merge_prompt, merge_result, prompts, raw_outputs

    async def _call_schema_inference_json(
        self,
        prompt: str,
        *,
        stage: str,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        repair_suffix = ""
        for attempt in range(1, 4):
            try:
                result, _usage = await call_openai_api_json_async(
                    [{"role": "user", "content": prompt + repair_suffix}],
                    reasoning_effort=self.reasoning_effort,
                    model=self.model,
                    stage=stage,
                )
            except LLMJsonResponseError as exc:


                last_error = exc
                self._save_schema_inference_attempt(
                    stage=stage,
                    attempt=attempt,
                    result={
                        "error_type": "provider_json_contract_failure",
                        "error": str(exc),
                    },
                    validation_error=str(exc),
                )
                repair_suffix = (
                    "\n\n## Previous Provider JSON Contract Failure\n"
                    f"Attempt {attempt} returned an invalid JSON response after provider repair: {exc}. "
                    "Produce the complete schema again from the authoritative inputs above. "
                    "Return JSON only, with `global_execution_schema` as a top-level object "
                    "and top-level `entities` and `relationships` arrays. Do not return a "
                    "schema fragment, commentary, markdown, or an entity-only object."
                )
                continue
            except Exception as exc:
                raise RuntimeError(
                    f"{stage} failed while producing canonical global_execution_schema"
                ) from exc
            try:
                parsed = parse_schema_inference_output(result)
            except Exception as exc:
                last_error = exc
                self._save_schema_inference_attempt(
                    stage=stage,
                    attempt=attempt,
                    result=result,
                    validation_error=str(exc),
                )
                top_level_keys = (
                    sorted(str(key) for key in result)
                    if isinstance(result, dict)
                    else []
                )
                repair_suffix = (
                    "\n\n## Previous Response Contract Failure\n"
                    f"Attempt {attempt} was rejected: {exc}. "
                    f"Observed top-level keys: {top_level_keys}. "
                    "Produce the complete schema again from the authoritative inputs above. "
                    "Return JSON only, with `global_execution_schema` as a top-level object "
                    "and top-level `entities` and `relationships` arrays. Do not return a "
                    "schema fragment, commentary, markdown, or an entity-only object."
                )
                continue
            self._save_schema_inference_attempt(
                stage=stage,
                attempt=attempt,
                result=parsed,
                validation_error="",
            )
            return parsed
        raise RuntimeError(
            f"{stage} failed while producing canonical global_execution_schema "
            "after 3 contract-repair attempts"
        ) from last_error

    def _save_schema_inference_attempt(
        self,
        *,
        stage: str,
        attempt: int,
        result: Any,
        validation_error: str,
    ) -> None:
        output_dir = str(getattr(self, "_current_output_dir", "") or "").strip()
        if not output_dir:
            return
        target_dir = os.path.join(
            output_dir,
            "phase_consolidation",
            "schema_registry",
            "schema_generation_raw_outputs",
        )
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(
            target_dir,
            f"{_safe_schema_artifact_key(stage)}_attempt_{attempt:02d}.json",
        )
        with open(target_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "format": "schema_inference_attempt_v1",
                    "stage": stage,
                    "attempt": attempt,
                    "status": "pass" if not validation_error else "invalid",
                    "validation_error": validation_error,
                    "raw_output": result,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )

    def _schema_generation_chunks(
        self,
        schema_generation_context: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        prompt_view = (
            schema_generation_context.get("prompt_view", {})
            if isinstance(schema_generation_context, Mapping)
            else {}
        )
        chunks = prompt_view.get("chunks", []) if isinstance(prompt_view, Mapping) else []
        if not isinstance(chunks, list):
            return []
        return [dict(item) for item in chunks if isinstance(item, Mapping)]

    def _schema_obligations_for_chunk(
        self,
        obligations: List[Dict[str, Any]],
        chunk: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        family_id = str(chunk.get("family_id") or "").strip()
        field_paths = {
            str(item).strip()
            for item in chunk.get("field_paths", [])
            if str(item or "").strip()
        }
        selected: List[Dict[str, Any]] = []
        for obligation in obligations:
            if not isinstance(obligation, dict):
                continue
            obligation_family = str(obligation.get("family_id") or "").strip()
            field_path = str(obligation.get("field_path") or "").strip()
            if family_id and obligation_family != family_id:
                continue
            if field_paths and field_path not in field_paths:
                continue
            selected.append(obligation)
        return selected

    def _build_schema_inference_chunk_candidate_context(
        self,
        *,
        base_candidate_context: Mapping[str, Any],
        schema_generation_context: Mapping[str, Any],
        chunk: Mapping[str, Any],
        chunk_obligations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        context = deepcopy(dict(base_candidate_context))
        prompt_view = (
            schema_generation_context.get("prompt_view", {})
            if isinstance(schema_generation_context, Mapping)
            else {}
        )
        family_id = str(chunk.get("family_id") or "").strip()
        field_paths = [
            str(item).strip()
            for item in chunk.get("field_paths", [])
            if str(item or "").strip()
        ]
        requirements_by_family = {}
        if isinstance(prompt_view, Mapping):
            full_table = prompt_view.get("requirements_by_family", {})
            if isinstance(full_table, Mapping):
                rows = full_table.get(family_id, [])
                if isinstance(rows, list):
                    scoped_rows = [
                        row for row in rows
                        if isinstance(row, list) and row and str(row[0]).strip() in set(field_paths)
                    ]
                    if scoped_rows:
                        requirements_by_family[family_id] = scoped_rows
        context["schema_generation_context"] = {
            "format": "schema_generation_prompt_view_v1",
            "purpose": "Chunk-scoped schema inference prompt view.",
            "chunk_id": chunk.get("chunk_id", ""),
            "chunk_scope": dict(chunk),
            "obligation_count": len(chunk_obligations),
            "field_owner_requirement_count": sum(len(rows) for rows in requirements_by_family.values()),
            "requirements_by_family": requirements_by_family,
            "schema_obligations": chunk_obligations,
            "full_context_summary": {
                "total_obligation_count": prompt_view.get("obligation_count", 0) if isinstance(prompt_view, Mapping) else 0,
                "total_chunk_count": len(prompt_view.get("chunks", [])) if isinstance(prompt_view, Mapping) else 0,
                "all_families": sorted((prompt_view.get("requirements_by_family", {}) or {}).keys())
                if isinstance(prompt_view, Mapping) and isinstance(prompt_view.get("requirements_by_family", {}), Mapping)
                else [],
            },
        }
        return context

    def _normalize_schema_inference_global_execution_schema(
        self,
        raw_result: Dict[str, Any],
        *,
        schema_obligations: List[Dict[str, Any]],
        require_when_obligations: bool,
    ) -> Dict[str, Any]:
        obligations_payload = {
            "format": "schema_obligations_v1",
            "obligation_count": len(schema_obligations),
            "obligations": schema_obligations,
        }
        try:
            global_schema = normalize_canonical_global_execution_schema(
                raw_result,
                source={
                    "source_kind": "api_llm_schema_inference",
                    "stage": "site_model_schema_inference",
                    "producer": "site_model.builder.consolidation._build_schema_from_traces",
                    "canonical": True,
                },
            )
        except Exception as exc:
            if require_when_obligations and schema_obligations:
                raise RuntimeError(
                    "site_model_schema_inference did not return a canonical "
                    "global_execution_schema for existing schema obligations"
                ) from exc
            return {}
        global_schema = complete_global_schema_from_obligations(
            global_schema,
            obligations_payload,
        )
        global_schema = mark_unreferenced_global_fields_optional(
            global_schema,
            obligations_payload,
            reason=(
                "Schema inference proposed a field that is not backed by schema_obligations; "
                "required owner authority remains limited to formal obligations."
            ),
        )

        validation = validate_canonical_global_execution_schema(
            global_schema,
            schema_obligations=obligations_payload,
            require_api_llm_source=True,
        )
        if validation.get("status") != "pass":
            unresolved_sample = validation.get("unresolved_obligations", [])[:5]
            raise RuntimeError(
                "canonical global_execution_schema validation failed: "
                f"{validation.get('errors', [])[:5]}; "
                f"unresolved_sample={unresolved_sample}"
            )
        return global_schema

    def _save_schema_candidate_ir(self, schema_candidate_ir: Dict[str, Any], output_dir: str) -> None:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        path = pom.debug("consolidation", "schema_candidates.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(schema_candidate_ir, f, indent=2, ensure_ascii=False)
            if self.logger:
                self.logger.log_info(f"[Schema] Saved schema candidate IR: {path}")
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Schema] Failed to save schema candidate IR: {e}")

    def _build_schema_obligations_for_schema_generation(
        self,
        runtime_state_observations: Dict[str, Any],
        page_execution_obligations: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(runtime_state_observations, dict):
            runtime_state_observations = {}
        derived = runtime_state_observations.get("derived", {})
        runtime_state_obligations = (
            derived.get("runtime_state_obligations", {})
            if isinstance(derived, dict)
            else {}
        )
        payload = collect_schema_obligations(
            runtime_state_obligations=runtime_state_obligations
            if isinstance(runtime_state_obligations, dict)
            else None,
            page_execution_obligations=page_execution_obligations
            if isinstance(page_execution_obligations, dict)
            else None,
        )
        obligations = payload.get("obligations", []) if isinstance(payload, dict) else []
        return [item for item in obligations if isinstance(item, dict)]

    def _build_trace_action_packs_for_schema_generation(
        self,
        traces: List["TraceData"],
        obs_cluster: Dict[str, str],
        page_info_map: Dict[str, Dict],
    ) -> Dict[str, Any]:

        action_packs: List[Dict[str, Any]] = []
        for trace in traces or []:
            trace_id = str(getattr(trace, "trace_id", "") or "").strip()
            if not trace_id:
                continue
            raw_steps = list(getattr(trace, "steps", []) or [])
            try:
                steps = sorted(raw_steps, key=lambda step: int(getattr(step, "step_index", 0) or 0))
            except Exception:
                steps = raw_steps
            pack_steps: List[Dict[str, Any]] = []
            for idx, step in enumerate(steps):
                step_index = getattr(step, "step_index", idx)
                source_filename = self._schema_generation_step_filename(
                    trace_id,
                    step_index,
                    obs_cluster,
                    page_info_map,
                )
                next_step_index = (
                    getattr(steps[idx + 1], "step_index", None)
                    if idx + 1 < len(steps)
                    else None
                )
                target_filename = (
                    self._schema_generation_step_filename(
                        trace_id,
                        next_step_index,
                        obs_cluster,
                        page_info_map,
                    )
                    if next_step_index is not None
                    else source_filename
                )
                action_text = self._schema_generation_step_action_text(step)
                if not (source_filename or target_filename or action_text):
                    continue
                pack_steps.append({
                    "step_index": step_index,
                    "pre_state": {"filename": source_filename},
                    "expected_post_state": {"filename": target_filename or source_filename},
                    "action": {
                        "operation": self._schema_generation_action_operation(action_text),
                        "intent": action_text,
                        "raw_action": str(getattr(step, "raw_action", "") or ""),
                        "action_text": action_text,
                    },
                })
            if pack_steps:
                action_packs.append({
                    "trace_id": trace_id,
                    "task_instruction": str(getattr(trace, "task", "") or ""),
                    "steps": pack_steps,
                })
        return {
            "format": "schema_generation_pre_pageenv_action_packs_v1",
            "action_pack_count": len(action_packs),
            "action_packs": action_packs,
        }

    def _schema_generation_step_filename(
        self,
        trace_id: str,
        step_index: Any,
        obs_cluster: Dict[str, str],
        page_info_map: Dict[str, Dict],
    ) -> str:
        if step_index is None:
            return ""
        raw_index = str(step_index).strip()
        candidate_keys = [
            f"{trace_id}:{raw_index}",
            f"{trace_id}:step{raw_index}",
            canonical_step_key(f"{trace_id}:{raw_index}"),
            canonical_step_key(f"{trace_id}:step{raw_index}"),
        ]
        cluster_id = ""
        for key in candidate_keys:
            if key and key in obs_cluster:
                cluster_id = str(obs_cluster.get(key) or "").strip()
                break
        if not cluster_id:
            return ""
        meta = page_info_map.get(cluster_id, {}) if isinstance(page_info_map, dict) else {}
        for key in (
            "filename",
            "page_filename",
            "materialized_filename",
            "page_id",
            "page_cluster_id",
            "page_name",
        ):
            value = str(meta.get(key, "") or "").strip() if isinstance(meta, dict) else ""
            if value:
                return value
        return cluster_id

    def _schema_generation_step_action_text(self, step: Any) -> str:
        parts: List[str] = []
        for attr in ("action", "raw_action", "thought"):
            value = getattr(step, attr, "")
            if value:
                parts.append(str(value))
        payload = getattr(step, "raw_action_payload", None)
        if isinstance(payload, Mapping):
            for key in ("action_type", "operation", "intent", "text", "value", "target", "label"):
                value = payload.get(key)
                if value:
                    parts.append(str(value))
        return " ".join(part.strip() for part in parts if part and part.strip())

    def _schema_generation_action_operation(self, action_text: str) -> str:
        text = str(action_text or "").lower()
        if any(token in text for token in ("fill", "type", "输入", "填写")):
            return "fill"
        if any(token in text for token in ("press", "delete", "删除", "backspace")):
            return "press"
        if any(token in text for token in ("click", "tap", "点击", "打开", "选择")):
            return "click"
        return "action"

    def _save_global_execution_schema_artifacts(
        self,
        *,
        schema_candidate_ir: Dict[str, Any],
        global_execution_schema: Dict[str, Any],
        output_dir: str,
        traces_context: Optional[List[Dict[str, Any]]] = None,
        global_structure_context: str = "",
        content_summary: str = "",
        schema_inference_prompt: str = "",
        schema_inference_raw: Optional[Dict[str, Any]] = None,
        schema_inference_prompts: Optional[Mapping[str, str]] = None,
        schema_inference_raw_outputs: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> None:
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        obligations_payload = self._schema_obligations_payload_from_candidate_ir(schema_candidate_ir)
        context = build_schema_generation_context(
            schema_candidate_ir=schema_candidate_ir,
            schema_obligations=obligations_payload,
            traces_context=traces_context or [],
            global_structure_context=global_structure_context,
            content_summary=content_summary,
        )
        artifacts = {
            "schema_registry/schema_obligations.json": obligations_payload,
            "schema_registry/schema_generation_context.json": context,
        }
        validation: Dict[str, Any] = {}
        if global_execution_schema:
            validation = validate_canonical_global_execution_schema(
                global_execution_schema,
                schema_obligations=obligations_payload,
                require_api_llm_source=True,
            )
            if validation.get("status") != "pass":
                raise RuntimeError(
                    "Refusing to publish invalid canonical global_execution_schema: "
                    f"{validation.get('errors', [])[:5]}"
                )
            artifacts.update({
                "schema_registry/global_execution_schema.json": global_execution_schema,
                "projections/schema/global_execution_schema.json": global_execution_schema,
                "schema_registry/global_execution_schema_validation.json": validation,
            })
        for rel_path, payload in artifacts.items():
            try:
                pom.write_json(
                    "consolidation",
                    "formal",
                    rel_path,
                    payload,
                    artifact_id=f"consolidation.{rel_path.replace('/', '.').removesuffix('.json')}",
                    artifact_kind="det_projection",
                    producer="site_model.builder.consolidation._build_schema_from_traces",
                    register=not rel_path.startswith("projections/"),
                )
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save global schema artifact {rel_path}: {e}")
        prompt_map: Dict[str, str] = {}
        if schema_inference_prompt:
            prompt_map["site_model_schema_inference"] = schema_inference_prompt
        if isinstance(schema_inference_prompts, Mapping):
            for key, prompt in schema_inference_prompts.items():
                if isinstance(prompt, str) and prompt:
                    prompt_map[_safe_schema_artifact_key(key)] = prompt
        for key, prompt in sorted(prompt_map.items()):
            try:
                prompt_path = pom.formal(
                    "consolidation",
                    f"schema_registry/schema_generation_prompts/{key}.txt",
                )
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(prompt, encoding="utf-8")
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save schema inference prompt {key}: {e}")

        raw_map: Dict[str, Dict[str, Any]] = {}
        if isinstance(schema_inference_raw, dict) and schema_inference_raw:
            raw_map["site_model_schema_inference"] = schema_inference_raw
        if isinstance(schema_inference_raw_outputs, Mapping):
            for key, payload in schema_inference_raw_outputs.items():
                if isinstance(payload, dict) and payload:
                    raw_map[_safe_schema_artifact_key(key)] = payload
        for key, payload in sorted(raw_map.items()):
            try:
                pom.write_json(
                    "consolidation",
                    "formal",
                    f"schema_registry/schema_generation_raw_outputs/{key}.json",
                    payload,
                    artifact_id=f"consolidation.schema_registry.schema_generation_raw_outputs.{key}",
                    artifact_kind="api_semantic_source",
                    producer="site_model.builder.consolidation._build_schema_from_traces",
                    register=True,
                )
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save schema inference raw output {key}: {e}")

    def _save_schema_projection_artifacts(
        self,
        schema_projection: Dict[str, Any],
        output_dir: str,
        boundary_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        debug_dir = pom.ensure_dir("consolidation", "debug", "schema_projection")
        boundary_context = {}
        if isinstance(boundary_plan, dict):
            boundary_context = boundary_plan.get("boundary_context", {}) or {}
        legacy_projection_units = {}
        legacy_boundary_families = {}
        legacy_projection_shards = {}
        if isinstance(boundary_context, dict):
            legacy_projection_units = {
                "format": "schema_projection_units_v1",
                "candidate_count": boundary_context.get("candidate_count", 0),
                "candidate_index": boundary_context.get("candidate_index", {}),
                "unit_count": len(boundary_context.get("units", []) or []),
                "units": boundary_context.get("units", []),
                "boundary_families": {
                    "format": "schema_boundary_families_v1",
                    "families": boundary_context.get("families", []),
                },
            }
            legacy_boundary_families = {
                "format": "schema_boundary_families_v1",
                "families": boundary_context.get("families", []),
            }
            legacy_projection_shards = {
                "format": "schema_projection_shards_v1",
                "shard_count": len(boundary_context.get("planner_slices", []) or []),
                "shards": boundary_context.get("planner_slices", []),
            }
        artifacts = {
            "boundary_context.json": boundary_context,
            "boundary_plan.json": boundary_plan or {},
            "projection_plan.json": boundary_plan or schema_projection,
            "projection_units.json": (
                legacy_projection_units
                if isinstance(boundary_plan, dict)
                else schema_projection.get("projection_units", {})
            ),
            "boundary_families.json": (
                legacy_boundary_families
                if isinstance(boundary_plan, dict)
                else schema_projection.get("boundary_families", {})
            ),
            "projection_shards.json": (
                legacy_projection_shards
                if isinstance(boundary_plan, dict)
                else schema_projection.get("projection_shards", {})
            ),
            "planner_outputs.json": (
                boundary_plan.get("planner_raw_outputs", [])
                if isinstance(boundary_plan, dict)
                else schema_projection.get("planner_raw_outputs", [])
            ),
            "projection_validation.json": (
                boundary_plan.get("validation", {})
                if isinstance(boundary_plan, dict)
                else schema_projection.get("projection_validation", {})
            ),
        }
        for filename, payload in artifacts.items():
            path = debug_dir / filename
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save {filename}: {e}")
        formal_artifacts = {
            "task_state_contract/task_state_contract.json": schema_projection.get("task_state_contract", {}),
            "interaction_contract/interaction_contract.json": schema_projection.get("interaction_contract", {}),
            "task_state_contract/success_evidence_hints.json": schema_projection.get("success_evidence_hints", {}),
            "schema_registry/schema_candidate_projection.json": {
                "format": "schema_candidate_projection",
                "schema_revision": 1,
                "decisions": schema_projection.get("decisions", []),
            },
        }
        legacy_formal_artifacts = {
            "task_state_contract.json": schema_projection.get("task_state_contract", {}),
            "interaction_contract.json": schema_projection.get("interaction_contract", {}),
            "success_evidence_hints.json": schema_projection.get("success_evidence_hints", {}),
            "schema_candidate_projection.json": formal_artifacts["schema_registry/schema_candidate_projection.json"],
        }
        for rel_path, payload in formal_artifacts.items():
            try:
                pom.write_json(
                    "consolidation",
                    "formal",
                    rel_path,
                    payload,
                    artifact_id=f"consolidation.{rel_path.replace('/', '.').removesuffix('.json')}",
                    artifact_kind="det_projection",
                    producer="site_model.builder.consolidation._save_schema_projection_artifacts",
                )
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save formal {rel_path}: {e}")
        for filename, payload in legacy_formal_artifacts.items():
            try:
                pom.write_json(
                    "consolidation",
                    "formal",
                    f"projections/schema/{filename}",
                    payload,
                    artifact_id=f"projections.schema.{filename.removesuffix('.json')}",
                    artifact_kind="det_projection",
                    producer="site_model.builder.consolidation._save_schema_projection_artifacts",
                    register=False,
                )
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save legacy formal mirror {filename}: {e}")
        reuse_decisions = {
            "task_state_contract": {
                "artifact_id": "consolidation.task_state_contract.task_state_contract",
                "artifact_path": "phase_consolidation/task_state_contract/task_state_contract.json",
            },
            "interaction_contract": {
                "artifact_id": "consolidation.interaction_contract.interaction_contract",
                "artifact_path": "phase_consolidation/interaction_contract/interaction_contract.json",
            },
            "schema_registry": {
                "artifact_id": "consolidation.schema_registry.schema_candidate_projection",
                "artifact_path": "phase_consolidation/schema_registry/schema_candidate_projection.json",
            },
        }
        for subphase, decision in reuse_decisions.items():
            payload = {
                "format": "artifact_reuse_decision",
                "schema_revision": 1,
                "artifact_id": decision["artifact_id"],
                "artifact_path": decision["artifact_path"],
                "reused": False,
                "reason": "recomputed",
                "input_artifacts": ["schema_projection"],
                "validators": [],
                "blocking_issue_count": 0,
                "blocking_issues": [],
            }
            try:
                pom.write_json(
                    "consolidation",
                    "debug",
                    f"{subphase}/reuse_decision.json",
                    payload,
                    artifact_id=f"consolidation.{subphase}.reuse_decision",
                    artifact_kind="debug_export",
                    producer="site_model.builder.consolidation._save_schema_projection_artifacts",
                )
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Schema] Failed to save {subphase} reuse decision: {e}")

    def _save_schema_registry_artifact(self, schema_registry: Dict[str, Any], output_dir: str) -> None:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        try:
            pom.write_json(
                "consolidation",
                "formal",
                "schema_registry/schema_registry.json",
                schema_registry,
                artifact_id="consolidation.schema_registry.schema_registry",
                artifact_kind="det_registry",
                producer="site_model.builder.consolidation._build_l0_schema_registry",
            )
            pom.write_json(
                "consolidation",
                "formal",
                "projections/schema/schema_registry.json",
                schema_registry,
                artifact_id="projections.schema.schema_registry",
                artifact_kind="det_registry",
                producer="site_model.builder.consolidation._build_l0_schema_registry",
                register=False,
            )
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[SchemaRegistry] Failed to save formal schema_registry.json: {e}")

    @staticmethod
    def _schema_registry_shape_valid(schema_registry: Any) -> bool:
        if not isinstance(schema_registry, dict):
            return False
        if str(schema_registry.get("format") or "") != "l0_schema_registry":
            return False
        return all(isinstance(schema_registry.get(key), dict) for key in ("catalog_entities", "runtime_states", "refs"))

    def _save_schema_registry_reuse_decision(self, decision: Dict[str, Any], output_dir: str) -> None:
        if not output_dir:
            return
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        payload = dict(decision)
        payload.setdefault("format", "schema_registry_reuse_decision")
        payload.setdefault("schema_revision", 1)
        payload.setdefault("artifact_id", "consolidation.schema.schema_registry")
        try:
            pom.write_json(
                "consolidation",
                "debug",
                "schema_registry/reuse_decision.json",
                payload,
                artifact_id="consolidation.schema_registry.reuse_decision",
                artifact_kind="debug_export",
                producer="site_model.builder.consolidation._materialize_l0_schema_registry",
            )
            pom.write_json(
                "consolidation",
                "debug",
                "schema_registry_reuse_decision.json",
                payload,
                artifact_id="debug.schema_registry_reuse_decision",
                artifact_kind="debug_export",
                producer="site_model.builder.consolidation._materialize_l0_schema_registry",
            )
            pom.write_json(
                "consolidation",
                "debug",
                "reuse/schema_registry.json",
                payload,
                artifact_id="debug.reuse.schema_registry",
                artifact_kind="debug_export",
                producer="site_model.builder.consolidation._materialize_l0_schema_registry",
            )
        except Exception:
            pass

    def _load_reusable_schema_registry(
        self,
        content: "SiteContent",
        output_dir: str,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        candidate_paths = [
            pom.formal("consolidation", "schema_registry", "schema_registry.json"),
            pom.formal("consolidation", "projections", "schema", "schema_registry.json"),
            pom.site_model_dir() / "schema_registry.json",
        ]
        decision: Dict[str, Any] = {
            "format": "schema_registry_reuse_decision",
            "schema_revision": 1,
            "artifact_id": "consolidation.schema.schema_registry",
            "decision": "missing",
            "reused": False,
            "reason": "missing_candidate_artifacts",
            "would_recompute": True,
            "candidate_paths": [],
            "formal_paths": [
                os.path.relpath(str(path), output_dir)
                for path in candidate_paths
            ],
            "source_path": "",
            "manual_invalidation_policy": "delete_or_deprecate_canonical_artifact_to_force_recompute",
        }
        for path in candidate_paths:
            item = {
                "path": os.path.relpath(str(path), output_dir),
                "exists": os.path.exists(path),
                "shape_valid": False,
            }
            if not os.path.exists(path):
                decision["candidate_paths"].append(item)
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                item["shape_valid"] = self._schema_registry_shape_valid(payload)
                decision["candidate_paths"].append(item)
                if item["shape_valid"]:
                    decision.update({
                        "decision": "reuse",
                        "reused": True,
                        "reason": "canonical_artifact_present",
                        "would_recompute": False,
                        "source_path": item["path"],
                        "catalog_entity_count": len(payload.get("catalog_entities", {})),
                        "runtime_state_count": len(payload.get("runtime_states", {})),
                    })
                    canonical_path = pom.formal("consolidation", "schema_registry", "schema_registry.json")
                    if os.path.abspath(str(path)) != os.path.abspath(str(canonical_path)):
                        self._save_schema_registry_artifact(payload, output_dir)
                    return payload, decision
            except Exception as e:
                item["error"] = str(e)
                decision["candidate_paths"].append(item)
        in_memory = content.schema_registry if isinstance(content.schema_registry, dict) else {}
        if self._schema_registry_shape_valid(in_memory):
            decision.update({
                "decision": "reuse",
                "reused": True,
                "reason": "content_schema_registry_present",
                "would_recompute": False,
                "source_path": "content.schema_registry",
                "catalog_entity_count": len(in_memory.get("catalog_entities", {})),
                "runtime_state_count": len(in_memory.get("runtime_states", {})),
            })
            return dict(in_memory), decision
        if in_memory:
            decision["reason"] = "invalid_in_memory_schema_registry_shape"
        return None, decision

    def _materialize_l0_schema_registry(
        self,
        content: "SiteContent",
        output_dir: str = "",
        *,
        force_recompute: bool = False,
    ) -> bool:
        if not self._l0_schema_registry_flow_enabled():
            return False
        registry: Optional[Dict[str, Any]] = None
        decision: Dict[str, Any] = {
            "format": "schema_registry_reuse_decision",
            "schema_revision": 1,
            "artifact_id": "consolidation.schema.schema_registry",
            "decision": "recompute" if force_recompute else "missing",
            "reused": False,
            "reason": "forced_recompute" if force_recompute else "output_dir_unavailable",
            "would_recompute": True,
            "source_path": "",
        }
        if output_dir and not force_recompute:
            registry, decision = self._load_reusable_schema_registry(content, output_dir)
        if registry is not None:
            content.schema_registry = registry
            if output_dir and decision.get("source_path") == "content.schema_registry":
                self._save_schema_registry_artifact(content.schema_registry, output_dir)
            if output_dir:
                self._save_schema_registry_reuse_decision(decision, output_dir)
            logger = getattr(self, "logger", None)
            if logger:
                logger.log_info(
                    "[SchemaRegistry] Reused L0 schema registry: "
                    f"{content.schema_registry.get('catalog_entity_count', 0)} catalog entities, "
                    f"{content.schema_registry.get('runtime_state_count', 0)} runtime states"
                )
            return True

        content.schema_registry = self._build_l0_schema_registry(content)
        if output_dir:
            self._save_schema_registry_artifact(content.schema_registry, output_dir)
            decision.update({
                "decision": "recompute",
                "reused": False,
                "would_recompute": True,
                "reason": decision.get("reason") or "no_reusable_schema_registry",
                "catalog_entity_count": content.schema_registry.get("catalog_entity_count", 0),
                "runtime_state_count": content.schema_registry.get("runtime_state_count", 0),
            })
            self._save_schema_registry_reuse_decision(decision, output_dir)
        logger = getattr(self, "logger", None)
        if logger:
            logger.log_info(
                "[SchemaRegistry] Materialized L0 schema registry: "
                f"{content.schema_registry.get('catalog_entity_count', 0)} catalog entities, "
                f"{content.schema_registry.get('runtime_state_count', 0)} runtime states"
            )
        return False

    def _save_schema_evaluation_report(self, content: "SiteContent", output_dir: str) -> None:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        reports_dir = pom.ensure_dir("consolidation", "debug", "reports")
        decisions = content.schema_candidate_projection if isinstance(content.schema_candidate_projection, list) else []
        task_related = [
            decision for decision in decisions
            if isinstance(decision, dict) and decision.get("task_related")
        ]
        report = {
            "format": "schema_evaluation_v1",
            "schema_count": len(content.entity_schemas or []),
            "schemas": [
                schema.get("name", "")
                for schema in content.entity_schemas or []
                if isinstance(schema, dict) and schema.get("name")
            ],
            "content_sample_entity_count": len(content.content_samples or {}),
            "content_sample_entities": sorted((content.content_samples or {}).keys()),
            "schema_candidate_count": len(decisions),
            "schema_candidate_projection_est_tokens": max(
                1,
                len(json.dumps(decisions, ensure_ascii=False)) // 4,
            ) if decisions else 0,
            "task_related_candidate_count": len(task_related),
            "task_related_candidates": [
                decision.get("candidate", "")
                for decision in task_related
                if decision.get("candidate")
            ],
            "projection_destination_counts": dict(Counter(
                decision.get("destination", "")
                for decision in decisions
                if isinstance(decision, dict)
            )),
            "taskish_coverage": {
                "task_state_count": len((content.task_state_contract or {}).get("states", [])),
                "interaction_action_count": len((content.interaction_contract or {}).get("actions", [])),
                "success_hint_count": len((content.success_evidence_hints or {}).get("hints", [])),
            },
        }
        path = reports_dir / "schema_eval.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            legacy_path = reports_dir / "schema_context_v2_evaluation.json"
            if os.path.exists(legacy_path):
                os.remove(legacy_path)
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[Schema] Failed to save schema evaluation report {path}: {e}")

    def _build_schema_candidate_ir(
        self,
        cluster_content_obs: Dict[str, Dict[str, Any]],
        page_info_map: Dict[str, Dict],
    ) -> Dict[str, Any]:
        grouped: Dict[str, Dict[str, Any]] = {}

        def _append_unique(values: List[Any], value: Any, limit: int = 12) -> None:
            if value is None or value == "":
                return
            if value not in values:
                values.append(value)
            if len(values) > limit:
                del values[limit:]

        def _merge_candidate_source(
            cluster_key: str,
            page_meta: Dict[str, Any],
            entity_name: str,
            items: List[Dict[str, Any]],
            relevance: Dict[str, Any],
            instance_ref: Optional[Dict[str, Any]] = None,
        ) -> None:
            if entity_name == "_metadata" or not isinstance(items, list):
                return
            rel = relevance.get(entity_name, {}) if isinstance(relevance.get(entity_name, {}), dict) else {}
            role = str(rel.get("role", "") or "catalog_data").strip()
            task_related = bool(rel.get("task_related", False))
            norm = self._normalize_entity_name(str(entity_name))
            candidate = grouped.setdefault(norm, {
                "entity_name": str(entity_name),
                "role": role,
                "task_related": task_related,
                "support": {
                    "trace_count": 0,
                    "trace_ids": [],
                    "step_refs": [],
                    "page_clusters": [],
                    "page_names": [],
                    "instance_refs": [],
                },
                "field_candidates": {},
                "sample_items": [],
            })
            if task_related:
                candidate["task_related"] = True
            if role and candidate.get("role") in ("", "catalog_data") and role != "catalog_data":
                candidate["role"] = role

            support = candidate["support"]
            support.setdefault("instance_refs", [])
            _append_unique(support["page_clusters"], cluster_key, 20)
            _append_unique(support["page_names"], page_meta.get("page_name", ""), 20)

            instance_trace_ids: List[str] = []
            instance_step_refs: List[str] = []
            for ref in rel.get("evidence_steps", []) if isinstance(rel.get("evidence_steps", []), list) else []:
                ref_text = str(ref).strip()
                _append_unique(support["step_refs"], ref_text, 24)
                if ref_text:
                    _append_unique(instance_step_refs, ref_text, 24)
                if ":" in ref_text:
                    trace_id = ref_text.split(":", 1)[0]
                    _append_unique(support["trace_ids"], trace_id, 24)
                    _append_unique(instance_trace_ids, trace_id, 24)
            for trace_id in rel.get("task_instruction_refs", []) if isinstance(rel.get("task_instruction_refs", []), list) else []:
                trace_text = str(trace_id).strip()
                _append_unique(support["trace_ids"], trace_text, 24)
                _append_unique(instance_trace_ids, trace_text, 24)

            instance_field_candidates: Dict[str, List[Any]] = {}
            sample_items_for_instance: List[Dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if len(candidate["sample_items"]) < 3:
                    candidate["sample_items"].append(dict(item))
                if len(sample_items_for_instance) < 2:
                    sample_items_for_instance.append(dict(item))
                for field_name, value in item.items():
                    if field_name.startswith("_"):
                        continue
                    field_values = candidate["field_candidates"].setdefault(field_name, [])
                    _append_unique(field_values, value, 8)
                    inst_field_values = instance_field_candidates.setdefault(field_name, [])
                    _append_unique(inst_field_values, value, 6)

            if instance_ref is not None:
                obs_keys = [
                    str(key).strip()
                    for key in instance_ref.get("obs_keys", [])
                    if str(key).strip()
                ]
                for obs_key in obs_keys:
                    _append_unique(instance_step_refs, obs_key, 24)
                    if ":" in obs_key:
                        _append_unique(instance_trace_ids, obs_key.split(":", 1)[0], 24)
                support["instance_refs"].append({
                    "cluster_id": cluster_key,
                    "page_name": page_meta.get("page_name", ""),
                    "instance_key": str(instance_ref.get("instance_key", "")).strip(),
                    "instance_label": str(instance_ref.get("instance_label", "")).strip(),
                    "obs_keys": obs_keys[:24],
                    "trace_ids": instance_trace_ids[:24],
                    "step_refs": instance_step_refs[:24],
                    "field_candidates": instance_field_candidates,
                    "sample_items": sample_items_for_instance,
                })
                if len(support["instance_refs"]) > 32:
                    del support["instance_refs"][32:]

        for cluster_key, content_obs in cluster_content_obs.items():
            if not isinstance(content_obs, dict):
                continue
            page_meta = page_info_map.get(cluster_key, {}) if isinstance(page_info_map, dict) else {}
            instance_candidates = (
                content_obs.get("instance_content_candidates", [])
                if isinstance(content_obs.get("instance_content_candidates", []), list)
                else []
            )
            if instance_candidates:
                for instance_candidate in instance_candidates:
                    if not isinstance(instance_candidate, dict):
                        continue
                    instance_content = instance_candidate.get("content_data", {})
                    if not isinstance(instance_content, dict):
                        continue
                    extracted_items = instance_content.get("extracted_items", {})
                    relevance = instance_content.get("entity_task_relevance", {})
                    if not isinstance(extracted_items, dict):
                        continue
                    if not isinstance(relevance, dict):
                        relevance = {}
                    instance_obs_keys = instance_candidate.get("obs_keys", [])
                    instance_source_step = str(instance_obs_keys[0]).strip() if instance_obs_keys else ""
                    for entity_name, items in extracted_items.items():
                        if instance_source_step and isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict) and not item.get("source_step_key"):
                                    item["source_step_key"] = instance_source_step
                        _merge_candidate_source(
                            cluster_key,
                            page_meta,
                            entity_name,
                            items,
                            relevance,
                            instance_ref=instance_candidate,
                        )
                continue

            extracted_items = content_obs.get("extracted_items", {})
            relevance = content_obs.get("entity_task_relevance", {})
            if not isinstance(extracted_items, dict):
                continue
            if not isinstance(relevance, dict):
                relevance = {}
            for entity_name, items in extracted_items.items():
                _merge_candidate_source(
                    cluster_key,
                    page_meta,
                    entity_name,
                    items,
                    relevance,
                )

        for candidate in grouped.values():
            trace_ids = candidate["support"].get("trace_ids", [])
            candidate["support"]["trace_count"] = len(trace_ids)
        candidates = sorted(
            grouped.values(),
            key=lambda item: (not item.get("task_related", False), item.get("entity_name", "")),
        )
        return {
            "format": "schema_candidate_ir_v1",
            "entity_candidates": candidates,
        }

    def _ensure_task_related_candidate_schemas(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        schema_candidate_ir: Dict[str, Any],
        rejected_candidates: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        schemas = list(entity_schemas or [])
        relationships = list(entity_relationships or [])
        existing = {
            self._normalize_entity_name(str(schema.get("name", "")))
            for schema in schemas
            if isinstance(schema, dict) and schema.get("name")
        }
        rejected = {
            self._normalize_entity_name(str(item.get("entity_name", "")))
            for item in rejected_candidates
            if isinstance(item, dict) and item.get("entity_name")
        }
        for candidate in schema_candidate_ir.get("entity_candidates", []) if isinstance(schema_candidate_ir, dict) else []:
            if not isinstance(candidate, dict) or not candidate.get("task_related"):
                continue
            entity_name = str(candidate.get("entity_name", "")).strip()
            norm = self._normalize_entity_name(entity_name)
            if not entity_name or norm in existing or norm in rejected:
                continue
            schema = self._candidate_to_seed_schema(
                candidate,
                inferred_from="schema_candidate_ir",
            )
            if not schema:
                continue
            schemas.append(schema)
            existing.add(norm)
            if self.logger:
                self.logger.log_info(f"[Schema] Promoted task-related schema candidate '{entity_name}'")
        return schemas, relationships

    def _candidate_to_seed_schema(
        self,
        candidate: Dict[str, Any],
        inferred_from: str = "schema_candidate_ir_seed",
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(candidate, dict):
            return None
        entity_name = str(candidate.get("entity_name", "")).strip()
        role = str(candidate.get("role", "") or "catalog_data").strip()
        if not entity_name or self._schema_candidate_contract_destination(entity_name, role, candidate) != "entity_schema":
            return None
        fields = [{"name": "id", "type": "string", "observed_examples": []}]
        field_candidates = candidate.get("field_candidates", {})
        if isinstance(field_candidates, dict):
            for field_name, values in field_candidates.items():
                if not field_name or str(field_name).startswith("_") or str(field_name) == "id":
                    continue
                field_type = self._infer_schema_candidate_field_type(values)
                observed_examples = [
                    str(value)
                    for value in (values if isinstance(values, list) else [values])
                    if value is not None and value != ""
                ][:3]
                fields.append({
                    "name": str(field_name),
                    "type": field_type,
                    "observed_examples": observed_examples,
                })
                if len(fields) >= 20:
                    break
        if len(fields) < 2:
            fields.append({
                "name": "display_text",
                "type": "string",
                "observed_examples": [],
            })
        return {
            "name": entity_name,
            "storage_key": _camel_to_snake(entity_name) + "s",
            "fields": fields,
            "data_pre_generation_num": "none" if role != "catalog_data" else "few",
            "schema_role": role or "task_state",
            "inferred_from": inferred_from,
        }

    def _build_candidate_seed_entity_schemas(
        self,
        schema_candidate_ir: Dict[str, Any],
        rejected_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        schemas: List[Dict[str, Any]] = []
        relationships: List[Dict[str, Any]] = []
        existing = set()
        rejected = {
            self._normalize_entity_name(str(item.get("entity_name", "") or item.get("name", "")))
            for item in (rejected_candidates or [])
            if isinstance(item, dict) and (item.get("entity_name") or item.get("name"))
        }
        for candidate in schema_candidate_ir.get("entity_candidates", []) if isinstance(schema_candidate_ir, dict) else []:
            schema = self._candidate_to_seed_schema(
                candidate,
                inferred_from="boundary_planner_seed",
            )
            if not schema:
                continue
            norm = self._normalize_entity_name(str(schema.get("name", "")).strip())
            if not norm or norm in existing or norm in rejected:
                continue
            existing.add(norm)
            schemas.append(schema)
        return schemas, relationships

    def _merge_schema_field_lists(
        self,
        fields_a: List[Dict[str, Any]],
        fields_b: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for field in list(fields_a or []) + list(fields_b or []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            name = str(field.get("name", "")).strip()
            if not name:
                continue
            if name not in merged:
                merged[name] = dict(field)
                merged[name]["observed_examples"] = list(field.get("observed_examples", []) or [])[:10]
                continue
            current = merged[name]
            if current.get("type") in {"", "string"} and field.get("type") not in {"", "string", None}:
                current["type"] = field.get("type")
            if not current.get("description") and field.get("description"):
                current["description"] = field.get("description")
            examples = list(current.get("observed_examples", []) or [])
            for value in field.get("observed_examples", []) or []:
                if value not in examples:
                    examples.append(value)
                if len(examples) >= 10:
                    break
            current["observed_examples"] = examples
        return list(merged.values())

    def _dedupe_entity_schemas_by_name(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        merged_by_norm: Dict[str, Dict[str, Any]] = {}
        rename_map: Dict[str, str] = {}
        for schema in entity_schemas or []:
            if not isinstance(schema, dict) or not schema.get("name"):
                continue
            name = str(schema.get("name", "")).strip()
            norm = self._normalize_entity_name(name)
            if norm not in merged_by_norm:
                merged_by_norm[norm] = dict(schema)
                merged_by_norm[norm]["fields"] = self._merge_schema_field_lists(schema.get("fields", []), [])
                continue
            kept = merged_by_norm[norm]
            rename_map[name] = str(kept.get("name", "")).strip() or name
            kept["fields"] = self._merge_schema_field_lists(
                kept.get("fields", []),
                schema.get("fields", []),
            )
            if kept.get("data_pre_generation_num") == "none" and schema.get("data_pre_generation_num") == "few":
                kept["data_pre_generation_num"] = "few"
        deduped_schemas = list(merged_by_norm.values())
        if not rename_map:
            return deduped_schemas, list(entity_relationships or [])
        deduped_relationships = []
        for rel in entity_relationships or []:
            if not isinstance(rel, dict):
                continue
            rel = dict(rel)
            for key in ("from", "to", "source", "target", "entity", "parent", "child"):
                value = rel.get(key)
                if value in rename_map:
                    rel[key] = rename_map[value]
            deduped_relationships.append(rel)
        return deduped_schemas, deduped_relationships

    _TASK_STATE_CANDIDATE_ROLES = {
        "selectable_task_state",
        "runtime_state",
        "form_value",
        "filter_condition",
    }
    _INTERACTION_CANDIDATE_ROLES = {
        "rule_or_policy",
        "interaction_only",
        "ui_action",
    }
    _DECORATIVE_CANDIDATE_ROLES = {
        "decorative",
        "layout_only",
        "chrome",
    }
    _INTERACTION_NAME_RE = re.compile(
        r"(?:button|btn|tab|navigation|nav|bar|action|sheet|modal|drawer|entry|dialog|confirmation)$",
        re.IGNORECASE,
    )
    @staticmethod
    def _candidate_has_record_lifecycle(candidate: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(candidate, dict):
            return False
        support = candidate.get("support", {}) if isinstance(candidate.get("support", {}), dict) else {}
        page_text = " ".join(
            str(value)
            for group in (
                support.get("page_clusters", []),
                support.get("page_names", []),
            )
            for value in (group if isinstance(group, list) else [])
        ).lower()
        field_names = {
            str(name).strip().lower()
            for name in (candidate.get("field_candidates", {}) or {}).keys()
            if str(name).strip()
        } if isinstance(candidate.get("field_candidates", {}), dict) else set()
        sample_text = json.dumps(candidate.get("sample_items", [])[:3], ensure_ascii=False).lower() if isinstance(candidate.get("sample_items", []), list) else ""
        combined_text = " ".join([page_text, sample_text])

        list_like = any(token in combined_text for token in ("list", "listing", "列表", "管理"))
        detail_like = any(token in combined_text for token in ("detail", "详情"))
        form_like = any(token in combined_text for token in ("form", "edit", "编辑", "新增", "create"))
        record_field_count = len(field_names - {"selected", "active", "enabled", "disabled", "status", "state"})
        has_identity_field = bool(field_names & {"name", "title", "title_name", "company_name", "tax_id", "taxpayer_id", "id"})

        return (
            (list_like and (detail_like or form_like) and record_field_count >= 2)
            or ((detail_like and form_like) and has_identity_field)
        )

    def _schema_candidate_contract_destination(
        self,
        entity_name: str,
        role: str,
        candidate: Optional[Dict[str, Any]] = None,
    ) -> str:
        role_norm = str(role or "").strip().lower()
        name = str(entity_name or "").strip()
        if role_norm in self._DECORATIVE_CANDIDATE_ROLES:
            return "discarded_decorative"
        if role_norm in self._INTERACTION_CANDIDATE_ROLES or self._INTERACTION_NAME_RE.search(name):
            return "interaction_contract"
        if role_norm in self._TASK_STATE_CANDIDATE_ROLES:
            if self._candidate_has_record_lifecycle(candidate):
                return "entity_schema"
            return "task_state_contract"
        return "entity_schema"

    def _build_deterministic_boundary_pre_routing(
        self,
        schema_candidate_ir: Dict[str, Any],
        rejected_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        rejected_by_norm = {
            self._normalize_entity_name(str(item.get("entity_name", "") or item.get("name", ""))): item
            for item in (rejected_candidates or [])
            if isinstance(item, dict) and (item.get("entity_name") or item.get("name"))
        }
        decisions: List[Dict[str, Any]] = []
        for candidate in schema_candidate_ir.get("entity_candidates", []) if isinstance(schema_candidate_ir, dict) else []:
            if not isinstance(candidate, dict):
                continue
            entity_name = str(candidate.get("entity_name", "")).strip()
            if not entity_name:
                continue
            norm = self._normalize_entity_name(entity_name)
            rejected = rejected_by_norm.get(norm)
            support = candidate.get("support", {}) if isinstance(candidate.get("support", {}), dict) else {}
            evidence_steps = [
                str(item).strip()
                for item in support.get("step_refs", [])
                if str(item).strip()
            ]
            page_clusters = [
                str(item).strip()
                for item in support.get("page_clusters", [])
                if str(item).strip()
            ]
            page_names = [
                str(item).strip()
                for item in support.get("page_names", [])
                if str(item).strip()
            ]
            if isinstance(rejected, dict):
                decisions.append({
                    "candidate": entity_name,
                    "destination": "rejected_with_reason",
                    "projected_to": "",
                    "reason": str(rejected.get("reason", "") or rejected.get("rejected_reason", "") or "Rejected before boundary planning.").strip(),
                    "planner_shard": "deterministic_boundary_filter",
                    "role": str(candidate.get("role", "") or "catalog_data").strip(),
                    "task_related": bool(candidate.get("task_related", False)),
                    "evidence_steps": evidence_steps,
                    "page_clusters": page_clusters,
                    "page_names": page_names,
                })
                continue
            role = str(candidate.get("role", "") or "catalog_data").strip()
            destination = self._schema_candidate_contract_destination(entity_name, role, candidate)
            if destination == "entity_schema":
                continue
            projected_to = ""
            reason = ""
            if destination == "task_state_contract":
                projected_to = _camel_to_snake(entity_name)
                reason = "Deterministic boundary filter routed runtime/form/selectable state out of entity boundary planning."
            elif destination == "interaction_contract":
                projected_to = _camel_to_snake(entity_name)
                reason = "Deterministic boundary filter routed UI action/policy candidate out of entity boundary planning."
            elif destination == "discarded_decorative":
                reason = "Deterministic boundary filter discarded decorative/layout-only candidate before boundary planning."
            decisions.append({
                "candidate": entity_name,
                "destination": destination,
                "projected_to": projected_to,
                "reason": reason,
                "planner_shard": "deterministic_boundary_filter",
                "role": role,
                "task_related": bool(candidate.get("task_related", False)),
                "evidence_steps": evidence_steps,
                "page_clusters": page_clusters,
                "page_names": page_names,
            })
        return decisions

    def _estimate_schema_boundary_slice_tokens(self, planner_slice: Dict[str, Any]) -> int:
        prompt = self._build_schema_boundary_planner_prompt(planner_slice)
        try:
            import tiktoken

            return max(1, len(tiktoken.get_encoding("o200k_base").encode(prompt)))
        except Exception:
            return max(1, len(prompt) // 4)

    @staticmethod
    def _schema_candidate_values(field_candidates: Dict[str, Any]) -> List[str]:
        if not isinstance(field_candidates, dict):
            return []
        preferred = ("name", "text", "title", "value", "query", "label")
        ordered_fields = [
            field for field in preferred if field in field_candidates
        ] + [
            field for field in field_candidates if field not in preferred
        ]
        values: List[str] = []
        for field in ordered_fields:
            raw_values = field_candidates.get(field, [])
            seq = raw_values if isinstance(raw_values, list) else [raw_values]
            for value in seq:
                if value is None or value == "":
                    continue
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)
                if len(values) >= 24:
                    return values
        return values

    def _schema_candidate_key_examples(self, field_candidates: Dict[str, Any]) -> Dict[str, List[str]]:
        if not isinstance(field_candidates, dict):
            return {}
        preferred = ("name", "title", "label", "text", "value", "query", "keyword", "price", "amount")
        ordered = [field for field in preferred if field in field_candidates]
        ordered.extend(field for field in field_candidates.keys() if field not in ordered)
        examples: Dict[str, List[str]] = {}
        for field in ordered[:8]:
            values = field_candidates.get(field, [])
            seq = values if isinstance(values, list) else [values]
            collected: List[str] = []
            for value in seq:
                if value is None or value == "":
                    continue
                text = str(value).strip()
                if text and text not in collected:
                    collected.append(text)
                if len(collected) >= 3:
                    break
            if collected:
                examples[str(field)] = collected
        return examples

    def _schema_projection_name_tokens(self, name: str) -> List[str]:
        raw = str(name or "").replace("_", " ").replace("-", " ")
        pieces = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+", raw)
        if not pieces:
            pieces = [part for part in re.split(r"[^A-Za-z0-9]+", raw) if part]
        return [piece.lower() for piece in pieces if piece]

    def _schema_projection_lexical_family(self, name: str) -> str:
        tokens = set(self._schema_projection_name_tokens(name))
        families = [
            ("commerce_product", {"food", "menu", "product", "dish", "sku", "item"}),
            ("search", {"search", "query", "suggestion", "keyword", "history", "autocomplete"}),
            ("address", {"address", "shipping", "delivery", "location", "book", "entry"}),
            ("payment", {"payment", "pay", "cashier", "method", "session", "submission"}),
            ("order", {"order", "checkout", "cart", "invoice", "fulfillment"}),
            ("merchant", {"merchant", "store", "shop", "restaurant"}),
            ("ride", {"ride", "pickup", "dropoff", "car", "driver", "booking"}),
        ]
        matched = [family for family, roots in families if tokens & roots]
        if matched:
            return matched[0]
        meaningful = sorted(token for token in tokens if token not in {"entity", "data", "info", "state"})
        return meaningful[0] if meaningful else self._normalize_entity_name(name)

    def _schema_boundary_domain(self, name: str) -> str:
        tokens = set(self._schema_projection_name_tokens(name))
        domains = [
            ("store_like", {"merchant", "store", "shop", "restaurant", "venue", "seller"}),
            ("product_like", {"product", "item", "menu", "dish", "food", "medicine", "sku", "deal"}),
            ("offer_like", {"offer", "coupon", "discount", "deal", "promotion", "voucher"}),
            ("place_like", {"place", "address", "location", "poi", "destination", "pickup"}),
            ("ride_like", {"ride", "vehicle", "car", "driver", "service", "booking"}),
            ("review_like", {"review", "comment", "rating"}),
            ("policy_like", {"policy", "rule", "agreement", "terms"}),
        ]
        for domain, roots in domains:
            if tokens & roots:
                return domain
        return "other"

    def _schema_boundary_field_set(self, summary: Dict[str, Any]) -> set:
        weak_fields = {
            "id", "name", "title", "label", "display_text", "text", "image", "image_url",
            "url", "icon", "logo", "avatar",
        }
        aliases = {
            "eta": "delivery_eta",
            "delivery_time": "delivery_eta",
            "delivery_eta_text": "delivery_eta",
            "monthly_sales_text": "monthly_sales",
            "sales_text": "monthly_sales",
            "current_price": "price",
            "price_text": "price",
            "price_from": "price",
            "new_user_price": "price",
            "estimated_price": "price",
            "estimated_price_or_range": "price",
            "full_address": "address",
            "product_name": "name",
            "item_name": "name",
            "store_name": "name",
            "merchant_name": "merchant",
        }
        fields = set()
        for field in summary.get("field_names", []) or []:
            norm = _camel_to_snake(str(field).strip())
            norm = aliases.get(norm, norm)
            if norm and norm not in weak_fields:
                fields.add(norm)
        return fields

    def _schema_boundary_family_id(self, domain: str, names: List[str]) -> str:
        parts = [self._normalize_entity_name(name) for name in sorted(names)[:8]]
        return f"{domain}__{'__'.join(parts)}"

    def _build_schema_boundary_families(self, projection_units: Dict[str, Any]) -> Dict[str, Any]:
        candidate_index = projection_units.get("candidate_index", {}) if isinstance(projection_units, dict) else {}
        if not isinstance(candidate_index, dict):
            candidate_index = {}

        summaries = {
            str(name): summary
            for name, summary in candidate_index.items()
            if isinstance(summary, dict) and str(name).strip()
        }
        by_domain: Dict[str, List[str]] = {}
        for name in summaries:
            domain = self._schema_boundary_domain(name)
            if domain == "other":
                continue
            by_domain.setdefault(domain, []).append(name)

        families: List[Dict[str, Any]] = []
        for domain, domain_names in sorted(by_domain.items()):
            if len(domain_names) < 2:
                continue
            max_family_size = 24
            edges: List[Dict[str, Any]] = []
            adjacency: Dict[str, set] = {name: set() for name in domain_names}
            allow_lexical_recall_edges = len(domain_names) <= 12
            for i, source in enumerate(sorted(domain_names)):
                for target in sorted(domain_names)[i + 1:]:
                    source_summary = summaries[source]
                    target_summary = summaries[target]
                    pair_edges: List[Dict[str, Any]] = []


                    if allow_lexical_recall_edges:
                        pair_edges.append({
                            "source": source,
                            "target": target,
                            "reason": "lexical",
                            "score": 0.4,
                            "shared_fields": [],
                        })

                    source_fields = self._schema_boundary_field_set(source_summary)
                    target_fields = self._schema_boundary_field_set(target_summary)
                    shared_fields = sorted(source_fields & target_fields)
                    overlap = (
                        len(shared_fields) / max(min(len(source_fields), len(target_fields)), 1)
                        if source_fields and target_fields else 0.0
                    )
                    if overlap >= 0.45 or (overlap >= 0.30 and len(shared_fields) >= 3):
                        pair_edges.append({
                            "source": source,
                            "target": target,
                            "reason": "field_overlap",
                            "score": round(overlap, 3),
                            "shared_fields": shared_fields[:10],
                        })

                    shared_pages = sorted(
                        set(source_summary.get("page_clusters", []) or [])
                        & set(target_summary.get("page_clusters", []) or [])
                    )
                    if shared_pages:
                        pair_edges.append({
                            "source": source,
                            "target": target,
                            "reason": "page_role",
                            "score": 0.35,
                            "shared_fields": [],
                            "shared_pages": shared_pages[:6],
                        })

                    strong_edges = [
                        edge for edge in pair_edges
                        if edge.get("reason") in {"field_overlap", "page_role"}
                    ]
                    if pair_edges and (allow_lexical_recall_edges or strong_edges):
                        adjacency[source].add(target)
                        adjacency[target].add(source)
                        edges.extend(pair_edges)

            seen = set()
            for start in sorted(domain_names):
                if start in seen:
                    continue
                stack = [start]
                component: List[str] = []
                seen.add(start)
                while stack:
                    node = stack.pop()
                    component.append(node)
                    for nxt in sorted(adjacency.get(node, set())):
                        if nxt not in seen:
                            seen.add(nxt)
                            stack.append(nxt)
                if len(component) < 2:
                    continue
                component_groups = [component]
                if len(component) > max_family_size:
                    component_groups = []
                    by_shard: Dict[str, List[str]] = {}
                    for name in component:
                        shard = str(summaries[name].get("shard_hint", "") or "ambiguous_mixed")
                        by_shard.setdefault(shard, []).append(name)
                    for shard_names in by_shard.values():
                        if len(shard_names) <= max_family_size:
                            component_groups.append(shard_names)
                            continue
                        by_primary_page: Dict[str, List[str]] = {}
                        for name in shard_names:
                            pages = summaries[name].get("page_clusters", []) or []
                            primary_page = str(pages[0]) if pages else "no_page"
                            by_primary_page.setdefault(primary_page, []).append(name)
                        for page_names in by_primary_page.values():
                            sorted_page_names = sorted(page_names)
                            for idx in range(0, len(sorted_page_names), max_family_size):
                                component_groups.append(sorted_page_names[idx:idx + max_family_size])

                for group in component_groups:
                    if len(group) < 2:
                        continue
                    component_set = set(group)
                    component_edges = [
                        edge for edge in edges
                        if edge.get("source") in component_set and edge.get("target") in component_set
                    ]
                    if not component_edges:
                        continue
                    reasons = sorted({str(edge.get("reason", "")) for edge in component_edges if edge.get("reason")})
                    role_counts: Dict[str, int] = {}
                    shard_hints: Dict[str, int] = {}
                    for name in group:
                        role = str(summaries[name].get("role", "") or "unknown")
                        shard = str(summaries[name].get("shard_hint", "") or "ambiguous_mixed")
                        role_counts[role] = role_counts.get(role, 0) + 1
                        shard_hints[shard] = shard_hints.get(shard, 0) + 1
                    family_type = (
                        "canonical_boundary_candidate"
                        if set(shard_hints.keys()) <= {"catalog"} else "mixed_boundary_candidate"
                    )
                    requires_split_reason = any(
                        edge.get("reason") == "field_overlap" and float(edge.get("score", 0.0)) >= 0.45
                        for edge in component_edges
                    )
                    family_candidates = []
                    for name in sorted(group):
                        summary = summaries[name]
                        family_candidates.append({
                            "name": name,
                            "role": summary.get("role", ""),
                            "task_related_count": 1 if summary.get("task_related") else 0,
                            "page_clusters": (summary.get("page_clusters", []) or [])[:6],
                            "page_names": (summary.get("page_names", []) or [])[:6],
                            "field_names": (summary.get("field_names", []) or [])[:14],
                            "top_examples": {
                                key: values[:2]
                                for key, values in (summary.get("value_examples", {}) or {}).items()
                                if isinstance(values, list)
                            },
                        })
                    families.append({
                        "family_id": self._schema_boundary_family_id(domain, group),
                        "family_type": family_type,
                        "domain": domain,
                        "construction_reasons": [
                            f"{reason}: deterministic boundary-family signal"
                            for reason in reasons
                        ],
                        "candidate_names": sorted(group),
                        "role_counts": role_counts,
                        "shard_hints": shard_hints,
                        "requires_split_reason": requires_split_reason,
                        "candidates": family_candidates,
                        "edges": component_edges[:80],
                        "planner_question": (
                            "Decide whether these candidates represent one durable business boundary "
                            "or multiple boundaries. Assign every candidate exactly once."
                        ),
                    })

        return {
            "format": "schema_boundary_families_v1",
            "family_count": len(families),
            "families": families,
        }

    def _schema_projection_shard_hint(self, entity_name: str, role: str) -> str:
        role_norm = str(role or "").strip().lower()
        name_norm = self._normalize_entity_name(entity_name)
        if role_norm in {"decorative", "layout_only", "chrome"}:
            return "decorative"
        if role_norm in {"rule_or_policy", "interaction_only", "ui_action"}:
            return "policy_interaction"
        if role_norm in {"form_value", "filter_condition"}:
            return "search_form_filter"
        if role_norm in {"runtime_state", "selectable_task_state"}:
            if "search" in name_norm or "query" in name_norm or "filter" in name_norm:
                return "search_form_filter"
            return "runtime_task_state"
        if role_norm == "catalog_data":
            return "catalog"
        return "ambiguous_mixed"

    def _compact_schema_projection_candidate(
        self,
        candidate: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
    ) -> Dict[str, Any]:
        entity_name = str(candidate.get("entity_name", "")).strip()
        field_candidates = candidate.get("field_candidates", {})
        support = candidate.get("support", {}) if isinstance(candidate.get("support", {}), dict) else {}
        schema_names = {
            str(schema.get("name", "")).strip()
            for schema in entity_schemas or []
            if isinstance(schema, dict) and schema.get("name")
        }
        rejected_by_norm = {
            self._normalize_entity_name(str(item.get("entity_name", "") or item.get("name", ""))): item
            for item in rejected_candidates or []
            if isinstance(item, dict) and (item.get("entity_name") or item.get("name"))
        }
        norm = self._normalize_entity_name(entity_name)
        rejected = rejected_by_norm.get(norm)
        role = str(candidate.get("role", "") or "catalog_data").strip()
        instance_refs = support.get("instance_refs", []) if isinstance(support.get("instance_refs", []), list) else []
        return {
            "name": entity_name,
            "normalized_name": norm,
            "role": role,
            "task_related": bool(candidate.get("task_related", False)),
            "shard_hint": self._schema_projection_shard_hint(entity_name, role),
            "page_clusters": [
                str(item).strip()
                for item in support.get("page_clusters", [])
                if str(item).strip()
            ][:12],
            "page_names": [
                str(item).strip()
                for item in support.get("page_names", [])
                if str(item).strip()
            ][:12],
            "instance_ref_count": len(instance_refs),
            "field_names": sorted([
                str(field)
                for field in field_candidates.keys()
                if str(field).strip() and not str(field).startswith("_")
            ])[:24] if isinstance(field_candidates, dict) else [],
            "value_examples": self._schema_candidate_key_examples(field_candidates),
            "matched_existing_schema": (
                local_to_global_entity.get(entity_name)
                or next((name for name in schema_names if self._normalize_entity_name(name) == norm), "")
            ),
            "rejected_reason": (
                str(rejected.get("reason", "") or rejected.get("rejected_reason", "")).strip()
                if isinstance(rejected, dict) else ""
            ),
        }

    def _build_schema_projection_units(
        self,
        schema_candidate_ir: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
        allowed_candidate_names: Optional[set] = None,
    ) -> Dict[str, Any]:
        candidates = [
            candidate for candidate in schema_candidate_ir.get("entity_candidates", [])
            if isinstance(candidate, dict)
            and str(candidate.get("entity_name", "")).strip()
            and (
                allowed_candidate_names is None
                or str(candidate.get("entity_name", "")).strip() in allowed_candidate_names
            )
        ] if isinstance(schema_candidate_ir, dict) else []
        summaries = {
            str(candidate.get("entity_name", "")).strip(): self._compact_schema_projection_candidate(
                candidate,
                entity_schemas,
                rejected_candidates,
                local_to_global_entity,
            )
            for candidate in candidates
        }
        units: List[Dict[str, Any]] = []

        def _make_unit(unit_id: str, unit_type: str, names: List[str], decision_names: Optional[List[str]] = None) -> None:
            unique_names = [name for name in names if name in summaries]
            seen = set()
            unique_names = [name for name in unique_names if not (name in seen or seen.add(name))]
            if not unique_names:
                return
            role_counts: Dict[str, int] = {}
            task_related_count = 0
            page_clusters: List[str] = []
            page_names: List[str] = []
            field_union: List[str] = []
            shard_hints: List[str] = []
            for name in unique_names:
                summary = summaries[name]
                role_counts[summary["role"]] = role_counts.get(summary["role"], 0) + 1
                task_related_count += 1 if summary["task_related"] else 0
                if summary["shard_hint"] not in shard_hints:
                    shard_hints.append(summary["shard_hint"])
                for page in summary["page_clusters"]:
                    if page not in page_clusters:
                        page_clusters.append(page)
                for page_name in summary["page_names"]:
                    if page_name not in page_names:
                        page_names.append(page_name)
                for field_name in summary["field_names"]:
                    if field_name not in field_union:
                        field_union.append(field_name)
            conflicts: List[str] = []
            if len(role_counts) > 1:
                conflicts.append("mixed_roles:" + ",".join(sorted(role_counts.keys())))
            matched = {
                name: summaries[name].get("matched_existing_schema", "")
                for name in unique_names
                if summaries[name].get("matched_existing_schema")
            }
            unit_decisions = decision_names if decision_names is not None else unique_names
            units.append({
                "unit_id": unit_id,
                "unit_type": unit_type,
                "candidate_names": unique_names,
                "decision_candidates": [
                    name for name in unit_decisions
                    if name in summaries
                ],
                "shard_hint": shard_hints[0] if len(shard_hints) == 1 else "ambiguous_mixed",
                "role_counts": role_counts,
                "task_related_count": task_related_count,
                "page_clusters": page_clusters[:16],
                "page_names": page_names[:16],
                "field_name_union": field_union[:32],
                "matched_existing_schemas": matched,
                "obvious_conflicts": conflicts,
                "candidates": [summaries[name] for name in unique_names],
            })


        by_norm: Dict[str, List[str]] = {}
        for name, summary in summaries.items():
            by_norm.setdefault(summary["normalized_name"], []).append(name)
        for norm, names in sorted(by_norm.items()):
            _make_unit(f"name:{norm}", "name_normalized", names, decision_names=names)

        by_family: Dict[str, List[str]] = {}
        for name in summaries.keys():
            family = self._schema_projection_lexical_family(name)
            by_family.setdefault(family, []).append(name)
        for family, names in sorted(by_family.items()):
            if len(names) >= 2:
                _make_unit(f"lexical:{family}", "lexical_family", names, decision_names=[])

        by_page: Dict[str, List[str]] = {}
        for name, summary in summaries.items():
            for page in summary.get("page_clusters", []):
                by_page.setdefault(page, []).append(name)
        for page, names in sorted(by_page.items()):
            if len(names) < 2:
                continue
            role_groups = {
                summaries[name]["shard_hint"]
                for name in names
                if name in summaries
            }
            unit_names = sorted(names)[:24]
            _make_unit(
                f"page:{page}:mixed" if len(role_groups) > 1 else f"page:{page}:{next(iter(role_groups))}",
                "page_role_neighborhood",
                unit_names,
                decision_names=[],
            )

        return {
            "format": "schema_projection_units_v1",
            "candidate_count": len(summaries),
            "unit_count": len(units),
            "candidate_index": summaries,
            "units": units,
            "boundary_families": self._build_schema_boundary_families({
                "candidate_index": summaries,
                "units": units,
            }),
        }

    def _build_schema_boundary_context(
        self,
        schema_candidate_ir: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
        allowed_candidate_names: Optional[set] = None,
    ) -> Dict[str, Any]:
        projection_units = self._build_schema_projection_units(
            schema_candidate_ir,
            entity_schemas,
            rejected_candidates,
            local_to_global_entity,
            allowed_candidate_names=allowed_candidate_names,
        )
        projection_shards = self._build_schema_projection_shards(projection_units)
        boundary_families = projection_units.get("boundary_families", {}) if isinstance(projection_units, dict) else {}
        families = boundary_families.get("families", []) if isinstance(boundary_families, dict) else []
        planner_slices = projection_shards.get("shards", []) if isinstance(projection_shards, dict) else []
        return {
            "format": "schema_boundary_context_v1",
            "candidate_count": projection_units.get("candidate_count", 0),
            "candidate_index": projection_units.get("candidate_index", {}),
            "units": projection_units.get("units", []),
            "families": families,
            "planner_slices": planner_slices,
        }

    def _build_schema_inference_candidate_context(
        self,
        schema_candidate_ir: Dict[str, Any],
    ) -> Dict[str, Any]:
        projection_units = self._build_schema_projection_units(
            schema_candidate_ir,
            entity_schemas=[],
            rejected_candidates=[],
            local_to_global_entity={},
        )
        compact_units = []
        for unit in projection_units.get("units", []):
            if not isinstance(unit, dict):
                continue
            compact_units.append({
                "unit_id": unit.get("unit_id", ""),
                "unit_type": unit.get("unit_type", ""),
                "candidate_names": unit.get("candidate_names", []),
                "role_counts": unit.get("role_counts", {}),
                "task_related_count": unit.get("task_related_count", 0),
                "page_clusters": unit.get("page_clusters", [])[:6],
                "field_name_union": unit.get("field_name_union", [])[:12],
                "obvious_conflicts": unit.get("obvious_conflicts", []),
            })
        candidate_summaries = []
        for candidate in projection_units.get("candidate_index", {}).values():
            if not isinstance(candidate, dict):
                continue
            value_examples = {}
            for key in ("name", "title", "label", "query", "keyword", "price", "amount"):
                if key in candidate.get("value_examples", {}):
                    value_examples[key] = candidate["value_examples"][key][:2]
                if len(value_examples) >= 3:
                    break
            candidate_summaries.append({
                "name": candidate.get("name", ""),
                "role": candidate.get("role", ""),
                "task_related": candidate.get("task_related", False),
                "shard_hint": candidate.get("shard_hint", ""),
                "page_clusters": candidate.get("page_clusters", [])[:4],
                "instance_ref_count": candidate.get("instance_ref_count", 0),
                "field_names": candidate.get("field_names", [])[:10],
                "value_examples": value_examples,
            })
        schema_generation_context = build_schema_generation_context(
            schema_candidate_ir=schema_candidate_ir,
            schema_obligations=schema_candidate_ir.get("schema_obligations", [])
            if isinstance(schema_candidate_ir, dict) else [],
        )
        schema_prompt_view = schema_generation_context.get("prompt_view", {})
        return {
            "format": "schema_candidate_projection_units_for_schema_inference_v1",
            "candidate_count": projection_units.get("candidate_count", 0),
            "unit_count": len(compact_units),
            "candidate_summaries": candidate_summaries,
            "projection_units": compact_units,
            "schema_generation_context": schema_prompt_view
            if isinstance(schema_prompt_view, dict) and schema_prompt_view
            else schema_generation_context,
            "schema_generation_context_artifact_format": schema_generation_context.get("format"),
        }

    def _build_schema_projection_shards(self, projection_units: Dict[str, Any]) -> Dict[str, Any]:
        candidate_index = projection_units.get("candidate_index", {}) if isinstance(projection_units, dict) else {}
        units = projection_units.get("units", []) if isinstance(projection_units, dict) else []
        boundary_families = (
            projection_units.get("boundary_families", {}).get("families", [])
            if isinstance(projection_units.get("boundary_families", {}), dict) else []
        ) if isinstance(projection_units, dict) else []
        shard_order = [
            "catalog",
            "runtime_task_state",
            "search_form_filter",
            "policy_interaction",
            "ambiguous_mixed",
            "decorative",
        ]
        assigned: Dict[str, str] = {}
        for name, summary in candidate_index.items():
            shard = str(summary.get("shard_hint", "") or "ambiguous_mixed")
            if shard not in shard_order:
                shard = "ambiguous_mixed"
            assigned[str(name)] = shard

        def _build_slice(candidate_names: List[str], coarse_shard: str, index: int) -> Dict[str, Any]:
            decision_set = set(candidate_names)
            slice_units = []
            for unit in units:
                unit_candidate_names = set(unit.get("candidate_names", []))
                decision_names = set(unit.get("decision_candidates", []))
                if decision_names & decision_set or unit_candidate_names & decision_set:
                    projected_unit = dict(unit)
                    projected_names = [
                        name for name in (unit.get("candidate_names", []) or [])
                        if name in decision_set
                    ]
                    projected_decision_names = [
                        name for name in (unit.get("decision_candidates", []) or [])
                        if name in decision_set
                    ]
                    projected_unit["candidate_names"] = projected_names
                    projected_unit["decision_candidates"] = projected_decision_names
                    if isinstance(unit.get("candidates"), list):
                        projected_unit["candidates"] = [
                            item for item in unit.get("candidates", [])
                            if isinstance(item, dict) and item.get("name") in decision_set
                        ]
                    slice_units.append(projected_unit)
            slice_families = []
            for family in boundary_families if isinstance(boundary_families, list) else []:
                family_candidate_names = set(family.get("candidate_names", []))
                if family_candidate_names & decision_set:
                    projected_family = dict(family)
                    projected_names = [
                        name for name in (family.get("candidate_names", []) or [])
                        if name in decision_set
                    ]
                    projected_family["candidate_names"] = projected_names
                    if isinstance(family.get("candidates"), list):
                        projected_family["candidates"] = [
                            item for item in family.get("candidates", [])
                            if isinstance(item, dict) and item.get("name") in decision_set
                        ]
                    if isinstance(family.get("edges"), list):
                        projected_family["edges"] = [
                            edge for edge in family.get("edges", [])
                            if isinstance(edge, dict)
                            and edge.get("source") in decision_set
                            and edge.get("target") in decision_set
                        ]
                    slice_families.append(projected_family)
            slice_id = coarse_shard if index == 0 else f"{coarse_shard}:{index + 1}"
            return {
                "shard_id": slice_id,
                "coarse_shard": coarse_shard,
                "decision_candidates": sorted(candidate_names),
                "unit_count": len(slice_units),
                "projection_units": slice_units,
                "boundary_families": slice_families,
            }

        def _split_oversized_group(group_names: List[str], coarse_shard: str) -> List[List[str]]:
            if len(group_names) <= self.schema_boundary_slice_candidate_budget:
                preview = _build_slice(group_names, coarse_shard, 0)
                if self._estimate_schema_boundary_slice_tokens(preview) <= self.schema_boundary_slice_token_budget:
                    return [sorted(group_names)]
            by_family_key: Dict[Tuple[str, str], List[str]] = {}
            for name in sorted(group_names):
                summary = candidate_index.get(name, {})
                lexical = self._schema_projection_lexical_family(name)
                pages = summary.get("page_clusters", []) or []
                primary_page = str(pages[0]) if pages else "no_page"
                by_family_key.setdefault((lexical, primary_page), []).append(name)
            chunks: List[List[str]] = []
            for names in by_family_key.values():
                sorted_names = sorted(names)
                current: List[str] = []
                for name in sorted_names:
                    candidate_chunk = current + [name]
                    preview = _build_slice(candidate_chunk, coarse_shard, len(chunks))
                    fits_candidate_budget = len(candidate_chunk) <= self.schema_boundary_slice_candidate_budget
                    fits_token_budget = (
                        self._estimate_schema_boundary_slice_tokens(preview)
                        <= self.schema_boundary_slice_token_budget
                    )
                    if current and (not fits_candidate_budget or not fits_token_budget):
                        chunks.append(current)
                        current = [name]
                    else:
                        current = candidate_chunk
                if current:
                    chunks.append(current)
            if not chunks:
                chunks = [sorted(group_names)]
            return chunks

        shards: List[Dict[str, Any]] = []
        for shard_id in shard_order:
            decision_candidates = sorted([
                name for name, assigned_shard in assigned.items()
                if assigned_shard == shard_id
            ])
            if not decision_candidates:
                continue
            decision_set = set(decision_candidates)
            groups: List[List[str]] = []
            grouped = set()
            for family in boundary_families if isinstance(boundary_families, list) else []:
                family_candidates = sorted(
                    name for name in (family.get("candidate_names", []) or [])
                    if name in decision_set
                )
                if len(family_candidates) >= 2:
                    groups.append(family_candidates)
                    grouped.update(family_candidates)
            for name in decision_candidates:
                if name not in grouped:
                    groups.append([name])
            normalized_groups: List[List[str]] = []
            for group in groups:
                if len(group) == 1:
                    normalized_groups.append(group)
                    continue
                normalized_groups.extend(_split_oversized_group(group, shard_id))
            normalized_groups.sort(key=lambda items: (-len(items), items[0]))

            packed_groups: List[List[str]] = []
            for group in normalized_groups:
                placed = False
                for idx, existing in enumerate(packed_groups):
                    merged_names = sorted(set(existing + group))
                    if len(merged_names) > self.schema_boundary_slice_candidate_budget:
                        continue
                    preview = _build_slice(merged_names, shard_id, idx)
                    if self._estimate_schema_boundary_slice_tokens(preview) > self.schema_boundary_slice_token_budget:
                        continue
                    packed_groups[idx] = merged_names
                    placed = True
                    break
                if not placed:
                    packed_groups.append(sorted(group))

            for idx, group in enumerate(packed_groups):
                shards.append(_build_slice(group, shard_id, idx))
        return {
            "format": "schema_projection_shards_v1",
            "shard_count": len(shards),
            "shards": shards,
        }

    def _validate_schema_projection_decisions(
        self,
        schema_candidate_ir: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        boundary_families: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        expected = [
            str(candidate.get("entity_name", "")).strip()
            for candidate in schema_candidate_ir.get("entity_candidates", [])
            if isinstance(candidate, dict) and str(candidate.get("entity_name", "")).strip()
        ] if isinstance(schema_candidate_ir, dict) else []
        expected_set = set(expected)
        allowed_destinations = {
            "entity_schema",
            "merged_into_schema",
            "content_sample_only",
            "task_state_contract",
            "interaction_contract",
            "discarded_decorative",
            "rejected_with_reason",
        }
        seen: Dict[str, int] = {}
        invalid: List[Dict[str, Any]] = []
        unknown: List[str] = []
        for decision in decisions if isinstance(decisions, list) else []:
            if not isinstance(decision, dict):
                invalid.append({"candidate": "", "reason": "decision is not an object"})
                continue
            candidate = str(decision.get("candidate", "")).strip()
            destination = str(decision.get("destination", "")).strip()
            if candidate not in expected_set:
                unknown.append(candidate)
            seen[candidate] = seen.get(candidate, 0) + 1
            if destination not in allowed_destinations:
                invalid.append({"candidate": candidate, "reason": f"invalid destination: {destination}"})
            if destination in {"entity_schema", "merged_into_schema", "content_sample_only", "task_state_contract", "interaction_contract"}:
                if not str(decision.get("projected_to", "") or candidate).strip():
                    invalid.append({"candidate": candidate, "reason": "missing projected_to"})
        missing = sorted(name for name in expected if seen.get(name, 0) == 0)
        duplicates = sorted(name for name, count in seen.items() if name and count > 1)
        core_valid = not missing and not duplicates and not invalid and not unknown
        boundary_invalid: List[Dict[str, Any]] = []
        if isinstance(boundary_families, dict):
            decision_by_candidate = {
                str(decision.get("candidate", "")).strip(): decision
                for decision in decisions if isinstance(decision, dict)
            }
            for family in boundary_families.get("families", []) if isinstance(boundary_families.get("families", []), list) else []:
                if not isinstance(family, dict) or not family.get("requires_split_reason"):
                    continue
                targets: Dict[str, List[str]] = {}
                for candidate in family.get("candidate_names", []) or []:
                    decision = decision_by_candidate.get(str(candidate).strip())
                    if not decision:
                        continue
                    if decision.get("destination") not in {"entity_schema", "merged_into_schema"}:
                        continue
                    target = str(decision.get("projected_to", "") or decision.get("candidate", "")).strip()
                    if target:
                        targets.setdefault(target, []).append(str(candidate).strip())
                if len(targets) <= 1:
                    continue
                missing_reason = [
                    candidate
                    for candidates_for_target in targets.values()
                    for candidate in candidates_for_target
                    if not decision_by_candidate.get(candidate, {}).get("split_reason")
                ]
                if missing_reason:
                    boundary_invalid.append({
                        "candidate": ",".join(sorted(missing_reason)),
                        "reason": (
                            f"boundary family {family.get('family_id', '')} keeps multiple schema targets "
                            "without split_reason"
                        ),
                    })
        invalid.extend(boundary_invalid)
        boundary_family_gate_valid = not boundary_invalid
        return {
            "valid": core_valid and boundary_family_gate_valid,
            "core_valid": core_valid,
            "boundary_family_gate_valid": boundary_family_gate_valid,
            "expected_count": len(expected),
            "decision_count": len(decisions) if isinstance(decisions, list) else 0,
            "missing_candidates": missing,
            "duplicate_candidates": duplicates,
            "invalid_decisions": invalid,
            "boundary_family_invalid_decisions": boundary_invalid,
            "unknown_candidates": sorted(set(item for item in unknown if item)),
        }

    def _build_schema_boundary_planner_prompt(self, planner_slice: Dict[str, Any]) -> str:
        payload = {
            "task": (
                "Decide canonical schema boundaries and final schema projection destinations "
                "for decision_candidates only."
            ),
            "response_format_instruction": "Return a JSON object only.",
            "allowed_destinations": [
                "entity_schema",
                "merged_into_schema",
                "content_sample_only",
                "task_state_contract",
                "interaction_contract",
                "discarded_decorative",
                "rejected_with_reason",
            ],
            "rules": [
                "Every decision_candidate must receive exactly one decision.",
                "Use projection_units only as compressed context; do not invent candidates.",
                "Use boundary_families to judge stable business boundaries before assigning candidate destinations.",
                "Same business object across different pages should usually merge to one canonical schema; page role differences are samples or optional fields, not schema boundaries.",
                "If a high-overlap boundary_family keeps multiple durable schema targets, every split candidate must include split_reason.",
                "Catalog durable business data may be entity_schema.",
                "Input values, selections, filters, runtime order/payment state should be task_state_contract.",
                "Buttons, policies, navigation actions, sheets, and UI-only controls should be interaction_contract or discarded_decorative.",
                "If a candidate maps to an existing schema, set projected_to to that schema name.",
            ],
            "planner_slice_id": planner_slice.get("shard_id", planner_slice.get("slice_id", "")),
            "decision_candidates": planner_slice.get("decision_candidates", []),
            "boundary_context": {
                "units": planner_slice.get("projection_units", []),
                "families": planner_slice.get("boundary_families", []),
            },
            "output_schema": {
                "canonical_boundaries": [
                    {
                        "canonical_schema": "CanonicalSchemaName",
                        "members": ["CandidateName"],
                        "boundary_type": "durable_entity|task_state|interaction|content_sample_only",
                        "reason": "why these members share or split a stable boundary",
                        "field_policy": {
                            "keep": ["field_name"],
                            "normalize": [
                                {"from": "old_field", "to": "canonical_field"}
                            ],
                        },
                    }
                ],
                "decisions": [
                    {
                        "candidate": "CandidateName",
                        "destination": "entity_schema|merged_into_schema|content_sample_only|task_state_contract|interaction_contract|discarded_decorative|rejected_with_reason",
                        "projected_to": "SchemaOrContractId",
                        "reason": "short reason",
                        "split_reason": {
                            "identity_lifecycle_diff": False,
                            "task_requires_domain_fields": False,
                            "domain_specific_fields": [],
                            "why_optional_fields_not_enough": "",
                        },
                    }
                ]
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _build_schema_projection_planner_prompt(self, shard: Dict[str, Any]) -> str:
        return self._build_schema_boundary_planner_prompt(shard)

    async def _plan_schema_boundaries(
        self,
        schema_candidate_ir: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
    ) -> Dict[str, Any]:
        pre_routed_decisions = self._build_deterministic_boundary_pre_routing(
            schema_candidate_ir,
            rejected_candidates=rejected_candidates,
        )
        pre_routed_candidate_names = {
            str(decision.get("candidate", "")).strip()
            for decision in pre_routed_decisions
            if isinstance(decision, dict) and str(decision.get("candidate", "")).strip()
        }
        planner_candidate_names = {
            str(candidate.get("entity_name", "")).strip()
            for candidate in schema_candidate_ir.get("entity_candidates", [])
            if isinstance(candidate, dict) and str(candidate.get("entity_name", "")).strip()
        } - pre_routed_candidate_names
        boundary_context = self._build_schema_boundary_context(
            schema_candidate_ir,
            entity_schemas,
            rejected_candidates,
            local_to_global_entity,
            allowed_candidate_names=planner_candidate_names,
        )
        planner_slices = boundary_context.get("planner_slices", []) if isinstance(boundary_context, dict) else []
        boundary_families = {
            "format": "schema_boundary_families_v1",
            "families": boundary_context.get("families", []),
        } if isinstance(boundary_context, dict) else {}
        planner_raw_outputs: List[Dict[str, Any]] = []
        resolved: Dict[str, Dict[str, Any]] = {}
        resolver_conflicts: List[Dict[str, Any]] = []

        async def _plan_slice(planner_slice: Dict[str, Any]) -> Dict[str, Any]:
            prompt = self._build_schema_boundary_planner_prompt(planner_slice)
            result: Dict[str, Any] = {}
            try:
                raw, _usage = await call_openai_api_json_async(
                    [{"role": "user", "content": prompt}],
                    reasoning_effort="medium",
                    model=self.model,
                    stage="schema_projection_planner",
                )
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if isinstance(raw, dict):
                    result = raw
            except Exception as e:
                result = {"decisions": [], "error": str(e)}
            return {
                "shard_id": planner_slice.get("shard_id", planner_slice.get("slice_id", "")),
                "decision_candidates": planner_slice.get("decision_candidates", []),
                "raw_output": result,
            }

        slice_results = await asyncio.gather(*[
            _plan_slice(planner_slice)
            for planner_slice in planner_slices
        ])
        planner_raw_outputs.extend(slice_results)

        for slice_result in slice_results:
            allowed = set(slice_result.get("decision_candidates", []))
            raw_output = slice_result.get("raw_output", {})
            decisions = raw_output.get("decisions", []) if isinstance(raw_output, dict) else []
            for decision in decisions if isinstance(decisions, list) else []:
                if not isinstance(decision, dict):
                    continue
                candidate = str(decision.get("candidate", "")).strip()
                if candidate not in allowed:
                    resolver_conflicts.append({
                        "candidate": candidate,
                        "shard_id": slice_result.get("shard_id", ""),
                        "reason": "decision outside slice ownership ignored",
                    })
                    continue
                normalized_decision = {
                    "candidate": candidate,
                    "destination": str(decision.get("destination", "")).strip(),
                    "projected_to": str(decision.get("projected_to", "") or candidate).strip(),
                    "reason": str(decision.get("reason", "")).strip(),
                    "planner_shard": slice_result.get("shard_id", ""),
                }
                if isinstance(decision.get("split_reason"), dict):
                    normalized_decision["split_reason"] = decision.get("split_reason")
                if candidate in resolved:
                    resolver_conflicts.append({
                        "candidate": candidate,
                        "kept_shard": resolved[candidate].get("planner_shard", ""),
                        "ignored_shard": slice_result.get("shard_id", ""),
                        "reason": "duplicate planner decision ignored by resolver",
                    })
                    continue
                resolved[candidate] = normalized_decision

        decisions = [resolved[name] for name in sorted(resolved.keys())]
        decisions.extend(pre_routed_decisions)
        decisions.sort(key=lambda item: str(item.get("candidate", "")))
        decisions = self._apply_schema_family_canonical_merges(
            decisions,
            schema_candidate_ir,
        )
        validation = self._validate_schema_projection_decisions(
            schema_candidate_ir,
            decisions,
            boundary_families=boundary_families,
        )
        validation["resolver_conflicts"] = resolver_conflicts
        if not validation.get("core_valid", validation["valid"]):
            fallback_projection = self._build_schema_candidate_projection(
                schema_candidate_ir,
                entity_schemas,
                rejected_candidates,
                local_to_global_entity,
            )
            fallback_decisions = fallback_projection.get("decisions", [])
            validation = self._validate_schema_projection_decisions(
                schema_candidate_ir,
                fallback_decisions,
                boundary_families=boundary_families,
            )
            validation["fallback_used"] = True
            validation["resolver_conflicts"] = resolver_conflicts
            decisions = self._apply_schema_family_canonical_merges(
                fallback_decisions,
                schema_candidate_ir,
            )
        else:
            validation["fallback_used"] = False

        return {
            "format": "schema_boundary_plan_v1",
            "boundary_context": boundary_context,
            "planner_raw_outputs": planner_raw_outputs,
            "decisions": decisions,
            "validation": validation,
        }

    async def _plan_schema_projection_decisions_with_units(
        self,
        schema_candidate_ir: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._plan_schema_boundaries(
            schema_candidate_ir,
            entity_schemas,
            rejected_candidates,
            local_to_global_entity,
        )

    def _schema_family_canonical_target(
        self,
        entity_name: str,
        projected_to: str,
        field_names: set,
    ) -> str:
        name = str(projected_to or entity_name or "").strip()
        if not name:
            return ""
        target_norm_source = _camel_to_snake(name).lower()
        candidate_norm_source = _camel_to_snake(str(entity_name or "")).lower()
        combined = f"{candidate_norm_source} {target_norm_source}"

        place_fields = {"address", "full_address", "distance", "poi_type", "area_section", "address_snippet"}
        if (
            "place" in combined
            and field_names.intersection(place_fields)
            and re.search(r"(?:search|result|nearby|map|poi|address|candidate)", combined)
        ):
            return "Place"

        review_fields = {"rating", "rating_goods", "rating_package", "reviewer_name", "username", "content", "merchant_reply_excerpt"}
        if "review" in combined and field_names.intersection(review_fields):
            return "Review"

        redeemable_fields = {
            "coupon_amount", "coupon_label", "coupon_value", "voucher_value",
            "value", "value_or_face", "value_and_condition", "condition",
            "use_condition", "action_text", "acquire_price", "countdown",
            "availability_text",
        }
        if re.search(r"(?:coupon|voucher)", combined) and (
            field_names.intersection(redeemable_fields)
            or "offer" in combined
        ):
            return "Voucher"

        offer_fields = {
            "offer_type", "offer_text", "rule_text", "discount_text", "discount_amount",
            "promo_badge", "promo_price", "campaign_name", "benefit_text", "reward_order_1",
            "reward_order_2", "reward_order_3", "original_price", "price",
        }
        has_reward_fields = any(field.startswith("reward_") for field in field_names)
        if re.search(r"(?:deal|offer|discount|promo|promotion|campaign|reward)", combined) and (
            field_names.intersection(offer_fields)
            or has_reward_fields
        ):
            return "Offer"

        return ""

    def _apply_schema_family_canonical_merges(
        self,
        decisions: List[Dict[str, Any]],
        schema_candidate_ir: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not decisions:
            return []
        candidate_by_name = {
            str(candidate.get("entity_name", "")).strip(): candidate
            for candidate in schema_candidate_ir.get("entity_candidates", [])
            if isinstance(candidate, dict) and str(candidate.get("entity_name", "")).strip()
        } if isinstance(schema_candidate_ir, dict) else {}

        rewritten: List[Dict[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            item = dict(decision)
            if item.get("destination") not in {"entity_schema", "merged_into_schema"}:
                rewritten.append(item)
                continue
            candidate_name = str(item.get("candidate", "")).strip()
            projected_to = str(item.get("projected_to", "") or candidate_name).strip()
            candidate = candidate_by_name.get(candidate_name, {})
            field_candidates = candidate.get("field_candidates", {}) if isinstance(candidate, dict) else {}
            field_names = {
                _camel_to_snake(str(field_name).strip()).lower()
                for field_name in (field_candidates.keys() if isinstance(field_candidates, dict) else [])
                if str(field_name).strip()
            }
            canonical = self._schema_family_canonical_target(
                candidate_name,
                projected_to,
                field_names,
            )
            if canonical:
                item["projected_to"] = canonical
                item["destination"] = (
                    "entity_schema"
                    if self._normalize_entity_name(candidate_name) == self._normalize_entity_name(canonical)
                    else "merged_into_schema"
                )
                reason = str(item.get("reason", "")).strip()
                family_reason = f"Canonical family merge normalized to {canonical}."
                item["reason"] = f"{reason} {family_reason}".strip() if reason else family_reason
            rewritten.append(item)
        return rewritten

    def _build_schema_candidate_projection(
        self,
        schema_candidate_ir: Dict[str, Any],
        entity_schemas: List[Dict[str, Any]],
        rejected_candidates: List[Dict[str, Any]],
        local_to_global_entity: Dict[str, Any],
        planned_decisions: Optional[List[Dict[str, Any]]] = None,
        projection_plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        schema_names = {
            str(schema.get("name", "")).strip()
            for schema in entity_schemas or []
            if isinstance(schema, dict) and schema.get("name")
        }
        schema_by_norm = {
            self._normalize_entity_name(name): name
            for name in schema_names
        }
        mapping_by_norm: Dict[str, str] = {}
        for local_name, global_name in (local_to_global_entity or {}).items():
            if not global_name:
                continue
            mapping_by_norm[self._normalize_entity_name(str(local_name))] = str(global_name).strip()

        rejected_by_norm: Dict[str, Dict[str, Any]] = {}
        for item in rejected_candidates or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("entity_name", "") or item.get("name", "")).strip()
            if name:
                rejected_by_norm[self._normalize_entity_name(name)] = item

        decisions: List[Dict[str, Any]] = []
        task_states: List[Dict[str, Any]] = []
        interactions: List[Dict[str, Any]] = []
        success_hints: List[Dict[str, Any]] = []
        planned_by_norm: Dict[str, Dict[str, Any]] = {}
        normalized_planned_decisions = self._apply_schema_family_canonical_merges(
            planned_decisions or [],
            schema_candidate_ir,
        )
        for decision in normalized_planned_decisions:
            if not isinstance(decision, dict):
                continue
            candidate_name = str(decision.get("candidate", "")).strip()
            if candidate_name:
                planned_by_norm[self._normalize_entity_name(candidate_name)] = decision

        for candidate in schema_candidate_ir.get("entity_candidates", []) if isinstance(schema_candidate_ir, dict) else []:
            if not isinstance(candidate, dict):
                continue
            entity_name = str(candidate.get("entity_name", "")).strip()
            if not entity_name:
                continue
            norm = self._normalize_entity_name(entity_name)
            role = str(candidate.get("role", "") or "catalog_data").strip()
            support = candidate.get("support", {}) if isinstance(candidate.get("support", {}), dict) else {}
            rejected = rejected_by_norm.get(norm)
            mapped_to = mapping_by_norm.get(norm, "")
            projected_to = ""
            destination = ""
            reason = ""

            contract_destination = self._schema_candidate_contract_destination(entity_name, role, candidate)
            planned = planned_by_norm.get(norm)
            if planned:
                destination = str(planned.get("destination", "")).strip()
                projected_to = str(planned.get("projected_to", "") or entity_name).strip()
                reason = str(planned.get("reason", "") or "Projected by model-based schema projection planner.").strip()
                if (
                    contract_destination != "entity_schema"
                    and destination in {"entity_schema", "merged_into_schema", "content_sample_only"}
                ):
                    destination = contract_destination
                    if destination == "task_state_contract":
                        projected_to = _camel_to_snake(entity_name)
                    elif destination == "interaction_contract":
                        projected_to = _camel_to_snake(entity_name)
                    else:
                        projected_to = ""
                    reason = (
                        "Deterministic boundary guard overrode planner durable-schema projection: "
                        + reason
                    )
            elif rejected:
                destination = "rejected_with_reason"
                reason = str(rejected.get("reason", "") or rejected.get("rejected_reason", "") or "Rejected by schema inference.").strip()
            elif contract_destination != "entity_schema":
                destination = contract_destination
                if destination == "task_state_contract":
                    projected_to = _camel_to_snake(entity_name)
                    reason = "Task-related state is modeled as page/task state instead of durable schema."
                elif destination == "interaction_contract":
                    projected_to = _camel_to_snake(entity_name)
                    reason = "UI/action/policy candidate is modeled as interaction behavior instead of durable schema."
                elif destination == "discarded_decorative":
                    reason = "Decorative or layout-only evidence is not projected into schema or contracts."
            elif mapped_to and mapped_to in schema_names:
                destination = "entity_schema"
                projected_to = mapped_to
                reason = "Mapped to a durable entity schema by schema inference."
            elif norm in schema_by_norm:
                destination = "entity_schema"
                projected_to = schema_by_norm[norm]
                reason = "Candidate is represented by a durable entity schema."
            else:
                destination = contract_destination
                projected_to = entity_name
                reason = "Candidate remains durable business data."

            evidence_steps = [
                str(item).strip()
                for item in support.get("step_refs", [])
                if str(item).strip()
            ]
            page_clusters = [
                str(item).strip()
                for item in support.get("page_clusters", [])
                if str(item).strip()
            ]
            page_names = [
                str(item).strip()
                for item in support.get("page_names", [])
                if str(item).strip()
            ]
            values = self._schema_candidate_values(candidate.get("field_candidates", {}))

            decision = {
                "candidate": entity_name,
                "role": role,
                "task_related": bool(candidate.get("task_related", False)),
                "destination": destination,
                "projected_to": projected_to,
                "reason": reason,
                "evidence_steps": evidence_steps,
                "page_clusters": page_clusters,
                "page_names": page_names,
            }
            decisions.append(decision)

            if destination == "task_state_contract":
                state_id = projected_to
                if role == "selectable_task_state" and not state_id.startswith("selected_"):
                    state_id = "selected_" + state_id
                task_states.append({
                    "state_id": state_id,
                    "entity_name": entity_name,
                    "state_type": role,
                    "values": values,
                    "observed_on_pages": page_clusters,
                    "page_names": page_names,
                    "evidence_steps": evidence_steps,
                })
            elif destination == "interaction_contract":
                interactions.append({
                    "action_id": _camel_to_snake(entity_name),
                    "source_entity": entity_name,
                    "interaction_type": role,
                    "preconditions": [],
                    "effects": [
                        {
                            "op": "update_or_navigate",
                            "target_ref": _camel_to_snake(entity_name),
                        }
                    ],
                    "observed_on_pages": page_clusters,
                    "page_names": page_names,
                    "evidence_steps": evidence_steps,
                })

            if bool(candidate.get("task_related", False)) and destination in {
                "task_state_contract",
                "interaction_contract",
                "entity_schema",
                "merged_into_schema",
            }:
                success_hints.append({
                    "hint_id": _camel_to_snake(entity_name),
                    "source_candidate": entity_name,
                    "evidence_kind": destination,
                    "projected_to": projected_to or entity_name,
                    "evidence_steps": evidence_steps,
                    "page_clusters": page_clusters,
                })

        projection = {
            "format": "schema_candidate_projection_v1",
            "decisions": decisions,
            "task_state_contract": {
                "format": "task_state_contract_v1",
                "states": task_states,
            },
            "interaction_contract": {
                "format": "interaction_contract_v1",
                "actions": interactions,
            },
            "success_evidence_hints": {
                "format": "success_evidence_hints_v1",
                "hints": success_hints,
            },
        }
        if projection_plan:
            projection["projection_units"] = projection_plan.get("projection_units", {})
            projection["boundary_families"] = projection_plan.get("boundary_families", {})
            projection["projection_shards"] = projection_plan.get("projection_shards", {})
            projection["planner_raw_outputs"] = projection_plan.get("planner_raw_outputs", [])
            projection["projection_validation"] = projection_plan.get("validation", {})
        return projection

    def _filter_schemas_by_projection(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        schema_projection: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        decisions = schema_projection.get("decisions", []) if isinstance(schema_projection, dict) else []
        merge_targets_by_norm = {
            self._normalize_entity_name(str(decision.get("candidate", ""))): str(decision.get("projected_to", "")).strip()
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("candidate")
            and decision.get("destination") == "merged_into_schema"
            and str(decision.get("projected_to", "")).strip()
        }
        deferred_names = {
            self._normalize_entity_name(str(decision.get("candidate", "")))
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("candidate")
            and decision.get("destination") not in {
                "entity_schema",
                "merged_into_schema",
                "content_sample_only",
            }
        }
        if not deferred_names and not merge_targets_by_norm:
            return list(entity_schemas or []), list(entity_relationships or [])

        filtered_schemas: List[Dict[str, Any]] = []
        removed_names: set = set()
        for schema in entity_schemas or []:
            if not isinstance(schema, dict):
                continue
            name = str(schema.get("name", "")).strip()
            norm_name = self._normalize_entity_name(name)
            if name and norm_name in deferred_names:
                removed_names.add(name)
                continue
            if name and norm_name in merge_targets_by_norm:
                schema = dict(schema)
                schema["name"] = merge_targets_by_norm[norm_name]
            filtered_schemas.append(schema)

        if not removed_names:
            return filtered_schemas, list(entity_relationships or [])

        filtered_relationships = [
            rel for rel in entity_relationships or []
            if isinstance(rel, dict)
            and all(
                rel.get(key) not in removed_names
                for key in ("from", "to", "source", "target", "entity", "parent", "child")
                if rel.get(key)
            )
        ]
        if self.logger:
            self.logger.log_info(
                f"[Schema] Deferred {len(removed_names)} task-state/interaction schema(s) to contracts: "
                f"{sorted(removed_names)}"
            )
        return filtered_schemas, filtered_relationships

    def _prune_unsupported_entity_schemas_after_projection(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        schema_projection: Dict[str, Any],
        content_samples: Dict[str, List[Dict]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        decisions = schema_projection.get("decisions", []) if isinstance(schema_projection, dict) else []
        supported_norms = {
            self._normalize_entity_name(str(decision.get("projected_to", "") or decision.get("candidate", "")))
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("destination") in {"entity_schema", "merged_into_schema"}
            and str(decision.get("projected_to", "") or decision.get("candidate", "")).strip()
        }
        if not entity_schemas:
            return [], list(entity_relationships or [])

        filtered_schemas: List[Dict[str, Any]] = []
        removed_names: set = set()
        removed_norms: set = set()
        for schema in entity_schemas or []:
            if not isinstance(schema, dict):
                continue
            name = str(schema.get("name", "")).strip()
            norm = self._normalize_entity_name(name)
            if name and norm not in supported_norms:
                removed_names.add(name)
                removed_norms.add(norm)
                continue
            filtered_schemas.append(schema)

        if not removed_names:
            return filtered_schemas, list(entity_relationships or [])

        filtered_relationships = []
        for rel in entity_relationships or []:
            if not isinstance(rel, dict):
                continue
            referenced_removed = False
            for key in ("from", "to", "source", "target", "entity", "parent", "child"):
                value = rel.get(key)
                if value and self._normalize_entity_name(str(value)) in removed_norms:
                    referenced_removed = True
                    break
            if not referenced_removed:
                filtered_relationships.append(rel)

        if self.logger:
            self.logger.log_info(
                f"[Schema] Pruned {len(removed_names)} unsupported entity schema(s) after projection: "
                f"{sorted(removed_names)}"
            )
        return filtered_schemas, filtered_relationships

    def _collect_schema_admission_signals(
        self,
        schema_name: str,
        decisions: List[Dict[str, Any]],
        candidate_by_name: Dict[str, Dict[str, Any]],
        weak_fields: set,
        strong_field_hints: set,
    ) -> Dict[str, Any]:
        task_related = False
        trace_ids: set = set()
        page_clusters: set = set()
        instance_count = 0
        merge_support_count = 0
        strong_fields: set = set()
        weak_field_count = 0
        total_field_count = 0
        for decision in decisions:
            if decision.get("task_related"):
                task_related = True
            if decision.get("destination") == "merged_into_schema":
                merge_support_count += 1
            candidate = candidate_by_name.get(str(decision.get("candidate", "")).strip(), {})
            support = candidate.get("support", {}) if isinstance(candidate.get("support", {}), dict) else {}
            for trace_id in support.get("trace_ids", []) or []:
                if str(trace_id).strip():
                    trace_ids.add(str(trace_id).strip())
            for page in support.get("page_clusters", []) or []:
                if str(page).strip():
                    page_clusters.add(str(page).strip())
            instance_count = max(instance_count, len(support.get("instance_refs", []) or []))
            field_candidates = candidate.get("field_candidates", {})
            if isinstance(field_candidates, dict):
                for field_name in field_candidates.keys():
                    field = _camel_to_snake(str(field_name).strip())
                    if not field:
                        continue
                    total_field_count += 1
                    if field in weak_fields:
                        weak_field_count += 1
                    if field in strong_field_hints or any(hint in field for hint in strong_field_hints):
                        strong_fields.add(field)
        weak_field_ratio = (
            weak_field_count / total_field_count
            if total_field_count > 0 else 1.0
        )
        return {
            "schema_name": schema_name,
            "candidate_count": len(decisions),
            "task_related": task_related,
            "trace_count": len(trace_ids),
            "page_count": len(page_clusters),
            "instance_count": instance_count,
            "merge_support_count": merge_support_count,
            "strong_field_count": len(strong_fields),
            "weak_field_ratio": weak_field_ratio,
        }

    def _durable_entity_score(self, signals: Dict[str, Any]) -> Tuple[int, List[str]]:
        score = 0
        reasons: List[str] = []
        name = str(signals.get("schema_name", "") or "")
        if signals.get("task_related"):
            score += 4
            reasons.append("task_related")
        candidate_count = int(signals.get("candidate_count", 0) or 0)
        if candidate_count >= 3:
            score += 2
            reasons.append("multi_candidate")
        elif candidate_count >= 2:
            score += 1
            reasons.append("two_candidates")
        merge_support_count = int(signals.get("merge_support_count", 0) or 0)
        if merge_support_count >= 2:
            score += 2
            reasons.append("merge_family_support")
        elif merge_support_count == 1:
            score += 1
            reasons.append("single_merge_support")
        trace_count = int(signals.get("trace_count", 0) or 0)
        if trace_count >= 3:
            score += 2
            reasons.append("multi_trace")
        elif trace_count >= 2:
            score += 1
            reasons.append("two_traces")
        page_count = int(signals.get("page_count", 0) or 0)
        if page_count >= 3:
            score += 2
            reasons.append("multi_page")
        elif page_count >= 2:
            score += 1
            reasons.append("two_pages")
        instance_count = int(signals.get("instance_count", 0) or 0)
        if instance_count >= 2:
            score += 2
            reasons.append("multi_instance")
        elif instance_count >= 1:
            score += 1
            reasons.append("instance_anchor")
        strong_field_count = int(signals.get("strong_field_count", 0) or 0)
        if strong_field_count >= 5:
            score += 3
            reasons.append("strong_business_fields>=5")
        elif strong_field_count >= 3:
            score += 2
            reasons.append("strong_business_fields>=3")
        elif strong_field_count >= 1:
            score += 1
            reasons.append("strong_business_fields>=1")
        weak_field_ratio = float(signals.get("weak_field_ratio", 1.0) or 1.0)
        if weak_field_ratio <= 0.25:
            score += 1
            reasons.append("low_weak_field_ratio")
        elif weak_field_ratio >= 0.60:
            score -= 2
            reasons.append("high_weak_field_ratio")
        if trace_count <= 1 and page_count <= 1 and instance_count == 0:
            score -= 2
            reasons.append("single_context_only")
        if strong_field_count == 0:
            score -= 2
            reasons.append("no_strong_business_field")
        if re.search(r"(keyword|topic|badge|feature|preview|campaign|tier|trend|ranking|discover)", name, re.IGNORECASE):
            score -= 2
            reasons.append("display_keyword_name")
        return score, reasons

    def _prune_low_durability_entity_schemas_after_projection(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        schema_projection: Dict[str, Any],
        schema_candidate_ir: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if not entity_schemas:
            return [], list(entity_relationships or [])

        candidate_by_name = {
            str(candidate.get("entity_name", "")).strip(): candidate
            for candidate in schema_candidate_ir.get("entity_candidates", [])
            if isinstance(candidate, dict) and str(candidate.get("entity_name", "")).strip()
        } if isinstance(schema_candidate_ir, dict) else {}

        weak_fields = {
            "id", "name", "title", "label", "text", "display_text", "subtitle", "note",
            "badge", "badge_text", "topic", "keyword", "rank", "ranking", "entry_type",
            "section", "section_title", "image", "image_url", "icon", "logo", "avatar",
            "tag", "promo_tag", "category_tag",
        }
        strong_field_hints = {
            "address", "phone", "price", "availability", "eta", "rating", "merchant",
            "product", "voucher_value", "distance", "sales", "quantity", "amount",
            "delivery_fee", "min_order", "category_hint", "recipient", "discount_amount",
            "coupon_value",
        }
        admission_threshold = 3

        decision_groups: Dict[str, List[Dict[str, Any]]] = {}
        for decision in schema_projection.get("decisions", []) if isinstance(schema_projection, dict) else []:
            if not isinstance(decision, dict):
                continue
            if decision.get("destination") not in {"entity_schema", "merged_into_schema"}:
                continue
            target = str(decision.get("projected_to", "") or decision.get("candidate", "")).strip()
            if target:
                decision_groups.setdefault(target, []).append(decision)

        removed_names: set = set()
        removed_norms: set = set()
        kept: List[Dict[str, Any]] = []
        for schema in entity_schemas or []:
            if not isinstance(schema, dict):
                continue
            name = str(schema.get("name", "")).strip()
            decisions = decision_groups.get(name, [])
            if not decisions:
                kept.append(schema)
                continue

            signals = self._collect_schema_admission_signals(
                name,
                decisions,
                candidate_by_name,
                weak_fields,
                strong_field_hints,
            )
            score, reasons = self._durable_entity_score(signals)
            if not signals.get("task_related") and score < admission_threshold:
                removed_names.add(name)
                removed_norms.add(self._normalize_entity_name(name))
                if self.logger:
                    self.logger.log_info(
                        f"[Schema] Admission gate removed '{name}' with score={score}: {', '.join(reasons)}"
                    )
                continue
            kept.append(schema)

        if not removed_names:
            return kept, list(entity_relationships or [])

        filtered_relationships = [
            rel for rel in entity_relationships or []
            if isinstance(rel, dict)
            and all(
                self._normalize_entity_name(str(rel.get(key))) not in removed_norms
                for key in ("from", "to", "source", "target", "entity", "parent", "child")
                if rel.get(key)
            )
        ]
        if self.logger:
            self.logger.log_info(
                f"[Schema] Pruned {len(removed_names)} low-durability schema(s): {sorted(removed_names)}"
            )
        return kept, filtered_relationships

    def _project_content_samples(
        self,
        content_samples: Dict[str, List[Dict]],
        schema_projection: Dict[str, Any],
    ) -> Dict[str, List[Dict]]:
        if not isinstance(content_samples, dict) or not isinstance(schema_projection, dict):
            return content_samples
        decisions = {
            self._normalize_entity_name(str(item.get("candidate", ""))): item
            for item in schema_projection.get("decisions", [])
            if isinstance(item, dict) and item.get("candidate")
        }
        projected: Dict[str, List[Dict]] = {}
        for entity_name, items in content_samples.items():
            decision = decisions.get(self._normalize_entity_name(str(entity_name)))
            if not decision:
                projected.setdefault(entity_name, []).extend(items)
                continue
            destination = decision.get("destination", "")
            if destination not in {"entity_schema", "merged_into_schema", "content_sample_only"}:
                continue
            target = str(decision.get("projected_to", "") or entity_name).strip()
            if not target:
                continue
            projected.setdefault(target, []).extend(items)
        return projected

    @staticmethod
    def _infer_schema_candidate_field_type(values: Any) -> str:
        seq = values if isinstance(values, list) else [values]
        non_empty = [value for value in seq if value is not None and value != ""]
        if non_empty and all(isinstance(value, bool) for value in non_empty):
            return "boolean"
        if non_empty and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_empty):
            return "number"
        return "string"

    def _apply_schema_operations(self, schemas, operations, existing_relationships=None):
        return apply_schema_operations(schemas, operations, existing_relationships=existing_relationships)

    def _consolidate_entity_schemas(self, content: "SiteContent") -> "SiteContent":
        import re as _re
        schemas = content.entity_schemas
        if not schemas:
            return content


        ui_only_patterns = {
            "navigationitem", "navitem", "navlink", "tabitem",
            "filteroption", "filterchip", "breadcrumbitem", "sidebarlink",
            "footerlink", "headerlink", "iconbutton", "toolbaritem",
        }


        groups: Dict[str, List[Dict]] = {}
        for schema in schemas:
            if not isinstance(schema, dict) or not schema.get("name"):
                continue
            norm = self._normalize_entity_name(schema["name"])
            groups.setdefault(norm, []).append(schema)

        merged: List[Dict] = []
        for norm, group in groups.items():
            if norm in ui_only_patterns:
                continue
            base = dict(group[0])
            if len(group) > 1:
                existing_fields = {f["name"] for f in base.get("fields", []) if isinstance(f, dict) and f.get("name")}
                for extra in group[1:]:
                    for f in extra.get("fields", []):
                        if isinstance(f, dict) and f.get("name") and f["name"] not in existing_fields:
                            base.setdefault("fields", []).append(f)
                            existing_fields.add(f["name"])
                    base["observed_item_count"] = max(
                        base.get("observed_item_count", 0),
                        extra.get("observed_item_count", 0)
                    )
            merged.append(base)


        comp_sources: Dict[str, set] = {}
        for ename, comps in content.entity_component_sources.items():
            norm = self._normalize_entity_name(ename)
            comp_sources.setdefault(norm, set()).update(comps)


        rel_entities = set()
        for rel in content.entity_relationships:
            if isinstance(rel, dict):
                for key in ("from", "to", "source", "target", "entity", "parent", "child"):
                    val = rel.get(key, "")
                    if val:
                        rel_entities.add(val)

        pruned = []
        for schema in merged:
            fields = [f for f in schema.get("fields", []) if isinstance(f, dict) and f.get("name")]
            if len(fields) <= 1 and schema.get("name", "") not in rel_entities:
                continue
            pruned.append(schema)
        merged = pruned


        merged = normalize_fields_to_snake_case(merged)


        def _pascal_words(name: str) -> set:
            words = _re.findall(r'[A-Z][a-z]*|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)', name)
            return {w.lower() for w in words if len(w) > 1}

        def _field_names(schema: Dict) -> set:
            return {f["name"] for f in schema.get("fields", []) if isinstance(f, dict) and f.get("name")}

        def _merge_two(a: Dict, b: Dict) -> Dict:
            result = dict(a)
            existing = {f["name"] for f in result.get("fields", []) if isinstance(f, dict) and f.get("name")}
            for f in b.get("fields", []):
                if isinstance(f, dict) and f.get("name") and f["name"] not in existing:
                    result.setdefault("fields", []).append(f)
                    existing.add(f["name"])
            result["observed_item_count"] = max(
                result.get("observed_item_count", 0), b.get("observed_item_count", 0)
            )
            return result

        def _pick_name(name_a: str, name_b: str, fields_a: set, fields_b: set) -> str:
            words_a, words_b = _pascal_words(name_a), _pascal_words(name_b)
            common = words_a & words_b
            generic_common_words = {
                "product", "item", "deal", "category", "offer", "service",
                "voucher", "coupon", "review", "merchant", "place", "promotion",
            }
            if (
                common
                and common != words_a
                and common != words_b
                and not (len(common) == 1 and next(iter(common)) in generic_common_words)
            ):
                return "".join(w.capitalize() for w in sorted(common))
            return name_a if len(fields_a) >= len(fields_b) else name_b

        rename_map: Dict[str, str] = {}


        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                if merged[i] is None:
                    continue
                for j in range(i + 1, len(merged)):
                    if merged[j] is None:
                        continue
                    words_i = _pascal_words(merged[i]["name"])
                    words_j = _pascal_words(merged[j]["name"])
                    if not words_i or not words_j:
                        continue

                    ci = comp_sources.get(self._normalize_entity_name(merged[i]["name"]), set())
                    cj = comp_sources.get(self._normalize_entity_name(merged[j]["name"]), set())
                    if ci and cj and not (ci & cj):
                        continue
                    jaccard = len(words_i & words_j) / len(words_i | words_j)
                    if jaccard <= 0.5:
                        continue

                    fi, fj = _field_names(merged[i]), _field_names(merged[j])
                    if fi and fj and (len(fi & fj) / min(len(fi), len(fj))) < 0.3:
                        continue

                    if len(fi | fj) > 20:
                        continue
                    old_i, old_j = merged[i]["name"], merged[j]["name"]
                    new_name = _pick_name(old_i, old_j, fi, fj)
                    merged[i] = _merge_two(merged[i], merged[j])
                    merged[i]["name"] = new_name
                    merged[j] = None
                    if old_i != new_name:
                        rename_map[old_i] = new_name
                    if old_j != new_name:
                        rename_map[old_j] = new_name
                    changed = True
                    break
                if changed:
                    break
            merged = [s for s in merged if s is not None]


        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                if merged[i] is None:
                    continue
                for j in range(i + 1, len(merged)):
                    if merged[j] is None:
                        continue
                    fi, fj = _field_names(merged[i]), _field_names(merged[j])
                    if not fi or not fj:
                        continue

                    ci = comp_sources.get(self._normalize_entity_name(merged[i]["name"]), set())
                    cj = comp_sources.get(self._normalize_entity_name(merged[j]["name"]), set())
                    if ci and cj and not (ci & cj):
                        continue
                    inter = fi & fj
                    if len(inter) < 3:
                        continue
                    if len(inter) / min(len(fi), len(fj)) <= 0.4:
                        continue

                    if len(fi | fj) > 20:
                        continue
                    old_i, old_j = merged[i]["name"], merged[j]["name"]
                    new_name = _pick_name(old_i, old_j, fi, fj)
                    merged[i] = _merge_two(merged[i], merged[j])
                    merged[i]["name"] = new_name
                    merged[j] = None
                    if old_i != new_name:
                        rename_map[old_i] = new_name
                    if old_j != new_name:
                        rename_map[old_j] = new_name
                    changed = True
                    break
                if changed:
                    break
            merged = [s for s in merged if s is not None]


        MAX_ENTITIES = 26
        if len(merged) > MAX_ENTITIES:

            rel_referenced: set = set()
            for rel in content.entity_relationships:
                if isinstance(rel, dict):
                    for key in ("from", "to", "source", "target", "entity", "parent", "child"):
                        val = rel.get(key, "")
                        if val:
                            rel_referenced.add(val)

            candidates = [
                s for s in merged
                if isinstance(s, dict)
                and s.get("data_pre_generation_num") == "none"
                and s.get("schema_role") not in {"task_state", "runtime_generated", "selectable_option"}
                and s.get("name", "") not in rel_referenced
            ]

            candidates.sort(key=lambda s: s.get("observed_item_count", 0))
            pruned_names: list = []
            candidate_set = {id(s) for s in candidates}
            for s in candidates:
                if len(merged) <= MAX_ENTITIES:
                    break
                merged = [e for e in merged if id(e) != id(s)]
                pruned_names.append(s.get("name", "?"))
            if pruned_names:
                self.logger.log_warning(
                    f"[Schema] Entity count cap: pruned {len(pruned_names)} runtime entities "
                    f"to stay within {MAX_ENTITIES}: {pruned_names}"
                )


        if rename_map:
            def _resolve(name):
                visited = set()
                while name in rename_map and name not in visited:
                    visited.add(name)
                    name = rename_map[name]
                return name

            for rel in content.entity_relationships:
                if not isinstance(rel, dict):
                    continue
                for key in ("from", "to", "source", "target", "entity", "parent", "child"):
                    if rel.get(key) in rename_map:
                        rel[key] = _resolve(rel[key])

            new_samples: Dict[str, list] = {}
            for entity_name, items in content.content_samples.items():
                resolved = _resolve(entity_name)
                new_samples.setdefault(resolved, []).extend(items)
            content.content_samples = new_samples

            new_constraints: Dict[str, Dict] = {}
            for entity_name, cdata in content.content_constraints.items():
                resolved = _resolve(entity_name)
                if resolved not in new_constraints:
                    new_constraints[resolved] = cdata
                else:
                    for k, v in cdata.items():
                        if k not in new_constraints[resolved]:
                            new_constraints[resolved][k] = v
            content.content_constraints = new_constraints


        final_groups: Dict[str, List[Dict[str, Any]]] = {}
        for schema in merged:
            if not isinstance(schema, dict) or not schema.get("name"):
                continue
            final_groups.setdefault(self._normalize_entity_name(str(schema.get("name", ""))), []).append(schema)
        if any(len(group) > 1 for group in final_groups.values()):
            collapsed: List[Dict[str, Any]] = []
            for group in final_groups.values():
                base = dict(group[0])
                for extra in group[1:]:
                    base = _merge_two(base, extra)
                    if base.get("data_pre_generation_num") == "none" and extra.get("data_pre_generation_num") == "few":
                        base["data_pre_generation_num"] = "few"
                collapsed.append(base)
            merged = collapsed

        content.entity_schemas = validate_and_fix_types(merged)


        valid_names = {s["name"] for s in merged}
        content_sample_only_names = {
            str(decision.get("projected_to", "") or decision.get("candidate", "")).strip()
            for decision in content.schema_candidate_projection or []
            if isinstance(decision, dict)
            and decision.get("destination") == "content_sample_only"
            and str(decision.get("projected_to", "") or decision.get("candidate", "")).strip()
        }
        allowed_sample_names = valid_names | content_sample_only_names

        new_samples: Dict[str, list] = {}
        for key, items in content.content_samples.items():
            if key in allowed_sample_names:
                existing = new_samples.setdefault(key, [])
                existing.extend(items)
            else:
                if self.logger:
                    self.logger.log_info(
                        f"[Schema] Dropped orphan entity '{key}' ({len(items)} items, no projection-approved schema)"
                    )
        content.content_samples = new_samples


        content.content_constraints = {
            k: v for k, v in content.content_constraints.items()
            if k in allowed_sample_names
        }


        content.entity_relationships = [
            r for r in content.entity_relationships
            if all(
                r.get(k) in valid_names
                for k in ("from", "to", "source", "target", "entity", "parent", "child")
                if r.get(k)
            )
        ]


        content.entity_relationships = dedup_relationships(content.entity_relationships)
        content.entity_relationships = remove_self_references(content.entity_relationships)


        content.content_samples = normalize_content_sample_fields(content.content_samples)


        for schema in merged:
            fields = schema.get("fields", [])
            if len(fields) > 20:
                self.logger.log_warning(
                    f"[Schema] Pruning entity {schema.get('name', '?')} from {len(fields)} to 20 fields"
                )
                schema["fields"] = fields[:20]

        return content


    _NUMBER_NAME_RE = re.compile(
        r'(price|amount|count|quantity|total|rating|score|sales|distance|'
        r'kcal|calories|weight|height|width|num|percent|age|year|month|day|'
        r'cost|fee|discount|balance|stock|capacity|duration|size|area|'
        r'longitude|latitude|order_count|review_count|star)', re.IGNORECASE
    )
    _BOOLEAN_NAME_RE = re.compile(
        r'^(is_|has_|can_|allow|enable|active|available|visible|selected|'
        r'checked|required|featured|verified|open|closed|free)', re.IGNORECASE
    )
    _URL_NAME_RE = re.compile(
        r'(url|link|href|image|photo|icon|avatar|logo|banner|thumbnail|'
        r'src|poster|cover|pic|img)', re.IGNORECASE
    )
    _DATE_NAME_RE = re.compile(
        r'(date|time|timestamp|created|updated|expired|deadline|_at$|'
        r'start_time|end_time|open_time|close_time|business_hours)', re.IGNORECASE
    )

    @staticmethod
    def _is_numeric_value(s: str) -> bool:
        s = s.strip().lstrip('\u00a5$\u20ac\u00a3\uffe5').replace(',', '').rstrip('+')
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_url_value(s: str) -> bool:
        s = str(s).strip()
        return s.startswith(('http://', 'https://', '//', 'data:image'))

    def _coerce_field_types(self, schemas: List[Dict[str, Any]]) -> None:
        for schema in schemas:
            for fld in schema.get("fields", []):
                if not isinstance(fld, dict):
                    continue
                name = fld.get("name", "")
                current_type = fld.get("type", "string")
                if current_type != "string":
                    continue


                if self._BOOLEAN_NAME_RE.search(name):
                    fld["type"] = "boolean"
                elif self._URL_NAME_RE.search(name):
                    fld["type"] = "url"
                elif self._DATE_NAME_RE.search(name):
                    fld["type"] = "date"
                elif self._NUMBER_NAME_RE.search(name):
                    fld["type"] = "number"
                else:

                    examples = [
                        str(e) for e in fld.get("observed_examples", [])
                        if e is not None and str(e).strip()
                    ]
                    if examples:
                        if all(self._is_numeric_value(e) for e in examples):
                            fld["type"] = "number"
                        elif all(self._is_url_value(e) for e in examples):
                            fld["type"] = "url"
                        elif all(e.lower() in ("true", "false", "yes", "no", "1", "0") for e in examples):
                            fld["type"] = "boolean"

    def _build_routing_context(self, canonical_pages: List[Dict]) -> str:
        lines = []
        for page in canonical_pages:
            if not isinstance(page, dict):
                continue
            page_type = page.get("page_type_hint", "")
            page_name = page.get("page_name", "") or page.get("page_cluster_id", "")
            desc = (page.get("description", "") or "")[:80]
            entry = f"- {page_name} ({page_type})"
            if desc:
                entry += f": {desc}"
            lines.append(entry)
        return "\n".join(lines) if lines else "No page routing information available."

    def _build_global_structure_context(
        self,
        cluster_structures: Dict[str, Dict[str, Any]],
        page_info_map: Dict[str, Dict],
        cluster_content_obs: Optional[Mapping[str, Any]] = None,
    ) -> str:
        lines = []
        for cluster_key, structure in cluster_structures.items():
            if not isinstance(structure, dict) or not structure:
                continue
            meta = page_info_map.get(cluster_key, {})
            page_name = meta.get("page_name", "") or cluster_key
            page_type = meta.get("page_type_hint", "")


            layout_info = structure.get("layout_structure", {})
            layout_type = ""
            section_count = ""
            if isinstance(layout_info, dict):
                layout_type = str(layout_info.get("structure", "")).strip()
                section_count = str(layout_info.get("section_count", "")).strip()


            components = structure.get("components", [])
            comp_parts = []
            if isinstance(components, list):
                for c in components:
                    if not isinstance(c, dict):
                        continue
                    ctype = str(c.get("type", "")).strip()
                    if not ctype:
                        continue
                    is_transient = c.get("is_transient", False)
                    if is_transient:
                        continue
                    item_count = c.get("item_count", 0)
                    entry = ctype
                    if item_count and isinstance(item_count, (int, float)) and item_count > 1:
                        entry += f"({int(item_count)})"
                    comp_parts.append(entry)


            relationships = structure.get("component_relationships", [])
            rel_parts = []
            if isinstance(relationships, list):
                for r in relationships:
                    if not isinstance(r, dict):
                        continue
                    source = str(r.get("source", "")).strip()
                    target = str(r.get("target", "")).strip()
                    rtype = str(r.get("relationship_type", "")).strip()
                    if source and target and rtype:
                        rel_parts.append(f"{source}->{target} ({rtype})")


            header = f"Page \"{page_name}\" ({page_type}):" if page_type else f"Page \"{page_name}\":"
            lines.append(header)
            if layout_type or section_count:
                layout_desc = layout_type or "unknown"
                if section_count:
                    layout_desc += f", {section_count} sections"
                lines.append(f"  Layout: {layout_desc}")
            if comp_parts:
                lines.append(f"  Components: {', '.join(comp_parts[:15])}")
            if rel_parts:
                lines.append(f"  Relationships: {'; '.join(rel_parts[:5])}")

            vlt = str(structure.get("visual_layout_text", "")).strip()
            if vlt:

                vlt_summary = vlt[:300].rstrip()
                if len(vlt) > 300:
                    vlt_summary += " ..."
                lines.append(f"  Spatial layout: {vlt_summary}")
            if not comp_parts and not layout_type and not vlt:
                lines.append("  (no structure extracted)")

        if lines:
            return "\n".join(lines)
        return self._build_content_surface_structure_context(
            cluster_content_obs or {},
            page_info_map,
        )

    def _build_content_surface_structure_context(
        self,
        cluster_content_obs: Mapping[str, Any],
        page_info_map: Dict[str, Dict],
    ) -> str:

        lines: List[str] = []
        for cluster_key, content_obs in (cluster_content_obs or {}).items():
            if not isinstance(content_obs, Mapping):
                continue
            extracted_items = content_obs.get("extracted_items", {})
            if not isinstance(extracted_items, Mapping) or not extracted_items:
                continue
            meta = page_info_map.get(cluster_key, {}) if isinstance(page_info_map, dict) else {}
            page_name = str(meta.get("page_name", "") or cluster_key).strip()
            page_type = str(meta.get("page_type_hint", "") or "").strip()
            component_by_entity: Dict[str, str] = {}
            for mapping in content_obs.get("entity_component_mapping", []) or []:
                if not isinstance(mapping, Mapping):
                    continue
                entity = str(mapping.get("entity", "") or "").strip()
                component = str(mapping.get("source_component", "") or "").strip()
                if entity and component:
                    component_by_entity[entity] = component

            entity_parts: List[str] = []
            for entity_name, items in extracted_items.items():
                if not isinstance(items, list) or not items:
                    continue
                samples: List[str] = []
                for item in items[:3]:
                    if isinstance(item, Mapping):
                        sample = (
                            item.get("name")
                            or item.get("label")
                            or item.get("title")
                            or item.get("value")
                            or item.get("text")
                        )
                    else:
                        sample = item
                    sample_text = str(sample or "").strip()
                    if sample_text and sample_text not in samples:
                        samples.append(sample_text[:40])
                component = component_by_entity.get(str(entity_name), "")
                part = f"{entity_name}({len(items)})"
                if component:
                    part += f" via {component}"
                if samples:
                    part += f" samples={samples}"
                entity_parts.append(part)
            if not entity_parts:
                continue
            header = f"Page \"{page_name}\" ({page_type}):" if page_type else f"Page \"{page_name}\":"
            lines.append(header)
            lines.append("  Schema-relevant surfaces: " + "; ".join(entity_parts[:10]))
        return "\n".join(lines)

    def _build_content_summary(
        self,
        cluster_content_obs: Dict[str, Dict[str, Any]],
        page_info_map: Dict[str, Dict],
    ) -> str:
        entity_pages: Dict[str, list] = {}

        for cluster_key, content_obs in cluster_content_obs.items():
            if not isinstance(content_obs, dict):
                continue
            meta = page_info_map.get(cluster_key, {})
            page_name = meta.get("page_name", "") or cluster_key

            extracted_items = content_obs.get("extracted_items", {})
            if not isinstance(extracted_items, dict):
                continue


            ecm = content_obs.get("entity_component_mapping", [])
            ecm_lookup: Dict[str, str] = {}
            if isinstance(ecm, list):
                for mapping in ecm:
                    if isinstance(mapping, dict):
                        ename = str(mapping.get("entity", "")).strip()
                        comp = str(mapping.get("source_component", "")).strip()
                        if ename and comp:
                            ecm_lookup[ename] = comp

            for entity_name, items in extracted_items.items():
                if not isinstance(items, list):
                    continue
                count = len(items)
                if count == 0:
                    continue
                if entity_name not in entity_pages:
                    entity_pages[entity_name] = []
                entry: Dict[str, Any] = {"page": page_name, "count": count}
                source_comp = ecm_lookup.get(entity_name, "")
                if source_comp:
                    entry["component"] = source_comp
                entity_pages[entity_name].append(entry)

        if not entity_pages:
            return ""

        lines = ["Entities extracted across pages:"]
        sorted_entities = sorted(
            entity_pages.items(),
            key=lambda x: sum(e["count"] for e in x[1]),
            reverse=True,
        )
        for entity_name, pages in sorted_entities[:20]:
            total = sum(e["count"] for e in pages)
            page_details = []
            for p in pages:
                detail = f"{p['page']}"
                if p.get("component"):
                    detail += f"/{p['component']}"
                page_details.append(detail)
            pages_str = ", ".join(page_details[:5])
            lines.append(f"  {entity_name}: {total} items (from: {pages_str})")

        multi_page_entities = [
            (name, pages) for name, pages in sorted_entities
            if len(pages) > 1
        ]
        if multi_page_entities:
            lines.append("")
            lines.append("Cross-page patterns:")
            for name, pages in multi_page_entities[:5]:
                lines.append(f"  {name} appears on {len(pages)} pages")

        return "\n".join(lines)

    def _aggregate_content_samples(
        self,
        cluster_content_obs: Dict[str, Dict[str, Any]],
    ) -> Dict[str, List[Dict]]:
        entity_groups: Dict[str, Dict[str, Any]] = {}
        for cluster_key, obs in cluster_content_obs.items():
            for entity, items in (obs.get("extracted_items") or {}).items():
                if not isinstance(items, list):
                    continue
                norm = self._normalize_entity_name(entity)
                if norm not in entity_groups:
                    entity_groups[norm] = {"display": entity, "items": []}
                group = entity_groups[norm]
                for item in items:
                    if isinstance(item, dict):
                        stamped = dict(item)
                        stamped["_source_cluster_id"] = cluster_key
                        group["items"].append(stamped)
        return {
            group["display"]: group["items"]
            for group in entity_groups.values()
        }

    def _aggregate_runtime_state_samples(
        self,
        cluster_content_obs: Dict[str, Dict[str, Any]],
    ) -> Dict[str, List[Dict]]:
        entity_groups: Dict[str, Dict[str, Any]] = {}
        for obs in cluster_content_obs.values():
            for entity, items in (obs.get("runtime_items") or {}).items():
                if not isinstance(items, list):
                    continue
                norm = self._normalize_entity_name(entity)
                if norm not in entity_groups:
                    entity_groups[norm] = {"display": entity, "items": []}
                group = entity_groups[norm]
                for item in items:
                    if isinstance(item, dict):
                        group["items"].append(item)
        return {
            group["display"]: group["items"]
            for group in entity_groups.values()
        }

    _CONSTRAINT_KEYWORDS = re.compile(
        r"rule|limit|constraint|restrict|max_select|min_select|selectable|"
        r"availability|stock|capacity|quota|allow|forbidden|duplicate|repeat",
        re.IGNORECASE,
    )

    _ID_FIELD_RE = re.compile(r"(?:_id|_key|_code)$|^id$", re.IGNORECASE)
    _TEMPORAL_RE = re.compile(
        r"\d{1,2}:\d{2}|\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[-/]\d{1,2}"
    )
    _BOOLEAN_TRUTHY = {"true", "1", "yes"}
    _BOOLEAN_FALSY = {"false", "0", "no"}
    _BOOLEAN_ALL = _BOOLEAN_TRUTHY | _BOOLEAN_FALSY

    @staticmethod
    def _try_parse_numeric(value) -> Optional[float]:
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _extract_content_constraints(
        self,
        content_samples: Dict[str, List[Dict]],
    ) -> Dict[str, Dict]:
        constraints: Dict[str, Dict] = {}
        for entity_name, items in content_samples.items():
            if not items:
                continue

            constraint_fields: Dict[str, set] = {}
            for item in items:
                for field_name, value in item.items():
                    if value is None or value == "":
                        continue
                    if self._CONSTRAINT_KEYWORDS.search(field_name):
                        constraint_fields.setdefault(field_name, set()).add(str(value))
            entity_constraints: Dict[str, Dict] = {}
            if constraint_fields:
                for field_name, values in constraint_fields.items():
                    entity_constraints[field_name] = {
                        "type": "enum",
                        "values": sorted(values),
                    }


            field_values: Dict[str, List] = {}
            for item in items:
                for field_name, value in item.items():
                    if value is None or value == "":
                        continue
                    field_values.setdefault(field_name, []).append(value)

            for field_name, values in field_values.items():

                if field_name in constraint_fields:
                    continue

                if self._ID_FIELD_RE.search(field_name):
                    continue

                if len(values) < 3:
                    continue


                numeric_vals = [self._try_parse_numeric(v) for v in values]
                if all(n is not None for n in numeric_vals):
                    min_val = min(numeric_vals)
                    max_val = max(numeric_vals)
                    if min_val != max_val:
                        entity_constraints[field_name] = {
                            "type": "range",
                            "min": min_val,
                            "max": max_val,
                            "sample_count": len(values),
                        }
                    continue


                str_vals_lower = [str(v).lower() for v in values]
                if all(sv in self._BOOLEAN_ALL for sv in str_vals_lower):
                    true_count = sum(
                        1 for sv in str_vals_lower if sv in self._BOOLEAN_TRUTHY
                    )
                    entity_constraints[field_name] = {
                        "type": "boolean",
                        "true_ratio": true_count / len(str_vals_lower),
                    }
                    continue


                str_values = [str(v) for v in values]
                unique = set(str_values)
                if 2 <= len(unique) <= 10:
                    entity_constraints[field_name] = {
                        "type": "enum",
                        "values": sorted(unique),
                    }
                    continue


                temporal_matches = sum(
                    1 for v in values if self._TEMPORAL_RE.search(str(v))
                )
                if temporal_matches >= len(values) * 0.5:
                    entity_constraints[field_name] = {
                        "type": "temporal",
                        "sample_values": [str(v) for v in values[:3]],
                    }

            if entity_constraints:
                constraints[entity_name] = entity_constraints
        return constraints

    @staticmethod
    def _normalize_entity_name(name: str) -> str:
        n = name.strip().lower()
        if n.endswith("s") and len(n) > 2 and not n.endswith("ss"):
            n = n[:-1]
        return n

    @staticmethod
    def _item_dedup_key(item: Dict[str, Any]) -> str:
        for key in ("name", "title", "product_name", "item_name", "id", "label"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()

        for val in item.values():
            if isinstance(val, str) and val.strip():
                return val.strip().lower()

        return json.dumps(item, ensure_ascii=False, sort_keys=True)

    def _build_site_graph_from_observations(
        self,
        traces: List[TraceData],
        obs_to_page_id: Dict[str, str],
        obs_map: Optional[Dict[str, FrameObservation]] = None,
    ) -> List[GraphEdge]:
        edges: List[GraphEdge] = []
        seen: Dict[Tuple[str, str, str], GraphEdge] = {}
        obs_map = obs_map or {}
        for trace in traces:
            trace_steps = sorted(trace.steps, key=lambda s: s.step_index)
            for i in range(len(trace_steps) - 1):
                curr = trace_steps[i]
                nxt = trace_steps[i + 1]
                from_id = obs_to_page_id.get(f"{trace.trace_id}:{curr.step_index}", "")
                to_id = obs_to_page_id.get(f"{trace.trace_id}:{nxt.step_index}", "")
                if not from_id or not to_id or from_id == to_id:
                    continue
                if self._is_same_page_observation(obs_map.get(f"{trace.trace_id}:{nxt.step_index}")):
                    continue
                trigger = curr.action or "navigate"
                key = (from_id, to_id, trigger)
                step_ref = f"{trace.trace_id}:{curr.step_index}"
                if key not in seen:
                    seen[key] = GraphEdge(
                        from_page_id=from_id,
                        to_page_id=to_id,
                        trigger=trigger,
                        params={},
                        observed_in_trace=trace.trace_id,
                        step_index=curr.step_index,
                        evidence_count=1,
                        evidence_steps=[step_ref],
                        confidence=0.5
                    )
                else:
                    seen[key].evidence_count += 1
                    if step_ref not in seen[key].evidence_steps:
                        seen[key].evidence_steps.append(step_ref)
        edges.extend(seen.values())
        return sorted(edges, key=lambda e: (e.from_page_id, e.to_page_id, e.trigger))

    def _parse_evidence_step_ref(self, value: Any) -> Optional[Tuple[str, int]]:
        if isinstance(value, dict):
            trace_id = str(value.get("trace_id", "")).strip()
            raw_index = value.get("step_index", value.get("step", ""))
            if not trace_id:
                return None
            try:
                return trace_id, int(raw_index)
            except Exception:
                return None
        text = str(value or "").strip()
        if ":" not in text:
            return None
        trace_id, raw_index = text.rsplit(":", 1)
        trace_id = trace_id.strip()
        if not trace_id:
            return None
        raw_index = raw_index.strip()
        if raw_index.startswith("step"):
            raw_index = raw_index[4:]
        try:
            return trace_id, int(raw_index)
        except Exception:
            return None

    def _materialize_canonical_route_by_evidence(
        self,
        route: Dict[str, Any],
        obs_to_page_id: Dict[str, str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        evidence_steps = route.get("evidence_steps", [])
        if not isinstance(evidence_steps, list):
            evidence_steps = []
        trigger = str(route.get("trigger", "navigate")).strip() or "navigate"
        try:
            confidence = float(route.get("confidence", 0.5))
        except Exception:
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        unresolved: List[Dict[str, Any]] = []
        for raw_ref in evidence_steps:
            parsed = self._parse_evidence_step_ref(raw_ref)
            if parsed is None:
                unresolved.append({
                    "evidence_step": str(raw_ref),
                    "reason": "invalid_evidence_step_ref",
                    "route": self._compact_canonical_route_for_debug(route),
                })
                continue
            trace_id, step_index = parsed
            from_key = f"{trace_id}:{step_index}"
            to_key = f"{trace_id}:{step_index + 1}"
            from_page_id = obs_to_page_id.get(from_key, "")
            to_page_id = obs_to_page_id.get(to_key, "")
            if not from_page_id or not to_page_id:
                unresolved.append({
                    "evidence_step": f"{trace_id}:{step_index}",
                    "from_observation_key": from_key,
                    "to_observation_key": to_key,
                    "from_page_id": from_page_id,
                    "to_page_id": to_page_id,
                    "reason": "missing_materialized_observation_mapping",
                    "route": self._compact_canonical_route_for_debug(route),
                })
                continue
            if from_page_id == to_page_id:
                continue
            pair = (from_page_id, to_page_id)
            if pair not in grouped:
                grouped[pair] = {
                    "from_page_id": from_page_id,
                    "to_page_id": to_page_id,
                    "trigger": trigger,
                    "params": {},
                    "observed_in_trace": trace_id,
                    "step_index": step_index,
                    "evidence_count": 0,
                    "evidence_steps": [],
                    "confidence": confidence,
                    "source_canonical_route": self._compact_canonical_route_for_debug(route),
                }
            entry = grouped[pair]
            ref = f"{trace_id}:{step_index}"
            entry["evidence_count"] = int(entry.get("evidence_count") or 0) + 1
            if ref not in entry["evidence_steps"]:
                entry["evidence_steps"].append(ref)
            entry["confidence"] = max(float(entry.get("confidence") or 0.0), confidence)
        return list(grouped.values()), unresolved

    def _compact_canonical_route_for_debug(self, route: Dict[str, Any]) -> Dict[str, Any]:
        evidence_steps = route.get("evidence_steps", [])
        if not isinstance(evidence_steps, list):
            evidence_steps = []
        return {
            "from_page_cluster_id": str(route.get("from_page_cluster_id", "")).strip(),
            "to_page_cluster_id": str(route.get("to_page_cluster_id", "")).strip(),
            "trigger": str(route.get("trigger", "") or ""),
            "evidence_steps": [str(step) for step in evidence_steps if str(step).strip()],
            "confidence": route.get("confidence", 0.5),
        }

    def _admit_canonical_routes_with_materialization(
        self,
        canonical_routes: List[Dict[str, Any]],
        obs_to_page_id: Dict[str, str],
        split_clusters: set,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        split_cluster_ids = {
            str(cluster or "").strip()
            for cluster in split_clusters
            if str(cluster or "").strip()
        }
        if not canonical_routes:
            return [], [], [], []
        cluster_routes: List[Dict[str, Any]] = []
        materialized_routes: List[Dict[str, Any]] = []
        unresolved_steps: List[Dict[str, Any]] = []
        for route in canonical_routes:
            if not isinstance(route, dict):
                continue
            from_cluster = str(route.get("from_page_cluster_id", "")).strip()
            to_cluster = str(route.get("to_page_cluster_id", "")).strip()
            if not ({from_cluster, to_cluster} & split_cluster_ids):
                cluster_routes.append(route)
                continue
            route_materialized, route_unresolved = self._materialize_canonical_route_by_evidence(
                route,
                obs_to_page_id,
            )
            unresolved_steps.extend(route_unresolved)
            if route_materialized:
                materialized_routes.extend(route_materialized)
            else:
                cluster_routes.append(route)

        conflicts = self._detect_materialized_route_conflicts(materialized_routes)
        return cluster_routes, materialized_routes, conflicts, unresolved_steps

    def _detect_materialized_route_conflicts(
        self,
        materialized_routes: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        by_from_trigger: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = defaultdict(dict)
        trigger_metadata: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for route in materialized_routes:
            if not isinstance(route, dict):
                continue
            from_page_id = str(route.get("from_page_id", "")).strip()
            to_page_id = str(route.get("to_page_id", "")).strip()
            trigger = str(route.get("trigger", "") or "navigate").strip() or "navigate"
            if not from_page_id or not to_page_id:
                continue
            group_key = (from_page_id, trigger)
            meta = trigger_metadata.setdefault(group_key, {
                "trigger_key": trigger,
                "trigger_key_reason": "canonical_trigger_text",
                "canonical_triggers": [],
            })
            if trigger not in meta["canonical_triggers"]:
                meta["canonical_triggers"].append(trigger)
            targets = by_from_trigger[group_key]
            target_entry = targets.setdefault(to_page_id, {
                "to_page_id": to_page_id,
                "evidence_steps": [],
                "source_canonical_routes": [],
                "canonical_triggers": [],
            })
            if trigger not in target_entry["canonical_triggers"]:
                target_entry["canonical_triggers"].append(trigger)
            for step in route.get("evidence_steps", []) if isinstance(route.get("evidence_steps", []), list) else []:
                step_text = str(step).strip()
                if step_text and step_text not in target_entry["evidence_steps"]:
                    target_entry["evidence_steps"].append(step_text)
            source_route = route.get("source_canonical_route", {})
            if isinstance(source_route, dict) and source_route not in target_entry["source_canonical_routes"]:
                target_entry["source_canonical_routes"].append(source_route)

        conflicts: List[Dict[str, Any]] = []
        for (from_page_id, trigger_key), targets in sorted(by_from_trigger.items()):
            if len(targets) <= 1:
                continue
            meta = trigger_metadata.get((from_page_id, trigger_key), {})
            conflicts.append({
                "from_materialized_page_id": from_page_id,
                "canonical_trigger": trigger_key,
                "trigger_key": trigger_key,
                "trigger_key_reason": str(meta.get("trigger_key_reason") or ""),
                "canonical_triggers": sorted(str(item) for item in meta.get("canonical_triggers", []) if str(item)),
                "target_count": len(targets),
                "target_page_ids": sorted(targets),
                "evidence_by_target": [
                    targets[target_id]
                    for target_id in sorted(targets)
                ],
                "conflict_type": "same_materialized_source_and_trigger_multi_target",
            })
        return conflicts

    def _build_site_graph_from_canonical_and_materialized_routes(
        self,
        cluster_routes: List[Dict[str, Any]],
        materialized_routes: List[Dict[str, Any]],
        cluster_to_page_id: Dict[str, str],
    ) -> List[GraphEdge]:
        merged: Dict[Tuple[str, str, str], GraphEdge] = {
            (edge.from_page_id, edge.to_page_id, edge.trigger): edge
            for edge in self._build_site_graph_from_canonical_routes(cluster_routes, cluster_to_page_id)
        }
        for route in materialized_routes:
            if not isinstance(route, dict):
                continue
            from_id = str(route.get("from_page_id", "")).strip()
            to_id = str(route.get("to_page_id", "")).strip()
            if not from_id or not to_id or from_id == to_id:
                continue
            trigger = str(route.get("trigger", "navigate")).strip() or "navigate"
            trigger_lower = trigger.lower()
            if any(token in trigger_lower for token in ("swipe", "scroll", "drag", "上滑", "下滑", "滑动", "滚动")):
                continue
            evidence_steps = route.get("evidence_steps", [])
            if not isinstance(evidence_steps, list):
                evidence_steps = []
            evidence_steps = [str(s) for s in evidence_steps if str(s).strip()]
            try:
                confidence = float(route.get("confidence", 0.5))
            except Exception:
                confidence = 0.5
            route_key = (from_id, to_id, trigger)
            if route_key not in merged:
                try:
                    step_index = int(route.get("step_index", -1) or -1)
                except Exception:
                    step_index = -1
                merged[route_key] = GraphEdge(
                    from_page_id=from_id,
                    to_page_id=to_id,
                    trigger=trigger,
                    params={},
                    observed_in_trace=str(route.get("observed_in_trace", "") or ""),
                    step_index=step_index,
                    evidence_count=max(1, len(evidence_steps)),
                    evidence_steps=evidence_steps,
                    confidence=max(0.0, min(1.0, confidence)),
                )
                continue
            edge = merged[route_key]
            edge.evidence_count += max(1, len(evidence_steps))
            for step in evidence_steps:
                if step not in edge.evidence_steps:
                    edge.evidence_steps.append(step)
            edge.confidence = max(edge.confidence, max(0.0, min(1.0, confidence)))
        return sorted(merged.values(), key=lambda e: (e.from_page_id, e.to_page_id, e.trigger))

    def _build_site_graph_from_canonical_routes(
        self,
        canonical_routes: List[Dict[str, Any]],
        cluster_to_page_id: Dict[str, str]
    ) -> List[GraphEdge]:
        if not canonical_routes:
            return []
        merged: Dict[Tuple[str, str, str], GraphEdge] = {}
        for route in canonical_routes:
            if not isinstance(route, dict):
                continue
            from_cluster = str(route.get("from_page_cluster_id", "")).strip()
            to_cluster = str(route.get("to_page_cluster_id", "")).strip()
            from_id = cluster_to_page_id.get(from_cluster, from_cluster if from_cluster.startswith("p_") else "")
            to_id = cluster_to_page_id.get(to_cluster, to_cluster if to_cluster.startswith("p_") else "")
            if not from_id or not to_id or from_id == to_id:
                continue
            trigger = str(route.get("trigger", "navigate")).strip() or "navigate"
            trigger_lower = trigger.lower()
            if any(token in trigger_lower for token in ("swipe", "scroll", "drag", "上滑", "下滑", "滑动", "滚动")):
                continue
            evidence_steps = route.get("evidence_steps", [])
            if not isinstance(evidence_steps, list):
                evidence_steps = []
            evidence_steps = [str(s) for s in evidence_steps if str(s).strip()]
            try:
                confidence = float(route.get("confidence", 0.5))
            except Exception:
                confidence = 0.5
            route_key = (from_id, to_id, trigger)
            if route_key not in merged:
                merged[route_key] = GraphEdge(
                    from_page_id=from_id,
                    to_page_id=to_id,
                    trigger=trigger,
                    params={},
                    observed_in_trace="",
                    step_index=-1,
                    evidence_count=max(1, len(evidence_steps)),
                    evidence_steps=evidence_steps,
                    confidence=max(0.0, min(1.0, confidence))
                )
            else:
                edge = merged[route_key]
                edge.evidence_count += max(1, len(evidence_steps))
                for step in evidence_steps:
                    if step not in edge.evidence_steps:
                        edge.evidence_steps.append(step)
                edge.confidence = max(edge.confidence, max(0.0, min(1.0, confidence)))
        return sorted(merged.values(), key=lambda e: (e.from_page_id, e.to_page_id, e.trigger))

    def _build_site_graph_nodes_and_hierarchy(
        self,
        page_prototypes: List[PagePrototype]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        parent_children: Dict[str, List[str]] = {}
        route_to_page: Dict[str, str] = {}
        route_pattern_to_page_key: Dict[str, str] = {}

        for page in page_prototypes:
            structure_data = page.structure_data if isinstance(page.structure_data, dict) else {}
            route_path = str(structure_data.get("route_path", "")).strip() or f"/page/{page.page_id}"
            route_pattern = self._normalize_route_pattern(route_path)
            parent_route_path = str(structure_data.get("parent_route_path", "")).strip()
            try:
                route_level = int(structure_data.get("route_level", 0))
            except Exception:
                route_level = 0
            page_name_full = page.page_name_full or page.page_name
            page_key = self._build_page_key(route_pattern, page_name_full)
            node = {
                "page_id": page.page_id,
                "page_name": page.page_name,
                "page_name_full": page_name_full,
                "page_type": page.page_type,
                "route_path": route_path,
                "route_pattern": route_pattern,
                "page_key": page_key,
                "parent_route_path": parent_route_path,
                "route_level": max(0, route_level)
            }
            nodes.append(node)
            route_to_page[route_path] = page.page_id
            route_pattern_to_page_key[route_pattern] = page_key
            if parent_route_path:
                parent_children.setdefault(parent_route_path, []).append(route_path)
            else:
                parent_children.setdefault("__root__", []).append(route_path)

        route_hierarchy = {
            "root_routes": sorted(set(parent_children.get("__root__", []))),
            "by_parent_route": {
                parent: sorted(set(children))
                for parent, children in parent_children.items()
                if parent != "__root__"
            },
            "route_to_page_id": route_to_page,
            "route_pattern_to_page_key": route_pattern_to_page_key,
            "node_primary_key_format": "route_pattern::page_name"
        }
        return nodes, route_hierarchy

    def _enrich_edges_with_page_context(
        self,
        edges: List[GraphEdge],
        nodes: List[Dict[str, Any]]
    ) -> List[GraphEdge]:
        page_map = {
            str(n.get("page_id", "")): n
            for n in nodes
            if isinstance(n, dict) and str(n.get("page_id", "")).strip()
        }
        for edge in edges:
            from_node = page_map.get(edge.from_page_id, {})
            to_node = page_map.get(edge.to_page_id, {})
            edge.from_page_name = str(from_node.get("page_name_full", from_node.get("page_name", ""))).strip()
            edge.to_page_name = str(to_node.get("page_name_full", to_node.get("page_name", ""))).strip()
            edge.from_route_path = str(from_node.get("route_path", "")).strip()
            edge.to_route_path = str(to_node.get("route_path", "")).strip()
            edge.from_route_pattern = str(from_node.get("route_pattern", "")).strip()
            edge.to_route_pattern = str(to_node.get("route_pattern", "")).strip()
            edge.from_page_key = str(from_node.get("page_key", "")).strip()
            edge.to_page_key = str(to_node.get("page_key", "")).strip()
        return edges

    def _normalize_route_pattern(self, route_path: str) -> str:
        path = str(route_path or "").strip()
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        path = path.replace("{", ":").replace("}", "")
        while "//" in path:
            path = path.replace("//", "/")
        return path

    def _build_page_key(self, route_pattern: str, page_name: str) -> str:
        route = self._normalize_route_pattern(route_pattern)
        name = str(page_name or "").strip() or "Unnamed Page"
        return f"{route}::{name}"

    def _build_route_tree(
        self,
        page_prototypes: List[PagePrototype],
        website_url: str
    ) -> Dict[str, Any]:
        parsed_site = urlparse(website_url or "")
        site_host = parsed_site.netloc or (website_url or "")
        root_node: Dict[str, Any] = {"_page_id": "", "children": {}}

        for page in page_prototypes:
            structure_data = page.structure_data if isinstance(page.structure_data, dict) else {}
            route_path = self._normalize_route_pattern(structure_data.get("route_path", "") or f"/page/{page.page_id}")
            parts = [p for p in route_path.split("/") if p]
            current = root_node
            if not parts:
                current["_page_id"] = page.page_id
                current["_page_name"] = page.page_name_full or page.page_name
                continue
            for seg in parts:
                child = current.setdefault("children", {}).setdefault(seg, {"children": {}})
                current = child
            current["_page_id"] = page.page_id
            current["_page_name"] = page.page_name_full or page.page_name

        return {
            "site": site_host,
            "route_tree": {"/": root_node}
        }

    def _build_state_graph(
        self,
        page_prototypes: List[PagePrototype],
        observations: List[FrameObservation],
        traces: List[TraceData],
        obs_to_page_id: Dict[str, str]
    ) -> Dict[str, Any]:
        page_map: Dict[str, PagePrototype] = {p.page_id: p for p in page_prototypes}
        obs_map: Dict[str, FrameObservation] = {self._obs_key(o): o for o in observations}
        pages: Dict[str, Dict[str, Any]] = {}
        sig_to_state_id: Dict[str, Dict[str, str]] = {}

        for page in page_prototypes:
            route_pattern = self._normalize_route_pattern(
                (page.structure_data or {}).get("route_path", "") if isinstance(page.structure_data, dict) else ""
            )
            pages[page.page_id] = {
                "page_id": page.page_id,
                "page_name": page.page_name_full or page.page_name,
                "route_pattern": route_pattern,
                "states": {},
                "transitions": []
            }
            sig_to_state_id[page.page_id] = {}

        def ensure_state(page_id: str, obs: FrameObservation) -> str:
            page_states = pages.setdefault(page_id, {
                "page_id": page_id,
                "page_name": page_map.get(page_id).page_name_full if page_id in page_map else page_id,
                "route_pattern": "/",
                "states": {},
                "transitions": []
            })["states"]
            page_sig_map = sig_to_state_id.setdefault(page_id, {})
            sig = str(obs.structure_signature or obs.content_signature or obs.description or f"{obs.trace_id}:{obs.step_index}").strip()
            if sig in page_sig_map:
                state_id = page_sig_map[sig]
            else:
                state_id = f"S{len(page_sig_map)}"
                page_sig_map[sig] = state_id
                page_states[state_id] = {
                    "sig": sig,
                    "structure_sig": obs.structure_signature,
                    "content_sig": obs.content_signature,
                    "note": obs.description,
                    "viewport": "desktop",
                    "evidence_steps": []
                }
            ref = f"{obs.trace_id}:{obs.step_index}"
            if ref not in page_states[state_id]["evidence_steps"]:
                page_states[state_id]["evidence_steps"].append(ref)
            return state_id

        transition_seen: Dict[str, Dict[Tuple[str, str, str], Dict[str, Any]]] = {}
        for trace in traces:
            steps = sorted(trace.steps, key=lambda s: s.step_index)
            for i in range(len(steps)):
                step = steps[i]
                key = f"{trace.trace_id}:{step.step_index}"
                page_id = obs_to_page_id.get(key, "")
                obs = obs_map.get(key)
                if not page_id or obs is None:
                    continue
                ensure_state(page_id, obs)
            for i in range(len(steps) - 1):
                curr = steps[i]
                nxt = steps[i + 1]
                curr_key = f"{trace.trace_id}:{curr.step_index}"
                nxt_key = f"{trace.trace_id}:{nxt.step_index}"
                page_id = obs_to_page_id.get(curr_key, "")
                next_page_id = obs_to_page_id.get(nxt_key, "")
                curr_obs = obs_map.get(curr_key)
                nxt_obs = obs_map.get(nxt_key)
                if not page_id or page_id != next_page_id or curr_obs is None or nxt_obs is None:
                    continue
                from_state = ensure_state(page_id, curr_obs)
                to_state = ensure_state(page_id, nxt_obs)
                action_text = curr.action or ""
                transition_key = (from_state, to_state, action_text)
                per_page = transition_seen.setdefault(page_id, {})
                ref = f"{trace.trace_id}:{curr.step_index}"
                if transition_key not in per_page:
                    per_page[transition_key] = {
                        "from": from_state,
                        "to": to_state,
                        "action": {
                            "type": "click" if action_text.startswith("click(") else (
                                "type" if action_text.startswith("type(") else (
                                    "scroll" if action_text.startswith("scroll(") else "event"
                                )
                            ),
                            "raw": action_text,
                        },
                        "replayable": True,
                        "evidence_steps": [ref]
                    }
                else:
                    if ref not in per_page[transition_key]["evidence_steps"]:
                        per_page[transition_key]["evidence_steps"].append(ref)

        for page_id, trans in transition_seen.items():
            if page_id in pages:
                pages[page_id]["transitions"] = sorted(
                    list(trans.values()),
                    key=lambda x: (x.get("from", ""), x.get("to", ""), x.get("action", {}).get("raw", ""))
                )

        return {"pages": pages}

    def _aggregate_global_navigation(self, pages: List[PagePrototype]) -> Dict[str, List[Dict[str, Any]]]:
        MAX_GLOBAL_NAV = 8


        header_candidates: Dict[str, Dict[str, Any]] = {}
        footer_links: Dict[str, Dict[str, Any]] = {}


        page_filenames = {p.filename for p in pages if p.filename}

        for page in pages:
            nav = {}
            if isinstance(page.structure_data, dict):
                nav = page.structure_data.get("navigation_elements", {}) or {}
            if not nav and isinstance(page.content_data, dict):
                nav = page.content_data.get("navigation_elements", {}) or {}
            main_nav = nav.get("main_nav", [])
            for item in main_nav if isinstance(main_nav, list) else []:
                if isinstance(item, dict):
                    text = str(item.get("text", "")).strip()
                    href = str(item.get("href_hint", "")).strip()
                else:
                    text = str(item).strip()
                    href = ""
                if not text:
                    continue
                if text in header_candidates:
                    header_candidates[text]["freq"] += 1
                else:
                    header_candidates[text] = {"text": text, "url": href, "freq": 1}
            f_links = nav.get("footer_links", [])
            for item in f_links if isinstance(f_links, list) else []:
                if isinstance(item, dict):
                    text = str(item.get("text") or item.get("section") or "").strip()
                    href = str(item.get("href_hint", "")).strip()
                else:
                    text = str(item).strip()
                    href = ""
                if text and text not in footer_links:
                    footer_links[text] = {"text": text, "url": href}


        num_pages = max(len(pages), 1)
        freq_threshold = max(2, int(num_pages * 0.3))

        app_level = []
        for item in header_candidates.values():
            href = item.get("url", "")
            freq = item.get("freq", 1)

            if not href or href == "#" or href.startswith("#"):
                continue

            if freq >= freq_threshold:
                app_level.append(item)

            elif href in page_filenames:
                app_level.append(item)


        app_level.sort(key=lambda x: x.get("freq", 0), reverse=True)


        filtered_header = [
            {"text": it["text"], "url": it["url"]}
            for it in app_level[:MAX_GLOBAL_NAV]
        ]

        return {
            "header_links": filtered_header,
            "footer_links": list(footer_links.values())
        }

    @staticmethod
    def _extract_route_params(route_pattern: str) -> List[str]:
        import re
        return re.findall(r':(\w+)', route_pattern)

    def _build_capture_actions_from_traces(
        self,
        traces: List,
        step_labels: List[Dict[str, Any]],
        obs_to_page_id: Dict[str, str],
        cluster_to_page_ids: Dict[str, List[str]],
        obs_map: Optional[Dict[str, FrameObservation]] = None,
    ) -> Dict[str, list]:
        from trace2world_pipeline.app_spec.trace_loader import TraceData


        step_to_cluster: Dict[str, str] = {}
        for label in step_labels:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id", "")).strip()
            try:
                step_index = int(label.get("step_index", -1))
            except Exception:
                continue
            cluster_id = str(label.get("page_cluster_id", "")).strip()
            if cluster_id:
                step_to_cluster[f"{trace_id}:{step_index}"] = cluster_id

        capture_actions: Dict[str, list] = {}
        obs_map = obs_map or {}

        def observation_transition(obs: Optional[FrameObservation]) -> tuple[str, Optional[bool]]:
            if not obs:
                return "", None
            transition_kind = str(
                getattr(obs, "transition_kind", "")
                or (
                    obs.page_state.get("transition_kind", "")
                    if isinstance(getattr(obs, "page_state", None), dict)
                    else ""
                )
                or ""
            ).strip()
            transition_from_previous = getattr(obs, "transition_from_previous", None)
            if transition_from_previous is None and isinstance(getattr(obs, "page_state", None), dict):
                transition_from_previous = obs.page_state.get("transition_from_previous")
            if not isinstance(transition_from_previous, bool):
                transition_from_previous = None
            return transition_kind, transition_from_previous

        def transient_handoff_step(
            obs: Optional[FrameObservation],
            action_text: str,
            raw_action: str,
        ) -> bool:
            if not obs:
                return False
            description = str(getattr(obs, "description", "") or "").lower()
            text = f"{action_text} {raw_action}".lower()
            transient_visual = any(
                token in description
                for token in (
                    "black screen",
                    "completely black",
                    "no visible",
                    "transient",
                    "loading screen",
                    "system authorization",
                    "system permission",
                    "authorization prompt",
                    "privacy authorization",
                    "rendering gap",
                )
            ) or any(token in description for token in ("黑屏", "无可见", "授权", "隐私", "指纹", "系统"))
            handoff_action = any(
                token in text
                for token in (
                    "确认",
                    "确定",
                    "允许",
                    "同意",
                    "授权",
                    "隐私",
                    "指纹",
                    "permission",
                    "authorize",
                    "confirm",
                    "allow",
                    "agree",
                )
            )
            return transient_visual and handoff_action

        for trace in traces:
            if not isinstance(trace, TraceData):
                continue

            trace_id = trace.trace_id
            actions = []
            last_page_id = ""

            for step in trace.steps:
                step_index = step.step_index
                step_key = f"{trace_id}:{step_index}"
                page_id = obs_to_page_id.get(step_key, "")
                if not page_id:
                    cluster_id = step_to_cluster.get(step_key, "")
                    candidate_pages = cluster_to_page_ids.get(cluster_id, []) if cluster_id else []
                    page_id = candidate_pages[0] if candidate_pages else ""
                if not page_id:
                    curr_obs_for_assignment = obs_map.get(step_key)
                    action_text_for_assignment = step.action.strip()
                    raw_action_for_assignment = str(getattr(step, "raw_action", "") or "").strip()
                    if last_page_id and transient_handoff_step(
                        curr_obs_for_assignment,
                        action_text_for_assignment,
                        raw_action_for_assignment,
                    ):
                        page_id = last_page_id
                    else:
                        continue


                action_text = step.action.strip()
                raw_action = str(getattr(step, "raw_action", "") or "").strip()
                action_type = "init"
                target_element = ""


                raw_parts = raw_action.split(None, 1) if raw_action else []
                raw_action_type = raw_parts[0].lower() if raw_parts else ""
                if raw_action_type in ("click", "tap"):
                    action_type = "click"
                    target_element = action_text.replace("点击", "").replace("click", "").strip() if action_text else ""
                elif raw_action_type in ("double_click", "dblclick", "press"):
                    action_type = raw_action_type
                    target_element = (
                        action_text.replace("双击", "")
                        .replace("double click", "")
                        .replace("double_click", "")
                        .replace("dblclick", "")
                        .replace("press", "")
                        .strip()
                        if action_text
                        else ""
                    )
                elif raw_action_type in ("type", "input", "input_text"):
                    action_type = "type"
                    target_element = raw_parts[1] if len(raw_parts) > 1 else ""
                elif raw_action_type in ("scroll", "swipe", "drag"):
                    action_type = "scroll"
                elif raw_action_type in ("select",):
                    action_type = "select"
                    target_element = raw_parts[1] if len(raw_parts) > 1 else ""
                elif raw_action_type == "system_button":
                    action_type = "system_button"
                    raw_payload = (
                        step.raw_action_payload
                        if isinstance(getattr(step, "raw_action_payload", None), dict)
                        else {}
                    )
                    target_element = str(
                        raw_payload.get("target_label")
                        or raw_payload.get("text")
                        or (raw_parts[1] if len(raw_parts) > 1 else "")
                        or ""
                    ).strip()
                    if not target_element:
                        target_match = re.search(
                            r"(?:target|button)\s*=\s*([^;,\s]+)",
                            action_text,
                            flags=re.IGNORECASE,
                        )
                        if target_match:
                            target_element = target_match.group(1).strip()

                elif action_text.startswith("点击") or action_text.startswith("click"):
                    action_type = "click"
                    target_element = action_text.replace("点击", "").replace("click", "").strip()
                elif action_text.startswith("双击") or action_text.startswith("double click") or action_text.startswith("double_click"):
                    action_type = "double_click"
                    target_element = (
                        action_text.replace("双击", "")
                        .replace("double click", "")
                        .replace("double_click", "")
                        .strip()
                    )
                elif (
                    action_text.startswith("滚动")
                    or action_text.startswith("scroll")
                    or any(marker in action_text for marker in ("滑动", "上滑", "下滑"))
                ):
                    action_type = "scroll"
                elif action_text.startswith("输入") or action_text.startswith("type") or "输入" in action_text or "键入" in action_text:
                    action_type = "type"
                    target_element = action_text.replace("输入", "").replace("type", "").strip()
                elif action_text.startswith("选择") or action_text.startswith("select"):
                    action_type = "select"
                    target_element = action_text.replace("选择", "").replace("select", "").strip()
                elif re.search(r"\baction_type\s*=\s*system_button\b", action_text, flags=re.IGNORECASE):
                    action_type = "system_button"
                    target_match = re.search(
                        r"(?:target|button)\s*=\s*([^;,\s]+)",
                        action_text,
                        flags=re.IGNORECASE,
                    )
                    if target_match:
                        target_element = target_match.group(1).strip()
                elif step_index == 0:
                    action_type = "init"

                curr_obs = obs_map.get(step_key)
                next_obs = obs_map.get(f"{trace_id}:{step_index + 1}")
                current_transition_kind, current_transition_from_previous = observation_transition(curr_obs)
                next_transition_kind, next_transition_from_previous = observation_transition(next_obs)
                entry = {
                    "step": step_index + 1,
                    "step_index": step_index,
                    "obs_key": step_key,
                    "action": action_type,
                    "page": page_id,
                    "page_cluster_id": step_to_cluster.get(step_key, ""),
                    "description": action_text,
                    "task_instruction": getattr(trace, "task", ""),
                    "thought": getattr(step, "thought", ""),
                    "current_description": getattr(curr_obs, "description", "") if curr_obs else "",
                    "next_description": getattr(next_obs, "description", "") if next_obs else "",
                    "current_transition_kind": current_transition_kind,
                    "next_transition_kind": next_transition_kind,
                    "transition_kind": next_transition_kind or current_transition_kind,
                }
                if current_transition_from_previous is not None:
                    entry["current_transition_from_previous"] = current_transition_from_previous
                if next_transition_from_previous is not None:
                    entry["next_transition_from_previous"] = next_transition_from_previous
                    entry["transition_from_previous"] = next_transition_from_previous

                if target_element:
                    entry["target_element"] = target_element
                if step_key not in obs_to_page_id and page_id == last_page_id:
                    entry["page_assignment_source"] = "previous_executable_page_for_transient_handoff"

                actions.append(entry)
                last_page_id = page_id

            terminal_observations = sorted(
                [
                    item
                    for item in (getattr(trace, "terminal_observations", []) or [])
                    if isinstance(item, dict)
                ],
                key=lambda item: int(item.get("step_index", -1)),
            )
            if actions and terminal_observations:
                last_action = actions[-1]
                last_action_step = int(last_action.get("step_index", -1))
                terminal_observation = next(
                    (
                        item
                        for item in terminal_observations
                        if int(item.get("step_index", -1)) > last_action_step
                    ),
                    terminal_observations[-1],
                )
                terminal_step_index = int(terminal_observation.get("step_index", -1))
                terminal_observation_key = f"{trace_id}:{terminal_step_index}"
                terminal_page_id = str(
                    obs_to_page_id.get(terminal_observation_key, "") or ""
                )
                terminal_obs = obs_map.get(terminal_observation_key)
                terminal_contract = (
                    getattr(trace, "terminal_contract", {})
                    if isinstance(getattr(trace, "terminal_contract", {}), dict)
                    else {}
                )
                terminal_description = str(
                    terminal_contract.get("terminal_boundary_description")
                    or terminal_observation.get("summary")
                    or terminal_observation.get("detail")
                    or terminal_observation.get("status")
                    or ""
                )
                last_action.update({
                    "target_observation_step_index": terminal_step_index,
                    "target_observation_key": terminal_observation_key,
                    "target_observation_role": "terminal_observation",
                    "target_observation_screenshot": str(
                        terminal_observation.get("screenshot_path") or ""
                    ),
                    "terminal_observation": {
                        "step_index": terminal_step_index,
                        "obs_key": terminal_observation_key,
                        "page_id": terminal_page_id,
                        "screenshot_path": str(terminal_observation.get("screenshot_path") or ""),
                        "status": str(terminal_observation.get("status") or ""),
                        "summary": str(terminal_observation.get("summary") or ""),
                        "detail": str(terminal_observation.get("detail") or ""),
                        "semantic_effect": "terminal_evidence",
                    },
                })
                if terminal_page_id:
                    last_action["target_page_id"] = terminal_page_id
                    last_action["target_observation_page_id"] = terminal_page_id
                if terminal_obs and getattr(terminal_obs, "description", ""):
                    last_action["next_description"] = str(terminal_obs.description)
                elif terminal_description:
                    last_action["next_description"] = terminal_description
                last_action["next_transition_kind"] = "terminal_observation"
                last_action["transition_kind"] = "terminal_observation"

            if actions:
                capture_actions[trace_id] = sorted(actions, key=lambda x: x["step"])

        return capture_actions

    def _build_page_behaviors(
        self,
        page_prototypes: List,
        edges: List,
        capture_actions: Dict[str, list],
        global_navigation: Dict[str, Any]
    ) -> Dict[str, Any]:
        page_map = {p.page_id: p for p in page_prototypes}
        page_id_to_filename = {p.page_id: p.filename for p in page_prototypes if p.filename}


        out_edges: Dict[str, List[Dict]] = {}
        in_edges: Dict[str, List[Dict]] = {}
        for e in edges:

            to_route_params = self._extract_route_params(e.to_route_pattern) if e.to_route_pattern else []
            out_entry = {
                "to_page_id": e.to_page_id,
                "to_page": e.to_page_name or e.to_page_id,
                "to_filename": page_id_to_filename.get(e.to_page_id, ""),
                "trigger": e.trigger,
                "to_route": e.to_route_path,
                "to_route_pattern": e.to_route_pattern,
                "params_needed": to_route_params,
            }
            out_edges.setdefault(e.from_page_id, []).append(out_entry)


            in_entry = {
                "from_page_id": e.from_page_id,
                "from_page": e.from_page_name or e.from_page_id,
                "from_filename": page_id_to_filename.get(e.from_page_id, ""),
                "trigger": e.trigger,
            }
            in_edges.setdefault(e.to_page_id, []).append(in_entry)


        actions_by_page: Dict[str, List[Dict]] = {}
        for trace_id, actions in capture_actions.items():
            for act in actions:
                page_id = act.get("page", "")
                if not page_id:
                    continue
                entry = {
                    "trace": trace_id,
                    "step": act.get("step", 0),
                    "action": act.get("action", ""),
                    "description": act.get("description", ""),
                }
                if act.get("target_element"):
                    entry["target_element"] = act["target_element"]
                if act.get("result_page"):
                    entry["result_page"] = act["result_page"]
                for key in (
                    "task_instruction",
                    "current_description",
                    "next_description",
                    "transition_kind",
                    "transition_from_previous",
                    "current_transition_kind",
                    "current_transition_from_previous",
                    "next_transition_kind",
                    "next_transition_from_previous",
                ):
                    if key in act:
                        entry[key] = act.get(key)
                actions_by_page.setdefault(page_id, []).append(entry)

        behaviors: Dict[str, Any] = {}
        for page in page_prototypes:
            pid = page.page_id
            sd = page.structure_data if isinstance(page.structure_data, dict) else {}
            route_pattern = self._normalize_route_pattern(sd.get("route_path", "") or f"/page/{pid}")
            route_params = self._extract_route_params(route_pattern)

            page_actions = actions_by_page.get(pid, [])


            click_set: set = set()
            for act in page_actions:
                target = act.get("target_element", "")
                if target:
                    click_set.add(target)

            nav_elems = sd.get("navigation_elements", {}) if isinstance(sd.get("navigation_elements"), dict) else {}
            for btn in nav_elems.get("cta_buttons", []):
                if isinstance(btn, dict) and btn.get("text"):
                    click_set.add(btn["text"])
            for nav_item in nav_elems.get("main_nav", []):
                if isinstance(nav_item, dict) and nav_item.get("text"):
                    click_set.add(nav_item["text"])

            for ctx_item in nav_elems.get("contextual", []):
                if isinstance(ctx_item, str):
                    ctx_text = ctx_item.strip()
                    if ctx_text and len(ctx_text) <= 15 and not any(c in ctx_text for c in ':：（('):
                        click_set.add(ctx_text)
            click_set.discard("")

            click_set = {
                item for item in click_set
                if len(item) <= 20
                and "\n" not in item
                and not (item[-1:] in (".", "。") and not item.endswith("...") and not item.endswith("…"))
            }
            structured_actions = {
                "click_targets": list(click_set),
                "navigations": [
                    {"to": e["to_page"], "trigger": e["trigger"]}
                    for e in out_edges.get(pid, [])
                    if e.get("trigger")
                ],
            }

            behaviors[pid] = {
                "page_id": pid,
                "page_name": page.page_name_full or page.page_name,
                "route_pattern": route_pattern,
                "route_params": route_params,
                "navigation_out": out_edges.get(pid, []),
                "navigation_in": in_edges.get(pid, []),
                "page_actions": page_actions,
                "structured_actions": structured_actions,
            }
        return behaviors

    @staticmethod
    def _normalize_task_label(text: str) -> str:
        label = str(text or "").strip()
        for suffix in ("按钮", "入口", "链接", "页签", "标签", "tab"):
            if label.endswith(suffix):
                label = label[: -len(suffix)].strip()
        return label.lower().replace("“", "").replace("”", "").replace('"', "").replace("'", "")

    @staticmethod
    def _is_same_page_transition_kind(value: Any) -> bool:
        return str(value or "").strip() in {"same_page_scroll", "same_page_filter", "same_page_other", "overlay"}

    def _is_same_page_capture_action(self, action: Dict[str, Any]) -> bool:
        if not isinstance(action, dict):
            return False
        for key in ("transition_kind", "next_transition_kind", "current_transition_kind"):
            if self._is_same_page_transition_kind(action.get(key)):
                return True
        for key in ("transition_from_previous", "next_transition_from_previous", "current_transition_from_previous"):
            if action.get(key) is False:
                return True
        return False

    def _action_matches_navigation_trigger(self, action: Dict[str, Any], trigger: str) -> bool:
        trigger_label = self._normalize_task_label(trigger)
        if not trigger_label:
            return False
        action_labels = [
            self._normalize_task_label(str(action.get("description", "") or "")),
            self._normalize_task_label(str(action.get("target_element", "") or "")),
        ]
        return any(
            action_label
            and (action_label in trigger_label or trigger_label in action_label)
            for action_label in action_labels
        )

    def _page_has_persistent_nav(self, page) -> bool:
        for src in (getattr(page, "structure_data", None), getattr(page, "content_data", None)):
            if not isinstance(src, dict):
                continue
            nav = src.get("navigation_elements", {})
            if not isinstance(nav, dict):
                continue
            if nav.get("main_nav") or nav.get("footer_links"):
                return True
        return False

    def _resolve_task_nav_target(self, display_text: str, page_prototypes: List) -> Optional[Dict[str, Any]]:
        target_key = self._normalize_task_label(display_text)
        if not target_key:
            return None

        for page in page_prototypes:
            names = [
                getattr(page, "page_name", ""),
                getattr(page, "page_name_full", ""),
            ]
            for name in names:
                if self._normalize_task_label(name) == target_key:
                    return {
                        "page_id": page.page_id,
                        "page_name": page.page_name_full or page.page_name,
                        "filename": page.filename,
                    }
        return None

    def _infer_missing_continuation_kind(self, element: Dict[str, Any]) -> str:
        explicit = str(element.get("continuation_kind", "") or "").strip()
        return explicit or "task_action_candidate"

    @staticmethod
    def _detect_task_state_ref(text: str) -> str:
        return ""

    @staticmethod
    def _stable_short_hash(payload: Dict[str, Any], prefix: str) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return "{}_{}".format(prefix, digest)

    def _l0_schema_registry_flow_enabled(self) -> bool:
        return str(
            getattr(self, "task_interaction_contract_logic_flow", "l0_schema_registry")
            or "l0_schema_registry"
        ).strip().lower() == "l0_schema_registry"

    @staticmethod
    def _l0_state_registry_kind(state: Dict[str, Any]) -> str:
        state_id = str(state.get("state_id", "") or "").lower()
        state_type = str(state.get("state_type", "") or state.get("role", "") or "").lower()
        entity_name = str(state.get("entity_name", "") or state.get("entity_ref", "") or "").lower()
        text = " ".join([state_id, state_type, entity_name])
        if any(token in text for token in ("form", "draft", "input", "doorplate", "query", "searchinput")):
            return "draft_state"
        if any(token in text for token in ("filter", "sort", "drawer", "sheet", "dialog", "inline", "tab")):
            return "ui_state"
        return "runtime_state"

    def _build_l0_schema_registry(self, content: "SiteContent") -> Dict[str, Any]:
        catalog_entities: Dict[str, Dict[str, Any]] = {}
        runtime_states: Dict[str, Dict[str, Any]] = {}
        refs: Dict[str, List[Dict[str, str]]] = {}

        def add_ref(name: str, kind: str, ref: str) -> None:
            key = str(name or "").strip()
            if not key or not ref:
                return
            entry = {"kind": kind, "ref": ref}
            bucket = refs.setdefault(key, [])
            if entry not in bucket:
                bucket.append(entry)

        for schema in content.entity_schemas or []:
            if not isinstance(schema, dict):
                continue
            name = str(schema.get("name", "") or "").strip()
            if not name:
                continue
            fields = []
            for field in schema.get("fields", []) or []:
                if isinstance(field, dict) and str(field.get("name", "") or "").strip():
                    fields.append({
                        "name": str(field.get("name", "") or "").strip(),
                        "type": str(field.get("type", "") or "string"),
                    })
            catalog_entities[name] = {
                "kind": "catalog_entity",
                "name": name,
                "schema_ref": name,
                "storage_key": str(schema.get("storage_key", "") or ""),
                "fields": fields,
            }
            add_ref(name, "catalog_entity", name)

        for state in (content.task_state_contract or {}).get("states", []):
            if not isinstance(state, dict):
                continue
            state_id = str(state.get("state_id", "") or "").strip()
            if not state_id:
                continue
            entity_name = str(state.get("entity_name", "") or state.get("entity_ref", "") or "").strip()
            kind = self._l0_state_registry_kind(state)
            runtime_states[state_id] = {
                "kind": kind,
                "state_id": state_id,
                "entity_name": entity_name,
                "state_type": str(state.get("state_type", "") or ""),
                "observed_on_pages": list(state.get("observed_on_pages", []))
                if isinstance(state.get("observed_on_pages", []), list)
                else [],
                "value_shape": {
                    "state_type": str(state.get("state_type", "") or ""),
                    "entity_name": entity_name,
                    "role": str(state.get("role", "") or ""),
                },
            }
            add_ref(state_id, kind, state_id)
            if entity_name:
                add_ref(entity_name, kind, state_id)

        return {
            "format": "l0_schema_registry",
            "schema_revision": 1,
            "catalog_entity_count": len(catalog_entities),
            "runtime_state_count": len(runtime_states),
            "catalog_entities": catalog_entities,
            "runtime_states": runtime_states,
            "refs": refs,
        }

    @staticmethod
    def _l0_state_ref_slug(value: str) -> str:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or "")).lower()
        text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
        return text or "state"

    def _l0_contract_anchor_slug(self, contract: Dict[str, Any]) -> str:
        candidate = contract.get("candidate", {}) if isinstance(contract.get("candidate", {}), dict) else {}
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        field_contract = contract.get("field_contract", {}) if isinstance(contract.get("field_contract", {}), dict) else {}
        field_parts: List[str] = []
        for key in ("typed_fields", "observed_fields", "unobserved_required_fields"):
            values = field_contract.get(key, [])
            if isinstance(values, list):
                field_parts.extend(str(value) for value in values[:4] if str(value))
        parts = [
            candidate.get("source_page_id"),
            candidate.get("source_filename"),
            candidate.get("page_cluster_id"),
            candidate.get("materialized_page_id"),
            contract.get("action_semantic"),
            contract.get("state_scope"),
            candidate.get("element_name"),
            candidate.get("display_text"),
            hydration.get("source_ref"),
            " ".join(field_parts),
        ]
        seed = " ".join(str(part or "") for part in parts if str(part or "").strip())
        slug = self._l0_state_ref_slug(seed)
        if len(slug) <= 72:
            return slug
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
        return "{}_{}".format(slug[:56].strip("_"), digest)

    @staticmethod
    def _l0_runtime_state_kind_for_scope(state_scope: str, state_ref: str) -> str:
        scope = str(state_scope or "").strip()
        if scope in {"draft", "transaction"} or str(state_ref or "").endswith("_draft"):
            return "draft_state"
        if scope in {"inline_ui", "selection"}:
            return "ui_state"
        return "runtime_state"

    @staticmethod
    def _l0_add_registry_ref(schema_registry: Dict[str, Any], name: str, kind: str, ref: str) -> None:
        key = str(name or "").strip()
        ref_value = str(ref or "").strip()
        if not key or not ref_value:
            return
        entry = {"kind": str(kind or "runtime_state"), "ref": ref_value}
        refs = schema_registry.setdefault("refs", {})
        bucket = refs.setdefault(key, [])
        if entry not in bucket:
            bucket.append(entry)

    def _resolve_l0_structured_identity(
        self,
        contract: Dict[str, Any],
        content: "SiteContent",
    ) -> Dict[str, str]:
        schema_registry = content.schema_registry if isinstance(content.schema_registry, dict) else {}
        if not schema_registry:
            schema_registry = self._build_l0_schema_registry(content)
            content.schema_registry = schema_registry
        refs = schema_registry.get("refs", {}) if isinstance(schema_registry, dict) else {}
        refs = refs if isinstance(refs, dict) else {}
        runtime_states = schema_registry.get("runtime_states", {}) if isinstance(schema_registry, dict) else {}
        runtime_states = runtime_states if isinstance(runtime_states, dict) else {}
        catalog_entities = schema_registry.get("catalog_entities", {}) if isinstance(schema_registry, dict) else {}
        catalog_entities = catalog_entities if isinstance(catalog_entities, dict) else {}
        entity_schema_names = {
            str(schema.get("name", "") or "")
            for schema in (content.entity_schemas or [])
            if isinstance(schema, dict) and str(schema.get("name", "") or "")
        }

        candidates: List[Tuple[str, str]] = []
        identity_evidence = contract.get("identity_evidence", [])
        if isinstance(identity_evidence, list):
            for item in identity_evidence:
                value = str(item or "").strip()
                if not value:
                    continue
                if value.startswith("schema_registry:"):
                    candidates.append((value[len("schema_registry:"):].strip(), "identity_evidence"))
        value_shape = contract.get("value_shape", {})
        if isinstance(value_shape, dict):
            for key in ("state_ref", "state_id", "entity_name"):
                value = str(value_shape.get(key, "") or "").strip()
                if value:
                    candidates.append((value, f"value_shape.{key}"))

        seen = set()
        for name, source in candidates:
            if not name or name in seen:
                continue
            seen.add(name)
            runtime_state = runtime_states.get(name)
            if isinstance(runtime_state, dict):
                return {
                    "state_ref": name,
                    "entity_ref": str(runtime_state.get("entity_name", "") or runtime_state.get("entity_ref", "") or ""),
                    "source": source,
                }
            entries = refs.get(name, [])
            entries = entries if isinstance(entries, list) else []
            for preferred_kind in ("runtime_state", "draft_state", "ui_state"):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("kind", "") or "") != preferred_kind:
                        continue
                    state_ref = str(entry.get("ref", "") or "").strip()
                    state = runtime_states.get(state_ref, {}) if state_ref else {}
                    state = state if isinstance(state, dict) else {}
                    return {
                        "state_ref": state_ref,
                        "entity_ref": str(state.get("entity_name", "") or state.get("entity_ref", "") or ""),
                        "source": source,
                    }
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("kind", "") or "") != "catalog_entity":
                    continue
                entity_ref = str(entry.get("ref", "") or "").strip() or name
                return {
                    "state_ref": "",
                    "entity_ref": entity_ref,
                    "source": source,
                }
            if name in catalog_entities or name in entity_schema_names:
                return {
                    "state_ref": "",
                    "entity_ref": name,
                    "source": source,
                }
        return {}

    def _register_l0_runtime_state_shape(
        self,
        content: "SiteContent",
        state_ref: str,
        entity_ref: str,
        state_scope: str,
        role: str,
    ) -> bool:
        schema_registry = content.schema_registry if isinstance(content.schema_registry, dict) else {}
        if not schema_registry:
            schema_registry = self._build_l0_schema_registry(content)
            content.schema_registry = schema_registry
        state_key = str(state_ref or "").strip()
        if not state_key:
            return False
        runtime_states = schema_registry.setdefault("runtime_states", {})
        created = state_key not in runtime_states
        kind = self._l0_runtime_state_kind_for_scope(state_scope, state_key)
        runtime_states.setdefault(
            state_key,
            {
                "kind": kind,
                "state_id": state_key,
                "entity_name": str(entity_ref or "").strip(),
                "state_type": str(state_scope or "runtime_state"),
                "observed_on_pages": [],
                "value_shape": {
                    "state_type": str(state_scope or "runtime_state"),
                    "entity_name": str(entity_ref or "").strip(),
                    "role": str(role or ""),
                },
                "provenance": "derived_from_task_interaction_contract_identity",
            },
        )
        self._l0_add_registry_ref(schema_registry, state_key, kind, state_key)
        if entity_ref:
            self._l0_add_registry_ref(schema_registry, entity_ref, kind, state_key)
        schema_registry["runtime_state_count"] = len(runtime_states)
        return created

    def _derive_l0_state_ref_for_contract(self, contract: Dict[str, Any]) -> Tuple[str, str]:
        state_ref = str(contract.get("state_ref", "") or "").strip()
        if state_ref:
            return state_ref, "existing_state_ref"
        entity_ref = str(contract.get("entity_ref", "") or "").strip()
        action_semantic = str(contract.get("action_semantic", "") or "").strip()
        state_scope = str(contract.get("state_scope", "") or "").strip()
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        effect = str(sdk_effect.get("effect", "") or "").strip()
        if entity_ref:
            slug = self._l0_state_ref_slug(entity_ref)
            derivation_suffix = ""
        else:
            slug = self._l0_contract_anchor_slug(contract)
            derivation_suffix = "_from_contract_anchor"
        if not slug:
            return "", "missing_state_and_entity_ref"
        if state_scope == "draft" or action_semantic in {"create_record_draft", "update_current_draft"}:
            return f"current_{slug}_draft", "derived_current_draft" + derivation_suffix
        if state_scope == "transaction" or action_semantic == "commit_transaction" or effect == "submit":
            return f"current_{slug}_transaction", "derived_transaction" + derivation_suffix
        if state_scope == "record" or action_semantic in {"open_detail", "edit_existing_record"}:
            return f"selected_{slug}", "derived_selected_record" + derivation_suffix
        if state_scope == "collection" or action_semantic == "open_collection":
            return f"{slug}_collection", "derived_collection" + derivation_suffix
        if state_scope == "selection" or action_semantic.startswith("select_"):
            return f"selected_{slug}", "derived_selection" + derivation_suffix
        if state_scope == "accumulator" or action_semantic == "append_accumulator":
            return f"{slug}_accumulator", "derived_accumulator" + derivation_suffix
        if state_scope == "inline_ui" or action_semantic == "inline_ui_state":
            return f"{slug}_ui", "derived_inline_ui" + derivation_suffix
        return "", "unhandled_state_identity"

    @staticmethod
    def _l0_contract_requires_record_route(action_semantic: str, state_scope: str, sdk_effect: Dict[str, Any]) -> bool:
        effect = str((sdk_effect or {}).get("effect", "") or "").strip()
        semantic = str(action_semantic or "").strip()
        scope = str(state_scope or "").strip()
        return semantic in {"open_detail", "edit_existing_record"} or (scope == "record" and effect == "navigate")

    @staticmethod
    def _l0_contract_requires_hydration(action_semantic: str, state_scope: str, sdk_effect: Dict[str, Any]) -> bool:
        effect = str((sdk_effect or {}).get("effect", "") or "").strip()
        semantic = str(action_semantic or "").strip()
        scope = str(state_scope or "").strip()
        return (
            semantic in {"open_detail", "edit_existing_record", "commit_transaction"}
            or effect == "submit"
            or (scope == "record" and effect == "navigate")
        )

    @staticmethod
    def _l0_contract_requires_identity_grounding(contract: Dict[str, Any]) -> bool:
        action_semantic = str(contract.get("action_semantic", "") or "").strip()
        state_scope = str(contract.get("state_scope", "") or "").strip()
        route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        field_contract = contract.get("field_contract", {}) if isinstance(contract.get("field_contract", {}), dict) else {}
        effect = str(sdk_effect.get("effect", "") or "").strip()
        if bool(route_binding.get("requires_params", False)) or bool(hydration.get("required", False)):
            return True
        if state_scope == "collection" and action_semantic == "open_collection":
            typed_fields = field_contract.get("typed_fields", [])
            required_fields = field_contract.get("unobserved_required_fields", [])
            if effect in {"", "none", "navigate"} and not (typed_fields or required_fields):
                return False
        critical_scopes = {"record", "draft", "transaction", "selection", "accumulator", "collection"}
        non_executable_semantics = {"read_only", "inline_ui_state", "unknown"}
        return state_scope in critical_scopes and action_semantic not in non_executable_semantics

    def _canonicalize_l0_contract_state_identity(
        self,
        contract: Dict[str, Any],
        content: "SiteContent",
        summary: Dict[str, Any],
    ) -> None:
        if not isinstance(contract, dict):
            return
        action_semantic = str(contract.get("action_semantic", "") or "").strip()
        state_scope = str(contract.get("state_scope", "") or "").strip()
        entity_ref = str(contract.get("entity_ref", "") or "").strip()
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        requires_identity_grounding = self._l0_contract_requires_identity_grounding(contract)
        original_state_ref = str(contract.get("state_ref", "") or "").strip()
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        hydration_state_ref = str(hydration.get("state_ref", "") or "").strip()
        if hydration_state_ref and not original_state_ref:
            contract["state_ref"] = hydration_state_ref
            original_state_ref = hydration_state_ref
        if not original_state_ref and not entity_ref:
            resolved_identity = self._resolve_l0_structured_identity(contract, content)
            if resolved_identity:
                resolved_state_ref = str(resolved_identity.get("state_ref", "") or "").strip()
                resolved_entity_ref = str(resolved_identity.get("entity_ref", "") or "").strip()
                if resolved_state_ref:
                    contract["state_ref"] = resolved_state_ref
                    original_state_ref = resolved_state_ref
                if resolved_entity_ref:
                    contract["entity_ref"] = resolved_entity_ref
                    entity_ref = resolved_entity_ref
                summary["identity_backfill_count"] += 1
                summary.setdefault("identity_backfills", []).append({
                    "contract_id": str(contract.get("contract_id", "") or ""),
                    "candidate_id": str(contract.get("candidate_id", "") or ""),
                    "state_scope": state_scope,
                    "state_ref": resolved_state_ref,
                    "entity_ref": resolved_entity_ref,
                    "source": str(resolved_identity.get("source", "") or ""),
                })
        state_ref, derivation = self._derive_l0_state_ref_for_contract(contract)
        if state_ref and not original_state_ref:
            contract["state_ref"] = state_ref
            summary["canonicalized_count"] += 1
            summary.setdefault("changes", []).append({
                "contract_id": str(contract.get("contract_id", "") or ""),
                "candidate_id": str(contract.get("candidate_id", "") or ""),
                "state_scope": state_scope,
                "entity_ref": entity_ref,
                "state_ref": state_ref,
                "derivation": derivation,
            })
        elif not state_ref and requires_identity_grounding:
            summary.setdefault("unresolved", []).append({
                "contract_id": str(contract.get("contract_id", "") or ""),
                "candidate_id": str(contract.get("candidate_id", "") or ""),
                "state_scope": state_scope,
                "entity_ref": entity_ref,
                "action_semantic": action_semantic,
                "code": derivation,
            })
            summary["unresolved_count"] += 1
            return

        if state_ref:
            if isinstance(hydration, dict) and bool(hydration.get("required", False)):
                if not str(hydration.get("state_ref", "") or "").strip():
                    hydration["state_ref"] = state_ref
                contract["hydration"] = hydration
            created = self._register_l0_runtime_state_shape(
                content,
                state_ref,
                entity_ref,
                state_scope,
                derivation,
            )
            if created:
                summary["created_runtime_state_count"] += 1

        if state_ref and self._l0_contract_requires_record_route(action_semantic, state_scope, sdk_effect):
            route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
            params = route_binding.get("params", [])
            if not isinstance(params, list):
                params = []
            if entity_ref:
                record_param = f"{self._l0_state_ref_slug(entity_ref)}_id"
                if record_param not in params:
                    params.append(record_param)
            route_binding["requires_params"] = bool(params)
            route_binding["params"] = params
            route_binding.setdefault("source", "canonicalized_from_entity_ref")
            route_binding.setdefault("preserve_existing", True)
            contract["route_binding"] = route_binding

        if state_ref and self._l0_contract_requires_hydration(action_semantic, state_scope, sdk_effect):
            hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
            hydration["required"] = True
            hydration["state_ref"] = str(hydration.get("state_ref", "") or state_ref)
            if not hydration.get("mode"):
                hydration["mode"] = "from_route_or_state" if state_scope == "record" else "from_state"
            hydration.setdefault("bind_fields_from_state", state_scope in {"record", "transaction"})
            hydration.setdefault("source_ref", "canonicalized_from_task_interaction_state_identity")
            contract["hydration"] = hydration

    def _canonicalize_l0_task_interaction_state_identity(self, site_model: AppSpec) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "format": "task_interaction_state_identity_canonicalization",
            "schema_revision": 1,
            "enabled": self._l0_schema_registry_flow_enabled(),
            "canonicalized_count": 0,
            "identity_backfill_count": 0,
            "created_runtime_state_count": 0,
            "unresolved_count": 0,
            "changes": [],
            "identity_backfills": [],
            "unresolved": [],
        }
        if not summary["enabled"]:
            return summary
        content = getattr(site_model, "content", None)
        if content is None:
            return summary
        if not isinstance(content.schema_registry, dict) or not content.schema_registry:
            content.schema_registry = self._build_l0_schema_registry(content)
        artifact = getattr(site_model, "task_interaction_contracts", {})
        if not isinstance(artifact, dict):
            return summary

        contracts: List[Dict[str, Any]] = []
        for contract in artifact.get("contracts", []) or []:
            if isinstance(contract, dict):
                contracts.append(contract)
        contracts_by_candidate_id = artifact.get("contracts_by_candidate_id", {})
        if isinstance(contracts_by_candidate_id, dict):
            for contract in contracts_by_candidate_id.values():
                if isinstance(contract, dict):
                    contracts.append(contract)

        seen_ids = set()
        for contract in contracts:
            object_id = id(contract)
            if object_id in seen_ids:
                continue
            seen_ids.add(object_id)
            self._canonicalize_l0_contract_state_identity(contract, content, summary)

        by_candidate = {}
        by_contract = {}
        for contract in contracts:
            candidate_id = str(contract.get("candidate_id", "") or "")
            contract_id = str(contract.get("contract_id", "") or "")
            if candidate_id:
                by_candidate[candidate_id] = contract
            if contract_id:
                by_contract[contract_id] = contract

        structure = getattr(site_model, "structure", None)
        page_behaviors = getattr(structure, "page_behaviors", {}) if structure is not None else {}
        if isinstance(page_behaviors, dict):
            for behavior in page_behaviors.values():
                if not isinstance(behavior, dict):
                    continue
                continuations = behavior.get("task_continuations", [])
                if not isinstance(continuations, list):
                    continue
                for continuation in continuations:
                    if not isinstance(continuation, dict):
                        continue
                    candidate_id = str(continuation.get("candidate_id", "") or "")
                    contract_id = str(continuation.get("contract_id", "") or "")
                    contract = by_candidate.get(candidate_id) or by_contract.get(contract_id)
                    if not contract:
                        nested = continuation.get("interaction_contract")
                        if isinstance(nested, dict):
                            contract = nested
                            self._canonicalize_l0_contract_state_identity(contract, content, summary)
                    if contract:
                        continuation["contract_id"] = contract.get("contract_id", "")
                        continuation["interaction_contract"] = contract

        task_elements = getattr(site_model, "task_elements", {})
        if isinstance(task_elements, dict) and isinstance(page_behaviors, dict):
            task_elements["continuations_by_page"] = {
                page_id: behavior.get("task_continuations", [])
                for page_id, behavior in page_behaviors.items()
                if isinstance(behavior, dict) and behavior.get("task_continuations")
            }
            task_meta = task_elements.setdefault("task_interaction_contracts", {})
            if isinstance(task_meta, dict):
                task_meta["state_identity_canonicalization"] = {
                    "canonicalized_count": summary["canonicalized_count"],
                    "identity_backfill_count": summary["identity_backfill_count"],
                    "created_runtime_state_count": summary["created_runtime_state_count"],
                    "unresolved_count": summary["unresolved_count"],
                }
        return summary

    @staticmethod
    def _l0_schema_registry_resolves_ref(schema_registry: Dict[str, Any], ref: str) -> bool:
        value = str(ref or "").strip()
        if not value:
            return True
        refs = schema_registry.get("refs", {}) if isinstance(schema_registry, dict) else {}
        return isinstance(refs, dict) and value in refs

    def _scope_l0_field_obligations(self, contract: Dict[str, Any]) -> None:
        if not self._l0_schema_registry_flow_enabled():
            return
        field_contract = contract.get("field_contract", {})
        if not isinstance(field_contract, dict):
            return
        required_fields = [
            str(item)
            for item in field_contract.get("unobserved_required_fields", [])
            if str(item)
        ] if isinstance(field_contract.get("unobserved_required_fields", []), list) else []
        if not required_fields:
            field_contract.setdefault("field_obligations", [])
            field_contract.setdefault("dropped_unobserved_required_fields", [])
            return
        action_semantic = str(contract.get("action_semantic", "") or "")
        state_scope = str(contract.get("state_scope", "") or "")
        state_ref = str(contract.get("state_ref", "") or "")
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        hydration_state_ref = str(hydration.get("state_ref", "") or "")
        allowed_actions = {
            "create_record_draft",
            "edit_existing_record",
            "update_current_draft",
            "commit_transaction",
        }
        has_form_state = bool(state_ref or hydration_state_ref or state_scope in {"draft", "record", "transaction"})
        scoped: List[str] = []
        dropped: List[Dict[str, str]] = []
        for field in required_fields:
            if action_semantic in allowed_actions and has_form_state:
                scoped.append(field)
            else:
                dropped.append({
                    "field": field,
                    "reason": "out_of_scope_action",
                    "action_semantic": action_semantic,
                    "state_scope": state_scope,
                })
        field_contract["unobserved_required_fields"] = scoped
        field_contract["field_obligations"] = [
            {
                "field": field,
                "scope": "same_form_state_only",
                "state_ref": state_ref or hydration_state_ref,
                "action_semantic": action_semantic,
            }
            for field in scoped
        ]
        field_contract["dropped_unobserved_required_fields"] = dropped

    @staticmethod
    def _task_interaction_contract_text(candidate: Dict[str, Any]) -> str:
        parts = [
            candidate.get("display_text", ""),
            candidate.get("element_name", ""),
            candidate.get("reason", ""),
            candidate.get("evidence", ""),
        ]
        return " ".join(str(part or "") for part in parts)

    @staticmethod
    def _task_interaction_has_field_contract(candidate: Dict[str, Any]) -> bool:
        field_contract = candidate.get("field_contract", {})
        if not isinstance(field_contract, dict):
            return False
        for key in ("observed_fields", "typed_fields", "unobserved_required_fields"):
            values = field_contract.get(key, [])
            if isinstance(values, list) and values:
                return True
        return False

    def _admit_task_interaction_candidates(
        self,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        admitted: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        terminal_markers = (
            "任务完成",
            "结束任务",
            "终止任务",
            "终止操作",
            "现在结束",
            "用户接管",
            "请求用户",
            "告知用户",
        )
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("candidate_id", "") or "")
            text = self._task_interaction_contract_text(candidate)
            has_target = bool(
                str(candidate.get("target_filename", "") or "").strip()
                or str(candidate.get("target_page_id", "") or "").strip()
            )
            has_runtime_binding = bool(candidate.get("runtime_state_observation_ids")) or bool(
                candidate.get("runtime_state_obligation_ids")
            )
            has_field_contract = self._task_interaction_has_field_contract(candidate)
            continuation_kind = str(candidate.get("continuation_kind", "") or "").strip()
            element_type = str(candidate.get("element_type", "") or "").strip()
            terminal_only = (
                not has_target
                and not has_runtime_binding
                and not has_field_contract
                and any(marker in text for marker in terminal_markers)
            )
            if terminal_only:
                dropped.append({
                    "candidate_id": candidate_id,
                    "admission": "terminal_or_user_takeover",
                    "reason": "terminal or user-takeover trace narration without executable state binding",
                    "display_text": str(candidate.get("display_text", "") or ""),
                    "source_page_id": str(candidate.get("source_page_id", "") or ""),
                    "source_filename": str(candidate.get("source_filename", "") or ""),
                })
                continue
            evidence_only = (
                not has_target
                and not has_runtime_binding
                and not has_field_contract
                and not continuation_kind
                and not element_type
            )
            if evidence_only:
                dropped.append({
                    "candidate_id": candidate_id,
                    "admission": "evidence_only",
                    "reason": "candidate has no executable target, runtime binding, field contract, or UI action shape",
                    "display_text": str(candidate.get("display_text", "") or ""),
                    "source_page_id": str(candidate.get("source_page_id", "") or ""),
                    "source_filename": str(candidate.get("source_filename", "") or ""),
                })
                continue
            admitted.append(candidate)
        return {
            "format": "task_interaction_contract_admission",
            "schema_revision": 1,
            "candidate_count": len([item for item in candidates or [] if isinstance(item, dict)]),
            "admitted_count": len(admitted),
            "dropped_count": len(dropped),
            "admitted_candidates": admitted,
            "dropped_candidates": dropped,
        }

    @staticmethod
    def _compact_task_interaction_field_contract(field_contract: Any, limit: int = 12) -> Dict[str, List[str]]:
        source = field_contract if isinstance(field_contract, dict) else {}
        compact: Dict[str, List[str]] = {}
        for key in ("observed_fields", "typed_fields", "unobserved_required_fields"):
            values = source.get(key, [])
            compact[key] = [
                str(item)
                for item in values[:limit]
                if str(item)
            ] if isinstance(values, list) else []
        return compact

    @staticmethod
    def _compact_task_interaction_state_ref_candidates(candidates: Any, limit: int = 8) -> List[Dict[str, Any]]:
        compact: List[Dict[str, Any]] = []
        if not isinstance(candidates, list):
            return compact
        for item in candidates[:limit]:
            if not isinstance(item, dict):
                if str(item):
                    compact.append({"state_ref": str(item)})
                continue
            compact.append({
                "state_ref": str(item.get("state_ref", "") or ""),
                "entity_ref": str(item.get("entity_ref", "") or ""),
                "field_ref": str(item.get("field_ref", "") or ""),
                "confidence": item.get("confidence", ""),
                "reasons": [
                    str(reason)
                    for reason in item.get("reasons", [])[:4]
                    if str(reason)
                ] if isinstance(item.get("reasons", []), list) else [],
            })
        return compact

    @staticmethod
    def _compact_task_interaction_runtime_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "observation_id": str(item.get("observation_id", "") or ""),
            "trace_id": str(item.get("trace_id", "") or ""),
            "step_index": item.get("step_index", ""),
            "obs_key": str(item.get("obs_key", "") or ""),
            "page_cluster_id": str(item.get("page_cluster_id", "") or ""),
            "materialized_page_id": str(item.get("materialized_page_id", "") or ""),
            "general_page_id": str(item.get("general_page_id", "") or ""),
            "state_scope": str(item.get("state_scope", "") or item.get("scope", "") or ""),
            "lifecycle": str(item.get("lifecycle", "") or ""),
            "mutation": str(item.get("mutation", "") or ""),
            "value_hints": [
                str(value)
                for value in item.get("value_hints", [])[:8]
                if str(value)
            ] if isinstance(item.get("value_hints", []), list) else [],
            "negative_constraints": [
                str(value)
                for value in item.get("negative_constraints", [])[:8]
                if str(value)
            ] if isinstance(item.get("negative_constraints", []), list) else [],
            "raw_evidence": str(item.get("raw_evidence", "") or "")[:240],
        }

    def _build_task_interaction_identity_grounding_pack(
        self,
        candidates: List[Dict[str, Any]],
        content: "SiteContent",
        runtime_state_observations: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        schema_registry = content.schema_registry if content and isinstance(content.schema_registry, dict) else {}
        runtime_states = schema_registry.get("runtime_states", {}) if isinstance(schema_registry, dict) else {}
        observations_by_id: Dict[str, Dict[str, Any]] = {}
        observations_by_obs_key: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(runtime_state_observations, dict):
            for item in runtime_state_observations.get("observations", []) if isinstance(runtime_state_observations.get("observations", []), list) else []:
                if not isinstance(item, dict):
                    continue
                compact = self._compact_task_interaction_runtime_evidence(item)
                observation_id = compact.get("observation_id", "")
                obs_key = compact.get("obs_key", "")
                if observation_id:
                    observations_by_id[observation_id] = compact
                if obs_key:
                    observations_by_obs_key.setdefault(obs_key, []).append(compact)

        def registry_state_option(state_ref: str, reasons: List[str]) -> Dict[str, Any]:
            state = runtime_states.get(state_ref, {}) if isinstance(runtime_states, dict) else {}
            state = state if isinstance(state, dict) else {}
            return {
                "state_ref": str(state_ref or ""),
                "entity_ref": str(state.get("entity_name", "") or state.get("entity_ref", "") or ""),
                "kind": str(state.get("kind", "") or ""),
                "state_type": str(state.get("state_type", "") or ""),
                "observed_on_pages": [
                    str(page)
                    for page in state.get("observed_on_pages", [])[:12]
                    if str(page)
                ] if isinstance(state.get("observed_on_pages", []), list) else [],
                "reasons": reasons,
            }

        pack_candidates: List[Dict[str, Any]] = []
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            runtime_evidence: List[Dict[str, Any]] = []
            seen_evidence = set()
            for observation_id in candidate.get("runtime_state_observation_ids", []) if isinstance(candidate.get("runtime_state_observation_ids", []), list) else []:
                evidence = observations_by_id.get(str(observation_id or ""))
                if evidence and evidence.get("observation_id") not in seen_evidence:
                    runtime_evidence.append(evidence)
                    seen_evidence.add(evidence.get("observation_id"))
            obs_key = str(candidate.get("obs_key", "") or "")
            for evidence in observations_by_obs_key.get(obs_key, []):
                evidence_id = evidence.get("observation_id", "")
                if evidence_id and evidence_id not in seen_evidence:
                    runtime_evidence.append(evidence)
                    seen_evidence.add(evidence_id)

            registry_candidates: Dict[str, Dict[str, Any]] = {}
            for item in candidate.get("state_ref_candidates", []) if isinstance(candidate.get("state_ref_candidates", []), list) else []:
                if not isinstance(item, dict):
                    continue
                state_ref = str(item.get("state_ref", "") or "").strip()
                if not state_ref:
                    continue
                reasons = [
                    str(reason)
                    for reason in item.get("reasons", [])[:4]
                    if str(reason)
                ] if isinstance(item.get("reasons", []), list) else []
                registry_candidates[state_ref] = registry_state_option(
                    state_ref,
                    reasons or ["candidate_state_ref_candidate"],
                )

            candidate_pages = {
                str(candidate.get("page_cluster_id", "") or ""),
                str(candidate.get("materialized_page_id", "") or ""),
                str(candidate.get("source_page_id", "") or ""),
            }
            for evidence in runtime_evidence:
                candidate_pages.update({
                    str(evidence.get("page_cluster_id", "") or ""),
                    str(evidence.get("general_page_id", "") or ""),
                    str(evidence.get("materialized_page_id", "") or ""),
                })
            candidate_pages.discard("")
            for state_ref, state in (runtime_states.items() if isinstance(runtime_states, dict) else []):
                if state_ref in registry_candidates or not isinstance(state, dict):
                    continue
                observed_pages = {
                    str(page)
                    for page in state.get("observed_on_pages", [])
                    if str(page)
                } if isinstance(state.get("observed_on_pages", []), list) else set()
                if observed_pages and observed_pages.intersection(candidate_pages):
                    registry_candidates[str(state_ref)] = registry_state_option(
                        str(state_ref),
                        ["registry_state_observed_on_candidate_page"],
                    )

            pack_candidates.append({
                "candidate_id": str(candidate.get("candidate_id", "") or ""),
                "runtime_evidence": runtime_evidence[:8],
                "registry_candidates": list(registry_candidates.values())[:12],
                "selection_rule": (
                    "For executable stateful actions, choose state_ref/entity_ref from "
                    "registry_candidates or candidate.state_ref_candidates. Leave both empty "
                    "only for pure read_only/user-handoff/non-app actions, or provide unresolved_reason."
                ),
            })

        return {
            "format": "task_interaction_identity_grounding_pack",
            "schema_revision": 1,
            "candidate_count": len(pack_candidates),
            "candidates": pack_candidates,
        }

    def _compact_task_interaction_candidate_for_planner(
        self,
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(candidate, dict):
            return {}
        evidence = candidate.get("evidence", {})
        evidence = evidence if isinstance(evidence, dict) else {}
        compact = {
            "candidate_id": str(candidate.get("candidate_id", "") or ""),
            "source_page_id": str(candidate.get("source_page_id", "") or ""),
            "source_page_name": str(candidate.get("source_page_name", "") or ""),
            "source_page_type": str(candidate.get("source_page_type", "") or ""),
            "source_filename": str(candidate.get("source_filename", "") or ""),
            "element_name": str(candidate.get("element_name", "") or ""),
            "display_text": str(candidate.get("display_text", "") or ""),
            "element_type": str(candidate.get("element_type", "") or ""),
            "continuation_kind": str(candidate.get("continuation_kind", "") or ""),
            "target_page_id": str(candidate.get("target_page_id", "") or ""),
            "target_page_name": str(candidate.get("target_page_name", "") or ""),
            "target_page_type": str(candidate.get("target_page_type", "") or ""),
            "target_filename": str(candidate.get("target_filename", "") or ""),
            "reason": str(candidate.get("reason", "") or ""),
            "trace_id": str(candidate.get("trace_id", "") or ""),
            "step_index": candidate.get("step_index", ""),
            "obs_key": str(candidate.get("obs_key", "") or ""),
            "page_cluster_id": str(candidate.get("page_cluster_id", "") or ""),
            "materialized_page_id": str(candidate.get("materialized_page_id", "") or ""),
            "materialized_page_key": str(candidate.get("materialized_page_key", "") or ""),
            "state_scope_candidate": str(candidate.get("state_scope_candidate", "") or ""),
            "lifecycle": str(candidate.get("lifecycle", "") or ""),
            "mutation": str(candidate.get("mutation", "") or ""),
            "field_contract": self._compact_task_interaction_field_contract(
                candidate.get("field_contract", {})
            ),
            "state_ref_candidates": self._compact_task_interaction_state_ref_candidates(
                candidate.get("state_ref_candidates", [])
            ),
            "runtime_state_observation_ids": [
                str(item)
                for item in candidate.get("runtime_state_observation_ids", [])
                if str(item)
            ] if isinstance(candidate.get("runtime_state_observation_ids", []), list) else [],
            "runtime_state_obligation_ids": [
                str(item)
                for item in candidate.get("runtime_state_obligation_ids", [])
                if str(item)
            ] if isinstance(candidate.get("runtime_state_obligation_ids", []), list) else [],
            "negative_constraints": [
                str(item)
                for item in candidate.get("negative_constraints", [])
                if str(item)
            ] if isinstance(candidate.get("negative_constraints", []), list) else [],
            "value_hints": [
                str(item)
                for item in candidate.get("value_hints", [])[:8]
                if str(item)
            ] if isinstance(candidate.get("value_hints", []), list) else [],
            "evidence": {
                "trigger_text": str(evidence.get("trigger_text", "") or ""),
                "task_instruction": str(evidence.get("task_instruction", "") or ""),
                "lifecycle_hint": str(evidence.get("lifecycle_hint", "") or ""),
            },
        }
        return compact

    @staticmethod
    def _task_interaction_candidate_signature_value(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(
                (str(key), ConsolidationMixin._task_interaction_candidate_signature_value(val))
                for key, val in sorted(value.items(), key=lambda item: str(item[0]))
                if val not in ("", [], {}, None)
            )
        if isinstance(value, list):
            return tuple(
                ConsolidationMixin._task_interaction_candidate_signature_value(item)
                for item in value
                if item not in ("", [], {}, None)
            )
        return str(value or "")

    def _task_interaction_candidate_representative_signature(
        self,
        candidate: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        has_target = bool(
            str(candidate.get("target_page_id", "") or "").strip()
            or str(candidate.get("target_filename", "") or "").strip()
        )
        has_runtime_binding = bool(candidate.get("runtime_state_observation_ids")) or bool(
            candidate.get("runtime_state_obligation_ids")
        )
        if not has_target and not has_runtime_binding:
            return ("candidate", str(candidate.get("candidate_id", "") or ""))
        compact = self._compact_task_interaction_candidate_for_planner(candidate)
        return (
            "representative",
            compact.get("source_page_id") or compact.get("source_filename", ""),
            compact.get("source_page_type", ""),
            compact.get("element_type", ""),
            compact.get("continuation_kind", ""),
            compact.get("target_page_id") or compact.get("target_filename", ""),
            compact.get("target_page_type", ""),
            compact.get("reason", ""),
            compact.get("page_cluster_id", ""),
            compact.get("materialized_page_id", ""),
            compact.get("materialized_page_key", ""),
            compact.get("state_scope_candidate", ""),
            compact.get("lifecycle", ""),
            compact.get("mutation", ""),
            self._task_interaction_candidate_signature_value(compact.get("field_contract", {})),
            self._task_interaction_candidate_signature_value(compact.get("state_ref_candidates", [])),
            self._task_interaction_candidate_signature_value(compact.get("negative_constraints", [])),
            self._task_interaction_candidate_signature_value(compact.get("value_hints", [])),
        )

    def _build_task_interaction_planning_representatives(
        self,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            groups.setdefault(
                self._task_interaction_candidate_representative_signature(candidate),
                [],
            ).append(candidate)

        representatives: List[Dict[str, Any]] = []
        representative_groups: List[Dict[str, Any]] = []
        alias_map: Dict[str, List[str]] = {}
        for signature, group in groups.items():
            group = [
                candidate for candidate in group
                if str(candidate.get("candidate_id", "") or "")
            ]
            if not group:
                continue
            representative = dict(group[0])
            representative_id = str(representative.get("candidate_id", "") or "")
            alias_ids = [
                str(candidate.get("candidate_id", "") or "")
                for candidate in group
                if str(candidate.get("candidate_id", "") or "")
            ]
            representatives.append(representative)
            alias_map[representative_id] = alias_ids
            representative_groups.append({
                "representative_candidate_id": representative_id,
                "alias_candidate_ids": alias_ids,
                "alias_count": len(alias_ids),
                "signature": [str(item) for item in signature],
            })
        return {
            "format": "task_interaction_planning_representatives",
            "schema_revision": 1,
            "input_candidate_count": len([item for item in candidates or [] if isinstance(item, dict)]),
            "representative_count": len(representatives),
            "merged_alias_count": sum(max(0, len(item.get("alias_candidate_ids", [])) - 1) for item in representative_groups),
            "representatives": representatives,
            "alias_map": alias_map,
            "representative_groups": representative_groups,
        }

    def _scope_task_interaction_page_context(
        self,
        candidates: List[Dict[str, Any]],
        page_context: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not self._l0_schema_registry_flow_enabled():
            return page_context
        wanted_page_ids = set()
        wanted_filenames = set()
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            for key in ("source_page_id", "target_page_id"):
                value = str(candidate.get(key, "") or "").strip()
                if value:
                    wanted_page_ids.add(value)
            for key in ("source_filename", "target_filename"):
                value = str(candidate.get(key, "") or "").strip()
                if value:
                    wanted_filenames.add(value)
        scoped = [
            page for page in page_context
            if str(page.get("page_id", "") or "") in wanted_page_ids
            or str(page.get("filename", "") or "") in wanted_filenames
        ]
        return scoped or page_context

    @staticmethod
    def _task_interaction_candidate_runtime_keys(
        candidates: List[Dict[str, Any]],
    ) -> Tuple[set[str], set[str]]:
        observation_ids: set[str] = set()
        obs_keys: set[str] = set()
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            for item in candidate.get("runtime_state_observation_ids", []) if isinstance(candidate.get("runtime_state_observation_ids", []), list) else []:
                value = str(item or "").strip()
                if value:
                    observation_ids.add(value)
            value = str(candidate.get("obs_key", "") or "").strip()
            if value:
                obs_keys.add(value)
        return observation_ids, obs_keys

    @staticmethod
    def _compact_schema_entity_for_task_interaction(entity: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(entity, dict):
            return {}
        fields = entity.get("fields", [])
        compact_fields = []
        if isinstance(fields, list):
            for field in fields[:20]:
                if not isinstance(field, dict):
                    continue
                compact_fields.append({
                    "name": str(field.get("name", "") or field.get("field_name", "") or ""),
                    "type": str(field.get("type", "") or field.get("field_type", "") or ""),
                    "required": bool(field.get("required", False)),
                })
        return {
            "name": str(entity.get("name", "") or entity.get("entity_name", "") or ""),
            "fields": [field for field in compact_fields if field.get("name")],
        }

    @staticmethod
    def _compact_task_state_for_task_interaction(state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {}
        compact = {
            "state_id": str(state.get("state_id", "") or state.get("id", "") or state.get("name", "") or ""),
            "name": str(state.get("name", "") or state.get("state_name", "") or ""),
            "entity_name": str(state.get("entity_name", "") or state.get("entity_ref", "") or ""),
            "state_type": str(state.get("state_type", "") or state.get("type", "") or ""),
            "role": str(state.get("role", "") or state.get("state_role", "") or ""),
        }
        fields = state.get("fields", [])
        if isinstance(fields, list):
            compact["fields"] = [
                str(field.get("name", "") or field.get("field_name", "") or field)
                for field in fields[:16]
                if str(field.get("name", "") if isinstance(field, dict) else field)
            ]
        return {key: value for key, value in compact.items() if value not in ("", [], {}, None)}

    @staticmethod
    def _compact_interaction_action_for_task_interaction(action: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(action, dict):
            return {}
        return {
            key: str(action.get(key, "") or "")
            for key in ("action_id", "id", "name", "action_name", "type", "description")
            if str(action.get(key, "") or "")
        }

    def _build_task_interaction_schema_context_for_chunk(
        self,
        *,
        candidates: List[Dict[str, Any]],
        content: "SiteContent",
        identity_grounding_pack: Dict[str, Any],
    ) -> Dict[str, Any]:
        entity_names: set[str] = set()
        state_refs: set[str] = set()
        candidate_pages: set[str] = set()
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            for key in ("source_page_id", "target_page_id", "page_cluster_id", "materialized_page_id"):
                value = str(candidate.get(key, "") or "").strip()
                if value:
                    candidate_pages.add(value)
            for item in candidate.get("state_ref_candidates", []) if isinstance(candidate.get("state_ref_candidates", []), list) else []:
                if not isinstance(item, dict):
                    continue
                state_ref = str(item.get("state_ref", "") or "").strip()
                if state_ref:
                    state_refs.add(state_ref)
                entity_ref = str(item.get("entity_ref", "") or item.get("entity_name", "") or "").strip()
                if entity_ref:
                    entity_names.add(entity_ref)

        for pack_candidate in identity_grounding_pack.get("candidates", []) if isinstance(identity_grounding_pack, dict) else []:
            if not isinstance(pack_candidate, dict):
                continue
            for option in pack_candidate.get("registry_candidates", []) if isinstance(pack_candidate.get("registry_candidates", []), list) else []:
                if not isinstance(option, dict):
                    continue
                state_ref = str(option.get("state_ref", "") or "").strip()
                if state_ref:
                    state_refs.add(state_ref)
                entity_ref = str(option.get("entity_ref", "") or "").strip()
                if entity_ref:
                    entity_names.add(entity_ref)

        schema_registry = content.schema_registry if content and isinstance(content.schema_registry, dict) else {}
        runtime_states = schema_registry.get("runtime_states", {}) if isinstance(schema_registry, dict) else {}
        catalog_entities = schema_registry.get("catalog_entities", {}) if isinstance(schema_registry, dict) else {}
        scoped_runtime_states = {}
        if isinstance(runtime_states, dict):
            for state_ref, state in runtime_states.items():
                if not isinstance(state, dict):
                    continue
                observed_pages = {
                    str(page)
                    for page in state.get("observed_on_pages", [])
                    if str(page)
                } if isinstance(state.get("observed_on_pages", []), list) else set()
                if str(state_ref) in state_refs or (observed_pages and observed_pages.intersection(candidate_pages)):
                    scoped_runtime_states[str(state_ref)] = {
                        key: state.get(key)
                        for key in ("entity_name", "entity_ref", "kind", "state_type", "role", "observed_on_pages")
                        if state.get(key) not in ("", [], {}, None)
                    }
                    entity_ref = str(state.get("entity_name", "") or state.get("entity_ref", "") or "").strip()
                    if entity_ref:
                        entity_names.add(entity_ref)
                if len(scoped_runtime_states) >= 64:
                    break

        compact_entities = []
        for entity in content.entity_schemas if content and isinstance(content.entity_schemas, list) else []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name", "") or entity.get("entity_name", "") or "").strip()
            if entity_names and name not in entity_names:
                continue
            compact = self._compact_schema_entity_for_task_interaction(entity)
            if compact.get("name"):
                compact_entities.append(compact)
            if len(compact_entities) >= 48:
                break

        compact_states = []
        for state in (content.task_state_contract or {}).get("states", []) if content and isinstance(content.task_state_contract, dict) else []:
            if not isinstance(state, dict):
                continue
            state_id = str(state.get("state_id", "") or state.get("id", "") or state.get("name", "") or "").strip()
            entity_ref = str(state.get("entity_name", "") or state.get("entity_ref", "") or "").strip()
            if state_refs and state_id not in state_refs and entity_ref not in entity_names:
                continue
            compact = self._compact_task_state_for_task_interaction(state)
            if compact:
                compact_states.append(compact)
            if len(compact_states) >= 64:
                break

        compact_actions = []
        for action in (content.interaction_contract or {}).get("actions", []) if content and isinstance(content.interaction_contract, dict) else []:
            compact = self._compact_interaction_action_for_task_interaction(action)
            if compact:
                compact_actions.append(compact)
            if len(compact_actions) >= 96:
                break

        scoped_catalog_entities = {}
        if isinstance(catalog_entities, dict):
            for entity_name in sorted(entity_names):
                entity = catalog_entities.get(entity_name)
                if isinstance(entity, dict):
                    scoped_catalog_entities[entity_name] = {
                        key: entity.get(key)
                        for key in ("entity_name", "primary_key", "identity_fields", "fields")
                        if entity.get(key) not in ("", [], {}, None)
                    }

        return {
            "entity_schemas": compact_entities,
            "task_state_contract": {
                "format": (content.task_state_contract or {}).get("format", "task_state_contract_v1") if content else "task_state_contract_v1",
                "states": compact_states,
            },
            "interaction_contract": {
                "format": (content.interaction_contract or {}).get("format", "interaction_contract_v1") if content else "interaction_contract_v1",
                "actions": compact_actions,
            },
            "schema_registry": {
                "format": schema_registry.get("format", "l0_schema_registry") if isinstance(schema_registry, dict) else "l0_schema_registry",
                "catalog_entities": scoped_catalog_entities,
                "runtime_states": scoped_runtime_states,
            },
            "projection_note": (
                "Chunk-scoped schema projection. Empty lists mean the chunk had no resolvable "
                "state/entity refs; use unresolved_reason rather than inventing ids."
            ),
        }

    @staticmethod
    def _contract_candidate(contract: Dict[str, Any]) -> Dict[str, Any]:
        candidate = contract.get("candidate", {})
        return candidate if isinstance(candidate, dict) else {}

    @staticmethod
    def _contract_target_key(contract: Dict[str, Any]) -> str:
        candidate = ConsolidationMixin._contract_candidate(contract)
        return str(
            candidate.get("target_filename")
            or candidate.get("target_page_id")
            or contract.get("target_filename")
            or ""
        ).strip()

    @staticmethod
    def _contract_trigger_key(contract: Dict[str, Any]) -> Tuple[str, str, str, str]:
        candidate = ConsolidationMixin._contract_candidate(contract)
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        return (
            str(candidate.get("source_page_id") or candidate.get("source_filename") or "").strip(),
            str(candidate.get("element_name") or candidate.get("display_text") or "").strip(),
            str(contract.get("action_semantic", "") or "").strip(),
            str(sdk_effect.get("method", "") or sdk_effect.get("effect", "") or "").strip(),
        )

    @staticmethod
    def _contract_canonical_signature(contract: Dict[str, Any]) -> Tuple[Any, ...]:
        candidate = ConsolidationMixin._contract_candidate(contract)
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
        params = route_binding.get("params", [])
        if not isinstance(params, list):
            params = []
        return (
            str(candidate.get("source_page_id") or candidate.get("source_filename") or "").strip(),
            str(candidate.get("target_filename") or candidate.get("target_page_id") or "").strip(),
            str(contract.get("action_semantic", "") or "").strip(),
            str(contract.get("state_scope", "") or "").strip(),
            str(contract.get("state_ref", "") or "").strip(),
            str(contract.get("entity_ref", "") or "").strip(),
            str(contract.get("source_lifecycle", "") or "").strip(),
            str(sdk_effect.get("effect", "") or "").strip(),
            str(sdk_effect.get("method", "") or "").strip(),
            tuple(sorted(str(item or "") for item in params)),
        )

    @staticmethod
    def _merge_unique_list_values(*values: Any) -> List[str]:
        merged: List[str] = []
        for value in values:
            if isinstance(value, list):
                iterable = value
            elif value:
                iterable = [value]
            else:
                iterable = []
            for item in iterable:
                text = str(item)
                if text and text not in merged:
                    merged.append(text)
        return merged

    def _merge_task_interaction_contract_group(
        self,
        contracts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        primary = dict(contracts[0])
        alias_ids = self._merge_unique_list_values(
            primary.get("alias_candidate_ids", []),
            primary.get("candidate_id", ""),
        )
        evidence_ids = self._merge_unique_list_values(
            primary.get("evidence_candidate_ids", []),
            primary.get("candidate_id", ""),
        )
        runtime_observation_ids = self._merge_unique_list_values(
            primary.get("runtime_state_observation_ids", [])
        )
        runtime_obligation_ids = self._merge_unique_list_values(
            primary.get("runtime_state_obligation_ids", [])
        )
        terminal_constraints = self._merge_unique_list_values(primary.get("terminal_constraints", []))
        field_contract = dict(primary.get("field_contract", {}) if isinstance(primary.get("field_contract", {}), dict) else {})
        for key in ("observed_fields", "typed_fields", "unobserved_required_fields"):
            field_contract[key] = self._merge_unique_list_values(field_contract.get(key, []))

        for contract in contracts[1:]:
            candidate_id = str(contract.get("candidate_id", "") or "")
            alias_ids = self._merge_unique_list_values(alias_ids, contract.get("alias_candidate_ids", []), candidate_id)
            evidence_ids = self._merge_unique_list_values(
                evidence_ids,
                contract.get("evidence_candidate_ids", []),
                candidate_id,
            )
            runtime_observation_ids = self._merge_unique_list_values(
                runtime_observation_ids,
                contract.get("runtime_state_observation_ids", []),
            )
            runtime_obligation_ids = self._merge_unique_list_values(
                runtime_obligation_ids,
                contract.get("runtime_state_obligation_ids", []),
            )
            terminal_constraints = self._merge_unique_list_values(
                terminal_constraints,
                contract.get("terminal_constraints", []),
            )
            other_field_contract = contract.get("field_contract", {})
            if isinstance(other_field_contract, dict):
                for key in ("observed_fields", "typed_fields", "unobserved_required_fields"):
                    field_contract[key] = self._merge_unique_list_values(
                        field_contract.get(key, []),
                        other_field_contract.get(key, []),
                    )
            primary["confidence"] = max(float(primary.get("confidence", 0.0) or 0.0), float(contract.get("confidence", 0.0) or 0.0))

        primary["alias_candidate_ids"] = alias_ids
        primary["evidence_candidate_ids"] = evidence_ids
        primary["runtime_state_observation_ids"] = runtime_observation_ids
        primary["runtime_state_obligation_ids"] = runtime_obligation_ids
        primary["terminal_constraints"] = terminal_constraints
        primary["field_contract"] = field_contract
        self._scope_l0_field_obligations(primary)
        primary["contract_id"] = self._stable_short_hash({
            "signature": list(self._contract_canonical_signature(primary)),
            "candidate_ids": alias_ids,
        }, "ticon")
        return primary

    def _reconcile_task_interaction_contracts(
        self,
        contracts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        valid_contracts = [contract for contract in contracts or [] if isinstance(contract, dict)]
        quarantined_ids = set()
        quarantined: List[Dict[str, Any]] = []
        by_trigger: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
        for contract in valid_contracts:
            by_trigger.setdefault(self._contract_trigger_key(contract), []).append(contract)
        for group in by_trigger.values():
            targets = {self._contract_target_key(contract) for contract in group if self._contract_target_key(contract)}
            action_semantic = str(group[0].get("action_semantic", "") or "")
            if len(targets) <= 1 or action_semantic not in {"open_detail", "open_collection", "navigate_shell"}:
                continue
            for contract in group:
                candidate_id = str(contract.get("candidate_id", "") or "")
                quarantined_ids.add(candidate_id)
                item = dict(contract)
                item["quarantine_reason"] = "conflicting_targets_for_same_trigger"
                quarantined.append(item)

        by_signature: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        for contract in valid_contracts:
            if str(contract.get("candidate_id", "") or "") in quarantined_ids:
                continue
            by_signature.setdefault(self._contract_canonical_signature(contract), []).append(contract)

        reconciled_contracts: List[Dict[str, Any]] = []
        contracts_by_candidate_id: Dict[str, Dict[str, Any]] = {}
        merged_groups: List[Dict[str, Any]] = []
        for signature, group in by_signature.items():
            merged = self._merge_task_interaction_contract_group(group)
            reconciled_contracts.append(merged)
            for candidate_id in merged.get("alias_candidate_ids", []):
                contracts_by_candidate_id[str(candidate_id)] = merged
            if len(group) > 1:
                merged_groups.append({
                    "canonical_contract_id": merged.get("contract_id", ""),
                    "candidate_ids": merged.get("alias_candidate_ids", []),
                    "signature": list(signature),
                })

        return {
            "format": "task_interaction_contract_reconciliation",
            "schema_revision": 1,
            "input_contract_count": len(valid_contracts),
            "canonical_contract_count": len(reconciled_contracts),
            "merged_group_count": len(merged_groups),
            "quarantined_count": len(quarantined),
            "contracts": reconciled_contracts,
            "contracts_by_candidate_id": contracts_by_candidate_id,
            "merged_groups": merged_groups,
            "quarantined_contracts": quarantined,
        }

    @staticmethod
    def _extract_observed_fields(text: str) -> List[str]:
        value = str(text or "")
        fields: List[str] = []
        for marker in ("字段", "输入 row", "输入行"):
            value = value.replace(marker, "输入框")
        candidates = []
        import re
        quoted = re.findall(r"[“\"]([^”\"]{1,24})[”\"]", value)
        if any(token in value.lower() for token in ("form", "field", "input", "表单", "字段", "输入", "必填", "选填")):
            candidates.extend(quoted)
        candidates.extend(re.findall(r"[“\"]([^”\"]{1,24})[”\"](?:（[^）]*）)?(?:输入框|输入项|字段|选项|开关|按钮)", value))
        candidates.extend(re.findall(r"(?:字段|包括|依次展示|下方依次展示)[^。；]*?([一-龥A-Za-z0-9/·]{2,24})(?:（[^）]*）)?(?:必填|选填|已填写|未选|选中)", value))
        for chunk in re.findall(r"包括([^。；]{1,160})", value):
            for item in re.split(r"[、,，/]|以及|和|与", chunk):
                item = item.strip(" ：:；;。,.（）()\"'“”")
                item = re.sub(r"^(必填|选填|可编辑字段|字段)", "", item).strip()
                item = re.sub(r"(等.*|标注.*|均.*)$", "", item).strip()
                if 2 <= len(item) <= 16 and any("一" <= ch <= "鿿" for ch in item):
                    candidates.append(item)
        for item in candidates:
            field = str(item).strip(" ：:；;。,.（）()\"'“”")
            if field and field not in fields:
                fields.append(field)
        return fields

    @staticmethod
    def _extract_typed_fields(text: str, element_type: str) -> List[str]:
        value = str(text or "")
        typed_fields = []
        if element_type in {"type", "input", "fill", "init"} or any(token in value for token in ("输入", "填写")):
            for marker in ("输入框", "填写区域", "输入区域"):
                if marker in value:
                    prefix = value.split(marker, 1)[0]
                    field = prefix.rsplit("在", 1)[-1].rsplit("点击", 1)[-1].strip(" ，。：“”\"'")
                    if field and len(field) <= 24:
                        typed_fields.append(field)
                    break
        return typed_fields

    @staticmethod
    def _extract_unobserved_required_fields(task_text: str, evidence_text: str, observed_fields: Optional[List[str]] = None) -> List[str]:
        task_value = str(task_text or "")
        evidence_value = str(evidence_text or "")
        field_keywords = {
            "邮箱": ("邮箱", "收票邮箱", "email"),
            "税号": ("税号", "纳税人识别号"),
            "备注": ("备注",),
            "手机号": ("手机号", "电话"),
            "地址": ("地址",),
        }
        missing = []
        observed_joined = " ".join(observed_fields or [])
        negative_evidence = any(token in evidence_value for token in ("未出现", "没有显示", "没看到", "未显示", "找不到"))
        for canonical, aliases in field_keywords.items():
            mentioned_in_task = any(alias in task_value for alias in aliases)
            negated_in_evidence = any(
                neg in evidence_value and alias in evidence_value
                for alias in aliases
                for neg in ("未出现", "没有显示", "没看到", "未显示", "找不到")
            )
            positively_visible = any(alias in observed_joined for alias in aliases) or (any(alias in evidence_value for alias in aliases) and not negated_in_evidence)
            if mentioned_in_task and (negated_in_evidence or not positively_visible or (negative_evidence and not positively_visible)):
                missing.append(canonical)
        return missing

    def _build_field_contract(self, text: str, element_type: str, task_text: str = "", evidence_text: str = "") -> Dict[str, Any]:
        observed_fields = self._extract_observed_fields(evidence_text or text)
        typed_fields = self._extract_typed_fields(text, element_type)
        for field in typed_fields:
            if field not in observed_fields and field in str(evidence_text or text):
                observed_fields.append(field)
        return {
            "observed_fields": observed_fields,
            "typed_fields": typed_fields,
            "unobserved_required_fields": self._extract_unobserved_required_fields(task_text, evidence_text, observed_fields),
        }

    @staticmethod
    def _empty_route_binding() -> Dict[str, Any]:
        return {
            "requires_params": False,
            "params": [],
            "source": "",
            "preserve_existing": False,
            "fallback": "",
        }

    @staticmethod
    def _empty_hydration() -> Dict[str, Any]:
        return {
            "required": False,
            "mode": "",
            "state_ref": "",
            "source_ref": "",
            "fallback": "",
            "bind_fields_from_state": False,
        }

    @staticmethod
    def _empty_sdk_effect() -> Dict[str, Any]:
        return {
            "trigger": "none",
            "effect": "none",
            "method": "",
        }

    def _empty_task_interaction_contract(self, candidate_id: str = "") -> Dict[str, Any]:
        return {
            "format": "task_interaction_contract",
            "schema_revision": 1,
            "contract_id": "",
            "candidate_id": candidate_id,
            "planner_source": "candidate_builder",
            "action_semantic": "unknown",
            "interaction_outcome": "unknown",
            "state_scope": "none",
            "state_provenance": "none",
            "source_lifecycle": "none",
            "state_ref": "",
            "entity_ref": "",
            "state_role": "",
            "value_shape": {},
            "identity_evidence": [],
            "interaction_ref": "",
            "route_binding": self._empty_route_binding(),
            "hydration": self._empty_hydration(),
            "sdk_effect": self._empty_sdk_effect(),
            "field_contract": {"observed_fields": [], "typed_fields": [], "unobserved_required_fields": []},
            "runtime_state_observation_ids": [],
            "runtime_state_obligation_ids": [],
            "terminal_constraints": [],
            "unresolved_reason": "",
            "rationale": "Pending model-planned task interaction classification.",
            "confidence": 0.0,
        }

    def _build_task_continuation_contract(
        self,
        *,
        element_name: str,
        display_text: str,
        element_type: str,
        continuation_kind: str,
        target_page_id: str = "",
        target_filename: str = "",
        reason: str,
        source_page_id: str = "",
        task_instruction: str = "",
        current_description: str = "",
        next_description: str = "",
        lifecycle_hint: str = "",
        target_page_type: str = "",
        visible_binding: Optional[Dict[str, Any]] = None,
        operation_binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        visible_binding = dict(visible_binding) if isinstance(visible_binding, dict) else {}
        operation_binding = dict(operation_binding) if isinstance(operation_binding, dict) else {}
        display_text = (
            display_text
            if visible_binding.get("visible_text_allowed") is True
            else ""
        )
        element_name = (
            element_name
            if visible_binding.get("visible_text_allowed") is True
            else str(operation_binding.get("operation") or element_type or "task_action")
        )
        evidence_text = " ".join(str(part or "") for part in (current_description, next_description))
        contract_text = " ".join(str(part or "") for part in (element_name, display_text, target_page_id, target_filename, reason, task_instruction, evidence_text))
        candidate_seed = {
            "source_page_id": source_page_id,
            "element_name": element_name,
            "display_text": display_text,
            "element_type": element_type,
            "continuation_kind": continuation_kind,
            "target_page_id": target_page_id,
            "target_filename": target_filename,
            "reason": reason,
        }
        candidate_id = self._stable_short_hash(candidate_seed, "ticand")
        interaction_contract = self._empty_task_interaction_contract(candidate_id=candidate_id)
        interaction_contract["field_contract"] = self._build_field_contract(
            contract_text,
            element_type,
            task_instruction,
            evidence_text,
        )
        interaction_contract["evidence"] = {
            "source_page_id": source_page_id,
            "target_page_id": target_page_id,
            "target_filename": target_filename,
            "target_page_type": target_page_type,
            "task_instruction": task_instruction,
            "current_description": current_description,
            "next_description": next_description,
            "lifecycle_hint": lifecycle_hint,
            "visible_binding": visible_binding,
            "operation_binding": operation_binding,
        }
        if visible_binding.get("visible_text_allowed") is True and (display_text or element_name):
            interaction_contract["evidence"]["trigger_text"] = display_text or element_name
        continuation = {
            "candidate_id": candidate_id,
            "element_name": element_name,
            "display_text": display_text,
            "element_type": element_type,
            "continuation_kind": continuation_kind,
            "source_page_id": source_page_id,
            "target_page_id": target_page_id,
            "target_filename": target_filename,
            "reason": reason,
            "interaction_contract": interaction_contract,
            "visible_binding": visible_binding,
            "operation_binding": operation_binding,
        }
        return continuation

    def _build_interaction_candidate_record(
        self,
        *,
        continuation: Dict[str, Any],
        source_page: Optional[PagePrototype] = None,
        target_page: Optional[PagePrototype] = None,
    ) -> Dict[str, Any]:
        contract = continuation.get("interaction_contract", {}) if isinstance(continuation.get("interaction_contract", {}), dict) else {}
        evidence = contract.get("evidence", {}) if isinstance(contract.get("evidence", {}), dict) else {}
        field_contract = contract.get("field_contract", {}) if isinstance(contract.get("field_contract", {}), dict) else {}
        return {
            "format": "interaction_candidate",
            "candidate_id": str(continuation.get("candidate_id", "") or ""),
            "source_page_id": str(continuation.get("source_page_id", "") or evidence.get("source_page_id", "") or ""),
            "source_page_name": getattr(source_page, "page_name_full", "") or getattr(source_page, "page_name", "") or "",
            "source_page_type": getattr(source_page, "page_type", "") or "",
            "source_filename": getattr(source_page, "filename", "") or "",
            "element_name": str(continuation.get("element_name", "") or ""),
            "display_text": str(continuation.get("display_text", "") or ""),
            "element_type": str(continuation.get("element_type", "") or ""),
            "continuation_kind": str(continuation.get("continuation_kind", "") or ""),
            "target_page_id": str(continuation.get("target_page_id", "") or ""),
            "target_page_name": getattr(target_page, "page_name_full", "") or getattr(target_page, "page_name", "") or "",
            "target_page_type": getattr(target_page, "page_type", "") or evidence.get("target_page_type", "") or "",
            "target_filename": str(continuation.get("target_filename", "") or ""),
            "reason": str(continuation.get("reason", "") or ""),
            "evidence": {
                "trigger_text": evidence.get("trigger_text", continuation.get("display_text", "")),
                "task_instruction": evidence.get("task_instruction", ""),
                "current_description": evidence.get("current_description", ""),
                "next_description": evidence.get("next_description", ""),
                "lifecycle_hint": evidence.get("lifecycle_hint", ""),
            },
            "field_contract": field_contract,
        }

    @staticmethod
    def _merge_contract_dict(default: Dict[str, Any], value: Any) -> Dict[str, Any]:
        merged = dict(default)
        if isinstance(value, dict):
            for key, item in value.items():
                if item is not None:
                    merged[key] = item
        return merged

    @staticmethod
    def _contract_match_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    def _state_ref_candidate_score(self, state: Dict[str, Any], text: str) -> Tuple[int, List[str]]:
        state_id = str(state.get("state_id", "") or "").strip()
        entity_name = str(state.get("entity_name", "") or state.get("entity_ref", "") or "").strip()
        state_type = str(state.get("state_type", "") or "").strip()
        state_text = self._contract_match_text(" ".join([state_id, entity_name, state_type]))
        match_text = self._contract_match_text(text)
        score = 0
        reasons: List[str] = []

        state_id_norm = state_id.lower()
        entity_norm = entity_name.lower()
        if state_id_norm and state_id_norm in match_text:
            score += 100
            reasons.append("state_id_exact_text_match")
        if entity_norm and entity_norm in match_text:
            score += 80
            reasons.append("entity_name_exact_text_match")

        if score <= 0:
            return 0, []
        return score, reasons

    def _attach_state_ref_candidates_to_interaction_candidates(
        self,
        candidates: List[Dict[str, Any]],
        content: "SiteContent",
    ) -> List[Dict[str, Any]]:
        states = (content.task_state_contract or {}).get("states", []) if content else []
        if not isinstance(states, list) or not states:
            return [dict(candidate) for candidate in candidates]

        enriched: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            text_parts = [
                candidate.get("source_page_name", ""),
                candidate.get("source_page_type", ""),
                candidate.get("element_name", ""),
                candidate.get("display_text", ""),
                candidate.get("continuation_kind", ""),
                candidate.get("target_page_name", ""),
                candidate.get("target_page_type", ""),
                candidate.get("target_filename", ""),
                candidate.get("reason", ""),
            ]
            evidence = candidate.get("evidence", {}) if isinstance(candidate.get("evidence", {}), dict) else {}
            text_parts.extend([
                evidence.get("trigger_text", ""),
                evidence.get("task_instruction", ""),
                evidence.get("current_description", ""),
                evidence.get("next_description", ""),
                evidence.get("lifecycle_hint", ""),
            ])
            match_text = " ".join(str(part or "") for part in text_parts)
            scored: List[Dict[str, Any]] = []
            for state in states:
                if not isinstance(state, dict):
                    continue
                state_id = str(state.get("state_id", "") or "").strip()
                if not state_id:
                    continue
                score, reasons = self._state_ref_candidate_score(state, match_text)
                if score <= 0:
                    continue
                scored.append({
                    "state_ref": state_id,
                    "entity_name": str(state.get("entity_name", "") or state.get("entity_ref", "") or "").strip(),
                    "state_type": str(state.get("state_type", "") or "").strip(),
                    "score": score,
                    "reasons": reasons,
                })
            scored.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("state_ref", ""))))
            item = dict(candidate)
            if scored:
                item["state_ref_candidates"] = scored[:5]
            enriched.append(item)
        return enriched

    def _build_runtime_state_interaction_candidates(
        self,
        runtime_state_observations: Dict[str, Any],
        page_prototypes: List[PagePrototype],
    ) -> List[Dict[str, Any]]:
        if not isinstance(runtime_state_observations, dict):
            return []
        observations = runtime_state_observations.get("observations", [])
        if not isinstance(observations, list):
            return []

        pages_by_cluster: Dict[str, List[PagePrototype]] = {}
        pages_by_materialized_id: Dict[str, List[PagePrototype]] = {}
        for page in page_prototypes or []:
            structure = page.structure_data if isinstance(page.structure_data, dict) else {}
            materialized_id = self._materialized_page_id_for_runtime_state_page(page)
            if materialized_id:
                pages_by_materialized_id.setdefault(materialized_id, []).append(page)
            for key in ("source_cluster_id", "cluster_id", "general_page_id"):
                cluster_id = str(structure.get(key, "") or "").strip()
                if cluster_id:
                    pages_by_cluster.setdefault(cluster_id, []).append(page)

        materialized_view = {}
        if isinstance(runtime_state_observations.get("derived", {}), dict):
            materialized_view = runtime_state_observations.get("derived", {}).get("materialized_semantic_view", {})
        observations_by_obs_key = (
            materialized_view.get("observations_by_obs_key", {})
            if isinstance(materialized_view, dict)
            else {}
        )
        obligations_by_observation_id = {}
        if isinstance(runtime_state_observations.get("derived", {}), dict):
            obligations_artifact = runtime_state_observations.get("derived", {}).get("runtime_state_obligations", {})
            obligations_by_observation_id = (
                obligations_artifact.get("obligations_by_observation_id", {})
                if isinstance(obligations_artifact, dict)
                else {}
            )

        candidates: List[Dict[str, Any]] = []
        seen = set()
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            scope = str(obs.get("state_scope", "") or obs.get("scope", "") or "").strip()
            if not scope:
                continue
            cluster_id = str(obs.get("page_cluster_id", "") or "").strip()
            obs_key = str(obs.get("obs_key", "") or "").strip()
            semantic_match = {}
            semantic_matches = observations_by_obs_key.get(obs_key, []) if isinstance(observations_by_obs_key, dict) else []
            if isinstance(semantic_matches, list):
                observation_id = str(obs.get("observation_id", "") or "").strip()
                for item in semantic_matches:
                    if not isinstance(item, dict):
                        continue
                    if observation_id and str(item.get("observation_id", "") or "").strip() != observation_id:
                        continue
                    semantic_match = item
                    break
                if not semantic_match:
                    semantic_match = next((item for item in semantic_matches if isinstance(item, dict)), {})
            materialized_page_id = str(
                semantic_match.get("materialized_page_id", "")
                if isinstance(semantic_match, dict)
                else ""
            ).strip()
            pages = pages_by_materialized_id.get(materialized_page_id, []) if materialized_page_id else []
            if not pages and isinstance(semantic_match, dict):
                cluster_id = str(semantic_match.get("general_page_id", "") or cluster_id).strip()
            if not pages:
                pages = pages_by_cluster.get(cluster_id, [])
            if not pages and page_prototypes:
                pages = [page_prototypes[0]]
            if not pages:
                continue
            page = pages[0]
            observation_id = str(obs.get("observation_id", "") or "").strip()
            trace_id = str(obs.get("trace_id", "") or "").strip()
            step_index = obs.get("step_index", "")
            dedupe_key = (
                trace_id,
                str(step_index),
                page.page_id,
                scope,
                str(obs.get("mutation", "") or ""),
                observation_id,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            seed = {
                "source": "runtime_state_observation",
                "trace_id": trace_id,
                "step_index": step_index,
                "page_id": page.page_id,
                "scope": scope,
                "mutation": str(obs.get("mutation", "") or ""),
                "observation_id": observation_id,
            }
            candidate_id = self._stable_short_hash(seed, "rsobs")
            raw_evidence = str(obs.get("raw_evidence", "") or "")
            obligation_ids = list(obs.get("runtime_state_obligation_ids", [])) if isinstance(obs.get("runtime_state_obligation_ids", []), list) else []
            if not obligation_ids and observation_id:
                for obligation in obligations_by_observation_id.get(observation_id, []) if isinstance(obligations_by_observation_id, dict) else []:
                    if isinstance(obligation, dict) and str(obligation.get("obligation_id", "") or ""):
                        obligation_ids.append(str(obligation.get("obligation_id", "")))
            resolved_materialized_page_id = (
                materialized_page_id or self._materialized_page_id_for_runtime_state_page(page)
            )
            candidate = {
                "candidate_id": candidate_id,
                "source": "runtime_state_observation",
                "source_page_id": page.page_id,
                "source_page_name": page.page_name_full or page.page_name,
                "source_page_type": page.page_type,
                "source_filename": page.filename,
                "element_name": "runtime_state:{}".format(scope),
                "display_text": raw_evidence[:160] or "{} runtime state".format(scope),
                "element_type": "runtime_state_observation",
                "continuation_kind": "stateful_action",
                "target_page_id": "",
                "target_page_name": "",
                "target_page_type": "",
                "target_filename": "",
                "reason": "runtime_state_observation",
                "trace_id": trace_id,
                "step_index": step_index,
                "obs_key": obs_key,
                "page_cluster_id": cluster_id,
                "materialized_page_id": resolved_materialized_page_id,
                "materialized_page_key": (page.structure_data or {}).get("instance_key", ""),
                "runtime_state_observation_ids": [observation_id] if observation_id else [],
                "runtime_state_obligation_ids": [str(item) for item in obligation_ids if str(item)],
                "state_scope_candidate": scope,
                "lifecycle": str(obs.get("lifecycle", "") or ""),
                "mutation": str(obs.get("mutation", "") or ""),
                "raw_evidence": raw_evidence,
                "negative_constraints": list(obs.get("negative_constraints", [])) if isinstance(obs.get("negative_constraints", []), list) else [],
                "value_hints": list(obs.get("value_hints", [])) if isinstance(obs.get("value_hints", []), list) else [],
                "state_ref_candidates": list(obs.get("state_ref_candidates", [])) if isinstance(obs.get("state_ref_candidates", []), list) else [],
            }


            mid_value = str(resolved_materialized_page_id or "").strip()
            source_step_value = str(obs_key or "").strip()
            if mid_value and source_step_value:
                attach_provenance(
                    candidate,
                    Provenance(mid=mid_value, source_step=source_step_value),
                )
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _materialized_page_id_for_runtime_state_page(page: PagePrototype) -> str:
        structure = page.structure_data if isinstance(page.structure_data, dict) else {}
        explicit = str(
            structure.get("materialized_page_id", "")
            or structure.get("page_cluster_id", "")
            or ""
        ).strip()
        if explicit:
            return explicit
        general_id = str(
            structure.get("source_cluster_id", "")
            or structure.get("general_page_id", "")
            or structure.get("cluster_id", "")
            or ""
        ).strip()
        instance_key = str(structure.get("instance_key", "") or "").strip()
        if general_id and instance_key:
            return "{}::{}".format(general_id, instance_key)
        return general_id

    def _contract_has_hydration_ref(self, contract: Dict[str, Any]) -> bool:
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        return bool(
            str(hydration.get("state_ref", "") or "").strip()
            or str(contract.get("state_ref", "") or "").strip()
            or str(contract.get("entity_ref", "") or "").strip()
        )

    def _bind_missing_hydration_ref_from_candidates(
        self,
        contract: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> None:
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        if not bool(hydration.get("required", False)) or self._contract_has_hydration_ref(contract):
            return
        candidates = candidate.get("state_ref_candidates", []) if isinstance(candidate, dict) else []
        if not candidates and isinstance(contract.get("candidate", {}), dict):
            candidates = contract.get("candidate", {}).get("state_ref_candidates", [])
        if not isinstance(candidates, list) or not candidates:
            return
        for item in candidates:
            if not isinstance(item, dict):
                continue
            state_ref = str(item.get("state_ref", "") or "").strip()
            if not state_ref:
                continue
            contract["state_ref"] = state_ref
            contract["hydration"]["state_ref"] = state_ref
            reasons = item.get("reasons", []) if isinstance(item.get("reasons", []), list) else []
            if self.logger:
                self.logger.log_info(
                    "[TaskInteraction] Bound missing hydration state_ref={} for candidate {} ({})".format(
                        state_ref,
                        contract.get("candidate_id", ""),
                        ",".join(str(reason) for reason in reasons[:3]),
                    )
            )
            return

    def _bind_missing_identity_ref_from_candidates(
        self,
        contract: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> None:
        if self._contract_has_hydration_ref(contract):
            return
        if not self._l0_contract_requires_identity_grounding(contract):
            return
        candidates = candidate.get("state_ref_candidates", []) if isinstance(candidate, dict) else []
        if not candidates and isinstance(contract.get("candidate", {}), dict):
            candidates = contract.get("candidate", {}).get("state_ref_candidates", [])
        if not isinstance(candidates, list) or not candidates:
            return
        for item in candidates:
            if not isinstance(item, dict):
                continue
            state_ref = str(item.get("state_ref", "") or "").strip()
            if not state_ref:
                continue
            contract["state_ref"] = state_ref
            hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
            if bool(hydration.get("required", False)):
                hydration["state_ref"] = state_ref
                contract["hydration"] = hydration
            reasons = item.get("reasons", []) if isinstance(item.get("reasons", []), list) else []
            if self.logger:
                self.logger.log_info(
                    "[TaskInteraction] Bound missing identity state_ref={} for candidate {} ({})".format(
                        state_ref,
                        contract.get("candidate_id", ""),
                        ",".join(str(reason) for reason in reasons[:3]),
                    )
                )
            return

    def _repair_non_data_navigation_hydration(self, contract: Dict[str, Any]) -> None:
        return

    def _normalize_task_interaction_contract(
        self,
        raw_contract: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidate_id = str(candidate.get("candidate_id", "") or raw_contract.get("candidate_id", "") or "")
        contract = self._empty_task_interaction_contract(candidate_id=candidate_id)
        contract["planner_source"] = str(raw_contract.get("planner_source", "model_planner") or "model_planner")
        for key in (
            "action_semantic",
            "interaction_outcome",
            "state_scope",
            "state_provenance",
            "source_lifecycle",
            "state_ref",
            "entity_ref",
            "state_role",
            "interaction_ref",
            "unresolved_reason",
            "rationale",
        ):
            value = raw_contract.get(key)
            if value is not None:
                contract[key] = str(value)
        try:
            contract["confidence"] = float(raw_contract.get("confidence", contract["confidence"]))
        except Exception:
            contract["confidence"] = 0.0
        contract["route_binding"] = self._merge_contract_dict(
            self._empty_route_binding(),
            raw_contract.get("route_binding", {}),
        )
        contract["hydration"] = self._merge_contract_dict(
            self._empty_hydration(),
            raw_contract.get("hydration", {}),
        )
        contract["sdk_effect"] = self._merge_contract_dict(
            self._empty_sdk_effect(),
            raw_contract.get("sdk_effect", {}),
        )
        if isinstance(raw_contract.get("value_shape", {}), dict):
            contract["value_shape"] = dict(raw_contract.get("value_shape", {}))
        if isinstance(raw_contract.get("identity_evidence", []), list):
            contract["identity_evidence"] = [
                str(item)
                for item in raw_contract.get("identity_evidence", [])
                if str(item)
            ]
        observation_ids = raw_contract.get("runtime_state_observation_ids", candidate.get("runtime_state_observation_ids", []))
        if isinstance(observation_ids, list):
            contract["runtime_state_observation_ids"] = [str(item) for item in observation_ids if str(item)]
        obligation_ids = raw_contract.get("runtime_state_obligation_ids", candidate.get("runtime_state_obligation_ids", []))
        if isinstance(obligation_ids, list):
            contract["runtime_state_obligation_ids"] = [str(item) for item in obligation_ids if str(item)]
        terminal_constraints = raw_contract.get("terminal_constraints", candidate.get("negative_constraints", []))
        if isinstance(terminal_constraints, list):
            contract["terminal_constraints"] = [str(item) for item in terminal_constraints if str(item)]
        if (
            str(contract["hydration"].get("mode", "") or "").strip() == "preserve_stack"
            and str(contract["hydration"].get("source_ref", "") or "").strip() == "session"
            and not self._contract_has_hydration_ref(contract)
        ):


            contract["hydration"]["required"] = False
        self._bind_missing_hydration_ref_from_candidates(contract, candidate)
        self._bind_missing_identity_ref_from_candidates(contract, candidate)
        self._repair_non_data_navigation_hydration(contract)
        candidate_field_contract = candidate.get("field_contract", {}) if isinstance(candidate.get("field_contract", {}), dict) else {}
        raw_field_contract = raw_contract.get("field_contract", {}) if isinstance(raw_contract.get("field_contract", {}), dict) else {}
        field_contract = {"observed_fields": [], "typed_fields": [], "unobserved_required_fields": []}
        for source in (candidate_field_contract, raw_field_contract):
            for field_key in field_contract:
                for value in source.get(field_key, []) if isinstance(source.get(field_key, []), list) else []:
                    if value not in field_contract[field_key]:
                        field_contract[field_key].append(value)
        contract["field_contract"] = field_contract
        self._scope_l0_field_obligations(contract)
        contract["candidate"] = {
            key: candidate.get(key)
            for key in (
                "source_page_id",
                "source_page_name",
                "source_page_type",
                "source_filename",
                "element_name",
                "display_text",
                "element_type",
                "continuation_kind",
                "target_page_id",
                "target_page_name",
                "target_page_type",
                "target_filename",
                "reason",
                "state_ref_candidates",
                "runtime_state_observation_ids",
                "runtime_state_obligation_ids",
                "state_scope_candidate",
                "lifecycle",
                "mutation",
                "negative_constraints",
            )
        }


        candidate_provenance = candidate.get("provenance")
        if isinstance(candidate_provenance, dict):
            contract["candidate"]["provenance"] = dict(candidate_provenance)
        if (
            contract["action_semantic"] == "edit_existing_record"
            and contract.get("state_scope") == "draft"
            and contract.get("state_provenance") == "current_draft"
        ):
            contract["action_semantic"] = "update_current_draft"
        contract_id_seed = {
            "candidate_id": candidate_id,
            "action_semantic": contract["action_semantic"],
            "target_page_id": candidate.get("target_page_id", ""),
            "target_filename": candidate.get("target_filename", ""),
        }
        contract["contract_id"] = str(raw_contract.get("contract_id", "") or self._stable_short_hash(contract_id_seed, "ticon"))
        return contract

    def _build_task_interaction_planner_prompt(
        self,
        *,
        candidates: List[Dict[str, Any]],
        page_context: List[Dict[str, Any]],
        content: "SiteContent",
        runtime_state_observations: Dict[str, Any] = None,
    ) -> str:
        identity_grounding_pack = self._build_task_interaction_identity_grounding_pack(
            candidates,
            content,
            runtime_state_observations or {},
        ) if self._l0_schema_registry_flow_enabled() else {}
        schema_context = self._build_task_interaction_schema_context_for_chunk(
            candidates=candidates,
            content=content,
            identity_grounding_pack=identity_grounding_pack,
        )
        planner_candidates = (
            [
                self._compact_task_interaction_candidate_for_planner(candidate)
                for candidate in candidates
                if isinstance(candidate, dict)
            ]
            if self._l0_schema_registry_flow_enabled()
            else candidates
        )
        compact_runtime_observations = []
        observations_by_obs_key = {}
        wanted_observation_ids, wanted_obs_keys = self._task_interaction_candidate_runtime_keys(candidates)
        if isinstance(runtime_state_observations, dict):
            materialized_view = runtime_state_observations.get("derived", {}).get("materialized_semantic_view", {}) if isinstance(runtime_state_observations.get("derived", {}), dict) else {}
            observations_by_obs_key = (
                materialized_view.get("observations_by_obs_key", {})
                if isinstance(materialized_view, dict)
                else {}
            )
        if isinstance(runtime_state_observations, dict):
            for item in runtime_state_observations.get("observations", []):
                if not isinstance(item, dict):
                    continue
                observation_id = str(item.get("observation_id", "") or "")
                obs_key = str(item.get("obs_key", "") or "")
                if wanted_observation_ids or wanted_obs_keys:
                    if observation_id not in wanted_observation_ids and obs_key not in wanted_obs_keys:
                        continue
                obs_key = str(item.get("obs_key", "") or "")
                materialized_item = {}
                materialized_matches = observations_by_obs_key.get(obs_key, []) if isinstance(observations_by_obs_key, dict) else []
                if isinstance(materialized_matches, list):
                    for candidate in materialized_matches:
                        if not isinstance(candidate, dict):
                            continue
                        if observation_id and str(candidate.get("observation_id", "") or "") != observation_id:
                            continue
                        materialized_item = candidate
                        break
                    if not materialized_item:
                        materialized_item = next((candidate for candidate in materialized_matches if isinstance(candidate, dict)), {})
                compact_runtime_observations.append({
                    "observation_id": item.get("observation_id", ""),
                    "trace_id": item.get("trace_id", ""),
                    "step_index": item.get("step_index", ""),
                    "page_cluster_id": item.get("page_cluster_id", ""),
                    "materialized_page_id": materialized_item.get("materialized_page_id", "") if isinstance(materialized_item, dict) else "",
                    "general_page_id": materialized_item.get("general_page_id", "") if isinstance(materialized_item, dict) else "",
                    "instance_key": materialized_item.get("instance_key", "") if isinstance(materialized_item, dict) else "",
                    "scope": item.get("scope", ""),
                    "lifecycle": item.get("lifecycle", ""),
                    "mutation": item.get("mutation", ""),
                    "negative_constraints": item.get("negative_constraints", []),
                    "value_hints": item.get("value_hints", []),
                })
                if len(compact_runtime_observations) >= 96:
                    break
        output_schema = {
            "contracts": [
                {
                    "candidate_id": "copy from input",
                    "action_semantic": "open_collection|navigate_shell|open_detail|edit_existing_record|create_record_draft|update_current_draft|select_candidate|select_existing_option|update_filter|append_accumulator|commit_transaction|inline_ui_state|read_only|unknown",
                    "interaction_outcome": "short normalized outcome",
                    "state_scope": "none|collection|record|draft|selection|session|accumulator|transaction|inline_ui",
                    "state_provenance": "none|clicked_record|existing_record|current_record|current_draft|candidate_result|session|generated_by_action",
                    "source_lifecycle": "none|collection|existing_record|new_draft|selected_option|session_context|accumulator|transaction",
                    "state_ref": "state id from task_state_contract when applicable, otherwise empty",
                    "entity_ref": "entity name when applicable, otherwise empty",
                    "state_role": "current_draft|selected_record|collection_state|selection_state|accumulator_state|transaction_state|ui_state|none",
                    "value_shape": {
                        "state_type": "",
                        "entity_name": "",
                        "role": ""
                    },
                    "identity_evidence": [],
                    "interaction_ref": "interaction/action name from site-level interaction_contract when applicable, otherwise empty",
                    "route_binding": {
                        "requires_params": False,
                        "params": [],
                        "source": "",
                        "preserve_existing": False,
                        "fallback": ""
                    },
                    "hydration": {
                        "required": False,
                        "mode": "",
                        "state_ref": "",
                        "source_ref": "",
                        "fallback": "",
                        "bind_fields_from_state": False
                    },
                    "sdk_effect": {
                        "trigger": "none",
                        "effect": "none|create|update|select|apply|append|submit|navigate|toggle",
                        "method": ""
                    },
                    "field_contract": {
                        "observed_fields": [],
                        "typed_fields": [],
                        "unobserved_required_fields": []
                    },
                    "runtime_state_observation_ids": [],
                    "runtime_state_obligation_ids": [],
                    "terminal_constraints": [],
                    "unresolved_reason": "required when an executable/stateful contract cannot be grounded to a resolvable state_ref or entity_ref",
                    "rationale": "short Chinese rationale if source UI text is Chinese",
                    "confidence": 0.0
                }
            ]
        }
        return (
            "You are the AppSpec task interaction contract planner.\n"
            "Classify each deterministic interaction candidate into exactly one canonical task_interaction_contract.\n"
            "The deterministic candidate builder only says which actions require judgment; you must decide state, route binding, hydration, and SDK effect.\n\n"
            "Hard rules:\n"
            "- Return one contract for every input candidate_id; do not omit candidates.\n"
            "- Return only the canonical task_interaction_contract fields shown in the output schema.\n"
            "- If an input candidate has state_ref_candidates, those candidates came from the reference-trace semantic artifact; choose one when hydration or runtime state binding is needed.\n"
            "- hydration.required=true implies hydration.state_ref, top-level state_ref, or entity_ref must be non-empty and resolvable. Do not set hydration.required=true with only a free-text source_ref.\n"
            "- source_ref is descriptive evidence only; it is not a resolvable state binding. Use state_ref for executable binding.\n"
            "- In l0_schema_registry flow, every non-empty state_ref/entity_ref must resolve through schema_registry. Use state_ref for runtime/draft/UI state and entity_ref for durable catalog records when possible.\n"
            "- Stateful/executable contracts with state_scope in record|draft|transaction|selection|accumulator|collection, route_binding.requires_params=true, or hydration.required=true must provide state_ref or entity_ref or unresolved_reason. If reference-trace/schema_registry evidence cannot ground the identity, fill unresolved_reason instead of guessing from business text.\n"
            "- Use the State Identity Grounding Pack as the per-candidate namespace for runtime evidence and registry candidates. Prefer registry_candidates and state_ref_candidates over free-text labels.\n"
            "- When choosing identity, populate state_role, value_shape, and identity_evidence with the chosen registry/evidence ids.\n"
            "- Use route_binding only when the target page needs an identity parameter from the clicked/current record.\n"
            "- open_collection and navigate_shell must not hydrate a clicked/existing specific record unless the reference-trace semantic artifact explicitly says the navigation continues that state.\n"
            "- create_record_draft for a new blank record must not require an existing id. A derived draft created from a clicked/current/existing source record may require a source/parent/current record param, but must mark source_lifecycle/state_provenance/route_binding.source and include unresolved_reason if the source identity is not grounded.\n"
            "- Use edit_existing_record only when selecting/opening an existing record for edit mode; it may require clicked_record/existing_record route binding and hydration.\n"
            "- Use update_current_draft for typing, deleting, toggling, or selecting fields inside the current draft/form, even when the draft originated from an existing record.\n"
            "- Back/close navigation that preserves browser/app stack is route_binding.preserve_existing=true plus sdk_effect.effect=navigate; do not mark it as hydration.required unless a concrete state_ref/entity_ref is needed.\n"
            "- A selection from search results is select_candidate/current_draft unless it is explicitly a saved existing record.\n"
            "- Candidates with runtime_state_observation_ids are backed by model-extracted reference trace semantics. Preserve those ids and carry their explicit negative_constraints into terminal_constraints.\n"
            "- Preserve runtime_state_obligation_ids whenever they are present on candidates; these ids are the executable obligations derived from the reference-trace semantic artifact.\n"
            "- Prefer finite state signatures over screenshot-specific content. Reference task_state_contract/entity_schemas by id/name; do not copy full data items.\n\n"
            "- unobserved_required_fields must stay scoped to same form/draft create/update/commit actions. Do not attach invoice/address/search form fields to navigation, read-only, open_collection, or accumulator actions.\n\n"
            "- Optimized-flow candidates are compact views: frame descriptions are intentionally omitted; rely on trigger_text, task_instruction, page context, field contract, state refs, and runtime ids.\n\n"
            "Materialized pages:\n```json\n{}\n```\n\n"
            "Schema and state context:\n```json\n{}\n```\n\n"
            "Runtime state observations:\n```json\n{}\n```\n\n"
            "State Identity Grounding Pack:\n```json\n{}\n```\n\n"
            "Interaction candidates:\n```json\n{}\n```\n\n"
            "Return JSON only with this shape:\n```json\n{}\n```"
        ).format(
            json.dumps(page_context, indent=2, ensure_ascii=False),
            json.dumps(schema_context, indent=2, ensure_ascii=False),
            json.dumps(compact_runtime_observations, indent=2, ensure_ascii=False),
            json.dumps(identity_grounding_pack, indent=2, ensure_ascii=False),
            json.dumps(planner_candidates, indent=2, ensure_ascii=False),
            json.dumps(output_schema, indent=2, ensure_ascii=False),
        )

    def _load_cached_task_interaction_contracts(
        self,
        output_dir: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        current_candidate_ids = {
            str(candidate.get("candidate_id", "") or "")
            for candidate in candidates or []
            if isinstance(candidate, dict) and str(candidate.get("candidate_id", "") or "")
        }
        candidate_by_id = {
            str(candidate.get("candidate_id", "") or ""): candidate
            for candidate in candidates or []
            if isinstance(candidate, dict) and str(candidate.get("candidate_id", "") or "")
        }
        result = {
            "contracts": [],
            "planner_outputs": [],
            "cached_candidate_ids": set(),
            "stale_candidate_ids": [],
        }
        if not output_dir:
            return result
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        path = pom.debug("consolidation", "task_interaction_contract_planner.json")
        if not os.path.exists(path):
            return result
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return result

        cached_contract_items: List[Tuple[str, Dict[str, Any]]] = []
        contracts_by_candidate_id = payload.get("contracts_by_candidate_id", {})
        if isinstance(contracts_by_candidate_id, dict) and contracts_by_candidate_id:
            for candidate_id, contract in contracts_by_candidate_id.items():
                if isinstance(contract, dict):
                    cached_contract_items.append((str(candidate_id or ""), contract))
        else:
            for contract in payload.get("contracts", []) if isinstance(payload.get("contracts", []), list) else []:
                if isinstance(contract, dict):
                    cached_contract_items.append((str(contract.get("candidate_id", "") or ""), contract))

        for candidate_id, contract in cached_contract_items:
            if not isinstance(contract, dict):
                continue
            if not candidate_id:
                continue
            if candidate_id not in current_candidate_ids:
                result["stale_candidate_ids"].append(candidate_id)
                continue
            contract = json.loads(json.dumps(contract, ensure_ascii=False))
            contract["candidate_id"] = candidate_id
            candidate = candidate_by_id.get(candidate_id, {})
            self._bind_missing_hydration_ref_from_candidates(contract, candidate)
            self._bind_missing_identity_ref_from_candidates(contract, candidate)
            self._repair_non_data_navigation_hydration(contract)
            for key in ("runtime_state_observation_ids", "runtime_state_obligation_ids"):
                candidate_ids = candidate.get(key, []) if isinstance(candidate, dict) else []
                if not isinstance(candidate_ids, list):
                    continue
                existing = contract.setdefault(key, [])
                if not isinstance(existing, list):
                    existing = []
                    contract[key] = existing
                for item in candidate_ids:
                    value = str(item)
                    if value and value not in existing:
                        existing.append(value)
            candidate_constraints = candidate.get("negative_constraints", []) if isinstance(candidate, dict) else []
            if isinstance(candidate_constraints, list):
                existing_constraints = contract.setdefault("terminal_constraints", [])
                if not isinstance(existing_constraints, list):
                    existing_constraints = []
                    contract["terminal_constraints"] = existing_constraints
                for item in candidate_constraints:
                    value = str(item)
                    if value and value not in existing_constraints:
                        existing_constraints.append(value)
            result["contracts"].append(contract)
            result["cached_candidate_ids"].add(candidate_id)
        planner_outputs = payload.get("planner_outputs", [])
        if isinstance(planner_outputs, list):
            result["planner_outputs"] = planner_outputs
        return result

    async def _plan_task_interaction_contracts_with_model(
        self,
        *,
        candidates: List[Dict[str, Any]],
        page_prototypes: List[PagePrototype],
        content: "SiteContent",
        output_dir: str = "",
        runtime_state_observations: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        runtime_candidates = self._build_runtime_state_interaction_candidates(
            runtime_state_observations or {},
            page_prototypes,
        )
        if runtime_candidates:
            existing_ids = {str(candidate.get("candidate_id", "") or "") for candidate in candidates if isinstance(candidate, dict)}
            candidates = list(candidates or []) + [
                candidate for candidate in runtime_candidates
                if str(candidate.get("candidate_id", "") or "") not in existing_ids
            ]
        admission_artifact = None
        if self._l0_schema_registry_flow_enabled():
            admission_artifact = self._admit_task_interaction_candidates(candidates)
            candidates = admission_artifact.get("admitted_candidates", [])
            if output_dir:
                self._save_task_interaction_debug(
                    "task_interaction_contract_admission.json",
                    admission_artifact,
                    output_dir,
                )
        if not candidates:
            artifact = {
                "format": "task_interaction_contracts",
                "schema_revision": 1,
                "contracts": [],
                "contracts_by_candidate_id": {},
                "planner_outputs": [],
            }
            if admission_artifact:
                artifact["admission"] = {
                    "candidate_count": admission_artifact.get("candidate_count", 0),
                    "admitted_count": admission_artifact.get("admitted_count", 0),
                    "dropped_count": admission_artifact.get("dropped_count", 0),
                }
            return artifact
        candidates = self._attach_state_ref_candidates_to_interaction_candidates(candidates, content)
        page_context = [
            {
                "page_id": page.page_id,
                "page_name": page.page_name_full or page.page_name,
                "page_type": page.page_type,
                "filename": page.filename,
                "general_page_id": (page.structure_data or {}).get("source_cluster_id") or (page.structure_data or {}).get("cluster_id", ""),
                "materialized_page_key": (page.structure_data or {}).get("instance_key", ""),
                "description": page.description,
            }
            for page in page_prototypes
        ]
        cached = self._load_cached_task_interaction_contracts(output_dir, candidates)
        cached_candidate_ids = set(cached.get("cached_candidate_ids", set()))
        candidates_to_plan = [
            candidate for candidate in candidates
            if str(candidate.get("candidate_id", "") or "") not in cached_candidate_ids
        ]
        if self.logger and cached_candidate_ids:
            self.logger.log_info(
                "[TaskInteraction] Reused {} cached contracts; planning {} missing candidates".format(
                    len(cached_candidate_ids),
                    len(candidates_to_plan),
                )
            )
        representative_artifact = None
        planning_candidates = candidates_to_plan
        representative_alias_map: Dict[str, List[str]] = {}
        if self._l0_schema_registry_flow_enabled():
            representative_artifact = self._build_task_interaction_planning_representatives(candidates_to_plan)
            planning_candidates = representative_artifact.get("representatives", [])
            representative_alias_map = representative_artifact.get("alias_map", {})
            if output_dir:
                self._save_task_interaction_debug(
                    "task_interaction_contract_representatives.json",
                    {
                        key: value
                        for key, value in representative_artifact.items()
                        if key != "representatives"
                    },
                    output_dir,
                )
            if self.logger:
                self.logger.log_info(
                    "[TaskInteraction] Optimized planner representatives: {} candidates -> {} representatives ({} aliases merged)".format(
                        len(candidates_to_plan),
                        len(planning_candidates),
                        representative_artifact.get("merged_alias_count", 0),
                    )
                )
        chunk_size = (
            int(getattr(self, "task_interaction_contract_planner_chunk_size", 16) or 16)
            if self._l0_schema_registry_flow_enabled()
            else 32
        )
        chunk_size = max(1, min(chunk_size, 64))
        chunks = [planning_candidates[idx:idx + chunk_size] for idx in range(0, len(planning_candidates), chunk_size)]
        async def _plan_chunk(index: int, chunk: List[Dict[str, Any]]) -> Dict[str, Any]:
            scoped_page_context = self._scope_task_interaction_page_context(chunk, page_context)
            prompt = self._build_task_interaction_planner_prompt(
                candidates=chunk,
                page_context=scoped_page_context,
                content=content,
                runtime_state_observations=runtime_state_observations or {},
            )
            async def _call_planner() -> Tuple[Any, Any]:
                return await call_openai_api_json_async(
                    [{"role": "user", "content": prompt}],
                    reasoning_effort=getattr(
                        self,
                        "task_interaction_contract_planner_reasoning_effort",
                        self.reasoning_effort,
                    ),
                    model=getattr(self, "task_interaction_contract_planner_model", None) or self.model,
                    stage="task_interaction_contract_planner",
                )
            raw, _usage = await _call_planner()
            if isinstance(raw, str):
                raw = json.loads(raw)
            if not isinstance(raw, dict):
                raise RuntimeError("task_interaction_contract planner returned non-object JSON")
            return {
                "chunk_index": index,
                "candidate_ids": [item.get("candidate_id", "") for item in chunk],
                "page_context_count": len(scoped_page_context),
                "raw_output": raw,
            }

        planner_outputs = []
        if chunks:
            planner_outputs = await asyncio.gather(*[
                _plan_chunk(index, chunk)
                for index, chunk in enumerate(chunks)
            ])
        candidate_by_id = {
            str(candidate.get("candidate_id", "") or ""): candidate
            for candidate in candidates
            if str(candidate.get("candidate_id", "") or "")
        }
        planning_candidate_by_id = {
            str(candidate.get("candidate_id", "") or ""): candidate
            for candidate in planning_candidates
            if str(candidate.get("candidate_id", "") or "")
        }
        candidate_to_representative: Dict[str, str] = {}
        for representative_id, alias_ids in (representative_alias_map or {}).items():
            representative_value = str(representative_id or "")
            if not representative_value:
                continue
            if isinstance(alias_ids, list):
                for alias_id in alias_ids:
                    alias_value = str(alias_id or "")
                    if alias_value:
                        candidate_to_representative[alias_value] = representative_value
            candidate_to_representative.setdefault(representative_value, representative_value)
        contracts: List[Dict[str, Any]] = list(cached.get("contracts", []))
        seen_candidate_ids = set(cached_candidate_ids)

        def _append_planner_contracts(outputs: List[Dict[str, Any]]) -> None:
            for output in outputs or []:
                raw_output = output.get("raw_output", {}) if isinstance(output, dict) else {}
                raw_contracts = raw_output.get("contracts", []) if isinstance(raw_output, dict) else []
                if not isinstance(raw_contracts, list):
                    raw_contracts = []
                for raw_contract in raw_contracts:
                    if not isinstance(raw_contract, dict):
                        continue
                    representative_candidate_id = str(raw_contract.get("candidate_id", "") or "")
                    alias_candidate_ids = representative_alias_map.get(
                        representative_candidate_id,
                        [representative_candidate_id],
                    )
                    for candidate_id in alias_candidate_ids:
                        if candidate_id in seen_candidate_ids:
                            continue
                        candidate = candidate_by_id.get(candidate_id)
                        if not candidate:
                            continue
                        fanout_contract = json.loads(json.dumps(raw_contract, ensure_ascii=False))
                        fanout_contract["candidate_id"] = candidate_id
                        normalized = self._normalize_task_interaction_contract(fanout_contract, candidate)
                        normalized["representative_candidate_id"] = representative_candidate_id
                        contracts.append(normalized)
                        seen_candidate_ids.add(candidate_id)

        _append_planner_contracts(planner_outputs)
        missing = sorted(set(candidate_by_id.keys()) - seen_candidate_ids)
        omission_retries: List[Dict[str, Any]] = []
        retry_attempt_count = max(
            0,
            int(getattr(self, "task_interaction_contract_planner_retry_attempts", 2) or 0),
        )
        for retry_attempt in range(1, retry_attempt_count + 1):
            if not missing:
                break
            missing_representative_ids = sorted({
                candidate_to_representative.get(candidate_id, candidate_id)
                for candidate_id in missing
            })
            retry_candidates = [
                planning_candidate_by_id[candidate_id]
                for candidate_id in missing_representative_ids
                if candidate_id in planning_candidate_by_id
            ]
            if not retry_candidates:
                break
            if self.logger:
                self.logger.log_warning(
                    "[TaskInteraction] Planner omitted {} candidate(s); retrying {} representative(s) (attempt {}/{})".format(
                        len(missing),
                        len(retry_candidates),
                        retry_attempt,
                        retry_attempt_count,
                    )
                )
            retry_outputs = [await _plan_chunk(len(planner_outputs) + retry_attempt - 1, retry_candidates)]
            planner_outputs.extend(retry_outputs)
            _append_planner_contracts(retry_outputs)
            remaining = sorted(set(candidate_by_id.keys()) - seen_candidate_ids)
            omission_retries.append({
                "attempt": retry_attempt,
                "missing_before": missing,
                "retry_representative_ids": missing_representative_ids,
                "retried_count": len(retry_candidates),
                "missing_after": remaining,
            })
            missing = remaining
        if omission_retries and output_dir:
            self._save_task_interaction_debug(
                "task_interaction_contract_omission_retries.json",
                {
                    "format": "task_interaction_contract_omission_retries",
                    "schema_revision": 1,
                    "attempts": omission_retries,
                },
                output_dir,
            )
        if missing:
            raise RuntimeError(
                "task_interaction_contract planner omitted {} candidate(s) after retry: {}".format(
                    len(missing),
                    ", ".join(missing[:10]),
                )
            )
        contracts_by_candidate_id = {
            contract["candidate_id"]: contract
            for contract in contracts
        }
        reconciliation_artifact = None
        planner_output_artifact = list(cached.get("planner_outputs", [])) + planner_outputs
        if self._l0_schema_registry_flow_enabled():
            reconciliation_artifact = self._reconcile_task_interaction_contracts(contracts)
            contracts = reconciliation_artifact.get("contracts", [])
            contracts_by_candidate_id = reconciliation_artifact.get("contracts_by_candidate_id", {})
            planner_output_artifact = [
                {
                    "chunk_index": output.get("chunk_index"),
                    "candidate_ids": output.get("candidate_ids", []),
                    "page_context_count": output.get("page_context_count", 0),
                    "raw_contract_count": len(
                        output.get("raw_output", {}).get("contracts", [])
                        if isinstance(output.get("raw_output", {}), dict)
                        else []
                    ),
                }
                for output in planner_outputs
                if isinstance(output, dict)
            ]
            if output_dir:
                self._save_task_interaction_debug(
                    "task_interaction_contract_reconciliation.json",
                    {
                        key: value
                        for key, value in reconciliation_artifact.items()
                        if key not in {"contracts", "contracts_by_candidate_id"}
                    },
                    output_dir,
                )
                self._save_task_interaction_debug(
                    "task_interaction_contract_conflicts.json",
                    {
                        "format": "task_interaction_contract_conflicts",
                        "schema_revision": 1,
                        "conflict_count": reconciliation_artifact.get("quarantined_count", 0),
                        "conflicts": reconciliation_artifact.get("quarantined_contracts", []),
                    },
                    output_dir,
                )
                self._save_task_interaction_debug(
                    "task_interaction_contract_budget_report.json",
                    {
                        "format": "task_interaction_contract_budget_report",
                        "schema_revision": 1,
                        "candidate_count": (admission_artifact or {}).get("candidate_count", len(candidates)),
                        "admitted_candidate_count": len(candidates),
                        "dropped_candidate_count": (admission_artifact or {}).get("dropped_count", 0),
                    "planned_contract_count": reconciliation_artifact.get("input_contract_count", len(contracts)),
                    "canonical_contract_count": len(contracts),
                    "merged_group_count": reconciliation_artifact.get("merged_group_count", 0),
                    "quarantined_contract_count": reconciliation_artifact.get("quarantined_count", 0),
                    "planner_representative_count": (
                        representative_artifact or {}
                    ).get("representative_count", len(candidates_to_plan)),
                    "planner_merged_alias_count": (
                        representative_artifact or {}
                    ).get("merged_alias_count", 0),
                },
                    output_dir,
                )
        artifact = {
            "format": "task_interaction_contracts",
            "schema_revision": 1,
            "contracts": contracts,
            "contracts_by_candidate_id": contracts_by_candidate_id,
            "planner_outputs": planner_output_artifact,
            "cache": {
                "reused_contract_count": len(cached_candidate_ids),
                "planned_candidate_count": len(candidates_to_plan),
                "stale_contract_count": len(cached.get("stale_candidate_ids", [])),
            },
        }
        if admission_artifact:
            artifact["admission"] = {
                "candidate_count": admission_artifact.get("candidate_count", 0),
                "admitted_count": admission_artifact.get("admitted_count", 0),
                "dropped_count": admission_artifact.get("dropped_count", 0),
            }
        if representative_artifact:
            artifact["representatives"] = {
                "input_candidate_count": representative_artifact.get("input_candidate_count", 0),
                "representative_count": representative_artifact.get("representative_count", 0),
                "merged_alias_count": representative_artifact.get("merged_alias_count", 0),
            }
        if reconciliation_artifact:
            artifact["reconciliation"] = {
                "input_contract_count": reconciliation_artifact.get("input_contract_count", 0),
                "canonical_contract_count": reconciliation_artifact.get("canonical_contract_count", 0),
                "merged_group_count": reconciliation_artifact.get("merged_group_count", 0),
                "quarantined_count": reconciliation_artifact.get("quarantined_count", 0),
            }
        if output_dir:
            self._save_task_interaction_debug("task_interaction_contract_planner.json", artifact, output_dir)
        return artifact

    def _apply_task_interaction_contracts(
        self,
        page_behaviors: Dict[str, Any],
        contracts_by_candidate_id: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        enriched = {page_id: dict(behavior) for page_id, behavior in page_behaviors.items()}
        for behavior in enriched.values():
            continuations = behavior.get("task_continuations", [])
            if not isinstance(continuations, list):
                continue
            updated = []
            for continuation in continuations:
                if not isinstance(continuation, dict):
                    continue
                item = dict(continuation)
                for legacy_key in ("route_param", "hydrate_rule", "state_effect"):
                    item.pop(legacy_key, None)
                candidate_id = str(item.get("candidate_id", "") or "")
                contract = contracts_by_candidate_id.get(candidate_id)
                if (
                    self._l0_schema_registry_flow_enabled()
                    and candidate_id
                    and not contract
                ):
                    continue
                if contract:
                    item["contract_id"] = contract.get("contract_id", "")
                    item["interaction_contract"] = contract
                    if not item.get("source_step") and isinstance(contract, dict):
                        evidence = contract.get("evidence", {}) if isinstance(contract.get("evidence"), dict) else {}
                        step = (
                            str(evidence.get("source_step_key") or evidence.get("step_key") or "").strip()
                            or str(item.get("source_step_key") or "").strip()
                        )
                        if step:
                            item["source_step"] = step
                updated.append(item)
            behavior["task_continuations"] = updated
        return enriched

    def _validate_task_interaction_contracts(
        self,
        contracts: List[Dict[str, Any]],
        content: "SiteContent",
    ) -> Dict[str, Any]:
        known_state_refs = {
            str(item.get("state_id", "") or "").strip()
            for item in (content.task_state_contract or {}).get("states", [])
            if isinstance(item, dict) and str(item.get("state_id", "") or "").strip()
        }
        known_entity_refs = {
            str(item.get("name", "") or "").strip()
            for item in content.entity_schemas
            if isinstance(item, dict) and str(item.get("name", "") or "").strip()
        }
        known_state_entity_refs = {
            str(item.get("entity_name", "") or item.get("entity_ref", "") or "").strip()
            for item in (content.task_state_contract or {}).get("states", [])
            if isinstance(item, dict) and str(item.get("entity_name", "") or item.get("entity_ref", "") or "").strip()
        }
        schema_registry = content.schema_registry if isinstance(content.schema_registry, dict) else {}
        if self._l0_schema_registry_flow_enabled() and not schema_registry:
            schema_registry = self._build_l0_schema_registry(content)
            content.schema_registry = schema_registry
        registry_enabled = self._l0_schema_registry_flow_enabled()
        issues: List[Dict[str, Any]] = []
        for contract in contracts:
            candidate_id = str(contract.get("candidate_id", "") or "")
            action_semantic = str(contract.get("action_semantic", "") or "")
            state_scope = str(contract.get("state_scope", "") or "")
            state_provenance = str(contract.get("state_provenance", "") or "")
            state_ref = str(contract.get("state_ref", "") or "")
            entity_ref = str(contract.get("entity_ref", "") or "")
            route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
            hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
            hydration_required = bool(hydration.get("required", False))
            route_requires_params = bool(route_binding.get("requires_params", False))
            hydration_source_ref = str(hydration.get("source_ref", "") or "").strip()
            hydration_mode = str(hydration.get("mode", "") or "").strip()
            unresolved_reason = str(contract.get("unresolved_reason", "") or "").strip()
            specific_record_sources = {"clicked_record", "existing_record", "current_record", "route.id"}
            specific_record_hydration = (
                hydration_required
                and (
                    hydration_source_ref in specific_record_sources
                    or hydration_mode in {"edit", "record", "detail"}
                )
            )
            if action_semantic in {"open_collection", "navigate_shell"} and specific_record_hydration:
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "collection_or_shell_must_not_hydrate",
                    "message": "{} cannot require clicked/existing record hydration".format(action_semantic),
                })
            route_binding_source = str(route_binding.get("source", "") or "").strip()
            route_binding_params = route_binding.get("params", [])
            if not isinstance(route_binding_params, list):
                route_binding_params = []
            route_binding_param_names = [
                str(param).strip()
                for param in route_binding_params
                if str(param).strip()
            ]
            derived_draft_source = (
                str(contract.get("source_lifecycle", "") or "") == "existing_record"
                or state_provenance in {"clicked_record", "existing_record", "current_record"}
                or route_binding_source in specific_record_sources
                or any(
                    name.startswith(("source_", "parent_", "current_"))
                    for name in route_binding_param_names
                )
            )
            if (
                action_semantic == "create_record_draft"
                and route_requires_params
                and not derived_draft_source
            ):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "create_draft_must_not_require_existing_id",
                    "message": "new create_record_draft cannot require existing route params unless it is a source/parent/current-record derived draft",
                })
            if (
                action_semantic == "edit_existing_record"
                and state_provenance not in {"clicked_record", "existing_record", "current_record"}
                and str(contract.get("source_lifecycle", "") or "") != "existing_record"
                and hydration_source_ref not in specific_record_sources
            ):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "edit_existing_requires_record_provenance",
                    "message": "edit_existing_record must identify clicked/existing record provenance",
                })
            hydration_state_ref = str(hydration.get("state_ref", "") or state_ref or "")
            if hydration_required and not (hydration_state_ref or entity_ref):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "hydration_missing_ref",
                    "message": "hydration requires a state_ref or entity_ref",
                })
            requires_identity_grounding = self._l0_contract_requires_identity_grounding(contract)
            if requires_identity_grounding and not (state_ref or hydration_state_ref or entity_ref or unresolved_reason):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "executable_contract_missing_identity_grounding",
                    "message": "executable/stateful contract requires state_ref, entity_ref, or unresolved_reason",
                    "action_semantic": action_semantic,
                    "state_scope": state_scope,
                })
            if hydration_state_ref and known_state_refs and hydration_state_ref not in known_state_refs:
                issues.append({
                    "severity": "warning",
                    "candidate_id": candidate_id,
                    "code": "hydration_state_ref_not_declared",
                    "message": "hydration state_ref is not declared in task_state_contract",
                    "state_ref": hydration_state_ref,
                })
            if registry_enabled and hydration_state_ref and not self._l0_schema_registry_resolves_ref(schema_registry, hydration_state_ref):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "state_ref_not_registered",
                    "message": "state_ref is not registered in l0 schema_registry",
                    "state_ref": hydration_state_ref,
                })
            if (
                entity_ref
                and known_entity_refs
                and entity_ref not in known_entity_refs
                and entity_ref not in known_state_entity_refs
            ):
                issues.append({
                    "severity": "warning",
                    "candidate_id": candidate_id,
                    "code": "entity_ref_not_declared",
                    "message": "entity_ref is not declared in entity_schemas",
                    "entity_ref": entity_ref,
                })
            if registry_enabled and entity_ref and not self._l0_schema_registry_resolves_ref(schema_registry, entity_ref):
                issues.append({
                    "severity": "error",
                    "candidate_id": candidate_id,
                    "code": "entity_ref_not_registered",
                    "message": "entity_ref is not registered in l0 schema_registry",
                    "entity_ref": entity_ref,
                })
            field_contract = contract.get("field_contract", {}) if isinstance(contract.get("field_contract", {}), dict) else {}
            unobserved_required = [
                str(item)
                for item in field_contract.get("unobserved_required_fields", [])
                if str(item)
            ] if isinstance(field_contract.get("unobserved_required_fields", []), list) else []
            if registry_enabled and unobserved_required:
                allowed_actions = {
                    "create_record_draft",
                    "edit_existing_record",
                    "update_current_draft",
                    "commit_transaction",
                }
                has_form_state = bool(
                    state_ref
                    or hydration_state_ref
                    or state_scope in {"draft", "record", "transaction"}
                )
                if action_semantic not in allowed_actions or not has_form_state:
                    issues.append({
                        "severity": "error",
                        "candidate_id": candidate_id,
                        "code": "unobserved_required_fields_out_of_scope",
                        "message": "unobserved required fields must stay scoped to same form/draft commit actions",
                        "fields": unobserved_required,
                        "action_semantic": action_semantic,
                        "state_scope": state_scope,
                    })
        error_count = len([issue for issue in issues if issue.get("severity") == "error"])
        return {
            "format": "task_interaction_contract_validation",
            "schema_revision": 1,
            "valid": error_count == 0,
            "error_count": error_count,
            "issue_count": len(issues),
            "issues": issues,
        }

    def _build_page_signatures(
        self,
        page_prototypes: List[PagePrototype],
        page_behaviors: Dict[str, Any],
    ) -> Dict[str, Any]:
        pages = []
        for page in page_prototypes:
            behavior = page_behaviors.get(page.page_id, {}) if isinstance(page_behaviors, dict) else {}
            continuations = behavior.get("task_continuations", []) if isinstance(behavior.get("task_continuations", []), list) else []
            dimensions: List[Dict[str, Any]] = []
            contract_ids: List[str] = []
            for continuation in continuations:
                if not isinstance(continuation, dict):
                    continue
                contract = continuation.get("interaction_contract", {}) if isinstance(continuation.get("interaction_contract", {}), dict) else {}
                contract_id = str(contract.get("contract_id", "") or continuation.get("contract_id", "") or "")
                if contract_id and contract_id not in contract_ids:
                    contract_ids.append(contract_id)
                action_semantic = str(contract.get("action_semantic", "") or "")
                state_ref = str(contract.get("state_ref", "") or "")
                route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
                hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
                sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
                if bool(route_binding.get("requires_params", False)):
                    dimensions.append({"name": "selected_record_ref", "source": action_semantic, "state_ref": state_ref})
                if bool(hydration.get("required", False)):
                    dimensions.append({"name": "hydrated_record", "source": action_semantic, "state_ref": hydration.get("state_ref", state_ref)})
                effect = str(sdk_effect.get("effect", "") or "")
                if effect in {"create", "update"}:
                    dimensions.append({"name": "draft_fields", "source": action_semantic, "state_ref": state_ref})
                elif effect == "select":
                    dimensions.append({"name": "selected_option", "source": action_semantic, "state_ref": state_ref})
                elif effect == "apply":
                    dimensions.append({"name": "filter_or_query", "source": action_semantic, "state_ref": state_ref})
                elif effect == "append":
                    dimensions.append({"name": "accumulator", "source": action_semantic, "state_ref": state_ref})
                elif action_semantic == "inline_ui_state":
                    dimensions.append({"name": "inline_ui_state", "source": action_semantic, "state_ref": state_ref})
            deduped_dimensions = []
            seen_dimensions = set()
            for dimension in dimensions:
                key = (dimension.get("name", ""), dimension.get("state_ref", ""), dimension.get("source", ""))
                if key in seen_dimensions:
                    continue
                deduped_dimensions.append(dimension)
                seen_dimensions.add(key)
            signature_seed = {
                "page_id": page.page_id,
                "dimensions": deduped_dimensions,
                "contract_ids": contract_ids,
            }
            pages.append({
                "page_id": page.page_id,
                "page_name": page.page_name_full or page.page_name,
                "page_type": page.page_type,
                "filename": page.filename,
                "general_page_id": (page.structure_data or {}).get("source_cluster_id") or (page.structure_data or {}).get("cluster_id", ""),
                "materialized_page_key": (page.structure_data or {}).get("instance_key", ""),
                "signature_id": self._stable_short_hash(signature_seed, "pagesig"),
                "finite_state_dimensions": deduped_dimensions,
                "interaction_contract_ids": contract_ids,
                "excluded_from_pagegen": bool((page.structure_data or {}).get("excluded_from_pagegen")),
                "pagegen_exclusion_reason": str((page.structure_data or {}).get("pagegen_exclusion_reason") or ""),
            })
        return {
            "format": "page_signatures",
            "schema_revision": 1,
            "pages": pages,
            "pages_by_id": {page["page_id"]: page for page in pages},
        }

    def _save_task_interaction_debug(self, filename: str, payload: Dict[str, Any], output_dir: str) -> None:
        pom = getattr(self, "pom", None) or PhaseOutputManager(output_dir)
        path = pom.debug("consolidation", filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.logger:
                self.logger.log_warning("[TaskInteraction] Failed to save {}: {}".format(filename, e))

    def _enrich_task_interactions(
        self,
        page_prototypes: List,
        page_behaviors: Dict[str, Any],
        task_elements: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        raw_by_page = task_elements.get("elements_by_page", {}) if isinstance(task_elements, dict) else {}
        capture_actions = task_elements.get("capture_actions", {}) if isinstance(task_elements, dict) else {}

        effective_by_page: Dict[str, List[Dict[str, Any]]] = {
            page_id: [dict(elem) for elem in elements]
            for page_id, elements in raw_by_page.items()
        }
        for page in page_prototypes:
            page_id = getattr(page, "page_id", "")
            if page_id:
                effective_by_page.setdefault(page_id, [])
        for page_id in page_behaviors:
            effective_by_page.setdefault(page_id, [])

        persistent_scope_pages = {
            page.page_id for page in page_prototypes
            if self._page_has_persistent_nav(page)
        }

        persistent_elements_by_scope: Dict[str, List[Dict[str, Any]]] = {"main_navigation": []}
        persistent_index: Dict[str, Dict[str, Any]] = {}

        for page_id, elements in raw_by_page.items():
            for elem in elements:
                if not isinstance(elem, dict):
                    continue
                if elem.get("element_type") != "nav_link":
                    continue
                name = str(elem.get("element_name", ""))
                if not name.startswith("nav_"):
                    continue
                target = self._resolve_task_nav_target(elem.get("display_text", ""), page_prototypes)
                if not target:
                    continue
                key = name
                if key not in persistent_index:
                    persistent_index[key] = {
                        "element_name": name,
                        "display_text": elem.get("display_text", ""),
                        "element_type": elem.get("element_type", "nav_link"),
                        "scope": "persistent_navigation",
                        "scope_group": "main_navigation",
                        "target_page_id": target["page_id"],
                        "target_page_name": target["page_name"],
                        "target_filename": target["filename"],
                        "source_pages": [page_id],
                    }
                elif page_id not in persistent_index[key]["source_pages"]:
                    persistent_index[key]["source_pages"].append(page_id)

        persistent_elements_by_scope["main_navigation"] = list(persistent_index.values())
        persistent_scope_pages |= {
            elem.get("target_page_id", "")
            for elem in persistent_elements_by_scope["main_navigation"]
            if elem.get("target_page_id")
        }

        page_map = {page.page_id: page for page in page_prototypes if getattr(page, "page_id", "")}

        for page_id in persistent_scope_pages:
            effective_by_page.setdefault(page_id, [])
            existing_names = {elem.get("element_name") for elem in effective_by_page[page_id] if isinstance(elem, dict)}
            for scope_group in ("main_navigation",):
                for persistent in persistent_elements_by_scope.get(scope_group, []):
                    if persistent["element_name"] not in existing_names:
                        effective_by_page[page_id].append(dict(persistent))
                        existing_names.add(persistent["element_name"])

        enriched_behaviors = {pid: dict(behavior) for pid, behavior in page_behaviors.items()}
        continuations_by_page: Dict[str, List[Dict[str, Any]]] = {}
        interaction_candidates: List[Dict[str, Any]] = []

        for page_id, elements in effective_by_page.items():
            behavior = enriched_behaviors.setdefault(page_id, {})
            nav_out = behavior.get("navigation_out", []) if isinstance(behavior.get("navigation_out", []), list) else []
            page_obj = page_map.get(page_id)
            shortcut_nav = self._infer_shortcut_navigation_out(page_obj, page_prototypes)
            existing_nav_keys = {
                (str(item.get("trigger", "")).strip(), str(item.get("to_filename", "")).strip())
                for item in nav_out if isinstance(item, dict)
            }
            for item in shortcut_nav:
                key = (str(item.get("trigger", "")).strip(), str(item.get("to_filename", "")).strip())
                if key not in existing_nav_keys:
                    nav_out.append(item)
            page_action_sequence = behavior.get("page_actions", []) if isinstance(behavior.get("page_actions", []), list) else []
            same_page_actions = [
                action for action in page_action_sequence
                if isinstance(action, dict) and self._is_same_page_capture_action(action)
            ]
            if same_page_actions:
                nav_out = [
                    nav for nav in nav_out
                    if not (
                        isinstance(nav, dict)
                        and any(
                            self._action_matches_navigation_trigger(action, str(nav.get("trigger", "") or ""))
                            for action in same_page_actions
                        )
                    )
                ]
            behavior["navigation_out"] = nav_out
            page_sequence_text = " ".join(
                " ".join(str(action.get(key, "") or "") for key in ("description", "target_element", "task_instruction", "current_description", "next_description"))
                for action in page_action_sequence if isinstance(action, dict)
            )
            page_lifecycle_hint = ""
            continuations: List[Dict[str, Any]] = []
            continuation_keys = set()
            nav_trigger_labels = [
                self._normalize_task_label(str(nav.get("trigger", "") or ""))
                for nav in nav_out
                if isinstance(nav, dict) and str(nav.get("trigger", "") or "").strip()
            ]

            for nav in nav_out:
                if not isinstance(nav, dict):
                    continue
                trigger = str(nav.get("trigger", "") or "").strip()
                target_filename = str(nav.get("to_filename", "") or "").strip()
                if not trigger or not target_filename:
                    continue
                continuation = self._build_task_continuation_contract(
                    element_name=trigger,
                    display_text=trigger.replace("点击", "", 1).strip() if trigger.startswith("点击") else trigger,
                    element_type="navigation_action",
                    continuation_kind="page_navigation",
                    source_page_id=page_id,
                    target_page_id=str(nav.get("to_page_id", "") or ""),
                    target_filename=target_filename,
                    reason="navigation_state_continuation",
                    target_page_type=str(getattr(page_map.get(str(nav.get("to_page_id", "") or "")), "page_type", "") or ""),
                    visible_binding={
                        "source_kind": "navigation_out",
                        "role": "operation_evidence",
                        "visible_text_allowed": nav.get("trigger_visible_text_allowed") is True,
                    },
                    operation_binding={
                        "source_kind": "navigation_out",
                        "operation": "page_navigation",
                        "visible_text_allowed": False,
                    },
                )
                key = (continuation["element_name"], continuation.get("target_filename", ""))
                if key not in continuation_keys:
                    continuations.append(continuation)
                    continuation_keys.add(key)

            for action in behavior.get("page_actions", []) if isinstance(behavior.get("page_actions", []), list) else []:
                if not isinstance(action, dict):
                    continue
                description = str(action.get("description", "") or action.get("target_element", "") or "").strip()
                if not description:
                    continue
                action_kind = str(action.get("action", "") or "").strip().lower()
                target_text = str(action.get("target_element", "") or "").strip()
                has_observed_target = bool(target_text)
                operation_binding = {
                    "source_kind": "trace_action",
                    "operation": action_kind or "task_action",
                    "step": action.get("step"),
                    "trace": action.get("trace"),
                    "visible_text_allowed": False,
                }
                visible_binding = {
                    "source_kind": "observed_action_target",
                    "role": "visible_label",
                    "visible_text_allowed": True,
                } if has_observed_target else {
                    "source_kind": "trace_action",
                    "role": "operation_intent",
                    "visible_text_allowed": False,
                }
                if action.get("action") == "click":
                    action_labels = [
                        self._normalize_task_label(description),
                        self._normalize_task_label(target_text),
                    ]
                    if any(
                        action_label
                        and nav_label
                        and (action_label in nav_label or nav_label in action_label)
                        for action_label in action_labels
                        for nav_label in nav_trigger_labels
                    ):
                        continue
                same_page_action = self._is_same_page_capture_action(action)
                continuation = self._build_task_continuation_contract(
                    element_name=target_text if has_observed_target else (action_kind or "task_action"),
                    display_text=target_text if has_observed_target else "",
                    element_type=str(action.get("action", "task_action")),
                    continuation_kind=(
                        "stateful_action"
                        if same_page_action or action.get("action") != "click"
                        else "inline_state"
                    ),
                    source_page_id=page_id,
                    target_page_id="",
                    target_filename="",
                    reason="same_page_transition_continuation" if same_page_action else "page_action_state_continuation",
                    task_instruction=str(action.get("task_instruction", "") or ""),
                    current_description=str(action.get("current_description", "") or ""),
                    next_description=str(action.get("next_description", "") or ""),
                    lifecycle_hint=page_lifecycle_hint,
                    visible_binding=visible_binding,
                    operation_binding=operation_binding,
                )
                key = (continuation["element_name"], continuation.get("target_filename", ""), continuation.get("display_text", ""))
                if key not in continuation_keys:
                    continuations.append(continuation)
                    continuation_keys.add(key)

            page_action_sequence = behavior.get("page_actions", []) if isinstance(behavior.get("page_actions", []), list) else []
            sequence_task_text = " ".join(str(action.get("task_instruction", "") or "") for action in page_action_sequence if isinstance(action, dict))
            sequence_evidence_text = " ".join(
                " ".join(str(action.get(key, "") or "") for key in ("current_description", "next_description"))
                for action in page_action_sequence if isinstance(action, dict)
            )
            sequence_text = " ".join(
                str(action.get("description", "") or action.get("target_element", "") or "")
                for action in page_action_sequence if isinstance(action, dict)
            )
            sequence_field_contract = self._build_field_contract(sequence_text, "sequence", sequence_task_text, sequence_evidence_text)
            sequence_commit = ""

            for elem in elements:
                if not isinstance(elem, dict):
                    continue
                element_name = elem.get("element_name", "")
                display_text = elem.get("display_text", "")
                element_type = elem.get("element_type", "")

                if elem.get("scope") == "persistent_navigation" and elem.get("target_filename"):
                    continuation = self._build_task_continuation_contract(
                        element_name=element_name,
                        display_text=display_text,
                        element_type=element_type,
                        continuation_kind="page_navigation",
                        source_page_id=page_id,
                        target_page_id=elem.get("target_page_id", ""),
                        target_filename=elem.get("target_filename", ""),
                        reason="persistent_task_navigation",
                        target_page_type=str(getattr(page_map.get(str(elem.get("target_page_id", "") or "")), "page_type", "") or ""),
                        visible_binding={
                            "source_kind": "task_element",
                            "role": "visible_label",
                            "visible_text_allowed": True,
                        },
                    )
                    key = (continuation["element_name"], continuation.get("target_filename", ""))
                    if key not in continuation_keys:
                        continuations.append(continuation)
                        continuation_keys.add(key)
                    continue

                if element_type != "action_button":
                    continue

                explicit_nav = next((
                    nav for nav in nav_out
                    if isinstance(nav, dict)
                    and (
                        self._normalize_task_label(display_text) in self._normalize_task_label(nav.get("trigger", ""))
                        or self._normalize_task_label(element_name) in self._normalize_task_label(nav.get("trigger", ""))
                    )
                ), None)
                if explicit_nav:
                    continuation = self._build_task_continuation_contract(
                        element_name=element_name,
                        display_text=display_text,
                        element_type=element_type,
                        continuation_kind="page_navigation",
                        source_page_id=page_id,
                        target_page_id=explicit_nav.get("to_page_id", ""),
                        target_filename=explicit_nav.get("to_filename", ""),
                        reason="task_action_with_explicit_navigation",
                        target_page_type=str(getattr(page_map.get(str(explicit_nav.get("to_page_id", "") or "")), "page_type", "") or ""),
                        visible_binding={
                            "source_kind": "task_element",
                            "role": "visible_label",
                            "visible_text_allowed": True,
                        },
                    )
                    key = (continuation["element_name"], continuation.get("target_filename", ""))
                    if key not in continuation_keys:
                        continuations.append(continuation)
                        continuation_keys.add(key)
                    continue

                continuation = self._build_task_continuation_contract(
                    element_name=element_name,
                    display_text=display_text,
                    element_type=element_type,
                    continuation_kind=self._infer_missing_continuation_kind(elem),
                    source_page_id=page_id,
                    target_page_id="",
                    target_filename="",
                    reason="task_action_without_explicit_navigation",
                    visible_binding={
                        "source_kind": "task_element",
                        "role": "visible_label",
                        "visible_text_allowed": True,
                    },
                )
                key = (continuation["element_name"], continuation.get("target_filename", ""))
                if key not in continuation_keys:
                    continuations.append(continuation)
                    continuation_keys.add(key)

            if continuations and (sequence_field_contract["observed_fields"] or sequence_field_contract["typed_fields"] or sequence_field_contract["unobserved_required_fields"] or sequence_commit):
                for continuation in continuations:
                    contract = continuation.get("interaction_contract", {}) if isinstance(continuation.get("interaction_contract", {}), dict) else {}
                    field_contract = contract.setdefault("field_contract", {"observed_fields": [], "typed_fields": [], "unobserved_required_fields": []})
                    for field_key in ("observed_fields", "typed_fields", "unobserved_required_fields"):
                        field_contract.setdefault(field_key, [])
                        for value in sequence_field_contract.get(field_key, []):
                            if value not in field_contract[field_key]:
                                field_contract[field_key].append(value)
                    if sequence_commit:
                        contract.setdefault("sdk_effect", self._empty_sdk_effect())["observed_trigger_text"] = sequence_commit

            if continuations:
                behavior["task_continuations"] = continuations
                continuations_by_page[page_id] = continuations
                for continuation in continuations:
                    interaction_candidates.append(
                        self._build_interaction_candidate_record(
                            continuation=continuation,
                            source_page=page_obj,
                            target_page=page_map.get(str(continuation.get("target_page_id", "") or "")),
                        )
                    )
            floating_component = self._infer_floating_component_for_page(
                page_obj,
                behavior.get("page_actions", []) or [],
            )
            if floating_component:
                behavior["component_contracts"] = [floating_component]
                behavior["floating_component"] = floating_component
                behavior["overlay_policy"] = floating_component
            behavior["interaction_policy"] = self._build_interaction_policy_for_page(
                page=page_obj,
                elements=elements,
                continuations=continuations,
                navigation_out=nav_out,
            )

        return (
            {
                "effective_elements_by_page": effective_by_page,
                "persistent_elements_by_scope": persistent_elements_by_scope,
                "continuations_by_page": continuations_by_page,
                "interaction_candidates": interaction_candidates,
                "capture_actions": capture_actions,
            },
            enriched_behaviors,
        )

    def _aggregate_visual_style_from_structures(self, pages: List[PagePrototype]) -> Dict[str, Any]:
        brand_identity = {}
        color_scheme = {}
        typography = {}
        spacing = {}
        icons = set()
        for page in pages:
            sd = page.structure_data if isinstance(page.structure_data, dict) else {}

            bi = sd.get("brand_identity", {})
            if isinstance(bi, dict) and bi and not brand_identity:
                brand_identity = bi

            ds = sd.get("design_system", {})
            if isinstance(ds, dict):
                color_scheme = color_scheme or (ds.get("color_scheme", {}) if isinstance(ds.get("color_scheme", {}), dict) else {})
                typography = typography or (ds.get("typography", {}) if isinstance(ds.get("typography", {}), dict) else {})
                spacing = spacing or (ds.get("spacing", {}) if isinstance(ds.get("spacing", {}), dict) else {})
                ds_icons = ds.get("icons", {})
                if isinstance(ds_icons, dict):
                    for icon in ds_icons.get("commonly_used", []) if isinstance(ds_icons.get("commonly_used", []), list) else []:
                        if isinstance(icon, str):
                            icons.add(icon)

            ir = sd.get("identified_resources", {})
            if isinstance(ir, dict):
                used = ir.get("icons_used", [])
                for entry in used if isinstance(used, list) else []:
                    if isinstance(entry, dict):
                        icon = entry.get("lucide_icon")
                        if isinstance(icon, str) and icon:
                            icons.add(icon)
        return {
            "brand_identity": brand_identity,
            "color_scheme": color_scheme,
            "typography": typography,
            "spacing": spacing,
            "icons": {"library": "lucide", "commonly_used": sorted(icons)}
        }
