
import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

TASK_CONTEXT_PROTOCOL = "ghostweb.task_launch_context.v1"


def _url_with_sid(url: str, sid: str, app_base_url: str) -> str:
    if not sid:
        return url
    target = urlsplit(url)
    app = urlsplit(app_base_url)
    app_prefix = app.path.rstrip("/") + "/"
    if (target.scheme, target.netloc) != (app.scheme, app.netloc):
        return url
    if not target.path.startswith(app_prefix):
        return url
    query = dict(parse_qsl(target.query, keep_blank_values=True))
    if query.get("sid") == sid:
        return url
    query["sid"] = sid
    return urlunsplit(
        (target.scheme, target.netloc, target.path, urlencode(query), target.fragment)
    )


def _task_url(app_base_url: str, start_page: str, sid: str | None) -> str:

    base = app_base_url.rstrip("/")
    path = str(start_page or "").lstrip("/")
    query = urlencode({"sid": sid}) if sid else ""
    return f"{base}/{path}" + (f"?{query}" if query else "")


class DisplayIdRequiredError(KeyError):
    ...


class UnknownDisplayError(KeyError):

    def __init__(self, display_id: int) -> None:
        self.display_id = display_id
        super().__init__(f"Unknown display_id: {display_id}")


def _trace_context_script(
    task_id: str,
    *,
    start_filename: str = "",
    trainable: bool = True,
    response_mode: str = "env_text",
    actionability_preflight: bool = True,
    initial_state: dict | None = None,
    source_reference_task_id: str | None = None,
    sid: str | None = None,
) -> str:


    source_trace_id = source_reference_task_id or task_id
    context = {
        "task_context_protocol": TASK_CONTEXT_PROTOCOL,
        "task_id": task_id,
        "taskId": task_id,
        "source_reference_task_id": source_reference_task_id,
        "reference_trace_id": source_trace_id,
        "trace_id": source_trace_id,
        "trainable": bool(trainable),
        "source": "webenv_server",
        "start_filename": start_filename,
        "response_mode": response_mode,
        "actionability_preflight": bool(actionability_preflight),
        "initial_state": initial_state or {},
        "sid": sid,
    }
    context_json = json.dumps(context, ensure_ascii=False)
    return f"""
    (() => {{
      const context = {context_json};
      try {{
        const text = JSON.stringify(context);
        window.GhostwebActiveTraceId = context.trace_id;
        window.__ghostwebActiveTraceId = context.trace_id;
        window.__ghostwebExecutionContext = context;
        window.localStorage.setItem('ghostweb.execution_context', text);
        window.localStorage.setItem(
          'ghostweb.task_initial_state',
          JSON.stringify(context.initial_state || {{}})
        );
        window.localStorage.setItem('ghostweb.current_task_id', context.task_id);
        window.localStorage.setItem('ghostweb.trainable_trace_id', context.task_id);
        window.localStorage.setItem('ghostweb.response_mode', context.response_mode || 'env_text');
        window.localStorage.setItem(
          'ghostweb.actionability_preflight',
          String(!!context.actionability_preflight)
        );
        window.sessionStorage.setItem('ghostweb.execution_context', text);
        window.sessionStorage.setItem(
          'ghostweb.task_initial_state',
          JSON.stringify(context.initial_state || {{}})
        );
        window.sessionStorage.setItem('ghostweb.current_task_id', context.task_id);
        window.sessionStorage.setItem('ghostweb.trainable_trace_id', context.task_id);
        window.sessionStorage.setItem(
          'ghostweb.response_mode',
          context.response_mode || 'env_text'
        );
        window.sessionStorage.setItem(
          'ghostweb.actionability_preflight',
          String(!!context.actionability_preflight)
        );
        window.__GhostwebResponseMode = context.response_mode || 'env_text';
        window.__GhostwebActionabilityPreflight = !!context.actionability_preflight;
        window.GhostwebTaskInitialState = context.initial_state || {{}};
        window.__ghostwebTaskInitialState = context.initial_state || {{}};
        window.__ghostwebTaskContextBootstrapped = true;
      }} catch (e) {{
        console.error('Failed to initialize Ghostweb task context', e);
      }}
    }})()
    """


_FRAME_STRIP_JS = """
(() => {
    const style = document.createElement('style');
    style.textContent = `
        html {
            background: #fff !important;
            overflow: hidden !important;
            height: 100% !important;
        }
        body {
            max-width: none !important;
            margin: 0 !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            width: 100vw !important;
            height: 100vh !important;
            overflow: hidden !important;
        }
        body::before, body::after {
            display: none !important;
        }
    `;
    if (document.head) {
        document.head.appendChild(style);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            document.head.appendChild(style);
        });
    }
})();
"""


