
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    """Configuration for the bundled text-feedback agent.

    String fields may use ``env/NAME`` to read the value from the environment.
    """

    type: str
    model_name: str
    llm_base_url: str
    llm_backend: Literal["http"] = "http"
    api_key: str = "EMPTY"
    max_steps: int = 50
    runtime_conf: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _resolve_env_vars(self) -> "AgentConfig":
        for field in ("api_key", "llm_base_url", "model_name"):
            val = getattr(self, field)
            if isinstance(val, str) and val.startswith("env/"):
                var_name = val[4:]
                resolved = os.environ.get(var_name, "")
                if not resolved:
                    if field == "api_key":
                        resolved = "EMPTY"
                    else:
                        logger.warning(
                            "Environment variable %r is not set "
                            "(referenced by agent.%s)",
                            var_name, field,
                        )
                setattr(self, field, resolved)
        return self
