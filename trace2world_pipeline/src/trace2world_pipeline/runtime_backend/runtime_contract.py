
import re
from typing import Any, Dict, List, Optional


def _clean_method_stem(name: str) -> str:
    return "".join(ch for ch in (name or "Entity") if ch.isalnum()) or "Entity"


def _lower_camel(name: str) -> str:
    stem = _clean_method_stem(name)
    return stem[:1].lower() + stem[1:]


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name or "item")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return value or "item"


def _plural(name: str) -> str:
    if not name:
        return "Items"
    if name.endswith("y") and (len(name) < 2 or name[-2].lower() not in "aeiou"):
        return name[:-1] + "ies"
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


def _iter_interfaces(interfaces: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    if not isinstance(interfaces, dict):
        return items
    for value in interfaces.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    items.append(item)
    return items


def _return_properties(interface: Dict[str, Any]) -> Dict[str, Any]:
    returns = interface.get("returns", {})
    if not isinstance(returns, dict):
        return {}
    props = returns.get("properties", {})
    return props if isinstance(props, dict) else {}


def _array_return_aliases(interface: Dict[str, Any]) -> List[str]:
    aliases = []
    for prop_name, prop_def in _return_properties(interface).items():
        if isinstance(prop_def, dict) and prop_def.get("type") == "array":
            aliases.append(prop_name)
    return aliases


def _normalized_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _entity_alias_tokens(entity_name: str, entity: Dict[str, Any]) -> set[str]:
    stem = str(entity.get("method_stem") or _clean_method_stem(entity_name))
    plural = str(entity.get("method_plural_stem") or _plural(stem))
    storage_key = str(entity.get("storage_key") or "")
    tokens = {
        entity_name,
        stem,
        plural,
        _lower_camel(stem),
        _lower_camel(plural),
        _snake_case(entity_name),
        _snake_case(plural),
        storage_key,
    }
    return {_normalized_alias(token) for token in tokens if token}


def _entity_from_return_aliases(interface: Dict[str, Any], entities: Dict[str, Dict[str, Any]]) -> Optional[str]:
    aliases = _array_return_aliases(interface) + _object_return_aliases(interface)
    for alias in aliases:
        normalized = _normalized_alias(alias)
        if not normalized:
            continue
        for entity_name, entity in entities.items():
            if normalized in _entity_alias_tokens(entity_name, entity):
                return entity_name
    return None


def _object_return_aliases(interface: Dict[str, Any]) -> List[str]:
    aliases = []
    for prop_name, prop_def in _return_properties(interface).items():
        if isinstance(prop_def, dict) and prop_def.get("type") == "object":
            aliases.append(prop_name)
    return aliases


def _operation_from_name(method_name: str) -> str:
    return "custom"


def _entity_from_method_name(method_name: str, entity_names: List[str]) -> Optional[str]:
    normalized_name = _normalized_alias(method_name)
    for entity_name in sorted(entity_names, key=len, reverse=True):
        if _normalized_alias(entity_name) in normalized_name:
            return entity_name
    return None


def _entity_from_parameters(interface: Dict[str, Any]) -> Optional[str]:
    params = interface.get("parameters", [])
    if not isinstance(params, list):
        return None
    for param in params:
        if not isinstance(param, dict):
            continue
        ref = param.get("entityReference")
        if isinstance(ref, dict) and ref.get("entity"):
            return ref.get("entity")
    return None


def _identifier_aliases(param_name: str) -> List[str]:
    if not param_name:
        return []
    aliases = set([param_name, _snake_case(param_name)])
    if param_name != "mode":
        aliases.add("id")
    return sorted(alias for alias in aliases if alias)


def _identifier_parameters(interface: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    params = interface.get("parameters", [])
    if not isinstance(params, list):
        return result
    for param in params:
        if not isinstance(param, dict):
            continue
        name = str(param.get("name", "") or "").strip()
        if not name or name == "mode":
            continue
        has_entity_ref = isinstance(param.get("entityReference"), dict)
        is_identifier_name = name == "id" or name.endswith("Id") or name.endswith("_id")
        if has_entity_ref or is_identifier_name:
            result.append(param)
    return result


def _operation_intent_from_interface(operation: str, has_identifier: bool) -> str:
    return operation or "custom"


def _handle_kind_from_scope(scope: str, action_semantic: str = "") -> str:
    scope = str(scope or "").strip()
    action_semantic = str(action_semantic or "").strip()
    if scope == "accumulator":
        return "accumulator_ref"
    if scope == "collection":
        return "collection_ref"
    if scope == "draft":
        return "draft_ref"
    if scope == "inline_ui":
        return "inline_ui_state_ref"
    if scope == "selection":
        return "selected_option_ref"
    if scope == "session":
        return "session_ref"
    if scope == "transaction":
        return "transaction_ref"
    if scope == "record":
        return "record_ref"
    if action_semantic == "append_accumulator":
        return "accumulator_ref"
    if action_semantic in {"open_collection", "update_filter"}:
        return "collection_ref"
    if action_semantic in {"create_record_draft", "update_current_draft"}:
        return "draft_ref"
    if action_semantic in {"inline_ui_state", "read_only"}:
        return "inline_ui_state_ref"
    if action_semantic.startswith("select_"):
        return "selected_option_ref"
    if action_semantic == "commit_transaction":
        return "transaction_ref"
    if action_semantic == "edit_existing_record":
        return "record_ref"
    return "state_ref"


def _stateful_contexts_from_methods(methods_by_entity: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    contexts = []
    for entity_name, methods in methods_by_entity.items():
        for method_name, method in methods.items():
            if method.get("source") != "declared_interface":
                continue
            params = method.get("parameters", [])
            interface_shape = {"parameters": params}
            identifier_params = _identifier_parameters(interface_shape)
            if not identifier_params:
                continue
            aliases = []
            required = False
            param_names = []
            for param in identifier_params:
                name = str(param.get("name", "") or "")
                param_names.append(name)
                aliases.extend(_identifier_aliases(name))
                required = required or bool(param.get("required", False))
            aliases = sorted(set(aliases))
            operation = method.get("operation", "")
            intent = method.get("operation_intent") or _operation_intent_from_interface(operation, bool(identifier_params))
            contexts.append({
                "context_id": "interface:{}:{}".format(method_name, entity_name),
                "source": "interface",
                "entity": entity_name,
                "method": method_name,
                "operation": operation,
                "operation_intent": intent,
                "state_handle": {
                    "kind": "record_ref",
                    "entity_ref": entity_name,
                    "identifier_params": param_names,
                    "identifier_aliases": aliases,
                    "required": required,
                    "source": "query_param|dom_dataset|current_context",
                },
                "carriers": [
                    {"type": "query_param", "aliases": aliases},
                    {"type": "dom_dataset", "aliases": aliases},
                ],
                "hydration": {
                    "required": operation in {"get", "form_page_model", "list_page_model"},
                    "mode": "edit" if operation == "form_page_model" else "record",
                    "method": method_name,
                },
                "sdk_effect": {
                    "effect": operation,
                    "method": method_name,
                },
            })
    return contexts


def _iter_task_contracts(task_interaction_contracts: Any) -> List[Dict[str, Any]]:
    if isinstance(task_interaction_contracts, dict):
        contracts = task_interaction_contracts.get("contracts", [])
        if isinstance(contracts, list):
            return [item for item in contracts if isinstance(item, dict)]
    if isinstance(task_interaction_contracts, list):
        return [item for item in task_interaction_contracts if isinstance(item, dict)]
    return []


def _stateful_contexts_from_task_contracts(task_interaction_contracts: Any) -> List[Dict[str, Any]]:
    contexts = []
    for contract in _iter_task_contracts(task_interaction_contracts):
        contract_id = str(contract.get("contract_id", "") or contract.get("candidate_id", "") or "")
        action_semantic = str(contract.get("action_semantic", "") or "").strip()
        state_scope = str(contract.get("state_scope", "") or "").strip()
        route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        params = route_binding.get("params", [])
        if not isinstance(params, list):
            params = []
        aliases = []
        for param_name in params:
            aliases.extend(_identifier_aliases(str(param_name or "")))
        aliases = sorted(set(aliases))
        if not (action_semantic or state_scope or aliases or contract.get("state_ref") or contract.get("entity_ref")):
            continue
        contexts.append({
            "context_id": "task_interaction:{}".format(contract_id or len(contexts)),
            "source": "task_interaction_contract",
            "operation_intent": action_semantic or "unknown",
            "state_scope": state_scope,
            "source_lifecycle": contract.get("source_lifecycle", ""),
            "runtime_state_observation_ids": [
                str(item) for item in contract.get("runtime_state_observation_ids", [])
            ] if isinstance(contract.get("runtime_state_observation_ids", []), list) else [],
            "runtime_state_obligation_ids": [
                str(item) for item in contract.get("runtime_state_obligation_ids", [])
            ] if isinstance(contract.get("runtime_state_obligation_ids", []), list) else [],
            "terminal_constraints": [
                str(item) for item in contract.get("terminal_constraints", [])
            ] if isinstance(contract.get("terminal_constraints", []), list) else [],
            "state_handle": {
                "kind": _handle_kind_from_scope(state_scope, action_semantic),
                "state_ref": contract.get("state_ref", ""),
                "entity_ref": contract.get("entity_ref", ""),
                "identifier_params": [str(item or "") for item in params],
                "identifier_aliases": aliases,
                "required": bool(route_binding.get("requires_params", False)) or bool(hydration.get("required", False)),
                "source": route_binding.get("source", "") or hydration.get("source_ref", ""),
            },
            "carriers": [
                {"type": "query_param", "aliases": aliases},
                {"type": "dom_dataset", "aliases": aliases},
            ] if aliases else [],
            "route_binding": route_binding,
            "hydration": hydration,
            "sdk_effect": sdk_effect,
        })
    return contexts


def _finite_runtime_signature_from_task_contracts(task_interaction_contracts: Any) -> Dict[str, Any]:
    dimensions = []
    seen = set()
    for contract in _iter_task_contracts(task_interaction_contracts):
        contract_id = str(contract.get("contract_id", "") or contract.get("candidate_id", "") or "").strip()
        action_semantic = str(contract.get("action_semantic", "") or "").strip()
        state_scope = str(contract.get("state_scope", "") or "").strip()
        state_ref = str(contract.get("state_ref", "") or "").strip()
        entity_ref = str(contract.get("entity_ref", "") or "").strip()
        route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding", {}), dict) else {}
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration", {}), dict) else {}
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect", {}), dict) else {}
        params = route_binding.get("params", [])
        if not isinstance(params, list):
            params = []
        effect = str(sdk_effect.get("effect", "") or "").strip()
        method = str(sdk_effect.get("method", "") or "").strip()
        handle_kind = _handle_kind_from_scope(state_scope, action_semantic)
        if not (contract_id or state_ref or entity_ref or state_scope or action_semantic):
            continue
        key = (contract_id, state_scope, state_ref, entity_ref, action_semantic, tuple(str(item or "") for item in params))
        if key in seen:
            continue
        seen.add(key)
        seed = "|".join(str(item or "") for item in key)
        dimension_id = "sig_" + _snake_case(seed)[:80]
        dimensions.append({
            "dimension_id": dimension_id,
            "contract_id": contract_id,
            "state_scope": state_scope,
            "state_ref": state_ref,
            "entity_ref": entity_ref,
            "handle_kind": handle_kind,
            "action_semantic": action_semantic,
            "route_params": [str(item or "") for item in params],
            "route_requires_params": bool(route_binding.get("requires_params", False)),
            "hydration_required": bool(hydration.get("required", False)),
            "hydration_mode": str(hydration.get("mode", "") or ""),
            "hydration_state_ref": str(hydration.get("state_ref", "") or state_ref),
            "sdk_effect": effect,
            "sdk_method": method,
        })
    return {
        "format": "finite_runtime_signature",
        "schema_revision": 1,
        "state_key": {
            "strategy": "page_id_plus_signature_dimensions",
            "hash_input": ["page_id", "dimensions"],
        },
        "dimensions": dimensions,
    }


def _build_entities(entity_schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    entities = {}
    if not isinstance(entity_schema, dict):
        return entities

    raw_entities = entity_schema.get("entities")
    if isinstance(raw_entities, list):
        iterable = raw_entities
    else:
        iterable = []
        for name, spec in entity_schema.items():
            if isinstance(spec, dict) and not name.startswith("_"):
                item = dict(spec)
                item.setdefault("name", name)
                iterable.append(item)

    for entity in iterable:
        if not isinstance(entity, dict):
            continue
        name = entity.get("name")
        if not name or str(name).startswith("_"):
            continue
        stem = _clean_method_stem(name)
        plural = _plural(stem)
        fields = []
        for field in entity.get("fields", []) or []:
            if isinstance(field, dict) and field.get("name"):
                field_contract = {
                    "name": field.get("name"),
                    "type": field.get("type", "string"),
                }
                if field.get("required") is not None:
                    field_contract["required"] = bool(field.get("required"))
                if field.get("observed_examples") is not None:
                    field_contract["observed_examples"] = field.get("observed_examples")
                fields.append(field_contract)
        entities[name] = {
            "name": name,
            "storage_key": entity.get("storage_key") or _snake_case(name) + "s",
            "method_stem": stem,
            "method_plural_stem": plural,
            "lower_stem": _lower_camel(stem),
            "lower_plural_stem": _lower_camel(plural),
            "snake_stem": _snake_case(name),
            "fields": fields,
        }
    return entities


def _default_method_specs(entity: Dict[str, Any]) -> List[Dict[str, Any]]:
    return []


def build_runtime_contract(entity_schema: Dict[str, Any],
                           interfaces: Optional[Dict[str, Any]] = None,
                           task_interaction_contracts: Optional[Any] = None,
                           render_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entities = _build_entities(entity_schema or {})
    entity_names = list(entities.keys())
    methods_by_entity = {name: {} for name in entity_names}

    for entity_name, entity in entities.items():
        for spec in _default_method_specs(entity):
            methods_by_entity[entity_name][spec["name"]] = {
                "name": spec["name"],
                "operation": spec["operation"],
                "entity": entity_name,
                "source": "derived_from_entity_schema",
                "collection_aliases": [],
                "record_aliases": [],
            }

    for interface in _iter_interfaces(interfaces or {}):
        method_name = interface.get("name")
        entity_name = str(interface.get("entity", "") or interface.get("entity_ref", "") or "").strip()
        if entity_name not in entities:
            entity_name = _entity_from_parameters(interface)
        if entity_name not in entities:
            entity_name = _entity_from_return_aliases(interface, entities)
        if entity_name not in entities:
            entity_name = _entity_from_method_name(method_name, entity_names)
        if entity_name not in entities:
            continue
        operation = str(interface.get("operation", "") or interface.get("operation_intent", "") or "").strip() or _operation_from_name(method_name)
        methods_by_entity[entity_name][method_name] = {
            "name": method_name,
            "operation": operation,
            "operation_intent": str(interface.get("operation_intent", "") or ""),
            "entity": entity_name,
            "source": "declared_interface",
            "collection_aliases": _array_return_aliases(interface),
            "record_aliases": _object_return_aliases(interface),
            "parameters": interface.get("parameters", []),
        }

    stateful_contexts = _stateful_contexts_from_methods(methods_by_entity)
    stateful_contexts.extend(_stateful_contexts_from_task_contracts(task_interaction_contracts))

    contract = {
        "version": "runtime_contract",
        "entities": entities,
        "methods_by_entity": methods_by_entity,
        "stateful_continuation_contexts": stateful_contexts,
        "finite_runtime_signature": _finite_runtime_signature_from_task_contracts(task_interaction_contracts),
    }
    if isinstance(render_context, dict) and render_context:
        contract["environment"] = {
            "authority_ref": (
                "phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
                "render_context.json"
            ),
            "environment_profile_hash": render_context.get("profile_hash"),
            "platform": render_context.get("platform"),
            "viewport": render_context.get("viewport"),
            "browser_viewport": render_context.get("browser_viewport"),
            "input_mode": render_context.get("input_mode"),
            "shell_mode": render_context.get("shell_mode"),
        }
    return contract
