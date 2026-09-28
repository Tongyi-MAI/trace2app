import re
import json
import copy
from typing import Dict, Any, List

from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
from trace2world_pipeline.app_spec.model import AppSpec, GraphEdge, PagePrototype


class AppSpecEnricher:

    def __init__(self, schema_enricher, logger=None, model=None,
                 reasoning_effort="medium"):
        self.schema_enricher = schema_enricher
        self.logger = logger or Trace2WorldLogger()
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def enrich(self, site_model: AppSpec, tasks: List[Dict[str, Any]],
                     website_type: str) -> AppSpec:
        enriched = copy.deepcopy(site_model)


        self.logger.log_info("AppSpec enrichment 4.5a: Schema enrichment")
        enriched_schemas, enriched_rels = await self.schema_enricher.enrich_schemas(
            enriched.content.entity_schemas,
            enriched.content.entity_relationships, tasks, website_type,
        )
        enriched.content.entity_schemas = enriched_schemas
        enriched.content.entity_relationships = enriched_rels


        self.logger.log_info("AppSpec enrichment 4.5b: Page prototype enrichment")
        await self._enrich_page_prototypes(enriched, tasks, website_type)


        self.logger.log_info("AppSpec enrichment 4.5c: Site graph enrichment")
        await self._enrich_site_graph(enriched, tasks, website_type)


        self.logger.log_info("AppSpec enrichment 4.5d: Page behaviors enrichment")
        await self._enrich_page_behaviors(enriched, tasks, website_type)

        return enriched

    async def _enrich_page_prototypes(self, site_model: AppSpec,
                                      tasks: List[Dict[str, Any]],
                                      website_type: str) -> None:
        protos = site_model.structure.page_prototypes
        protos_compact = [{
            "page_id": p.page_id, "page_type": p.page_type,
            "page_name": p.page_name, "filename": p.filename,
            "description": p.description,
            "primary_functions": p.primary_functions,
            "components": [c.get("name", "") for c in p.components],
        } for p in protos]
        tasks_compact = self._compact_tasks(tasks)

        prompt = f"""You are a web architect enriching page prototypes for a {website_type} website.

Current Page Prototypes:
{json.dumps(protos_compact, indent=2, ensure_ascii=False)}

Generated User Tasks:
{json.dumps(tasks_compact, indent=2, ensure_ascii=False)}

Analyze each task's steps and identify:
1. Pages that tasks reference but do NOT exist in the current prototypes (CREATE them).
2. Existing pages that need additional components or primary_functions based on task requirements.

Output a JSON object with an "operations" array. Each operation must use one of:
- {{"op": "create_page", "page": {{"page_id": "new_id", "page_type": "form|detail|listing|search_results|utility|homepage", "page_name": "Human Name", "filename": "type-N.html", "description": "...", "primary_functions": ["..."], "components": [{{"name": "...", "type": "...", "description": "..."}}]}}}}
- {{"op": "add_component", "page_id": "existing_id", "component": {{"name": "...", "type": "...", "description": "..."}}}}
- {{"op": "update_page", "page_id": "existing_id", "updates": {{"description": "...", "primary_functions": ["added_func"]}}}}

Rules:
- ONLY add pages/components implied by tasks but MISSING from current prototypes.
- Do NOT remove or rename existing pages.
- page_id must be lowercase_snake_case.
- If no gaps found, output {{"operations": []}}.

Output JSON: {{"operations": [...]}}"""

        call_id = self.logger.log_api_call("Enrich Page Prototypes", prompt)
        result, usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model, reasoning_effort=self.reasoning_effort,
            stage="app_spec_enrich_page_prototypes",
        )
        if isinstance(result, str):
            result = json.loads(result)
        self.logger.log_api_response(
            "Enrich Page Prototypes", success=True, response=result,
            usage_info=usage, call_id=call_id,
        )
        operations = result.get("operations", [])
        if not operations:
            return
        self.logger.log_info(f"Applying {len(operations)} page prototype ops")
        self._apply_page_operations(protos, operations)

    def _apply_page_operations(self, protos: List[PagePrototype],
                               operations: List[Dict]) -> None:
        page_map = {p.page_id: p for p in protos}
        type_counts: Dict[str, int] = {}
        for p in protos:
            type_counts[p.page_type] = type_counts.get(p.page_type, 0) + 1

        for op_data in operations:
            op = op_data.get("op", "")
            if op == "create_page":
                page_dict = op_data.get("page", {})
                if not page_dict or not page_dict.get("page_id"):
                    continue
                if page_dict["page_id"] in page_map:
                    continue
                ptype = page_dict.get("page_type", "utility")
                idx = type_counts.get(ptype, 0) + 1
                type_counts[ptype] = idx
                filename = page_dict.get("filename") or f"{ptype}-{idx}.html"
                comps = page_dict.get("components", [])
                for c in comps:
                    if isinstance(c, dict):
                        c["source"] = "task_inferred"
                new_page = PagePrototype(
                    page_id=page_dict["page_id"], page_type=ptype,
                    page_name=page_dict.get("page_name", page_dict["page_id"]),
                    filename=filename,
                    description=page_dict.get("description", ""),
                    primary_functions=page_dict.get("primary_functions", []),
                    components=comps, confidence=0.6,
                )
                new_page.structure_data = {"source": "task_inferred"}
                protos.append(new_page)
                page_map[new_page.page_id] = new_page
            elif op == "add_component":
                pid = op_data.get("page_id", "")
                comp = op_data.get("component", {})
                page = page_map.get(pid)
                if page and isinstance(comp, dict) and comp.get("name"):
                    comp["source"] = "task_inferred"
                    page.components.append(comp)
            elif op == "update_page":
                pid = op_data.get("page_id", "")
                updates = op_data.get("updates", {})
                page = page_map.get(pid)
                if page and isinstance(updates, dict):
                    if "description" in updates:
                        page.description = updates["description"]
                    if "primary_functions" in updates:
                        existing = set(page.primary_functions)
                        for fn in updates["primary_functions"]:
                            if fn not in existing:
                                page.primary_functions.append(fn)
                                existing.add(fn)

    async def _enrich_site_graph(self, site_model: AppSpec,
                                 tasks: List[Dict[str, Any]],
                                 website_type: str) -> None:
        edges = site_model.structure.site_graph.edges
        protos = site_model.structure.page_prototypes
        edges_compact = [{
            "from_page_id": e.from_page_id, "to_page_id": e.to_page_id,
            "trigger": e.trigger,
        } for e in edges]
        pages_compact = [{
            "page_id": p.page_id, "page_name": p.page_name,
            "filename": p.filename, "page_type": p.page_type,
        } for p in protos]
        tasks_compact = self._compact_tasks(tasks)

        prompt = f"""You are a web architect enriching the site navigation graph for a {website_type} website.

Current Pages:
{json.dumps(pages_compact, indent=2, ensure_ascii=False)}

Current Navigation Edges:
{json.dumps(edges_compact, indent=2, ensure_ascii=False)}

Generated User Tasks:
{json.dumps(tasks_compact, indent=2, ensure_ascii=False)}

Analyze each task's steps and identify navigation edges implied by tasks but MISSING from the current graph.

Output a JSON object with an "operations" array. Each operation must use:
- {{"op": "create_edge", "edge": {{"from_page_id": "...", "to_page_id": "...", "trigger": "User clicks...", "to_route_path": "/...", "to_route_pattern": "/.../:param"}}}}

Rules:
- ONLY add edges implied by tasks but MISSING from the current graph.
- from_page_id and to_page_id MUST reference existing page_ids from the pages list.
- Do NOT create duplicate edges (same from_page_id + to_page_id + trigger).
- If no gaps found, output {{"operations": []}}.

Output JSON: {{"operations": [...]}}"""

        call_id = self.logger.log_api_call("Enrich Site Graph", prompt)
        result, usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model, reasoning_effort=self.reasoning_effort,
            stage="app_spec_enrich_site_graph",
        )
        if isinstance(result, str):
            result = json.loads(result)
        self.logger.log_api_response(
            "Enrich Site Graph", success=True, response=result,
            usage_info=usage, call_id=call_id,
        )
        operations = result.get("operations", [])
        if not operations:
            return
        self.logger.log_info(f"Applying {len(operations)} site graph ops")
        self._apply_edge_operations(edges, operations)

    def _apply_edge_operations(self, edges: List[GraphEdge],
                               operations: List[Dict]) -> None:
        existing_keys = {
            (e.from_page_id, e.to_page_id, e.trigger) for e in edges
        }
        for op_data in operations:
            if op_data.get("op") != "create_edge":
                continue
            edge_dict = op_data.get("edge", {})
            from_id = edge_dict.get("from_page_id", "")
            to_id = edge_dict.get("to_page_id", "")
            if not from_id or not to_id:
                continue
            trigger = edge_dict.get("trigger", "")
            key = (from_id, to_id, trigger)
            if key in existing_keys:
                continue
            new_edge = GraphEdge(
                from_page_id=from_id, to_page_id=to_id,
                trigger=trigger,
                to_route_path=edge_dict.get("to_route_path", ""),
                to_route_pattern=edge_dict.get("to_route_pattern", ""),
                confidence=0.6, evidence_steps=["task_inferred"],
            )
            edges.append(new_edge)
            existing_keys.add(key)

    async def _enrich_page_behaviors(self, site_model: AppSpec,
                                     tasks: List[Dict[str, Any]],
                                     website_type: str) -> None:

        capture_actions = site_model.task_elements.get("capture_actions", {}) if site_model.task_elements else {}
        behaviors = self._build_page_behaviors(
            site_model.structure.page_prototypes,
            site_model.structure.site_graph.edges,
            capture_actions,
            site_model.structure.global_navigation,
        )

        behaviors_compact = [{
            "page_id": pid, "page_name": b.get("page_name", ""),
            "route_params": b.get("route_params", []),
            "actions_count": len(b.get("page_actions", [])),
            "nav_out_count": len(b.get("navigation_out", [])),
        } for pid, b in behaviors.items()]
        tasks_compact = self._compact_tasks(tasks)

        prompt = f"""You are a web architect enriching page behavior specs for a {website_type} website.

Current Page Behaviors (summary):
{json.dumps(behaviors_compact, indent=2, ensure_ascii=False)}

Generated User Tasks:
{json.dumps(tasks_compact, indent=2, ensure_ascii=False)}

Analyze each task's steps and identify page actions and route parameters implied by tasks but MISSING from current page behaviors.

Output a JSON object with an "operations" array. Each operation must use one of:
- {{"op": "add_action", "page_id": "...", "action": {{"action": "click|scroll|type|select", "target_element": "...", "description": "..."}}}}
- {{"op": "add_route_param", "page_id": "...", "param": "param_name"}}

Rules:
- ONLY add actions/params implied by tasks but MISSING.
- page_id MUST match an existing page in the behaviors list.
- If no gaps found, output {{"operations": []}}.

Output JSON: {{"operations": [...]}}"""

        call_id = self.logger.log_api_call("Enrich Page Behaviors", prompt)
        result, usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model, reasoning_effort=self.reasoning_effort,
            stage="app_spec_enrich_page_behaviors",
        )
        if isinstance(result, str):
            result = json.loads(result)
        self.logger.log_api_response(
            "Enrich Page Behaviors", success=True, response=result,
            usage_info=usage, call_id=call_id,
        )
        operations = result.get("operations", [])
        if operations:
            self.logger.log_info(f"Applying {len(operations)} page behavior ops")
            self._apply_behavior_operations(behaviors, operations)
        site_model.structure.page_behaviors = behaviors

    def _apply_behavior_operations(self, behaviors: Dict[str, Any],
                                   operations: List[Dict]) -> None:
        for op_data in operations:
            op = op_data.get("op", "")
            pid = op_data.get("page_id", "")
            if pid not in behaviors:
                continue
            page_b = behaviors[pid]
            if op == "add_action":
                action = op_data.get("action", {})
                if not action.get("action"):
                    continue
                page_b.setdefault("page_actions", []).append({
                    "trace": "enrichment",
                    "step": -1,
                    "action": action.get("action", ""),
                    "target_element": action.get("target_element", ""),
                    "description": action.get("description", ""),
                })
            elif op == "add_route_param":
                param = op_data.get("param", "")
                if param and param not in page_b.get("route_params", []):
                    page_b.setdefault("route_params", []).append(param)


    def _build_page_behaviors(
        self, page_prototypes: List[PagePrototype], edges: List[GraphEdge],
        capture_actions: Dict[str, list], global_navigation: Dict[str, Any],
    ) -> Dict[str, Any]:
        page_id_to_filename = {
            p.page_id: p.filename for p in page_prototypes if p.filename
        }
        out_edges: Dict[str, List[Dict]] = {}
        in_edges: Dict[str, List[Dict]] = {}
        for e in edges:
            to_rp = self._extract_route_params(e.to_route_pattern) if e.to_route_pattern else []
            out_edges.setdefault(e.from_page_id, []).append({
                "to_page": e.to_page_name or e.to_page_id,
                "to_filename": page_id_to_filename.get(e.to_page_id, ""),
                "trigger": e.trigger,
                "to_route": e.to_route_path,
                "to_route_pattern": e.to_route_pattern,
                "params_needed": to_rp,
            })
            in_edges.setdefault(e.to_page_id, []).append({
                "from_page": e.from_page_name or e.from_page_id,
                "from_filename": page_id_to_filename.get(e.from_page_id, ""),
                "trigger": e.trigger,
            })

        actions_by_page: Dict[str, List[Dict]] = {}
        for trace_id, actions in capture_actions.items():
            for act in actions:
                page_id = act.get("page", "")
                if not page_id:
                    continue
                entry = {
                    "trace": trace_id, "step": act.get("step", 0),
                    "action": act.get("action", ""),
                    "description": act.get("description", ""),
                }
                if act.get("target_element"):
                    entry["target_element"] = act["target_element"]
                actions_by_page.setdefault(page_id, []).append(entry)
        behaviors: Dict[str, Any] = {}
        for page in page_prototypes:
            pid = page.page_id
            sd = page.structure_data if isinstance(page.structure_data, dict) else {}
            route_pattern = self._normalize_route_pattern(
                sd.get("route_path", "") or f"/page/{pid}"
            )
            route_params = self._extract_route_params(route_pattern)
            behaviors[pid] = {
                "page_id": pid,
                "page_name": page.page_name_full or page.page_name,
                "route_pattern": route_pattern, "route_params": route_params,
                "navigation_out": out_edges.get(pid, []),
                "navigation_in": in_edges.get(pid, []),
                "page_actions": actions_by_page.get(pid, []),
            }
        return behaviors

    @staticmethod
    def _extract_route_params(route_pattern: str) -> List[str]:
        return re.findall(r':(\w+)', route_pattern)

    @staticmethod
    def _normalize_route_pattern(route_path: str) -> str:
        path = str(route_path or "").strip()
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        path = path.replace("{", ":").replace("}", "")
        while "//" in path:
            path = path.replace("//", "/")
        return path

    @staticmethod
    def _compact_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{
            "id": t.get("id"), "name": t.get("name"),
            "description": t.get("description", ""),
            "steps": t.get("steps", []),
        } for t in tasks]
