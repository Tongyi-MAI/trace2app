import json
import copy
from typing import Dict, Any, List, Tuple

from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
from trace2world_pipeline.app_spec.reconcile.consolidation import apply_schema_operations, materialize_fk_fields


class SchemaEnricher:

    def __init__(self, logger: Trace2WorldLogger = None, model: str = None,
                 reasoning_effort: str = "medium"):
        self.logger = logger or Trace2WorldLogger()
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def enrich_schemas(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        website_type: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if not tasks:
            self.logger.log_info("No tasks provided, skipping schema enrichment")
            return entity_schemas, entity_relationships

        original_schemas = copy.deepcopy(entity_schemas)
        original_rels = copy.deepcopy(entity_relationships)

        prompt = self._build_enrichment_prompt(
            entity_schemas, entity_relationships, tasks, website_type
        )

        call_id = self.logger.log_api_call("Enrich Schemas", prompt)
        messages = [{"role": "user", "content": prompt}]
        result, usage = await call_openai_api_json_async(
            messages,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            stage="schema_enrich",
        )
        if isinstance(result, str):
            result = json.loads(result)
        self.logger.log_api_response(
            "Enrich Schemas", success=True, response=result,
            usage_info=usage, call_id=call_id
        )

        operations = result.get("operations", [])
        if not operations:
            self.logger.log_info("No schema enrichment operations needed")
            return entity_schemas, entity_relationships

        self.logger.log_info(f"Applying {len(operations)} schema enrichment operations")

        enriched_schemas, new_rels = apply_schema_operations(
            copy.deepcopy(entity_schemas), operations
        )

        merged_rels = list(entity_relationships)
        existing_rel_keys = {
            (r.get("from"), r.get("to"), r.get("field"))
            for r in merged_rels if isinstance(r, dict)
        }
        for rel in new_rels:
            key = (rel.get("from"), rel.get("to"), rel.get("field"))
            if key not in existing_rel_keys:
                merged_rels.append(rel)
                existing_rel_keys.add(key)

        self._tag_new_additions(original_schemas, enriched_schemas)
        materialize_fk_fields(enriched_schemas, merged_rels)

        return enriched_schemas, merged_rels

    def _build_enrichment_prompt(
        self,
        entity_schemas: List[Dict[str, Any]],
        entity_relationships: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        website_type: str,
    ) -> str:
        schemas_compact = []
        for s in entity_schemas:
            fields_compact = [
                {"name": f.get("name"), "type": f.get("type", "string")}
                for f in s.get("fields", []) if isinstance(f, dict)
            ]
            schemas_compact.append({
                "name": s.get("name"),
                "storage_key": s.get("storage_key"),
                "fields": fields_compact,
                "data_pre_generation_num": s.get("data_pre_generation_num", "few"),
            })

        tasks_compact = []
        for t in tasks:
            tasks_compact.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description", ""),
                "steps": t.get("steps", []),
            })

        return f"""You are a data architect enriching entity schemas for a {website_type} website.

Current Entity Schemas (extracted from visual traces — PARTIAL, may be incomplete):
{json.dumps(schemas_compact, indent=2, ensure_ascii=False)}

Current Entity Relationships:
{json.dumps(entity_relationships, indent=2, ensure_ascii=False)}

Generated User Tasks:
{json.dumps(tasks_compact, indent=2, ensure_ascii=False)}

Analyze each task's name, description, and steps. Identify entities and fields that tasks reference but are NOT present in the current schemas.

Common gaps:
- Tasks that create/submit/modify objects imply entities (e.g., "submit order" → Order, "write review" → Review)
- Tasks that filter/sort by a field imply that field exists (e.g., "filter by color" → Product.color)
- Tasks referencing user accounts, carts, wishlists, reviews imply those entities
- Tasks with specific field values (prices, ratings, sizes, dates) imply typed fields

Output a JSON object with an "operations" array. Each operation must use one of:
- {{"op": "create_entity", "entity": {{"name": "PascalCase", "storage_key": "lowercase_plural", "description": "...", "fields": [{{"name": "...", "type": "string|number|boolean|url|date", "description": "..."}}], "data_pre_generation_num": "many|few|none"}}}}
- {{"op": "add_field", "entity_name": "ExistingEntity", "field": {{"name": "...", "type": "...", "description": "..."}}}}
- {{"op": "update_entity", "entity_name": "ExistingEntity", "updates": {{"data_pre_generation_num": "...", "description": "..."}}}}
- {{"op": "create_relationship", "relationship": {{"from": "Entity", "to": "Entity", "type": "belongs_to|has_many", "field": "foreign_key_field"}}}}

Rules:
- ONLY add entities/fields that are implied by the tasks but MISSING from current schemas.
- Do NOT remove or rename existing entities/fields.
- Entity names: PascalCase (e.g., OrderItem, UserReview).
- data_pre_generation_num: "many" (≥10 items, e.g., products, articles), "few" (2-9, e.g., categories, payment methods), "none" (user-created at runtime, e.g., orders, cart items, reviews, sessions).
- Every new entity MUST have an "id" field.
- If no gaps found, output {{"operations": []}}.

Output JSON: {{"operations": [...]}}"""

    def _tag_new_additions(
        self,
        original_schemas: List[Dict[str, Any]],
        enriched_schemas: List[Dict[str, Any]],
    ) -> None:
        original_entities = {
            s["name"] for s in original_schemas
            if isinstance(s, dict) and s.get("name")
        }
        original_fields: Dict[str, set] = {}
        for s in original_schemas:
            if isinstance(s, dict) and s.get("name"):
                original_fields[s["name"]] = {
                    f["name"] for f in s.get("fields", []) if isinstance(f, dict)
                }

        for schema in enriched_schemas:
            name = schema.get("name", "")
            if name not in original_entities:
                schema["source"] = "task_inferred"
                for f in schema.get("fields", []):
                    if isinstance(f, dict):
                        f["source"] = "task_inferred"
            else:
                orig_field_names = original_fields.get(name, set())
                for f in schema.get("fields", []):
                    if isinstance(f, dict) and f["name"] not in orig_field_names:
                        f["source"] = "task_inferred"
