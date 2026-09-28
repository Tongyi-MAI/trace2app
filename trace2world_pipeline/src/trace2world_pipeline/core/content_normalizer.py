
import copy
import re
from typing import Any, Dict, List, Optional


def _to_pascal(name: str) -> str:
    if "_" in name:
        return "".join(p.capitalize() for p in name.split("_"))

    return name[0].upper() + name[1:] if name else ""


def _snake_to_camel(snake: str) -> str:
    if "_" not in snake:
        return snake
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _prefix(name: str) -> str:
    return "".join(p[0] for p in name.split("_"))


def assign_ids_and_resolve_fks(
    static_data: Dict[str, Any],
    relationships: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    data = copy.deepcopy(static_data)


    collections: Dict[str, list] = {}
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            collections[k] = v

    if not collections:
        return data


    for col_name, items in collections.items():
        pfx = _prefix(col_name)
        for i, item in enumerate(items):
            if not item.get("id"):
                item["id"] = f"{pfx}_{i + 1}"

    real_ids: Dict[str, list] = {
        col: [it["id"] for it in items]
        for col, items in collections.items()
    }


    if relationships:

        name_to_storage: Dict[str, str] = {}
        for col_name in collections:

            name_to_storage[col_name] = col_name
            pascal = _to_pascal(col_name)
            name_to_storage[pascal] = col_name

        for rel in relationships:
            from_pascal = rel.get("from", "")
            to_pascal = rel.get("to", "")
            fk_field = rel.get("field", "")
            if not from_pascal or not to_pascal or not fk_field:
                continue
            from_col = name_to_storage.get(from_pascal)
            to_col = name_to_storage.get(to_pascal)
            if not from_col or not to_col:
                continue
            if from_col not in collections or to_col not in collections:
                continue
            is_id_fk = fk_field.endswith("_id")
            to_ids = real_ids.get(to_col, [])
            if not to_ids:
                continue
            from_items = collections[from_col]
            camel_fk = _snake_to_camel(fk_field)
            for i, item in enumerate(from_items):
                if fk_field not in item:
                    if camel_fk in item and camel_fk != fk_field:
                        val = item.pop(camel_fk)
                        if is_id_fk:
                            item[fk_field] = val if val in to_ids else to_ids[i % len(to_ids)]
                        else:
                            item[fk_field] = val
                    elif is_id_fk:
                        item[fk_field] = to_ids[i % len(to_ids)]
                elif is_id_fk:
                    val = item[fk_field]
                    if isinstance(val, str) and val not in to_ids:
                        item[fk_field] = to_ids[i % len(to_ids)]

        real_ids = {
            col: [it["id"] for it in items]
            for col, items in collections.items()
        }


    fk_to_col: Dict[str, str] = {}
    for col_name in collections:
        camel = _snake_to_camel(col_name) + "Id"
        fk_to_col[camel] = col_name

    for col_name, items in collections.items():
        fk_placeholders: Dict[str, list] = {}
        for item in items:
            for field, value in item.items():
                if (
                    field.endswith("Id")
                    and field != "id"
                    and isinstance(value, str)
                ):
                    ref_col = fk_to_col.get(field)
                    if ref_col and ref_col in real_ids:
                        if value not in real_ids[ref_col]:
                            fk_placeholders.setdefault(field, [])
                            if value not in fk_placeholders[field]:
                                fk_placeholders[field].append(value)

        if not fk_placeholders:
            continue

        for item in items:
            for field, placeholders in fk_placeholders.items():
                value = item.get(field)
                if value in placeholders:
                    ref_col = fk_to_col[field]
                    idx = placeholders.index(value)
                    ref_list = real_ids[ref_col]
                    item[field] = ref_list[idx] if idx < len(ref_list) else ref_list[0]

    return data
