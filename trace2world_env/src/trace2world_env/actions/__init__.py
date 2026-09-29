"""The agent action space: vocabulary, execution in a page, and agent-visible feedback."""

from trace2world_env.actions.executor import ActionContext, execute_action
from trace2world_env.actions.feedback import feedback_text, resolve_app_aliases
from trace2world_env.actions.vocabulary import TERMINAL_ACTIONS, is_terminal

__all__ = [
    "TERMINAL_ACTIONS",
    "ActionContext",
    "execute_action",
    "feedback_text",
    "is_terminal",
    "resolve_app_aliases",
]
