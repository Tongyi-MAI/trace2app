from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import threading
import time
from typing import Any

from openai import AsyncOpenAI, OpenAI

from trace2world_pipeline.core.llm_response_schemas import (
    is_strict_llm_response_schema_compatible,
    resolve_strict_llm_response_schema,
)
from trace2world_pipeline.core.token_tracker import get_token_tracker

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a declared dependency
    Image = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMJsonResponseError(ValueError):
    def __init__(self, message: str, *, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class LLMAPIResponseError(RuntimeError):
    ...


# ---------------------------------------------------------------------------
# Defaults and configuration state
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOKENS = 32768
DEFAULT_MAX_OUTPUT_TOKENS_CAP = 40000
DEFAULT_MAX_RETRIES = 5
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_API_TIMEOUT = float(os.getenv("LLM_API_TIMEOUT", "500"))
DEFAULT_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEFAULT_ENDPOINTS = [DEFAULT_BASE_URL]
DEFAULT_DEPLOYMENT = "gpt-5.2-2025-12-11"

# Chat-completions models that accept the ``reasoning_effort`` parameter.
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_NO_REASONING_EFFORT = {"none", "off", "disabled"}

_endpoints: list[str] = list(DEFAULT_ENDPOINTS)
_endpoint_index = 0
_endpoint_lock = threading.Lock()
current_deployment = DEFAULT_DEPLOYMENT
_max_output_tokens_cap = DEFAULT_MAX_OUTPUT_TOKENS_CAP
_stage_model_routing: dict[str, str] = {}
_model_concurrency_config: dict[str, int] = {}
_global_max_concurrent = 0
_api_log_file = None


def configure_load_balancing(endpoints=None, strategy=None, deployment=None, **_ignored):
    """Set the OpenAI-compatible base URLs (round-robin) and the default model."""
    global _endpoints, _endpoint_index, current_deployment
    if endpoints:
        cleaned = [str(e).rstrip("/") for e in endpoints if str(e).strip()]
        if cleaned:
            _endpoints = cleaned
            _endpoint_index = 0
    if deployment:
        current_deployment = str(deployment)
    if strategy and strategy != "round_robin":
        _api_print(f"load_balance_strategy={strategy!r} is not supported; using round_robin")


def configure_stage_model_routing(routing: dict[str, str] | None):
    """Map a stage name (or stage-name prefix) to a model."""
    global _stage_model_routing
    _stage_model_routing = {str(k): str(v) for k, v in (routing or {}).items() if v}


def configure_model_concurrency(model: str, max_concurrent: int, initial_concurrent: int | None = None):
    _model_concurrency_config[str(model)] = max(1, int(max_concurrent))
    _semaphores.pop(("model", str(model)), None)


def configure_global_concurrency(max_concurrent: int, initial_concurrent: int | None = None):
    global _global_max_concurrent
    _global_max_concurrent = max(0, int(max_concurrent or 0))
    _semaphores.pop(("global", ""), None)


def configure_api_log(path: str):
    global _api_log_file
    if _api_log_file:
        _api_log_file.close()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _api_log_file = open(path, "a", buffering=1, encoding="utf-8")
    print(f"API monitoring log: {path}")


def _api_print(msg: str):
    if _api_log_file:
        _api_log_file.write(msg + "\n")
    else:
        print(msg)


def _resolve_stage_model(model: str, stage: str) -> str:
    if not _stage_model_routing or not stage:
        return model
    if stage in _stage_model_routing:
        return _stage_model_routing[stage]
    for prefix, override_model in _stage_model_routing.items():
        if stage.startswith(prefix):
            return override_model
    return model


def _resolve_requested_model(model: str | None, stage: str | None) -> str:
    requested = model if model is not None else current_deployment
    return _resolve_stage_model(requested, stage or "")


def estimate_llm_stage_parallelism(
    model: str | None,
    stage: str | None,
    response_format: dict | None = None,
    messages: list[dict] | None = None,
    *,
    default: int = 1,
) -> int:
    """Single-endpoint client: the caller's requested parallelism is the answer."""
    return max(1, int(default or 1))


# ---------------------------------------------------------------------------
# Clients and concurrency
# ---------------------------------------------------------------------------

_sync_clients: dict[str, OpenAI] = {}
_async_clients: dict[tuple[int, str], AsyncOpenAI] = {}
_semaphores: dict[tuple[str, str], tuple[int, asyncio.Semaphore]] = {}


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def get_next_endpoint() -> str:
    global _endpoint_index
    with _endpoint_lock:
        endpoint = _endpoints[_endpoint_index % len(_endpoints)]
        _endpoint_index = (_endpoint_index + 1) % len(_endpoints)
    return endpoint


def _get_sync_client(endpoint: str) -> OpenAI:
    client = _sync_clients.get(endpoint)
    if client is None:
        client = OpenAI(
            api_key=_api_key() or "missing-api-key",
            base_url=endpoint,
            timeout=DEFAULT_API_TIMEOUT,
            max_retries=DEFAULT_MAX_RETRIES,
        )
        _sync_clients[endpoint] = client
    return client


def get_async_client_for_endpoint(endpoint: str) -> AsyncOpenAI:
    loop = asyncio.get_event_loop()
    key = (id(loop), endpoint)
    client = _async_clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=_api_key() or "missing-api-key",
            base_url=endpoint,
            timeout=DEFAULT_API_TIMEOUT,
            max_retries=DEFAULT_MAX_RETRIES,
        )
        _async_clients[key] = client
    return client


