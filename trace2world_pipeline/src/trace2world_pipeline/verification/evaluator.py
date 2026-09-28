
import asyncio
import json
from typing import List, Dict, Any
from dataclasses import dataclass, asdict
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.verification.data_models import InstrumentationPlan
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async


@dataclass
class Trace2WorldEvaluator:
    task_id: str
    name: str
    description: str
    localStorage_variables: List[str]
    evaluation_logic: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trace2WorldEvaluator':
        return cls(**data)


class Trace2WorldInstrumentationEvaluator:

    def __init__(self, logger: Trace2WorldLogger = None, max_retries: int = 3,
                 reasoning_effort: str = "medium", model: str = None):
        self.logger = logger or Trace2WorldLogger()
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.model = model

    async def generate_evaluators(self,
                                  tasks: List[Dict[str, Any]],
                                  instrumentation_plan: InstrumentationPlan,
                                  datadict: Dict[str, Any],
                                  static_data_types: List[str],
                                  business_logic_code: str = None,
                                  test_data: Dict[str, Any] = None,
                                  website_data: Dict[str, Any] = None) -> List[Trace2WorldEvaluator]:
        self.logger.start_stage("Generate Evaluators")
        self.logger.log_info("📝 Generating evaluators based on instrumentation variables...")

        try:

            var_mapping = self._prepare_variable_mapping(instrumentation_plan)


            evaluators_data = await self._call_llm_for_evaluators(
                tasks=tasks,
                var_mapping=var_mapping,
                static_data_types=static_data_types,
                datadict=datadict,
                business_logic_code=business_logic_code,
                test_data=test_data,
                website_data=website_data
            )


            evaluators = []
            for eval_data in evaluators_data.get("evaluators", []):
                evaluator = Trace2WorldEvaluator.from_dict(eval_data)
                evaluators.append(evaluator)

            self.logger.log_info(f"✅ Generated {len(evaluators)} evaluators")
            self.logger.end_stage("Generate Evaluators")

            return evaluators

        except Exception as e:
            error_msg = f"Failed to generate evaluators: {str(e)}"
            self.logger.log_error(error_msg)
            self.logger.end_stage("Generate Evaluators")
            raise Exception(error_msg)

    def _prepare_variable_mapping(self, plan: InstrumentationPlan) -> Dict[str, Any]:
        def _payload_contract(obj):
            value_spec = getattr(obj, 'value_spec', None)
            return ((value_spec.to_dict() if hasattr(value_spec, 'to_dict') else value_spec) if value_spec else None)

        def _payload_fields(obj):
            value_spec = getattr(obj, 'value_spec', None)
            if not value_spec:
                return []
            if hasattr(value_spec, 'fields'):
                fields = value_spec.fields
                kind = getattr(value_spec, 'kind', '')
            else:
                fields = value_spec.get('fields', [])
                kind = value_spec.get('kind', '')
            if kind != 'object':
                return []
            return [f.get('name') for f in fields if f.get('name')]

        def _sig_entry(name, role, obj, stage=''):
            return {
                'name': name,
                'role': role,
                'stage': stage,
                'type': obj.variable_type,
                'expected_value': obj.value_to_set,
                'payload_contract': _payload_contract(obj),
                'payload_fields': _payload_fields(obj),
            }

        mapping = {}
        for req in plan.requirements:
            progress_entries = [
                _sig_entry(sig.signal_name, sig.signal_role, sig, sig.stage)
                for sig in (getattr(req, 'progress_signals', None) or [])
            ]
            success_entry = None
            if getattr(req, 'success_signal', None):
                sig = req.success_signal
                success_entry = _sig_entry(sig.signal_name, sig.signal_role, sig, sig.stage)

            legacy_entries = [
                _sig_entry(var.variable_name, 'legacy', var, '')
                for var in req.required_variables
            ]

            mapping[req.task_id] = {
                'task_name': req.task_name,
                'needs_instrumentation': req.needs_instrumentation,
                'existing_variables': req.existing_variables,
                'progress_signals': progress_entries,
                'success_signal': success_entry,
                'instrumentation_variables': progress_entries + ([success_entry] if success_entry else []) + legacy_entries,
            }
        return mapping

    def _extract_relevant_methods(self, code: str, tasks: list, var_mapping: dict, max_chars: int = 12000) -> str:
        import re


        essential_patterns = [
            r'constructor\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}',
            r'_initStorage\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}',
            r'_getFromStorage\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}',
            r'_saveToStorage\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}',
            r'_generateId\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}',
        ]


        target_methods = set()
        for task_id, mapping in var_mapping.items():
            for var_info in mapping.get('existing_variables', []):
                if isinstance(var_info, dict):

                    var_name = var_info.get('name', '') or var_info.get('variable_name', '')

                    target_methods.add(var_name)
                elif isinstance(var_info, str):
                    target_methods.add(var_info)
            for var_info in mapping.get('instrumentation_variables', []):
                if isinstance(var_info, dict):
                    var_name = var_info.get('name', '')
                    target_methods.add(var_name)


        for task in tasks:
            task_str = json.dumps(task, ensure_ascii=False)

            for match in re.findall(r'\b([a-z][a-zA-Z]+(?:Item|Cart|Order|User|Product|List|Search|Filter|Add|Remove|Update|Delete|Get|Set|Create|Submit|Save|Load|Check|Toggle|Register|Login|Logout))\b', task_str):
                target_methods.add(match)


        extracted_parts = []


        for pattern in essential_patterns:
            matches = re.findall(pattern, code, re.DOTALL)
            for m in matches[:1]:
                if m not in '\n'.join(extracted_parts):
                    extracted_parts.append(m)


        lines = code.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]

            method_match = re.match(r'\s+(async\s+)?(\w+)\s*\(', line)
            if method_match:
                method_name = method_match.group(2)
                if method_name in target_methods or any(t.lower() in method_name.lower() for t in target_methods if len(t) > 3):

                    brace_count = 0
                    method_lines = []
                    for j in range(i, len(lines)):
                        method_lines.append(lines[j])
                        brace_count += lines[j].count('{') - lines[j].count('}')
                        if brace_count <= 0 and '{' in '\n'.join(method_lines):
                            break
                    method_body = '\n'.join(method_lines)
                    if method_body not in '\n'.join(extracted_parts):
                        extracted_parts.append(method_body)
            i += 1

        if extracted_parts:
            result = '\n\n'.join(extracted_parts)
            if len(result) <= max_chars:
                return result

            return result[:max_chars] + '\n// ... (truncated, showing task-relevant methods only)'


        if len(code) > max_chars:
            return code[:max_chars] + '\n// ... (truncated to first ' + str(max_chars) + ' chars)'
        return code

    async def _call_llm_for_evaluators_single(self,
                                              tasks: List[Dict[str, Any]],
                                              var_mapping: Dict[str, Any],
                                              static_data_types: List[str],
                                              datadict: Dict[str, Any] = None,
                                              business_logic_code: str = None,
                                              test_data: Dict[str, Any] = None,
                                              website_data: Dict[str, Any] = None) -> Dict[str, Any]:

        business_logic_section = ""
        if business_logic_code:

            code_snippet = self._extract_relevant_methods(business_logic_code, tasks, var_mapping)
            business_logic_section = f"""
BUSINESS LOGIC IMPLEMENTATION:
This is the actual implementation code that shows how data is stored and manipulated in localStorage.
Pay special attention to the data structures used (e.g., objects vs strings, array structures, etc.)

```javascript
{code_snippet}
```
"""

        datadict_section = ""
        if datadict:
            datadict_section = f"""
DATA DICTIONARY:
This shows the data model structure and relationships between entities.

{json.dumps(datadict, indent=2)}
"""

        website_data_section = ""
        if website_data:

            truncated_data = {}
            for key, value in website_data.items():
                if isinstance(value, list) and len(value) > 10:

                    truncated_data[key] = value[:10] + [{"...": f"and {len(value) - 10} more items"}]
                else:
                    truncated_data[key] = value

            website_data_section = f"""
COMPLETE WEBSITE DATA (Static Initial Data):
This is the actual data that exists in seed_data.json. Use this to:
1. Reference actual IDs, names, and values that exist in the data
2. Understand the exact data structure and format (e.g., whether editHistory is strings or objects)
3. Know which data structures exist (e.g., whether 'reports' array exists)
4. Use correct field names and values in your evaluators

{json.dumps(truncated_data, indent=2)}

IMPORTANT: This is the STATIC data loaded at initialization. Some features may create or modify data at runtime.
Check the business logic code to understand how data is stored in localStorage (direct arrays vs object wrappers).
"""

        test_data_section = ""
        if test_data:
            test_data_section = f"""
TEST DATA (First 3 items of each entity):
{json.dumps(test_data, indent=2)}
"""

        prompt = f"""You are generating evaluators to check if users completed tasks successfully.

TASKS:
{json.dumps(tasks, indent=2)}

INSTRUMENTATION VARIABLES AVAILABLE:
{json.dumps(var_mapping, indent=2)}

STATIC DATA TYPES:
{json.dumps(static_data_types, indent=2)}
{business_logic_section}{datadict_section}{website_data_section}{test_data_section}
INSTRUCTIONS:
For each task, create an evaluator based on the instrumentation plan:

**CRITICAL: Use BOTH the WEBSITE DATA and BUSINESS LOGIC IMPLEMENTATION!**
- Use the COMPLETE WEBSITE DATA to reference actual IDs, values, and field names that exist
- Check what data structures exist in the static data (e.g., is there a 'reports' array?)
- Look at how data is stored in localStorage in the business_logic code
- Match the exact data structure used in the implementation
- Understand the difference between static data format and runtime format:
  - Static data (seed_data.json): Initial data loaded on first visit
  - Runtime data (localStorage): May have different format after business logic processing
  - Example: editHistory might be strings in static data but objects at runtime
- For example, if the code stores favorites as objects with {{id, bookId, addedAt}}, check for that structure
- DO NOT assume simple arrays if the implementation uses objects
- DO NOT reference data structures that don't exist in seed_data.json unless they're created at runtime

**Case 1: Tasks with needs_instrumentation=true (has progress_signals / success_signal / instrumentation_variables)**
- Use progress_signals to evaluate correct business-stage progression
- Use success_signal to evaluate final task completion
- If both are available, success MUST dominate the final score
- Legacy instrumentation_variables may still appear for backward compatibility; prefer progress_signals / success_signal semantics when present

**Case 2: Tasks with needs_instrumentation=false (has existing_variables only)**
- Use the existing_variables to infer task completion
- Check the ACTUAL data structure from the business logic implementation
- For example, if existing_variables includes "favoritebooks":
  - Check the business_logic to see if it stores strings or objects
  - If objects like {{id, bookId, addedAt}}, use: favorites.some(f => f.bookId === id)
  - If strings, use: favorites.includes(id)

All evaluators must:
- Check if the variables exist in localStorage
- Use the EXACT data structure from the business logic implementation
- Return a SCORE from 0.0 to 1.0 (NOT a boolean)

GUIDELINES:
- Use localStorage.getItem() to access variables
- Parse JSON when retrieving objects: JSON.parse(localStorage.getItem('key') || 'null')
- Check for null/undefined before accessing properties
- Use the exact variable names from the instrumentation mapping
- Include both instrumentation variables and any existing variables in localStorage_variables list

## SCORING FORMAT (IMPORTANT!)

Instead of returning true/false, return a score from 0.0 to 1.0 based on checkpoints.
When progress_signals and success_signal are available, structure checkpoints in two layers:
- progress checkpoints: reward correct intermediate business-stage progression
- success checkpoint(s): reward final task completion

The success checkpoint(s) must dominate the final score. Without success, the score must remain bounded away from 1.0.

Use a checkpoint-based scoring pattern:

```javascript
const checkpoints = [];
// Checkpoint 1: Data exists (weight)
checkpoints.push({{ passed: dataExists, weight: 0.1 }});
// Checkpoint 2: Found qualifying items (weight)
checkpoints.push({{ passed: qualifyingFound, weight: 0.2 }});
// Checkpoint 3-N: Action completed for each item (weight each)
[1,2,3].forEach(n => {{
  checkpoints.push({{ passed: completed.length >= n, weight: 0.2 }});
}});
// Calculate final score
return checkpoints.reduce((sum, cp) => sum + (cp.passed ? cp.weight : 0), 0);
```

CHECKPOINT WEIGHT GUIDELINES:
- Total weights MUST sum to 1.0
- **CRITICAL: Only reward CORRECT behavior on the path to task completion**

**DO NOT create checkpoints for:**
  - Data existence checks (e.g., "data array exists") - these are free points unrelated to user behavior
  - Generic actions (e.g., "user clicked any item") - these can be reward-hacked
  - System state that doesn't depend on user actions

**DO create checkpoints for:**
  - User performed filtering/search with CORRECT criteria matching task requirements
  - User selected/interacted with items that MATCH the task criteria
  - User completed the final action with the CORRECT target

**Example for "buy cheapest plan with roadside assistance":**
  - CP1 (0.30): User filtered with roadside=true (verified via instrumentation)
  - CP2 (0.30): User selected a plan that HAS roadside assistance (check selected ID against data)
  - CP3 (0.40): User selected the CHEAPEST qualifying plan (compare selected with computed cheapest)

This design prevents reward hacking - partial credit is only given for actions on the correct path

**CRITICAL ANTI-HACKING RULES (MUST FOLLOW):**

**RULE 1: ALWAYS combine "action" and "correctness" into ONE checkpoint**

This is the MOST IMPORTANT rule. NEVER create separate checkpoints for "did user do X" and "was X correct".

BAD PATTERNS (DO NOT USE):
- CP1="user searched for something", CP2="search was for correct criteria"
- CP1="user bookmarked restaurants", CP2="bookmarked restaurants meet criteria"
- CP1="user requested directions", CP2="directions were for correct market"
- CP1="user added items to cart", CP2="items meet price requirement"

GOOD PATTERNS (USE THESE):
- CP1="user bookmarked restaurants that HAVE ≥4 stars AND are open on Wednesday"
- CP1="user requested directions for the CORRECT (nearest Saturday) market"
- CP1="user added items to cart that ARE under $15 AND are stainless steel"
- CP1="user searched with CORRECT filter parameters (rating≥4, day=Wednesday)"

WHY THIS MATTERS:
- If split: User gets 50% for bookmarking ANY restaurant (wrong behavior rewarded)
- If combined: User gets 0% for bookmarking wrong restaurants (only correct behavior rewarded)

IMPLEMENTATION PATTERN:
```javascript
// BAD - Split (rewards wrong actions):
checkpoints.push({{ passed: bookmarkedIds.length >= 2, weight: 0.30 }});  // ANY bookmarks
checkpoints.push({{ passed: allBookmarksQualify, weight: 0.30 }});  // Correctness separate

// GOOD - Combined (only rewards correct actions):
const qualifyingBookmarks = bookmarkedIds.filter(id => {{
  const r = restaurants.find(r => r.id === id);
  return r && r.rating >= 4 && r.openDays.includes('Wednesday');
}});
checkpoints.push({{ passed: qualifyingBookmarks.length >= 2, weight: 0.60 }});  // Only qualifying count
```

**RULE 2: DO NOT hardcode target_ids - use DYNAMIC validation**
- BAD: `ids.includes('rest_001') && ids.includes('rest_003')` (hardcoded IDs)
- GOOD: `items.filter(item => item.rating >= 4 && item.openDays.includes('Wednesday'))` (dynamic check)
- The ground_truth.target_ids are for REFERENCE only, not for hardcoding in evaluators
- Evaluator MUST verify selected items match task CRITERIA dynamically

**SELF-CHECK BEFORE GENERATING:**
For each checkpoint, ask: "Can a user get points for this by doing the WRONG thing?"
- If YES → Combine with correctness check
- If NO → Checkpoint is valid

EXAMPLE 1 (with instrumentation_variables - tracking CORRECT behavior):
Task: "Buy cheapest auto insurance with roadside assistance"
Instrumentation variables: task1_filterParams (object), task1_selectedPlanId (string)
Existing variables: insuranceplans

Evaluator (NO data existence checks - only user behavior):
{{
  "task_id": "task_1",
  "name": "Buy Cheapest Roadside Plan Evaluator",
  "description": "Checks if user filtered correctly, selected a qualifying plan, and chose the cheapest one",
  "localStorage_variables": ["task1_filterParams", "task1_selectedPlanId", "insuranceplans"],
  "evaluation_logic": "const checkpoints = [];\\nconst filterParams = JSON.parse(localStorage.getItem('task1_filterParams') || 'null');\\nconst selectedId = localStorage.getItem('task1_selectedPlanId');\\nconst plans = JSON.parse(localStorage.getItem('insuranceplans') || '[]');\\n// Find qualifying plans (with roadside) and cheapest\\nconst qualifying = plans.filter(p => p.hasRoadsideAssistance);\\nconst cheapest = qualifying.sort((a,b) => a.price - b.price)[0];\\nconst selectedPlan = plans.find(p => p.id === selectedId);\\n// CP1 (0.30): User used correct filter (roadside=true)\\ncheckpoints.push({{ passed: filterParams && filterParams.hasRoadside === true, weight: 0.30 }});\\n// CP2 (0.30): User selected a plan WITH roadside assistance\\ncheckpoints.push({{ passed: selectedPlan && selectedPlan.hasRoadsideAssistance, weight: 0.30 }});\\n// CP3 (0.40): User selected the CHEAPEST qualifying plan\\ncheckpoints.push({{ passed: cheapest && selectedId === cheapest.id, weight: 0.40 }});\\nreturn checkpoints.reduce((sum, cp) => sum + (cp.passed ? cp.weight : 0), 0);"
}}

EXAMPLE 2 (with existing_variables only - multiple items, NO data existence check):
Task: "Add 3 qualifying books (rating >= 4.5) to favorites"
Instrumentation variables: (none - needs_instrumentation=false)
Existing variables: favoritebooks, books
Business Logic shows: favorites.push({{id: 'fav_001', bookId: bookId, addedAt: new Date().toISOString()}})

Evaluator (only checks if CORRECT books were added):
{{
  "task_id": "task_5",
  "name": "Favorite Qualifying Books Evaluator",
  "description": "Checks if user added books with rating >= 4.5 to favorites",
  "localStorage_variables": ["favoritebooks", "books"],
  "evaluation_logic": "const checkpoints = [];\\nconst favorites = JSON.parse(localStorage.getItem('favoritebooks') || '[]');\\nconst books = JSON.parse(localStorage.getItem('books') || '[]');\\n// Find which favorited books are qualifying (rating >= 4.5)\\nconst qualifyingFavorites = favorites.filter(f => {{ const book = books.find(b => b.id === f.bookId); return book && book.rating >= 4.5; }});\\n// CP1-3: Each qualifying book added (0.33 each) - NO data existence check!\\n[1,2,3].forEach(n => {{ checkpoints.push({{ passed: qualifyingFavorites.length >= n, weight: 0.33 }}); }});\\nreturn checkpoints.reduce((sum, cp) => sum + (cp.passed ? cp.weight : 0), 0);"
}}

RETURN FORMAT (JSON):
{{
  "evaluators": [
    {{
      "task_id": "task_1",
      "name": "Evaluator Name",
      "description": "What this evaluator checks",
      "localStorage_variables": ["var1", "var2"],
      "evaluation_logic": "// JavaScript code using checkpoint pattern\\nconst checkpoints = [];\\n// ... checkpoint definitions ...\\nreturn checkpoints.reduce((sum, cp) => sum + (cp.passed ? cp.weight : 0), 0);"
    }}
  ]
}}

IMPORTANT:
- For tasks with needs_instrumentation=true: Use the instrumentation_variables
- For tasks with needs_instrumentation=false: Use the existing_variables to infer completion
- NEVER return 0 simply because instrumentation_variables is empty - check existing_variables!
- The evaluation_logic must return a NUMBER between 0.0 and 1.0, NOT a boolean
- Always use the checkpoint pattern shown above
- Ensure checkpoint weights sum to exactly 1.0"""


        call_id = self.logger.log_api_call(
            "Generate Evaluators",
            prompt
        )


        for attempt in range(self.max_retries):
            try:
                result, usage_info = await call_openai_api_json_async(
                    [{"role": "user", "content": prompt}],
                    reasoning_effort=self.reasoning_effort,
                    model=self.model,
                    stage="generate_evaluators",
                )

                self.logger.log_api_response(
                    "Generate Evaluators",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    call_id=call_id
                )


                if isinstance(result, str):
                    result = json.loads(result)


                if not isinstance(result, dict) or "evaluators" not in result:
                    raise ValueError("Invalid response - missing 'evaluators'")


                for eval_data in result["evaluators"]:
                    required_fields = ["task_id", "name", "description", "localStorage_variables", "evaluation_logic"]
                    for field in required_fields:
                        if field not in eval_data:
                            raise ValueError(f"Missing required field '{field}' in evaluator")

                return result

            except Exception as e:
                self.logger.log_error(f"API call failed on attempt {attempt + 1}: {str(e)}")
                if attempt == self.max_retries - 1:
                    self.logger.log_api_response(
                        "Generate Evaluators",
                        success=False,
                        error=str(e),
                        call_id=call_id
                    )
                    raise

        raise Exception("Failed after max retries")

    async def _call_llm_for_single_task_evaluator(self,
                                                   task: Dict[str, Any],
                                                   task_var_mapping: Dict[str, Any],
                                                   static_data_types: List[str],
                                                   datadict: Dict[str, Any] = None,
                                                   business_logic_code: str = None,
                                                   test_data: Dict[str, Any] = None,
                                                   website_data: Dict[str, Any] = None) -> Dict[str, Any]:

        business_logic_section = ""
        if business_logic_code:
            code_snippet = self._extract_relevant_methods(
                business_logic_code, [task], {task.get('id', 'unknown'): task_var_mapping}
            )
            business_logic_section = f"""
BUSINESS LOGIC IMPLEMENTATION (relevant methods for this task):
```javascript
{code_snippet}
```
"""

        datadict_section = ""
        if datadict:
            datadict_section = f"""
DATA DICTIONARY:
{json.dumps(datadict, indent=2)}
"""


        website_data_section = ""
        if website_data:

            relevant_keys = set()
            for var_info in task_var_mapping.get('existing_variables', []):
                if isinstance(var_info, dict):
                    var_name = var_info.get('name', '') or var_info.get('variable_name', '')
                    if var_name:
                        relevant_keys.add(var_name)
                elif isinstance(var_info, str):
                    relevant_keys.add(var_info)

            filtered_data = {}
            for key, value in website_data.items():
                if key in relevant_keys or any(rk in key for rk in relevant_keys):
                    if isinstance(value, list) and len(value) > 5:
                        filtered_data[key] = value[:5] + [{"...": f"and {len(value) - 5} more items"}]
                    else:
                        filtered_data[key] = value

            if filtered_data:
                website_data_section = f"""
RELEVANT WEBSITE DATA (for this task):
{json.dumps(filtered_data, indent=2)}
"""

        test_data_section = ""
        if test_data:
            test_data_section = f"""
TEST DATA (First 3 items of each entity):
{json.dumps(test_data, indent=2)}
"""

        prompt = f"""You are generating an evaluator to check if a user completed a specific task successfully.

TASK:
{json.dumps(task, indent=2)}

INSTRUMENTATION VARIABLES FOR THIS TASK:
{json.dumps(task_var_mapping, indent=2)}

STATIC DATA TYPES:
{json.dumps(static_data_types, indent=2)}
{business_logic_section}{datadict_section}{website_data_section}{test_data_section}
INSTRUCTIONS:
Create ONE evaluator for this specific task based on the instrumentation plan.

If needs_instrumentation=true: validate the instrumentation_variables match expected values.
If needs_instrumentation=false: use existing_variables to infer task completion.

The evaluator's evaluation_logic should be a JavaScript function body that:
1. Reads localStorage variables
2. Checks conditions for task completion
3. Returns true if the task was completed successfully, false otherwise

Return JSON format:
{{
    "evaluators": [
        {{
            "task_id": "{task.get('id', '')}",
            "name": "evaluator name",
            "description": "what this evaluator checks",
            "localStorage_variables": ["var1", "var2"],
            "evaluation_logic": "javascript function body that returns true/false"
        }}
    ]
}}
"""

        response, usage = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            stage="generate_single_evaluator",
        )

        if isinstance(response, str):
            response = json.loads(response)

        return response

    async def _call_llm_for_evaluators(self,
                                       tasks: List[Dict[str, Any]],
                                       var_mapping: Dict[str, Any],
                                       static_data_types: List[str],
                                       datadict: Dict[str, Any] = None,
                                       business_logic_code: str = None,
                                       test_data: Dict[str, Any] = None,
                                       website_data: Dict[str, Any] = None) -> Dict[str, Any]:
        if len(tasks) <= 3:

            return await self._call_llm_for_evaluators_single(
                tasks, var_mapping, static_data_types, datadict,
                business_logic_code, test_data, website_data
            )

        self.logger.log_info(f"Using parallel per-task evaluator generation ({len(tasks)} tasks)")


        eval_tasks = []
        for task in tasks:
            task_id = task.get('id', '')
            task_var = var_mapping.get(task_id, {})

            eval_tasks.append(
                self._call_llm_for_single_task_evaluator(
                    task=task,
                    task_var_mapping=task_var,
                    static_data_types=static_data_types,
                    datadict=datadict,
                    business_logic_code=business_logic_code,
                    test_data=test_data,
                    website_data=website_data
                )
            )


        results = await asyncio.gather(*eval_tasks, return_exceptions=True)


        all_evaluators = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.logger.log_error(f"Evaluator generation failed for task {tasks[i].get('id', i)}: {result}")
                continue
            if isinstance(result, dict):
                all_evaluators.extend(result.get('evaluators', []))

        self.logger.log_info(f"Generated {len(all_evaluators)} evaluators from {len(tasks)} parallel calls")
        return {"evaluators": all_evaluators}
