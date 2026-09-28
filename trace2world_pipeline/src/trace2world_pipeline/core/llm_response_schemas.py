
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


JsonSchema = dict[str, Any]


def string_schema(*, enum: list[str] | None = None, min_length: int | None = None) -> JsonSchema:
    schema: JsonSchema = {"type": "string"}
    if enum:
        schema["enum"] = list(enum)
    if min_length is not None:
        schema["minLength"] = min_length
    return schema


def number_schema() -> JsonSchema:
    return {"type": "number"}


def integer_schema() -> JsonSchema:
    return {"type": "integer"}


def boolean_schema() -> JsonSchema:
    return {"type": "boolean"}


def null_schema() -> JsonSchema:
    return {"type": "null"}


def any_schema() -> JsonSchema:
    return {}


def nullable(schema: JsonSchema) -> JsonSchema:
    return {"anyOf": [schema, null_schema()]}


def array_schema(
    item_schema: JsonSchema | None = None,
    *,
    min_items: int | None = None,
    max_items: int | None = None,
) -> JsonSchema:
    schema: JsonSchema = {"type": "array", "items": item_schema or any_schema()}
    if min_items is not None:
        schema["minItems"] = int(min_items)
    if max_items is not None:
        schema["maxItems"] = int(max_items)
    return schema


def object_schema(
    properties: dict[str, JsonSchema] | None = None,
    *,
    required: list[str] | None = None,
    additional_properties: bool | JsonSchema = True,
) -> JsonSchema:
    schema: JsonSchema = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional_properties,
    }
    if required is not None:
        schema["required"] = list(required)
    return schema


def free_object_schema() -> JsonSchema:
    return object_schema(additional_properties=True)


def strict_object_schema(properties: dict[str, JsonSchema] | None = None) -> JsonSchema:
    props = properties or {}
    return object_schema(
        props,
        required=list(props),
        additional_properties=False,
    )


STRING_ARRAY = array_schema(string_schema())
FREE_OBJECT = free_object_schema()


AUTOCOMPLETE_EVIDENCE_SCHEMA = object_schema(
    {
        "visible_rows": array_schema(
            object_schema(
                {
                    "title": string_schema(),
                    "subtitle": string_schema(),
                    "category": string_schema(),
                    "row_order": integer_schema(),
                    "row_kind": string_schema(),
                    "evidence_reason": string_schema(),
                },
                required=[
                    "title",
                    "subtitle",
                    "category",
                    "row_order",
                    "row_kind",
                    "evidence_reason",
                ],
                additional_properties=False,
            )
        ),
        "total_visible_count": integer_schema(),
        "empty_reason": string_schema(),
    },
    required=["visible_rows", "total_visible_count", "empty_reason"],
    additional_properties=False,
)


PAGEGEN_RESOURCE_SEMANTIC_LABELING_SCHEMA = object_schema(
    {
        "resources": array_schema(
            object_schema(
                {
                    "index": integer_schema(),
                    "semantic_label": string_schema(),
                    "asset_kind": string_schema(
                        enum=[
                            "cover_image",
                            "banner",
                            "avatar",
                            "logo",
                            "icon",
                            "symbol_icon",
                            "badge",
                            "map_marker",
                            "decorative_image",
                            "unknown",
                        ]
                    ),
                    "sprite_group": string_schema(
                        enum=["icon", "content_visual", "asset"]
                    ),
                    "visual_role": string_schema(),
                    "text_only": boolean_schema(),
                },
                required=[
                    "index",
                    "semantic_label",
                    "asset_kind",
                    "sprite_group",
                    "visual_role",
                    "text_only",
                ],
                additional_properties=False,
            )
        )
    },
    required=["resources"],
    additional_properties=False,
)


GENERATE_HTML_SCHEMA = object_schema(
    {
        "html_content": string_schema(),
    },
    required=["html_content"],
    additional_properties=False,
)


GENERATE_HTML_FIX_SCHEMA = object_schema(
    {
        "edits": array_schema(
            object_schema(
                {
                    "oldText": string_schema(),
                    "newText": string_schema(),
                    "reason": string_schema(),
                },
                required=["oldText", "newText", "reason"],
                additional_properties=False,
            )
        )
    },
    required=["edits"],
    additional_properties=False,
)


GENERATE_SDK_METHODS_SCHEMA = object_schema(
    {
        "methods": string_schema(),
    },
    required=["methods"],
    additional_properties=False,
)


INTERFACE_ENTITY_REFERENCE_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "displayField": string_schema(),
        "valueField": string_schema(),
    }
)


INTERFACE_PARAMETER_SCHEMA = strict_object_schema(
    {
        "name": string_schema(),
        "type": string_schema(),
        "required": boolean_schema(),
        "entityReference": INTERFACE_ENTITY_REFERENCE_SCHEMA,
    }
)


