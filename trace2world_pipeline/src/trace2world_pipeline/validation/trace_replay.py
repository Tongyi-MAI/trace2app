
import asyncio
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from trace2world_pipeline.core.llm_caller import (
    call_openai_api_with_tools_async,
    call_openai_api_json_async,
    call_openai_api_async,
    encode_image,
    get_next_endpoint,
    get_async_client_for_endpoint,
)
from trace2world_pipeline.app_spec.trace_loader import TraceData, TraceStep
from trace2world_pipeline.app_spec.viewport_defaults import (
    MOBILE_VIEWPORT,
    MOBILE_VIEWPORT_HEIGHT,
    MOBILE_VIEWPORT_WIDTH,
)
from trace2world_pipeline.app_spec.environment_profile import (
    load_environment_profile_from_result_dir,
    resolve_environment_profile,
)
from trace2world_pipeline.app_spec.environment_prompt_policy import build_environment_prompt_policy

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    step_index: int
    action: str
    page: str
    action_success: bool
    action_error: Optional[str] = None
    generated_screenshot: Optional[str] = None
    page_url: Optional[str] = None


@dataclass
class TraceReplayResult:
    trace_id: str
    total_steps: int
    completed_steps: int
    step_results: List[StepResult] = field(default_factory=list)
    blocking_step: Optional[int] = None


@dataclass
class VisualStepComparison:
    step_index: int
    reference_screenshot: str
    generated_screenshot: Optional[str] = None
    visual_score: float = 0.0
    differences: List[str] = field(default_factory=list)
    missing_elements: List[str] = field(default_factory=list)
    layout_issues: List[str] = field(default_factory=list)


@dataclass
class VisualGapReport:
    trace_id: str
    step_comparisons: List[VisualStepComparison] = field(default_factory=list)
    avg_visual_score: float = 0.0


@dataclass
class GapReport:
    iteration: int
    functional_results: List[TraceReplayResult] = field(default_factory=list)
    visual_reports: List[VisualGapReport] = field(default_factory=list)
    overall_functional_score: float = 0.0
    overall_visual_score: float = 0.0


@dataclass
class OptimizationResult:
    iterations_run: int = 0
    initial_functional_score: float = 0.0
    final_functional_score: float = 0.0
    final_visual_score: float = 0.0
    gap_reports: List[GapReport] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    patches_applied: int = 0


BASH_MAX_OUTPUT_LINES = 200
BASH_MAX_OUTPUT_BYTES = 40_000


_PY_PLAYWRIGHT_SESSIONS: Dict[str, "_PythonPlaywrightSession"] = {}
_PY_PLAYWRIGHT_EXECUTION_CONTEXTS: Dict[str, Dict[str, Any]] = {}


def _playwright_import_available() -> Tuple[bool, str]:
    try:
        from playwright.async_api import async_playwright
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _register_replay_execution_context(session_id: str, payload: Dict[str, Any]) -> None:
    _PY_PLAYWRIGHT_EXECUTION_CONTEXTS[session_id] = dict(payload or {})


def _drop_replay_execution_context(session_id: str) -> None:
    _PY_PLAYWRIGHT_EXECUTION_CONTEXTS.pop(session_id, None)


def _format_browser_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _resolve_file_url(working_dir: str, target: str) -> str:
    target = str(target or "").strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
        return target
    root = Path(working_dir)
    phase_dir = root / "phase_pagegen" / "pages"
    if phase_dir.is_dir():
        return (phase_dir / target).resolve().as_uri()
    direct = root / target
    for candidate in (
        direct,
        root / "frontend" / "pages" / target,
    ):
        if candidate.is_file():
            return candidate.resolve().as_uri()
    return direct.resolve().as_uri()


def _generated_page_exists(website_dir: str, filename: str) -> bool:
    filename = str(filename or "").strip()
    if not filename:
        return False
    root = Path(website_dir)
    phase_dir = root / "phase_pagegen" / "pages"
    if phase_dir.is_dir():
        return (phase_dir / filename).is_file()
    return any(
        (base / filename).is_file()
        for base in (
            root,
            root / "frontend" / "pages",
        )
    )


def _generated_page_path(website_dir: str, filename: str) -> Path:
    filename = str(filename or "").strip()
    root = Path(website_dir)
    phase_dir = root / "phase_pagegen" / "pages"
    if phase_dir.is_dir():
        return phase_dir / filename
    direct = root / filename
    for candidate in (
        direct,
        root / "frontend" / "pages" / filename,
    ):
        if candidate.is_file():
            return candidate
    return direct


def _extract_playwright_cli_command(command: str) -> Optional[Tuple[str, List[str]]]:
    try:
        parts = shlex.split(str(command or ""))
    except ValueError:
        return None
    if not parts or parts[0] != "playwright-cli":
        return None

    session_id = ""
    cmd_args: List[str] = []
    skip_next = False
    for idx, part in enumerate(parts[1:]):
        if skip_next:
            skip_next = False
            continue
        if part.startswith("-s="):
            session_id = part.split("=", 1)[1]
            continue
        if part == "-s":
            next_pos = idx + 2
            if next_pos < len(parts):
                session_id = parts[next_pos]
                skip_next = True
            continue
        cmd_args.append(part)

    if not session_id or not cmd_args:
        return None
    return session_id, cmd_args


def _extract_page_evaluate_argument(code: str) -> str:
    marker = "page.evaluate("
    start = code.find(marker)
    if start < 0:
        return ""
    pos = start + len(marker)
    depth = 1
    quote = ""
    escape = False
    chars: List[str] = []
    for ch in code[pos:]:
        if escape:
            chars.append(ch)
            escape = False
            continue
        if ch == "\\":
            chars.append(ch)
            escape = True
            continue
        if quote:
            chars.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            chars.append(ch)
            quote = ch
            continue
        if ch == "(":
            depth += 1
            chars.append(ch)
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(chars).strip()
            chars.append(ch)
            continue
        chars.append(ch)
    return ""


