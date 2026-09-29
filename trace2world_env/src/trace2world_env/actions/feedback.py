"""Agent-visible feedback for actions the host rejects or answers without touching the page.

Every payload carries ``env_text`` (the exact text shown to the agent), a structured
``actionability_feedback`` block, and a ``last_action_result`` summary. The ``ERROR[...]``
strings are part of the policy's training distribution and must not change.
"""

from __future__ import annotations

from typing import Any


REJECT_STATUSES = frozenset({"noninteractive", "disabled", "passive_static"})
NONINTERACTIVE_MESSAGE = "The selected component is not interactive."
ASK_USER_UNDECLARED_MESSAGE = (
    "The current page does not declare an ask_user action host. "
    "No user request was sent and no page state was changed."
)
BACK_BOUNDARY_MESSAGE = (
    "System back would leave the current app. The current page was kept. "
    "Use a visible in-app back control instead."
)
REJECTION_MESSAGES = {
    "NOT_INTERACTIVE": "ERROR[NOT_INTERACTIVE]\nNo state change. Choose another target.",
    "NO_EDITABLE_FOCUS": (
        "ERROR[NO_EDITABLE_FOCUS]\n"
        "No state change. Click an editable field before typing."
    ),
    "INVALID_ACTION": "ERROR[INVALID_ACTION]\nNo state change. Choose a supported action.",
    "FORMAT_INVALID": (
        "ERROR[FORMAT_INVALID]\n"
        "Your action could not be parsed. Emit exactly one valid tool call."
    ),
}


def normalise_app_alias(value: Any) -> str:
    """Case- and whitespace-insensitive form of an app name."""
    return " ".join(str(value or "").strip().casefold().split())


def resolve_app_aliases(declared: list[str] | None, environment_id: str) -> list[str]:
    """Names ``open_app`` accepts for the current app: the task's ``available_apps``, else the
    environment id."""
    aliases = [
        value.strip() for value in declared or [] if isinstance(value, str) and value.strip()
    ]
    slug = environment_id.strip()
    if not aliases and slug:
        aliases.append(slug)
    return aliases


def open_app_feedback(app_name: Any, aliases: list[str]) -> dict[str, Any]:
    """Answer ``open_app``: satisfied when the name is an alias of the current app, rejected
    otherwise (the host never leaves the app)."""
    requested = str(app_name or "").strip()
    alias_set = {normalise_app_alias(value) for value in aliases}
    if not requested:
        message = "No app name was supplied; the current app was left unchanged."
        reason = "missing_app_name"
        status = "invalid"
        error_class = "parse_or_schema_error"
    elif normalise_app_alias(requested) in alias_set:
        current = aliases[0] if aliases else "the current app"
        message = f"Already in {current}; open_app was satisfied without navigation."
        reason = "already_in_requested_app"
        status = "satisfied"
        error_class = "none"
    else:
        shown = ", ".join(aliases) if aliases else "the current app"
        message = f"App '{requested}' is not available in this environment. Available app: {shown}."
        reason = "app_unavailable"
        status = "unsupported"
        error_class = "action_unavailable"
    return {
        "actionability_rejected": status != "satisfied",
        "env_text": message,
        "actionability_feedback": {
            "schema": "trace2world.open_app_result.v1",
            "message": message,
            "result": {
                "schema": "trace2world.open_app_result.v1",
                "status": status,
                "response_mode": "env_text",
                "reason": reason,
                "requested_app": requested,
                "available_apps": aliases,
                "error_class": error_class,
                "business_failure": False,
            },
        },
        "last_action_result": {
            "kind": "open_app",
            "status": status,
            "reason": reason,
            "requested_app": requested,
            "available_apps": aliases,
            "error_class": error_class,
            "business_failure": False,
        },
    }


def unknown_action_payload(action: dict[str, Any]) -> dict[str, Any]:
    """Reject an unsupported or unparsable action (``INVALID_ACTION`` / ``FORMAT_INVALID``)."""
    action_type = str(action.get("action_type") or "").strip() or "<missing>"
    error_detail = str(action.get("error") or "").strip().casefold()
    unsupported_action = (
        error_detail.startswith("unsupported action_type:")
        or error_detail.startswith("unsupported action")
    )
    parser_invalid = (
        not unsupported_action
        and (
            action_type in {"<missing>", "unknown"}
            or action.get("format_valid") is False
            or str(action.get("error_class") or "") == "parse_or_schema_error"
        )
    )
    error_code = "FORMAT_INVALID" if parser_invalid else "INVALID_ACTION"
    message = REJECTION_MESSAGES[error_code]
    reason = "parser_format_invalid" if parser_invalid else "unsupported_action"
    return {
        "actionability_rejected": True,
        "environment_suspect": False,
        "environment_suspect_reason": "model_format_or_unsupported_action",
        "env_text": message,
        "actionability_feedback": {
            "schema": "trace2world.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "trace2world.action_rejected.v1",
                "status": "invalid",
                "response_mode": "env_text",
                "reason": reason,
                "action_type": action_type,
                "error_code": error_code,
                "state_changed": False,
                "error_class": (
                    "parse_or_schema_error" if parser_invalid else "unsupported_action"
                ),
                "format_valid": not parser_invalid,
                "business_failure": False,
            },
        },
        "last_action_result": {
            "kind": "parser_format" if parser_invalid else "unsupported_action",
            "status": "invalid",
            "action_type": action_type,
            "reason": reason,
            "error_code": error_code,
            "state_changed": False,
            "error_class": "parse_or_schema_error" if parser_invalid else "unsupported_action",
            "format_valid": not parser_invalid,
            "business_failure": False,
        },
    }