INTERFACE_RETURN_PROPERTY_SCHEMA = strict_object_schema(
    {
        "name": string_schema(),
        "type": string_schema(),
        "description": string_schema(),
        "required": boolean_schema(),
    }
)


INTERFACE_RETURN_SCHEMA = strict_object_schema(
    {
        "type": string_schema(),
        "properties": array_schema(INTERFACE_RETURN_PROPERTY_SCHEMA),
        "items": array_schema(INTERFACE_RETURN_PROPERTY_SCHEMA),
    }
)


GENERATE_INTERFACE_GROUP_SCHEMA = strict_object_schema(
    {
        "interfaces": array_schema(
            strict_object_schema(
                {
                    "name": string_schema(),
                    "description": string_schema(),
                    "parameters": array_schema(INTERFACE_PARAMETER_SCHEMA),
                    "returns": INTERFACE_RETURN_SCHEMA,
                    "relatedTasks": STRING_ARRAY,
                }
            )
        )
    }
)


DISPATCH_INTERFACE_SCHEMA = strict_object_schema(
    {
        "name": string_schema(),
        "description": string_schema(),
        "param_names": STRING_ARRAY,
        "used_by": STRING_ARRAY,
        "related_entities": STRING_ARRAY,
    }
)


DISPATCH_INTERFACES_SCHEMA = strict_object_schema(
    {
        "capability_groups": array_schema(
            strict_object_schema(
                {
                    "group_id": string_schema(),
                    "semantic_contract": string_schema(),
                    "used_by": STRING_ARRAY,
                    "related_entities": STRING_ARRAY,
                    "interfaces": array_schema(DISPATCH_INTERFACE_SCHEMA),
                }
            )
        ),
        "helper_functions": array_schema(
            strict_object_schema(
                {
                    "name": string_schema(),
                    "description": string_schema(),
                }
            )
        ),
    }
)


EXTERNAL_UI_REGION_SCHEMA = strict_object_schema(
    {
        "region_type": string_schema(
            enum=[
                "os_notification_banner",
                "incoming_call_bar",
                "system_status_bar",
                "keyboard_ime",
                "permission_dialog",
                "app_switcher",
                "other",
            ]
        ),
        "location": string_schema(
            enum=["top", "bottom", "center", "full_screen", "unknown"]
        ),
        "description": string_schema(),
        "ignore_for_app_model": boolean_schema(),
    }
)


FRAME_STATE_DESCRIPTION_SCHEMA = object_schema(
    {
        "description": string_schema(),
        "body_pattern": string_schema(),
        "header_pattern": string_schema(),
        "shell_section": string_schema(),
        "sticky_action_pattern": string_schema(),
        "identity_confidence": number_schema(),
        "transition_from_previous": nullable(boolean_schema()),
        "transition_kind": string_schema(),
        "transition_evidence": STRING_ARRAY,
        "external_ui_regions": array_schema(EXTERNAL_UI_REGION_SCHEMA),
        "interaction_surface_role": string_schema(),
        "host_page_still_visible": nullable(boolean_schema()),
        "host_page_identity": string_schema(),
        "opened_surface_identity": string_schema(),
        "standalone_page_evidence": STRING_ARRAY,
        "host_page_evidence": STRING_ARRAY,
    },
    required=[
        "description",
        "body_pattern",
        "header_pattern",
        "shell_section",
        "sticky_action_pattern",
        "identity_confidence",
        "transition_from_previous",
        "transition_kind",
        "transition_evidence",
        "external_ui_regions",
        "interaction_surface_role",
        "host_page_still_visible",
        "host_page_identity",
        "opened_surface_identity",
        "standalone_page_evidence",
        "host_page_evidence",
    ],
    additional_properties=False,
)


REFERENCE_TRACE_SEMANTIC_EXTRACTOR_SCHEMA = object_schema(
    {
        "format": string_schema(),
        "runtime_state_observations": array_schema(
            strict_object_schema(
                {
                    "observation_id": string_schema(),
                    "trace_id": string_schema(),
                    "step_index": integer_schema(),
                    "obs_key": string_schema(),
                    "page_cluster_id": string_schema(),
                    "state_scope": string_schema(),
                    "lifecycle": string_schema(),
                    "mutation": string_schema(),
                    "value_hints": STRING_ARRAY,
                    "negative_constraints": STRING_ARRAY,
                    "evidence": STRING_ARRAY,
                }
            )
        ),
        "content_lifecycle_steps": array_schema(
            strict_object_schema(
                {
                    "trace_id": string_schema(),
                    "step_index": integer_schema(),
                    "obs_key": string_schema(),
                    "page_cluster_id": string_schema(),
                    "lifecycle_phase": string_schema(),
                    "exclude_from_initial_value_hints": STRING_ARRAY,
                    "reason": string_schema(),
                }
            )
        ),
    },
    required=["format", "runtime_state_observations", "content_lifecycle_steps"],
    additional_properties=False,
)


