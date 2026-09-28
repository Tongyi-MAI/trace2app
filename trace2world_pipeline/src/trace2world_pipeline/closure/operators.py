
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .owners import Failure, Verdict


@runtime_checkable
class ClosureOperators(Protocol):

    def build_obligations(self, source_traces: Any) -> Any:
        ...

    def execute(self, environment: Any, obligations: Any) -> Any:
        ...

    def review(self, source_traces: Any, execution: Any, obligations: Any) -> Verdict | Failure:
        ...

    def probe(self, environment: Any, page_specifications: Any, obligations: Any) -> Verdict | Failure:
        ...

    def repair(self, environment: Any, failure: Failure, page_specifications: Any) -> tuple[Any, frozenset[str]]:
        ...

    def invalidate(self, delta: frozenset[str]) -> None:
        ...

    def revision_evidence(self, environment: Any) -> Any:
        ...
