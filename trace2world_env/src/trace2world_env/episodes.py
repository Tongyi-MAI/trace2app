"""Episodes: one task run in its own browser (and state-kernel session), driven step by step.

The manager also keeps the *execution context step*: the index of the reference-trace step the
agent is believed to be at. Pages use ``<trace_id>:step<N>`` to attribute the next input to a
declared trace action; the host advances it before each action, rolls it back when the action is
rejected, and adopts the step the page actually observed when it reports one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trace2world_env.actions import (
    ActionContext,
    execute_action,
    is_terminal,
    resolve_app_aliases,
)
from trace2world_env.actions.vocabulary import is_back_action
from trace2world_env.browser import BrowserLauncher, BrowserSession
from trace2world_env.catalog import Catalog, Environment, Task
from trace2world_env.page_runtime import (
    capture_screenshot,
    flush_before_screenshot,
    launch_context_script,
    page_identity,
    read_runtime_audit_state,
    read_runtime_current_state,
    set_context_step,
    task_url,
)
from trace2world_env.scoring.browser import evaluate_browser_checkpoints
from trace2world_env.scoring.state_kernel import StateKernel

logger = logging.getLogger(__name__)

#: Upper bound for the first runtime-state read after a state-kernel task page opens.
TASK_INIT_SNAPSHOT_TIMEOUT_SECONDS = 60.0
#: Upper bound for closing one episode at shutdown (waiting for an in-flight action, tearing
#: down its state session, closing its browser) before its browser is closed directly.
SHUTDOWN_CLOSE_TIMEOUT_SECONDS = 30.0


class UnknownTaskError(KeyError):
    """No hosted environment declares the requested task id."""


class UnknownEpisodeError(KeyError):
    """The episode id is not (or no longer) active."""


class StateKernelNotConfiguredError(LookupError):
    """The episode's environment has no packaged state service."""


@dataclass
class Episode:
    """One active episode."""

    episode_id: str
    ordinal: int
    task: Task
    environment: Environment
    app_base_url: str
    sid: str
    session: BrowserSession | None = None
    step_count: int = 0
    context_step: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def page(self) -> Any:
        """The episode's Playwright page."""
        assert self.session is not None
        return self.session.page

    def identity(self) -> dict[str, Any]:
        """Current URL, page file, viewport, and device scale factor."""
        return page_identity(
            self.page,
            device_scale_factor=self.environment.browser_profile.device_scale_factor,
        )


def observed_trace_step(action_observation: object) -> int | None:
    """The ``N`` of an observed ``<trace>:step<N>`` action id, if any."""
    if not isinstance(action_observation, dict):
        return None
    action_id = action_observation.get("action_id")
    if not isinstance(action_id, str):
        return None
    match = re.search(r":step(\d+)$", action_id)
    return int(match.group(1)) if match else None


def next_context_step(last_context_step: int | None, action_index: int) -> int:
    """The provisional context step for the next action: at least the action index and past the
    last published step."""
    if isinstance(action_index, bool) or action_index < 0:
        raise ValueError("execution_action_index must be a non-negative integer")
    if isinstance(last_context_step, bool) or (
        last_context_step is not None and last_context_step < 0
    ):
        raise ValueError("last_context_step must be non-negative or None")
    if last_context_step is None:
        return action_index
    return max(action_index, last_context_step + 1)


def should_republish_context(action: dict[str, Any], before_url: str, after_url: str) -> bool:
    """Whether a new document may have lost the published step (back action or URL change)."""
    if is_back_action(action):
        return True
    return bool(before_url and after_url and before_url != after_url)