SCREENSHOT_RENDER_CLASSIFICATION_SCHEMA = strict_object_schema(
    {
        "unique_steps": array_schema(
            strict_object_schema(
                {
                    "step_key": string_schema(),
                    "reason": string_schema(),
                    "render_role": string_schema(),
                    "evidence": STRING_ARRAY,
                }
            )
        ),
        "duplicate_steps": array_schema(
            strict_object_schema(
                {
                    "step_key": string_schema(),
                    "duplicate_of": string_schema(),
                    "reason": string_schema(),
                    "render_role": string_schema(),
                    "evidence": STRING_ARRAY,
                }
            )
        ),
        "cluster_rationale": string_schema(),
    }
)


CONTENT_FIELD_SCHEMA = strict_object_schema(
    {
        "name": string_schema(),
        "value": string_schema(),
    }
)


CONTENT_ITEM_SCHEMA = strict_object_schema(
    {
        "item_ref": string_schema(),
        "fields": array_schema(CONTENT_FIELD_SCHEMA),
        "evidence_steps": STRING_ARRAY,
        "source_component": string_schema(),
        "reason": string_schema(),
    }
)


CONTENT_ENTITY_GROUP_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "items": array_schema(CONTENT_ITEM_SCHEMA),
    }
)


CONTENT_EXCLUDED_FROM_INITIAL_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "item_ref": string_schema(),
        "matched_hints": STRING_ARRAY,
        "reason": string_schema(),
    }
)


CONTENT_ITEM_LIFECYCLE_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "item_ref": string_schema(),
        "lifecycle_phase": string_schema(enum=["initial", "runtime_post_commit"]),
        "evidence_steps": STRING_ARRAY,
        "reason": string_schema(),
    }
)


CONTENT_ENTITY_COMPONENT_MAPPING_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "source_component": string_schema(),
        "relationship": string_schema(),
    }
)


CONTENT_ENTITY_TASK_RELEVANCE_SCHEMA = strict_object_schema(
    {
        "entity": string_schema(),
        "task_related": boolean_schema(),
        "role": string_schema(),
        "evidence_steps": STRING_ARRAY,
        "task_instruction_refs": STRING_ARRAY,
        "reason": string_schema(),
    }
)


CLUSTER_CONTENT_EXTRACTION_SCHEMA = strict_object_schema(
    {
        "confidence": number_schema(),
        "page_type_hint": string_schema(),
        "description": string_schema(),
        "metadata": strict_object_schema(
            {
                "active_tab": string_schema(),
                "active_filters": STRING_ARRAY,
            }
        ),
        "extracted_item_groups": array_schema(CONTENT_ENTITY_GROUP_SCHEMA),
        "runtime_item_groups": array_schema(CONTENT_ENTITY_GROUP_SCHEMA),
        "excluded_from_initial": array_schema(CONTENT_EXCLUDED_FROM_INITIAL_SCHEMA),
        "item_lifecycle": array_schema(CONTENT_ITEM_LIFECYCLE_SCHEMA),
        "entity_component_mapping": array_schema(CONTENT_ENTITY_COMPONENT_MAPPING_SCHEMA),
        "entity_task_relevance_entries": array_schema(CONTENT_ENTITY_TASK_RELEVANCE_SCHEMA),
        "validation_log": STRING_ARRAY,
    }
)


CLUSTER_CONTENT_EXTRACTION_REDUCE_SCHEMA = CLUSTER_CONTENT_EXTRACTION_SCHEMA


SCHEMA_FIELD_RENAME_SCHEMA = strict_object_schema(
    {
        "from": string_schema(),
        "to": string_schema(),
    }
)


SCHEMA_FIELD_POLICY_SCHEMA = strict_object_schema(
    {
        "keep": STRING_ARRAY,
        "normalize": array_schema(SCHEMA_FIELD_RENAME_SCHEMA),
    }
)


SCHEMA_SPLIT_REASON_SCHEMA = strict_object_schema(
    {
        "identity_lifecycle_diff": boolean_schema(),
        "task_requires_domain_fields": boolean_schema(),
        "domain_specific_fields": STRING_ARRAY,
        "why_optional_fields_not_enough": string_schema(),
    }
)


SCHEMA_PROJECTION_PLANNER_SCHEMA = strict_object_schema(
    {
        "canonical_boundaries": array_schema(
            strict_object_schema(
                {
                    "canonical_schema": string_schema(),
                    "members": STRING_ARRAY,
                    "boundary_type": string_schema(),
                    "reason": string_schema(),
                    "field_policy": SCHEMA_FIELD_POLICY_SCHEMA,
                }
            )
        ),
        "decisions": array_schema(
            strict_object_schema(
                {
                    "candidate": string_schema(),
                    "destination": string_schema(
                        enum=[
                            "entity_schema",
                            "merged_into_schema",
                            "content_sample_only",
                            "task_state_contract",
                            "interaction_contract",
                            "discarded_decorative",
                            "rejected_with_reason",
                        ]
                    ),
                    "projected_to": string_schema(),
                    "reason": string_schema(),
                    "split_reason": SCHEMA_SPLIT_REASON_SCHEMA,
                }
            )
        ),
    }
)


