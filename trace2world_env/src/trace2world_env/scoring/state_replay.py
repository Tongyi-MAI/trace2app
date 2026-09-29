"""Replay of the state service's typed state diffs.

The strict evidence reducer only trusts a snapshot when the episode's event stream, replayed
diff by diff from the sealed initial snapshot, reproduces the final snapshot state exactly.
Typed paths look like ``$.collections.orders[id=42].status``.
"""

from __future__ import annotations

import json
from typing import Any

from trace2world_env.scoring.expressions import (
    MISSING,
    canonical_clone,
    canonical_equal,
    canonical_hash,
    canonical_json,
    finite_number,
)


def typed_path_value(state: Any, path: str) -> Any:
    """Resolve a typed diff path (``$.a.b[id=7].c``) in ``state``; :data:`MISSING` if absent."""
    if path == "$":
        return state
    if not isinstance(path, str) or not path.startswith("$."):
        return MISSING
    remaining = path[2:]
    current = state
    while remaining:
        if isinstance(current, dict):
            key = next(
                (
                    candidate
                    for candidate in sorted(current, key=len, reverse=True)
                    if remaining == candidate
                    or remaining.startswith(f"{candidate}.")
                    or remaining.startswith(f"{candidate}[")
                ),
                None,
            )
            if key is None:
                return MISSING
            current = current[key]
            remaining = remaining[len(key) :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        if isinstance(current, list) and remaining.startswith("[id="):
            end = remaining.find("]")
            if end < 0:
                return MISSING
            entity_id = remaining[4:end]
            current = next(
                (
                    item
                    for item in current
                    if isinstance(item, dict) and str(item.get("id")) == entity_id
                ),
                MISSING,
            )
            if current is MISSING:
                return MISSING
            remaining = remaining[end + 1 :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        return MISSING
    return current


def _typed_path_target(state: Any, path: str) -> tuple[Any, Any] | None:
    """Locate the parent container and key a typed diff path writes to, or ``None``."""
    if not isinstance(path, str) or not path.startswith("$."):
        return None
    remaining = path[2:]
    current = state
    while remaining:
        if isinstance(current, dict):
            key = next(
                (
                    candidate
                    for candidate in sorted(current, key=len, reverse=True)
                    if remaining == candidate
                    or remaining.startswith(f"{candidate}.")
                    or remaining.startswith(f"{candidate}[")
                ),
                None,
            )
            if key is None:
                if "[id=" in remaining:
                    return None
                return current, remaining
            if remaining == key:
                return current, key
            current = current[key]
            remaining = remaining[len(key) :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        if isinstance(current, list) and remaining.startswith("[id="):
            end = remaining.find("]")
            if end < 0:
                return None
            entity_id = remaining[4:end]
            index = next(
                (
                    index
                    for index, item in enumerate(current)
                    if isinstance(item, dict) and str(item.get("id")) == entity_id
                ),
                None,
            )
            tail = remaining[end + 1 :]
            if index is None:
                return (current, ("entity", entity_id)) if not tail else None
            if not tail:
                return current, index
            current = current[index]
            remaining = tail[1:] if tail.startswith(".") else tail
            continue
        return None
    return None


def _apply_typed_diff(state: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Apply one diff row to ``state`` in place; returns a mismatch reason or ``None``."""
    operation = row["operation"]
    path = row["path"]
    detached_after = canonical_clone(row["after"])
    actual = typed_path_value(state, path)
    expected_missing = operation in {"field_added", "entity_added"}
    if expected_missing:
        if actual is not MISSING:
            return "diff_before_expected_missing"
    elif actual is MISSING or not canonical_equal(actual, row["before"]):
        return "diff_before_mismatch"

    target = _typed_path_target(state, path)
    if target is None:
        return "diff_path_not_applicable"
    parent, key = target
    if operation == "field_added":
        if not isinstance(parent, dict) or not isinstance(key, str):
            return "diff_operation_target_mismatch"
        parent[key] = detached_after
    elif operation == "field_removed":
        if not isinstance(parent, dict) or not isinstance(key, str) or key not in parent:
            return "diff_operation_target_mismatch"
        del parent[key]
    elif operation == "entity_added":
        if (
            not isinstance(parent, list)
            or not isinstance(key, tuple)
            or len(key) != 2
            or key[0] != "entity"
            or not isinstance(row["after"], dict)
            or str(row["after"].get("id")) != key[1]
        ):
            return "diff_operation_target_mismatch"
        parent.append(detached_after)
    elif operation == "entity_removed":
        if not isinstance(parent, list) or not isinstance(key, int):
            return "diff_operation_target_mismatch"
        del parent[key]
    else:
        if (
            isinstance(parent, dict)
            and isinstance(key, str)
            and key in parent
            or isinstance(parent, list)
            and isinstance(key, int)
            and key < len(parent)
        ):
            parent[key] = detached_after
        else:
            return "diff_operation_target_mismatch"
    updated = typed_path_value(state, path)
    if operation in {"field_removed", "entity_removed"}:
        return None if updated is MISSING else "diff_after_mismatch"
    return None if canonical_equal(updated, row["after"]) else "diff_after_mismatch"


def is_valid_diff_row(row: Any) -> bool:
    """Structural check of one typed diff row (operation, path, before/after, delta)."""
    if not isinstance(row, dict):
        return False
    try:
        canonical_json(row)
    except (TypeError, ValueError):
        return False
    if not {"operation", "path", "before", "after"}.issubset(row):
        return False
    operation = row.get("operation")
    path = row.get("path")
    before = row.get("before")
    after = row.get("after")
    if not isinstance(path, str) or not path.startswith("$") or canonical_equal(before, after):
        return False
    if operation == "field_added":
        return before is None
    if operation == "field_removed":
        return after is None
    if operation == "field_replaced":
        return True
    if operation == "ordered_sequence_changed":
        return isinstance(before, list) and isinstance(after, list)
    if operation == "entity_added":
        return "[id=" in path and before is None and isinstance(after, dict)
    if operation == "entity_removed":
        return "[id=" in path and isinstance(before, dict) and after is None
    if operation == "numeric_delta":
        before_number = finite_number(before)
        after_number = finite_number(after)
        delta = finite_number(row.get("delta"))
        return (
            before_number is not None
            and after_number is not None
            and delta is not None
            and delta == after_number - before_number
        )
    return False


def validate_carrier(
    *,
    task_id: str,
    sid: str,
    binding: Any,
    snapshot: Any,
    events: Any,
    expected_snapshot_kind: str,
    validate_runtime_events: bool,
) -> tuple[dict[str, Any], list[str], dict[int, dict[str, Any]]]:
    """Validate the episode binding, the snapshot identity/hash, and (when the contract needs
    runtime events) replay the event stream from the sealed initial state.

    Returns ``(ledger, reasons, post_state_by_event_index)``; any reason invalidates the
    evaluation.
    """
    ledger: dict[str, Any] = {
        "contract_valid": True,
        "identity_valid": False,
        "snapshot_valid": False,
        "events_valid": False,
        "reasons": [],
        "event_rows": [],
        "server_episode_binding": None,
    }
    reasons: list[str] = ledger["reasons"]
    post_state_by_event_index: dict[int, dict[str, Any]] = {}
    if (
        not isinstance(binding, dict)
        or binding.get("task_id") != task_id
        or binding.get("sid") != sid
    ):
        reasons.append("server_episode_binding_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["server_episode_binding"] = {
        "task_id": binding.get("task_id"),
        "sid": binding.get("sid"),
        "episode_epoch": binding.get("episode_epoch"),
        "initial_snapshot_id": binding.get("initial_snapshot_id"),
        "initial_revision": binding.get("initial_revision"),
        "initial_state_hash": binding.get("initial_state_hash"),
    }
    if not isinstance(snapshot, dict):
        reasons.append("canonical_snapshot_missing")
        return ledger, reasons, post_state_by_event_index
    state = snapshot.get("state")
    version = snapshot.get("state_version")
    handle = snapshot.get("evaluation_handle")
    if (
        not isinstance(handle, dict)
        and expected_snapshot_kind == "preview"
        and isinstance(version, dict)
    ):
        handle = {
            "sid": version.get("sid"),
            "episode_epoch": version.get("episode_epoch"),
            "revision": version.get("revision"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "state_hash": snapshot.get("state_hash"),
        }
        ledger["evaluation_handle_source"] = "server_bound_preview_snapshot"
    else:
        ledger["evaluation_handle_source"] = "sidecar_finalization_handle"
    if not isinstance(state, dict) or not isinstance(version, dict) or not isinstance(handle, dict):
        reasons.append("canonical_snapshot_schema_invalid")
        return ledger, reasons, post_state_by_event_index
    revision = version.get("revision")
    identity_values = {
        "sid": sid,
        "episode_epoch": binding.get("episode_epoch"),
        "revision": revision,
    }
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or any(version.get(key) != value for key, value in identity_values.items())
        or any(handle.get(key) != value for key, value in identity_values.items())
        or not isinstance(snapshot.get("snapshot_id"), str)
        or not snapshot["snapshot_id"]
        or handle.get("snapshot_id") != snapshot.get("snapshot_id")
        or snapshot.get("snapshot_kind") != expected_snapshot_kind
    ):
        reasons.append("snapshot_identity_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["identity_valid"] = True
    ledger["effective_evaluation_handle"] = dict(handle)
    try:
        state_hash = canonical_hash(state)
    except (TypeError, ValueError):
        reasons.append("canonical_snapshot_state_not_finite_json")
        return ledger, reasons, post_state_by_event_index
    ledger["snapshot_identity"] = {
        "sid": version.get("sid"),
        "episode_epoch": version.get("episode_epoch"),
        "revision": revision,
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_kind": snapshot.get("snapshot_kind"),
    }
    ledger["snapshot_hash_validation"] = {
        "declared": snapshot.get("state_hash"),
        "computed": state_hash,
        "handle": handle.get("state_hash"),
    }
    if snapshot.get("state_hash") != state_hash or handle.get("state_hash") != state_hash:
        reasons.append("snapshot_state_hash_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["snapshot_valid"] = True
    if not validate_runtime_events:
        ledger["events_valid"] = None
        ledger["event_validation_mode"] = "not_declared"
        return ledger, reasons, post_state_by_event_index
    ledger["event_validation_mode"] = "strict_canonical_sidecar_v1"
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        reasons.append("event_stream_schema_invalid")
        return ledger, reasons, post_state_by_event_index

    initial_state = binding.get("initial_state")
    initial_state_hash = binding.get("initial_state_hash")
    try:
        computed_initial_state_hash = (
            canonical_hash(initial_state) if isinstance(initial_state, dict) else None
        )
    except (TypeError, ValueError):
        computed_initial_state_hash = None
    if (
        not isinstance(initial_state, dict)
        or not isinstance(binding.get("initial_snapshot_id"), str)
        or not binding["initial_snapshot_id"]
        or binding.get("initial_revision") != 0
        or not isinstance(initial_state_hash, str)
        or computed_initial_state_hash != initial_state_hash
    ):
        reasons.append("initial_snapshot_binding_missing_or_invalid")
        return ledger, reasons, post_state_by_event_index
    replay_state = json.loads(canonical_json(initial_state))
    ledger["event_replay"] = {
        "initial_snapshot_id": binding.get("initial_snapshot_id"),
        "initial_state_hash": initial_state_hash,
        "diff_rows": [],
        "touched_paths": [],
    }

    previous_revision = 0
    touched_paths: set[str] = set()
    for index, event in enumerate(events):
        event_revision = event.get("revision")
        row_ledger = {"index": index, "revision": event_revision, "valid": False, "reasons": []}
        ledger["event_rows"].append(row_ledger)
        if (
            isinstance(event_revision, bool)
            or not isinstance(event_revision, int)
            or event_revision < 1
        ):
            row_ledger["reasons"].append("invalid_event_revision")
            reasons.append("invalid_event_revision")
            continue
        if event_revision <= previous_revision:
            row_ledger["reasons"].append("non_monotonic_event_revision")
            reasons.append("non_monotonic_event_revision")
        elif event_revision != previous_revision + 1:
            row_ledger["reasons"].append("event_revision_gap")
            reasons.append("event_revision_gap")
        if event_revision > revision:
            row_ledger["reasons"].append("event_revision_after_snapshot")
            reasons.append("event_revision_after_snapshot")
        previous_revision = event_revision
        transaction_id = event.get("transaction_id")
        if isinstance(transaction_id, str) and transaction_id:
            diffs = event.get("state_diff")
            if not isinstance(diffs, list) or not diffs:
                row_ledger["reasons"].append("empty_transaction_state_diff")
                reasons.append("empty_transaction_state_diff")
            else:
                for diff_index, diff in enumerate(diffs):
                    diff_ledger = {
                        "event_index": index,
                        "diff_index": diff_index,
                        "operation": diff.get("operation") if isinstance(diff, dict) else None,
                        "path": diff.get("path") if isinstance(diff, dict) else None,
                        "valid": False,
                        "reason": None,
                    }
                    ledger["event_replay"]["diff_rows"].append(diff_ledger)
                    if not is_valid_diff_row(diff):
                        row_ledger["reasons"].append("invalid_typed_state_diff")
                        reasons.append("invalid_typed_state_diff")
                        diff_ledger["reason"] = "invalid_typed_state_diff"
                        continue
                    path = diff["path"]
                    touched_paths.add(path)
                    replay_error = _apply_typed_diff(replay_state, diff)
                    if replay_error is not None:
                        row_ledger["reasons"].append("state_diff_replay_mismatch")
                        reasons.append("state_diff_replay_mismatch")
                        diff_ledger["reason"] = replay_error
                    else:
                        diff_ledger["valid"] = True
        elif (
            not isinstance(event.get("event_type"), str)
            or not event.get("event_type")
            or not isinstance(event.get("action_id"), str)
            or not event.get("action_id")
            or not isinstance(event.get("payload", {}), dict)
        ):
            row_ledger["reasons"].append("unknown_event_shape")
            reasons.append("unknown_event_shape")
        row_ledger["valid"] = not row_ledger["reasons"]
        post_state_by_event_index[index] = json.loads(canonical_json(replay_state))
        row_ledger["post_state_hash"] = canonical_hash(replay_state)
    if events and previous_revision != revision:
        reasons.append("event_revision_gap")
    if not events and revision != 0:
        reasons.append("event_revision_gap")
    ledger["event_replay"]["touched_paths"] = sorted(touched_paths)
    ledger["event_replay"]["replayed_touched_state_hash"] = canonical_hash(
        {
            path: (
                {"missing": True}
                if typed_path_value(replay_state, path) is MISSING
                else typed_path_value(replay_state, path)
            )
            for path in sorted(touched_paths)
        }
    )
    ledger["event_replay"]["replayed_final_state_hash"] = canonical_hash(replay_state)
    ledger["event_replay"]["final_snapshot_state_hash"] = canonical_hash(state)
    if not canonical_equal(replay_state, state):
        reasons.append("event_replay_final_state_mismatch")
    ledger["events_valid"] = not reasons
    return ledger, reasons, post_state_by_event_index
