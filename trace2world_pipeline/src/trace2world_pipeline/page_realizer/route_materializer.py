
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_TAG_WITH_DATA_ELEMENT_RE = re.compile(
    r'<(\w+)\b([^>]*\bdata-element-id\s*=\s*"([^"]+)"[^>]*)>',
    re.DOTALL,
)
_PAIR_HOST_RE = re.compile(
    r"<(?P<tag>a|button|div|span|li)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_ANCHOR_HOST_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

_DATA_GHOSTWEB_ACTION_RE = re.compile(
    r'\bdata-ghostweb-action\s*=\s*"[^"]*"'
)
_DATA_GHOSTWEB_TARGET_RE = re.compile(
    r'\bdata-ghostweb-target\s*=\s*"[^"]*"'
)
_DATA_GHOSTWEB_ACTION_ID_RE = re.compile(
    r'\bdata-ghostweb-action-id\s*=\s*"[^"]*"'
)

_HREF_RE = re.compile(r'\bhref\s*=\s*"([^"]*)"')

_INLINE_ROUTE_JS_RE = re.compile(
    r"""(?:onclick|onmousedown|onmouseup)\s*=\s*"[^"]*"""
    r"""GhostwebRouteExecution\.(?:navigate|returnBack|goBack)\s*\("""
    r"""[^"]*" """,
    re.VERBOSE,
)

_PAGE_LOCAL_ROUTE_EXECUTION_RE = re.compile(
    r'<script[^>]*>(?:(?!data-ghostweb-runtime)[\s\S])*'
    r'window\.GhostwebRouteExecution\s*=\s*\{[\s\S]*?</script>',
    re.DOTALL,
)
_ROUTE_RUNTIME_SCRIPT_RE = re.compile(
    r'\s*<script\b[^>]*\bsrc=["\'][^"\']*route_runtime\.js(?:\?[^"\']*)?["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)
_ROUTE_MANIFEST_SCRIPT_RE = re.compile(
    r'\s*<script\b[^>]*\bsrc=["\'][^"\']*route_manifest\.js(?:\?[^"\']*)?["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)
_ROUTE_RUNTIME_OBLIGATION_RE = re.compile(
    r"GhostwebRouteExecution\.|data-ghostweb-(?:action|target|route-action)\s*=",
    re.IGNORECASE,
)
_SCRIPT_REFERENCING_ROUTE_RUNTIME_RE = re.compile(
    r"<script\b(?:(?!</script>).)*GhostwebRouteExecution\.(?:(?!</script>).)*</script>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_WITH_DATA_ELEMENT_ALIASES_RE = re.compile(
    r'<(?P<tag>\w+)\b(?P<attrs>[^>]*\bdata-element-aliases\s*=\s*"[^"]*"[^>]*)>',
    re.DOTALL,
)
_MATERIALIZED_ROUTE_SHELL_RE = re.compile(
    r'\s*<nav\b'
    r'(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bghostweb-route-shell\b[^"\']*["\'])'
    r'(?=[^>]*\bdata-ghostweb-materialized\s*=\s*["\']route_contract["\'])'
    r'[^>]*>[\s\S]*?</nav>\s*',
    re.IGNORECASE,
)
_DECLARED_RETURN_HOST_RE = re.compile(
    r'\bdata-ghostweb-action\s*=\s*["\'](?:history_back|returnBack)["\']'
    r'|\bdata-ghostweb-route-action\s*=\s*["\']default_back["\']',
    re.IGNORECASE,
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)

_CANONICAL_ROUTE_RUNTIME_SCRIPTS = """\
<script data-ghostweb-runtime="route-manifest" src="../../phase_sdk_generation/runtime/route_manifest.js"></script>
<script data-ghostweb-runtime="route-execution" src="../../phase_sdk_generation/runtime/route_runtime.js"></script>"""

_SHARED_CLICK_LISTENER_JS = """\
<script data-ghostweb-runtime="route-materializer">
(function() {
  document.addEventListener("click", function(event) {
    var host = event.target.closest("[data-ghostweb-action]");
    if (!host) return;
    var action = host.getAttribute("data-ghostweb-action");
    if (action === "navigate") {
      event.preventDefault();
      var target = host.getAttribute("data-ghostweb-target");
      if (target && window.GhostwebRouteExecution && window.GhostwebRouteExecution.navigate) {
        window.GhostwebRouteExecution.navigate(target, {
          mode: host.getAttribute("data-ghostweb-history-mode") || "push"
        });
      }
    }
    if (action === "history_back") {
      event.preventDefault();
      var actionId = host.getAttribute("data-ghostweb-action-id") || "fallback_back";
      if (window.GhostwebRouteExecution && window.GhostwebRouteExecution.returnBack) {
        window.GhostwebRouteExecution.returnBack(actionId);
      } else if (window.GhostwebRouteExecution && window.GhostwebRouteExecution.goBack) {
        window.GhostwebRouteExecution.goBack();
      }
    }
  });
})();
</script>"""


def ensure_route_runtime_owner(html: str) -> str:
    if not html or not _ROUTE_RUNTIME_OBLIGATION_RE.search(html):
        return html

    without_route_scripts = _ROUTE_RUNTIME_SCRIPT_RE.sub("", html)
    without_route_scripts = _ROUTE_MANIFEST_SCRIPT_RE.sub("", without_route_scripts)
    if _CANONICAL_ROUTE_RUNTIME_SCRIPTS in without_route_scripts:
        return without_route_scripts

    first_route_script = _SCRIPT_REFERENCING_ROUTE_RUNTIME_RE.search(without_route_scripts)
    if first_route_script:
        insert_at = first_route_script.start()
        return (
            without_route_scripts[:insert_at]
            + _CANONICAL_ROUTE_RUNTIME_SCRIPTS
            + "\n"
            + without_route_scripts[insert_at:]
        )

    body_close = re.search(r"</body\s*>", without_route_scripts, re.IGNORECASE)
    if body_close:
        insert_at = body_close.start()
        return (
            without_route_scripts[:insert_at]
            + _CANONICAL_ROUTE_RUNTIME_SCRIPTS
            + "\n"
            + without_route_scripts[insert_at:]
        )

    return without_route_scripts.rstrip() + "\n" + _CANONICAL_ROUTE_RUNTIME_SCRIPTS + "\n"


def _inject_attr(attrs_str: str, attr_name: str, attr_value: str) -> str:
    pattern = re.compile(rf'\b{re.escape(attr_name)}\s*=\s*"[^"]*"')
    if pattern.search(attrs_str):
        return pattern.sub(f'{attr_name}="{attr_value}"', attrs_str)
    return attrs_str.rstrip() + f' {attr_name}="{attr_value}"'


def _remove_attr(attrs_str: str, attr_name: str) -> str:
    pattern = re.compile(rf'\s*\b{re.escape(attr_name)}\s*=\s*"[^"]*"')
    return pattern.sub("", attrs_str or "")


def _enable_authoritative_route_host(attrs_str: str) -> str:
    attrs = _remove_attr(attrs_str, "aria-disabled")
    attrs = re.sub(
        r"\s+\bdisabled(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?",
        "",
        attrs,
        flags=re.IGNORECASE,
    )
    if _attr_value(attrs, "data-ghostweb-click-contract-kind") == "noninteractive":
        attrs = _remove_attr(attrs, "data-ghostweb-click-contract-kind")
    return attrs


def _attr_value(attrs_str: str, attr_name: str) -> str:
    match = re.search(
        rf'\b{re.escape(attr_name)}\s*=\s*"([^"]*)"',
        attrs_str or "",
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _escape_attr(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_text(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inject_data_element_identity(attrs_str: str, component_id: str) -> str:
    existing = _attr_value(attrs_str, "data-element-id")
    if not existing:
        return _inject_attr(attrs_str, "data-element-id", component_id)
    if existing == component_id:
        return attrs_str
    aliases = [
        token
        for token in re.split(r"\s+", _attr_value(attrs_str, "data-element-aliases"))
        if token
    ]
    if component_id not in aliases:
        aliases.append(component_id)
    return _inject_attr(attrs_str, "data-element-aliases", " ".join(aliases))


def _remove_component_aliases_from_other_hosts(html: str, component_id: str) -> Tuple[str, bool]:
    if not component_id:
        return html, False
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        tag = match.group("tag")
        attrs = match.group("attrs") or ""
        if _attr_value(attrs, "data-element-id") == component_id:
            return match.group(0)
        aliases = [
            token
            for token in re.split(r"\s+", _attr_value(attrs, "data-element-aliases"))
            if token
        ]
        if component_id not in aliases:
            return match.group(0)
        aliases = [token for token in aliases if token != component_id]
        if aliases:
            attrs = _inject_attr(attrs, "data-element-aliases", " ".join(aliases))
        else:
            attrs = _remove_attr(attrs, "data-element-aliases")
        changed = True
        return f"<{tag}{attrs}>"

    return _TAG_WITH_DATA_ELEMENT_ALIASES_RE.sub(repl, html), changed


def _strip_materialized_route_shell_fragments(html: str) -> Tuple[str, int]:
    return _MATERIALIZED_ROUTE_SHELL_RE.subn("\n", html or "")


def _visible_text(html_fragment: str) -> str:
    text = re.sub(r"<script\b[\s\S]*?</script>", " ", html_fragment or "", flags=re.IGNORECASE)
    text = re.sub(r"<style\b[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", "", text)


def _find_action_slot_host_by_label_or_target(html: str, slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    label = re.sub(r"\s+", "", str(slot.get("control_label") or slot.get("label") or ""))
    target_filename = str(slot.get("target_filename") or "")
    if not label and not target_filename:
        return None
    is_shell_slot = _is_shell_route_slot(slot)
    shell_slot = str(slot.get("shell_slot_key") or "").strip()
    if is_shell_slot and shell_slot:
        for match in _PAIR_HOST_RE.finditer(html or ""):
            attrs = match.group("attrs") or ""
            if _attr_value(attrs, "data-ghostweb-shell-slot") != shell_slot:
                continue
            return {
                "tag": match.group("tag"),
                "attrs_str": attrs,
                "full_match": match.group(0),
                "start": match.start(),
                "end": match.end(),
            }
    for match in _ANCHOR_HOST_RE.finditer(html or ""):
        attrs = match.group("attrs") or ""
        body = match.group("body") or ""
        text = _visible_text(body)
        existing_shell_slot = _attr_value(attrs, "data-ghostweb-shell-slot")
        if is_shell_slot and shell_slot and existing_shell_slot and existing_shell_slot != shell_slot:
            continue
        target_matches = bool(target_filename and target_filename in attrs)
        label_matches = bool(label and (label in text or label in attrs))
        href = _attr_value(attrs, "href").strip()
        declared_target = _attr_value(attrs, "data-ghostweb-target").strip()
        placeholder_target = not declared_target and href in {
            "",
            "#",
            "javascript:void(0)",
            "javascript:void(0);",
        }
        shell_placeholder_matches = bool(
            is_shell_slot and label_matches and placeholder_target
        )
        if target_filename and not target_matches and not shell_placeholder_matches:
            continue
        if label and not label_matches and not (is_shell_slot and target_matches):
            continue
        return {
            "tag": "a",
            "attrs_str": attrs,
            "full_match": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for match in _PAIR_HOST_RE.finditer(html or ""):
        attrs = match.group("attrs") or ""
        existing_shell_slot = _attr_value(attrs, "data-ghostweb-shell-slot")
        if is_shell_slot and shell_slot and existing_shell_slot and existing_shell_slot != shell_slot:
            continue
        body = match.group("body") or ""
        text = _visible_text(body)
        score = 0
        if label and label in text:
            score += 100
        if target_filename and target_filename in attrs:
            score += 160
        if score <= 0:
            continue
        tag = match.group("tag")
        if str(tag).lower() in {"a", "button"}:
            score += 30
        record = {
            "tag": tag,
            "attrs_str": attrs,
            "full_match": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
        if best is None or score > best[0]:
            best = (score, record)
    return best[1] if best else None


def _is_shell_route_slot(slot: Dict[str, Any]) -> bool:
    shell_family = str(slot.get("shell_family_id") or "").strip()
    if shell_family:
        return True
    binding_source = str(slot.get("binding_source") or "").strip()
    binding_sources = {
        str(source).strip()
        for source in slot.get("binding_sources", [])
        if str(source).strip()
    }
    return binding_source == "shell_interaction_broadcast" or "shell_interaction_broadcast" in binding_sources


def _materialize_missing_shell_route_host(html: str, slot: Dict[str, Any]) -> str:
    component_id = str(slot.get("component_id") or "").strip()
    target_filename = str(slot.get("target_filename") or "").strip()
    if not component_id or not target_filename or not _is_shell_route_slot(slot):
        return html

    action_slot_id = str(slot.get("action_slot_id") or component_id)
    label = (
        str(slot.get("control_label") or slot.get("label") or "").strip()
        or str(slot.get("operation_intent") or "").strip()
        or "Navigate"
    )
    shell_family = str(slot.get("shell_family_id") or "shared_shell").strip()
    shell_slot = str(slot.get("shell_slot_key") or "").strip()
    history_mode = str(slot.get("history_mode") or "push").strip() or "push"
    family_class = re.sub(r"[^A-Za-z0-9_-]+", "-", shell_family).strip("-") or "shared-shell"

    if shell_family == "bottom_tab_bar":
        shell_style = (
            "position:fixed;left:0;right:0;bottom:13px;height:58px;"
            "display:flex;align-items:center;justify-content:center;"
            "background:#fff;border-top:1px solid rgba(0,0,0,.08);"
            "z-index:80;"
        )
        host_style = (
            "min-width:72px;min-height:44px;padding:6px 12px;"
            "display:flex;align-items:center;justify-content:center;"
            "font-size:12px;color:#111827;background:transparent;border:0;"
        )
    else:
        shell_style = (
            "position:fixed;left:12px;right:12px;bottom:13px;"
            "display:flex;align-items:center;justify-content:center;"
            "z-index:80;pointer-events:auto;"
        )
        host_style = (
            "min-width:88px;min-height:44px;padding:8px 14px;"
            "display:flex;align-items:center;justify-content:center;"
            "font-size:14px;color:#111827;background:#fff;"
            "border:1px solid rgba(0,0,0,.08);border-radius:8px;"
        )

    fragment = (
        f'\n<nav class="ghostweb-route-shell ghostweb-route-shell-{_escape_attr(family_class)}" '
        f'data-ghostweb-shell-family="{_escape_attr(shell_family)}" '
        f'data-ghostweb-materialized="route_contract" '
        f'style="{_escape_attr(shell_style)}" aria-label="Shared navigation">\n'
        f'  <a href="{_escape_attr(target_filename)}" '
        f'data-element-id="{_escape_attr(component_id)}" '
        f'data-ghostweb-action="navigate" '
        f'data-ghostweb-target="{_escape_attr(target_filename)}" '
        f'data-ghostweb-action-id="{_escape_attr(action_slot_id)}" '
        f'data-ghostweb-history-mode="{_escape_attr(history_mode)}" '
        f'data-ghostweb-shell-slot="{_escape_attr(shell_slot)}" '
        f'style="{_escape_attr(host_style)}">{_escape_text(label)}</a>\n'
        "</nav>\n"
    )
    body_close = re.search(r"</body\s*>", html or "", re.IGNORECASE)
    if body_close:
        return html[: body_close.start()] + fragment + html[body_close.start() :]
    return (html or "").rstrip() + fragment


def _visible_return_component_id(source_filename: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]+", "_", str(source_filename or "")).strip("_")
    return f"ghostweb_route_return__{token or 'page'}"


def _materialize_missing_visible_return_host(
    html: str,
    *,
    source_filename: str,
    target_filename: str,
) -> str:


    non_script_html = _SCRIPT_BLOCK_RE.sub("", html or "")
    if not source_filename or not target_filename or _DECLARED_RETURN_HOST_RE.search(non_script_html):
        return html
    component_id = _visible_return_component_id(source_filename)
    fragment = (
        '\n<button type="button" '
        'class="ghostweb-visible-return-affordance" '
        f'data-element-id="{_escape_attr(component_id)}" '
        'data-ghostweb-materialized="visible_return_affordance" '
        'data-ghostweb-action="history_back" '
        'data-ghostweb-action-id="fallback_back" '
        'data-history-back="true" '
        'aria-label="Return to previous page" '
        'title="Return to previous page" '
        'style="position:fixed;top:max(12px,env(safe-area-inset-top));left:12px;'
        'width:44px;height:44px;padding:0;border:0;border-radius:22px;'
        'background:rgba(255,255,255,.96);color:#111827;font-size:28px;line-height:44px;'
        'text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:2147483000;'
        'cursor:pointer">&#8592;</button>\n'
    )
    body_close = re.search(r"</body\s*>", html or "", re.IGNORECASE)
    if body_close:
        return html[: body_close.start()] + fragment + html[body_close.start() :]
    return (html or "").rstrip() + fragment


def materialize_visible_return_affordances(
    *,
    route_manifest: Dict[str, Any],
    generated_pages_dir: Path,
    write_modified: bool = True,
    included_page_filenames: List[str] | None = None,
) -> Dict[str, Any]:
    pages = route_manifest.get("pages") if isinstance(route_manifest, dict) else None
    if not isinstance(pages, dict):
        raise ValueError("route manifest pages must be an object")

    included_pages = set(included_page_filenames or [])
    bindings: List[Dict[str, Any]] = []
    for source_filename, page_contract in sorted(pages.items()):
        if included_pages and str(source_filename) not in included_pages:
            continue
        if not isinstance(page_contract, dict):
            continue
        target_filename = str(page_contract.get("back_target") or "").strip()
        if not target_filename:
            continue
        source_path = generated_pages_dir / str(source_filename)
        target_path = generated_pages_dir / target_filename
        row: Dict[str, Any] = {
            "source_filename": str(source_filename),
            "target_filename": target_filename,
            "action_id": "fallback_back",
            "executor": "GhostwebRouteExecution.returnBack",
            "component_id": _visible_return_component_id(str(source_filename)),
        }
        if not source_path.is_file():
            row.update(status="source_page_missing", reason="source_page_not_materialized")
        elif not target_path.is_file():
            row.update(status="target_page_missing", reason="fallback_target_not_materialized")
        else:
            original = source_path.read_text(encoding="utf-8")
            updated = _materialize_missing_visible_return_host(
                original,
                source_filename=str(source_filename),
                target_filename=target_filename,
            )
            if updated == original:
                row.update(status="existing_declared_return_host", reason="")
            else:
                if write_modified:
                    source_path.write_text(updated, encoding="utf-8")
                row.update(status="materialized", reason="")
        bindings.append(row)

    materialized = sum(row["status"] == "materialized" for row in bindings)
    existing = sum(row["status"] == "existing_declared_return_host" for row in bindings)
    missing = sum(row["status"] in {"source_page_missing", "target_page_missing"} for row in bindings)
    return {
        "format": "ghostweb_visible_return_affordance_materialization",
        "schema_revision": 1,
        "source": "frontend.route_materializer.materialize_visible_return_affordances",
        "summary": {
            "declared_fallback_count": len(bindings),
            "materialized_count": materialized,
            "existing_declared_return_host_count": existing,
            "missing_page_count": missing,
        },
        "bindings": bindings,
    }


def _strip_inline_route_js(attrs_str: str) -> str:
    return _INLINE_ROUTE_JS_RE.sub("", attrs_str)


def _find_host_records(html: str) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for m in _TAG_WITH_DATA_ELEMENT_RE.finditer(html):
        tag = m.group(1)
        attrs_str = m.group(2)
        element_id = m.group(3)
        if element_id not in records:
            records[element_id] = {
                "tag": tag,
                "attrs_str": attrs_str,
                "full_match": m.group(0),
                "start": m.start(),
                "end": m.end(),
            }
    return records


def _action_from_executor(executor: str) -> str:
    if "returnBack" in executor or "goBack" in executor:
        return "history_back"
    if "navigate" in executor:
        return "navigate"
    return ""


def _build_materialization_bindings(
    *,
    step_transition_contracts: Dict[str, Any],
    action_slot_registry: Optional[Dict[str, Any]],
    generated_pages_dir: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    bindings: List[Dict[str, Any]] = []
    modified_pages: Dict[str, str] = {}

    contracts = step_transition_contracts.get("contracts", [])
    route_contracts = [
        c for c in contracts
        if (
            c.get("boundary_decision")
            in {
                "route_transition",
                "materialized_page_split",
                "same_filename_materialized_page_transition",
            }
            and c.get("smoke_required")
            and c.get("url_resolution_rule") != "none"
        )
    ]

    boundary_by_action_id = {}
    for c in contracts:
        step_id = str(c.get("step_id") or "")
        decision = str(c.get("boundary_decision") or "")
        if not step_id:
            continue
        boundary_by_action_id[step_id] = decision
        if ":step" in step_id:
            trace_id, raw_step = step_id.rsplit(":step", 1)
            boundary_by_action_id.setdefault(f"{trace_id}:{raw_step}", decision)
        elif ":" in step_id:
            trace_id, raw_step = step_id.rsplit(":", 1)
            if raw_step.isdigit():
                boundary_by_action_id.setdefault(f"{trace_id}:step{raw_step}", decision)

    slots_by_source: Dict[str, List[Dict]] = {}
    if action_slot_registry:
        slots_by_source = action_slot_registry.get("slots_by_source_filename", {})

    contracts_by_source: Dict[str, List[Dict]] = {}
    for c in route_contracts:
        src = c.get("source_filename", "")
        if src:
            contracts_by_source.setdefault(src, []).append(c)

    all_sources = set(contracts_by_source.keys()) | set(slots_by_source.keys())

    for source_filename in sorted(all_sources):
        html_path = generated_pages_dir / source_filename
        if not html_path.exists():
            for c in contracts_by_source.get(source_filename, []):
                bindings.append({
                    "step_id": c.get("step_id", ""),
                    "source_filename": source_filename,
                    "host_selector": "",
                    "target_filename": c.get("expected_target_filename", ""),
                    "executor": c.get("executor", ""),
                    "status": "page_missing",
                    "reason": f"generated page not found: {source_filename}",
                })
            continue

        html = html_path.read_text(encoding="utf-8")
        html, stripped_shell_count = _strip_materialized_route_shell_fragments(html)
        host_records = _find_host_records(html)
        modified = stripped_shell_count > 0

        source_slots = slots_by_source.get(source_filename, [])
        slot_by_component: Dict[str, Dict] = {}
        for slot in source_slots:
            slot_action_id = str(
                slot.get("step_key")
                or slot.get("evidence_action_id")
                or slot.get("action_slot_id")
                or ""
            )
            if boundary_by_action_id.get(slot_action_id) == "runtime_projection":
                continue
            cid = slot.get("component_id", "")
            if cid:
                slot_by_component[cid] = slot

        source_contracts = contracts_by_source.get(source_filename, [])
        processed_targets: set = set()

        for c in source_contracts:
            step_id = c.get("step_id", "")
            target_filename = c.get("expected_target_filename", "")
            executor = c.get("executor", "")
            action = _action_from_executor(executor)

            matched_host = None
            matched_cid = None

            for cid, slot in slot_by_component.items():
                if slot.get("target_filename") == target_filename:
                    if cid in host_records:
                        matched_host = host_records[cid]
                        matched_cid = cid
                        break

            if not matched_host:
                for cid, record in host_records.items():
                    if cid in processed_targets:
                        continue
                    attrs = record["attrs_str"]
                    if target_filename and target_filename in attrs:
                        matched_host = record
                        matched_cid = cid
                        break

            if not matched_host:
                bindings.append({
                    "step_id": step_id,
                    "source_filename": source_filename,
                    "host_selector": "",
                    "target_filename": target_filename,
                    "executor": executor,
                    "status": "host_not_found",
                    "reason": f"no host candidate for {target_filename} in {source_filename}",
                })
                continue

            new_attrs = matched_host["attrs_str"]
            if action:
                new_attrs = _inject_attr(new_attrs, "data-ghostweb-action", action)
            if action == "history_back":
                new_attrs = _inject_attr(new_attrs, "data-history-back", "true")
            elif target_filename:
                new_attrs = _inject_attr(new_attrs, "data-ghostweb-target", target_filename)
            if step_id:
                new_attrs = _inject_attr(new_attrs, "data-ghostweb-action-id", step_id)

            new_attrs = _enable_authoritative_route_host(new_attrs)

            new_attrs = _strip_inline_route_js(new_attrs)

            if matched_host["tag"].lower() == "a" and target_filename:
                new_attrs = _HREF_RE.sub(f'href="{target_filename}"', new_attrs)

            if new_attrs != matched_host["attrs_str"]:
                old_tag = f'<{matched_host["tag"]}{matched_host["attrs_str"]}>'
                new_tag = f'<{matched_host["tag"]}{new_attrs}>'
                html = html.replace(old_tag, new_tag, 1)
                modified = True

            processed_targets.add(matched_cid or "")

            bindings.append({
                "step_id": step_id,
                "source_filename": source_filename,
                "host_selector": f'[data-element-id="{matched_cid}"]' if matched_cid else "",
                "target_filename": target_filename,
                "executor": executor,
                "status": "bound",
                "reason": "",
            })


        host_records = _find_host_records(html)

        for cid, slot in slot_by_component.items():
            target_filename = slot.get("target_filename", "")
            slot_action_id = slot.get("action_slot_id", cid)
            slot_boundary_action_id = str(
                slot.get("step_key")
                or slot.get("evidence_action_id")
                or slot_action_id
            )
            if boundary_by_action_id.get(slot_boundary_action_id) == "runtime_projection":
                continue
            matching_route_contract = next(
                (
                    contract
                    for contract in source_contracts
                    if str(contract.get("step_id") or "") == slot_boundary_action_id
                    or (
                        target_filename
                        and contract.get("expected_target_filename") == target_filename
                    )
                ),
                {},
            )
            host = host_records.get(cid)
            materialized_identity = False
            if cid in processed_targets and host:
                current_action = _attr_value(host["attrs_str"], "data-ghostweb-action")
                current_target = (
                    _attr_value(host["attrs_str"], "data-ghostweb-target")
                    or _attr_value(host["attrs_str"], "href")
                )
                if current_action == "history_back" or (
                    target_filename and current_target == target_filename
                ):
                    continue
            if not host:
                host = _find_action_slot_host_by_label_or_target(html, slot)
                materialized_identity = bool(host)
            if not host and _is_shell_route_slot(slot):
                updated_html = _materialize_missing_shell_route_host(html, slot)
                if updated_html != html:
                    html = updated_html
                    host_records = _find_host_records(html)
                    host = host_records.get(cid)
                    modified = True
            if not host:
                bindings.append({
                    "step_id": slot_action_id,
                    "source_filename": source_filename,
                    "host_selector": "",
                    "target_filename": target_filename,
                    "executor": slot.get("executor", "GhostwebRouteExecution.navigate"),
                    "status": "host_not_found",
                    "reason": f"no action slot host candidate for {cid} in {source_filename}",
                    "source": "action_slot_registry",
                })
                continue
            if not target_filename:
                bindings.append({
                    "step_id": slot_action_id,
                    "source_filename": source_filename,
                    "host_selector": f'[data-element-id="{cid}"]',
                    "target_filename": "",
                    "executor": slot.get("executor", "GhostwebRouteExecution.navigate"),
                    "status": "target_missing",
                    "reason": f"action slot {slot_action_id} has no target_filename",
                    "source": "action_slot_registry",
                })
                continue

            new_attrs = host["attrs_str"]
            if materialized_identity:
                new_attrs = _inject_data_element_identity(new_attrs, cid)
            slot_action = _action_from_executor(
                str(matching_route_contract.get("executor") or slot.get("executor") or "")
            ) or "navigate"
            new_attrs = _inject_attr(new_attrs, "data-ghostweb-action", slot_action)
            if slot_action == "history_back":
                new_attrs = _inject_attr(new_attrs, "data-history-back", "true")
                new_attrs = _remove_attr(new_attrs, "data-ghostweb-target")
            else:
                new_attrs = _inject_attr(new_attrs, "data-ghostweb-target", target_filename)
            new_attrs = _inject_attr(new_attrs, "data-ghostweb-action-id", slot_action_id)
            new_attrs = _enable_authoritative_route_host(new_attrs)
            new_attrs = _strip_inline_route_js(new_attrs)

            if host["tag"].lower() == "a" and slot_action != "history_back":
                new_attrs = _HREF_RE.sub(f'href="{target_filename}"', new_attrs)

            if new_attrs != host["attrs_str"]:
                old_tag = f'<{host["tag"]}{host["attrs_str"]}>'
                new_tag = f'<{host["tag"]}{new_attrs}>'
                html = html.replace(old_tag, new_tag, 1)
                modified = True
            html, alias_deduped = _remove_component_aliases_from_other_hosts(html, cid)
            modified = modified or alias_deduped

            processed_targets.add(cid)
            bindings.append({
                "step_id": slot_action_id,
                "source_filename": source_filename,
                "host_selector": f'[data-element-id="{cid}"]',
                "target_filename": target_filename,
                "executor": slot.get("executor", "GhostwebRouteExecution.navigate"),
                "status": "bound",
                "reason": "bound_by_label_or_target" if materialized_identity else "",
                "source": "action_slot_registry",
            })

        if modified:
            if "</body>" in html:
                html = html.replace("</body>", f"{_SHARED_CLICK_LISTENER_JS}\n</body>", 1)
            else:
                html += "\n" + _SHARED_CLICK_LISTENER_JS
            modified_pages[source_filename] = html

    return bindings, modified_pages


def materialize_route_execution(
    *,
    step_transition_contracts: Dict[str, Any],
    action_slot_registry: Optional[Dict[str, Any]] = None,
    generated_pages_dir: Path,
    write_modified: bool = True,
) -> Dict[str, Any]:
    bindings, modified_pages = _build_materialization_bindings(
        step_transition_contracts=step_transition_contracts,
        action_slot_registry=action_slot_registry,
        generated_pages_dir=generated_pages_dir,
    )

    if write_modified:
        for filename, html in modified_pages.items():
            path = generated_pages_dir / filename
            path.write_text(html, encoding="utf-8")

    bound = sum(1 for b in bindings if b["status"] == "bound")
    host_not_found = sum(1 for b in bindings if b["status"] == "host_not_found")
    page_missing = sum(1 for b in bindings if b["status"] == "page_missing")
    target_missing = sum(1 for b in bindings if b["status"] == "target_missing")

    return {
        "format": "route_materialization_report",
        "schema_revision": 1,
        "summary": {
            "total_route_bindings": len(bindings),
            "bound": bound,
            "host_not_found": host_not_found,
            "page_missing": page_missing,
            "target_missing": target_missing,
            "modified_pages": len(modified_pages),
        },
        "bindings": bindings,
        "modified_pages": sorted(modified_pages.keys()),
    }