QUERY_IDENTITY_RESOLUTION_SCHEMA = strict_object_schema(
    {
        "query_id": string_schema(),
        "query_transition_kind": string_schema(),
        "query_state_owner": string_schema(),
        "target_identity_hints": STRING_ARRAY,
        "target_identity_status": string_schema(),
        "result_projection": string_schema(),
        "reason_code": string_schema(),
        "confidence": number_schema(),
    }
)


PAGE_ENVIRONMENT_QUERY_IDENTITY_ENRICHER_SCHEMA = strict_object_schema(
    {
        "query_identity_resolutions": array_schema(QUERY_IDENTITY_RESOLUTION_SCHEMA),
    }
)


PAGE_ENV_COMPONENT_SCHEMA = strict_object_schema(
    {
        "component_id": string_schema(),
        "component_kind": string_schema(),
        "component_role": string_schema(),
        "reads_state": string_schema(),
        "render_phase": string_schema(),
        "layout_policy": string_schema(),
        "source_step_keys": STRING_ARRAY,
        "l0_transition_ids": STRING_ARRAY,
        "confidence": number_schema(),
    }
)


PAGE_ENV_RENDER_PROJECTION_SCHEMA = strict_object_schema(
    {
        "projection_id": string_schema(),
        "component_ref": string_schema(),
        "reads_state": string_schema(),
        "render_phase": string_schema(),
        "layout_policy": string_schema(),
        "source_step_keys": STRING_ARRAY,
        "l0_transition_ids": STRING_ARRAY,
    }
)


PAGE_ENV_ORACLE_REQUIREMENT_SCHEMA = strict_object_schema(
    {
        "oracle_type": string_schema(),
        "step_key": string_schema(),
        "component_ref": string_schema(),
        "expected": string_schema(),
    }
)


PAGE_ENV_COMPONENT_PROJECTION_PAGE_SCHEMA = strict_object_schema(
    {
        "filename": string_schema(),
        "components": array_schema(PAGE_ENV_COMPONENT_SCHEMA),
        "render_projections": array_schema(PAGE_ENV_RENDER_PROJECTION_SCHEMA),
        "oracle_requirements": array_schema(PAGE_ENV_ORACLE_REQUIREMENT_SCHEMA),
    }
)


PAGE_ENVIRONMENT_COMPONENT_PROJECTION_ENRICHER_SCHEMA = strict_object_schema(
    {
        "pages": array_schema(PAGE_ENV_COMPONENT_PROJECTION_PAGE_SCHEMA),
    }
)


PAGE_ENV_COMPONENT_SPLIT_SCHEMA = strict_object_schema(
    {
        "component_role": string_schema(),
        "candidate_ids": STRING_ARRAY,
    }
)


PAGE_ENV_CONFLICT_RESOLUTION_SCHEMA = strict_object_schema(
    {
        "conflict_id": string_schema(),
        "selected_default": string_schema(),
        "state_variants": STRING_ARRAY,
        "runtime_only": STRING_ARRAY,
        "section_render": STRING_ARRAY,
        "visual_samples": STRING_ARRAY,
        "dropped": STRING_ARRAY,
        "component_splits": array_schema(PAGE_ENV_COMPONENT_SPLIT_SCHEMA),
        "reason_code": string_schema(),
        "confidence": number_schema(),
        "notes": string_schema(),
    }
)


PAGE_ENVIRONMENT_CONFLICT_RESOLVER_SCHEMA = strict_object_schema(
    {
        "resolutions": array_schema(PAGE_ENV_CONFLICT_RESOLUTION_SCHEMA),
    }
)


ENVIRONMENT_ROUTE_SCHEMA = strict_object_schema(
    {
        "path": string_schema(),
        "params": strict_object_schema({}),
        "queryParams": strict_object_schema({}),
        "entryPoint": string_schema(),
        "scroll_containers": STRING_ARRAY,
    }
)


ENVIRONMENT_UI_ACTION_SCHEMA = strict_object_schema(
    {
        "id": string_schema(),
        "label": string_schema(),
        "behavior": string_schema(),
        "target_route": string_schema(),
    }
)


ENVIRONMENT_UI_STATE_SCHEMA = strict_object_schema(
    {
        "id": string_schema(),
        "search": strict_object_schema({}),
        "description": string_schema(),
        "actions": array_schema(ENVIRONMENT_UI_ACTION_SCHEMA),
    }
)


ENVIRONMENT_TYPED_ACTION_SCHEMA = strict_object_schema(
    {
        "action_id": string_schema(),
        "label": string_schema(),
        "behavior": string_schema(),
        "target_filename": string_schema(),
        "state_effect": string_schema(),
        "surface": string_schema(),
    }
)