# Portfolio-wide device profile.  Every shipped environment declares this same
# profile in resource_manifest.browser_viewport; the fallback matches it so an
# environment that omits the declaration cannot silently change the screenshot
# size a policy sees across episodes.
_VIEWPORT_WIDTH = 430
_VIEWPORT_HEIGHT = 884
_DEVICE_SCALE_FACTOR = 2

_BROWSER_CRASH_MARKERS = (
    "browser.new_context: target page, context or browser has been closed",
    "target page, context or browser has been closed",
    "target crashed",
    "page crashed",
)


def _is_browser_crash_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _BROWSER_CRASH_MARKERS)


class ContextManager:

    def __init__(
        self,
        browser: Browser | None,
        app_base_url: str,
        *,
        entry_page: str = "homepage.html",
        response_mode: str = "env_text",
        actionability_preflight: bool = True,
        browser_factory: Callable[[], Awaitable[Browser]] | None = None,
        viewport_size: tuple[int, int] = (_VIEWPORT_WIDTH, _VIEWPORT_HEIGHT),
        device_scale_factor: float = _DEVICE_SCALE_FACTOR,
    ) -> None:
        if browser is None and browser_factory is None:
            raise ValueError("browser or browser_factory is required")
        width, height = viewport_size
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError("viewport_size must contain positive integer CSS pixels")
        if isinstance(device_scale_factor, bool) or float(device_scale_factor) <= 0:
            raise ValueError("device_scale_factor must be positive")
        self._browser = browser
        self._browser_factory = browser_factory
        self.app_base_url = app_base_url
        self.entry_page = entry_page
        self.response_mode = response_mode
        self.actionability_preflight = bool(actionability_preflight)
        self.viewport_size = {"width": width, "height": height}
        self.device_scale_factor = float(device_scale_factor)
        self._contexts: dict[int, BrowserContext] = {}
        self._pages: dict[int, Page] = {}


        self._owned_browsers: dict[int, Browser] = {}
        self._display_locks: dict[int, asyncio.Lock] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    async def _new_context(
        self,
        *,
        viewport_size: tuple[int, int] | None = None,
        device_scale_factor: float | None = None,
    ) -> tuple[Browser | None, BrowserContext]:
        owned_browser: Browser | None = None
        if self._browser_factory is not None:
            owned_browser = await self._browser_factory()
            browser = owned_browser
        else:
            assert self._browser is not None
            browser = self._browser
        try:
            viewport = self.viewport_size
            if viewport_size is not None:
                viewport = {"width": viewport_size[0], "height": viewport_size[1]}
            context = await browser.new_context(
                viewport=viewport,
                device_scale_factor=(
                    self.device_scale_factor
                    if device_scale_factor is None
                    else float(device_scale_factor)
                ),
            )
        except Exception:
            if owned_browser is not None:
                try:
                    await owned_browser.close()
                except Exception as close_exc:
                    logger.debug(
                        "[ContextManager] Browser close after context failure: %s",
                        close_exc,
                    )
            raise
        return owned_browser, context

    async def _close_resources(
        self,
        context: BrowserContext | None,
        browser: Browser | None,
        *,
        display_id: int,
    ) -> None:
        close_timeout_seconds = 2.0
        if context is not None:
            try:
                await asyncio.wait_for(
                    context.close(),
                    timeout=close_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "[ContextManager] Context close timed out display_id=%d "
                    "after %.1fs",
                    display_id,
                    close_timeout_seconds,
                )
            except Exception as exc:
                logger.info(
                    "[ContextManager] Context already unavailable display_id=%d: %s",
                    display_id,
                    exc,
                )
        if browser is not None:
            try:
                await asyncio.wait_for(
                    browser.close(),
                    timeout=close_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "[ContextManager] Browser close timed out display_id=%d "
                    "after %.1fs",
                    display_id,
                    close_timeout_seconds,
                )
            except Exception as exc:
                logger.info(
                    "[ContextManager] Browser already unavailable display_id=%d: %s",
                    display_id,
                    exc,
                )

    async def _initial_page(
        self,
        *,
        navigate_entry: bool,
    ) -> tuple[Browser | None, BrowserContext, Page]:
        attempts = 2 if self._browser_factory is not None else 1
        for attempt in range(attempts):
            owned_browser: Browser | None = None
            context: BrowserContext | None = None
            try:
                owned_browser, context = await self._new_context()
                await context.add_init_script(_FRAME_STRIP_JS)
                page = await context.new_page()
                if navigate_entry:
                    entry_url = f"{self.app_base_url}/{self.entry_page}"
                    await page.goto(entry_url, wait_until="load")
                return owned_browser, context, page
            except Exception as exc:
                await self._close_resources(context, owned_browser, display_id=0)
                if attempt + 1 < attempts and _is_browser_crash_error(exc):
                    logger.warning(
                        "[ContextManager] Chromium crashed during acquire; retrying "
                        "with a fresh browser: %s",
                        exc,
                    )
                    continue
                raise
        raise AssertionError("unreachable")

    async def acquire(self, *, navigate_entry: bool = True) -> int:
        with self._lock:
            display_id = self._next_id
            self._next_id += 1
            self._display_locks[display_id] = asyncio.Lock()


        if self._browser_factory is not None and not navigate_entry:
            logger.debug("[ContextManager] Reserved lazy display_id=%d", display_id)
            return display_id

        try:
            owned_browser, context, page = await self._initial_page(
                navigate_entry=navigate_entry,
            )
        except Exception:
            with self._lock:
                self._display_locks.pop(display_id, None)
            raise
        with self._lock:
            self._contexts[display_id] = context
            self._pages[display_id] = page
            if owned_browser is not None:
                self._owned_browsers[display_id] = owned_browser

        logger.debug("[ContextManager] Created context display_id=%d", display_id)
        return display_id

    async def release(self, display_id: int) -> None:
        with self._lock:
            display_lock = self._display_locks.get(display_id)
        if display_lock is None:
            return

        async with display_lock:
            with self._lock:
                context = self._contexts.pop(display_id, None)
                self._pages.pop(display_id, None)
                owned_browser = self._owned_browsers.pop(display_id, None)
            await self._close_resources(
                context,
                owned_browser,
                display_id=display_id,
            )
        with self._lock:
            if self._display_locks.get(display_id) is display_lock:
                self._display_locks.pop(display_id, None)
        logger.debug("[ContextManager] Released display_id=%d", display_id)

    async def deactivate(self, display_id: int) -> None:
        with self._lock:
            display_lock = self._display_locks.get(display_id)
        if display_lock is None:
            raise UnknownDisplayError(display_id)
        async with display_lock:
            with self._lock:
                context = self._contexts.pop(display_id, None)
                self._pages.pop(display_id, None)
                owned_browser = self._owned_browsers.pop(display_id, None)
            await self._close_resources(
                context,
                owned_browser,
                display_id=display_id,
            )
        logger.debug("[ContextManager] Deactivated display_id=%d", display_id)

    async def close_all(self) -> None:
        with self._lock:
            display_ids = list(self._display_locks)
        for display_id in display_ids:
            await self.release(display_id)

    async def _fresh_task_page(
        self,
        script: str,
        url: str,
        *,
        sid: str | None = None,
        app_base_url: str | None = None,
        viewport_size: tuple[int, int] | None = None,
        device_scale_factor: float | None = None,
    ) -> tuple[Browser | None, BrowserContext, Page]:
        attempts = 2 if self._browser_factory is not None else 1
        for attempt in range(attempts):
            owned_browser: Browser | None = None
            context: BrowserContext | None = None
            try:
                owned_browser, context = await self._new_context(
                    viewport_size=viewport_size,
                    device_scale_factor=device_scale_factor,
                )
                await context.add_init_script(_FRAME_STRIP_JS)
                await context.add_init_script(script)
                task_app_base_url = app_base_url or self.app_base_url
                if sid:
                    async def preserve_task_sid(route, request) -> None:
                        if request.resource_type == "document":
                            target_url = _url_with_sid(request.url, sid, task_app_base_url)
                            if target_url != request.url:


                                await route.fulfill(
                                    status=307,
                                    headers={"location": target_url},
                                    body="",
                                )
                            else:
                                await route.continue_()
                        else:
                            await route.continue_()

                    await context.route(f"{task_app_base_url}/**", preserve_task_sid)
                page = await context.new_page()


                await asyncio.wait_for(
                    page.goto(url, wait_until="commit"),
                    timeout=10.0,
                )
                return owned_browser, context, page
            except Exception as exc:
                await self._close_resources(context, owned_browser, display_id=0)
                if attempt + 1 < attempts and _is_browser_crash_error(exc):
                    logger.warning(
                        "[ContextManager] Chromium crashed during task init; "
                        "retrying with a fresh browser: %s",
                        exc,
                    )
                    continue
                raise
        raise AssertionError("unreachable")

    @property
    def active_display_count(self) -> int:
        with self._lock:
            return len(self._display_locks)

    @property
    def active_browser_count(self) -> int:
        with self._lock:
            return len(self._owned_browsers) + int(
                self._browser is not None and bool(self._pages)
            )

    def has_display(self, display_id: int) -> bool:
        with self._lock:
            return display_id in self._display_locks

    def get_page(self, display_id: int | None) -> Page:
        if display_id is None:
            with self._lock:


                if len(self._pages) == 1:
                    return next(iter(self._pages.values()))
            raise DisplayIdRequiredError("display_id is required")
        with self._lock:
            try:
                return self._pages[display_id]
            except KeyError:
                raise UnknownDisplayError(display_id) from None

    def resolve_display_id(self, display_id: int | None) -> int:
        if display_id is not None:
            if self.has_display(display_id):
                return display_id
            raise UnknownDisplayError(display_id)
        with self._lock:
            if len(self._display_locks) == 1:
                return next(iter(self._display_locks))
        raise DisplayIdRequiredError("display_id is required")

    async def open_task(
        self,
        display_id: int | None,
        start_page: str,
        task_id: str,
        *,
        trainable: bool = True,
        initial_state: dict | None = None,
        source_reference_task_id: str | None = None,
        sid: str | None = None,
        app_base_url: str | None = None,
        viewport_size: tuple[int, int] | None = None,
        device_scale_factor: float | None = None,
    ) -> None:
        resolved_display_id = self.resolve_display_id(display_id)
        with self._lock:
            display_lock = self._display_locks[resolved_display_id]
        script = _trace_context_script(
            task_id,
            start_filename=start_page,
            trainable=trainable,
            response_mode=self.response_mode,
            actionability_preflight=self.actionability_preflight,
            initial_state=initial_state,
            source_reference_task_id=source_reference_task_id,
            sid=sid,
        )
        task_app_base_url = app_base_url or self.app_base_url
        url = _task_url(task_app_base_url, start_page, sid)
        async with display_lock:
            with self._lock:
                old_context = self._contexts.get(resolved_display_id)
                old_browser = self._owned_browsers.get(resolved_display_id)


            if self._browser_factory is not None:


                with self._lock:
                    self._contexts.pop(resolved_display_id, None)
                    self._pages.pop(resolved_display_id, None)
                    self._owned_browsers.pop(resolved_display_id, None)
                await self._close_resources(
                    old_context,
                    old_browser,
                    display_id=resolved_display_id,
                )
                old_context = None
                old_browser = None

            new_browser, new_context, new_page = await self._fresh_task_page(
                script,
                url,
                sid=sid,
                app_base_url=task_app_base_url,
                viewport_size=viewport_size,
                device_scale_factor=device_scale_factor,
            )
            with self._lock:
                self._contexts[resolved_display_id] = new_context
                self._pages[resolved_display_id] = new_page
                if new_browser is not None:
                    self._owned_browsers[resolved_display_id] = new_browser
                else:
                    self._owned_browsers.pop(resolved_display_id, None)

            if old_context is not None:
                await self._close_resources(
                    old_context,
                    old_browser,
                    display_id=resolved_display_id,
                )
        logger.debug(
            "[ContextManager] open_task display_id=%s task=%s → %s",
            resolved_display_id, task_id, start_page,
        )

    async def update_task_context_step(
        self,
        display_id: int | None,
        step_index: int | None,
    ) -> None:
        resolved_display_id = self.resolve_display_id(display_id)
        with self._lock:
            display_lock = self._display_locks[resolved_display_id]
        async with display_lock:
            page = self.get_page(resolved_display_id)
            update_script = """(nextStepIndex) => {
                  let context = window.__ghostwebExecutionContext;
                  if (!context || typeof context !== 'object') {
                    try {
                      context = JSON.parse(
                        window.sessionStorage.getItem('ghostweb.execution_context') ||
                        window.localStorage.getItem('ghostweb.execution_context') ||
                        '{}'
                      );
                    } catch (error) {
                      context = {};
                    }
                  }
                  context = Object.assign({}, context);
                  const traceId = String(
                    context.reference_trace_id || context.trace_id || context.task_id || ''
                  );
                  if (nextStepIndex === null) {
                    delete context.step_index;
                    delete context.raw_step_index;
                    delete context.action_id;
                  } else {
                    context.step_index = nextStepIndex;
                    context.raw_step_index = nextStepIndex;
                    context.action_id = traceId ? traceId + ':step' + nextStepIndex : '';
                  }
                  const serialized = JSON.stringify(context);
                  window.__ghostwebExecutionContext = context;
                  window.GhostwebActiveTraceId = traceId;
                  window.__ghostwebActiveTraceId = window.GhostwebActiveTraceId;
                  try { window.localStorage.setItem('ghostweb.execution_context', serialized); } catch (error) {}
                  try { window.sessionStorage.setItem('ghostweb.execution_context', serialized); } catch (error) {}
                  window.dispatchEvent(new CustomEvent('ghostweb:task-context-update', {
                    detail: {
                      step_index: nextStepIndex,
                      action_id: context.action_id || '',
                      context
                    }
                  }));
                }"""


            for attempt in range(5):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except Exception:

                    pass
                try:
                    await page.evaluate(update_script, step_index)
                    return
                except Exception as exc:
                    if "Execution context was destroyed" not in str(exc) or attempt == 4:
                        raise
                    await asyncio.sleep(0.05)