def back_boundary_payload() -> dict[str, Any]:
    """Reject a back navigation that would leave the app."""
    message = BACK_BOUNDARY_MESSAGE
    return {
        "actionability_rejected": True,
        "env_text": message,
        "actionability_feedback": {
            "schema": "trace2world.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "trace2world.action_rejected.v1",
                "status": "blocked",
                "response_mode": "env_text",
                "reason": "no_in_app_back_history",
                "error_class": "navigation_boundary",
                "business_failure": False,
                "message": message,
            },
        },
        "last_action_result": {
            "kind": "navigation_boundary",
            "rejected": True,
            "status": "blocked",
            "reason": "no_in_app_back_history",
            "error_class": "navigation_boundary",
            "business_failure": False,
            "message": message,
        },
    }


def focused_editable_rejection_payload(
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reject typing when no editable element has focus (``NO_EDITABLE_FOCUS``)."""
    probe_unavailable = result is None
    reason = "focused_editable_probe_unavailable" if probe_unavailable else "no_focused_editable"
    message = REJECTION_MESSAGES["NO_EDITABLE_FOCUS"]
    observed_element_tag = str((result or {}).get("observed_element_tag") or "")[:32]
    observed_input_type = str((result or {}).get("observed_input_type") or "")[:32]
    feedback_result = {
        "schema": "trace2world.action_rejected.v1",
        "status": "noninteractive",
        "response_mode": "env_text",
        "reason": reason,
        "error_class": "action_context_missing",
        "error_code": "NO_EDITABLE_FOCUS",
        "state_changed": False,
        "business_failure": False,
        "observed_element_tag": observed_element_tag,
        "observed_input_type": observed_input_type,
    }
    return {
        "actionability_rejected": True,
        "environment_suspect": False,
        "environment_suspect_reason": "no_editable_focus",
        "env_text": message,
        "actionability_feedback": {
            "schema": "trace2world.action_rejected.v1",
            "message": message,
            "result": feedback_result,
        },
        "last_action_result": {
            "kind": "input_text",
            "rejected": True,
            "status": "noninteractive",
            "reason": reason,
            "error_class": "action_context_missing",
            "error_code": "NO_EDITABLE_FOCUS",
            "state_changed": False,
            "business_failure": False,
            "observed_element_tag": observed_element_tag,
            "observed_input_type": observed_input_type,
        },
    }


def rejected_actionability_payload(
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reject a tap on a component the page declares non-interactive (``NOT_INTERACTIVE``).

    Returns ``None`` when the inspected status allows the action.
    """
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "")
    if status not in REJECT_STATUSES:
        return None
    message = REJECTION_MESSAGES["NOT_INTERACTIVE"]
    sanitized_result = {
        key: result[key] for key in ("schema", "status", "response_mode", "reason") if key in result
    }
    sanitized_result["message"] = message
    sanitized_result["error_code"] = "NOT_INTERACTIVE"
    sanitized_result["state_changed"] = False
    diagnostics = result.get("action_host_diagnostics")
    diagnostic_click_kind = ""
    diagnostic_click_id = ""
    if isinstance(diagnostics, dict):
        diagnostic_click_kind = str(diagnostics.get("click_host_action_kind") or "")
        diagnostic_click_id = str(diagnostics.get("click_host_action_id") or "")
        diagnostic_selected_tag = str(diagnostics.get("selected_tag") or "").casefold()
    else:
        diagnostic_selected_tag = ""
    gesture_mismatch = str(result.get("reason") or "") in {
        "gesture_host_requires_gesture",
        "gesture_action_required",
    } or (
        "gesture" in diagnostic_click_kind.casefold()
        or "swipe" in diagnostic_click_kind.casefold()
    )
    environment_suspect = (
        bool(diagnostic_click_id)
        or diagnostic_selected_tag in {
            "a", "button", "input", "textarea", "select", "option", "summary",
        }
    ) and not gesture_mismatch
    payload = {
        "actionability_rejected": True,
        "environment_suspect": environment_suspect,
        "environment_suspect_reason": (
            "declared_visual_semantic_host_rejected"
            if environment_suspect
            else "noninteractive_or_gesture_mismatch"
        ),
        "env_text": message,
        "actionability_feedback": {
            "schema": "sim.actionability.env_step.v1",
            "message": message,
            "result": sanitized_result,
        },
        "last_action_result": {
            "kind": "actionability_preflight",
            "rejected": True,
            "status": status,
            "message": message,
            "reason": result.get("reason"),
            "error_code": "NOT_INTERACTIVE",
            "state_changed": False,
        },
    }
    if isinstance(diagnostics, dict):
        payload["actionability_diagnostics"] = diagnostics
    return payload


def ask_user_rejected_payload(message: str, reason: str) -> dict[str, Any]:
    """Reject an ``ask_user`` action the page could not deliver."""
    return {
        "actionability_rejected": True,
        "env_text": message,
        "actionability_feedback": {
            "schema": "trace2world.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "trace2world.action_rejected.v1",
                "status": "rejected",
                "response_mode": "env_text",
                "reason": reason,
                "message": message,
            },
        },
        "last_action_result": {
            "kind": "external_action_host",
            "rejected": True,
            "status": "rejected",
            "message": message,
            "reason": reason,
        },
    }


def feedback_text(result: dict[str, Any]) -> str | None:
    """The text the agent receives after an action, or ``None`` for a silent success.

    Order: ``env_text``, then ``actionability_feedback.message``, then
    ``actionability_feedback.result.message``.
    """
    feedback = result.get("env_text")
    structured = result.get("actionability_feedback")
    if not feedback and isinstance(structured, dict):
        feedback = structured.get("message")
        if not feedback and isinstance(structured.get("result"), dict):
            feedback = structured["result"].get("message")
    return str(feedback) if feedback else None