class _PythonPlaywrightSession:
    def __init__(self, session_id: str, working_dir: str):
        self.session_id = session_id
        self.working_dir = working_dir
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.ref_selectors: Dict[str, str] = {}

    async def ensure_started(self) -> None:
        if self.page is not None:
            return
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        execution_context = _PY_PLAYWRIGHT_EXECUTION_CONTEXTS.get(self.session_id) or {}
        environment_profile = resolve_environment_profile(
            execution_context.get("environment_profile")
        )
        self.context = await self.browser.new_context(
            viewport=dict(environment_profile["browser_viewport"]),
            is_mobile=environment_profile["platform"] == "mobile",
            device_scale_factor=1,
        )
        if execution_context:
            payload = json.dumps(execution_context, ensure_ascii=False).replace("</", "<\\/")
            value = json.dumps(payload, ensure_ascii=False)
            await self.context.add_init_script(
                script=(
                    "(() => { try { "
                    f"window.__ghostwebExecutionContext = {payload}; "
                    f"localStorage.setItem('ghostweb.execution_context', {value}); "
                    "} catch (e) {} })();"
                )
            )
        self.page = await self.context.new_page()

    async def close(self) -> str:
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        finally:
            self.playwright = None
            self.browser = None
            self.context = None
            self.page = None
            self.ref_selectors = {}
        return "closed"

    async def run(self, args: List[str]) -> str:
        if not args:
            return "No browser command provided"
        command = args[0]
        if command == "close":
            return await self.close()
        await self.ensure_started()

        if command in {"open", "goto"}:
            if len(args) < 2:
                return f"{command} requires a URL"
            url = _resolve_file_url(self.working_dir, args[1])
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._settle()
            return self.page.url

        if command == "snapshot":
            return await self.snapshot()
        if command == "click":
            return await self.click(args[1] if len(args) > 1 else "")
        if command == "tap":
            return await self.tap(args[1:])
        if command == "fill":
            if len(args) < 3:
                return "fill requires <ref> and <text>"
            return await self.fill(args[1], args[2])
        if command in {"type-text", "input-text"}:
            if len(args) < 2:
                return f"{command} requires <text>"
            intent = ""
            text_parts: List[str] = []
            idx = 1
            while idx < len(args):
                if args[idx] == "--intent" and idx + 1 < len(args):
                    intent = args[idx + 1]
                    idx += 2
                    continue
                text_parts.append(args[idx])
                idx += 1
            return await self.type_text(" ".join(text_parts), intent=intent)
        if command == "press":
            if len(args) < 2:
                return "press requires <key>"
            await self.page.keyboard.press(args[1])
            await self._settle()
            return f"pressed {args[1]}"
        if command == "scroll":
            return await self.scroll(args[1:])
        if command == "swipe":
            return await self.swipe(args[1:])
        if command == "run-code":
            if len(args) < 2:
                return "run-code requires JavaScript"
            return await self.run_code(args[1])
        if command == "screenshot":
            filename = self._screenshot_filename(args[1:])
            if not filename:
                return "screenshot requires --filename"
            path = Path(filename)
            if not path.is_absolute():
                path = Path(self.working_dir, filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(path), full_page=False)
            return str(path)

        return f"Unsupported browser command: {command}"

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except Exception:
            pass
        try:
            await self.page.wait_for_timeout(150)
        except Exception:
            pass

    @staticmethod
    def _screenshot_filename(args: List[str]) -> str:
        for idx, arg in enumerate(args):
            if arg.startswith("--filename="):
                return arg.split("=", 1)[1]
            if arg == "--filename" and idx + 1 < len(args):
                return args[idx + 1]
        return ""

    async def snapshot(self) -> str:
        script = r"""
        () => {
          const selectors = 'a,button,input,textarea,select,summary,label,[role="button"],[role="link"],[onclick],[tabindex]';
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
              rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0 &&
              rect.top <= window.innerHeight + 400 && rect.left <= window.innerWidth + 400;
          };
          const cssPath = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            const parts = [];
            let node = el;
            while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
              const tag = node.tagName.toLowerCase();
              let nth = 1;
              let sib = node;
              while ((sib = sib.previousElementSibling)) {
                if (sib.tagName.toLowerCase() === tag) nth += 1;
              }
              parts.unshift(`${tag}:nth-of-type(${nth})`);
              node = node.parentElement;
            }
            return 'body > ' + parts.join(' > ');
          };
          const label = (el) => {
            const parts = [
              el.getAttribute('aria-label'),
              el.getAttribute('title'),
              el.getAttribute('alt'),
              el.getAttribute('placeholder'),
              el.value,
              el.innerText,
              el.textContent,
            ].filter(Boolean).map(v => String(v).replace(/\s+/g, ' ').trim()).filter(Boolean);
            return (parts[0] || '').slice(0, 140);
          };
          const explicit = Array.from(document.querySelectorAll(selectors));
          const pointer = Array.from(document.querySelectorAll('*')).filter(el => {
            try { return window.getComputedStyle(el).cursor === 'pointer'; } catch (e) { return false; }
          });
          const seen = new Set();
          const elements = [];
          for (const el of explicit.concat(pointer)) {
            if (!el || seen.has(el) || !visible(el)) continue;
            seen.add(el);
            const text = label(el);
            if (!text && !['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) continue;
            const rect = el.getBoundingClientRect();
            elements.push({
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || '',
              type: el.getAttribute('type') || '',
              text,
              href: el.getAttribute('href') || '',
              selector: cssPath(el),
              rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
            });
            if (elements.length >= 250) break;
          }
          return {
            url: window.location.href,
            title: document.title || '',
            text: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1600),
            elements
          };
        }
        """
        data = await self.page.evaluate(script)
        self.ref_selectors = {}
        lines = [
            f"URL: {data.get('url', '')}",
            f"Title: {data.get('title', '')}",
            "Visible text:",
            data.get("text", ""),
            "Elements:",
        ]
        for idx, item in enumerate(data.get("elements", []), start=1):
            ref = f"e{idx}"
            self.ref_selectors[ref] = item.get("selector", "")
            label = item.get("text") or item.get("href") or item.get("selector") or ""
            meta = item.get("role") or item.get("type") or ""
            rect = item.get("rect") or {}
            lines.append(
                f"- {ref}: {item.get('tag', '')}"
                f"{('/' + meta) if meta else ''} \"{label}\" "
                f"at ({rect.get('x')},{rect.get('y')},{rect.get('w')}x{rect.get('h')})"
            )
        return "\n".join(lines)

    async def _locator_for_ref(self, ref: str):
        ref = str(ref or "").strip()
        selector = self.ref_selectors.get(ref)
        if not selector:
            return None, f"Unknown ref {ref}. Run snapshot first."
        locator = self.page.locator(selector).first
        return locator, ""

    async def click(self, ref: str) -> str:
        locator, error = await self._locator_for_ref(ref)
        if error:
            return error
        try:
            await locator.scroll_into_view_if_needed(timeout=3_000)
            await locator.click(timeout=5_000)
            await self._settle()
            return f"clicked {ref}"
        except Exception as exc:
            return f"click {ref} failed: {exc}"

    async def tap(self, args: List[str]) -> str:
        if len(args) < 2:
            return "tap requires <x> <y>"
        try:
            x = float(args[0])
            y = float(args[1])
            await self.page.mouse.click(x, y)
            await self._settle()
            return f"tapped {int(round(x))} {int(round(y))}"
        except Exception as exc:
            return f"tap failed: {exc}"

    async def fill(self, ref: str, text: str) -> str:
        locator, error = await self._locator_for_ref(ref)
        if error:
            return error
        try:
            await locator.fill(str(text), timeout=5_000)
            await self._settle()
            return f"filled {ref}"
        except Exception as exc:
            return f"fill {ref} failed: {exc}"

    async def type_text(self, text: str, intent: str = "") -> str:
        try:
            applied = await self.page.evaluate(
                """payload => {
                  const text = String((payload && payload.text) || '');
                  const intent = String((payload && payload.intent) || '');
                  try {
                    if (typeof window.__ghostwebEnsureObligationInputSurface === 'function') {
                      window.__ghostwebEnsureObligationInputSurface();
                    }
                  } catch (error) {}
                  const visible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                      rect.width > 0 && rect.height > 0;
                  };
                  const writable = (node) => {
                    if (!node || !visible(node)) return false;
                    const tag = (node.tagName || '').toUpperCase();
                    return tag === 'INPUT' || tag === 'TEXTAREA' || !!node.isContentEditable;
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, '').toLowerCase();
                  const labelFor = (node) => {
                    const parts = [];
                    const id = node.getAttribute && node.getAttribute('id');
                    if (id) {
                      const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                      if (label) parts.push(label.innerText || label.textContent || '');
                    }
                    const explicit = node.closest && node.closest('label');
                    if (explicit) parts.push(explicit.innerText || explicit.textContent || '');
                    parts.push(node.getAttribute && node.getAttribute('aria-label'));
                    parts.push(node.getAttribute && node.getAttribute('placeholder'));
                    parts.push(node.getAttribute && node.getAttribute('name'));
                    parts.push(node.getAttribute && node.getAttribute('data-element-id'));
                    parts.push(node.getAttribute && node.getAttribute('data-field'));
                    const row = node.closest && node.closest('li, label, .form-row, .field-row, [data-field-row], [role="group"]');
                    if (row) parts.push(row.innerText || row.textContent || '');
                    return parts.filter(Boolean).join(' ');
                  };
                  const fieldScore = (node) => {
                    const intentText = normalize(intent);
                    const labelText = normalize(labelFor(node));
                    let score = 0;
                    if (!intentText || !labelText) return score;
                    if (intentText.includes(labelText)) score += 120;
                    if (labelText.includes(intentText)) score += 80;
                    const rawLabels = labelFor(node).split(/[\\s:：,，/|()（）\\[\\]【】]+/).map(normalize).filter(Boolean);
                    for (const token of rawLabels) {
                      if (token.length >= 2 && intentText.includes(token)) {
                        score += Math.min(40, token.length * 4);
                      }
                    }
                    return score;
                  };
                  const active = document.activeElement;
                  const candidates = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]'))
                    .filter(writable);
                  let el = null;
                  let bestScore = -1;
                  for (const candidate of candidates) {
                    let score = fieldScore(candidate);
                    if (active && active === candidate) score += 5;
                    if (score > bestScore) {
                      bestScore = score;
                      el = candidate;
                    }
                  }
                  if ((!el || bestScore <= 0) && active && active !== document.body && writable(active)) {
                    el = active;
                  }
                  if (!el) {
                    el = candidates[0];
                  }
                  if (!el) return false;
                  const tag = (el.tagName || '').toUpperCase();
                  if (tag === 'INPUT' || tag === 'TEXTAREA') {
                    el.focus();
                    el.value = text;
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    try {
                      if (typeof window.__ghostwebNoteTypedInput === 'function') {
                        window.__ghostwebNoteTypedInput(text);
                      }
                    } catch (error) {}
                    return true;
                  }
                  if (el.isContentEditable) {
                    el.focus();
                    el.textContent = text;
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
                    try {
                      if (typeof window.__ghostwebNoteTypedInput === 'function') {
                        window.__ghostwebNoteTypedInput(text);
                      }
                    } catch (error) {}
                    return true;
                  }
                  return false;
                }""",
                {"text": str(text), "intent": str(intent or "")},
            )
            await self._settle()
            if applied:
                return f"typed text {str(text)[:60]}"
            return "type-text failed: no active text input"
        except Exception as exc:
            return f"type-text failed: {exc}"

    async def _scroll_by(self, dx: float, dy: float, origin: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
        return await self.page.evaluate(
            """payload => {
              const dx = Number(payload && payload.dx) || 0;
              const dy = Number(payload && payload.dy) || 0;
              const origin = payload && payload.origin;
              const axis = Math.abs(dy) >= Math.abs(dx) ? 'y' : 'x';
              const visible = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                  rect.width > 0 && rect.height > 0;
              };
              const canScroll = (el) => {
                if (!el) return false;
                if (el === document.scrollingElement || el === document.documentElement || el === document.body) {
                  return axis === 'y'
                    ? document.scrollingElement.scrollHeight > document.scrollingElement.clientHeight
                    : document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth;
                }
                if (!visible(el)) return false;
                return axis === 'y'
                  ? el.scrollHeight > el.clientHeight + 1
                  : el.scrollWidth > el.clientWidth + 1;
              };
              const ancestors = [];
              if (origin && Number.isFinite(origin.x) && Number.isFinite(origin.y)) {
                let node = document.elementFromPoint(origin.x, origin.y);
                while (node && node.nodeType === 1) {
                  ancestors.push(node);
                  node = node.parentElement;
                }
              }
              const preferred = [
                '[data-ghostweb-scroll-container]',
                '.app-shell main',
                '.app-shell [data-scroll-container]',
                'main',
                '[role="main"]',
                '.scroll-container',
                '.overflow-y-auto',
                '.overflow-auto',
              ];
              const candidates = ancestors.concat(
                preferred.flatMap((selector) => Array.from(document.querySelectorAll(selector))),
                Array.from(document.querySelectorAll('*')).filter((el) => {
                  try {
                    const style = window.getComputedStyle(el);
                    return /(auto|scroll)/.test(axis === 'y' ? style.overflowY : style.overflowX);
                  } catch (error) {
                    return false;
                  }
                }),
                [document.scrollingElement || document.documentElement]
              );
              const seen = new Set();
              const target = candidates.find((el) => {
                if (!el || seen.has(el)) return false;
                seen.add(el);
                return canScroll(el);
              }) || (document.scrollingElement || document.documentElement);
              const before = {
                x: target === document.scrollingElement ? window.scrollX : target.scrollLeft,
                y: target === document.scrollingElement ? window.scrollY : target.scrollTop,
              };
              if (target === document.scrollingElement || target === document.documentElement || target === document.body) {
                window.scrollBy(dx, dy);
              } else if (typeof target.scrollBy === 'function') {
                target.scrollBy({ left: dx, top: dy, behavior: 'auto' });
              } else {
                target.scrollLeft += dx;
                target.scrollTop += dy;
              }
              const after = {
                x: target === document.scrollingElement ? window.scrollX : target.scrollLeft,
                y: target === document.scrollingElement ? window.scrollY : target.scrollTop,
              };
              return {
                tag: (target.tagName || '').toLowerCase(),
                id: target.id || '',
                className: String(target.className || ''),
                before,
                after
              };
            }""",
            {
                "dx": dx,
                "dy": dy,
                **({"origin": {"x": origin[0], "y": origin[1]}} if origin else {}),
            },
        )

    @staticmethod
    def _split_intent_args(args: List[str]) -> Tuple[str, List[str]]:
        intent = ""
        rest: List[str] = []
        idx = 0
        while idx < len(args):
            if args[idx] == "--intent" and idx + 1 < len(args):
                intent = args[idx + 1]
                idx += 2
                continue
            rest.append(args[idx])
            idx += 1
        return intent, rest

    async def _align_scroll_to_intent(self, intent: str) -> bool:
        if not str(intent or "").strip():
            return False
        return bool(await self.page.evaluate(
            """intent => {
              const rawIntent = String(intent || '');
              if (!rawIntent || /加载更多/.test(rawIntent)) return false;
              const normalize = (value) => String(value || '').replace(/\\s+/g, '').toLowerCase();
              const intentText = normalize(rawIntent);
              const visibleEnough = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                  rect.width > 0 && rect.height > 0;
              };
              const scrollParent = (el) => {
                let node = el && el.parentElement;
                while (node && node !== document.body) {
                  if (node.scrollHeight > node.clientHeight + 1) return node;
                  node = node.parentElement;
                }
                return document.querySelector('.app-shell main') || document.scrollingElement || document.documentElement;
              };
              const explicit = [];
              rawIntent.replace(/[“"']([^”"']{2,16})[”"']/g, (_m, value) => {
                const token = normalize(value);
                if (token) explicit.push(token);
                return _m;
              });
              const generic = new Set(['搜索', '首页', '外卖', '页面', '按钮', '更多', '进入', '筛选']);
              const candidates = Array.from(document.querySelectorAll('h1,h2,h3,h4,button,a,span,div,p,section'))
                .map((el) => {
                  if (!visibleEnough(el)) return null;
                  const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                  if (!text || text.length < 2 || text.length > 16 || generic.has(text)) return null;
                  let score = 0;
                  if (explicit.some((token) => token === text || token.includes(text) || text.includes(token))) {
                    score += 200 + text.length;
                  }
                  if (intentText.includes(text)) score += 40 + text.length;
                  if (!score) return null;
                  const rect = el.getBoundingClientRect();
                  score += Math.max(0, Math.min(30, rect.top / 20));
                  return { el, score, top: rect.top };
                })
                .filter(Boolean)
                .sort((a, b) => b.score - a.score);
              if (!candidates.length) return false;
              const target = candidates[0].el;
              const parent = scrollParent(target);
              const before = target.getBoundingClientRect();
              if (before.top >= 96 && before.top <= 220) return false;
              const delta = before.top - 128;
              if (parent === document.scrollingElement || parent === document.documentElement || parent === document.body) {
                window.scrollBy(0, delta);
              } else if (typeof parent.scrollBy === 'function') {
                parent.scrollBy({ top: delta, left: 0, behavior: 'auto' });
              } else {
                parent.scrollTop += delta;
              }
              return true;
            }""",
            str(intent or ""),
        ))

    async def scroll(self, args: List[str]) -> str:
        intent, args = self._split_intent_args(args)
        direction = str(args[0] if args else "down").lower()
        amount = 620
        if len(args) > 1:
            try:
                amount = abs(int(float(args[1])))
            except (TypeError, ValueError):
                amount = 620
        elif direction.lstrip("-").isdigit():
            amount = abs(int(direction))
            direction = "down" if int(direction) >= 0 else "up"

        dx = 0
        dy = 0
        if direction in {"up", "top"}:
            dy = -amount
        elif direction in {"left"}:
            dx = -amount
        elif direction in {"right"}:
            dx = amount
        else:
            dy = amount

        details = await self._scroll_by(dx, dy)
        anchored = await self._align_scroll_to_intent(intent)
        await self._settle()
        after = details.get("after", {}) if isinstance(details, dict) else {}
        return f"scrolled {direction} {amount} to {int(after.get('x', 0))},{int(after.get('y', 0))}{' anchored' if anchored else ''}"

    async def swipe(self, args: List[str]) -> str:
        intent, args = self._split_intent_args(args)
        if len(args) >= 4:
            try:
                sx, sy, ex, ey = [float(item) for item in args[:4]]
                dx = sx - ex
                dy = sy - ey
                details = await self._scroll_by(dx, dy, origin=(sx, sy))
                anchored = await self._align_scroll_to_intent(intent)
                await self._settle()
                after = details.get("after", {}) if isinstance(details, dict) else {}
                return f"swiped {int(sx)},{int(sy)}->{int(ex)},{int(ey)} scrolled to {int(after.get('x', 0))},{int(after.get('y', 0))}{' anchored' if anchored else ''}"
            except (TypeError, ValueError):
                pass

        direction = str(args[0] if args else "up").lower()
        if direction in {"up", "down"}:
            scroll_direction = "down" if direction == "up" else "up"
        elif direction in {"left", "right"}:
            scroll_direction = direction
        else:
            scroll_direction = "down"
        scroll_args = ["--intent", intent, scroll_direction, args[1] if len(args) > 1 else "620"] if intent else [scroll_direction, args[1] if len(args) > 1 else "620"]
        return await self.scroll(scroll_args)

    async def run_code(self, code: str) -> str:
        code = str(code or "").strip()
        try:
            if "setViewportSize" in code:
                width_match = re.search(r"width\s*:\s*(\d+)", code)
                height_match = re.search(r"height\s*:\s*(\d+)", code)
                await self.page.set_viewport_size({
                    "width": int(width_match.group(1)) if width_match else MOBILE_VIEWPORT_WIDTH,
                    "height": int(height_match.group(1)) if height_match else MOBILE_VIEWPORT_HEIGHT,
                })
                return "viewport set"
            if "page.url()" in code:
                return self.page.url

            evaluate_arg = _extract_page_evaluate_argument(code)
            if evaluate_arg:
                return _format_browser_result(await self.page.evaluate(evaluate_arg))

            return _format_browser_result(await self.page.evaluate(code))
        except Exception as exc:
            return f"run-code failed: {exc}"


async def _run_python_playwright_cli(
    session_id: str,
    args: List[str],
    working_dir: str,
) -> str:
    session = _PY_PLAYWRIGHT_SESSIONS.get(session_id)
    if session is None:
        session = _PythonPlaywrightSession(session_id, working_dir)
        _PY_PLAYWRIGHT_SESSIONS[session_id] = session
    try:
        return await session.run(args)
    finally:
        if args and args[0] == "close":
            _PY_PLAYWRIGHT_SESSIONS.pop(session_id, None)


def _resolve_replay_start_filename(website_dir: str, trace: TraceData) -> Tuple[str, str]:
    candidates: List[Tuple[str, str]] = [
        ("meituan.html", "app_home_fallback"),
        ("home.html", "home_fallback"),
        ("index.html", "legacy_index_fallback"),
    ]

    seen = set()
    for candidate, reason in candidates:
        filename = str(candidate or "").strip()
        if not filename or filename in seen:
            continue
        seen.add(filename)
        if _generated_page_exists(website_dir, filename):
            return filename, reason

    return "index.html", "missing_legacy_index_fallback"


def _resolve_replay_start_filename_from_observation_bindings(
    trace: TraceData,
    obs_to_filename: Dict[str, str],
    *,
    external_system_observation_keys: Optional[set[str]] = None,
) -> Tuple[str, str]:
    external_keys = external_system_observation_keys or set()
    for index, step in enumerate(trace.steps):
        obs_key = _trace_observation_key(trace, step)
        if obs_key in external_keys:
            continue
        operation, _metadata = _trace_step_operation(step)
        if index == 0 and (operation == "open_app" or _looks_like_initial_app_open(step)):
            continue
        filename = str(obs_to_filename.get(obs_key) or "").strip()
        if filename:
            return filename, "first_executable_materialized_observation"
        if _operation_preserves_page_identity(operation) and index + 1 < len(trace.steps):
            post_obs_key = _trace_observation_key(trace, trace.steps[index + 1])
            post_filename = str(obs_to_filename.get(post_obs_key) or "").strip()
            if post_filename:
                return post_filename, "first_page_preserving_post_observation"
        return "", "missing_first_executable_source_binding"
    return "", "missing_executable_trace_step"


def _resolve_replay_start_filename_from_site_model(
    site_model: Any,
    trace: TraceData,
    *,
    external_system_observation_keys: Optional[set[str]] = None,
) -> Tuple[str, str]:
    obs_to_filename = _build_obs_to_filename_from_site_model(
        site_model,
    )
    return _resolve_replay_start_filename_from_observation_bindings(
        trace,
        obs_to_filename,
        external_system_observation_keys=external_system_observation_keys,
    )


def _read_json_file(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _base_filename(filename: Any) -> str:
    return str(filename or "").split("?", 1)[0].strip()


def _identity_tokens(*values: Any) -> List[str]:
    tokens: List[str] = []
    for value in values:
        text = str(value or "").lower()
        for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", text):
            token = token.strip()
            if len(token) >= 2:
                tokens.append(token)
    return list(dict.fromkeys(tokens))


def _token_overlap_score(source_tokens: List[str], candidate_tokens: List[str]) -> int:
    score = 0
    for source in source_tokens:
        for candidate in candidate_tokens:
            if source == candidate:
                score += max(4, min(len(source), 12))
            elif len(source) >= 2 and len(candidate) >= 2 and (source in candidate or candidate in source):
                score += min(len(source), len(candidate), 8)
    return score


def _load_page_signature_filename_maps(root: Path) -> Tuple[Dict[Tuple[str, str], str], Dict[str, List[Dict[str, str]]]]:
    payload = _read_json_file(root / "data" / "site_model" / "page_signatures.json")
    pages = payload.get("pages", []) if isinstance(payload, dict) else []
    by_general_instance: Dict[Tuple[str, str], str] = {}
    by_general: Dict[str, List[Dict[str, str]]] = {}
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            continue
        filename = _base_filename(page.get("filename"))
        general_id = str(page.get("general_page_id") or "").strip()
        instance_key = str(page.get("materialized_page_key") or "").strip()
        if not filename or not general_id:
            continue
        item = {
            "filename": filename,
            "general_id": general_id,
            "instance_key": instance_key,
            "page_name": str(page.get("page_name") or ""),
            "page_id": str(page.get("page_id") or ""),
        }
        by_general.setdefault(general_id, []).append(item)
        by_general_instance[(general_id, instance_key)] = filename
        by_general_instance[(general_id, "")] = filename if len(by_general.get(general_id, [])) == 1 else by_general_instance.get((general_id, ""), "")
    return by_general_instance, by_general


def _resolve_step_label_filename(
    label: Dict[str, Any],
    *,
    by_general_instance: Dict[Tuple[str, str], str],
    by_general: Dict[str, List[Dict[str, str]]],
) -> str:
    materialized_page_id = str(label.get("materialized_page_id") or "").strip()
    general_id = str(label.get("general_page_id") or label.get("page_cluster_id") or label.get("source_page_cluster_id") or "").strip()
    instance_key = str(label.get("instance_key") or "").strip()
    if materialized_page_id and "::" in materialized_page_id:
        candidate_general, candidate_instance = materialized_page_id.split("::", 1)
        filename = by_general_instance.get((candidate_general, candidate_instance))
        if filename:
            return filename
        general_id = general_id or candidate_general
        instance_key = instance_key or candidate_instance
    elif materialized_page_id:
        filename = by_general_instance.get((materialized_page_id, ""))
        if filename:
            return filename
        general_id = general_id or materialized_page_id
    if general_id and instance_key:
        filename = by_general_instance.get((general_id, instance_key))
        if filename:
            return filename
    candidates = by_general.get(general_id, []) if general_id else []
    if len(candidates) == 1:
        return candidates[0].get("filename", "")
    if not candidates:
        return ""
    source_tokens = _identity_tokens(
        materialized_page_id,
        instance_key,
        label.get("instance_label"),
        label.get("page_name"),
    )
    if not source_tokens:
        return ""
    ranked: List[Tuple[int, str]] = []
    for candidate in candidates:
        candidate_tokens = _identity_tokens(
            candidate.get("filename"),
            candidate.get("instance_key"),
            candidate.get("page_name"),
        )
        score = _token_overlap_score(source_tokens, candidate_tokens)
        if score > 0:
            ranked.append((score, candidate.get("filename", "")))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if len(ranked) == 1:
        return ranked[0][1]
    if len(ranked) >= 2 and ranked[0][0] > ranked[1][0]:
        return ranked[0][1]
    return ""


def _global_reasoning_candidates_for_obs_mapping(root: Path) -> List[Path]:
    debug_dir = root / "data" / "debug"
    return [debug_dir / "global_reasoning_final.json"]


def _load_obs_to_expected_filename(website_dir: str) -> Dict[str, str]:
    root = Path(website_dir)
    try:
        from trace2world_pipeline.app_spec.materialized_observation_bindings import load_obs_to_filename_from_materialized_bindings_from_disk

        materialized_mapping = load_obs_to_filename_from_materialized_bindings_from_disk(root)
        if materialized_mapping:
            return materialized_mapping
    except Exception:
        pass
    page_contracts = _read_json_file(root / "data" / "site_model" / "page_execution_contracts.json")
    instance_plan = _read_json_file(root / "data" / "debug" / "instance_plan.json")
    cluster_assignments = _read_json_file(root / "data" / "debug" / "cluster_assignments.json")

    pages = page_contracts.get("pages", []) if isinstance(page_contracts, dict) else []
    by_general_instance: Dict[Tuple[str, str], str] = {}
    by_general: Dict[str, List[str]] = {}
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            continue
        filename = str(page.get("filename") or "").strip()
        general_id = str(page.get("general_page_id") or "").strip()
        instance_key = str(page.get("instance_key") or "").strip()
        if not filename or not general_id:
            continue
        by_general.setdefault(general_id, []).append(filename)
        by_general_instance[(general_id, instance_key)] = filename
        materialized_page_id = str(page.get("materialized_page_id") or "").strip()
        if materialized_page_id and "::" in materialized_page_id:
            by_general_instance[(general_id, materialized_page_id.split("::", 1)[1])] = filename
        elif materialized_page_id:
            by_general_instance[(general_id, "")] = filename
    signature_by_general_instance, signature_by_general = _load_page_signature_filename_maps(root)
    for key, filename in signature_by_general_instance.items():
        if filename:
            by_general_instance.setdefault(key, filename)
    for general_id, items in signature_by_general.items():
        filenames = [item.get("filename", "") for item in items if item.get("filename")]
        if filenames:
            by_general.setdefault(general_id, filenames)

    mapping: Dict[str, str] = {}
    for candidate in _global_reasoning_candidates_for_obs_mapping(root):
        global_reasoning = _read_json_file(candidate)
        step_labels = global_reasoning.get("step_labels", []) if isinstance(global_reasoning, dict) else []
        for label in step_labels if isinstance(step_labels, list) else []:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id") or "")
            step_index = label.get("step_index")
            if not trace_id or step_index is None:
                continue
            filename = _resolve_step_label_filename(
                label,
                by_general_instance=by_general_instance,
                by_general=signature_by_general,
            )
            if filename:
                mapping[f"{trace_id}:{step_index}"] = filename
        if mapping:
            break
    if isinstance(instance_plan, dict):
        for general_id, plan in instance_plan.items():
            if not isinstance(plan, dict):
                continue
            groups = plan.get("instance_groups", [])
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                instance_key = str(group.get("instance_key") or "").strip()
                filename = by_general_instance.get((str(general_id), instance_key))
                if not filename:
                    continue
                for obs_key in group.get("obs_keys", []) if isinstance(group.get("obs_keys", []), list) else []:
                    if str(obs_key):
                        mapping[str(obs_key)] = filename

    obs_to_cluster = (
        cluster_assignments.get("obs_to_cluster", {})
        if isinstance(cluster_assignments, dict)
        else {}
    )
    if isinstance(obs_to_cluster, dict):
        for obs_key, general_id in obs_to_cluster.items():
            if str(obs_key) in mapping:
                continue
            filenames = by_general.get(str(general_id), [])
            if len(filenames) == 1:
                mapping[str(obs_key)] = filenames[0]
    return mapping


def _load_obs_to_cluster(website_dir: str) -> Dict[str, str]:
    cluster_assignments = _read_json_file(Path(website_dir) / "data" / "debug" / "cluster_assignments.json")
    obs_to_cluster = (
        cluster_assignments.get("obs_to_cluster", {})
        if isinstance(cluster_assignments, dict)
        else {}
    )
    if not isinstance(obs_to_cluster, dict):
        return {}
    return {
        str(obs_key): str(cluster_id)
        for obs_key, cluster_id in obs_to_cluster.items()
        if str(obs_key) and str(cluster_id)
    }


def _build_obs_to_filename_from_site_model(site_model: Any) -> Dict[str, str]:
    try:
        from trace2world_pipeline.app_spec.materialized_observation_bindings import load_obs_to_filename_from_materialized_bindings
        materialized_mapping = load_obs_to_filename_from_materialized_bindings(site_model)
        if materialized_mapping:
            return materialized_mapping
    except Exception:
        pass

    page_contracts = site_model.page_execution_contracts
    instance_plan = site_model.instance_plan
    cluster_assignments = site_model.cluster_assignments
    page_signatures = site_model.page_signatures

    pages = page_contracts.get("pages", []) if isinstance(page_contracts, dict) else []
    by_general_instance: Dict[Tuple[str, str], str] = {}
    by_general: Dict[str, List[str]] = {}
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            continue
        filename = str(page.get("filename") or "").strip()
        general_id = str(page.get("general_page_id") or "").strip()
        instance_key = str(page.get("instance_key") or "").strip()
        if not filename or not general_id:
            continue
        by_general.setdefault(general_id, []).append(filename)
        by_general_instance[(general_id, instance_key)] = filename
        materialized_page_id = str(page.get("materialized_page_id") or "").strip()
        if materialized_page_id and "::" in materialized_page_id:
            by_general_instance[(general_id, materialized_page_id.split("::", 1)[1])] = filename
        elif materialized_page_id:
            by_general_instance[(general_id, "")] = filename


    sig_pages = page_signatures.get("pages", []) if isinstance(page_signatures, dict) else []
    signature_by_general_instance: Dict[Tuple[str, str], str] = {}
    signature_by_general: Dict[str, List[Dict[str, str]]] = {}
    for page in sig_pages if isinstance(sig_pages, list) else []:
        if not isinstance(page, dict):
            continue
        filename = _base_filename(page.get("filename"))
        general_id = str(page.get("general_page_id") or "").strip()
        instance_key = str(page.get("materialized_page_key") or "").strip()
        if not filename or not general_id:
            continue
        item = {
            "filename": filename,
            "general_id": general_id,
            "instance_key": instance_key,
            "page_name": str(page.get("page_name") or ""),
            "page_id": str(page.get("page_id") or ""),
        }
        signature_by_general.setdefault(general_id, []).append(item)
        signature_by_general_instance[(general_id, instance_key)] = filename
        signature_by_general_instance[(general_id, "")] = (
            filename if len(signature_by_general.get(general_id, [])) == 1
            else signature_by_general_instance.get((general_id, ""), "")
        )
    for key, filename in signature_by_general_instance.items():
        if filename:
            by_general_instance.setdefault(key, filename)
    for general_id, items in signature_by_general.items():
        filenames = [item.get("filename", "") for item in items if item.get("filename")]
        if filenames:
            by_general.setdefault(general_id, filenames)

    mapping: Dict[str, str] = {}

    global_reasoning = site_model.metadata.get("global_reasoning") if isinstance(site_model.metadata, dict) else None
    if isinstance(global_reasoning, dict):
        step_labels = global_reasoning.get("step_labels", [])
        for label in step_labels if isinstance(step_labels, list) else []:
            if not isinstance(label, dict):
                continue
            trace_id = str(label.get("trace_id") or "")
            step_index = label.get("step_index")
            if not trace_id or step_index is None:
                continue
            filename = _resolve_step_label_filename(
                label,
                by_general_instance=by_general_instance,
                by_general=signature_by_general,
            )
            if filename:
                mapping[f"{trace_id}:{step_index}"] = filename

    if isinstance(instance_plan, dict):
        for general_id, plan in instance_plan.items():
            if not isinstance(plan, dict):
                continue
            groups = plan.get("instance_groups", [])
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                instance_key = str(group.get("instance_key") or "").strip()
                filename = by_general_instance.get((str(general_id), instance_key))
                if not filename:
                    continue
                for obs_key in group.get("obs_keys", []) if isinstance(group.get("obs_keys", []), list) else []:
                    if str(obs_key):
                        mapping[str(obs_key)] = filename

    obs_to_cluster = (
        cluster_assignments.get("obs_to_cluster", {})
        if isinstance(cluster_assignments, dict)
        else {}
    )
    if isinstance(obs_to_cluster, dict):
        for obs_key, general_id in obs_to_cluster.items():
            if str(obs_key) in mapping:
                continue
            filenames = by_general.get(str(general_id), [])
            if len(filenames) == 1:
                mapping[str(obs_key)] = filenames[0]
    return mapping


def _build_obs_to_cluster_from_site_model(site_model: Any) -> Dict[str, str]:
    cluster_assignments = site_model.cluster_assignments
    obs_to_cluster = (
        cluster_assignments.get("obs_to_cluster", {})
        if isinstance(cluster_assignments, dict)
        else {}
    )
    if not isinstance(obs_to_cluster, dict):
        return {}
    return {
        str(obs_key): str(cluster_id)
        for obs_key, cluster_id in obs_to_cluster.items()
        if str(obs_key) and str(cluster_id)
    }


def _looks_like_external_system_frame(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    page_state = payload.get("page_state") if isinstance(payload.get("page_state"), dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            payload.get("description"),
            payload.get("action"),
            payload.get("thought"),
            page_state.get("last_action") if isinstance(page_state, dict) else "",
            " ".join(str(item) for item in page_state.get("transition_evidence", []) or [])
            if isinstance(page_state, dict)
            else "",
        )
    ).lower()
    external_markers = (
        "android home screen",
        "home screen page",
        "home screen/launcher",
        "device home screen",
        "system home launcher",
        "launcher page",
        "app icon grid",
        "system status bar",
        "bottom dock",
        "dock row",
        "no in-app header",
        "not the meituan app interface",
        "device-level navigation",
    )
    if any(marker in text for marker in external_markers):
        return True
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    extracted_items = payload.get("extracted_items") if isinstance(payload.get("extracted_items"), dict) else {}
    shell_section = str(payload.get("shell_section") or "").strip().lower()
    body_pattern = str(payload.get("body_pattern") or "").strip().lower()
    header_pattern = str(payload.get("header_pattern") or "").strip().lower()
    return (
        shell_section == "home"
        and body_pattern in {"", "unknown"}
        and header_pattern in {"", "unknown"}
        and not components
        and not extracted_items
    )


def _load_external_system_observation_keys(website_dir: str) -> set[str]:
    root = Path(website_dir)
    frame_dir = root / "data" / "debug" / "frame_observations"
    if not frame_dir.is_dir():
        return set()
    keys: set[str] = set()
    for path in frame_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not _looks_like_external_system_frame(payload):
            continue
        trace_id = str(payload.get("trace_id") or "")
        step_index = payload.get("step_index")
        if trace_id and step_index is not None:
            keys.add(f"{trace_id}:{step_index}")
    return keys


def _trace_observation_key(trace: TraceData, step: Optional[TraceStep]) -> str:
    if step is None:
        return ""
    return f"{trace.trace_id}:{step.step_index}"


def _trace_step_operation(step: TraceStep) -> Tuple[str, Dict[str, Any]]:
    return _trace_step_operation_for_profile(step, resolve_environment_profile())


def _trace_step_operation_for_profile(
    step: TraceStep,
    environment_profile: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    raw = str(getattr(step, "raw_action", "") or "").strip()
    text = raw or str(getattr(step, "action", "") or "").strip()
    lowered = text.lower()
    metadata: Dict[str, Any] = {"raw_action": raw, "action_text": str(getattr(step, "action", "") or "")}
    raw_payload = getattr(step, "raw_action_payload", {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}

    if lowered.startswith(("open_app", "open ")):
        return "open_app", metadata
    if lowered.startswith(("type ", "input ")):
        value = re.sub(r"^(type|input)\s+", "", text, flags=re.IGNORECASE).strip()
        metadata["input_text"] = value
        return "fill", metadata
    if lowered.startswith(("swipe", "drag")):
        coords = re.findall(r"-?\d+(?:\.\d+)?", text)
        if len(coords) >= 4:
            metadata["coordinates"] = [float(item) for item in coords[:4]]
            _enrich_trace_coordinates(metadata, step, environment_profile)
        else:
            start = raw_payload.get("start_coordinate")
            end = raw_payload.get("end_coordinate")
            if isinstance(start, list) and isinstance(end, list) and len(start) >= 2 and len(end) >= 2:
                metadata["coordinates"] = [
                    float(start[0]),
                    float(start[1]),
                    float(end[0]),
                    float(end[1]),
                ]
                _enrich_trace_coordinates(metadata, step, environment_profile)
        return "swipe", metadata
    if lowered.startswith(("click", "tap")):
        coords = re.findall(r"-?\d+(?:\.\d+)?", text)
        if len(coords) >= 2:
            metadata["coordinates"] = [float(coords[0]), float(coords[1])]
            _enrich_trace_coordinates(metadata, step, environment_profile)
        return "click", metadata
    if "滑动" in text or "滚动" in text:
        return "scroll", metadata
    return "agent_execute", metadata


def _operation_preserves_page_identity(operation: str) -> bool:
    return str(operation or "").strip().lower() in {"scroll", "swipe"}


def _looks_like_trace_terminal_or_takeover(step_contract: Dict[str, Any]) -> bool:
    if not isinstance(step_contract, dict):
        return False
    action = step_contract.get("action", {}) if isinstance(step_contract.get("action", {}), dict) else {}
    raw_action = str(action.get("raw_action") or "").strip().lower()
    if raw_action == "takeover":
        return True
    operation = str(action.get("operation") or "").strip().lower()
    text = " ".join(
        str(action.get(key) or "")
        for key in ("intent", "action_text", "raw_action")
    )
    if operation == "agent_execute" and any(
        marker in text
        for marker in ("任务完成", "已完成", "终止", "结束操作", "完成全部任务", "请求用户接管", "接管操作")
    ):
        return True
    return False


def _looks_like_initial_app_open(step: TraceStep) -> bool:
    text = str(getattr(step, "action", "") or "")
    if "打开" not in text:
        return False
    return any(marker in text for marker in ("APP", "App", "app", "应用"))


def _image_size(path: str) -> Optional[Tuple[int, int]]:
    if not path:
        return None
    try:
        from PIL import Image
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def _enrich_trace_coordinates(
    metadata: Dict[str, Any],
    step: TraceStep,
    environment_profile: Dict[str, Any],
) -> None:
    coords = metadata.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return
    profile = resolve_environment_profile(environment_profile)
    size = _image_size(getattr(step, "screenshot_path", ""))
    if not size:
        if profile["platform"] == "desktop":
            raise ValueError(
                "Desktop trace coordinate transformation requires a readable source screenshot size"
            )
        metadata["viewport_coordinates"] = [int(round(float(item))) for item in coords]
        metadata["coordinate_transform_version"] = "legacy_mobile_identity_without_source_size"
        metadata["environment_profile_hash"] = profile["profile_hash"]
        return
    source_w, source_h = size
    if source_w <= 0 or source_h <= 0:
        return
    raw_payload = getattr(step, "raw_action_payload", {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    source_rect = raw_payload.get("source_app_content_rect")
    if not isinstance(source_rect, dict):
        source_rect = {"x": 0, "y": 0, "width": source_w, "height": source_h}
    try:
        source_x = int(source_rect.get("x", 0))
        source_y = int(source_rect.get("y", 0))
        content_w = int(source_rect.get("width", source_w))
        content_h = int(source_rect.get("height", source_h))
    except (TypeError, ValueError) as exc:
        raise ValueError("source_app_content_rect must contain integer coordinates") from exc
    if (
        source_x < 0
        or source_y < 0
        or content_w <= 0
        or content_h <= 0
        or source_x + content_w > source_w
        or source_y + content_h > source_h
    ):
        raise ValueError("source_app_content_rect must fit within the source screenshot")
    viewport_w = int(profile["viewport"]["width"])
    viewport_h = int(profile["viewport"]["height"])
    scaled: List[int] = []
    for idx in range(0, len(coords), 2):
        if idx + 1 >= len(coords):
            break
        x = max(
            0,
            min(
                viewport_w - 1,
                round((float(coords[idx]) - source_x) / content_w * viewport_w),
            ),
        )
        y = max(
            0,
            min(
                viewport_h - 1,
                round((float(coords[idx + 1]) - source_y) / content_h * viewport_h),
            ),
        )
        scaled.extend([int(x), int(y)])
    metadata["coordinate_space"] = {"width": source_w, "height": source_h}
    metadata["coordinate_transform_version"] = "environment_profile_v1"
    metadata["source_coordinate_space"] = {"width": source_w, "height": source_h}
    metadata["source_app_content_rect"] = {
        "x": source_x,
        "y": source_y,
        "width": content_w,
        "height": content_h,
    }
    metadata["target_interaction_viewport"] = dict(profile["viewport"])
    metadata["target_browser_viewport"] = dict(profile["browser_viewport"])
    metadata["target_app_surface"] = dict(profile["app_surface"])
    metadata["environment_profile_hash"] = profile["profile_hash"]
    metadata["viewport_coordinates"] = scaled
    app_surface = profile["app_surface"]
    metadata["browser_coordinates"] = [
        value + (int(app_surface["x"]) if idx % 2 == 0 else int(app_surface["y"]))
        for idx, value in enumerate(scaled)
    ]


def _build_execution_pack_steps(
    trace: TraceData,
    *,
    start_filename: str,
    materialized_filenames: set,
    obs_to_filename: Dict[str, str],
    obs_to_cluster: Dict[str, str],
    external_system_observation_keys: set,
    environment_profile: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    current_filename = start_filename if start_filename and start_filename in materialized_filenames else ""
    steps: List[Dict[str, Any]] = []
    terminal_observations = sorted(
        [
            item
            for item in (getattr(trace, "terminal_observations", []) or [])
            if isinstance(item, dict)
            and (
                item.get("terminal_evidence") is True
                or str(item.get("observation_disposition") or "").strip().lower() == "retain"
            )
        ],
        key=lambda item: int(item.get("step_index", -1)),
    )
    for index, step in enumerate(trace.steps):
        operation, metadata = _trace_step_operation_for_profile(step, environment_profile)
        next_step = trace.steps[index + 1] if index + 1 < len(trace.steps) else None
        pre_observation_key = _trace_observation_key(trace, step)
        terminal_observation = None
        if next_step is None:
            terminal_observation = next(
                (
                    item
                    for item in terminal_observations
                    if int(item.get("step_index", -1)) > int(step.step_index)
                ),
                None,
            )
        post_observation_key = (
            str((terminal_observation or {}).get("obs_key") or "").strip()
            or (
                f"{trace.trace_id}:{int(terminal_observation.get('step_index'))}"
                if terminal_observation is not None
                and isinstance(terminal_observation.get("step_index"), int)
                else ""
            )
            or _trace_observation_key(trace, next_step)
        )
        post_screenshot_path = (
            str(terminal_observation.get("screenshot_path") or "")
            if terminal_observation is not None
            else (next_step.screenshot_path if next_step else "")
        )
        pre_state = {
            "observation_key": pre_observation_key,
            "screenshot_path": step.screenshot_path,
        }
        expected_post_state = {
            "observation_key": post_observation_key,
            "screenshot_path": post_screenshot_path,
        }
        if current_filename:
            pre_state["filename"] = current_filename
        elif obs_to_filename.get(pre_observation_key):
            pre_state["filename"] = obs_to_filename[pre_observation_key]
        if obs_to_filename.get(post_observation_key):
            expected_post_state["filename"] = obs_to_filename[post_observation_key]
        pre_cluster = obs_to_cluster.get(pre_observation_key)
        post_cluster = obs_to_cluster.get(post_observation_key)
        same_general_page_observation = bool(pre_cluster and post_cluster and pre_cluster == post_cluster)
        unresolved_materialized_post = bool(post_cluster and not expected_post_state.get("filename"))
        if (
            post_observation_key
            and pre_state.get("filename")
            and (
                _operation_preserves_page_identity(operation)
                or (not expected_post_state.get("filename") and same_general_page_observation)
                or unresolved_materialized_post
            )
        ):
            expected_post_state["filename"] = pre_state["filename"]
        if expected_post_state.get("filename"):
            current_filename = str(expected_post_state["filename"])
        execution_policy = "agent_execute"
        if index == 0 and (operation == "open_app" or _looks_like_initial_app_open(step)):
            operation = "open_app"
            execution_policy = "normalize_initial_app_open"
        elif pre_observation_key in external_system_observation_keys:
            execution_policy = "external_system_observation_skip"
        steps.append({
            "step_index": step.step_index,
            "source_observation_key": pre_observation_key,
            "pre_state": pre_state,
            "action": {
                "operation": operation,
                "intent": str(step.action or ""),
                **metadata,
            },
            "expected_post_state": expected_post_state,
            "execution_policy": execution_policy,
        })
    return steps, current_filename


def build_trace_action_execution_pack_from_site_model(
    site_model: Any,
    trace: TraceData,
    *,
    output_dir: str = "",
) -> Dict[str, Any]:
    environment_profile = resolve_environment_profile(
        getattr(site_model, "environment_profile", None)
    )
    materialized_filenames = {p.filename for p in site_model.structure.page_prototypes if p.filename}
    obs_to_filename = _build_obs_to_filename_from_site_model(site_model)
    obs_to_cluster = _build_obs_to_cluster_from_site_model(site_model)
    external_system_observation_keys = _load_external_system_observation_keys(output_dir) if output_dir else set()
    start_filename, start_reason = _resolve_replay_start_filename_from_observation_bindings(
        trace,
        obs_to_filename,
        external_system_observation_keys=external_system_observation_keys,
    )
    steps, _ = _build_execution_pack_steps(
        trace,
        start_filename=start_filename,
        materialized_filenames=materialized_filenames,
        obs_to_filename=obs_to_filename,
        obs_to_cluster=obs_to_cluster,
        external_system_observation_keys=external_system_observation_keys,
        environment_profile=environment_profile,
    )
    terminal_contract = (
        dict(trace.terminal_contract)
        if isinstance(getattr(trace, "terminal_contract", None), dict)
        else {}
    )
    return {
        "format": "reference_trace_action_execution_pack",
        "schema_revision": 1,
        "trace_id": trace.trace_id,
        "task": trace.task,
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "start_state": {
            "filename": start_filename,
            "reason": start_reason,
        },
        "terminal_contract": terminal_contract,
        "steps": steps,
    }


def build_trace_action_execution_pack(
    website_dir: str,
    trace: TraceData,
) -> Dict[str, Any]:
    materialized_filenames: set = set()
    root = Path(website_dir)
    environment_profile = load_environment_profile_from_result_dir(root)
    page_dirs = (root / "phase_pagegen" / "pages",) if (root / "phase_pagegen" / "pages").is_dir() else (
        root,
        root / "frontend" / "pages",
    )
    for candidate in page_dirs:
        if candidate.is_dir():
            materialized_filenames.update(f.name for f in candidate.iterdir() if f.is_file() and f.suffix == ".html")
    obs_to_filename = _load_obs_to_expected_filename(website_dir)
    obs_to_cluster = _load_obs_to_cluster(website_dir)
    external_system_observation_keys = _load_external_system_observation_keys(website_dir)
    binding_start_filename, binding_start_reason = _resolve_replay_start_filename_from_observation_bindings(
        trace,
        obs_to_filename,
        external_system_observation_keys=external_system_observation_keys,
    )
    compatibility_filename, compatibility_reason = _resolve_replay_start_filename(website_dir, trace)
    if compatibility_reason.startswith("missing_"):
        start_filename, start_reason = binding_start_filename, binding_start_reason
    elif binding_start_filename == compatibility_filename:
        start_filename, start_reason = binding_start_filename, binding_start_reason
    else:
        start_filename, start_reason = compatibility_filename, compatibility_reason
    steps, _ = _build_execution_pack_steps(
        trace,
        start_filename=start_filename,
        materialized_filenames=materialized_filenames,
        obs_to_filename=obs_to_filename,
        obs_to_cluster=obs_to_cluster,
        external_system_observation_keys=external_system_observation_keys,
        environment_profile=environment_profile,
    )
    terminal_contract = (
        dict(trace.terminal_contract)
        if isinstance(getattr(trace, "terminal_contract", None), dict)
        else {}
    )
    return {
        "format": "reference_trace_action_execution_pack",
        "schema_revision": 1,
        "trace_id": trace.trace_id,
        "task": trace.task,
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "start_state": {
            "filename": start_filename,
            "reason": start_reason,
        },
        "terminal_contract": terminal_contract,
        "steps": steps,
    }


def _is_normalized_initial_open_app_step(step_contract: Dict[str, Any]) -> bool:
    return (
        isinstance(step_contract, dict)
        and step_contract.get("execution_policy") == "normalize_initial_app_open"
        and (step_contract.get("action", {}) or {}).get("operation") == "open_app"
    )


def _scroll_direction_from_intent(intent: str) -> str:
    text = str(intent or "")
    if "向下" in text or "下滑" in text:
        return "up"
    if "向左" in text or "左滑" in text:
        return "right"
    if "向右" in text or "右滑" in text:
        return "left"
    return "down"


def _deterministic_step_command(step_contract: Dict[str, Any]) -> str:
    action = step_contract.get("action", {}) if isinstance(step_contract, dict) else {}
    if not isinstance(action, dict):
        return ""
    operation = str(action.get("operation") or "")
    coords = action.get("viewport_coordinates")
    if operation == "click" and isinstance(coords, list) and len(coords) >= 2:
        return f"tap {int(coords[0])} {int(coords[1])}"
    if operation == "fill" and action.get("input_text") not in (None, ""):
        intent = str(action.get("intent") or action.get("action_text") or "")
        return (
            "type-text --intent "
            + shlex.quote(intent)
            + " "
            + shlex.quote(str(action.get("input_text") or ""))
        )
    if operation == "swipe":
        intent = str(action.get("intent") or action.get("action_text") or "")
        prefix = "swipe --intent " + shlex.quote(intent) + " " if intent else "swipe "
        if isinstance(coords, list) and len(coords) >= 4:
            return f"{prefix}{int(coords[0])} {int(coords[1])} {int(coords[2])} {int(coords[3])}"
        return f"{prefix}{_scroll_direction_from_intent(intent)}"
    if operation == "scroll":
        intent = str(action.get("intent") or action.get("action_text") or "")
        prefix = "scroll --intent " + shlex.quote(intent) + " " if intent else "scroll "
        return f"{prefix}{_scroll_direction_from_intent(intent)}"
    return ""


def _browser_command_succeeded(output: str) -> bool:
    lowered = str(output or "").lower()
    failure_markers = (
        "failed",
        "unsupported",
        "unknown ref",
        "requires",
        "timed out",
        "command error",
        "parse error",
    )
    return bool(str(output or "").strip()) and not any(marker in lowered for marker in failure_markers)


def _filename_from_page_url(page_url: str) -> str:
    parsed = urlparse(str(page_url or ""))
    path = parsed.path or ""
    return os.path.basename(path)


def _expected_post_state_error(step_contract: Dict[str, Any], page_url: str) -> str:
    expected_post_state = step_contract.get("expected_post_state", {}) if isinstance(step_contract, dict) else {}
    expected_filename = str(expected_post_state.get("filename") or "").strip() if isinstance(expected_post_state, dict) else ""
    if not expected_filename:
        return ""
    actual_filename = _filename_from_page_url(page_url)
    if actual_filename == expected_filename:
        return ""
    return f"Expected post-state filename {expected_filename}, got {actual_filename or page_url}"


def _reference_post_screenshot_path(trace: TraceData, step_result: StepResult) -> str:
    steps = list(getattr(trace, "steps", []) or [])
    for index, step in enumerate(steps):
        if int(getattr(step, "step_index", -1)) != int(step_result.step_index):
            continue
        if index + 1 < len(steps):
            return str(getattr(steps[index + 1], "screenshot_path", "") or "")
        return str(getattr(step, "screenshot_path", "") or "")
    return ""


OPTIMIZER_TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": (
            "Execute a bash command. Browser commands using playwright-cli syntax "
            "are handled by the Python Playwright adapter when available; use for "
            "browser operations, "
            "reading files, processing data with unix tools, etc. "
            "Output is truncated to last 200 lines or 40KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 120)"},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "report_done",
        "description": "Report step completion. Call this when you have finished attempting the action.",
        "parameters": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "description": "Whether the action was completed successfully"},
                "error": {"type": "string", "description": "Error description if action failed"},
            },
        },
    },
]


STEP_EXECUTION_PROMPT = """You are replaying user interaction traces on a generated website via a Playwright browser adapter.
Session: {session_id} | Viewport: {platform} ({viewport_width}x{viewport_height})

Task: {task}

Complete step plan:
{step_plan}

You will be asked to execute steps one at a time.  Previous step results are in
the conversation history — use them to understand the current page state.

Use the bash tool with this browser command syntax (IMPORTANT: use -s={session_id} for all commands):
- playwright-cli -s={session_id} snapshot         → take accessibility snapshot (returns a11y tree with ref IDs like e1, e2, ...)
- playwright-cli -s={session_id} click <ref>       → click an element by its ref ID (e.g. click e32)
- playwright-cli -s={session_id} tap <x> <y>       → tap viewport coordinates from trace action contracts
- playwright-cli -s={session_id} fill <ref> "text" → fill a form field by ref ID
- playwright-cli -s={session_id} goto "url"        → navigate to a URL
- playwright-cli -s={session_id} press <key>       → press a keyboard key (e.g. press Enter)
- playwright-cli -s={session_id} scroll <direction> [amount] → scroll current page; direction is up/down/left/right
- playwright-cli -s={session_id} swipe <direction> [amount]  → swipe current page; direction is up/down/left/right
- playwright-cli -s={session_id} run-code "async page => {{ ... }}"  → run JavaScript
- playwright-cli -s={session_id} screenshot --filename="path.png" → capture screenshot
- playwright-cli -s={session_id} close           → close this browser session

CRITICAL: To click or interact with elements, you MUST:
1. First run `snapshot` to get the accessibility tree
2. Find the target element's ref ID (e.g. e32) from the snapshot output
3. Use `click <ref>` with that ref ID — do NOT use CSS selectors or text selectors

Strategy:
1. Take a snapshot to see the current page's accessibility tree
2. Find the element matching the step action by reading element labels in the tree
3. Click or interact with it using the ref ID
4. Verify the action succeeded (take another snapshot if needed)
5. Call report_done with success=true if action completed, or success=false with error description

Max 10 bash calls per step. Be efficient."""

VISUAL_COMPARISON_PROMPT = """Compare these two {platform_label} webpage screenshots.
FIRST image: reference screenshot (ground truth from {reference_label}).
Step context: {action}
SECOND image: generated website at the same step, same {platform_label} viewport ({viewport_width}x{viewport_height}).

Environment contract: {environment_contract}

Analyze structural and functional differences. Return JSON:
{{
    "visual_score": <float 0.0-1.0, where 1.0 is identical>,
    "differences": ["<list of visual differences>"],
    "missing_elements": ["<elements present in reference but missing in generated>"],
    "layout_issues": ["<layout/positioning problems>"]
}}

Focus on structural and functional gaps, not minor color/font differences."""

VISUAL_FIX_PROMPT = """Fix visual/layout issues in this generated website CSS file.
The website was compared against reference screenshots from {reference_label},
and the following visual problems were identified:

Environment contract: {environment_contract}

File: {filename}
Visual Issues:
{issues_json}

Current file content (with line numbers):
{numbered_content}

Generate search-replace patches to fix these visual/layout issues. Return JSON:
{{
    "replacements": [
        {{"old": "<exact substring to find>", "new": "<replacement text>"}}
    ]
}}

Rules:
- Each "old" must be an exact substring of the file content
- Focus on CSS layout fixes: widths, heights, margins, paddings, flex/grid properties, positioning
- Fix layout_issues and missing_elements by adjusting CSS, not by adding new HTML
- Keep changes minimal and targeted — only fix the reported visual issues
- Do not refactor or reorganize unrelated CSS"""

FIX_GENERATION_PROMPT = """Fix functional issues in this generated website file.
The website is being tested by replaying user interaction traces, and the following steps failed:

File: {filename}
Issues:
{issues_json}

Current file content (with line numbers):
{numbered_content}

Generate search-replace patches to fix these issues. Return JSON:
{{
    "replacements": [
        {{"old": "<exact substring to find>", "new": "<replacement text>"}}
    ]
}}

Rules:
- Each "old" must be an exact substring of the file content
- Only fix issues related to the reported failures
- Keep changes minimal and targeted
- Do not add new features or refactor unrelated code"""


async def execute_bash(command: str, working_dir: str, env: dict, timeout: int = 30) -> str:
    timeout = min(max(timeout, 5), 120)

    parsed_browser_command = _extract_playwright_cli_command(command)
    if parsed_browser_command and _playwright_import_available()[0]:
        session_id, browser_args = parsed_browser_command
        try:
            return await asyncio.wait_for(
                _run_python_playwright_cli(session_id, browser_args, working_dir),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"Command timed out after {timeout}s"
        except Exception as exc:
            return f"Command error: {exc}"

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env=env,
        )

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return f"Command timed out after {timeout}s"

        output = stdout.decode('utf-8', errors='replace')

    except Exception as e:
        return f"Command error: {e}"

    lines = output.splitlines(keepends=True)
    total_lines = len(lines)
    total_bytes = len(output.encode('utf-8'))

    if total_lines <= BASH_MAX_OUTPUT_LINES and total_bytes <= BASH_MAX_OUTPUT_BYTES:
        return output


    kept_lines = []
    kept_bytes = 0
    for line in lines:
        line_bytes = len(line.encode('utf-8'))
        if kept_bytes + line_bytes > BASH_MAX_OUTPUT_BYTES:
            break
        if len(kept_lines) >= BASH_MAX_OUTPUT_LINES:
            break
        kept_lines.append(line)
        kept_bytes += line_bytes

    truncated = ''.join(kept_lines)
    remaining = total_lines - len(kept_lines)
    hint = f"\n\n[Showing lines 1-{len(kept_lines)} of {total_lines} ({remaining} more lines).]"
    return truncated + hint


async def exec_playwright(
    session_id: str, command: str, working_dir: str, env: dict, timeout: int = 30
) -> str:
    if _playwright_import_available()[0]:
        try:
            return await asyncio.wait_for(
                _run_python_playwright_cli(session_id, shlex.split(command), working_dir),
                timeout=timeout,
            )
        except ValueError as exc:
            return f"(command parse error: {exc})"
        except asyncio.TimeoutError:
            return f"(command timed out after {timeout}s)"
        except Exception as exc:
            return f"(command error: {exc})"

    full_cmd = f'playwright-cli -s={session_id} {command}'

    try:
        process = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=working_dir,
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        output = stdout.decode('utf-8', errors='replace')
        if not output.strip() and stderr:
            output = stderr.decode('utf-8', errors='replace')
        return output

    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        return f"(command timed out after {timeout}s)"
    except Exception as e:
        return f"(command error: {e})"


async def cleanup_playwright_session(session_id: str, env: dict, working_dir: str):
    try:
        await exec_playwright(session_id, 'close', working_dir, env, timeout=10)
    except Exception:
        pass
    finally:
        _drop_replay_execution_context(session_id)


def _convert_output_to_input(output_items) -> list:
    result = []
    for item in output_items:
        if item.type == "function_call":
            result.append({
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            })
        elif item.type == "message":
            for content in item.content:
                if hasattr(content, "text") and content.text:
                    result.append({
                        "role": "assistant",
                        "content": content.text,
                    })
    return result


async def _check_playwright_available() -> bool:
    if _playwright_import_available()[0]:
        return True
    try:
        proc = await asyncio.create_subprocess_shell(
            "which playwright-cli",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


class TraceReplayOptimizer:

    def __init__(
        self,
        logger=None,
        max_iterations: int = 3,
        functional_threshold: float = 1.0,
        model: str = None,
        vision_model: str = None,
        reasoning_effort: str = "medium",
        diagnostic_only: bool = False,
        replay_progress_path: Optional[str] = None,
        replay_action_pack_path: Optional[str] = None,
        replay_max_concurrent: Optional[int] = None,
    ):
        self.tdd_logger = logger
        self.max_iterations = max_iterations
        self.functional_threshold = functional_threshold
        self.model = model
        self.vision_model = vision_model or model
        self.reasoning_effort = reasoning_effort
        self.diagnostic_only = diagnostic_only
        self.last_replay_status = "not_started"
        self.last_replay_error = ""
        self.replay_progress_path = replay_progress_path
        self.replay_action_pack_path = replay_action_pack_path
        self.replay_max_concurrent = int(replay_max_concurrent or 0)
        self.environment_profile = resolve_environment_profile()
        self.visual_prompt_policy = build_environment_prompt_policy(
            self.environment_profile,
            "visual_evaluation",
        )

    def _log_info(self, msg: str):
        if self.tdd_logger:
            self.tdd_logger.log_info(msg)
        else:
            logger.info(msg)

    def _log_warning(self, msg: str):
        if self.tdd_logger:
            self.tdd_logger.log_warning(msg)
        else:
            logger.warning(msg)

    def _log_error(self, msg: str):
        if self.tdd_logger:
            self.tdd_logger.log_error(msg)
        else:
            logger.error(msg)


    async def optimize(
        self,
        website_dir: str,
        traces: List[TraceData],
        resource_dir: Optional[str] = None,
    ) -> OptimizationResult:
        if not traces:
            self._log_warning("No traces provided, skipping optimization")
            return OptimizationResult()
        self.environment_profile = load_environment_profile_from_result_dir(website_dir)
        self.visual_prompt_policy = build_environment_prompt_policy(
            self.environment_profile,
            "visual_evaluation",
        )


        if not await _check_playwright_available():
            self._log_warning("Playwright replay adapter not available, skipping optimization")
            return OptimizationResult()

        all_gap_reports = []
        all_files_modified = []
        total_patches = 0
        initial_score = None


        self._log_info(f"Starting functional optimization (max {self.max_iterations} iterations)")

        for iteration in range(self.max_iterations):
            self._log_info(f"Iteration {iteration + 1}/{self.max_iterations}")

            results = await self._replay_all_traces(
                website_dir, traces, capture_screenshots=False
            )


            total_steps = sum(r.total_steps for r in results)
            completed_steps = sum(r.completed_steps for r in results)
            functional_score = completed_steps / total_steps if total_steps > 0 else 0.0

            if initial_score is None:
                initial_score = functional_score

            self._log_info(
                f"  Functional score: {functional_score:.0%} "
                f"({completed_steps}/{total_steps} steps)"
            )

            gap_report = GapReport(
                iteration=iteration + 1,
                functional_results=results,
                overall_functional_score=functional_score,
            )
            all_gap_reports.append(gap_report)

            if functional_score >= self.functional_threshold:
                self._log_info("  Functional threshold reached, stopping iterations")
                break


            if getattr(self, 'diagnostic_only', False):
                self._log_info("  Diagnostic mode: skipping patches")
                break

            modified, patches = await self._apply_fixes(website_dir, results)
            all_files_modified.extend(modified)
            total_patches += patches
            self._log_info(f"  Applied {patches} patches to {len(modified)} files")

            if patches == 0:
                self._log_info("  No patches applied, stopping iterations")
                break


        self._log_info("Final replay with screenshot capture")
        final_results = await self._replay_all_traces(
            website_dir, traces, capture_screenshots=True
        )

        final_total = sum(r.total_steps for r in final_results)
        final_completed = sum(r.completed_steps for r in final_results)
        final_functional_score = final_completed / final_total if final_total > 0 else 0.0


        self._log_info("Running visual comparisons")
        visual_reports = await self._run_visual_comparisons(traces, final_results)

        avg_visual = 0.0
        if visual_reports:
            scores = [vr.avg_visual_score for vr in visual_reports if vr.step_comparisons]
            avg_visual = sum(scores) / len(scores) if scores else 0.0


        if self.diagnostic_only:
            self._log_info(f"Diagnostic mode: skipping visual CSS fixes (score={avg_visual:.2f})")
        elif visual_reports and avg_visual < 0.95:
            self._log_info(f"Visual score {avg_visual:.2f} < 0.95, applying visual fixes")
            vis_modified, vis_patches = await self._apply_visual_fixes(
                website_dir, visual_reports
            )
            all_files_modified.extend(vis_modified)
            total_patches += vis_patches
            self._log_info(f"  Visual fixes: {vis_patches} patches to {len(vis_modified)} files")

            if vis_patches > 0:

                self._log_info("Re-replay after visual fixes")
                post_vis_results = await self._replay_all_traces(
                    website_dir, traces, capture_screenshots=True
                )
                post_vis_total = sum(r.total_steps for r in post_vis_results)
                post_vis_completed = sum(r.completed_steps for r in post_vis_results)
                final_functional_score = post_vis_completed / post_vis_total if post_vis_total > 0 else 0.0


                post_vis_reports = await self._run_visual_comparisons(traces, post_vis_results)
                if post_vis_reports:
                    post_scores = [vr.avg_visual_score for vr in post_vis_reports if vr.step_comparisons]
                    post_avg = sum(post_scores) / len(post_scores) if post_scores else 0.0
                    self._log_info(f"  Visual score: {avg_visual:.2f} → {post_avg:.2f}")
                    avg_visual = post_avg
                    visual_reports = post_vis_reports
                    final_results = post_vis_results


        final_gap = GapReport(
            iteration=len(all_gap_reports) + 1,
            functional_results=final_results,
            visual_reports=visual_reports,
            overall_functional_score=final_functional_score,
            overall_visual_score=avg_visual,
        )
        all_gap_reports.append(final_gap)

        return OptimizationResult(
            iterations_run=len(all_gap_reports) - 1,
            initial_functional_score=initial_score or 0.0,
            final_functional_score=final_functional_score,
            final_visual_score=avg_visual,
            gap_reports=all_gap_reports,
            files_modified=list(set(all_files_modified)),
            patches_applied=total_patches,
        )


    async def replay_traces(
        self,
        website_dir: str,
        traces: List[TraceData],
        capture_screenshots: bool = True,
    ) -> Tuple[List[TraceReplayResult], float]:
        if not traces:
            self.last_replay_status = "no_traces"
            self.last_replay_error = ""
            return [], 0.0

        if not await _check_playwright_available():
            ok, import_error = _playwright_import_available()
            self.last_replay_status = "not_run"
            self.last_replay_error = (
                "Python Playwright is not importable"
                + (f": {import_error}" if import_error else "")
                if not ok
                else "No replay browser adapter available"
            )
            self._log_warning(f"{self.last_replay_error}, skipping replay")
            return [], 0.0

        self.last_replay_status = "running"
        self.last_replay_error = ""

        results = await self._replay_all_traces(
            website_dir, traces, capture_screenshots=capture_screenshots
        )

        total_steps = sum(r.total_steps for r in results)
        completed = sum(r.completed_steps for r in results)
        score = completed / total_steps if total_steps > 0 else 0.0

        self._log_info(
            f"Replay complete: {completed}/{total_steps} steps ({score:.0%})"
        )
        self.last_replay_status = "completed"
        return results, score

    async def visual_compare(
        self,
        traces: List[TraceData],
        replay_results: List[TraceReplayResult],
    ) -> Tuple[List[VisualGapReport], float]:
        reports = await self._run_visual_comparisons(traces, replay_results)

        avg_visual = 0.0
        if reports:
            scores = [vr.avg_visual_score for vr in reports if vr.step_comparisons]
            avg_visual = sum(scores) / len(scores) if scores else 0.0

        self._log_info(f"Visual comparison complete: avg_score={avg_visual:.2f}")
        return reports, avg_visual


    async def _replay_all_traces(
        self,
        website_dir: str,
        traces: List[TraceData],
        capture_screenshots: bool,
    ) -> List[TraceReplayResult]:
        semaphore = (
            asyncio.Semaphore(self.replay_max_concurrent)
            if self.replay_max_concurrent > 0
            else None
        )
        results: List[Optional[TraceReplayResult]] = [None] * len(traces)

        async def replay_one(index: int, trace: TraceData):
            if semaphore is None:
                result = await self._replay_single_trace(website_dir, trace, capture_screenshots)
            else:
                async with semaphore:
                    result = await self._replay_single_trace(website_dir, trace, capture_screenshots)
            return index, result

        tasks = [
            asyncio.create_task(replay_one(index, trace))
            for index, trace in enumerate(traces)
        ]
        for task in asyncio.as_completed(tasks):
            index, result = await task
            results[index] = result
            self._write_replay_progress(result)
            self._log_info(
                f"Replay trace done: {result.trace_id} "
                f"{result.completed_steps}/{result.total_steps}"
            )
        return [result for result in results if result is not None]

    def _write_replay_progress(self, result: TraceReplayResult) -> None:
        if not self.replay_progress_path:
            return
        try:
            os.makedirs(os.path.dirname(self.replay_progress_path), exist_ok=True)
            with open(self.replay_progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
        except Exception as exc:
            self._log_warning(f"Failed to write replay progress: {exc}")

    def _write_replay_action_pack(self, action_pack: Dict[str, Any]) -> None:
        if not self.replay_action_pack_path:
            return
        try:
            os.makedirs(os.path.dirname(self.replay_action_pack_path), exist_ok=True)
            with open(self.replay_action_pack_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(action_pack, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._log_warning(f"Failed to write replay action pack: {exc}")

    async def _replay_single_trace(
        self,
        website_dir: str,
        trace: TraceData,
        capture_screenshots: bool,
    ) -> TraceReplayResult:
        session_id = f"opt_{trace.trace_id}_{int(time.time() * 1000) % 100000}"
        abs_dir = os.path.abspath(website_dir)
        action_pack = build_trace_action_execution_pack(abs_dir, trace)
        self._write_replay_action_pack(action_pack)
        start_state = action_pack.get("start_state", {})
        start_filename = str(start_state.get("filename") or "index.html")
        start_reason = str(start_state.get("reason") or "")
        website_url = _resolve_file_url(abs_dir, start_filename)
        start_path = _generated_page_path(abs_dir, start_filename)

        env = os.environ.copy()
        env['PLAYWRIGHT_MCP_BROWSER'] = 'chrome'
        env['PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS'] = 'true'

        step_results: List[StepResult] = []
        blocking_step = None

        _register_replay_execution_context(
            session_id,
            {
                "mode": "reference_trace_replay",
                "task_id": trace.trace_id,
                "reference_trace_id": trace.trace_id,
                "start_filename": start_filename,
                "start_reason": start_reason,
                "environment_profile": self.environment_profile,
            },
        )

        if not start_path.is_file():
            self._log_error(
                f"Trace {trace.trace_id} replay start file missing: {start_filename}"
            )
            _drop_replay_execution_context(session_id)
            return TraceReplayResult(
                trace_id=trace.trace_id,
                total_steps=len(trace.steps),
                completed_steps=0,
                step_results=[],
                blocking_step=trace.steps[0].step_index if trace.steps else None,
            )


        step_plan_lines = []
        for s in trace.steps:
            step_plan_lines.append(f"  Step {s.step_index}: {s.action}")
        step_plan = "\n".join(step_plan_lines)


        system_prompt = STEP_EXECUTION_PROMPT.format(
            session_id=session_id,
            platform=self.environment_profile["platform"],
            viewport_width=self.environment_profile["browser_viewport"]["width"],
            viewport_height=self.environment_profile["browser_viewport"]["height"],
            task=trace.task or trace.trace_id,
            step_plan=step_plan,
        )
        input_list = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Structured reference action execution pack:\n"
                    + json.dumps(action_pack, ensure_ascii=False, indent=2)
                ),
            },
        ]

        try:

            open_output = await exec_playwright(
                session_id, f'open "{website_url}"', abs_dir, env, timeout=30
            )


            await execute_bash(
                f'playwright-cli -s={session_id} run-code '
                f'"async page => {{ await page.setViewportSize({{width: {self.environment_profile["browser_viewport"]["width"]}, height: {self.environment_profile["browser_viewport"]["height"]}}}); }}"',
                abs_dir, env, timeout=10,
            )

            step_contracts = action_pack.get("steps", [])
            for step_index, step in enumerate(trace.steps):
                step_contract = step_contracts[step_index] if step_index < len(step_contracts) else {}
                if _is_normalized_initial_open_app_step(step_contract):
                    post_state = step_contract.get("expected_post_state", {}) if isinstance(step_contract, dict) else {}
                    expected_filename = str(post_state.get("filename") or "").strip() if isinstance(post_state, dict) else ""
                    materialized = False
                    materialize_error = ""
                    if expected_filename:
                        actual_url = await self._get_page_url(session_id, abs_dir, env)
                        expected_error = _expected_post_state_error(step_contract, actual_url)
                        if expected_error:
                            expected_path = Path(abs_dir, expected_filename)
                            if expected_path.is_file():
                                await exec_playwright(
                                    session_id,
                                    f'open "{expected_path.resolve().as_uri()}"',
                                    abs_dir,
                                    env,
                                    timeout=30,
                                )
                                materialized = True
                            else:
                                materialize_error = f"Missing expected initial app page {expected_filename}"
                    screenshot_path = None
                    if capture_screenshots:
                        screenshot_path = await self._capture_screenshot(
                            session_id, abs_dir, env, trace.trace_id, step.step_index
                        )
                    page_url = await self._get_page_url(session_id, abs_dir, env)
                    post_state_error = _expected_post_state_error(step_contract, page_url)
                    success = not post_state_error and not materialize_error
                    action_error = materialize_error or post_state_error or None
                    if action_error:
                        blocking_step = step.step_index
                    input_list.append({
                        "role": "assistant",
                        "content": (
                            f"Step {step.step_index} normalized: open_app is satisfied by "
                            f"loading replay start page {start_filename} ({start_reason})"
                            + (
                                f"; materialized expected app page {expected_filename}."
                                if materialized
                                else "."
                            )
                        ),
                    })
                    step_results.append(StepResult(
                        step_index=step.step_index,
                        action=step.action,
                        page="",
                        action_success=success,
                        action_error=action_error,
                        generated_screenshot=screenshot_path,
                        page_url=page_url,
                    ))
                    if not success:
                        break
                    continue

                deterministic_command = _deterministic_step_command(step_contract)
                if deterministic_command:
                    command_output = await exec_playwright(
                        session_id, deterministic_command, abs_dir, env, timeout=20
                    )
                    success = _browser_command_succeeded(command_output)
                    screenshot_path = None
                    if capture_screenshots:
                        screenshot_path = await self._capture_screenshot(
                            session_id, abs_dir, env, trace.trace_id, step.step_index
                        )
                    page_url = await self._get_page_url(session_id, abs_dir, env)
                    post_state_error = _expected_post_state_error(step_contract, page_url)
                    if post_state_error:
                        success = False
                        command_output = f"{command_output}; {post_state_error}"
                    input_list.append({
                        "role": "assistant",
                        "content": (
                            f"Step {step.step_index} deterministic browser action: "
                            f"`{deterministic_command}` -> {command_output}"
                        ),
                    })
                    step_results.append(StepResult(
                        step_index=step.step_index,
                        action=step.action,
                        page="",
                        action_success=success,
                        action_error=None if success else command_output,
                        generated_screenshot=screenshot_path,
                        page_url=page_url,
                    ))
                    if not success:
                        blocking_step = step.step_index
                        break
                    continue

                post_state = step_contract.get("expected_post_state", {}) if isinstance(step_contract, dict) else {}
                expected_filename = str(post_state.get("filename") or "").strip() if isinstance(post_state, dict) else ""
                if expected_filename:
                    actual_url = await self._get_page_url(session_id, abs_dir, env)
                    expected_error = _expected_post_state_error(step_contract, actual_url)
                    materialized = False
                    materialize_error = ""
                    if expected_error:
                        expected_path = Path(abs_dir, expected_filename)
                        if expected_path.is_file():
                            await exec_playwright(
                                session_id,
                                f'open "{expected_path.resolve().as_uri()}"',
                                abs_dir,
                                env,
                                timeout=30,
                            )
                            materialized = True
                        else:
                            materialize_error = f"Missing expected continuation page {expected_filename}"
                    screenshot_path = None
                    if capture_screenshots:
                        screenshot_path = await self._capture_screenshot(
                            session_id, abs_dir, env, trace.trace_id, step.step_index
                        )
                    page_url = await self._get_page_url(session_id, abs_dir, env)
                    post_state_error = _expected_post_state_error(step_contract, page_url)
                    success = not post_state_error and not materialize_error
                    action_error = materialize_error or post_state_error or None
                    input_list.append({
                        "role": "assistant",
                        "content": (
                            f"Step {step.step_index} structured continuation: "
                            f"expected `{expected_filename}`"
                            + ("; materialized expected page." if materialized else "; current page already matched.")
                        ),
                    })
                    step_results.append(StepResult(
                        step_index=step.step_index,
                        action=step.action,
                        page="",
                        action_success=success,
                        action_error=action_error,
                        generated_screenshot=screenshot_path,
                        page_url=page_url,
                    ))
                    if not success:
                        blocking_step = step.step_index
                        break
                    continue

                if _looks_like_trace_terminal_or_takeover(step_contract):
                    screenshot_path = None
                    if capture_screenshots:
                        screenshot_path = await self._capture_screenshot(
                            session_id, abs_dir, env, trace.trace_id, step.step_index
                        )
                    page_url = await self._get_page_url(session_id, abs_dir, env)
                    input_list.append({
                        "role": "assistant",
                        "content": f"Step {step.step_index} terminal/reference continuation treated as no-op success.",
                    })
                    step_results.append(StepResult(
                        step_index=step.step_index,
                        action=step.action,
                        page="",
                        action_success=True,
                        action_error=None,
                        generated_screenshot=screenshot_path,
                        page_url=page_url,
                    ))
                    continue


                step_msg = (
                    f"Execute Step {step.step_index}: {step.action}\n"
                    "Structured step contract:\n"
                    f"{json.dumps(step_contract, ensure_ascii=False, indent=2)}\n"
                    f"Start by taking a snapshot of the current page."
                )
                input_list.append({"role": "user", "content": step_msg})

                success, error, input_list = await self._execute_step_with_agent(
                    session_id, input_list, abs_dir, env
                )

                screenshot_path = None
                if capture_screenshots:
                    screenshot_path = await self._capture_screenshot(
                        session_id, abs_dir, env, trace.trace_id, step.step_index
                    )


                page_url = await self._get_page_url(session_id, abs_dir, env)

                step_results.append(StepResult(
                    step_index=step.step_index,
                    action=step.action,
                    page="",
                    action_success=success,
                    action_error=error if not success else None,
                    generated_screenshot=screenshot_path,
                    page_url=page_url,
                ))

                if not success:
                    blocking_step = step.step_index
                    break

        except Exception as e:
            self._log_error(f"Trace {trace.trace_id} replay error: {e}")
        finally:
            await cleanup_playwright_session(session_id, env, abs_dir)

        completed = sum(1 for sr in step_results if sr.action_success)
        return TraceReplayResult(
            trace_id=trace.trace_id,
            total_steps=len(trace.steps),
            completed_steps=completed,
            step_results=step_results,
            blocking_step=blocking_step,
        )

    async def _execute_step_with_agent(
        self,
        session_id: str,
        input_list: list,
        working_dir: str,
        env: dict,
    ) -> Tuple[bool, Optional[str], list]:
        max_agent_turns = 12
        bash_count = 0
        max_bash = 10

        for turn in range(1, max_agent_turns + 1):
            if bash_count >= max_bash:
                return False, f"Max bash calls ({max_bash}) exceeded without completion", input_list

            try:
                response, usage = await asyncio.wait_for(
                    call_openai_api_with_tools_async(
                        input_messages=input_list,
                        tools=OPTIMIZER_TOOLS,
                        model=self.model,
                        reasoning_effort=self.reasoning_effort,
                        stage="react_optimizer",
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                return False, f"LLM call timed out at turn {turn}", input_list

            if response is None:
                return False, "LLM returned None", input_list

            converted = _convert_output_to_input(response.output)
            input_list += converted

            function_calls = [
                item for item in response.output if item.type == "function_call"
            ]

            if not function_calls:

                input_list.append({
                    "role": "user",
                    "content": "Use the bash tool to run commands, or call report_done.",
                })
                continue

            for fc in function_calls:
                try:
                    tool_args = json.loads(fc.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                if fc.name == "bash":
                    command = tool_args.get("command", "")
                    cmd_timeout = tool_args.get("timeout", 30)
                    result_text = await execute_bash(command, working_dir, env, cmd_timeout)
                    bash_count += 1

                    input_list.append({
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": result_text,
                    })

                elif fc.name == "report_done":
                    success = tool_args.get("success", False)
                    error = tool_args.get("error", "")

                    input_list.append({
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": json.dumps({"success": success, "error": error}),
                    })
                    return success, error if not success else None, input_list

                else:
                    input_list.append({
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": f"Unknown tool: {fc.name}. Use 'bash' or 'report_done'.",
                    })

        return False, "Max agent turns exceeded", input_list

    async def _capture_screenshot(
        self,
        session_id: str,
        working_dir: str,
        env: dict,
        trace_id: str,
        step_index: int,
    ) -> Optional[str]:
        screenshots_dir = os.path.join(working_dir, "data", "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        path = os.path.join(screenshots_dir, f"{trace_id}_step{step_index:02d}.png")

        await execute_bash(
            f'playwright-cli -s={session_id} screenshot --filename="{path}"',
            working_dir, env, timeout=15,
        )
        return path if os.path.exists(path) else None

    async def _get_page_url(
        self, session_id: str, working_dir: str, env: dict
    ) -> str:
        output = await execute_bash(
            f'playwright-cli -s={session_id} run-code "async page => {{ return page.url(); }}"',
            working_dir, env, timeout=10,
        )
        url = output.strip()

        if url.startswith("(") or url.startswith("Command"):
            return ""
        return url


    async def _apply_fixes(
        self,
        website_dir: str,
        results: List[TraceReplayResult],
    ) -> Tuple[List[str], int]:

        failures = []
        affected_files = set()

        for result in results:
            for sr in result.step_results:
                if not sr.action_success and sr.action_error:
                    failure = {
                        "trace_id": result.trace_id,
                        "step_index": sr.step_index,
                        "action": sr.action,
                        "page": sr.page,
                        "error": sr.action_error,
                    }
                    failures.append(failure)


                    if sr.page_url:
                        parsed = urlparse(sr.page_url)
                        fname = os.path.basename(parsed.path)
                        if fname:
                            affected_files.add(fname)

        if not failures:
            return [], 0


        affected_files.add("index.html")
        for fname in os.listdir(website_dir):
            if fname.endswith('.js') and os.path.isfile(os.path.join(website_dir, fname)):
                affected_files.add(fname)

        files_modified = []
        total_patches = 0

        for fname in affected_files:
            fpath = os.path.join(website_dir, fname)
            if not os.path.isfile(fpath):
                continue

            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue

            if not content.strip():
                continue

            fixed_content, patch_count = await self._generate_and_apply_fix(
                fpath, content, failures
            )

            if fixed_content and fixed_content != content and patch_count > 0:
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(fixed_content)
                files_modified.append(fname)
                total_patches += patch_count

        return files_modified, total_patches

    async def _generate_and_apply_fix(
        self,
        file_path: str,
        content: str,
        failures: List[dict],
    ) -> Tuple[Optional[str], int]:
        filename = os.path.basename(file_path)


        numbered_lines = []
        for i, line in enumerate(content.split('\n'), 1):
            numbered_lines.append(f"{i:4d}| {line}")
        numbered_content = '\n'.join(numbered_lines)

        prompt = FIX_GENERATION_PROMPT.format(
            filename=filename,
            issues_json=json.dumps(failures, indent=2),
            numbered_content=numbered_content,
        )

        try:
            response, usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                max_tokens=32000,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="react_optimizer_fix",
            )

            if not response:
                return None, 0

            if isinstance(response, str):
                parsed = json.loads(response)
            else:
                parsed = response

            replacements = parsed.get("replacements", [])
            if not replacements:
                return None, 0

            modified, success, failed = self._apply_replacements(content, replacements)
            if success > 0:
                self._log_info(f"  {filename}: {success} patches applied, {failed} failed")
            return modified if success > 0 else None, success

        except Exception as e:
            self._log_error(f"Fix generation failed for {filename}: {e}")
            return None, 0

    @staticmethod
    def _apply_replacements(
        content: str, replacements: List[dict]
    ) -> Tuple[str, int, int]:
        modified = content
        success = 0
        failed = 0

        for r in replacements:
            old = r.get("old", "")
            new = r.get("new", "")

            if not old:
                failed += 1
                continue

            if old in modified:
                modified = modified.replace(old, new, 1)
                success += 1
            else:
                failed += 1

        return modified, success, failed


    async def _apply_visual_fixes(
        self,
        website_dir: str,
        visual_reports: List[VisualGapReport],
    ) -> Tuple[List[str], int]:

        page_issues: dict = {}

        for report in visual_reports:
            for comp in report.step_comparisons:
                if not comp.layout_issues and not comp.missing_elements:
                    continue


                page_name = None
                if comp.generated_screenshot:

                    basename = os.path.basename(comp.generated_screenshot)

                    parts = basename.replace('.png', '').replace('.jpg', '').split('_')

                    page_name = basename

                issues = []
                if comp.layout_issues:
                    issues.extend([{"type": "layout", "detail": i} for i in comp.layout_issues])
                if comp.missing_elements:
                    issues.extend([{"type": "missing_element", "detail": i} for i in comp.missing_elements])
                if comp.differences:
                    issues.extend([{"type": "visual_diff", "detail": i} for i in comp.differences[:3]])

                if page_name not in page_issues:
                    page_issues[page_name] = []
                page_issues[page_name].extend(issues)

        if not page_issues:
            self._log_info("  No visual issues to fix")
            return [], 0

        total_issues = sum(len(v) for v in page_issues.values())
        self._log_info(f"  Found {total_issues} visual issues across {len(page_issues)} screenshot groups")


        css_files = []
        for fname in os.listdir(website_dir):
            fpath = os.path.join(website_dir, fname)
            if os.path.isfile(fpath) and (fname.endswith('.css') or fname.endswith('.html')):
                css_files.append(fname)

        if not css_files:
            self._log_info("  No CSS/HTML files found to patch")
            return [], 0


        all_issues = []
        for page_name, issues in page_issues.items():
            for issue in issues:
                all_issues.append({
                    "screenshot": page_name,
                    **issue,
                })

        files_modified = []
        total_patches = 0

        for fname in css_files:
            fpath = os.path.join(website_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue

            if not content.strip():
                continue

            fixed_content, patch_count = await self._generate_visual_fix(
                fpath, content, all_issues
            )

            if fixed_content and fixed_content != content and patch_count > 0:
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(fixed_content)
                files_modified.append(fname)
                total_patches += patch_count

        return files_modified, total_patches

    async def _generate_visual_fix(
        self,
        file_path: str,
        content: str,
        visual_issues: List[dict],
    ) -> Tuple[Optional[str], int]:
        filename = os.path.basename(file_path)


        numbered_lines = []
        for i, line in enumerate(content.split('\n'), 1):
            numbered_lines.append(f"{i:4d}| {line}")
        numbered_content = '\n'.join(numbered_lines)

        prompt = VISUAL_FIX_PROMPT.format(
            reference_label=(
                "the real mobile app"
                if self.environment_profile["platform"] == "mobile"
                else "the real desktop web/application UI"
            ),
            environment_contract=self.visual_prompt_policy["rendered_section"],
            filename=filename,
            issues_json=json.dumps(visual_issues, indent=2),
            numbered_content=numbered_content,
        )

        try:
            response, usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                max_tokens=32000,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="react_optimizer_visual_fix",
            )

            if not response:
                return None, 0

            if isinstance(response, str):
                parsed = json.loads(response)
            else:
                parsed = response

            replacements = parsed.get("replacements", [])
            if not replacements:
                return None, 0

            modified, success, failed = self._apply_replacements(content, replacements)
            if success > 0:
                self._log_info(f"  {filename}: {success} visual patches applied, {failed} failed")
            return modified if success > 0 else None, success

        except Exception as e:
            self._log_error(f"Visual fix generation failed for {filename}: {e}")
            return None, 0


    async def _run_visual_comparisons(
        self,
        traces: List[TraceData],
        replay_results: List[TraceReplayResult],
    ) -> List[VisualGapReport]:

        all_tasks = []

        for trace, result in zip(traces, replay_results):
            step_by_index = {
                int(getattr(step, "step_index", -1)): step
                for step in getattr(trace, "steps", []) or []
            }
            for step_result in result.step_results:
                step = step_by_index.get(int(step_result.step_index))
                if step is None:
                    continue
                ref_path = _reference_post_screenshot_path(trace, step_result)
                gen_path = step_result.generated_screenshot

                if gen_path and os.path.exists(gen_path) and os.path.exists(ref_path):
                    all_tasks.append((
                        trace.trace_id,
                        step.step_index,
                        ref_path,
                        gen_path,
                        f"Step {step.step_index}: {step.action}",
                    ))

        if not all_tasks:
            self._log_info("  No screenshot pairs to compare")
            return []

        self._log_info(f"  Comparing {len(all_tasks)} screenshot pairs")

        async def compare_one(task):
            trace_id, step_idx, ref_path, gen_path, context = task
            return (trace_id, step_idx, ref_path, gen_path,
                    await self._compare_single_step(ref_path, gen_path, context))

        comparison_results = await asyncio.gather(
            *[compare_one(t) for t in all_tasks],
            return_exceptions=True,
        )


        trace_comparisons: dict = {}
        for item in comparison_results:
            if isinstance(item, Exception):
                self._log_warning(f"  Visual comparison failed: {item}")
                continue

            trace_id, step_idx, ref_path, gen_path, comparison = item
            if trace_id not in trace_comparisons:
                trace_comparisons[trace_id] = []

            trace_comparisons[trace_id].append(VisualStepComparison(
                step_index=step_idx,
                reference_screenshot=ref_path,
                generated_screenshot=gen_path,
                visual_score=comparison.get("visual_score", 0.0),
                differences=comparison.get("differences", []),
                missing_elements=comparison.get("missing_elements", []),
                layout_issues=comparison.get("layout_issues", []),
            ))


        reports = []
        for trace_id, comparisons in trace_comparisons.items():
            scores = [c.visual_score for c in comparisons]
            avg = sum(scores) / len(scores) if scores else 0.0
            reports.append(VisualGapReport(
                trace_id=trace_id,
                step_comparisons=comparisons,
                avg_visual_score=avg,
            ))

        return reports

    async def _compare_single_step(
        self,
        ref_path: str,
        gen_path: str,
        context: str,
    ) -> dict:
        try:
            ref_url = encode_image(ref_path)
            gen_url = encode_image(gen_path)
        except Exception as e:
            return {"visual_score": 0.0, "differences": [f"Image encoding error: {e}"],
                    "missing_elements": [], "layout_issues": []}

        platform = self.environment_profile["platform"]
        prompt = VISUAL_COMPARISON_PROMPT.format(
            action=context,
            platform_label="mobile" if platform == "mobile" else "desktop application",
            reference_label=(
                "real mobile app" if platform == "mobile" else "real desktop web/application UI"
            ),
            viewport_width=self.environment_profile["browser_viewport"]["width"],
            viewport_height=self.environment_profile["browser_viewport"]["height"],
            environment_contract=self.visual_prompt_policy["rendered_section"],
        )

        messages = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": ref_url},
                {"type": "input_image", "image_url": gen_url},
            ],
        }]

        try:
            response, usage = await call_openai_api_async(
                messages,
                max_tokens=4000,
                response_format={"type": "json_object"},
                model=self.vision_model,
                reasoning_effort=self.reasoning_effort,
                stage="react_optimizer_visual",
            )

            if not response:
                return {"visual_score": 0.0, "differences": ["No LLM response"],
                        "missing_elements": [], "layout_issues": []}

            if isinstance(response, str):
                return json.loads(response)
            return response

        except Exception as e:
            return {"visual_score": 0.0, "differences": [f"Comparison error: {e}"],
                    "missing_elements": [], "layout_issues": []}
