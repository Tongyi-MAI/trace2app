
from __future__ import annotations

from typing import Any, Dict, Mapping

from .spec_executability_eval import build_spec_issue_router_report


def route_spec_executability_issues(spec_executability_eval: Mapping[str, Any]) -> Dict[str, Any]:

    return build_spec_issue_router_report(spec_executability_eval)
