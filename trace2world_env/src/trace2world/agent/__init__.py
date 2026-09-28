
import inspect
from typing import Any

from trace2world.schema.config import AgentConfig

from .variants import (
    AgentVariant,
    PolicyFactory,
    get_agent,
    get_entry,
    list_agents,
    load_builtin_agents,
    register_policy_factory,
    register_variant,
)

__all__ = [
    "AgentVariant",
    "PolicyFactory",
    "create_policy",
    "get_agent",
    "get_entry",
    "list_agents",
    "register_policy_factory",
    "register_variant",
]


def create_policy(cfg: AgentConfig) -> Any:
    entry = get_entry(cfg.type)
    if isinstance(entry, AgentVariant):
        return _build_variant_policy(entry, cfg)
    if entry is not None:
        return entry(cfg)

    raise ValueError(f"Unknown agent type {cfg.type!r}; available: {sorted(list_agents())}")


def _ctor_accepts(cls: type, name: str) -> bool:
    for klass in cls.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        params = inspect.signature(init).parameters
        if name in params:
            return True
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            return False
    return False


def _build_variant_policy(variant: AgentVariant, cfg: AgentConfig) -> Any:
    rc = cfg.runtime_conf or {}
    extra = variant.extra or {}
    available_apps = rc.get("available_apps")
    if available_apps is not None:
        if (
            not isinstance(available_apps, list)
            or not available_apps
            or not all(isinstance(app, str) and bool(app.strip()) for app in available_apps)
        ):
            raise ValueError(
                "agent.runtime_conf.available_apps must be a non-empty list of non-empty strings"
            )
        available_apps = [app.strip() for app in available_apps]
    system_prompt = variant.render_system_prompt(available_apps)


    def _knob(key: str) -> Any:
        return rc.get(key, extra.get(key))


    server_pixels = bool(extra.get("release_server_pixels"))

    if cfg.llm_backend != "http":
        raise ValueError(f"Unsupported llm_backend: {cfg.llm_backend!r}")
    from trace2world.agent.backends.http_chat_model import HttpChatModel

    chat_model = HttpChatModel(
        url=cfg.llm_base_url,
        model=cfg.model_name,
        tool_parser_name=variant.parser_name,
        api_key=cfg.api_key,
        preserve_thinking=variant.preserve_thinking,
        enable_thinking=_knob("enable_thinking"),
        max_pixels=_knob("max_pixels") if server_pixels else None,
        min_pixels=_knob("min_pixels") if server_pixels else None,
        max_concurrent=rc.get("llm_max_concurrent", 0),
        provider_telemetry_path=rc.get("provider_telemetry_path"),
    )

    sampling_params: dict[str, Any] = {
        "max_new_tokens": rc.get("max_new_tokens", rc.get("max_response_len", 2048)),
    }
    if not extra.get("omit_sampling_controls"):
        sampling_params["temperature"] = rc.get("temperature", 0.0)
        sampling_params["top_p"] = rc.get("top_p", 1.0)
    else:


        for key in ("temperature", "top_p"):
            if rc.get(key) is not None:
                sampling_params[key] = rc[key]

    if variant.stop_sequences:
        sampling_params["stop"] = list(variant.stop_sequences)
        sampling_params["no_stop_trim"] = True

    kwargs: dict[str, Any] = dict(
        chat_model=chat_model,
        sampling_params=sampling_params,
        tools=extra.get("tools"),
        original_image_width=rc.get("original_image_width"),
        original_image_height=rc.get("original_image_height"),
        resize_width=rc.get("resize_width"),
        resize_height=rc.get("resize_height"),
        system_prompt=system_prompt,
        image_retention=rc.get("image_retention", variant.default_image_retention),
    )
    messages_log_dir = rc.get("messages_log_dir")
    if messages_log_dir is not None and _ctor_accepts(variant.policy_cls, "messages_log_dir"):
        kwargs["messages_log_dir"] = messages_log_dir


    if not server_pixels:
        for key in ("resize_longest_edge", "max_pixels", "min_pixels", "image_factor"):
            val = rc.get(key)
            if val is not None and _ctor_accepts(variant.policy_cls, key):
                kwargs[key] = val
    return variant.policy_cls(**kwargs)


load_builtin_agents()
