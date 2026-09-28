
import os
import json
import re
import subprocess
import tempfile
from typing import Dict, Any, List, Tuple, Optional
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.verification.data_models import ValidationResult, InstrumentationPlan
from trace2world_pipeline.core.llm_caller import call_openai_api_async


class Trace2WorldInstrumentationValidator:

    def __init__(self, logger: Trace2WorldLogger = None, max_fix_iterations: int = 5,
                 reasoning_effort: str = "medium", model: str = None):
        self.logger = logger or Trace2WorldLogger()
        self.max_fix_iterations = max_fix_iterations
        self.reasoning_effort = reasoning_effort
        self.model = model

    @staticmethod
    def validate_evaluator_instrumentation_match(evaluators: Dict[str, Any],
                                                   instrumented_code: str) -> List[Dict[str, str]]:
        mismatches = []


        instr_pattern = re.compile(
            r"localStorage\.setItem\('([^']+)'\s*,\s*JSON\.stringify\((\{[^}]+\})\)\)",
            re.DOTALL
        )
        instr_keys = {}
        for m in instr_pattern.finditer(instrumented_code):
            key = m.group(1)
            value = m.group(2)

            fields = set(re.findall(r'"(\w+)"\s*:', value))
            instr_keys[key] = fields

        for evaluator in evaluators.get("evaluators", []):
            task_id = evaluator.get("task_id", "unknown")
            eval_logic = evaluator.get("evaluation_logic", "")
            ls_vars = evaluator.get("localStorage_variables", [])


            progress_vars = [v for v in ls_vars if str(v).startswith('progress_')]
            success_vars = [v for v in ls_vars if str(v).startswith('success_')]
            if progress_vars and not success_vars:
                mismatches.append({
                    "task_id": task_id,
                    "type": "missing_success_signal_consumption",
                    "detail": "Evaluator consumes progress_* signals but no success_* signal",
                    "severity": "critical"
                })
            if success_vars and not progress_vars:
                mismatches.append({
                    "task_id": task_id,
                    "type": "missing_progress_signal_consumption",
                    "detail": "Evaluator consumes success_* signals but no progress_* signals",
                    "severity": "warning"
                })


            for var_name in ls_vars:

                static_data_keys = {"venues", "group_buy_deals", "checkout_items",
                                    "menu_set_meals", "service_categories", "room_types",
                                    "booking_packages", "menu_products", "combo_selection_categories",
                                    "cart_items", "user_coupons", "orders"}
                if var_name in static_data_keys:
                    continue

                if var_name not in instr_keys:
                    mismatches.append({
                        "task_id": task_id,
                        "type": "missing_instrumentation_key",
                        "detail": f"Evaluator references localStorage key '{var_name}' but it is not instrumented",
                        "severity": "critical"
                    })


            evt_refs = set(re.findall(r'evt\.(\w+)', eval_logic))
            for ref in evt_refs:


                found_in_any = False
                for instr_key, instr_fields in instr_keys.items():
                    if instr_key in eval_logic and ref in instr_fields:
                        found_in_any = True
                        break
                if not found_in_any and ref not in static_data_keys:

                    all_fields = set()
                    for fields in instr_keys.values():
                        all_fields.update(fields)
                    if ref not in all_fields:
                        mismatches.append({
                            "task_id": task_id,
                            "type": "missing_event_field",
                            "detail": f"Evaluator references evt.{ref} but no instrumentation payload contains this field",
                            "severity": "critical"
                        })


            for instr_key in instr_keys:
                if instr_key in eval_logic:
                    if "JSON.parse" not in eval_logic:
                        mismatches.append({
                            "task_id": task_id,
                            "type": "missing_json_parse",
                            "detail": f"Evaluator references '{instr_key}' but doesn't use JSON.parse()",
                            "severity": "critical"
                        })

                    parse_pattern = rf"JSON\.parse\(localStorage\.getItem\(['\"]{re.escape(instr_key)}['\"]"
                    if not re.search(parse_pattern, eval_logic):
                        mismatches.append({
                            "task_id": task_id,
                            "type": "missing_json_parse_for_key",
                            "detail": f"Evaluator mentions '{instr_key}' but doesn't JSON.parse(localStorage.getItem('{instr_key}'))",
                            "severity": "critical"
                        })

        return mismatches

    @staticmethod
    def _run_instrumentation_e2e_test(instrumented_code: str,
                                       evaluators: Dict[str, Any],
                                       test_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        e2e_results = []

        try:
            with tempfile.TemporaryDirectory() as tmpdir:

                code_path = os.path.join(tmpdir, "business_logic.js")
                with open(code_path, 'w', encoding='utf-8') as f:
                    f.write(instrumented_code)


                with open(os.path.join(tmpdir, "package.json"), 'w') as f:
                    json.dump({"name": "e2e-test", "version": "1.0.0"}, f)


                e2e_script = """
const { LocalStorage } = require('node-localstorage');
global.localStorage = new LocalStorage('./test-storage');

const BusinessLogic = require('./business_logic.js');
const logic = new BusinessLogic();
logic._initStorage();

const evaluators = __EVALUATORS__;
const results = [];

for (const evaluator of evaluators) {
    const taskId = evaluator.task_id;
    const logicStr = evaluator.evaluation_logic;

    try {
        const score = eval(`(() => { ${logicStr} })()`);
        results.push({
            task_id: taskId,
            score: score,
            valid: typeof score === 'number' && score >= 0 && score <= 1
        });
    } catch (e) {
        results.push({
            task_id: taskId,
            score: null,
            valid: false,
            error: e.message
        });
    }
}

console.log(JSON.stringify(results, null, 2));
"""
                evaluators_for_js = json.dumps(evaluators.get("evaluators", []))
                e2e_script = e2e_script.replace("__EVALUATORS__", evaluators_for_js)

                with open(os.path.join(tmpdir, "e2e_test.js"), 'w') as f:
                    f.write(e2e_script)


                npm_cmd = "npm.cmd" if os.name == 'nt' else "npm"
                subprocess.run(
                    [npm_cmd, "install", "--silent", "node-localstorage", "--no-audit", "--no-fund"],
                    cwd=tmpdir, capture_output=True, text=True, timeout=120
                )

                result = subprocess.run(
                    ["node", "e2e_test.js"],
                    cwd=tmpdir, capture_output=True, text=True, timeout=30
                )


                for line in result.stdout.strip().split('\n'):
                    if line.strip().startswith('['):
                        try:
                            results = json.loads('\n'.join(
                                l for l in result.stdout.strip().split('\n')
                                if l.strip() and (l.strip().startswith('[') or '{' in l)
                            ))
                            e2e_results = results
                        except json.JSONDecodeError:
                            pass

        except Exception as e:
            e2e_results = [{"error": f"E2E test execution failed: {str(e)}"}]

        return e2e_results

    @staticmethod
    def build_instrumentation_assertion_specs(plan: InstrumentationPlan) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []
        def _fields_from_value(obj) -> List[str]:
            value_spec = getattr(obj, 'value_spec', None)
            if isinstance(value_spec, dict) and value_spec.get('kind') == 'object':
                if value_spec.get('fields'):
                    return [f.get('name') for f in value_spec.get('fields', []) if f.get('name')]
                return sorted(set(re.findall(r'([A-Za-z_]\w*)\s*:', str(value_spec.get('expr') or ''))))
            if hasattr(value_spec, 'kind') and getattr(value_spec, 'kind', '') == 'object':
                return [f.get('name') for f in getattr(value_spec, 'fields', []) if f.get('name')]
            return sorted(set(re.findall(r'"(\w+)"\s*:', str(getattr(obj, 'value_to_set', '') or ''))))

        for req in plan.requirements:
            if not req.needs_instrumentation:
                continue
            checks = []


            for sig in list(getattr(req, 'progress_signals', []) or []):
                var_type = str(sig.variable_type or "").strip().lower()
                checks.append({
                    "key": sig.signal_name,
                    "signal_role": "progress",
                    "storage_type": "json" if var_type == "object" else "primitive",
                    "required_fields": _fields_from_value(sig) if var_type == "object" else [],
                })
            if getattr(req, 'success_signal', None):
                sig = req.success_signal
                var_type = str(sig.variable_type or "").strip().lower()
                checks.append({
                    "key": sig.signal_name,
                    "signal_role": "success",
                    "storage_type": "json" if var_type == "object" else "primitive",
                    "required_fields": _fields_from_value(sig) if var_type == "object" else [],
                })


            for var in req.required_variables:
                var_type = str(var.variable_type or "").strip().lower()
                if var_type == "object":
                    checks.append({
                        "key": var.variable_name,
                        "signal_role": "legacy",
                        "storage_type": "json",
                        "required_fields": _fields_from_value(var),
                    })
                else:
                    checks.append({
                        "key": var.variable_name,
                        "signal_role": "legacy",
                        "storage_type": "primitive",
                    })
            specs.append({
                "task_id": req.task_id,
                "checks": checks,
            })
        return specs

    async def validate_pipeline_artifacts(self,
                                          instrumented_code: str,
                                          original_tests: str,
                                          test_data: Dict[str, Any],
                                          instrumentation_plan: InstrumentationPlan,
                                          evaluators: Optional[Dict[str, Any]] = None,
                                          skip_fix: bool = False) -> Dict[str, Any]:
        validated_code, validation_result = await self.validate_and_fix(
            instrumented_code=instrumented_code,
            original_tests=original_tests,
            test_data=test_data,
            skip_fix=skip_fix,
        )

        instrumentation_assertions = self.build_instrumentation_assertion_specs(instrumentation_plan)
        runtime_assertion_result = self._run_instrumentation_assertion_checks(
            validated_code,
            original_tests,
            test_data,
            instrumentation_assertions,
        )

        evaluator_mismatches: List[Dict[str, Any]] = []
        evaluator_smoke: List[Dict[str, Any]] = []
        if evaluators is not None:
            evaluator_mismatches = self.validate_evaluator_instrumentation_match(
                evaluators,
                validated_code,
            )
            evaluator_smoke = self._run_instrumentation_e2e_test(
                validated_code,
                evaluators,
                test_data,
            )

        success = (
            validation_result.success
            and runtime_assertion_result.get("success", False)
            and len([m for m in evaluator_mismatches if m.get("severity") == "critical"]) == 0
            and all(r.get("valid", True) for r in evaluator_smoke if isinstance(r, dict) and "valid" in r)
        )

        return {
            "success": success,
            "validated_code": validated_code,
            "code_validation_result": validation_result.to_dict(),
            "runtime_assertion_result": runtime_assertion_result,
            "evaluator_mismatches": evaluator_mismatches,
            "evaluator_smoke": evaluator_smoke,
        }

    def _run_instrumentation_assertion_checks(self,
                                              instrumented_code: str,
                                              original_tests: str,
                                              test_data: Dict[str, Any],
                                              assertion_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not original_tests or not assertion_specs:
            return {"success": True, "message": "No original tests or no instrumentation assertions"}

        helper_js = """
  assertStorageKeyExists(key) {
    const raw = localStorage.getItem(key);
    this.assert(raw !== null && raw !== undefined && raw !== '', `Expected localStorage key ${key} to exist`);
    return raw;
  }

  assertStorageJSONHasFields(key, requiredFields) {
    const raw = this.assertStorageKeyExists(key);
    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      throw new Error(`Expected ${key} to contain valid JSON, got: ${raw}`);
    }
    for (const field of requiredFields || []) {
      this.assert(Object.prototype.hasOwnProperty.call(parsed, field), `Expected ${key} JSON to contain field ${field}`);
    }
    return parsed;
  }

  assertStoragePrimitiveExists(key) {
    const raw = this.assertStorageKeyExists(key);
    this.assert(raw !== '[object Object]', `Expected ${key} to be stored as primitive, got [object Object]`);
    return raw;
  }

  assertInstrumentation(spec) {
    for (const check of spec.checks || []) {
      if (check.storage_type === 'json') {
        this.assertStorageJSONHasFields(check.key, check.required_fields || []);
      } else {
        this.assertStoragePrimitiveExists(check.key);
      }
    }
  }
"""

        appended_checks = []
        for spec in assertion_specs:
            task_id = spec["task_id"]
            checks_json = json.dumps(spec, ensure_ascii=False)
            appended_checks.append(
                f"""
if (typeof this['testFlow_{task_id}'] === 'function') {{
  const original_{task_id} = this['testFlow_{task_id}'].bind(this);
  this['testFlow_{task_id}'] = () => {{
    original_{task_id}();
    this.assertInstrumentation({checks_json});
  }};
}}
"""
            )

        patched_tests = original_tests
        if "runAllTests()" in patched_tests and "recordFailure(" in patched_tests:
            insertion_point = patched_tests.find("  runAllTests() {")
            if insertion_point != -1:
                patched_tests = patched_tests[:insertion_point] + helper_js + "\n" + patched_tests[insertion_point:]
            patched_tests += "\n" + "\n".join(appended_checks)

        result = self._run_tests(
            instrumented_code,
            patched_tests,
            test_data,
            "Original Tests + Instrumentation Assertions",
        )
        success = result.get("passed", 0) == result.get("total", 0)
        return {
            "success": success,
            "total": result.get("total", 0),
            "passed": result.get("passed", 0),
            "failed": result.get("failed", 0),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "message": "Instrumentation assertions passed" if success else "Instrumentation assertions failed",
        }

    async def validate_and_fix(self,
                               instrumented_code: str,
                               original_tests: str,
                               test_data: Dict[str, Any],
                               skip_fix: bool = False) -> Tuple[str, ValidationResult]:
        self.logger.start_stage("Validate Instrumentation")
        self.logger.log_info("✅ Validating instrumented code preserves original functionality...")

        current_code = instrumented_code
        iteration = 0

        while iteration < self.max_fix_iterations:
            iteration += 1
            self.logger.log_info(f"Validation iteration {iteration}/{self.max_fix_iterations}")


            original_results = self._run_tests(current_code, original_tests, test_data, "Original Tests")


            original_passed = original_results.get("passed", 0) == original_results.get("total", 0)

            self.logger.log_info(f"Original tests: {original_results.get('passed', 0)}/{original_results.get('total', 0)} passed")


            if original_passed:
                self.logger.log_info(f"✅ All original tests passed on iteration {iteration}")
                self.logger.log_info("   Instrumentation did not break original functionality")
                self.logger.end_stage("Validate Instrumentation")

                return current_code, ValidationResult(
                    success=True,
                    original_tests_passed=True,
                    instrumentation_tests_passed=True,
                    total_tests=original_results.get("total", 0),
                    passed_tests=original_results.get("passed", 0),
                    failed_tests=0,
                    iterations_used=iteration,
                    message="Original functionality preserved"
                )


            if skip_fix:
                self.logger.log_warning(f"Original tests failed — skipping LLM fix (skip_fix=True)")
                self.logger.end_stage("Validate Instrumentation")
                return instrumented_code, ValidationResult(
                    success=False,
                    original_tests_passed=False,
                    instrumentation_tests_passed=True,
                    total_tests=original_results.get("total", 0),
                    passed_tests=original_results.get("passed", 0),
                    failed_tests=original_results.get("failed", 0),
                    iterations_used=iteration,
                    message="Original tests failed — fix skipped",
                    errors=["Instrumentation may have broken original functionality"]
                )

            self.logger.log_warning(f"Original tests failed - attempting fix...")

            current_code = await self._fix_instrumented_code(
                current_code,
                original_results,
                None
            )


        self.logger.log_error(f"Max iterations ({self.max_fix_iterations}) reached with test failures")
        self.logger.end_stage("Validate Instrumentation")

        return current_code, ValidationResult(
            success=False,
            original_tests_passed=False,
            instrumentation_tests_passed=True,
            total_tests=original_results.get("total", 0),
            passed_tests=original_results.get("passed", 0),
            failed_tests=original_results.get("failed", 0),
            iterations_used=self.max_fix_iterations,
            message="Failed after max iterations",
            errors=["Max fix iterations reached", "Instrumentation broke original functionality"]
        )

    def _run_tests(self,
                   code: str,
                   tests: str,
                   test_data: Dict[str, Any],
                   test_name: str) -> Dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:

                code_path = os.path.join(tmpdir, "business_logic.js")
                with open(code_path, 'w', encoding='utf-8') as f:
                    f.write(code)


                test_path = os.path.join(tmpdir, "test_runner.js")
                with open(test_path, 'w', encoding='utf-8') as f:
                    f.write(tests)


                package_json = {
                    "name": "instrumentation-test",
                    "version": "1.0.0",
                    "dependencies": {
                        "node-localstorage": "^2.2.1"
                    }
                }
                package_path = os.path.join(tmpdir, "package.json")
                with open(package_path, 'w', encoding='utf-8') as f:
                    json.dump(package_json, f, indent=2)


                npm_cmd = "npm.cmd" if os.name == 'nt' else "npm"
                install_args = [npm_cmd, "install", "--silent"]
                if os.name != 'nt':
                    install_args.extend(["--no-audit", "--no-fund"])

                subprocess.run(
                    install_args,
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    shell=(os.name == 'nt')
                )


                test_data_path = os.path.join(tmpdir, "test_data.json")
                with open(test_data_path, 'w', encoding='utf-8') as f:
                    json.dump(test_data, f, ensure_ascii=False)


                executor_code = """
                // Setup localStorage mock for Node.js environment
                const { LocalStorage } = require('node-localstorage');
                const fs = require('fs');
                global.localStorage = new LocalStorage('./test-storage');

                // Clear any previous test data
                localStorage.clear();

                const BusinessLogic = require('./business_logic.js');
                const TestRunner = require('./test_runner.js');

                // Run tests
                const logic = new BusinessLogic();
                const runner = new TestRunner(logic);

                // Re-seed localStorage with full data from _initStorage to ensure
                // all entities referenced by tests are available.
                // TestRunner.setupTestData() may have overwritten with a limited
                // subset that doesn't contain all entities the test methods need.
                localStorage.clear();
                logic._initStorage();

                const results = runner.runAllTests();

                // Output results as JSON
                console.log(JSON.stringify({
                    total: results.length,
                    passed: results.filter(r => r.success).length,
                    failed: results.filter(r => !r.success).length,
                    details: results
                }, null, 2));

                // Clean up localStorage after tests
                localStorage.clear();
                """

                executor_path = os.path.join(tmpdir, "executor.js")
                with open(executor_path, 'w', encoding='utf-8') as f:
                    f.write(executor_code)


                node_cmd = "node"
                result = subprocess.run(
                    [node_cmd, executor_path],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=60
                )


                return self._parse_test_output(result.stdout, result.stderr, result.returncode)

        except subprocess.TimeoutExpired:
            self.logger.log_error(f"{test_name} timed out")
            return {"total": 0, "passed": 0, "failed": 0, "error": "Timeout"}
        except Exception as e:
            self.logger.log_error(f"{test_name} error: {str(e)}")
            return {"total": 0, "passed": 0, "failed": 0, "error": str(e)}

    def _parse_test_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, Any]:
        import re
        import json


        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict) and "total" in parsed and "passed" in parsed:
                self.logger.log_info(f"Successfully parsed JSON test output")
                return {
                    "total": parsed.get("total", 0),
                    "passed": parsed.get("passed", 0),
                    "failed": parsed.get("failed", 0),
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": returncode
                }
        except json.JSONDecodeError:
            pass


        lines = stdout.split('\n')
        for i in range(len(lines)):
            if lines[i].strip().startswith('{'):
                json_str = '\n'.join(lines[i:])
                try:
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict) and "total" in parsed and "passed" in parsed:
                        self.logger.log_info(f"Extracted and parsed JSON from output")
                        return {
                            "total": parsed.get("total", 0),
                            "passed": parsed.get("passed", 0),
                            "failed": parsed.get("failed", 0),
                            "stdout": stdout,
                            "stderr": stderr,
                            "returncode": returncode
                        }
                except json.JSONDecodeError:
                    continue


        passed = 0
        failed = 0
        total = 0

        passed_match = re.search(r'(\d+)\s+passed', stdout + stderr)
        failed_match = re.search(r'(\d+)\s+failed', stdout + stderr)

        if passed_match:
            passed = int(passed_match.group(1))
        if failed_match:
            failed = int(failed_match.group(1))

        total = passed + failed


        if total == 0:
            if returncode == 0:

                passed = 1
                total = 1
                self.logger.log_warning(f"No test output found, inferring success from exit code 0")
            else:

                failed = 1
                total = 1
                self.logger.log_warning(f"No test output found, inferring failure from exit code {returncode}")

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode
        }

    async def _fix_instrumented_code(self,
                                     code: str,
                                     original_test_failures: Dict[str, Any] = None,
                                     instrumentation_test_failures: Dict[str, Any] = None) -> str:
        failure_info = []

        if original_test_failures:
            failure_info.append(f"ORIGINAL TESTS FAILED:\n{original_test_failures.get('stderr', '')}\n{original_test_failures.get('stdout', '')}")

        if instrumentation_test_failures:
            failure_info.append(f"INSTRUMENTATION TESTS FAILED:\n{instrumentation_test_failures.get('stderr', '')}\n{instrumentation_test_failures.get('stdout', '')}")

        prompt = f"""The instrumented code has test failures. Fix the code.

CURRENT INSTRUMENTED CODE:
```javascript
{code}
```

TEST FAILURES:
{chr(10).join(failure_info)}

DIAGNOSIS:
- If ORIGINAL tests fail: Your instrumentation broke the original functionality. You must fix it without affecting original behavior.
- If INSTRUMENTATION tests fail: The instrumentation variables are not being set correctly. Fix the instrumentation logic.

REQUIREMENTS:
1. DO NOT remove required instrumentation variables
2. Ensure original functionality is preserved
3. Wrap all instrumentation in try-catch blocks
4. Ensure both test suites pass

Return the complete fixed business_logic.js code."""


        call_id = self.logger.log_api_call(
            "Fix Instrumented Code",
            prompt
        )

        try:
            result, usage_info = await call_openai_api_async(
                [{"role": "user", "content": prompt}],
                reasoning_effort=self.reasoning_effort,
                model=self.model,
                stage="fix_instrumented_code",
            )

            self.logger.log_api_response(
                "Fix Instrumented Code",
                success=True,
                response="[Fixed code generated]",
                usage_info=usage_info,
                call_id=call_id
            )


            fixed_code = self._extract_code(result)
            return fixed_code

        except Exception as e:
            self.logger.log_error(f"Fix failed: {str(e)}")
            self.logger.log_api_response(
                "Fix Instrumented Code",
                success=False,
                error=str(e),
                call_id=call_id
            )

            return code

    def _extract_code(self, text: str) -> str:
        import re

        pattern = r'```(?:javascript)?\s*\n(.*?)\n```'
        matches = re.findall(pattern, text, re.DOTALL)

        if matches:
            return max(matches, key=len)

        return text.strip()
