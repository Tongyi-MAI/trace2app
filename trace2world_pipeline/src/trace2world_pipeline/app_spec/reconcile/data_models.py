
from dataclasses import dataclass
from typing import Dict, Any, List


@dataclass
class FrameJob:
    trace_id: str
    task: str
    website_url: str
    step_index: int
    action: str
    thought: str
    screenshot_path: str
    previous_action: str = ""
    observation_role: str = "executable_step"
    action_disposition: str = "execute"
    observation_disposition: str = "retain"
    semantic_effect: str = ""

    @property
    def job_key(self) -> str:
        return f"{self.trace_id}:{self.step_index}"

    @property
    def after_screenshot(self) -> str:
        return self.screenshot_path

    @property
    def before_screenshot(self) -> str:
        return self.screenshot_path
