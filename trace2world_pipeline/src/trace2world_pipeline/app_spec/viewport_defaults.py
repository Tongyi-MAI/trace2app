
from typing import Any, Dict

MOBILE_VIEWPORT_WIDTH: int = 390
MOBILE_VIEWPORT_HEIGHT: int = 844
MOBILE_ASPECT_RATIO: float = round(MOBILE_VIEWPORT_HEIGHT / MOBILE_VIEWPORT_WIDTH, 2)

MOBILE_VIEWPORT: Dict[str, int] = {
    "width": MOBILE_VIEWPORT_WIDTH,
    "height": MOBILE_VIEWPORT_HEIGHT,
}


def default_mobile_device_context() -> Dict[str, Any]:
    return {
        "device_class": "mobile",
        "viewport_width": MOBILE_VIEWPORT_WIDTH,
        "viewport_height": MOBILE_VIEWPORT_HEIGHT,
        "aspect_ratio": MOBILE_ASPECT_RATIO,
    }