async def close_async_clients_for_current_loop() -> int:
    loop_id = id(asyncio.get_event_loop())
    closed = 0
    for key in [k for k in _async_clients if k[0] == loop_id]:
        client = _async_clients.pop(key)
        try:
            await client.close()
            closed += 1
        except Exception:
            pass
    return closed


def _semaphore(kind: str, name: str, limit: int) -> asyncio.Semaphore | None:
    if limit <= 0:
        return None
    loop_id = id(asyncio.get_event_loop())
    entry = _semaphores.get((kind, name))
    if entry is None or entry[0] != loop_id:
        entry = (loop_id, asyncio.Semaphore(limit))
        _semaphores[(kind, name)] = entry
    return entry[1]


def _resolve_concurrency_semaphore(deployment: str) -> asyncio.Semaphore | None:
    if deployment in _model_concurrency_config:
        return _semaphore("model", deployment, _model_concurrency_config[deployment])
    return _semaphore("global", "", _global_max_concurrent)


# ---------------------------------------------------------------------------
# Images and messages
# ---------------------------------------------------------------------------


def encode_image(image_path):
    if str(image_path).startswith(("http://", "https://", "data:image/")):
        return image_path
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file {image_path} not found")
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"
    return f"data:{mime_type};base64,{img_b64}"


