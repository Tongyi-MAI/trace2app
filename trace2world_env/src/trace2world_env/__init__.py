"""Trace2World environment host, client, and bundled feedback agent.

Start a host with ``t2w-serve --environments-dir <release>``; drive it with
:class:`EnvironmentClient` or ``t2w-rollout``.
"""

from trace2world_env.client import ActionOutcome, EnvironmentClient, Episode, Observation

__version__ = "0.2.0"

__all__ = ["ActionOutcome", "EnvironmentClient", "Episode", "Observation", "__version__"]