ENVIRONMENT_ROUTE_BINDING_SCHEMA = strict_object_schema(
    {
        "target_filename": string_schema(),
        "source_filename": string_schema(),
        "path": string_schema(),
        "rationale": string_schema(),
        "control_label": string_schema(),
    }
)


ENVIRONMENT_SDK_EFFECT_SCHEMA = strict_object_schema(
    {
        "effect": string_schema(),
        "trigger": string_schema(),
        "method": string_schema(),
        "target_state": string_schema(),
    }
)


ENVIRONMENT_INTERACTION_CONTRACT_SCHEMA = strict_object_schema(
    {
        "contract_id": string_schema(),
        "candidate_id": string_schema(),
        "action_semantic": string_schema(),
        "state_ref": string_schema(),
        "entity_ref": string_schema(),
        "identity_evidence": STRING_ARRAY,
        "route_binding": ENVIRONMENT_ROUTE_BINDING_SCHEMA,
        "sdk_effect": ENVIRONMENT_SDK_EFFECT_SCHEMA,
        "source": string_schema(),
    }
)


ENVIRONMENT_NAVIGATION_EDGE_SCHEMA = strict_object_schema(
    {
        "control_label": string_schema(),
        "target_filename": string_schema(),
        "surface": string_schema(),
        "confidence": number_schema(),
    }
)


ENVIRONMENT_QUERY_BINDING_SCHEMA = strict_object_schema(
    {
        "query_id": string_schema(),
        "query_transition_kind": string_schema(),
        "target_identity_status": string_schema(),
        "target_identity_hints": STRING_ARRAY,
        "reason_code": string_schema(),
    }
)


ENVIRONMENT_ACTION_ROUTE_CONFLICT_RESOLUTION_SCHEMA = strict_object_schema(
    {
        "conflict_id": string_schema(),
        "selected_default": string_schema(),
        "state_variants": STRING_ARRAY,
        "runtime_only": STRING_ARRAY,
        "dropped": STRING_ARRAY,
        "reason_code": string_schema(),
    }
)


ENVIRONMENT_ACTION_ROUTE_PLANNER_SCHEMA = strict_object_schema(
    {
        "route": ENVIRONMENT_ROUTE_SCHEMA,
        "ui_states": array_schema(ENVIRONMENT_UI_STATE_SCHEMA),
        "typed_actions": array_schema(ENVIRONMENT_TYPED_ACTION_SCHEMA),
        "interaction_contracts": array_schema(ENVIRONMENT_INTERACTION_CONTRACT_SCHEMA),
        "navigation_edges": array_schema(ENVIRONMENT_NAVIGATION_EDGE_SCHEMA),
        "query_bindings": array_schema(ENVIRONMENT_QUERY_BINDING_SCHEMA),
        "conflict_resolutions": array_schema(
            ENVIRONMENT_ACTION_ROUTE_CONFLICT_RESOLUTION_SCHEMA
        ),
        "state_signature_hints": STRING_ARRAY,
    }
)


SPRITE_PAGE_MERGE_ADMISSION_SCHEMA = strict_object_schema(
    {
        "merge_proposals": array_schema(
            strict_object_schema(
                {
                    "victim_page_id": string_schema(),
                    "target_page_id": string_schema(),
                    "reason": string_schema(),
                    "variant_type": string_schema(),
                }
            )
        ),
        "reasoning": string_schema(),
    }
)


PAGE_ASSIGNMENT_AUDIT_SCHEMA = strict_object_schema(
    {
        "target_page_id": string_schema(),
        "batch_id": string_schema(),
        "summary": strict_object_schema(
            {
                "total": integer_schema(),
                "keep": integer_schema(),
                "move_existing": integer_schema(),
                "create_new_page": integer_schema(),
                "skip": integer_schema(),
                "uncertain": integer_schema(),
                "target_page_purity": string_schema(enum=["pure", "mixed"]),
            }
        ),
        "new_pages": array_schema(
            strict_object_schema(
                {
                    "proposed_page_id": string_schema(),
                    "page_name": string_schema(),
                    "route_path": nullable(string_schema()),
                    "page_type_hint": string_schema(
                        enum=[
                            "homepage", "search_entry", "search_results",
                            "form", "settings", "list", "detail", "utility",
                        ]
                    ),
                    "description": string_schema(),
                    "distinguishing_visual_signature": STRING_ARRAY,
                    "seed_tile_ids": STRING_ARRAY,
                    "split_from_page_id": string_schema(),
                    "why_existing_candidates_do_not_fit": nullable(string_schema()),
                }
            )
        ),
        "decisions": array_schema(
            strict_object_schema(
                {
                    "tile_id": string_schema(),
                    "trace_id": string_schema(),
                    "step_index": integer_schema(),
                    "current_assigned_page_id": string_schema(),
                    "verdict": string_schema(
                        enum=[
                            "keep", "move_existing", "create_new_page",
                            "skip", "uncertain",
                        ]
                    ),
                    "corrected_page_id": nullable(string_schema()),
                    "proposed_new_page_id": nullable(string_schema()),
                    "confidence": number_schema(),
                    "durable_page_identity": string_schema(),
                    "target_encodes_durable_identity": boolean_schema(),
                    "override_transition_hint": boolean_schema(),
                    "route_or_shell_evidence": STRING_ARRAY,
                    "reason": string_schema(),
                    "visual_evidence": STRING_ARRAY,
                    "visual_mismatch": STRING_ARRAY,
                }
            )
        ),
    }
)


