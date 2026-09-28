
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

from trace2world.env.evidence_contract import _canonical_hash, reduce_state_kernel_evidence

logger = logging.getLogger(__name__)
_MISSING = object()


class StateKernelError(RuntimeError):
    ...


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _canonical_value(state: dict[str, Any], path: str) -> Any:
    runtime_fields = state.get("runtime_fields", {})
    if isinstance(runtime_fields, dict) and path in runtime_fields:
        return runtime_fields[path]


    if isinstance(runtime_fields, dict):
        matching_key = next(
            (
                key
                for key in sorted(runtime_fields, key=len, reverse=True)
                if path.startswith(f"{key}.")
            ),
            None,
        )
        if matching_key is not None:
            current: Any = runtime_fields[matching_key]
            suffix = path[len(matching_key) + 1 :]
            for part in suffix.split("."):
                if not isinstance(current, dict) or part not in current:
                    return _MISSING
                current = current[part]
            return current
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _assertion_matches(actual: Any, assertion: dict[str, Any]) -> bool:
    operation = str(assertion.get("op") or "eq")
    expected = assertion.get("value")
    if operation == "present":
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    if operation == "eq":
        return actual == expected
    if operation in {"contains", "selected_contains"}:
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        return str(expected) in str(actual)
    if operation == "truthy":
        return bool(actual)
    return False


def _is_ordered_subsequence(expected: list[str], observed: list[str]) -> bool:
    if not expected:
        return True
    cursor = 0
    for transaction_id in observed:
        if transaction_id == expected[cursor]:
            cursor += 1
            if cursor == len(expected):
                return True
    return False


def _nested_value(value: Any, path: str) -> Any:

    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _state_diff_assertions_match(
    event: dict[str, Any], declarations: Any,
) -> bool:

    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint state_diff_assertions must be a non-empty list")
    diffs = event.get("state_diff")
    if not isinstance(diffs, list):
        return False
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint state-diff assertion must be an object")
        path = declaration.get("path")
        if not isinstance(path, str) or not path:
            raise StateKernelError("checkpoint state-diff assertion requires path")
        candidates = [
            row for row in diffs
            if isinstance(row, dict) and str(row.get("path") or "") == path
        ]
        matched = False
        for row in candidates:
            if all(
                isinstance(assertion, dict)
                and _assertion_matches(row.get(field, _MISSING), assertion)
                for field, assertion in declaration.items()
                if field != "path"
            ):
                matched = True
                break
        if not matched:
            return False
    return True


def _checkpoint_event_matches(event: dict[str, Any], declaration: Any) -> bool:

    if not isinstance(declaration, dict):
        raise StateKernelError("checkpoint event assertion must be an object")
    match = declaration.get("match", {})
    if not isinstance(match, dict):
        raise StateKernelError("checkpoint event assertion match must be an object")
    for path, assertion in match.items():
        if not isinstance(assertion, dict):
            raise StateKernelError("checkpoint event field assertion must be an object")
        if not _assertion_matches(_nested_value(event, str(path)), assertion):
            return False
    return _state_diff_assertions_match(
        event, declaration.get("state_diff_assertions")
    )


def _checkpoint_event_assertions_match(
    events: list[dict[str, Any]], declarations: Any,
) -> bool:
    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint event_assertions must be a non-empty list")
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint event assertion must be an object")
        minimum = declaration.get("min_count", 1)
        if not isinstance(minimum, int) or minimum < 1:
            raise StateKernelError("checkpoint event assertion min_count must be positive")
        if sum(
            _checkpoint_event_matches(event, declaration) for event in events
        ) < minimum:
            return False
    return True


def _checkpoint_event_sequence_matches(
    events: list[dict[str, Any]], declarations: Any,
) -> bool:
    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint event_sequence must be a non-empty list")
    cursor = 0
    for event in events:
        if _checkpoint_event_matches(event, declarations[cursor]):
            cursor += 1
            if cursor == len(declarations):
                return True
    return False


