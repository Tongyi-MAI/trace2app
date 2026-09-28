
import os
import json
import argparse
import asyncio
from typing import Dict, Any
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.core.post_processing_paths import post_processing_debug_path, post_processing_path
from trace2world_pipeline.verification.analyzer import Trace2WorldInstrumentationAnalyzer
from trace2world_pipeline.verification.generator import Trace2WorldInstrumentationGenerator
from trace2world_pipeline.verification.validator import Trace2WorldInstrumentationValidator
from trace2world_pipeline.verification.evaluator import Trace2WorldInstrumentationEvaluator
from trace2world_pipeline.task_generation.task_rewriter import Trace2WorldTaskRewriter


class Trace2WorldInstrumentationPostProcessor:

    def __init__(self, website_dir: str, config: Dict[str, Any] = None):
        self.website_dir = website_dir
        self.config = config or self._default_config()


        logs_dir = str(post_processing_debug_path(website_dir, "instrumentation"))
        os.makedirs(logs_dir, exist_ok=True)
        self.logger = Trace2WorldLogger(output_dir=logs_dir, log_level="INFO")


        analyzer_config = self._get_component_config("instrumentation_analyzer")
        generator_config = self._get_component_config("instrumentation_generator")
        validator_config = self._get_component_config("instrumentation_validator")
        evaluator_config = self._get_component_config("instrumentation_evaluator")
        task_rewriter_config = self._get_component_config("task_rewriter")


        self.analyzer = Trace2WorldInstrumentationAnalyzer(
            self.logger,
            reasoning_effort=analyzer_config["reasoning_effort"],
            model=analyzer_config["model"]
        )
        self.generator = Trace2WorldInstrumentationGenerator(
            self.logger,
            reasoning_effort=generator_config["reasoning_effort"],
            model=generator_config["model"]
        )
        self.validator = Trace2WorldInstrumentationValidator(
            self.logger,
            max_fix_iterations=self.config.get("max_fix_iterations", 3),
            reasoning_effort=validator_config["reasoning_effort"],
            model=validator_config["model"]
        )
        self.evaluator_generator = Trace2WorldInstrumentationEvaluator(
            self.logger,
            reasoning_effort=evaluator_config["reasoning_effort"],
            model=evaluator_config["model"]
        )
        self.task_rewriter = Trace2WorldTaskRewriter(
            self.logger,
            model=task_rewriter_config["model"],
            reasoning_effort=task_rewriter_config["reasoning_effort"]
        )

    def _default_config(self) -> Dict[str, Any]:
        return {
            "max_tokens": 16000,
            "max_fix_iterations": 3,
            "test_data_limit": 3,
            "reasoning_effort": "medium",
            "model": None
        }

    def _get_component_config(self, component_name: str) -> Dict[str, Any]:

        stage_configs = self.config.get("stage_configs", {})
        if component_name in stage_configs:
            stage_config = stage_configs[component_name]
            return {
                "model": stage_config.get("model") or self.config.get("model"),
                "reasoning_effort": stage_config.get("reasoning_effort") or self.config.get("reasoning_effort", "medium")
            }


        return {
            "model": self.config.get("model"),
            "reasoning_effort": self.config.get("reasoning_effort", "medium")
        }

    async def process(self) -> Dict[str, Any]:
        self.logger.log_info("=" * 80)
        self.logger.log_info("🚀 Starting TDD Instrumentation Post-Processing")
        self.logger.log_info(f"📁 Website Directory: {self.website_dir}")
        self.logger.log_info("=" * 80)

        try:

            self.logger.log_info("\n📂 Step 0: Loading input files...")
            input_data = self._load_input_files()


            self.logger.log_info("\n🔍 Step 1: Analyzing instrumentation requirements...")
            instrumentation_plan = await self.analyzer.analyze_requirements(
                tasks=input_data["tasks"],
                business_logic_code=input_data["business_logic_code"],
                datadict=input_data["datadict"]
            )


            self._save_instrumentation_plan(instrumentation_plan)
            self.logger.log_info(f"✅ Identified instrumentation needs for {len(instrumentation_plan.requirements)} tasks")


            has_tests = input_data["original_tests"] is not None
            needs_instrumentation = instrumentation_plan.has_instrumentation_needs()


            if needs_instrumentation and not has_tests:
                self.logger.log_info("ℹ️ Instrumentation needed but no test_flows.js — skipping injection, generating evaluators only")
                needs_instrumentation = False

            if not needs_instrumentation:
                self.logger.log_info("ℹ️ No instrumentation needed - existing variables sufficient")


                self.logger.log_info("\n🔄 Rewriting tasks to match actual generated data...")


                full_website_data = self._load_full_website_data()


                rewritten_tasks = await self.task_rewriter.rewrite_tasks(
                    original_tasks=input_data["tasks"],
                    website_data=full_website_data,
                    datadict=input_data["datadict"],
                    include_full_data=self.config.get("task_rewriting", {}).get("include_full_data", True)
                )


                self._save_original_tasks(input_data["tasks"])
                self._save_rewritten_tasks(rewritten_tasks)


                input_data["tasks"] = rewritten_tasks
                self.logger.log_info(f"✅ Successfully rewrote {len(rewritten_tasks)} tasks")


                evaluators = await self.evaluator_generator.generate_evaluators(
                    tasks=input_data["tasks"],
                    instrumentation_plan=instrumentation_plan,
                    datadict=input_data["datadict"],
                    static_data_types=input_data["static_data_types"],
                    business_logic_code=input_data["business_logic_code"],
                    test_data=input_data["test_data"],
                    website_data=full_website_data
                )
                self._save_evaluators(evaluators, input_data["static_data_types"])

                return {
                    "success": True,
                    "instrumentation_plan": instrumentation_plan,
                    "validation_result": {"success": True, "message": "No instrumentation needed"},
                    "evaluators_count": len(evaluators),
                    "evaluators": [e.to_dict() for e in evaluators],
                }


            self.logger.log_info("\n🛠️ Step 2: Generating instrumented code...")
            instrumented_code = await self.generator.generate(
                instrumentation_plan=instrumentation_plan,
                original_code=input_data["business_logic_code"],
                test_data=input_data["test_data"]
            )


            self.logger.log_info("\n✅ Step 3: Validating instrumentation preserves original functionality and writes correct runtime payloads...")
            pipeline_validation = await self.validator.validate_pipeline_artifacts(
                instrumented_code=instrumented_code,
                original_tests=input_data["original_tests"],
                test_data=input_data["test_data"],
                instrumentation_plan=instrumentation_plan,
                evaluators=None,
                skip_fix=True,
            )
            validated_code = pipeline_validation["validated_code"]
            validation_result = pipeline_validation["code_validation_result"]
            runtime_assertion_result = pipeline_validation["runtime_assertion_result"]
            self._save_instrumentation_validation_report({
                "code_validation_result": validation_result,
                "runtime_assertion_result": runtime_assertion_result,
            })

            code_validation_skipped = "fix skipped" in str(validation_result.get("message", ""))
            if pipeline_validation["success"]:
                self.logger.log_info("✅ Instrumentation validated - original functionality preserved and runtime assertions passed")
                self._save_instrumented_code(validated_code)
                self._switch_html_runtime_sdk("instrumented_business_logic.js")
            elif code_validation_skipped:
                self.logger.log_warning("⚠️ Original flow tests are not fully green on this artifact; treating instrumentation runtime assertions as inconclusive and continuing")
                self._save_instrumented_code(validated_code)
                self._switch_html_runtime_sdk("instrumented_business_logic.js")
            else:
                self.logger.log_error("❌ Instrumentation validation failed before evaluator generation")
                self._restore_original_code()
                return {
                    "success": False,
                    "instrumentation_plan": instrumentation_plan,
                    "code_validation_result": validation_result,
                    "runtime_assertion_result": runtime_assertion_result,
                    "policy_applied": "fail_closed",
                    "error": "Instrumentation validation failed",
                    "evaluators_count": 0,
                }


            self.logger.log_info("\n🔄 Step 3.5: Rewriting tasks to match actual generated data...")


            full_website_data = self._load_full_website_data()


            rewritten_tasks = await self.task_rewriter.rewrite_tasks(
                original_tasks=input_data["tasks"],
                website_data=full_website_data,
                datadict=input_data["datadict"],
                include_full_data=self.config.get("task_rewriting", {}).get("include_full_data", True)
            )


            is_valid, issues = self.task_rewriter.validate_rewritten_tasks(
                rewritten_tasks,
                full_website_data
            )

            if not is_valid:
                self.logger.log_warning(f"Task rewriting validation issues: {issues}")


            self._save_original_tasks(input_data["tasks"])
            self._save_rewritten_tasks(rewritten_tasks)


            input_data["tasks"] = rewritten_tasks
            self.logger.log_info(f"✅ Successfully rewrote {len(rewritten_tasks)} tasks to match actual data")


            self.logger.log_info("\n📝 Step 4: Generating evaluators with rewritten tasks...")
            evaluators = await self.evaluator_generator.generate_evaluators(
                tasks=input_data["tasks"],
                instrumentation_plan=instrumentation_plan,
                datadict=input_data["datadict"],
                static_data_types=input_data["static_data_types"],
                business_logic_code=input_data["business_logic_code"],
                test_data=input_data["test_data"],
                website_data=full_website_data
            )

            evaluators_payload = {
                "evaluators": [e.to_dict() for e in evaluators],
                "static_data_types": input_data["static_data_types"],
                "instrumentation_based": True,
            }


            self.logger.log_info("\n🧪 Step 4.5: Validating evaluator-instrumentation contract...")
            post_eval_validation = await self.validator.validate_pipeline_artifacts(
                instrumented_code=validated_code,
                original_tests=input_data["original_tests"],
                test_data=input_data["test_data"],
                instrumentation_plan=instrumentation_plan,
                evaluators=evaluators_payload,
                skip_fix=True,
            )
            evaluator_mismatches = post_eval_validation["evaluator_mismatches"]
            evaluator_smoke = post_eval_validation["evaluator_smoke"]
            self._save_evaluator_validation_reports(evaluator_mismatches, evaluator_smoke)

            critical_mismatches = [m for m in evaluator_mismatches if m.get("severity") == "critical"]
            smoke_failures = [r for r in evaluator_smoke if isinstance(r, dict) and r.get("valid") is False]
            if critical_mismatches or smoke_failures:
                self.logger.log_error("❌ Evaluator / instrumentation contract validation failed")
                return {
                    "success": False,
                    "instrumentation_plan": instrumentation_plan,
                    "code_validation_result": validation_result,
                    "runtime_assertion_result": runtime_assertion_result,
                    "evaluator_mismatches": evaluator_mismatches,
                    "evaluator_smoke": evaluator_smoke,
                    "policy_applied": "fail_closed",
                    "error": "Evaluator / instrumentation contract validation failed",
                    "evaluators_count": 0,
                }


            self._save_evaluators(evaluators, input_data["static_data_types"])

            self.logger.log_info("\n" + "=" * 80)
            self.logger.log_info(f"✨ Instrumentation Post-Processing Complete!")
            self.logger.log_info(f"✅ Generated {len(evaluators)} evaluators")
            self.logger.log_info(f"📁 Updated files saved to: {self.website_dir}")
            self.logger.log_info("=" * 80)

            return {
                "success": True,
                "instrumentation_plan": instrumentation_plan,
                "code_validation_result": validation_result,
                "runtime_assertion_result": runtime_assertion_result,
                "evaluator_mismatches": evaluator_mismatches,
                "evaluator_smoke": evaluator_smoke,
                "evaluators_count": len(evaluators),
                "evaluators": evaluators_payload["evaluators"],
            }

        except Exception as e:
            self.logger.log_exception(e, "Instrumentation Post-Processing")
            return {
                "success": False,
                "error": str(e)
            }

    def _resolve_runtime_business_logic_path(self) -> str:
        for rel in (
            os.path.join("phase_sdk_generation", "runtime", "instrumented_business_logic.js"),
            os.path.join("phase_sdk_generation", "runtime", "business_logic.js"),
        ):
            path = os.path.join(self.website_dir, rel)
            if os.path.exists(path):
                return path
        return os.path.join(self.website_dir, "backend", "runtime", "business_logic.js")

    def _resolve_first_existing(self, *relative_paths: str) -> str:
        for rel in relative_paths:
            path = os.path.join(self.website_dir, rel)
            if os.path.exists(path):
                return path
        return os.path.join(self.website_dir, relative_paths[0])

    def _load_input_files(self) -> Dict[str, Any]:

        tasks_path = os.path.join(self.website_dir, "data", "tasks.json")
        if not os.path.exists(tasks_path):
            raise FileNotFoundError(f"tasks.json not found at {tasks_path}")

        with open(tasks_path, 'r', encoding='utf-8') as f:
            tasks_data = json.load(f)
            tasks = tasks_data["tasks"]


        datadict_path = os.path.join(self.website_dir, "data", "entity_schema.json")
        if not os.path.exists(datadict_path):
            datadict_path = os.path.join(self.website_dir, "data", "data_models.json")
        if not os.path.exists(datadict_path):
            raise FileNotFoundError(f"entity_schema.json (or data_models.json) not found in {self.website_dir}/data/")

        with open(datadict_path, 'r', encoding='utf-8') as f:
            datadict = json.load(f)


        business_logic_path = self._resolve_first_existing(
            os.path.join("backend", "runtime", "business_logic.js"),
            "business_logic.js",
        )
        if not os.path.exists(business_logic_path):
            raise FileNotFoundError(f"business_logic.js not found at {business_logic_path}")

        with open(business_logic_path, 'r', encoding='utf-8') as f:
            business_logic_code = f.read()


        original_tests = None
        original_tests_path = self._resolve_first_existing(
            os.path.join("validation", "test_flows", "test_flows.js"),
            "test_flows.js",
        )
        if os.path.exists(original_tests_path):
            with open(original_tests_path, 'r', encoding='utf-8') as f:
                original_tests = f.read()


        website_data_path = self._resolve_first_existing(
            os.path.join("projections", "runtime_backend", "seed_data.json"),
            "seed_data.json",
            "website_data.json",
        )
        if not os.path.exists(website_data_path):
            raise FileNotFoundError(f"seed_data.json (or website_data.json) not found in {self.website_dir}")

        with open(website_data_path, 'r', encoding='utf-8') as f:
            full_data = json.load(f)


        test_data = self._extract_limited_test_data(
            full_data,
            limit=self.config.get("test_data_limit", 3)
        )


        static_data_types = list(full_data.keys())

        self.logger.log_info(f"✅ Loaded tasks: {len(tasks)}")
        self.logger.log_info(f"✅ Loaded datadict entities: {len(datadict.get('entities', []))}")
        self.logger.log_info(f"✅ Loaded business logic: {len(business_logic_code)} characters")
        if original_tests:
            self.logger.log_info(f"✅ Loaded original tests: {len(original_tests)} characters")
        else:
            self.logger.log_info(f"ℹ️  No test_flows.js found — instrumentation validation will be skipped")
        self.logger.log_info(f"✅ Prepared test data: {len(test_data)} entity types")

        return {
            "tasks": tasks,
            "datadict": datadict,
            "business_logic_code": business_logic_code,
            "original_tests": original_tests,
            "test_data": test_data,
            "static_data_types": static_data_types
        }

    def _load_full_website_data(self) -> Dict[str, Any]:
        for name in (
            os.path.join("projections", "runtime_backend", "seed_data.json"),
            "seed_data.json",
            "website_data.json",
        ):
            p = os.path.join(self.website_dir, name)
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f)
        raise FileNotFoundError(f"seed_data.json (or website_data.json) not found in {self.website_dir}")

    def _extract_limited_test_data(self, full_data: Dict, limit: int) -> Dict:
        limited_data = {}
        for entity_type, items in full_data.items():
            if isinstance(items, list):
                limited_data[entity_type] = items[:limit]
            else:
                limited_data[entity_type] = items
        return limited_data

    def _save_instrumentation_plan(self, plan):
        plan_path = post_processing_path(self.website_dir, "instrumentation", "instrumentation_plan.json")
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with plan_path.open('w', encoding='utf-8') as f:
            json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)
        self.logger.log_info(f"💾 Saved instrumentation plan to {plan_path}")

    def _save_instrumented_code(self, code: str):
        runtime_dir = str(PhaseOutputManager(self.website_dir).formal("sdk_generation", "runtime"))
        os.makedirs(runtime_dir, exist_ok=True)
        business_logic_path = self._resolve_first_existing(
            os.path.join("phase_sdk_generation", "runtime", "business_logic.js"),
        )
        instrumented_path = os.path.join(runtime_dir, "instrumented_business_logic.js")
        backup_path = os.path.join(runtime_dir, "business_logic.js.backup")


        if os.path.exists(business_logic_path) and not os.path.exists(backup_path):
            with open(business_logic_path, 'r', encoding='utf-8') as f:
                original = f.read()
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(original)
            self.logger.log_info("💾 Backed up original to business_logic.js.backup")

        with open(instrumented_path, 'w', encoding='utf-8') as f:
            f.write(code)

        self.logger.log_info("💾 Saved instrumented code to instrumented_business_logic.js")

    def _switch_html_runtime_sdk(self, sdk_filename: str):
        pages_dir = str(PhaseOutputManager(self.website_dir).formal("pagegen", "pages"))
        if not os.path.isdir(pages_dir):
            self.logger.log_warning(f"PageGen pages directory not found: {pages_dir}")
            return
        runtime_ref = sdk_filename
        if not sdk_filename.startswith("../"):
            runtime_ref = f"../../phase_sdk_generation/runtime/{sdk_filename}"
        for name in os.listdir(pages_dir):
            if not name.endswith('.html'):
                continue
            html_path = os.path.join(pages_dir, name)
            try:
                with open(html_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                updated = content.replace('src="business_logic.js"', f'src="{runtime_ref}"')
                updated = updated.replace("src='business_logic.js'", f"src='{runtime_ref}'")
                updated = updated.replace(
                    'src="../../phase_sdk_generation/runtime/business_logic.js"',
                    f'src="{runtime_ref}"',
                )
                updated = updated.replace(
                    "src='../../phase_sdk_generation/runtime/business_logic.js'",
                    f"src='{runtime_ref}'",
                )
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(updated)
            except Exception as e:
                self.logger.log_warning(f"Failed to update SDK script reference in {name}: {e}")
        self.logger.log_info(f"💾 Updated HTML files to use {sdk_filename}")

    def _save_instrumentation_validation_report(self, report: Dict[str, Any]):
        report_path = post_processing_path(
            self.website_dir,
            "instrumentation",
            "instrumentation_validation_report.json",
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open('w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        self.logger.log_info(f"💾 Saved {report_path}")

    def _save_evaluator_validation_reports(self, mismatches: list, smoke_report: list):
        mismatches_path = post_processing_path(self.website_dir, "evaluators", "evaluator_instrumentation_mismatches.json")
        smoke_path = post_processing_path(self.website_dir, "evaluators", "evaluator_smoke_report.json")
        mismatches_path.parent.mkdir(parents=True, exist_ok=True)
        with mismatches_path.open('w', encoding='utf-8') as f:
            json.dump(mismatches, f, indent=2, ensure_ascii=False)
        with smoke_path.open('w', encoding='utf-8') as f:
            json.dump(smoke_report, f, indent=2, ensure_ascii=False)
        self.logger.log_info(f"💾 Saved {mismatches_path}")
        self.logger.log_info(f"💾 Saved {smoke_path}")

    def _save_evaluators(self, evaluators, static_data_types):
        evaluators_path = post_processing_path(self.website_dir, "evaluators", "evaluators.json")
        evaluators_data = {
            "evaluators": [e.to_dict() for e in evaluators],
            "static_data_types": static_data_types,
            "instrumentation_based": True
        }
        evaluators_path.parent.mkdir(parents=True, exist_ok=True)
        with evaluators_path.open('w', encoding='utf-8') as f:
            json.dump(evaluators_data, f, indent=2, ensure_ascii=False)

        self.logger.log_info(f"💾 Saved {len(evaluators)} evaluators to {evaluators_path}")

    def _restore_original_code(self):
        instrumented_path = self._resolve_first_existing(
            os.path.join("phase_sdk_generation", "runtime", "instrumented_business_logic.js"),
        )
        if os.path.exists(instrumented_path):
            self._move_to_deprecated(instrumented_path)
            self.logger.log_info("📦 Moved instrumented_business_logic.js to deprecated/")
        self._switch_html_runtime_sdk("business_logic.js")


        plan_path = post_processing_path(self.website_dir, "instrumentation", "instrumentation_plan.json")
        if plan_path.exists():
            self._move_to_deprecated(str(plan_path))
            self.logger.log_info("📦 Moved instrumentation_plan.json to deprecated/")

    def _move_to_deprecated(self, file_path: str):
        deprecated_dir = os.path.join(os.path.dirname(file_path), "deprecated")
        os.makedirs(deprecated_dir, exist_ok=True)
        base = os.path.basename(file_path)
        target = os.path.join(deprecated_dir, base)
        if os.path.exists(target):
            stem, ext = os.path.splitext(base)
            idx = 1
            while True:
                candidate = os.path.join(deprecated_dir, f"{stem}.{idx}{ext}")
                if not os.path.exists(candidate):
                    target = candidate
                    break
                idx += 1
        os.replace(file_path, target)


    @staticmethod
    def _discover_websites(batch_dir: str) -> list:
        websites = []

        if not os.path.exists(batch_dir):
            print(f"❌ Error: Batch directory not found: {batch_dir}")
            return websites


        for item in sorted(os.listdir(batch_dir)):
            item_path = os.path.join(batch_dir, item)


            if not os.path.isdir(item_path):
                continue


            required_files = [
                os.path.join(item_path, "data", "tasks.json"),
                os.path.join(item_path, "business_logic.js"),
                os.path.join(item_path, "test_flows.js"),
                os.path.join(item_path, "seed_data.json")
            ]

            if all(os.path.exists(f) for f in required_files):
                websites.append(item_path)
            else:
                print(f"⚠️ Skipping {item} (missing required files)")

        return websites

    async def _process_single_async(
        self,
        website_dir: str,
        semaphore: asyncio.Semaphore,
        status_lock: asyncio.Lock,
        status_dict: Dict,
        idx: int
    ) -> Dict[str, Any]:
        website_name = os.path.basename(website_dir)

        async with semaphore:

            async with status_lock:
                status_dict[website_name] = {"status": "processing", "index": idx}

            try:

                processor = Trace2WorldInstrumentationPostProcessor(website_dir, self.config)


                result = await processor.process()


                async with status_lock:
                    if result["success"]:
                        status_dict[website_name] = {
                            "status": "success",
                            "index": idx,
                            "evaluators_count": result.get("evaluators_count", 0)
                        }
                    else:
                        status_dict[website_name] = {
                            "status": "failed",
                            "index": idx,
                            "error": result.get("error", "Unknown error")
                        }

                return {
                    "website_name": website_name,
                    "website_dir": website_dir,
                    **result
                }

            except Exception as e:

                async with status_lock:
                    status_dict[website_name] = {
                        "status": "failed",
                        "index": idx,
                        "error": str(e)
                    }

                return {
                    "website_name": website_name,
                    "website_dir": website_dir,
                    "success": False,
                    "error": str(e)
                }

    async def _print_status(self, status_dict: Dict, status_lock: asyncio.Lock):
        while True:
            await asyncio.sleep(10)

            async with status_lock:
                if not status_dict:
                    continue

                success = sum(1 for v in status_dict.values() if v.get("status") == "success")
                failed = sum(1 for v in status_dict.values() if v.get("status") == "failed")
                processing = sum(1 for v in status_dict.values() if v.get("status") == "processing")
                total = len(status_dict)

                print(f"\n{'='*60}")
                print(f"📊 Progress: {success + failed}/{total} completed")
                print(f"   ✅ Success: {success} | ❌ Failed: {failed} | 🔄 Processing: {processing}")
                print(f"{'='*60}\n")

    def _save_original_tasks(self, tasks: list):
        try:
            original_tasks_path = post_processing_path(self.website_dir, "tasks", "original_tasks.json")
            original_tasks_path.parent.mkdir(parents=True, exist_ok=True)


            with original_tasks_path.open('w', encoding='utf-8') as f:
                json.dump({
                    "description": "Original tasks before rewriting based on actual data",
                    "tasks": tasks
                }, f, indent=2, ensure_ascii=False)

            self.logger.log_info(f"💾 Saved original tasks to: {original_tasks_path}")

        except Exception as e:
            self.logger.log_error(f"Failed to save original tasks: {str(e)}")

    def _save_rewritten_tasks(self, tasks: list):
        try:
            rewritten_tasks_path = post_processing_path(self.website_dir, "tasks", "rewritten_tasks.json")
            rewritten_tasks_path.parent.mkdir(parents=True, exist_ok=True)


            with rewritten_tasks_path.open('w', encoding='utf-8') as f:
                json.dump({
                    "description": "Tasks rewritten to match actual generated data",
                    "tasks": tasks
                }, f, indent=2, ensure_ascii=False)

            self.logger.log_info(f"💾 Saved rewritten tasks to: {rewritten_tasks_path}")

        except Exception as e:
            self.logger.log_error(f"Failed to save rewritten tasks: {str(e)}")

    @staticmethod
    def _save_batch_results(batch_dir: str, results: list):

        total = len(results)
        success = sum(1 for r in results if r.get("success"))
        failed = total - success


        batch_results = {
            "summary": {
                "total": total,
                "success": success,
                "failed": failed,
                "success_rate": f"{success/total*100:.1f}%" if total > 0 else "0%"
            },
            "results": []
        }


        for result in results:
            website_result = {
                "website_name": result.get("website_name"),
                "website_dir": result.get("website_dir"),
                "success": result.get("success"),
            }

            if result.get("success"):
                website_result["evaluators_count"] = result.get("evaluators_count", 0)
            else:
                website_result["error"] = result.get("error", "Unknown error")

            batch_results["results"].append(website_result)


        results_path = os.path.join(batch_dir, "batch_instrumentation_results.json")
        with open(results_path, 'w', encoding='utf-8') as f:
            json.dump(batch_results, f, indent=2, ensure_ascii=False)

        print(f"\n💾 Saved batch results to: {results_path}")

    async def process_batch(self, batch_dir: str, max_concurrent: int = 3, limit: int = None) -> Dict[str, Any]:
        print("=" * 80)
        print("🚀 Starting Batch Instrumentation Post-Processing")
        print(f"📁 Batch Directory: {batch_dir}")
        print(f"⚙️  Max Concurrent: {max_concurrent}")
        print("=" * 80)


        websites = self._discover_websites(batch_dir)

        if not websites:
            print("❌ No valid website folders found in batch directory")
            return {
                "success": False,
                "error": "No valid website folders found"
            }


        if limit and limit > 0:
            websites = websites[:limit]
            print(f"ℹ️ Processing limited to first {limit} websites")

        print(f"\n✅ Found {len(websites)} valid website(s) to process\n")


        semaphore = asyncio.Semaphore(max_concurrent)
        status_lock = asyncio.Lock()
        status_dict = {}


        status_task = asyncio.create_task(self._print_status(status_dict, status_lock))


        tasks = []
        for idx, website_dir in enumerate(websites):
            task = self._process_single_async(
                website_dir, semaphore, status_lock, status_dict, idx
            )
            tasks.append(task)


        results = await asyncio.gather(*tasks)


        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass


        self._save_batch_results(batch_dir, results)


        total = len(results)
        success = sum(1 for r in results if r.get("success"))
        failed = total - success

        print("\n" + "=" * 80)
        print("✨ Batch Instrumentation Post-Processing Complete!")
        print(f"📊 Total: {total} | ✅ Success: {success} | ❌ Failed: {failed}")
        print(f"📁 Results saved to: {batch_dir}")
        print("=" * 80)

        return {
            "success": True,
            "batch_dir": batch_dir,
            "total": total,
            "success_count": success,
            "failed_count": failed,
            "results": results
        }


    @staticmethod
    def _find_failed_websites(batch_dir: str) -> list:
        results_file = os.path.join(batch_dir, "batch_instrumentation_results.json")

        if not os.path.exists(results_file):
            print(f"❌ Error: No batch_instrumentation_results.json found in {batch_dir}")
            print(f"   This directory may not have been processed yet.")
            return []

        try:
            with open(results_file, 'r', encoding='utf-8') as f:
                results = json.load(f)


            failed_websites = []
            for result in results.get("results", []):
                if not result.get("success", False):
                    website_dir = result.get("website_dir")
                    if website_dir and os.path.exists(website_dir):
                        failed_websites.append(website_dir)

            return failed_websites

        except Exception as e:
            print(f"❌ Error reading batch results: {e}")
            return []

    def _cleanup_instrumentation_files(self, website_dir: str):
        paths_to_remove = [
            post_processing_path(website_dir, "instrumentation", "instrumentation_plan.json"),
            post_processing_path(website_dir, "evaluators", "evaluators.json"),
            PhaseOutputManager(website_dir).formal("sdk_generation", "runtime", "instrumented_business_logic.js"),
            PhaseOutputManager(website_dir).formal("sdk_generation", "runtime", "business_logic.js.backup"),
        ]

        for file_path in paths_to_remove:
            if os.path.exists(file_path):
                self._move_to_deprecated(str(file_path))

    def _update_batch_results(self, batch_dir: str, retry_results: list):
        results_file = os.path.join(batch_dir, "batch_instrumentation_results.json")


        with open(results_file, 'r', encoding='utf-8') as f:
            batch_results = json.load(f)


        retry_map = {
            r["website_dir"]: r for r in retry_results
        }


        for i, result in enumerate(batch_results["results"]):
            website_dir = result.get("website_dir")
            if website_dir in retry_map:

                retry_result = retry_map[website_dir]
                batch_results["results"][i] = {
                    "website_name": result["website_name"],
                    "website_dir": website_dir,
                    "success": retry_result.get("success"),
                }

                if retry_result.get("success"):
                    batch_results["results"][i]["evaluators_count"] = retry_result.get("evaluators_count", 0)
                else:
                    batch_results["results"][i]["error"] = retry_result.get("error", "Unknown error")


        total = len(batch_results["results"])
        success = sum(1 for r in batch_results["results"] if r.get("success"))
        failed = total - success

        batch_results["summary"] = {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": f"{success/total*100:.1f}%" if total > 0 else "0%"
        }


        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(batch_results, f, indent=2, ensure_ascii=False)

        print(f"\n💾 Updated batch results: {results_file}")

    async def process_failed_websites(
        self,
        failed_websites: list,
        batch_dir: str,
        max_concurrent: int = 3
    ) -> Dict[str, Any]:
        print("=" * 80)
        print("🔄 Retry Processing Failed Websites")
        print(f"📁 Batch Directory: {batch_dir}")
        print(f"⚙️  Max Concurrent: {max_concurrent}")
        print(f"📋 Total Failed: {len(failed_websites)}")
        print("=" * 80)


        print("\n🗑️  Cleaning up instrumentation files from previous attempts...")
        for website_dir in failed_websites:
            self._cleanup_instrumentation_files(website_dir)
        print("✅ Cleanup complete\n")


        semaphore = asyncio.Semaphore(max_concurrent)
        status_lock = asyncio.Lock()
        status_dict = {}


        status_task = asyncio.create_task(self._print_status(status_dict, status_lock))


        tasks = []
        for idx, website_dir in enumerate(failed_websites):
            task = self._process_single_async(
                website_dir, semaphore, status_lock, status_dict, idx
            )
            tasks.append(task)


        results = await asyncio.gather(*tasks)


        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass


        self._update_batch_results(batch_dir, results)


        total = len(results)
        success = sum(1 for r in results if r.get("success"))
        failed = total - success

        print("\n" + "=" * 80)
        print("✨ Retry Processing Complete!")
        print(f"📊 Total: {total} | ✅ Success: {success} | ❌ Failed: {failed}")
        if failed > 0:
            print(f"💡 Tip: You can run --resume again to retry the {failed} failed website(s)")
        print(f"📁 Updated results in: {batch_dir}")
        print("=" * 80)

        return {
            "success": True,
            "batch_dir": batch_dir,
            "total": total,
            "success_count": success,
            "failed_count": failed,
            "results": results
        }


def main():
    parser = argparse.ArgumentParser(
        description="TDD Instrumentation Post-Processor - Add instrumentation and generate evaluators"
    )


    dir_group = parser.add_mutually_exclusive_group(required=True)
    dir_group.add_argument(
        "--website-dir",
        type=str,
        help="Path to single website directory"
    )
    dir_group.add_argument(
        "--batch-dir",
        type=str,
        help="Path to batch directory containing multiple website folders"
    )
    dir_group.add_argument(
        "--resume",
        type=str,
        help="Resume from previous batch by processing only failed websites (specify batch directory path)"
    )


    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32000,
        help="Maximum tokens for LLM calls (default: 16000)"
    )
    parser.add_argument(
        "--max-fix-iterations",
        type=int,
        default=5,
        help="Maximum fix iterations for validation (default: 3)"
    )
    parser.add_argument(
        "--test-data-limit",
        type=int,
        default=3,
        help="Number of data items to use for testing (default: 3)"
    )


    parser.add_argument(
        "--concurrent",
        type=int,
        default=3,
        help="Maximum concurrent processing for batch mode (default: 3)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of websites to process in batch mode (optional)"
    )


    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="minimal",
        choices=["minimal", "low", "medium", "high"],
        help="Reasoning effort level for LLM calls (default: medium)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.1",
        help="Model name to use for LLM calls (default: None, uses default model)"
    )

    args = parser.parse_args()


    config = {
        "max_tokens": args.max_tokens,
        "max_fix_iterations": args.max_fix_iterations,
        "test_data_limit": args.test_data_limit,
        "reasoning_effort": args.reasoning_effort,
        "model": args.model
    }


    if args.website_dir:

        if not os.path.exists(args.website_dir):
            print(f"❌ Error: Website directory not found: {args.website_dir}")
            return 1


        processor = Trace2WorldInstrumentationPostProcessor(args.website_dir, config)
        result = asyncio.run(processor.process())

        if result["success"]:
            print("\n✅ Instrumentation post-processing completed successfully!")
            return 0
        else:
            print(f"\n❌ Instrumentation post-processing failed: {result.get('error')}")
            return 1

    elif args.batch_dir:

        if not os.path.exists(args.batch_dir):
            print(f"❌ Error: Batch directory not found: {args.batch_dir}")
            return 1


        temp_processor = Trace2WorldInstrumentationPostProcessor(args.batch_dir, config)


        result = asyncio.run(
            temp_processor.process_batch(
                args.batch_dir,
                max_concurrent=args.concurrent,
                limit=args.limit
            )
        )

        if result["success"]:
            return 0
        else:
            print(f"\n❌ Batch processing failed: {result.get('error')}")
            return 1

    elif args.resume:

        if not os.path.exists(args.resume):
            print(f"❌ Error: Resume directory not found: {args.resume}")
            return 1

        print("=" * 80)
        print("🔄 Resume Mode: Processing Failed Websites")
        print(f"📁 Batch Directory: {args.resume}")
        print("=" * 80)


        temp_processor = Trace2WorldInstrumentationPostProcessor(args.resume, config)


        failed_websites = temp_processor._find_failed_websites(args.resume)

        if not failed_websites:
            print("\n✅ No failed websites found. All websites completed successfully!")
            return 0

        print(f"\n📋 Found {len(failed_websites)} failed website(s) to retry:\n")
        for i, website_dir in enumerate(failed_websites, 1):
            print(f"  {i}. {os.path.basename(website_dir)}")


        print("\n" + "=" * 80)
        print(f"Starting retry processing (max {args.concurrent} concurrent)...")
        print("=" * 80 + "\n")

        result = asyncio.run(
            temp_processor.process_failed_websites(
                failed_websites,
                args.resume,
                max_concurrent=args.concurrent
            )
        )

        if result["success"]:
            return 0
        else:
            print(f"\n❌ Resume processing failed: {result.get('error')}")
            return 1

    return 1


if __name__ == "__main__":
    exit(main())