def _build_instance_split_schema(
    *,
    require_split: bool = False,
    require_explicit_instance: bool = False,
    compact_membership: bool = False,
) -> JsonSchema:
    entry_action_audit = strict_object_schema(
        {
            "audited_obs_keys": STRING_ARRAY,
            "task_content_obs_keys": STRING_ARRAY,
            "stable_shell_control_obs_keys": STRING_ARRAY,
        }
    )
    member_action_audit = array_schema(
        strict_object_schema(
            {
                "obs_key": string_schema(),
                "current_action_target": string_schema(),
                "target_role": string_schema(
                    enum=["task_content", "stable_shell_control"]
                ),
                "bounded_slot": string_schema(
                    enum=["first", "second", "named", "none"]
                ),
                "visible_entity": string_schema(),
            }
        )
    )
    split_reason_base = {
        "dimension": string_schema(),
        "same_general_page": boolean_schema(),
        "why_not_same_instance": string_schema(),
    }


    split_reason_variants = []
    for include_why_not_general_page in (False, True):
        for include_member_action_audit in (False, True):
            properties = dict(split_reason_base)
            if include_why_not_general_page:
                properties["why_not_general_page"] = string_schema()
            if include_member_action_audit:
                properties["member_action_audit"] = member_action_audit
            split_reason_variants.append(strict_object_schema(properties))
    split_reason = {"anyOf": split_reason_variants}
    instance_base_properties = {
        "instance_id": string_schema(),
        "instance_label": string_schema(),
        "instance_key_hint": string_schema(),
        "obs_keys": array_schema(
            string_schema(),
            min_items=1,
        ),
        "split_dimension": string_schema(),
        "split_reason": split_reason,
        "reason": string_schema(),
    }
    if not compact_membership:
        instance_base_properties["trace_steps"] = array_schema(
            strict_object_schema(
                {
                    "trace_id": string_schema(),
                    "step_index": integer_schema(),
                }
            )
        )


    instance_variants = []
    for include_step_indices in ((False,) if compact_membership else (False, True)):
        properties = dict(instance_base_properties)
        if include_step_indices:
            properties["step_indices"] = array_schema(integer_schema())
        instance_variants.append(strict_object_schema(properties))
    instance_schema = {"anyOf": instance_variants}
    out_of_page_item_schema = strict_object_schema(
        {
            "obs_key": string_schema(),
            "observed_general_page": string_schema(),
            "why_not_page_identity": string_schema(),
        }
    )

    def candidate_properties(*, split_needed: bool) -> dict[str, JsonSchema]:
        if split_needed:
            return {
                "instances": array_schema(
                    instance_schema,
                    min_items=1 if require_explicit_instance else None,
                ),
                "out_of_page_obs": array_schema(out_of_page_item_schema),
                "split_needed": {"type": "boolean", "enum": [True]},
            }
        return {
            "instances": array_schema(instance_schema, max_items=0),
            "out_of_page_obs": array_schema(out_of_page_item_schema, max_items=0),
            "split_needed": {"type": "boolean", "enum": [False]},
        }

    variants = []
    for include_entry_action_audit in (False, True):
        for split_needed in ((True,) if require_split else (False, True)):
            properties = candidate_properties(split_needed=split_needed)
            if include_entry_action_audit:
                properties = {
                    "entry_action_audit": entry_action_audit,
                    **properties,
                }
            variants.append(strict_object_schema(properties))
    return {"anyOf": variants}


INSTANCE_SPLIT_SCHEMA = _build_instance_split_schema()
INSTANCE_SPLIT_PRODUCER_SHARD_SCHEMA = _build_instance_split_schema(
    require_split=True,
    require_explicit_instance=True,
)
INSTANCE_SPLIT_MERGE_SCHEMA = _build_instance_split_schema(
    require_split=True,
    require_explicit_instance=True,
    compact_membership=True,
)


TRACE_IDENTITY_JUDGE_SCHEMA = strict_object_schema(
    {
        "trace_id": string_schema(),
        "mismatches": array_schema(
            strict_object_schema(
                {
                    "step_index": integer_schema(),
                    "current_cluster_id": string_schema(),
                    "recommended_cluster_id": string_schema(),
                    "severity": string_schema(enum=["fixloop", "diagnostic"]),
                    "reason_code": string_schema(),
                    "confidence": number_schema(),
                    "evidence": STRING_ARRAY,
                    "route_implication": string_schema(),
                }
            )
        ),
        "non_mismatch_notes": STRING_ARRAY,
    }
)


