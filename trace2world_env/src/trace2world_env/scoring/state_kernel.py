"""Client for an environment's packaged SQLite state service (the state kernel).

Environments with ``runtime_profile: state_kernel_sqlite`` ship a small HTTP service. The host
starts one process per environment, opens one session (``sid``) per episode, records reward
authorities for the actions the agent performs, projects the page's runtime state into declared
state paths, and evaluates the finalized snapshot against the environment's
``state_evaluator_plan.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

from trace2world_env.scoring.errors import StateKernelError
from trace2world_env.scoring.evidence import reduce_state_kernel_evidence
from trace2world_env.scoring.expressions import canonical_hash
from trace2world_env.scoring.outcomes import evaluate_canonical_outcome

#: ``observation.source`` of a text-entry action; canonical input commits require it.
FOCUSED_INPUT_SOURCE = "env_playwright_focused_input"


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _canonical_input_expected_value(authority: dict[str, Any]) -> Any:
    """The single scalar a canonical-input authority writes, or ``None`` if ambiguous."""
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
    """Compare typed text with the expected value (digit strings ignore inner spaces)."""
    if expected is None or observed is None:
        return False
    expected_text = str(expected).strip()
    observed_text = str(observed).strip()
    expected_compact = "".join(expected_text.split())
    observed_compact = "".join(observed_text.split())
    if expected_compact.isdigit() and observed_compact.isdigit():
        return expected_compact == observed_compact
    return expected_text == observed_text


class StateKernel:
    """One packaged state-service process plus the host's per-episode bindings."""

    def __init__(
        self,
        resource_dir: str | Path,
        manifest: dict[str, Any],
        evaluator_plan: str | Path,
        *,
        writable_root: str | Path | None = None,
    ) -> None:
        self.resource_dir = Path(resource_dir).resolve()
        self.manifest = manifest
        self.plan = json.loads(Path(evaluator_plan).read_text(encoding="utf-8"))
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
        """The manifest's runtime-snapshot projection (transaction id + allowed paths), if any."""
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
        """Whether page runtime state is projected into the state service after each step."""
        return self._runtime_snapshot_projection() is not None

    async def start(self) -> None:
        """Launch the service on a free local port and wait for a hash-checked health reply."""
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
        """Close the HTTP client and terminate the service process."""
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
        """Forward one request to the service (only content-type/accept headers pass)."""
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

        Packaged state services on the ``trace2world_state_kernel_sqlite_v1``
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
            state_hash = canonical_hash(state)
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
        ordinal: int,
        initial_state: dict[str, Any] | None = None,
    ) -> str:
        """Create, seed, and seal a session for a new episode; returns its ``sid``.

        ``ordinal`` is the host's episode counter and only makes sids readable. The sealed
        initial snapshot is bound to the sid so the evidence reducer can replay from it.
        """
        sid = f"sid_{ordinal}_{uuid.uuid4().hex}"
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
        """Commit the reward authorities the evaluator plan binds to this observed action."""
        action_id = str(observation.get("action_id") or "")
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
                if source != FOCUSED_INPUT_SOURCE or event_type not in {
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
        """Record the terminal-action receipt when this step matches the plan's declaration
        (an observation match, or else the exact 1-based execution step).
        """
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

        """Write the declared paths of the page's runtime ``current_state`` in one transaction."""
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
        """Finalize the session and evaluate its final snapshot."""
        finalized = await self._post(f"/internal/v1/sessions/{sid}/:finalize", {})
        return await self._evaluate_snapshot(
            task_id, sid, finalized, expected_snapshot_kind="final"
        )

    async def snapshot_episode(
        self, sid: str, *, kind: str = "audit",
    ) -> dict[str, Any]:
        """Read a snapshot of the session without finalizing it (``kind``: audit/preview)."""
        return await self._post(
            f"/internal/v1/sessions/{sid}/:snapshot",
            {"kind": kind},
        )

    async def preview_and_evaluate(self, task_id: str, sid: str) -> dict[str, Any]:
        """Evaluate a preview snapshot; the session stays open."""
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
        """Dispatch to the strict evidence reducer or the canonical-outcome evaluator."""
        if self._is_revision_only_snapshot(snapshot):
            binding = self._episode_bindings.get(sid) or {}
            snapshot = self._canonical_snapshot(
                snapshot,
                sid=sid,
                kind=expected_snapshot_kind,
                episode_epoch=binding.get("episode_epoch", 1),
            )
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
        return await evaluate_canonical_outcome(
            evaluator,
            task_id=task_id,
            snapshot=snapshot,
            read_events=lambda: self._read_events(sid),
        )

    async def _read_events(self, sid: str) -> list[Any]:
        response = await self.request("GET", f"/internal/v1/sessions/{sid}/events?from_revision=0")
        if response.status_code >= 400:
            raise StateKernelError(f"state-service events read failed: {response.text}")
        return response.json().get("events", [])

    async def teardown_episode(self, sid: str) -> None:
        """Finalize and delete the session and forget its binding."""
        await self._post(f"/internal/v1/sessions/{sid}/:finalize", {})
        await self._post(f"/internal/v1/sessions/{sid}/:delete", {})
        self._episode_bindings.pop(sid, None)