class EpisodeManager:
    """Owns the browser launcher, the per-environment state kernels, and active episodes."""

    def __init__(
        self,
        catalog: Catalog,
        launcher: BrowserLauncher,
        *,
        app_root_url: str,
        state_root: str | Path | None = None,
    ) -> None:
        self.catalog = catalog
        self.launcher = launcher
        self.app_root_url = app_root_url.rstrip("/")
        self.state_root = Path(state_root) if state_root else None
        self.episodes: dict[str, Episode] = {}
        self.state_kernels: dict[str, StateKernel] = {}
        # Each kernel is its own process; serialise requests per kernel only, so unrelated
        # environments do not throttle each other.
        self._kernel_locks: dict[str, asyncio.Lock] = {}
        self._sid_environment: dict[str, str] = {}
        self._ordinal = 0

    async def start(self) -> None:
        """Start the browser driver and one state service per state-kernel environment."""
        await self.launcher.start()
        for environment in self.catalog.environments.values():
            if not environment.uses_state_kernel:
                continue
            kernel = StateKernel(
                environment.root,
                environment.manifest,
                environment.evaluator_plan,
                writable_root=(
                    self.state_root / environment.environment_id if self.state_root else None
                ),
            )
            await kernel.start()
            self.state_kernels[environment.environment_id] = kernel
            self._kernel_locks[environment.environment_id] = asyncio.Lock()

    async def stop(self) -> None:
        """Close every active episode, then stop the state services and the browser driver.

        Episodes are closed exactly like ``DELETE /episodes/{id}`` (state session torn down,
        browser closed) while their state services are still running. Shutdown is best effort:
        every step is bounded, failures are logged rather than raised, and all bookkeeping is
        cleared even when a step fails.
        """
        try:
            for episode_id in list(self.episodes):
                await self._close_at_shutdown(episode_id)
            for environment_id, kernel in list(self.state_kernels.items()):
                try:
                    await kernel.stop()
                except Exception:
                    logger.warning(
                        "State service %s did not stop cleanly", environment_id, exc_info=True
                    )
            try:
                await self.launcher.stop()
            except Exception:
                logger.warning("Browser driver did not stop cleanly", exc_info=True)
        finally:
            self.episodes.clear()
            self.state_kernels.clear()
            self._kernel_locks.clear()
            self._sid_environment.clear()

    async def _close_at_shutdown(self, episode_id: str) -> None:
        episode = self.episodes.get(episode_id)
        if episode is None:
            return
        try:
            await asyncio.wait_for(self.close(episode_id), timeout=SHUTDOWN_CLOSE_TIMEOUT_SECONDS)
            return
        except TimeoutError:
            logger.warning(
                "Episode %s did not close within %.0fs at shutdown",
                episode_id, SHUTDOWN_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "Episode %s did not close cleanly at shutdown", episode_id, exc_info=True
            )
        # Whatever failed, no browser may outlive the host: close it directly (bounded).
        self.episodes.pop(episode_id, None)
        self._sid_environment.pop(episode.sid, None)
        if episode.session is not None:
            try:
                await episode.session.close()
            except Exception:
                logger.warning("Episode %s browser did not close", episode_id, exc_info=True)
            episode.session = None

    def app_base_url(self, environment: Environment) -> str:
        """Base URL of an environment's static app (``.../app/<environment id>``)."""
        return f"{self.app_root_url}/{environment.environment_id}"

    def get(self, episode_id: str) -> Episode:
        """The active episode ``episode_id``; raises :class:`UnknownEpisodeError`."""
        try:
            return self.episodes[episode_id]
        except KeyError:
            raise UnknownEpisodeError(episode_id) from None

    def _kernel(self, environment: Environment) -> tuple[StateKernel | None, asyncio.Lock | None]:
        env_id = environment.environment_id
        return self.state_kernels.get(env_id), self._kernel_locks.get(env_id)

    async def start_episode(self, task_id: str) -> Episode:
        """Open a fresh browser context (and state-kernel session) at the task's start page."""
        try:
            task = self.catalog.task(task_id)
        except KeyError:
            raise UnknownTaskError(task_id) from None
        environment = self.catalog.environment_of(task)
        kernel, kernel_lock = self._kernel(environment)
        self._ordinal += 1
        episode = Episode(
            episode_id=uuid.uuid4().hex,
            ordinal=self._ordinal,
            task=task,
            environment=environment,
            app_base_url=self.app_base_url(environment),
            sid=uuid.uuid4().hex,
        )
        if kernel is not None and kernel_lock is not None:
            async with kernel_lock:
                episode.sid = await kernel.initialize_episode(
                    task.task_id, episode.ordinal, initial_state=task.initial_state
                )
        self._sid_environment[episode.sid] = environment.environment_id
        try:
            profile = environment.browser_profile
            episode.session = await self.launcher.open_task_page(
                task_url(episode.app_base_url, task.start_page, episode.sid),
                init_script=launch_context_script(
                    task.task_id,
                    start_filename=task.start_page,
                    initial_state=task.initial_state,
                    source_reference_task_id=task.source_reference_task_id,
                    sid=episode.sid,
                ),
                viewport=profile.viewport,
                device_scale_factor=profile.device_scale_factor,
                sid=episode.sid,
                app_base_url=episode.app_base_url,
            )
            if kernel is not None and kernel_lock is not None:
                runtime_snapshot = await asyncio.wait_for(
                    read_runtime_current_state(episode.page),
                    timeout=TASK_INIT_SNAPSHOT_TIMEOUT_SECONDS,
                )
                async with kernel_lock:
                    await _project_runtime_state(
                        kernel,
                        episode.sid,
                        runtime_snapshot,
                        idempotency_key="host-runtime-projection:task-init",
                    )
        except Exception:
            await self._release(episode)
            raise
        self.episodes[episode.episode_id] = episode
        logger.info(
            "Episode %s started: task=%s environment=%s",
            episode.episode_id, task.task_id, environment.environment_id,
        )
        return episode

    async def act(
        self, episode_id: str, action: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one action; returns ``(result, page_identity)``."""
        episode = self.get(episode_id)
        async with episode.lock:
            result = await self._act(episode, action)
            return result, episode.identity()

    async def _act(self, episode: Episode, action: dict[str, Any]) -> dict[str, Any]:
        page = episode.page
        environment = episode.environment
        profile = environment.browser_profile
        url_before = str(page.url or "")
        terminal = is_terminal(action)
        previous_step = episode.context_step
        action_index = episode.step_count
        provisional_step: int | None = None
        if not terminal:
            provisional_step = next_context_step(previous_step, action_index)
            await set_context_step(page, provisional_step)
            episode.context_step = provisional_step
        context = ActionContext(
            app_base_url=episode.app_base_url,
            entry_page=environment.entry_page,
            device_scale_factor=profile.device_scale_factor,
            scroll_delta=profile.scroll_delta,
            app_aliases=resolve_app_aliases(
                episode.task.available_apps, environment.environment_id
            ),
        )
        try:
            result = await execute_action(page, action, context)
        except Exception:
            if not terminal:
                await set_context_step(page, previous_step)
                episode.context_step = previous_step
            raise
        observation = result.get("action_observation")
        observed_step = observed_trace_step(observation)
        if not terminal:
            if result.get("actionability_rejected"):
                await set_context_step(page, previous_step)
                episode.context_step = previous_step
            else:
                episode.step_count = action_index + 1
                if observed_step is not None:
                    await set_context_step(page, observed_step)
                    episode.context_step = observed_step
                else:
                    if should_republish_context(action, url_before, str(page.url or "")):
                        await set_context_step(page, provisional_step)
                    episode.context_step = provisional_step
            result = {
                **result,
                "execution_context_step": episode.context_step,
                "execution_context_step_source": (
                    "observed_action_id"
                    if observed_step is not None
                    else "execution_step_fallback_pre_action"
                ),
            }
        elif observed_step is not None:
            await set_context_step(page, observed_step)

        kernel, kernel_lock = self._kernel(environment)
        if kernel is None or kernel_lock is None:
            return result
        authority_result = None
        terminal_boundary_result: dict[str, Any] = {
            "recorded": False,
            "reason": "action_observation_unavailable",
        }
        if isinstance(observation, dict):
            async with kernel_lock:
                authority_result = await kernel.record_action(
                    episode.task.task_id,
                    episode.sid,
                    observation,
                    idempotency_key=f"host-step:{episode.step_count}",
                )
                terminal_boundary_result = await kernel.record_terminal_action_boundary(
                    episode.task.task_id,
                    episode.sid,
                    episode.step_count,
                    observation,
                    idempotency_key=f"host-terminal-boundary:{episode.step_count}",
                )
        runtime_snapshot = await read_runtime_current_state(page)
        async with kernel_lock:
            projection_result = await _project_runtime_state(
                kernel,
                episode.sid,
                runtime_snapshot,
                idempotency_key=f"host-runtime-projection:step:{episode.step_count}",
            )
        return {
            **result,
            **(
                {"state_authority_result": authority_result}
                if authority_result is not None
                else {}
            ),
            "terminal_action_boundary_result": terminal_boundary_result,
            "runtime_snapshot_projection": projection_result,
        }

    async def observe(self, episode_id: str) -> tuple[bytes, dict[str, Any]]:
        """Screenshot (PNG bytes) of the settled page and its identity."""
        episode = self.get(episode_id)
        async with episode.lock:
            await flush_before_screenshot(episode.page)
            png = await capture_screenshot(episode.page)
            return png, episode.identity()

    async def evaluate(self, episode_id: str, *, final: bool = True) -> dict[str, Any]:
        """Evaluate the episode; ``final=False`` previews without finalizing the state kernel."""
        episode = self.get(episode_id)
        async with episode.lock:
            kernel, kernel_lock = self._kernel(episode.environment)
            if kernel is not None and kernel_lock is not None:
                async with kernel_lock:
                    if final:
                        return await kernel.finalize_and_evaluate(
                            episode.task.task_id, episode.sid
                        )
                    return await kernel.preview_and_evaluate(episode.task.task_id, episode.sid)
            assert episode.environment.evaluators_dir is not None
            result = await evaluate_browser_checkpoints(
                episode.page,
                task_id=episode.task.task_id,
                evaluators_dir=episode.environment.evaluators_dir,
            )
            return result.to_payload()

    async def close(self, episode_id: str) -> None:
        """Tear down the state-kernel session and close the episode's browser."""
        episode = self.get(episode_id)
        async with episode.lock:
            self.episodes.pop(episode_id, None)
            await self._release(episode)
        logger.info("Episode %s closed", episode_id)

    async def _release(self, episode: Episode) -> None:
        self._sid_environment.pop(episode.sid, None)
        try:
            kernel, kernel_lock = self._kernel(episode.environment)
            if kernel is not None and kernel_lock is not None:
                async with kernel_lock:
                    await kernel.teardown_episode(episode.sid)
        finally:
            if episode.session is not None:
                await episode.session.close()
                episode.session = None

    async def runtime_audit(self, episode_id: str) -> dict[str, Any]:
        """Read-only audit of the page runtime (see :func:`read_runtime_audit_state`)."""
        episode = self.get(episode_id)
        return await read_runtime_audit_state(episode.page)

    async def state_snapshot(self, episode_id: str, *, kind: str = "audit") -> dict[str, Any]:
        """A state-kernel snapshot of the episode without finalizing it."""
        episode = self.get(episode_id)
        kernel, kernel_lock = self._kernel(episode.environment)
        if kernel is None or kernel_lock is None:
            raise StateKernelNotConfiguredError(episode.environment.environment_id)
        async with kernel_lock:
            return await kernel.snapshot_episode(episode.sid, kind=kind)

    def kernel_for_sid(self, sid: str | None) -> tuple[StateKernel, asyncio.Lock] | None:
        """The state kernel serving the episode session ``sid``, if any."""
        env_id = self._sid_environment.get(sid or "")
        if env_id is None or env_id not in self.state_kernels:
            return None
        return self.state_kernels[env_id], self._kernel_locks[env_id]


async def _project_runtime_state(
    kernel: StateKernel,
    sid: str,
    runtime_snapshot: Any,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Project the page's runtime ``current_state`` into the kernel when declared."""
    if not kernel.runtime_snapshot_projection_enabled:
        return {"projected": False, "reason": "runtime_snapshot_projection_not_declared"}
    if not isinstance(runtime_snapshot, dict) or not runtime_snapshot.get("available"):
        return {
            "projected": False,
            "reason": (
                runtime_snapshot.get("reason", "runtime_snapshot_read_failed")
                if isinstance(runtime_snapshot, dict)
                else "runtime_snapshot_read_failed"
            ),
        }
    return await kernel.project_runtime_snapshot(
        sid,
        runtime_snapshot.get("current_state"),
        idempotency_key=idempotency_key,
    )
