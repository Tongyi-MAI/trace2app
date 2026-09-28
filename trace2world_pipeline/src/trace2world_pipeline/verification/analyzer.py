
import json
import re
from typing import List, Dict, Any
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.verification.data_models import (
    InstrumentationPlan,
    InstrumentationRequirement,
    InstrumentationVariable,
    InstrumentationSignal,
)
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async


class Trace2WorldInstrumentationAnalyzer:

    def __init__(self, logger: Trace2WorldLogger = None, max_retries: int = 3,
                 reasoning_effort: str = "medium", model: str = None):
        self.logger = logger or Trace2WorldLogger()
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.model = model

    async def analyze_requirements(self,
                                   tasks: List[Dict[str, Any]],
                                   business_logic_code: str,
                                   datadict: Dict[str, Any]) -> InstrumentationPlan:
        self.logger.start_stage("Analyze Instrumentation Requirements")
        self.logger.log_info(f"🔍 Analyzing instrumentation needs for {len(tasks)} tasks...")


        existing_storage_vars = self._extract_existing_storage_vars(business_logic_code)
        self.logger.log_info(f"Found {len(existing_storage_vars)} existing localStorage variables")


        storage_keys = self._extract_storage_keys(datadict)
        self.logger.log_info(f"Found {len(storage_keys)} data storage keys")

        try:

            requirements_data = await self._call_llm_for_analysis(
                tasks=tasks,
                business_logic_code=business_logic_code,
                existing_storage_vars=existing_storage_vars,
                storage_keys=storage_keys
            )


            plan = self._parse_requirements(requirements_data)


            self._enforce_state_mutating_instrumentation(plan, tasks, business_logic_code)

            self.logger.log_info(f"✅ Analysis complete:")
            self.logger.log_info(f"   - Tasks needing instrumentation: {sum(1 for r in plan.requirements if r.needs_instrumentation)}")
            self.logger.log_info(f"   - Total new variables needed: {len(plan.get_all_variables())}")
            self.logger.end_stage("Analyze Instrumentation Requirements")

            return plan

        except Exception as e:
            error_msg = f"Failed to analyze instrumentation requirements: {str(e)}"
            self.logger.log_error(error_msg)
            self.logger.end_stage("Analyze Instrumentation Requirements")
            raise Exception(error_msg)

    def _extract_existing_storage_vars(self, code: str) -> List[str]:
        import re


        patterns = [
            r"localStorage\.setItem\(['\"]([^'\"]+)['\"]",
            r"localStorage\.getItem\(['\"]([^'\"]+)['\"]",
            r"localStorage\.removeItem\(['\"]([^'\"]+)['\"]"
        ]

        vars_found = set()
        for pattern in patterns:
            matches = re.findall(pattern, code)
            vars_found.update(matches)

        return sorted(list(vars_found))

    def _extract_storage_keys(self, datadict: Dict[str, Any]) -> List[str]:
        storage_keys = []
        for entity in datadict.get("entities", []):
            if "storage_key" in entity:
                storage_keys.append(entity["storage_key"])
        return storage_keys

    async def _call_llm_for_analysis(self,
                                     tasks: List[Dict[str, Any]],
                                     business_logic_code: str,
                                     existing_storage_vars: List[str],
                                     storage_keys: List[str]) -> Dict[str, Any]:

        code_snippet = self._prepare_code_snippet(business_logic_code, max_lines=None)

        prompt = f"""You are analyzing JavaScript business logic to determine what instrumentation variables are needed to evaluate task completion.

CONTEXT:
- Each task describes a user action that must be completed on the website
- The business logic (SDK) implements functions that the website calls
- We need to add minimal instrumentation variables to localStorage to track task completion
- Instrumentation must NOT affect original functionality (non-invasive)

TASKS TO EVALUATE:
{json.dumps(tasks, indent=2)}

CURRENT BUSINESS LOGIC (complete):
{code_snippet}

EXISTING LOCALSTORAGE VARIABLES:
{json.dumps(existing_storage_vars, indent=2)}

DATA STORAGE KEYS:
{json.dumps(storage_keys, indent=2)}

ANALYSIS REQUIREMENTS:
For each task, determine:
1. What business stages must the agent pass through for the task to be completed correctly?
2. Which of those stages deserve process supervision as progress signals?
3. What is the final state-mutating business action that should serve as the success signal?
4. If the task involves state-mutating operations, it MUST have instrumentation — existing data is NOT sufficient.
5. For passive/read-only tasks (browsing, viewing, searching without commitment), existing data MAY be sufficient.

CRITICAL RULE — STATE-MUTATING TASKS ALWAYS NEED INSTRUMENTATION:
If any SDK method called during the task creates, modifies, or commits data (e.g., adding to cart, starting a booking, placing an order, confirming a selection), that task MUST have instrumentation. The reason: knowing "a booking exists in localStorage" tells us nothing about WHETHER the user performed the correct steps to create it. We need to track that the user called the RIGHT function with the RIGHT parameters.

Do NOT mark a task as needs_instrumentation: false just because the data entities (venues, deals, categories) already exist in localStorage. Data existence ≠ action verification.

INSTRUMENTATION GUIDELINES:
- Only add signals if existing localStorage is insufficient
- Do not artificially limit the number of progress signals; include as many as needed to represent the correct business-stage progression
- Progress signals must represent meaningful business milestones, not raw DOM clicks, page visits, or generic navigation
- Each task with instrumentation should output:
  - progress_signals: zero or more meaningful business-stage milestones
  - success_signal: exactly one final state-mutating business action
- Use clear naming convention:
  - progress_<task>_<stage>
  - success_<task>_<stage>
- Specify exactly which function should set each signal and under what condition
- Signals should be boolean, string, or simple objects

CRITICAL - TRACK CORRECTNESS, NOT JUST ACTIONS:
- Don't just track "user performed search" - track "user searched with CORRECT criteria"
- Don't just track "user selected item" - track "user selected item that MATCHES task requirements"
- Store enough context to verify correctness in evaluator (e.g., filter params, selected item ID)

INSTRUMENTATION PATTERNS FOR CORRECTNESS:
1. For filtering tasks: Store the filter parameters used
   - Example: task1_filterParams (object with filter criteria like {{hasRoadside: true}})
   - This allows evaluator to check if user used CORRECT filters

2. For selection tasks: Store the selected item ID
   - Example: task1_selectedPlanId (string)
   - Evaluator can verify if selected item MATCHES task requirements

3. For comparison tasks: Store which items were compared
   - Example: task1_comparedItemIds (array of IDs)
   - Evaluator can verify if user compared the RIGHT items

4. For multi-step tasks: Track completion of each CORRECT business stage
   - Example progress signals: progress_task1_correctFilterUsed, progress_task1_qualifyingItemSelected
   - Example success signal: success_task1_finalCheckoutCreated
   - Progress signals may be multiple; do NOT cap them to a fixed number if the task genuinely has more stages

RETURN FORMAT (JSON):
{{
  "analysis_summary": "Brief summary of findings",
  "requirements": [
    {{
      "task_id": "task_1",
      "task_name": "Task name",
      "task_description": "Task description",
      "needs_instrumentation": true/false,
      "reasoning": "Why instrumentation is/isn't needed",
      "existing_variables": ["var1", "var2"],  // If sufficient
      "progress_signals": [
        {{
          "signal_name": "progress_task1_searchCompleted",
          "signal_role": "progress",
          "stage": "search_completed",
          "variable_type": "object",
          "set_in_function": "searchNeighborhoods",
          "set_condition": "After successful search with results.length > 0",
          "value_to_set": "",  // legacy fallback, still supported
          "value_spec": {{
            "kind": "object",
            "fields": [
              {{"name": "query", "expr": "query"}},
              {{"name": "resultCount", "expr": "results.length"}}
            ]
          }},
          "reason": "Tracks that the agent reached the correct search stage"
        }}
      ],
      "success_signal": {{
        "signal_name": "success_task1_selectionCommitted",
        "signal_role": "success",
        "stage": "selection_committed",
        "variable_type": "object",
        "set_in_function": "selectNeighborhood",
        "set_condition": "After successful selection commit",
        "value_to_set": "",
        "value_spec": {{
          "kind": "object",
          "fields": [
            {{"name": "selectedId", "expr": "selectedId"}}
          ]
        }},
        "reason": "Tracks the final success action for the task"
      }},
      "required_variables": [  // legacy fallback only
        {{
          "variable_name": "task1_searchCompleted",
          "variable_type": "boolean",
          "set_in_function": "searchNeighborhoods",
          "set_condition": "After successful search with results.length > 0",
          "value_to_set": "true",
          "reason": "Legacy fallback"
        }}
      ]
    }}
  ]
}}

IMPORTANT: Be conservative - only add instrumentation if truly necessary for evaluating task completion."""


        call_id = self.logger.log_api_call(
            "Analyze Instrumentation Requirements",
            prompt
        )


        for attempt in range(self.max_retries):
            try:
                result, usage_info = await call_openai_api_json_async(
                    [{"role": "user", "content": prompt}],
                    reasoning_effort=self.reasoning_effort,
                    model=self.model,
                    stage="analyze_instrumentation_requirements",
                )


                self.logger.log_api_response(
                    "Analyze Instrumentation Requirements",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    call_id=call_id
                )


                if isinstance(result, str):
                    result = json.loads(result)


                if not isinstance(result, dict) or "requirements" not in result:
                    raise ValueError("Invalid response structure - missing 'requirements'")

                return result

            except Exception as e:
                self.logger.log_error(f"API call failed on attempt {attempt + 1}: {str(e)}")
                if attempt == self.max_retries - 1:
                    self.logger.log_api_response(
                        "Analyze Instrumentation Requirements",
                        success=False,
                        error=str(e),
                        call_id=call_id
                    )
                    raise

        raise Exception("Failed after max retries")

    def _prepare_code_snippet(self, code: str, max_lines: int = None) -> str:

        if max_lines is None:
            return code

        lines = code.split('\n')
        if len(lines) <= max_lines:
            return code


        snippet = '\n'.join(lines[:max_lines])
        snippet += f"\n\n... (truncated {len(lines) - max_lines} more lines)"
        return snippet

    def _parse_requirements(self, data: Dict[str, Any]) -> InstrumentationPlan:
        requirements = []

        for req_data in data.get("requirements", []):

            variables = []
            for var_data in req_data.get("required_variables", []):
                variable = InstrumentationVariable(
                    variable_name=var_data["variable_name"],
                    variable_type=var_data["variable_type"],
                    set_in_function=var_data["set_in_function"],
                    set_condition=var_data["set_condition"],
                    value_to_set=var_data.get("value_to_set", ""),
                    reason=var_data["reason"],
                    value_spec=var_data.get("value_spec")
                )
                variables.append(variable)

            progress_signals = []
            for sig_data in req_data.get("progress_signals", []):
                progress_signals.append(InstrumentationSignal.from_dict(sig_data))

            success_signal = None
            if isinstance(req_data.get("success_signal"), dict):
                success_signal = InstrumentationSignal.from_dict(req_data["success_signal"])


            requirement = InstrumentationRequirement(
                task_id=req_data["task_id"],
                task_name=req_data["task_name"],
                task_description=req_data["task_description"],
                needs_instrumentation=req_data["needs_instrumentation"],
                existing_variables=req_data.get("existing_variables", []),
                required_variables=variables,
                progress_signals=progress_signals,
                success_signal=success_signal,
            )
            requirements.append(requirement)

        return InstrumentationPlan(requirements=requirements)

    def _enforce_state_mutating_instrumentation(self, plan: InstrumentationPlan,
                                                  tasks: List[Dict[str, Any]],
                                                  business_logic_code: str) -> None:

        state_mutation_patterns = [
            r'(?i)(start|begin|create|submit|confirm|place|book|reserve)\w*(?:booking|order|checkout|purchase|payment|reservation)',
            r'(?i)add\w*(?:to|To)?\w*(?:cart|basket|order)',
            r'(?i)(?:select|choose|pick)\w*(?:deal|offer|room|package|payment|method|combo)',
            r'(?i)(?:apply|redeem)(?:coupon|voucher|discount)',
            r'(?i)(?:save|update|delete|remove|clear)\w*(?:cart|selection|booking)',
            r'(?i)(?:set|update)\w*(?:quantity|amount|status)',
            r'(?i)(?:checkout|completeOrder|finalize)',
        ]


        for req in plan.requirements:
            if req.needs_instrumentation:
                continue


            task_text = f"{req.task_name} {req.task_description}"
            for step_data in [t for t in tasks if t.get("id", "") == req.task_id or t.get("task_id", "") == req.task_id]:
                for step in step_data.get("steps", []):
                    if isinstance(step, dict):
                        task_text += " " + str(step.get("action", ""))
                        task_text += " " + str(step.get("expected_result", ""))
                    else:
                        task_text += " " + str(step)


            has_state_mutation = False
            for pattern in state_mutation_patterns:
                if re.search(pattern, task_text):
                    has_state_mutation = True
                    break


            if has_state_mutation:
                req.needs_instrumentation = True
                self.logger.log_info(
                    f"[Analyzer] Forced instrumentation for task '{req.task_id}' "
                    f"(state-mutating operation detected)"
                )