RAW_TRACE_STEP_PREDICTION_SCHEMA = strict_object_schema(
    {
        "step_index": integer_schema(),
        "verdict": string_schema(enum=["keep", "drop"]),
        "thinking": string_schema(),
        "action": string_schema(),
        "reason": string_schema(min_length=1),
    }
)


RAW_TRACE_TERMINAL_CONTRACT_SCHEMA = strict_object_schema(
    {
        "terminal_policy_candidate": string_schema(
            enum=["precommit_ready", "simulated_commit", "observed_terminal_state"]
        ),
        "confidence": string_schema(enum=["high", "medium", "low"]),
        "terminal_boundary_description": string_schema(),
        "outcome_fields": array_schema(string_schema()),
        "last_task_relevant_step": integer_schema(),
        "commit_action_observed": boolean_schema(),
        "forbidden_terminal_events": array_schema(string_schema()),
        "required_mock_state": array_schema(string_schema()),
        "evidence": array_schema(string_schema()),
        "uncertainties": array_schema(string_schema()),
    }
)


GENERATE_TRACE_THOUGHT_AND_ACTION_SCHEMA = strict_object_schema(
    {
        "step_predictions": array_schema(RAW_TRACE_STEP_PREDICTION_SCHEMA),
        "task_instruction": string_schema(),
        "terminal_contract": RAW_TRACE_TERMINAL_CONTRACT_SCHEMA,
    }
)


COMPACT_CHUNK_SCHEMA = strict_object_schema(
    {
        "app_type": string_schema(),
        "page_candidates": array_schema(
            strict_object_schema(
                {
                    "local_page_id": string_schema(),
                    "page_kind": string_schema(),
                    "page_name": string_schema(),
                    "description": string_schema(),
                    "primary_functions": STRING_ARRAY,
                    "route_path": string_schema(),
                    "include_as_page": boolean_schema(),
                    "evidence_segments": STRING_ARRAY,
                    "state_variants": STRING_ARRAY,
                }
            )
        ),
        "segment_assignments": array_schema(
            strict_object_schema(
                {
                    "segment_id": string_schema(),
                    "local_page_id": string_schema(),
                    "is_page_transition": boolean_schema(),
                    "confidence": number_schema(),
                }
            )
        ),
        "state_overlays": array_schema(
            strict_object_schema(
                {
                    "segment_id": string_schema(),
                    "underlying_local_page_id": string_schema(),
                    "state_kind": string_schema(),
                }
            )
        ),
        "skip_segments": array_schema(
            strict_object_schema(
                {
                    "segment_id": string_schema(),
                    "skip_reason": string_schema(),
                }
            )
        ),
    }
)


COMPACT_MERGE_SCHEMA = strict_object_schema(
    {
        "app_type": string_schema(),
        "local_to_global": array_schema(
            strict_object_schema(
                {
                    "local_page_id": string_schema(),
                    "global_page_id": string_schema(),
                }
            )
        ),
        "canonical_pages": array_schema(
            strict_object_schema(
                {
                    "page_cluster_id": string_schema(),
                    "page_type_hint": string_schema(),
                    "page_name": string_schema(),
                    "description": string_schema(),
                    "primary_functions": STRING_ARRAY,
                    "route_path": string_schema(),
                    "parent_route_path": string_schema(),
                    "route_level": integer_schema(),
                }
            )
        ),
        "state_policy": array_schema(
            strict_object_schema(
                {
                    "state_kind": string_schema(),
                    "materialize_as_page": boolean_schema(),
                    "underlying_global_page_id": string_schema(),
                }
            )
        ),
        "task_related_elements": strict_object_schema({}),
    }
)


