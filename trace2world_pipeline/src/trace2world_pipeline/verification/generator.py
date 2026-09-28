
import json
import re
from typing import Dict, Any, List
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.verification.data_models import InstrumentationPlan


class Trace2WorldInstrumentationGenerator:

    def __init__(self, logger: Trace2WorldLogger = None, max_retries: int = 3,
                 reasoning_effort: str = "medium", model: str = None):
        self.logger = logger or Trace2WorldLogger()
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.model = model

    async def generate(self,
                      instrumentation_plan: InstrumentationPlan,
                      original_code: str,
                      test_data: Dict[str, Any]) -> str:
        self.logger.start_stage("Generate Instrumented Code")
        self.logger.log_info("🛠️ Generating instrumented code...")

        try:

            self.logger.log_info("Adding instrumentation to business logic...")
            instrumented_code = await self._generate_instrumented_code(
                instrumentation_plan, original_code
            )
            self.logger.log_info("✅ Instrumented code generated")

            self.logger.end_stage("Generate Instrumented Code")
            return instrumented_code

        except Exception as e:
            error_msg = f"Failed to generate instrumented code: {str(e)}"
            self.logger.log_error(error_msg)
            self.logger.end_stage("Generate Instrumented Code")
            raise Exception(error_msg)

    async def _generate_instrumented_code(self,
                                         plan: InstrumentationPlan,
                                         original_code: str) -> str:


        instrumentation_specs = []
        for req in plan.requirements:
            if not req.needs_instrumentation:
                continue

            if getattr(req, 'progress_signals', None) or getattr(req, 'success_signal', None):
                for sig in list(getattr(req, 'progress_signals', []) or []):
                    instrumentation_specs.append({
                        "task_id": req.task_id,
                        "variable_name": sig.signal_name,
                        "signal_role": sig.signal_role,
                        "stage": sig.stage,
                        "variable_type": sig.variable_type,
                        "set_in_function": sig.set_in_function,
                        "set_condition": sig.set_condition,
                        "value_to_set": sig.value_to_set,
                        "value_spec": (sig.value_spec.to_dict() if hasattr(sig.value_spec, 'to_dict') else sig.value_spec) if sig.value_spec else None,
                        "reason": sig.reason,
                    })
                success_signal = getattr(req, 'success_signal', None)
                if success_signal:
                    instrumentation_specs.append({
                        "task_id": req.task_id,
                        "variable_name": success_signal.signal_name,
                        "signal_role": success_signal.signal_role,
                        "stage": success_signal.stage,
                        "variable_type": success_signal.variable_type,
                        "set_in_function": success_signal.set_in_function,
                        "set_condition": success_signal.set_condition,
                        "value_to_set": success_signal.value_to_set,
                        "value_spec": (success_signal.value_spec.to_dict() if hasattr(success_signal.value_spec, 'to_dict') else success_signal.value_spec) if success_signal.value_spec else None,
                        "reason": success_signal.reason,
                    })
            else:
                for var in req.required_variables:
                    instrumentation_specs.append({
                        "task_id": req.task_id,
                        "variable_name": var.variable_name,
                        "signal_role": "legacy",
                        "stage": "",
                        "variable_type": var.variable_type,
                        "set_in_function": var.set_in_function,
                        "set_condition": var.set_condition,
                        "value_to_set": var.value_to_set,
                        "value_spec": (var.value_spec.to_dict() if hasattr(var.value_spec, 'to_dict') else var.value_spec) if var.value_spec else None,
                        "reason": var.reason
                    })

        if not instrumentation_specs:
            self.logger.log_info("No instrumentation needed - returning original code")
            return original_code


        self.logger.log_info("Validating instrumentation variable scopes...")
        validated_specs = []
        for spec in instrumentation_specs:
            func_name = self._normalize_function_name(spec["set_in_function"])
            func_body = self._extract_function_body(original_code, func_name)
            if func_body:
                normalized_value = self._render_value_expression(spec.get("value_spec"), spec["value_to_set"])
                normalized_value = self._normalize_value_template(normalized_value)
                fixed_value = self._fix_variable_references(normalized_value, func_body)
                if fixed_value != spec["value_to_set"]:
                    self.logger.log_info(
                        f"[{func_name}] Fixed value expression: "
                        f"'{spec['value_to_set']}' → '{fixed_value}'"
                    )
                spec = dict(spec)
                spec["set_in_function"] = func_name
                spec["value_to_set"] = fixed_value
            else:
                spec = dict(spec)
                spec["set_in_function"] = func_name
            validated_specs.append(spec)
        instrumentation_specs = validated_specs


        function_patches = {}
        for spec in instrumentation_specs:
            func_name = spec["set_in_function"]
            if func_name not in function_patches:
                function_patches[func_name] = []
            function_patches[func_name].append(spec)


        injection_blocks = []
        for func_name, specs in function_patches.items():
            block_lines = []
            block_lines.append("")
            block_lines.append("    // Instrumentation for task completion tracking")
            block_lines.append("    try {")
            for spec in specs:
                var_name = spec["variable_name"]
                value = spec["value_to_set"]
                var_type = str(spec.get("variable_type") or "").strip().lower()
                if var_type == "object":


                    value_str = str(value).strip()
                    if value_str.startswith("JSON.stringify("):
                        storage_expr = value_str
                    else:
                        storage_expr = f"JSON.stringify({value})"
                else:


                    storage_expr = f"String({value})"
                block_lines.append(f"      localStorage.setItem('{var_name}', {storage_expr});")
            block_lines.append("    } catch (e) {")
            block_lines.append("      console.error('Instrumentation error:', e);")
            block_lines.append("    }")
            block_text = "\n".join(block_lines)
            injection_blocks.append((func_name, block_text))


        instrumented_code = self._inject_blocks(original_code, injection_blocks)

        self.logger.log_info(f"Injected {len(instrumentation_specs)} instrumentation points into {len(function_patches)} functions")
        return instrumented_code

    def _inject_blocks(self, code: str, blocks: list) -> str:
        for func_name, block in blocks:

            func_pattern = rf'(\n\s*)({re.escape(func_name)})\s*\([^)]*\)\s*\{{'
            func_match = re.search(func_pattern, code)
            if not func_match:
                self.logger.log_warning(f"Function '{func_name}' not found — skipping instrumentation")
                continue


            func_start = func_match.end()
            brace_count = 1
            pos = func_start
            last_return_pos = -1
            last_return_indent = ""

            while pos < len(code) and brace_count > 0:
                ch = code[pos]
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        break
                elif ch == '\n':
                    line_start = pos + 1
                    while line_start < len(code) and code[line_start] in ' \t':
                        line_start += 1
                    if code[line_start:line_start + 6] == 'return':
                        line_end = code.find('\n', pos + 1)
                        if line_end == -1:
                            line_end = len(code)
                        line = code[pos + 1:line_end]
                        indent_match = re.match(r'^(\s*)return', line)
                        if indent_match:
                            last_return_pos = line_start
                            last_return_indent = indent_match.group(1)
                pos += 1

            if last_return_pos > 0:
                indent = last_return_indent
                indented_block = block.replace("\n    ", f"\n{indent}  ")
                code = code[:last_return_pos] + indented_block + "\n" + indent + code[last_return_pos:]
            else:
                func_end = pos
                indent = "    "
                indented_block = block.replace("\n    ", f"\n{indent}")
                code = code[:func_end] + indented_block + "\n" + code[func_end:]

        return code

    def _extract_function_body(self, code: str, func_name: str) -> str:
        func_pattern = rf'(?:^|\n)(?:\s*){re.escape(func_name)}\s*\([^)]*\)\s*\{{'
        func_match = re.search(func_pattern, code)
        if not func_match:
            return ""

        func_start = func_match.end()
        brace_count = 1
        pos = func_start
        while pos < len(code) and brace_count > 0:
            ch = code[pos]
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
            pos += 1

        if brace_count == 0:
            return code[func_start:pos - 1]
        return ""

    def _fix_variable_references(self, value_template: str, func_body: str) -> str:

        result_defined = re.search(r'(?:const|let|var)\s+result\s*=', func_body)
        if result_defined:
            return value_template


        result_refs = re.findall(r'result\.(\w+(?:\.\w+)*)', value_template)
        if not result_refs:
            return value_template


        local_vars = {}
        for match in re.finditer(r'(?:const|let|var)\s+(\w+)\s*=\s*(.+?);', func_body):
            var_name = match.group(1)
            local_vars[var_name] = match.group(2).strip()


        param_match = re.search(r'^\s*\(([^)]*)\)', func_body[:200], re.MULTILINE)
        if param_match:
            for p in param_match.group(1).split(','):
                p = p.strip()
                if p and not p.startswith('{'):
                    local_vars[p] = p


        def fix_ref(match):
            path = match.group(1)

            resolved_from_return = self._resolve_result_reference_from_return_object(path, func_body)
            if resolved_from_return:
                return resolved_from_return


            first_segment = path.split('.')[0]
            remaining = '.'.join(path.split('.')[1:])


            if first_segment in local_vars:
                if remaining:
                    return f"{first_segment}.{remaining}"
                return first_segment


            return match.group(0)

        fixed = re.sub(r'result\.(\w+(?:\.\w+)*)', fix_ref, value_template)
        return fixed

    def _normalize_function_name(self, set_in_function: str) -> str:
        raw = str(set_in_function or "").strip()
        if not raw:
            return raw
        return re.sub(r"\s*\(.*$", "", raw).strip()

    def _render_value_expression(self, value_spec: Dict[str, Any], legacy_value: str) -> str:
        if not value_spec:
            return legacy_value

        kind = str(value_spec.get("kind") or "legacy").strip().lower()
        if kind == "primitive":
            return value_spec.get("expr") or legacy_value
        if kind == "object":
            if value_spec.get("expr"):
                return value_spec.get("expr")
            fields = []
            for field in value_spec.get("fields", []):
                name = field.get("name")
                expr = field.get("expr")
                if name and expr:
                    fields.append(f'"{name}": {expr}')
            if fields:
                return "{" + ", ".join(fields) + "}"
        if kind == "array":
            items = []
            for item in value_spec.get("items", []):
                expr = item.get("expr")
                if expr:
                    items.append(expr)
            if items:
                return "[" + ", ".join(items) + "]"
        return value_spec.get("expr_fallback") or legacy_value

    def _normalize_value_template(self, value_template: str) -> str:
        template = str(value_template or "")


        alias_map = {
            'setMeal.': 'set.',
            'pkgEntity.': 'pkg.',
        }
        for src, dst in alias_map.items():
            template = template.replace(src, dst)

        def replace_placeholder(match):
            expr = match.group(1).strip()
            if expr == "iso":
                return "new Date().toISOString()"
            if expr.startswith("checkoutItem."):


                return f"result.{expr}"
            return expr


        return re.sub(r'"<([^>]+)>"', replace_placeholder, template)

    def _resolve_result_reference_from_return_object(self, path: str, func_body: str) -> str:
        segments = path.split(".")
        if len(segments) != 2:
            return ""

        top_level_key, nested_field = segments
        obj_pattern = re.compile(
            rf'return\s*\{{[\s\S]*?\b{re.escape(top_level_key)}\s*:\s*\{{(?P<body>[\s\S]*?)\}}\s*[,}}]',
            re.MULTILINE,
        )
        match = obj_pattern.search(func_body)
        if not match:
            return ""

        object_body = match.group("body")
        field_pattern = re.compile(r'^\s*(\w+)\s*:\s*([^,\n]+)', re.MULTILINE)
        field_map = {m.group(1): m.group(2).strip() for m in field_pattern.finditer(object_body)}
        return field_map.get(nested_field, "")

    def _extract_code(self, text: str) -> str:

        pattern = r'```(?:javascript)?\s*\n(.*?)\n```'
        matches = re.findall(pattern, text, re.DOTALL)

        if matches:

            return max(matches, key=len)


        return text.strip()
