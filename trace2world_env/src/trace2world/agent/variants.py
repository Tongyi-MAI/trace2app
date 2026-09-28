
from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from typing import Any

from trace2world.schema.config import AgentConfig

from ._base import AgentPolicy, AgentVariant

logger = logging.getLogger(__name__)


PolicyFactory = Callable[[AgentConfig], Any]


__all__ = [
    "AgentPolicy",
    "AgentVariant",
    "PolicyFactory",
    "get_agent",
    "get_entry",
    "list_agents",
    "load_builtin_agents",
    "register_policy_factory",
    "register_variant",
]


_REGISTRY: dict[str, AgentVariant | PolicyFactory] = {}


def register_variant(variant: AgentVariant) -> None:
    if variant.name in _REGISTRY:
        raise ValueError(f"Agent {variant.name!r} already registered")
    _REGISTRY[variant.name] = variant
    logger.debug("Registered agent variant: %s", variant.name)


def register_policy_factory(agent_type: str, factory: PolicyFactory) -> None:
    if agent_type in _REGISTRY:
        raise ValueError(f"Agent {agent_type!r} already registered")
    _REGISTRY[agent_type] = factory
    logger.debug("Registered policy factory: %s", agent_type)


def get_entry(name: str) -> AgentVariant | PolicyFactory | None:
    load_builtin_agents()
    return _REGISTRY.get(name)


def get_agent(name: str) -> AgentVariant:
    entry = get_entry(name)
    if isinstance(entry, AgentVariant):
        return entry
    available = ", ".join(list_agents())
    raise ValueError(f"Unknown agent variant: {name!r}. Available: [{available}]")


def list_agents() -> list[str]:
    load_builtin_agents()
    return sorted(n for n, e in _REGISTRY.items() if isinstance(e, AgentVariant))



_loaded = False


def load_builtin_agents() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from trace2world.agent import implementations

    for mod in pkgutil.iter_modules(implementations.__path__):
        try:
            importlib.import_module(f"{implementations.__name__}.{mod.name}")
        except Exception as exc:
            logger.warning("agent module %s failed to import: %s", mod.name, exc)