def _reduce_declared_checkpoints(
    evaluator: dict[str, Any],
    state: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:

    declarations = evaluator.get("checkpoint_assertions")
    if declarations is None:
        return None
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint_assertions must be a non-empty list")

    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint declaration must be an object")
        checkpoint_id = declaration.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise StateKernelError("checkpoint declaration requires checkpoint_id")
        if checkpoint_id in rows:
            raise StateKernelError(f"duplicate checkpoint declaration: {checkpoint_id}")
        dependencies = declaration.get("depends_on", [])
        weight = declaration.get("weight")
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(value, str) or not value for value in dependencies)
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not 0 <= float(weight) <= 1
        ):
            raise StateKernelError(f"invalid checkpoint dependency/weight: {checkpoint_id}")
        assertions = declaration.get("state_assertions")
        has_state = isinstance(assertions, dict) and bool(assertions)
        has_events = any(
            declaration.get(field) is not None
            for field in ("event_assertions", "event_sequence")
        )
        if not has_state and not has_events:
            raise StateKernelError(f"checkpoint has no executable predicate: {checkpoint_id}")
        rows[checkpoint_id] = declaration
        order.append(checkpoint_id)

    unknown = sorted({
        dependency
        for declaration in rows.values()
        for dependency in declaration.get("depends_on", [])
        if dependency not in rows
    })
    if unknown:
        raise StateKernelError(f"checkpoint dependency is not declared: {unknown}")

    terminal_required = evaluator.get("terminal_required_checkpoint_ids", [])
    if not isinstance(terminal_required, list) or any(
        not isinstance(checkpoint_id, str) or not checkpoint_id
        for checkpoint_id in terminal_required
    ):
        raise StateKernelError(
            "terminal_required_checkpoint_ids must be a list of checkpoint ids"
        )
    if len(set(terminal_required)) != len(terminal_required):
        raise StateKernelError("terminal_required_checkpoint_ids contains duplicates")
    unknown_terminal = sorted(set(terminal_required) - set(rows))
    if unknown_terminal:
        raise StateKernelError(
            f"terminal required checkpoint is not declared: {unknown_terminal}"
        )

    outcomes: dict[str, bool] = {}
    remaining = set(order)
    while remaining:
        progressed = False
        for checkpoint_id in order:
            if checkpoint_id not in remaining:
                continue
            declaration = rows[checkpoint_id]
            dependencies = declaration.get("depends_on", [])
            if any(dependency not in outcomes for dependency in dependencies):
                continue
            assertions = declaration.get("state_assertions") or {}
            state_matches = all(
                isinstance(assertion, dict)
                and _assertion_matches(_canonical_value(state, str(path)), assertion)
                for path, assertion in assertions.items()
            )
            own_predicate = bool(
                state_matches
                and _checkpoint_event_assertions_match(
                    events, declaration.get("event_assertions")
                )
                and _checkpoint_event_sequence_matches(
                    events, declaration.get("event_sequence")
                )
            )
            outcomes[checkpoint_id] = bool(
                own_predicate and all(outcomes[dependency] for dependency in dependencies)
            )
            remaining.remove(checkpoint_id)
            progressed = True
        if not progressed:
            raise StateKernelError("checkpoint dependency graph contains a cycle")

    vector = []
    progress_reward = 0.0
    for checkpoint_id in order:
        declaration = rows[checkpoint_id]
        reached = outcomes[checkpoint_id]
        weight = float(declaration["weight"])
        contribution = weight if reached else 0.0
        progress_only = declaration.get("progress_only") is True
        if progress_only:
            progress_reward += contribution
        vector.append({
            "checkpoint_id": checkpoint_id,
            "reached": reached,
            "depends_on": list(declaration.get("depends_on", [])),
            "dependencies_satisfied": all(
                outcomes[dependency] for dependency in declaration.get("depends_on", [])
            ),
            "weight": weight,
            "progress_only": progress_only,
            "reward_contribution": contribution,
        })
    return {
        "checkpoint_outcomes": outcomes,
        "checkpoint_reward_vector": vector,
        "progress_reward": round(progress_reward, 12),
        "nonzero_checkpoint_reached": any(outcomes.values()),
        "terminal_required_checkpoint_ids": list(terminal_required),
        "missing_terminal_checkpoint_ids": [
            checkpoint_id
            for checkpoint_id in terminal_required
            if not outcomes[checkpoint_id]
        ],
        "terminal_checkpoints_satisfied": all(
            outcomes[checkpoint_id] for checkpoint_id in terminal_required
        ),
        "env_valid": True,
        "format_valid": True,
    }


def _canonical_input_expected_value(authority: dict[str, Any]) -> Any:
    writes = authority.get("canonical_writes")
    if not isinstance(writes, dict):
        return None
    candidates = [
        value
        for path, value in writes.items()
        if not str(path).startswith("state::")
        and isinstance(value, (str, int, float, bool))
    ]
    return candidates[0] if len(candidates) == 1 else None