_STAGE_RESPONSE_SCHEMAS: dict[str, JsonSchema] = {
    "reference_trace_semantic_extractor": REFERENCE_TRACE_SEMANTIC_EXTRACTOR_SCHEMA,
    "frame_state_description": FRAME_STATE_DESCRIPTION_SCHEMA,
    "site_model_global_reasoning_screenshot_render_classification": (
        SCREENSHOT_RENDER_CLASSIFICATION_SCHEMA
    ),
    "autocomplete_evidence_extraction": AUTOCOMPLETE_EVIDENCE_SCHEMA,
    "pagegen_resource_semantic_labeling": PAGEGEN_RESOURCE_SEMANTIC_LABELING_SCHEMA,
    "cluster_content_extraction": CLUSTER_CONTENT_EXTRACTION_SCHEMA,
    "cluster_content_extraction_reduce": CLUSTER_CONTENT_EXTRACTION_REDUCE_SCHEMA,
    "schema_projection_planner": SCHEMA_PROJECTION_PLANNER_SCHEMA,
    "page_environment_query_identity_enricher": PAGE_ENVIRONMENT_QUERY_IDENTITY_ENRICHER_SCHEMA,
    "page_environment_l0_component_render_binding_planner": PAGE_ENVIRONMENT_COMPONENT_PROJECTION_ENRICHER_SCHEMA,
    "page_environment_component_projection_enricher": PAGE_ENVIRONMENT_COMPONENT_PROJECTION_ENRICHER_SCHEMA,
    "page_environment_conflict_resolver": PAGE_ENVIRONMENT_CONFLICT_RESOLVER_SCHEMA,
    "environment_action_route_planner": ENVIRONMENT_ACTION_ROUTE_PLANNER_SCHEMA,
    "generate_interface_group": GENERATE_INTERFACE_GROUP_SCHEMA,
    "dispatch_interfaces": DISPATCH_INTERFACES_SCHEMA,
    "sprite_page_merge_admission": SPRITE_PAGE_MERGE_ADMISSION_SCHEMA,
    "generate_html": GENERATE_HTML_SCHEMA,
    "generate_html_retry_text": GENERATE_HTML_SCHEMA,
    "generate_html_retry_none": GENERATE_HTML_SCHEMA,
    "generate_sdk_methods": GENERATE_SDK_METHODS_SCHEMA,
    "page_assignment_audit": PAGE_ASSIGNMENT_AUDIT_SCHEMA,
    "instance_split": INSTANCE_SPLIT_SCHEMA,
    "site_model_global_reasoning_trace_identity_judge": TRACE_IDENTITY_JUDGE_SCHEMA,
    "generate_trace_thought_and_action_from_raw": GENERATE_TRACE_THOUGHT_AND_ACTION_SCHEMA,
}


_DYNAMIC_STAGE_RESPONSE_SCHEMA_PATTERNS: tuple[tuple[re.Pattern[str], JsonSchema], ...] = (
    (
        re.compile(r"^instance_split_producer_\d+$"),
        INSTANCE_SPLIT_PRODUCER_SHARD_SCHEMA,
    ),
    (
        re.compile(r"^instance_split_merge_r\d+_b\d+$"),
        INSTANCE_SPLIT_MERGE_SCHEMA,
    ),
    (
        re.compile(r"^site_model_global_reasoning_compact_chunk_\d+$"),
        COMPACT_CHUNK_SCHEMA,
    ),
    (
        re.compile(r"^site_model_global_reasoning_compact_merge_round_\d+_batch_\d+$"),
        COMPACT_MERGE_SCHEMA,
    ),
    (
        re.compile(r"^site_model_global_reasoning_compact_merge_final$"),
        COMPACT_MERGE_SCHEMA,
    ),
    (
        re.compile(r"^cascade_.+_round_1$"),
        GENERATE_HTML_SCHEMA,
    ),
    (
        re.compile(r"^cascade_.+_round_(?!1$)\d+$"),
        GENERATE_HTML_FIX_SCHEMA,
    ),
)


def _resolve_dynamic_stage_schema(stage: str) -> JsonSchema | None:
    for pattern, schema in _DYNAMIC_STAGE_RESPONSE_SCHEMA_PATTERNS:
        if pattern.match(stage):
            return schema
    return None


def resolve_llm_response_schema(stage: str | None) -> JsonSchema | None:
    if not stage:
        return None
    schema = _STAGE_RESPONSE_SCHEMAS.get(stage)
    if schema is None and stage.startswith("generate_html_fix_"):
        schema = GENERATE_HTML_FIX_SCHEMA
    if schema is None:
        schema = _resolve_dynamic_stage_schema(stage)
    return deepcopy(schema) if schema is not None else None


def _is_strict_json_schema_compatible(schema: Any) -> bool:
    if not isinstance(schema, dict) or not schema:
        return False
    if "anyOf" in schema:
        items = schema.get("anyOf")
        return isinstance(items, list) and bool(items) and all(
            _is_strict_json_schema_compatible(item) for item in items
        )
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return False
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required", [])) != set(properties):
            return False
        return all(_is_strict_json_schema_compatible(value) for value in properties.values())
    if schema_type == "array":
        return _is_strict_json_schema_compatible(schema.get("items"))
    return schema_type in {"string", "number", "integer", "boolean", "null"}


def is_strict_llm_response_schema_compatible(schema: Any) -> bool:
    return _is_strict_json_schema_compatible(schema)


def resolve_strict_llm_response_schema(stage: str | None) -> JsonSchema | None:
    schema = resolve_llm_response_schema(stage)
    if not _is_strict_json_schema_compatible(schema):
        return None
    return schema


def registered_llm_response_schema_stages() -> tuple[str, ...]:
    return tuple(sorted(_STAGE_RESPONSE_SCHEMAS))


def llm_response_schema_patterns() -> tuple[str, ...]:
    return tuple(pattern.pattern for pattern, _schema in _DYNAMIC_STAGE_RESPONSE_SCHEMA_PATTERNS)
