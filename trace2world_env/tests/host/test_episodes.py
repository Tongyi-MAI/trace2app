"""Episode-manager shutdown, with the browser and the state service replaced by fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from trace2world_env import episodes as episodes_module
from trace2world_env.catalog import load_catalog
from trace2world_env.episodes import Episode, EpisodeManager


class FakeSession:
    def __init__(self, log: list[str], name: str) -> None:
        self.log = log
        self.name = name

    async def close(self) -> None:
        self.log.append(f"browser:{self.name}")


class FakeKernel:
    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log = log
        self.fail = fail

    async def teardown_episode(self, sid: str) -> None:
        self.log.append(f"teardown:{sid}")
        if self.fail:
            raise RuntimeError("state service gone")

    async def stop(self) -> None:
        self.log.append("kernel:stop")
        if self.fail:
            raise RuntimeError("state service gone")


class FakeLauncher:
    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log = log
        self.fail = fail

    async def stop(self) -> None:
        self.log.append("driver:stop")
        if self.fail:
            raise RuntimeError("driver gone")


def _manager(release_dir: Path, log: list[str], *, fail: bool = False) -> EpisodeManager:
    catalog = load_catalog(release_dir)
    manager = EpisodeManager(
        catalog, FakeLauncher(log, fail=fail), app_root_url="http://localhost:17100/app"
    )
    manager.state_kernels["demo"] = FakeKernel(log, fail=fail)
    manager._kernel_locks["demo"] = asyncio.Lock()
    for ordinal, task_id in enumerate(("t1", "t2"), start=1):
        task = catalog.tasks[task_id]
        episode = Episode(
            episode_id=f"ep{ordinal}",
            ordinal=ordinal,
            task=task,
            environment=catalog.environment_of(task),
            app_base_url="http://localhost:17100/app/demo",
            sid=f"sid{ordinal}",
            session=FakeSession(log, f"ep{ordinal}"),
        )
        manager.episodes[episode.episode_id] = episode
        manager._sid_environment[episode.sid] = "demo"
    return manager


def _assert_cleared(manager: EpisodeManager) -> None:
    assert manager.episodes == {} and manager.state_kernels == {}
    assert manager._kernel_locks == {} and manager._sid_environment == {}
    assert manager.kernel_for_sid("sid1") is None


def test_stop_closes_episodes_before_stopping_services(release_dir: Path) -> None:
    log: list[str] = []
    manager = _manager(release_dir, log)

    asyncio.run(manager.stop())

    assert log == [
        "teardown:sid1", "browser:ep1",
        "teardown:sid2", "browser:ep2",
        "kernel:stop", "driver:stop",
    ]
    _assert_cleared(manager)


def test_stop_is_best_effort(release_dir: Path) -> None:
    log: list[str] = []
    manager = _manager(release_dir, log, fail=True)

    asyncio.run(manager.stop())

    assert log == [
        "teardown:sid1", "browser:ep1",
        "teardown:sid2", "browser:ep2",
        "kernel:stop", "driver:stop",
    ]
    _assert_cleared(manager)


def test_stop_does_not_wait_forever_for_a_busy_episode(
    release_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(episodes_module, "SHUTDOWN_CLOSE_TIMEOUT_SECONDS", 0.05)
    log: list[str] = []
    manager = _manager(release_dir, log)

    async def scenario() -> None:
        # An action that never finishes holds ep1's lock for the whole shutdown.
        await manager.episodes["ep1"].lock.acquire()
        await manager.stop()

    asyncio.run(scenario())

    assert log == [
        "browser:ep1",
        "teardown:sid2", "browser:ep2",
        "kernel:stop", "driver:stop",
    ]
    _assert_cleared(manager)
