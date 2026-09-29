"""HTTP API shapes and error mapping, with the browser layer replaced by a stub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trace2world_env.catalog import Catalog, load_catalog
from trace2world_env.episodes import (
    StateKernelNotConfiguredError,
    UnknownEpisodeError,
    UnknownTaskError,
)
from trace2world_env.server import create_app

PNG = b"\x89PNG\r\n\x1a\nstub"
IDENTITY = {
    "current_url": "http://localhost:17100/app/demo/index.html?sid=abc",
    "page_filename": "index.html",
    "viewport": {"width": 430, "height": 884},
    "device_scale_factor": 2.0,
}


class _StubEpisode:
    def __init__(self, episode_id: str, task: Any, environment: Any) -> None:
        self.episode_id = episode_id
        self.task = task
        self.environment = environment
        self.sid = "sid_1_abc"

    def identity(self) -> dict[str, Any]:
        return IDENTITY


class StubManager:
    """Implements the EpisodeManager surface the server uses, without a browser."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.episodes: dict[str, _StubEpisode] = {}
        self.state_kernels: dict[str, Any] = {}
        self.started = self.stopped = False
        self.next_result: dict[str, Any] = {}
        self.evaluation: dict[str, Any] = {}

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def get(self, episode_id: str) -> _StubEpisode:
        try:
            return self.episodes[episode_id]
        except KeyError:
            raise UnknownEpisodeError(episode_id) from None

    async def start_episode(self, task_id: str) -> _StubEpisode:
        if task_id not in self.catalog.tasks:
            raise UnknownTaskError(task_id)
        task = self.catalog.tasks[task_id]
        environment = self.catalog.environment_of(task)
        episode = _StubEpisode(f"ep{len(self.episodes) + 1}", task, environment)
        self.episodes[episode.episode_id] = episode
        return episode

    async def observe(self, episode_id: str) -> tuple[bytes, dict[str, Any]]:
        self.get(episode_id)
        return PNG, IDENTITY

    async def act(
        self, episode_id: str, action: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.get(episode_id)
        if action.get("action_type") == "explode":
            raise RuntimeError("page crashed")
        return self.next_result, IDENTITY

    async def evaluate(self, episode_id: str, *, final: bool = True) -> dict[str, Any]:
        self.get(episode_id)
        return {**self.evaluation, "final": final}

    async def close(self, episode_id: str) -> None:
        self.get(episode_id)
        del self.episodes[episode_id]

    async def runtime_audit(self, episode_id: str) -> dict[str, Any]:
        self.get(episode_id)
        return {"available": True}

    async def state_snapshot(self, episode_id: str, *, kind: str = "audit") -> dict[str, Any]:
        episode = self.get(episode_id)
        raise StateKernelNotConfiguredError(episode.environment.environment_id)

    def kernel_for_sid(self, sid: str | None) -> None:
        return None


@pytest.fixture
def api(release_dir: Path):
    catalog = load_catalog(release_dir)
    manager = StubManager(catalog)
    app = create_app(catalog, port=17100, manager=manager)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, manager
    assert manager.started and manager.stopped


def test_health_and_tasks(api) -> None:
    client, _ = api
    health = client.get("/health").json()
    assert health["ok"] is True and isinstance(health["instance_id"], str)
    assert (health["environments"], health["tasks"], health["active_episodes"]) == (1, 2, 0)
    tasks = client.get("/tasks").json()["tasks"]
    assert [task["task_id"] for task in tasks] == ["t1", "t2"]
    assert set(tasks[0]) == {
        "task_id", "environment", "instruction", "start_page", "initial_state", "runtime_profile"
    }


def test_episode_lifecycle_shapes(api) -> None:
    client, manager = api
    started = client.post("/episodes", json={"task_id": "t1"}).json()
    assert started == {
        "episode_id": "ep1",
        "task_id": "t1",
        "environment": "demo",
        "instruction": "Open the detail page",
    }
    observation = client.get("/episodes/ep1/observation").json()
    assert observation == {
        "screenshot": "iVBORw0KGgpzdHVi",
        "url": IDENTITY["current_url"],
        "page": "index.html",
        "viewport": {"width": 430, "height": 884},
        "device_scale_factor": 2.0,
    }

    manager.next_result = {
        "actionability_rejected": True,
        "env_text": "ERROR[INVALID_ACTION]\nNo state change. Choose a supported action.",
    }
    rejected = client.post("/episodes/ep1/actions", json={"action": {"action_type": "fly"}}).json()
    assert rejected["accepted"] is False and rejected["done"] is False
    assert rejected["feedback"].startswith("ERROR[INVALID_ACTION]")
    assert rejected["result"] == manager.next_result
    assert rejected["url"] == IDENTITY["current_url"] and rejected["page"] == "index.html"

    manager.next_result = {"action_observation": {"action_id": "x:step1"}}
    accepted = client.post(
        "/episodes/ep1/actions", json={"action": {"action_type": "answer", "text": "hi"}}
    ).json()
    assert (accepted["accepted"], accepted["feedback"], accepted["done"]) == (True, None, True)

    manager.evaluation = {"score": 0.0, "raw_reward": 0.5, "terminal_success": False, "reason": ""}
    evaluation = client.post("/episodes/ep1/evaluate", json={"final": False}).json()
    assert evaluation["reward"] == 0.5 and evaluation["success"] is False
    assert evaluation["details"]["final"] is False
    manager.evaluation = {"score": 1.0, "reason": "canonical_outcome_state_observed"}
    evaluation = client.post("/episodes/ep1/evaluate").json()
    assert (evaluation["reward"], evaluation["success"]) == (1.0, None)
    assert evaluation["reason"] == "canonical_outcome_state_observed"

    assert client.get("/episodes/ep1/page").json()["page"] == "index.html"
    assert client.get("/episodes/ep1/runtime").json() == {
        "episode_id": "ep1",
        "snapshots": {"available": True},
    }
    assert client.delete("/episodes/ep1").json() == {"closed": True}


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        ("post", "/episodes", {"task_id": "nope"}, 404, "unknown_task"),
        ("post", "/episodes", {"task": "t1"}, 422, "invalid_request"),
        ("get", "/episodes/missing/observation", None, 404, "unknown_episode"),
        ("post", "/episodes/missing/actions", {"action": {}}, 404, "unknown_episode"),
        ("delete", "/episodes/missing", None, 404, "unknown_episode"),
        ("get", "/no/such/route", None, 404, "not_found"),
    ],
)
def test_error_mapping(api, method: str, path: str, body: Any, status: int, code: str) -> None:
    client, _ = api
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert isinstance(response.json()["error"]["message"], str)