def encode_image_downsampled(image_path, max_width=1280, max_height=720, jpeg_quality=85):
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file {image_path} not found")
    if Image is None:
        return encode_image(image_path)
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        img.thumbnail((max(1, int(max_width)), max(1, int(max_height))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=max(1, min(95, int(jpeg_quality))), optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def _normalize_messages_for_chat_api(messages):
    normalized = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    parts.append(item)
                    continue
                item_type = item.get("type")
                if item_type == "input_text":
                    parts.append({"type": "text", "text": item.get("text", "")})
                elif item_type == "input_image":
                    image_url = item.get("image_url", "")
                    if isinstance(image_url, str):
                        parts.append({"type": "image_url", "image_url": {"url": image_url}})
                    else:
                        parts.append({"type": "image_url", "image_url": image_url})
                else:
                    parts.append(item)
            normalized.append({"role": role, "content": parts})
        else:
            normalized.append({"role": role, "content": content})
    return normalized


# ---------------------------------------------------------------------------
# Response formats and JSON contract validation
# ---------------------------------------------------------------------------


def _json_object_response_format(json_schema: dict | None = None) -> dict:
    response_format: dict = {"type": "json_object"}
    if json_schema:
        response_format["json_schema"] = json_schema
    return response_format


def _json_schema_from_response_format(response_format: dict | None) -> dict | None:
    if not isinstance(response_format, dict):
        return None
    if isinstance(response_format.get("json_schema"), dict):
        json_schema = response_format["json_schema"]
        if isinstance(json_schema.get("schema"), dict):
            return json_schema["schema"]
        return json_schema
    if isinstance(response_format.get("schema"), dict):
        return response_format["schema"]
    return None


def _response_format_with_registered_schema(response_format: dict | None, stage: str | None) -> dict | None:
    if not isinstance(response_format, dict):
        return response_format
    if _json_schema_from_response_format(response_format):
        return response_format
    if response_format.get("type") not in {"json_object", "json_schema"}:
        return response_format
    schema = resolve_strict_llm_response_schema(stage)
    if not schema:
        return response_format
    updated = dict(response_format)
    updated["json_schema"] = schema
    return updated


def _response_format_schema_name(response_format: dict) -> str:
    json_schema = response_format.get("json_schema")
    if isinstance(json_schema, dict) and isinstance(json_schema.get("name"), str):
        return json_schema["name"]
    return "trace2world_response"


def _openai_compatible_response_format(response_format: dict | None) -> dict | None:
    if not isinstance(response_format, dict):
        return response_format
    schema = _json_schema_from_response_format(response_format)
    if schema:
        if not is_strict_llm_response_schema_compatible(schema):
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _response_format_schema_name(response_format),
                "strict": True,
                "schema": schema,
            },
        }
    if response_format.get("type") in {"json_object", "json_schema"}:
        return {"type": "json_object"} if response_format.get("type") == "json_object" else response_format
    return response_format


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _schema_validation_errors(value: Any, schema: dict[str, Any] | None, path: str = "$") -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        branch_errors = [
            _schema_validation_errors(value, branch, path)
            for branch in schema["anyOf"]
            if isinstance(branch, dict)
        ]
        if any(not errors for errors in branch_errors):
            return []
        return [f"{path}: does not match anyOf"]

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _json_type_matches(value, expected_type):
        return [f"{path}: expected {expected_type}, got {type(value).__name__}"]

    errors: list[str] = []
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path}: value {value!r} not in enum")

    if expected_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}.{key}: missing required field")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(_schema_validation_errors(value[key], child_schema, f"{path}.{key}"))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: unexpected fields {extra[:8]}")
        elif isinstance(additional, dict):
            for key in sorted(set(value) - set(properties)):
                errors.extend(_schema_validation_errors(value[key], additional, f"{path}.{key}"))

    if expected_type == "array" and isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: expected at least {min_items} items, got {len(value)}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: expected at most {max_items} items, got {len(value)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(_schema_validation_errors(item, item_schema, f"{path}[{idx}]"))
    return errors


# ---------------------------------------------------------------------------
# JSON reply repair
# ---------------------------------------------------------------------------


def _extract_last_parseable_json_value(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, str]] = []
    for index, ch in enumerate(stripped):
        if ch not in "{[":
            continue
        try:
            _parsed, end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        candidate = stripped[index:index + end].strip()
        if candidate:
            candidates.append((index, index + end, candidate))
    if not candidates:
        return None
    _s, _e, best = max(candidates, key=lambda item: (item[1], -item[0]))
    return best


def _extract_json_from_text(text: str) -> str:
    stripped = text.strip()
    if (stripped.startswith("{") or stripped.startswith("[")) and (stripped.endswith("}") or stripped.endswith("]")):
        return stripped
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", stripped)
    if m:
        candidate = m.group(1).strip()
        if candidate and (candidate.startswith("{") or candidate.startswith("[")):
            return candidate
    parseable = _extract_last_parseable_json_value(stripped)
    if parseable:
        return parseable
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        if start == -1:
            continue
        end = stripped.rfind(close_ch)
        if end > start:
            return stripped[start:end + 1]
    return stripped


