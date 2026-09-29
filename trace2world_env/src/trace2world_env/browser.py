"""Playwright lifecycle: one Chromium process and one isolated context per episode.

Episodes never share a browser, so a renderer crash or leaked storage cannot cross episodes.
Opening a page retries once with a fresh browser when Chromium crashed during launch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from trace2world_env.page_runtime import FRAME_STRIP_JS, url_with_sid

logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_SECONDS = 2.0
_TASK_PAGE_COMMIT_TIMEOUT_SECONDS = 10.0
_LAUNCH_ATTEMPTS = 2
_BROWSER_CRASH_MARKERS = (
    "browser.new_context: target page, context or browser has been closed",
    "target page, context or browser has been closed",
    "target crashed",
    "page crashed",
)


def is_browser_crash(exc: BaseException) -> bool:
    """Whether ``exc`` reports a crashed or vanished Chromium."""
    message = str(exc).lower()
    return any(marker in message for marker in _BROWSER_CRASH_MARKERS)


@dataclass
class BrowserSession:
    """The browser, context, and page owned by one episode."""

    browser: Browser
    context: BrowserContext
    page: Page

    async def close(self) -> None:
        """Close the context and the browser, each bounded by a short timeout."""
        await _close(self.context, self.browser)


async def _close(context: BrowserContext | None, browser: Browser | None) -> None:
    for resource, label in ((context, "Context"), (browser, "Browser")):
        if resource is None:
            continue
        try:
            await asyncio.wait_for(resource.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("%s close timed out after %.1fs", label, _CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.info("%s already unavailable: %s", label, exc)


class BrowserLauncher:
    """Starts Playwright once and opens isolated episode pages."""

    def __init__(self, executable_path: str | None = None) -> None:
        self._executable_path = executable_path
        self._playwright: Playwright | None = None

    async def start(self) -> None:
        """Start the Playwright driver."""
        self._playwright = await async_playwright().start()

    async def stop(self) -> None:
        """Stop the Playwright driver (episode sessions must be closed first)."""
        if self._playwright is None:
            return
        try:
            await self._playwright.stop()
        except Exception as exc:
            logger.warning("Playwright stop during shutdown: %s", exc)
        self._playwright = None

    async def _launch(self) -> Browser:
        if self._playwright is None:
            raise RuntimeError("browser launcher is not started")
        options: dict[str, Any] = {"headless": True}
        if self._executable_path:
            options["executable_path"] = self._executable_path
        return await self._playwright.chromium.launch(**options)

    async def open_task_page(
        self,
        url: str,
        *,
        init_script: str,
        viewport: dict[str, int],
        device_scale_factor: float,
        sid: str | None,
        app_base_url: str,
    ) -> BrowserSession:
        """Launch a browser, install the frame-strip and launch-context scripts, and navigate
        to ``url`` (waiting for the commit only).

        When ``sid`` is set, every document request under ``app_base_url`` is redirected to
        carry ``?sid=`` so in-app navigation keeps the episode's state-kernel session.
        """
        for attempt in range(_LAUNCH_ATTEMPTS):
            browser: Browser | None = None
            context: BrowserContext | None = None
            try:
                browser = await self._launch()
                context = await browser.new_context(
                    viewport=viewport,
                    device_scale_factor=float(device_scale_factor),
                )
                await context.add_init_script(FRAME_STRIP_JS)
                await context.add_init_script(init_script)
                if sid:
                    await context.route(
                        f"{app_base_url}/**", _sid_preserving_route(sid, app_base_url)
                    )
                page = await context.new_page()
                await asyncio.wait_for(
                    page.goto(url, wait_until="commit"),
                    timeout=_TASK_PAGE_COMMIT_TIMEOUT_SECONDS,
                )
                return BrowserSession(browser, context, page)
            except Exception as exc:
                await _close(context, browser)
                if attempt + 1 < _LAUNCH_ATTEMPTS and is_browser_crash(exc):
                    logger.warning(
                        "Chromium crashed during task init; retrying with a fresh browser: %s",
                        exc,
                    )
                    continue
                raise
        raise AssertionError("unreachable")


def _sid_preserving_route(sid: str, app_base_url: str) -> Any:
    async def preserve_task_sid(route: Any, request: Any) -> None:
        if request.resource_type == "document":
            target_url = url_with_sid(request.url, sid, app_base_url)
            if target_url != request.url:
                # A redirect (not a rewrite) keeps the address bar and history on the sid URL.
                await route.fulfill(status=307, headers={"location": target_url}, body="")
                return
        await route.continue_()

    return preserve_task_sid