def test_action_body_must_be_an_object(api) -> None:
    client, _ = api
    client.post("/episodes", json={"task_id": "t1"})
    response = client.post("/episodes/ep1/actions", json={"action": "click"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_state_and_internal_errors(api) -> None:
    client, _ = api
    client.post("/episodes", json={"task_id": "t2"})
    state = client.get("/episodes/ep1/state", params={"kind": "audit"})
    assert state.status_code == 404
    assert state.json()["error"]["code"] == "state_kernel_not_configured"
    crashed = client.post("/episodes/ep1/actions", json={"action": {"action_type": "explode"}})
    assert crashed.status_code == 500
    assert crashed.json()["error"] == {
        "code": "internal_error",
        "message": "RuntimeError: page crashed",
    }


def test_page_facing_routes(api) -> None:
    client, manager = api
    page = client.get("/app/demo/index.html")
    assert page.status_code == 200 and "home" in page.text
    missing = client.get("/app/demo/absent.png")
    assert missing.status_code == 404 and "error" not in missing.json()
    proxy = client.get("/api/v1/sessions/abc/state")
    assert proxy.status_code == 404
    assert proxy.json() == {
        "error": {
            "code": "state_kernel_not_configured",
            "message": "no hosted environment has a state kernel",
        }
    }
    manager.state_kernels["demo"] = object()
    unbound = client.get("/api/v1/sessions/abc/state", params={"sid": "abc"})
    assert unbound.status_code == 404
    assert unbound.json() == {
        "error": {
            "code": "state_kernel_episode_not_found",
            "message": "no active state-kernel episode for sid: abc",
        }
    }