def _escape_unescaped_inner_quotes_in_json_strings(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False
    length = len(text)
    for index, ch in enumerate(text):
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue
        if ch == '"':
            lookahead = index + 1
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            next_ch = text[lookahead] if lookahead < length else ""
            if next_ch in {":", ",", "}", "]"}:
                chars.append(ch)
                in_string = False
            else:
                chars.append('\\"')
            continue
        chars.append(ch)
    return "".join(chars)


def _validate_and_fix_json_reply(reply: str, deployment: str) -> str:
    try:
        json.loads(reply)
        return reply
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _extract_json_from_text(reply)
    try:
        json.loads(repaired)
        return repaired
    except (json.JSONDecodeError, ValueError):
        quote_repaired = _escape_unescaped_inner_quotes_in_json_strings(repaired)
        try:
            json.loads(quote_repaired)
            return quote_repaired
        except (json.JSONDecodeError, ValueError):
            _api_print(f"[{deployment}] JSON repair failed")
            return reply


def _strip_complete_json_fence(reply: str) -> str:
    match = re.fullmatch(
        r"[ \t\r\n]*```(?:json)?[ \t]*\r?\n(?P<payload>[\s\S]*?)\r?\n?```[ \t\r\n]*",
        reply,
        flags=re.IGNORECASE,
    )
    if match is None:
        return reply
    return match.group("payload").strip()


def _extract_html_content_from_text(reply: str) -> str | None:
    stripped = (reply or "").strip()
    if not stripped:
        return None
    fence = re.search(r"```(?:html)?\s*\n?([\s\S]*?)\n?```", stripped, re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    lowered = stripped.lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        return stripped
    return None


def _normalize_generate_html_reply(reply: str, deployment: str, stage: str | None) -> str | None:
    if stage != "generate_html":
        return None
    html_content = _extract_html_content_from_text(reply)
    if not html_content:
        return None
    return json.dumps({"html_content": html_content}, ensure_ascii=False)


def _json_object_loads_ok(reply: str | None) -> bool:
    if not reply:
        return False
    try:
        return isinstance(json.loads(reply), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _raise_invalid_json_reply(reply: str, deployment: str, stage: str | None) -> None:
    preview = (reply or "").strip().replace("\n", "\\n")
    if len(preview) > 240:
        preview = preview[:240] + "..."
    raise LLMJsonResponseError(
        f"[{stage or 'unknown_stage'}] {deployment} returned invalid JSON after repair: {preview}",
        raw_response=reply,
    )


def _validate_contract_schema(reply: str, deployment: str, stage: str | None, response_format: dict | None) -> None:
    schema = _json_schema_from_response_format(response_format) or resolve_strict_llm_response_schema(stage)
    if schema is None:
        return
    try:
        parsed = json.loads(reply)
    except (json.JSONDecodeError, ValueError):
        _raise_invalid_json_reply(reply, deployment, stage)
    errors = _schema_validation_errors(parsed, schema)
    if errors:
        raise LLMJsonResponseError(
            f"[{stage or 'unknown_stage'}] {deployment} returned JSON that violates contract schema: "
            + "; ".join(errors[:6]),
            raw_response=reply,
        )


def _validate_json_reply_for_deployment(reply: str, deployment: str, stage: str | None, response_format: dict | None) -> str:
    if reply:
        if _json_schema_from_response_format(response_format):
            repaired = _strip_complete_json_fence(reply)
        else:
            repaired = _validate_and_fix_json_reply(reply, deployment)
        if _json_object_loads_ok(repaired):
            _validate_contract_schema(repaired, deployment, stage, response_format)
            return repaired
        normalized = _normalize_generate_html_reply(repaired, deployment, stage)
        if normalized and _json_object_loads_ok(normalized):
            _validate_contract_schema(normalized, deployment, stage, response_format)
            return normalized
        _raise_invalid_json_reply(repaired, deployment, stage)
    _raise_invalid_json_reply(reply, deployment, stage)


# ---------------------------------------------------------------------------
# Request building and response handling
# ---------------------------------------------------------------------------


def _supports_reasoning_effort(model: str) -> bool:
    return str(model).startswith(REASONING_MODEL_PREFIXES)


def _effective_max_tokens(max_tokens) -> int:
    if os.getenv("LLM_DEBUG_USE_REQUEST_MAX_TOKENS") == "1":
        return int(max_tokens or DEFAULT_MAX_TOKENS)
    return _max_output_tokens_cap


def _validate_optional_float(name: str, value, lower: float, upper: float):
    if value is None:
        return None
    number = float(value)
    if number < lower or number > upper:
        raise ValueError(f"{name} must be within [{lower}, {upper}], got {value!r}")
    return number


def _build_chat_params(deployment, messages, max_tokens, response_format=None, reasoning_effort=None, temperature=None, top_p=None) -> dict:
    chat_params: dict[str, Any] = {
        "model": deployment,
        "messages": _normalize_messages_for_chat_api(messages),
        "max_completion_tokens": _effective_max_tokens(max_tokens),
    }
    temperature = _validate_optional_float("temperature", temperature, 0.0, 2.0)
    top_p = _validate_optional_float("top_p", top_p, 0.0, 1.0)
    if temperature is not None:
        chat_params["temperature"] = temperature
    if top_p is not None:
        chat_params["top_p"] = top_p
    if response_format:
        chat_params["response_format"] = _openai_compatible_response_format(response_format)
    effort = reasoning_effort or DEFAULT_REASONING_EFFORT
    if _supports_reasoning_effort(deployment) and effort not in _NO_REASONING_EFFORT:
        chat_params["reasoning_effort"] = effort
    return chat_params


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            return value
    return 0


def _usage_info_from_response(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    try:
        prompt_tokens = int(_usage_value(usage, "prompt_tokens", "input_tokens") or 0)
        completion_tokens = int(_usage_value(usage, "completion_tokens", "output_tokens") or 0)
        total_tokens = int(_usage_value(usage, "total_tokens") or 0)
    except (TypeError, ValueError):
        prompt_tokens = completion_tokens = total_tokens = 0
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {"total_tokens": total_tokens, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


def _extract_text_from_chat_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(getattr(part, "text", "") or "")
            for part in content
        )
    return "" if content is None else str(content)


def _process_chat_response(response) -> tuple[str, dict[str, int]]:
    reply = ""
    choices = getattr(response, "choices", None) or []
    if choices:
        reply = _extract_text_from_chat_content(getattr(choices[0].message, "content", ""))
    return reply, _usage_info_from_response(response)


def _track_token_usage(deployment: str, usage_info: dict, stage: str | None) -> None:
    try:
        get_token_tracker().record_usage(
            model=deployment,
            input_tokens=usage_info["prompt_tokens"],
            output_tokens=usage_info["completion_tokens"],
            total_tokens=usage_info["total_tokens"],
            stage=stage,
        )
    except Exception as e:  # pragma: no cover - accounting must never break a call
        print(f"Warning: Failed to record token usage: {e}")


def _log_call(stage: str | None, deployment: str, endpoint: str, started_at: float, usage_info: dict | None, note: str = "") -> None:
    duration = time.perf_counter() - started_at
    usage = usage_info or {}
    _api_print(
        f"[{stage or 'unknown_stage'}] {deployment} endpoint={endpoint} duration={duration:.2f}s "
        f"prompt_tokens={usage.get('prompt_tokens', 0)} completion_tokens={usage.get('completion_tokens', 0)}"
        + (f" {note}" if note else "")
    )


def _compact_api_error(error: Exception) -> str:
    text = str(error).replace("\n", " ").strip()
    return text[:400] + ("..." if len(text) > 400 else "")


def _raise_api_response_error(*, deployment: str, endpoint: str | None, stage: str | None, reason: str, original_error: Exception | None = None) -> None:
    message = f"[{stage or 'unknown_stage'}] {deployment} API call failed: {reason} endpoint={endpoint or 'unknown_endpoint'}"
    if original_error is not None:
        message += f": {_compact_api_error(original_error)}"
        raise LLMAPIResponseError(message) from original_error
    raise LLMAPIResponseError(message)


def _finalize_reply(reply: str, usage_info: dict, *, deployment: str, endpoint: str, stage: str | None, response_format: dict | None, started_at: float) -> tuple[str, dict]:
    if not reply or not reply.strip():
        _log_call(stage, deployment, endpoint, started_at, usage_info, note="empty_response")
        _raise_api_response_error(deployment=deployment, endpoint=endpoint, stage=stage, reason="empty_response")
    if response_format:
        try:
            reply = _validate_json_reply_for_deployment(reply, deployment, stage, response_format)
        except LLMJsonResponseError:
            _log_call(stage, deployment, endpoint, started_at, usage_info, note="json_contract_failure")
            raise
    _track_token_usage(deployment, usage_info, stage)
    _log_call(stage, deployment, endpoint, started_at, usage_info)
    return reply, usage_info


# ---------------------------------------------------------------------------
# Public chat-completions calls
# ---------------------------------------------------------------------------


def call_openai_api(messages, max_tokens=DEFAULT_MAX_TOKENS, max_retries=DEFAULT_MAX_RETRIES, response_format=None, reasoning_effort=DEFAULT_REASONING_EFFORT, stage=None, model=None, stream=None):
    """Synchronous chat completion. Returns ``(reply_text, usage_info)``."""
    response_format = _response_format_with_registered_schema(response_format, stage)
    deployment = _resolve_requested_model(model, stage)
    endpoint = get_next_endpoint()
    params = _build_chat_params(deployment, messages, max_tokens, response_format, reasoning_effort)
    started_at = time.perf_counter()
    try:
        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}
            parts, usage_info = [], {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
            for chunk in _get_sync_client(endpoint).chat.completions.create(**params):
                if getattr(chunk, "choices", None):
                    parts.append(_extract_text_from_chat_content(getattr(chunk.choices[0].delta, "content", "")))
                if getattr(chunk, "usage", None):
                    usage_info = _usage_info_from_response(chunk)
            reply = "".join(parts)
        else:
            reply, usage_info = _process_chat_response(_get_sync_client(endpoint).chat.completions.create(**params))
    except Exception as e:
        _log_call(stage, deployment, endpoint, started_at, None, note=f"api_error: {_compact_api_error(e)}")
        _raise_api_response_error(deployment=deployment, endpoint=endpoint, stage=stage, reason="api_error", original_error=e)
    return _finalize_reply(reply, usage_info, deployment=deployment, endpoint=endpoint, stage=stage, response_format=response_format, started_at=started_at)


def call_openai_api_json(messages, max_tokens=DEFAULT_MAX_TOKENS, max_retries=DEFAULT_MAX_RETRIES, reasoning_effort=DEFAULT_REASONING_EFFORT, stage=None, model=None, json_schema=None):
    """Synchronous JSON-mode call. Returns ``(json_text, usage_info)``; ``json_text`` is validated."""
    response_format = _response_format_with_registered_schema(_json_object_response_format(json_schema), stage)
    return call_openai_api(messages, max_tokens, max_retries, response_format=response_format, reasoning_effort=reasoning_effort, stage=stage, model=model)


async def call_openai_api_async(messages, max_tokens=DEFAULT_MAX_TOKENS, max_retries=DEFAULT_MAX_RETRIES, response_format=None, reasoning_effort=DEFAULT_REASONING_EFFORT, model=None, stage=None, stream=None, throughput_controller=None, temperature=None, top_p=None):
    """Asynchronous chat completion. Returns ``(reply_text, usage_info)``."""
    response_format = _response_format_with_registered_schema(response_format, stage)
    deployment = _resolve_requested_model(model, stage)
    endpoint = get_next_endpoint()
    client = get_async_client_for_endpoint(endpoint)
    params = _build_chat_params(deployment, messages, max_tokens, response_format, reasoning_effort, temperature, top_p)
    sem = _resolve_concurrency_semaphore(deployment)
    if sem is not None:
        await sem.acquire()
    started_at = time.perf_counter()
    try:
        try:
            if stream:
                params["stream"] = True
                params["stream_options"] = {"include_usage": True}
                parts, usage_info = [], {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
                async for chunk in await client.chat.completions.create(**params):
                    if getattr(chunk, "choices", None):
                        parts.append(_extract_text_from_chat_content(getattr(chunk.choices[0].delta, "content", "")))
                    if getattr(chunk, "usage", None):
                        usage_info = _usage_info_from_response(chunk)
                reply = "".join(parts)
            else:
                reply, usage_info = _process_chat_response(await client.chat.completions.create(**params))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log_call(stage, deployment, endpoint, started_at, None, note=f"api_error: {_compact_api_error(e)}")
            _raise_api_response_error(deployment=deployment, endpoint=endpoint, stage=stage, reason="api_error", original_error=e)
        return _finalize_reply(reply, usage_info, deployment=deployment, endpoint=endpoint, stage=stage, response_format=response_format, started_at=started_at)
    finally:
        if sem is not None:
            sem.release()


async def call_openai_api_json_async(messages, max_tokens=DEFAULT_MAX_TOKENS, max_retries=DEFAULT_MAX_RETRIES, reasoning_effort=DEFAULT_REASONING_EFFORT, model=None, stage=None, json_schema=None, throughput_controller=None):
    """Asynchronous JSON-mode call. Returns ``(json_text, usage_info)``; ``json_text`` is validated."""
    response_format = _response_format_with_registered_schema(_json_object_response_format(json_schema), stage)
    return await call_openai_api_async(messages, max_tokens, max_retries, response_format=response_format, reasoning_effort=reasoning_effort, model=model, stage=stage)


# Kept as aliases: the generation stages used to route through a separate pool.
_call_generation_api_async = call_openai_api_async
_call_generation_api_json_async = call_openai_api_json_async


# ---------------------------------------------------------------------------
# Responses API with tools
# ---------------------------------------------------------------------------


def _responses_tool_params(deployment, input_messages, tools, *, max_tokens, reasoning_effort, instructions, previous_response_id, store, parallel_tool_calls, tool_choice, truncation, reasoning_summary, respect_requested_max_tokens) -> dict:
    max_output_tokens = int(max_tokens) if respect_requested_max_tokens else _effective_max_tokens(max_tokens)
    params: dict[str, Any] = {
        "model": deployment,
        "input": input_messages,
        "tools": tools,
        "max_output_tokens": max_output_tokens,
        "store": bool(store),
    }
    effort = reasoning_effort or DEFAULT_REASONING_EFFORT
    if _supports_reasoning_effort(deployment) and effort not in _NO_REASONING_EFFORT:
        params["reasoning"] = {"effort": effort}
        if reasoning_summary is not None:
            params["reasoning"]["summary"] = reasoning_summary
    if instructions is not None:
        params["instructions"] = instructions
    if previous_response_id is not None:
        params["previous_response_id"] = previous_response_id
    if parallel_tool_calls is not None:
        params["parallel_tool_calls"] = bool(parallel_tool_calls)
    if tool_choice is not None:
        params["tool_choice"] = tool_choice
    if truncation is not None:
        params["truncation"] = truncation
    return params


async def call_openai_api_with_tools_async(input_messages, tools, max_retries=DEFAULT_MAX_RETRIES, reasoning_effort=DEFAULT_REASONING_EFFORT, model=None, stage=None, client=None, max_tokens=DEFAULT_MAX_TOKENS, instructions=None, previous_response_id=None, store=False, parallel_tool_calls=None, tool_choice=None, truncation=None, reasoning_summary=None, respect_requested_max_tokens=False):
    """Responses API call with tools. Returns ``(response, usage_info)``."""
    deployment = _resolve_requested_model(model, stage)
    endpoint = "custom-client"
    if client is None:
        endpoint = get_next_endpoint()
        client = get_async_client_for_endpoint(endpoint)
    params = _responses_tool_params(
        deployment, input_messages, tools,
        max_tokens=max_tokens, reasoning_effort=reasoning_effort, instructions=instructions,
        previous_response_id=previous_response_id, store=store, parallel_tool_calls=parallel_tool_calls,
        tool_choice=tool_choice, truncation=truncation, reasoning_summary=reasoning_summary,
        respect_requested_max_tokens=respect_requested_max_tokens,
    )
    sem = _resolve_concurrency_semaphore(deployment)
    if sem is not None:
        await sem.acquire()
    started_at = time.perf_counter()
    try:
        try:
            response = await client.responses.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log_call(stage, deployment, endpoint, started_at, None, note=f"api_error (tools): {_compact_api_error(e)}")
            _raise_api_response_error(deployment=deployment, endpoint=endpoint, stage=stage, reason="api_error", original_error=e)
        if response is None:
            _raise_api_response_error(deployment=deployment, endpoint=endpoint, stage=stage, reason="empty_response")
        usage_info = _usage_info_from_response(response)
        _track_token_usage(deployment, usage_info, stage)
        _log_call(stage, deployment, endpoint, started_at, usage_info, note="tools")
        return response, usage_info
    finally:
        if sem is not None:
            sem.release()