def _canonical_input_value_matches(expected: Any, observed: Any) -> bool:
    if expected is None or observed is None:
        return False
    expected_text = str(expected).strip()
    observed_text = str(observed).strip()
    expected_compact = "".join(expected_text.split())
    observed_compact = "".join(observed_text.split())
    if expected_compact.isdigit() and observed_compact.isdigit():
        return expected_compact == observed_compact
    return expected_text == observed_text


class StateKernelManager:

    def __init__(
        self,
        resource_dir: str | Path,
        manifest: dict[str, Any],
        evaluator_plan: str | Path | None,
        *,
        writable_root: str | Path | None = None,
    ) -> None:
        self.resource_dir = Path(resource_dir).resolve()
        self.manifest = manifest
        self.evaluation_enabled = evaluator_plan is not None
        self.plan = (
            json.loads(Path(evaluator_plan).read_text(encoding="utf-8"))
            if evaluator_plan is not None
            else {}
        )
        configured = Path(writable_root).expanduser() if writable_root else None
        self.writable_root = (
            configured.resolve()
            if configured is not None
            else Path(tempfile.mkdtemp(prefix="t2w-state-kernel-")).resolve()
        )
        self.writable_root.mkdir(parents=True, exist_ok=True)
        self.port = _free_local_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._episode_bindings: dict[str, dict[str, Any]] = {}
        #: Hashes the state service itself attested during the startup health
        #: handshake.  Reused to rebuild a canonical ``state_version`` envelope
        #: for services whose snapshot payloads carry only a revision.
        self._attested_hashes: dict[str, str] = {}

    def _runtime_snapshot_projection(self) -> dict[str, Any] | None:

        state_service = self.manifest.get("state_service")
        if not isinstance(state_service, dict):
            return None
        projection = state_service.get("runtime_snapshot_projection")
        if projection is None:
            return None
        if not isinstance(projection, dict):
            raise StateKernelError("runtime_snapshot_projection must be an object")
        transaction_id = projection.get("transaction_id")
        allowed_paths = projection.get("allowed_state_paths")
        if (
            not isinstance(transaction_id, str)
            or not transaction_id
            or not isinstance(allowed_paths, list)
            or not allowed_paths
            or len({str(path) for path in allowed_paths}) != len(allowed_paths)
            or any(not isinstance(path, str) or not path.strip() for path in allowed_paths)
        ):
            raise StateKernelError("runtime_snapshot_projection declaration is invalid")
        return {
            "transaction_id": transaction_id,
            "allowed_state_paths": list(allowed_paths),
        }

    @property
    def runtime_snapshot_projection_enabled(self) -> bool:
        return self._runtime_snapshot_projection() is not None

    async def start(self) -> None:
        state_service = self.manifest.get("state_service", {})
        entrypoint = self.resource_dir / str(state_service.get("entrypoint") or "")
        state_db_path = self.writable_root / "state.sqlite"
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(entrypoint),
            "--result-root",
            str(self.resource_dir / str(self.manifest.get("app_root") or "app")),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--state-db-path",
            str(state_db_path),
            # The packaged state service lives inside the release tree.  Run it
            # without writing bytecode so hosting never mutates the artifact.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15.0)
        for _ in range(100):
            if self._process.returncode is not None:
                stderr = (await self._process.stderr.read()).decode("utf-8", errors="replace")
                raise StateKernelError(f"state-service exited during startup: {stderr}")
            try:
                response = await self._client.get("/api/v1/health")
                if response.status_code == 200:
                    health = response.json()
                    for key in ("schema_hash", "world_hash", "bundle_hash"):
                        expected = state_service.get(key)
                        if expected and health.get(key) != expected:
                            raise StateKernelError(f"state-service {key} mismatch")
                        if isinstance(health.get(key), str) and health.get(key):
                            self._attested_hashes[key] = health[key]
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        raise StateKernelError("state-service did not become healthy")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._client is None:
            raise StateKernelError("state-service is not running")
        forwarded_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() in {"content-type", "accept"}
        }
        return await self._client.request(
            method,
            path,
            content=content,
            headers=forwarded_headers,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.request(
            "POST",
            path,
            content=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        if response.status_code >= 400:
            raise StateKernelError(f"state-service {path} failed: {response.text}")
        return response.json()

    @staticmethod
    def _is_revision_only_snapshot(payload: Any) -> bool:
        """A snapshot from a service that exposes no ``state_version`` envelope.

        Packaged state services on the ``agentforge_state_kernel_sqlite_v1``
        protocol answer ``:snapshot``/``:finalize`` with
        ``{ok, sid, kind, state, revision, evaluation_handle}``.  The evidence
        contract validates against the canonical envelope, so such payloads are
        normalized before they reach it.
        """
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("state"), dict)
            and isinstance(payload.get("revision"), int)
            and not isinstance(payload.get("revision"), bool)
            and not isinstance(payload.get("state_version"), dict)
        )

    def _canonical_snapshot(
        self,
        payload: dict[str, Any],
        *,
        sid: str,
        kind: str,
        episode_epoch: int,
    ) -> dict[str, Any]:
        """Rebuild the canonical snapshot shape around a revision-only payload.

        ``state_version`` is composed from values the service already attested
        during the startup health handshake plus the revision it just reported.
        The state hash is computed host-side, so it is marked un-attested: the
        evidence ledger distinguishes a host-computed hash from one the service
        signed.
        """
        state = payload.get("state")
        revision = payload.get("revision")
        snapshot_kind = payload.get("kind") or kind
        snapshot_id = payload.get("evaluation_handle")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            snapshot_id = f"{sid}:{snapshot_kind}:{revision}"
        try:
            state_hash = _canonical_hash(state)
        except (TypeError, ValueError) as exc:
            raise StateKernelError(
                "state-service snapshot state is not canonical JSON"
            ) from exc
        state_version = {
            "sid": sid,
            "episode_epoch": episode_epoch,
            "revision": revision,
            **self._attested_hashes,
        }
        return {
            "state": state,
            "state_version": state_version,
            "state_hash": state_hash,
            "state_hash_attested": False,
            "snapshot_id": snapshot_id,
            "snapshot_kind": snapshot_kind,
            "evaluation_handle": {
                "sid": sid,
                "episode_epoch": episode_epoch,
                "revision": revision,
                "snapshot_id": snapshot_id,
                "state_hash": state_hash,
            },
        }

    async def initialize_episode(
        self,
        task_id: str,
        display_id: int,
        initial_state: dict[str, Any] | None = None,
    ) -> str:
        sid = f"sid_{display_id}_{uuid.uuid4().hex}"
        created = await self._post("/internal/v1/sessions", {"sid": sid})
        patch = initial_state or {}
        if patch:
            if not set(patch).issubset({"collections", "runtime_fields"}):
                raise StateKernelError(
                    f"task {task_id} initial_state must use collections/runtime_fields"
                )
            await self._post(f"/internal/v1/sessions/{sid}/:inject", patch)
        sealed = await self._post(f"/internal/v1/sessions/{sid}/:seal-initial", {})
        created_version = (
            created.get("state_version") if isinstance(created, dict) else None
        )
        sealed_version = (
            sealed.get("state_version") if isinstance(sealed, dict) else None
        )
        episode_epoch = (
            sealed_version.get("episode_epoch")
            if isinstance(sealed_version, dict)
            else created_version.get("episode_epoch")
            if isinstance(created_version, dict)
            else None
        )
        revision_only = episode_epoch is None and isinstance(sealed, dict)
        if revision_only:
            # A revision-only service reports a freshly sealed episode as the
            # first epoch at revision 0 and echoes the sid it bound.
            sealed_revision = sealed.get("revision")
            if sealed.get("sid") == sid and sealed_revision == 0:
                episode_epoch = 1
        if (
            isinstance(episode_epoch, bool)
            or not isinstance(episode_epoch, int)
            or episode_epoch < 1
            or (
                isinstance(sealed_version, dict)
                and sealed_version.get("sid") != sid
            )
        ):
            raise StateKernelError("state-service returned invalid episode identity")
        self._episode_bindings[sid] = {
            "task_id": task_id,
            "sid": sid,
            "episode_epoch": episode_epoch,
            "state_service_protocol": "revision_only" if revision_only else "canonical",
        }
        sealed_state = sealed.get("state") if isinstance(sealed, dict) else None
        sealed_state_hash = (
            sealed.get("state_hash") if isinstance(sealed, dict) else None
        )
        if isinstance(sealed_state, dict) and isinstance(sealed_state_hash, str):
            if (
                not isinstance(sealed.get("snapshot_id"), str)
                or not sealed["snapshot_id"]
                or sealed.get("snapshot_kind") != "initial"
                or not isinstance(sealed_version, dict)
                or sealed_version.get("revision") != 0
            ):
                raise StateKernelError("state-service initial snapshot identity mismatch")
            canonical_state_hash = hashlib.sha256(
                json.dumps(
                    sealed_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if canonical_state_hash != sealed_state_hash:
                raise StateKernelError("state-service initial snapshot hash mismatch")
            self._episode_bindings[sid].update({
                "initial_snapshot_id": sealed.get("snapshot_id"),
                "initial_revision": 0,
                "initial_state_hash": sealed_state_hash,
                "initial_state_hash_attested": True,
                "initial_state": sealed_state,
            })
        elif revision_only:
            # The sealed payload carried no state, so read the canonical initial
            # snapshot back from the service and bind it the same way.
            initial = await self._post(
                f"/internal/v1/sessions/{sid}/:snapshot", {"kind": "initial"}
            )
            canonical_initial = self._canonical_snapshot(
                initial, sid=sid, kind="initial", episode_epoch=episode_epoch
            )
            self._episode_bindings[sid].update({
                "initial_snapshot_id": canonical_initial["snapshot_id"],
                "initial_revision": canonical_initial["state_version"]["revision"],
                "initial_state_hash": canonical_initial["state_hash"],
                "initial_state_hash_attested": False,
                "initial_state": canonical_initial["state"],
            })
        return sid

    async def record_action(
        self,
        task_id: str,
        sid: str,
        observation: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        action_id = str(observation.get("action_id") or "")
        if not self.evaluation_enabled:
            return {
                "recorded": False,
                "reason": "evaluation_disabled_env_only",
                "action_id": action_id,
            }
        evaluator = self.plan.get("evaluators", {}).get(task_id, {})
        authorities = evaluator.get("required_outcome_authorities", [])
        matching = [
            row for row in authorities
            if isinstance(row, dict) and str(row.get("action_id") or "") == action_id
        ]
        if not matching:
            return {"recorded": False, "reason": "action_not_reward_authoritative"}
        results = []
        for index, authority in enumerate(matching):
            transaction_id = str(authority.get("transaction_id") or "")
            receipt_only = str(authority.get("authority_kind") or "") == "receipt"
            if transaction_id.endswith("::canonical_input_commit") and not receipt_only:
                source = str(observation.get("source") or "")
                event_type = str(observation.get("event_type") or "")
                expected_value = _canonical_input_expected_value(authority)
                if source != "webenv_playwright_focused_input" or event_type not in {
                    "type", "input_text",
                }:
                    return {
                        "recorded": False,
                        "reason": "canonical_input_requires_focused_text_entry",
                        "action_id": action_id,
                    }
                if not _canonical_input_value_matches(
                    expected_value, observation.get("observed_value")
                ):
                    return {
                        "recorded": False,
                        "reason": "canonical_input_value_mismatch",
                        "action_id": action_id,
                    }
            key = f"{idempotency_key}:{index}"
            if transaction_id and not receipt_only:
                result = await self._post(
                    f"/api/v1/sessions/{sid}/transactions/{transaction_id}",
                    {
                        "idempotency_key": key,
                        "payload": {"writes": authority.get("canonical_writes") or {}},
                    },
                )
            else:
                result = await self._post(
                    f"/internal/v1/sessions/{sid}/:record-action-receipt",
                    {
                        "action_id": action_id,
                        "receipt_event": authority.get("receipt_event") or "action_effect_observed",
                        "idempotency_key": key,
                        "payload": observation,
                    },
                )
            results.append(result)
        return {"recorded": True, "action_id": action_id, "results": results}

    async def record_terminal_action_boundary(
        self,
        task_id: str,
        sid: str,
        execution_step_index: int,
        observation: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        evaluator = self.plan.get("evaluators", {}).get(task_id, {})
        if not isinstance(evaluator, dict):
            return {"recorded": False, "reason": "missing_canonical_evaluator_plan"}
        receipt = evaluator.get("terminal_action_receipt")
        receipt = receipt if isinstance(receipt, dict) else {}
        observation_match = receipt.get("observation_match")
        if isinstance(observation_match, dict):
            observed = observation if isinstance(observation, dict) else {}
            field_map = {
                "action_ids": "action_id",
                "action_kinds": "action_kind",
                "targets": "target",
                "sources": "source",
            }
            for declaration, observed_field in field_map.items():
                allowed = observation_match.get(declaration)
                if allowed is None:
                    continue
                if not isinstance(allowed, list) or not allowed:
                    return {
                        "recorded": False,
                        "reason": "invalid_terminal_action_observation_match",
                        "field": declaration,
                    }
                allowed_values = {str(value) for value in allowed}
                if str(observed.get(observed_field) or "") not in allowed_values:
                    return {
                        "recorded": False,
                        "reason": "terminal_action_observation_mismatch",
                        "field": observed_field,
                        "observed": observed.get(observed_field),
                    }
        else:
            required = evaluator.get("required_terminal_action_step")
            if not isinstance(required, int) or required < 1:
                return {"recorded": False, "reason": "terminal_action_boundary_not_declared"}
            if int(execution_step_index) != required:
                return {
                    "recorded": False,
                    "reason": "not_declared_terminal_action_step",
                    "required_terminal_action_step": required,
                    "execution_step_index": int(execution_step_index),
                }
        action_id = str(receipt.get("action_id") or f"{task_id}:terminal_action_boundary")
        receipt_event = str(receipt.get("receipt_event") or "terminal_action_observed")
        return await self._post(
            f"/internal/v1/sessions/{sid}/:record-action-receipt",
            {
                "action_id": action_id,
                "receipt_event": receipt_event,
                "idempotency_key": idempotency_key,
                "payload": {
                    "execution_step_index": int(execution_step_index),
                    "action_observation": observation or {},
                    "terminal_action_boundary": True,
                },
            },
        )

    async def project_runtime_snapshot(
        self,
        sid: str,
        current_state: Any,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:

        projection = self._runtime_snapshot_projection()
        if projection is None:
            return {"projected": False, "reason": "runtime_snapshot_projection_not_declared"}
        if not isinstance(current_state, dict):
            return {"projected": False, "reason": "runtime_current_state_unavailable"}
        writes = {
            path: current_state[path]
            for path in projection["allowed_state_paths"]
            if path in current_state
        }
        if not writes:
            return {
                "projected": False,
                "reason": "no_declared_runtime_state_paths_present",
                "transaction_id": projection["transaction_id"],
            }
        result = await self._post(
            f"/api/v1/sessions/{sid}/transactions/{projection['transaction_id']}",
            {
                "idempotency_key": idempotency_key,
                "payload": {"writes": writes},
            },
        )
        return {
            "projected": True,
            "transaction_id": projection["transaction_id"],
            "projected_paths": sorted(writes),
            "result": result,
        }

    async def finalize_and_evaluate(self, task_id: str, sid: str) -> dict[str, Any]:
        finalized = await self._post(f"/internal/v1/sessions/{sid}/:finalize", {})
        return await self._evaluate_snapshot(
            task_id, sid, finalized, expected_snapshot_kind="final"
        )

    async def snapshot_episode(
        self, sid: str, *, kind: str = "audit",
    ) -> dict[str, Any]:
        return await self._post(
            f"/internal/v1/sessions/{sid}/:snapshot",
            {"kind": kind},
        )

    async def preview_and_evaluate(self, task_id: str, sid: str) -> dict[str, Any]:
        snapshot = await self.snapshot_episode(sid, kind="preview")
        result = await self._evaluate_snapshot(
            task_id, sid, snapshot, expected_snapshot_kind="preview"
        )
        return {**result, "preview": True}

    async def _evaluate_snapshot(
        self,
        task_id: str,
        sid: str,
        snapshot: dict[str, Any],
        *,
        expected_snapshot_kind: str,
    ) -> dict[str, Any]:
        if self._is_revision_only_snapshot(snapshot):
            binding = self._episode_bindings.get(sid) or {}
            snapshot = self._canonical_snapshot(
                snapshot,
                sid=sid,
                kind=expected_snapshot_kind,
                episode_epoch=binding.get("episode_epoch", 1),
            )
        if not self.evaluation_enabled:
            return {
                "score": 0.0,
                "reason": "evaluation_disabled_env_only",
                "evaluation_available": False,
                "evaluation_handle": snapshot.get("evaluation_handle"),
            }
        evaluator = self.plan.get("evaluators", {}).get(task_id)
        if not isinstance(evaluator, dict):
            return {
                "score": 0.0,
                "reason": "missing_canonical_evaluator_plan",
                "evaluation_handle": snapshot.get("evaluation_handle"),
            }
        if evaluator.get("state_kernel_evidence_contract") is not None:
            events_response = await self.request(
                "GET", f"/internal/v1/sessions/{sid}/events?from_revision=0"
            )
            if events_response.status_code >= 400:
                raise StateKernelError(
                    f"state-service events read failed: {events_response.text}"
                )
            try:
                events_payload = events_response.json()
            except json.JSONDecodeError:
                events_payload = None
            events = (
                events_payload.get("events")
                if isinstance(events_payload, dict)
                else None
            )
            return reduce_state_kernel_evidence(
                evaluator,
                task_id=task_id,
                sid=sid,
                binding=self._episode_bindings.get(sid),
                snapshot=snapshot,
                events=events,
                expected_snapshot_kind=expected_snapshot_kind,
            )
        if not evaluator.get("canonical_outcome_ready"):
            return {
                "score": 0.0,
                "reason": "missing_canonical_outcome_authority",
                "evaluation_handle": snapshot.get("evaluation_handle"),
            }
        expected = {str(value) for value in evaluator.get("outcome_transaction_ids", [])}
        required_authorities = [
            row for row in evaluator.get("required_outcome_authorities", [])
            if isinstance(row, dict)
        ]
        assertions = evaluator.get("canonical_assertions", {})
        terminal_authority_mode = evaluator.get("terminal_authority_mode")
        explicit_terminal_authority = terminal_authority_mode is not None
        allowed_terminal_authority_modes = {
            "canonical_assertions",
            "canonical_assertions_plus_checkpoint_dag",
        }
        terminal_checkpoint_ids = evaluator.get(
            "terminal_required_checkpoint_ids", []
        )
        checkpoint_declarations = evaluator.get("checkpoint_assertions")
        authority_contract_error = None
        if explicit_terminal_authority:
            if terminal_authority_mode not in allowed_terminal_authority_modes:
                authority_contract_error = "unknown_terminal_authority_mode"
            elif not isinstance(assertions, dict) or not assertions:
                authority_contract_error = "canonical_assertions_missing"
            elif terminal_authority_mode == "canonical_assertions" and (
                terminal_checkpoint_ids or checkpoint_declarations is not None
            ):
                authority_contract_error = "canonical_only_mode_declares_checkpoint_dag"
            elif (
                terminal_authority_mode
                == "canonical_assertions_plus_checkpoint_dag"
                and (
                    not isinstance(terminal_checkpoint_ids, list)
                    or not terminal_checkpoint_ids
                    or not isinstance(checkpoint_declarations, list)
                    or not checkpoint_declarations
                )
            ):
                authority_contract_error = "checkpoint_mode_missing_terminal_dag"
        if authority_contract_error is not None:
            return {
                "score": 0.0,
                "raw_reward": 0.0,
                "progress_reward": 0.0,
                "terminal_success": False,
                "reason": "invalid_terminal_authority_contract",
                "terminal_authority_mode": terminal_authority_mode,
                "terminal_authority_contract_error": authority_contract_error,
                "canonical_assertions_satisfied": False,
                "checkpoint_outcomes": {},
                "checkpoint_reward_vector": [],
                "nonzero_checkpoint_reached": False,
                "env_valid": False,
                "format_valid": True,
                "evaluation_handle": snapshot.get("evaluation_handle"),
            }
        outcome_kind = str(evaluator.get("reward_authority", {}).get("outcome_kind") or "")
        canonical_state = outcome_kind == "canonical_state"
        mutation = outcome_kind in {
            "business_mutation", "runtime_selection", "query_projection"
        }
        if (mutation or canonical_state) and (
            not isinstance(assertions, dict) or not assertions
        ):
            return {
                "score": 0.0,
                "reason": "missing_canonical_state_assertions",
                "evaluation_handle": snapshot.get("evaluation_handle"),
            }
        events_response = await self.request(
            "GET", f"/internal/v1/sessions/{sid}/events?from_revision=0"
        )
        if events_response.status_code >= 400:
            raise StateKernelError(f"state-service events read failed: {events_response.text}")
        observed = {
            str(event.get("transaction_id"))
            for event in events_response.json().get("events", [])
            if isinstance(event, dict) and event.get("transaction_id")
        }
        events = [
            event for event in events_response.json().get("events", [])
            if isinstance(event, dict)
        ]
        required_sequence = [
            str(transaction_id)
            for transaction_id in evaluator.get("required_transaction_sequence", [])
            if transaction_id
        ]
        observed_sequence = [
            str(event.get("transaction_id"))
            for event in events
            if event.get("transaction_id")
        ]
        transaction_sequence_satisfied = _is_ordered_subsequence(
            required_sequence,
            observed_sequence,
        )
        missing_receipts = []
        for authority in required_authorities:
            if authority.get("transaction_id"):
                continue
            action_id = str(authority.get("action_id") or "")
            receipt_event = str(authority.get("receipt_event") or "action_effect_observed")
            if not any(
                str(event.get("action_id") or "") == action_id
                and str(event.get("event_type") or "") == receipt_event
                for event in events
            ):
                missing_receipts.append({"action_id": action_id, "receipt_event": receipt_event})
        terminal_boundary = evaluator.get("required_terminal_action_step")
        terminal_receipt_plan = evaluator.get("terminal_action_receipt")
        terminal_receipt_plan = (
            terminal_receipt_plan if isinstance(terminal_receipt_plan, dict) else {}
        )
        terminal_match = terminal_receipt_plan.get("observation_match")
        terminal_receipt_required = (
            isinstance(terminal_match, dict)
            or (isinstance(terminal_boundary, int) and terminal_boundary > 0)
        )
        terminal_boundary_receipt = {
            "action_id": str(
                terminal_receipt_plan.get("action_id")
                or f"{task_id}:terminal_action_boundary"
            ),
            "receipt_event": str(
                terminal_receipt_plan.get("receipt_event")
                or "terminal_action_observed"
            ),
        }
        if (
            terminal_receipt_required
            and not any(
                str(event.get("action_id") or "") == terminal_boundary_receipt["action_id"]
                and str(event.get("event_type") or "") == terminal_boundary_receipt["receipt_event"]
                and (
                    isinstance(terminal_match, dict)
                    or int(
                        (event.get("payload") or {}).get("execution_step_index", -1)
                    ) == terminal_boundary
                )
                for event in events
                if isinstance(event, dict)
            )
        ):
            missing_receipts.append(terminal_boundary_receipt)
        missing = sorted(expected - observed)
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        assertion_failures = [
            path
            for path, assertion in assertions.items()
            if not isinstance(assertion, dict)
            or not _assertion_matches(_canonical_value(state, str(path)), assertion)
        ]
        if explicit_terminal_authority:


            passed = not assertion_failures
            success_reason = "canonical_assertions_satisfied"
        elif canonical_state:
            passed = bool(
                not assertion_failures
                and not missing
                and transaction_sequence_satisfied
            )
            success_reason = (
                "canonical_outcome_state_and_transaction_observed"
                if expected or required_sequence
                else "canonical_outcome_state_observed"
            )
        else:
            passed = bool(
                required_authorities
                and not missing
                and not missing_receipts
                and not assertion_failures
                and transaction_sequence_satisfied
            )
            if expected and any(not row.get("transaction_id") for row in required_authorities):
                success_reason = "canonical_outcome_transaction_and_receipt_observed"
            elif expected:
                success_reason = "canonical_outcome_transaction_observed"
            else:
                success_reason = "canonical_outcome_receipt_observed"
        result = {
            "score": 1.0 if passed else 0.0,


            "terminal_success": passed,
            "reason": (
                success_reason
                if passed
                else "canonical_outcome_state_or_transaction_mismatch"
            ),
            "expected_transaction_ids": sorted(expected),
            "missing_transaction_ids": missing,
            "missing_action_receipts": missing_receipts,
            "failed_canonical_assertions": sorted(assertion_failures),
            "required_transaction_sequence": required_sequence,
            "observed_transaction_sequence": observed_sequence,
            "transaction_sequence_satisfied": transaction_sequence_satisfied,
            "required_terminal_action_step": terminal_boundary,
            "terminal_action_match_mode": (
                "semantic_observation"
                if isinstance(terminal_match, dict)
                else "exact_execution_ordinal"
            ),
            "terminal_action_boundary_observed": not any(
                row.get("action_id") == terminal_boundary_receipt["action_id"]
                and row.get("receipt_event") == terminal_boundary_receipt["receipt_event"]
                for row in missing_receipts
            ),
            "evaluation_handle": snapshot.get("evaluation_handle"),
            "evidence_contract_mode": "legacy_compatibility",
            "portfolio_release_ready": False,
        }
        if explicit_terminal_authority:
            result.update({
                "raw_reward": result["score"],
                "progress_reward": 0.0,
                "terminal_authority_mode": terminal_authority_mode,
                "canonical_assertions_satisfied": not assertion_failures,
                "transaction_attribution_only": True,
                "checkpoint_outcomes": {},
                "checkpoint_reward_vector": [],
                "nonzero_checkpoint_reached": False,
                "env_valid": True,
                "format_valid": True,
            })
        checkpoint_reduction = _reduce_declared_checkpoints(evaluator, state, events)
        if checkpoint_reduction is not None:


            result.update(checkpoint_reduction)
            if passed and not checkpoint_reduction["terminal_checkpoints_satisfied"]:
                passed = False
                result["score"] = 0.0
                result["terminal_success"] = False
                result["reason"] = "terminal_required_checkpoints_not_reached"
            result["raw_reward"] = result["score"]
        return result

    async def teardown_episode(self, sid: str) -> None:
        await self._post(f"/internal/v1/sessions/{sid}/:finalize", {})
        await self._post(f"/internal/v1/sessions/{sid}/:delete", {})
        self._episode_bindings.pop(sid, None)
