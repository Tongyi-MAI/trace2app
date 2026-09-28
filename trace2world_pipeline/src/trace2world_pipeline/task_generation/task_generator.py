
import json
from typing import List, Dict, Any, Optional
from trace2world_pipeline.core.llm_caller import call_openai_api_json


class Trace2WorldTaskGenerator:

    def __init__(self, logger=None, min_steps=6, max_steps=10, model=None, reasoning_effort=None):
        self.logger = logger
        self.min_steps = min_steps
        self.max_steps = max_steps
        self.model = model
        self.reasoning_effort = reasoning_effort

    def generate_tasks(self, website_type: str,
                      task_count_range: str = "3-5",
                      custom_task_names: List[str] = None,
                      reference_tasks: List[Dict[str, Any]] = None,
                      observed_evidence: Dict[str, Any] = None,
                      site_model=None) -> List[Dict[str, Any]]:
        if site_model is not None:
            reference_tasks = site_model.metadata.get("reference_tasks", [])
            nav = site_model.structure.global_navigation
            sg = site_model.structure.site_graph
            observed_evidence = {
                "primary_architecture": {
                    "pages": [
                        {
                            "name": p.page_name,
                            "filename": p.filename,
                            "description": p.description,
                            "primary_functions": p.primary_functions or []
                        }
                        for p in site_model.structure.page_prototypes
                        if p.confidence >= 0.2
                    ],
                    **nav,
                },
                "entity_schemas": [
                    {
                        "name": e.get("name", ""),
                        "description": e.get("description", ""),
                        "fields": [
                            {
                                "name": f.get("name"),
                                "type": f.get("type", "string"),
                                "observed_examples": f.get("observed_examples", [])[:4],
                            }
                            for f in e.get("fields", [])
                            if isinstance(f, dict) and f.get("name")
                        ],
                    }
                    for e in site_model.content.entity_schemas
                ],
                "entity_relationships": site_model.content.entity_relationships,
                "route_hierarchy": sg.route_hierarchy,
                "navigation_flows": [
                    {
                        "from": e.from_page_name or e.from_page_id,
                        "to": e.to_page_name or e.to_page_id,
                        "trigger": e.trigger,
                    }
                    for e in sg.edges
                ],
            }
        if self.logger:
            self.logger.log_info(f"Generating tasks for {website_type}")
            if custom_task_names:
                self.logger.log_info(f"Using custom task names: {custom_task_names}")

        try:

            if custom_task_names and len(custom_task_names) > 0:
                prompt = self._build_custom_tasks_prompt(
                    website_type,
                    custom_task_names,
                    observed_evidence
                )
                prompt_type = "custom tasks"
            else:
                prompt = self._build_auto_tasks_prompt(
                    website_type,
                    task_count_range,
                    reference_tasks,
                    observed_evidence
                )
                prompt_type = "automatic generation"


            call_id = None
            if self.logger:
                call_id = self.logger.log_api_call(
                    "Generate User Tasks",
                    prompt,
                    additional_args={'function': 'call_openai_api_json'},
                    stage="Generate Tasks"
                )


            messages = [{"role": "user", "content": prompt}]
            result, usage_info = call_openai_api_json(
                messages,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="generate_tasks",
            )


            if self.logger:
                self.logger.log_api_response(
                    "Generate User Tasks",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    stage="Generate Tasks",
                    call_id=call_id
                )


            if isinstance(result, str):
                result = json.loads(result)

            tasks = result.get("tasks", [])


            validated_tasks = self._validate_tasks(tasks)

            if self.logger:
                self.logger.log_info(f"Successfully generated {len(validated_tasks)} tasks using {prompt_type}")

            return validated_tasks

        except Exception as e:
            if self.logger:
                self.logger.log_error(f"Failed to generate tasks: {str(e)}")
            raise

    def _build_custom_tasks_prompt(self, website_type: str,
                                   custom_task_names: List[str],
                                   observed_evidence: Dict[str, Any] = None) -> str:
        hybrid_section = self._build_hybrid_section(observed_evidence)
        return f"""
You are a UX researcher. Generate detailed user tasks based on the following task names for a {website_type}.

Task names provided by user:
{json.dumps(custom_task_names, ensure_ascii=False, indent=2)}

IMPORTANT REQUIREMENTS:
1. This is a mock website, so tasks should NOT depend on any external services like email authentication.
2. Each task MUST contain between {self.min_steps}-{self.max_steps} detailed steps for proper complexity.
3. Tasks should be suitable for RL model training, requiring multiple decisions and interactions.
4. Ensure all tasks can be completed within the localStorage environment.
5. Tasks should NOT contain file upload steps (e.g., uploading photos/images/documents).

For each task name provided:
- Create a complete task structure with ID, name, description and detailed steps
- The name should match the provided task name BUT add SPECIFIC criteria (e.g., if given "buy shoes", make it "buy running shoes under $100 with 4+ star rating")
- Description MUST include MEASURABLE success criteria (specific prices, quantities, ratings, features)
- Steps MUST be {self.min_steps}-{self.max_steps} detailed, specific ACTIONS (not verifications)
- Include EXACT selection criteria in steps (e.g., "click the first product", "select the cheapest option", "choose the item with highest rating")

Step detail requirements (ACTIONS ONLY):
- Clear navigation actions (e.g., "Navigate to the products page")
- Specific element interactions (e.g., "Click the menu button in the top-right corner")
- Precise data entry (e.g., "Type 'electronics' in the search field")
- Selection actions (e.g., "Select 'Size Large' from the dropdown")
- Form filling (e.g., "Enter 'john@example.com' in the email field")
- Page transitions (e.g., "Click the 'Continue' button to go to next page")
- List interactions (e.g., "Click on the second item in the results list")

AVOID verification/validation/confirmation steps - focus only on actions!

Example of SPECIFIC task with clear criteria:
Original request: "Search and filter products"
Enhanced task: "Find the cheapest Dell laptop between $500-$1500 with SSD storage"
Description: "Search for laptops, filter by Dell brand and $500-$1500 price range, select the cheapest one with SSD"
Steps:
1. Navigate to the homepage
2. Click on the search bar in the header section
3. Type "laptop SSD" in the search field
4. Press Enter to execute the search
5. Click on the "Price" filter section in the left sidebar
6. Enter "500" in the minimum price field
7. Enter "1500" in the maximum price field
8. Click the "Brand" filter checkbox for "Dell"
9. Click the sort dropdown and select "Price - Low to High"
10. Click on the first product card (the cheapest Dell laptop with SSD)

Return your response in JSON format:
{{
    "tasks": [
        {{
            "id": "task_1",
            "name": "Enhanced task name with SPECIFIC criteria added to the original",
            "description": "DETAILED description with EXACT success criteria (prices, quantities, ratings, specific features to select)",
            "steps": ["Specific action 1", "Specific action 2", "... ({self.min_steps}-{self.max_steps} ACTION steps with clear selection criteria)"]
        }}
    ]
}}
{hybrid_section}
"""

    def _build_auto_tasks_prompt(self, website_type: str,
                                 task_count_range: str,
                                 reference_tasks: List[Dict[str, Any]] = None,
                                 observed_evidence: Dict[str, Any] = None) -> str:

        reference_section = ""
        if reference_tasks:
            reference_section = f"""
REFERENCE TASKS FROM REAL WEBSITE TRACES:
The following tasks were performed by real users on the original website. Use them as reference for the difficulty level, style, and scope of tasks you generate. Do NOT copy them directly, but generate similar tasks that match the website's functionality.

{json.dumps(reference_tasks, indent=2, ensure_ascii=False)}

"""
        hybrid_section = self._build_hybrid_section(observed_evidence)

        return f"""
You are a UX researcher. Generate {task_count_range} realistic user tasks for a {website_type}.
{reference_section}

IMPORTANT REQUIREMENTS:
1. This is a mock website, so tasks should NOT depend on any external services like email authentication.
2. Each task MUST contain between {self.min_steps}-{self.max_steps} detailed steps for proper complexity.
3. Tasks should be suitable for RL model training, requiring multiple decisions and interactions.
4. Tasks should NOT contain file upload steps (e.g., uploading photos/images/documents).

Each task should:
- Represent a SPECIFIC user goal with MEASURABLE success criteria
- Contain {self.min_steps}-{self.max_steps} DETAILED action steps
- Include CLEAR decision criteria (e.g., "select the cheapest option", "choose items with 4+ stars")
- Specify EXACT targets (e.g., "add 3 items under $50", "find products with free shipping")
- Use CONCRETE values and thresholds (prices, quantities, ratings, dates)
- Cover different aspects of the website functionality

Task specificity requirements:
- BAD: "Compare products and select the best one"
- GOOD: "Compare two laptops and select the one with more RAM under $1000"
- BAD: "Search for headphones and add to cart"
- GOOD: "Search for wireless headphones under $200 with 4+ star rating and add the first result to cart"
- BAD: "Filter products by price"
- GOOD: "Filter products to show only items between $25-$75 with free shipping"

Step detail requirements (FOCUS ON ACTIONS, NOT VERIFICATION):
- Specific navigation actions (e.g., "Navigate to the homepage", "Go to the Electronics category")
- Clear element interactions (e.g., "Click the search button in the header", "Click the third product in the list")
- Precise data entry (e.g., "Type 'wireless headphones' in the search field", "Enter quantity as 2")
- Selection actions (e.g., "Select 'Blue' from the color dropdown", "Choose 'Express Shipping' option")
- Page transitions (e.g., "Click on the product image to open details page", "Navigate to checkout page")
- Form interactions (e.g., "Fill in the email field with 'user@example.com'", "Enter '12345' as ZIP code")
- List/grid interactions (e.g., "Scroll down to view more products", "Click 'Load More' button")

AVOID these types of steps:
- Verification steps (e.g., "Verify the page loaded", "Confirm the cart updated")
- Validation steps (e.g., "Validate the price is correct", "Check that the item appears")
- Confirmation steps (e.g., "Ensure the button is visible", "Make sure the form is complete")
- Waiting without action (e.g., "Wait for page to load" - instead specify next action directly)
- URL paths or fragments (e.g., do NOT write "/waimai/home", "#menu", "(/checkout/payment)" — describe UI elements and actions only)

AVOID these types of tasks entirely:
- Browse-only tasks with no concrete terminal state (e.g., "browse stylists then return to the deals tab" — there is nothing to verify)
- Tasks whose final step is just navigation back to a previous page

Example of SPECIFIC task with ACTION-FOCUSED steps:
Task Name: "Find and purchase the cheapest laptop with 16GB RAM under $800"
Description: "Search for laptops, filter to 16GB RAM models under $800, and add the cheapest one to cart"
Steps:
1. Navigate to the website homepage
2. Click on the search bar in the header
3. Type "laptop 16GB RAM" in the search field
4. Press Enter to execute the search
5. Click on the price filter dropdown in the sidebar
6. Enter "800" in the maximum price field
7. Click on the RAM filter and select "16GB" checkbox
8. Click on the sort dropdown menu
9. Select "Price - Low to High" from the sorting options
10. Click on the first product card in the sorted results (cheapest)

Return your response in JSON format:
{{
    "tasks": [
        {{
            "id": "task_1",
            "name": "SPECIFIC task name with CLEAR goal (e.g., 'Buy the cheapest red shirt under $30')",
            "description": "DETAILED description with MEASURABLE success criteria (e.g., 'Find red shirts, filter under $30, sort by price, add cheapest to cart')",
            "steps": ["Specific action 1", "Specific action 2", "... ({self.min_steps}-{self.max_steps} ACTION steps total, NO verifications)"]
        }}
    ]
}}
{hybrid_section}
"""

    def _build_hybrid_section(self,
                              observed_evidence: Dict[str, Any] = None) -> str:
        if not observed_evidence:
            return ""
        return f"""

Hybrid Pipeline Inputs:
IMPORTANT — PARTIAL OBSERVATION: The evidence below was collected from a limited set of agent traces and covers only a PARTIAL view of the website. Many pages, features, and user flows exist in the website but were NOT captured. Do NOT treat this as the complete site structure.

ObservedEvidence (REAL facts extracted from actual website traces; do not fabricate):
{json.dumps(observed_evidence or {}, indent=2, ensure_ascii=False)}

How to use each evidence field:
- primary_architecture.pages: Base task flows on the actual pages and their primary_functions. Only reference pages that exist.
- primary_architecture.*_links (header_links, footer_links, sidebar_links, etc.): Use the exact nav labels when writing step descriptions (e.g., "Click 'Shop' in the header" not "Click the navigation menu").
- entity_schemas[].fields[].observed_examples: Use these real values as concrete task criteria — actual brand names, category values, price points. Prefer these over invented values.
- entity_schemas[].fields[].type: Use type to choose appropriate operators (number fields → range filters; string fields → text/category filters).
- entity_relationships: Use to design tasks that span related entities (e.g., order→product→category cross-entity lookups).
- route_hierarchy.root_routes: Entry-point pages reachable directly from the homepage — prefer starting tasks here.
- route_hierarchy.by_parent_route: Child routes (detail pages) that must be reached via their parent listing page — use this to sequence navigation steps correctly.
- navigation_flows with kind=navigation: Design multi-step tasks along confirmed from→to page transitions only.
- navigation_flows with kind=filter_update/query_update: Include filter/search interaction steps that match confirmed trigger actions.

TASK DIVERSITY REQUIREMENT (mandatory):
Roughly HALF of all tasks must be GROUNDED (anchored to the observed evidence above), and roughly HALF must be SYNTHETIC (covering functionality typical for this website type but NOT directly observed in the traces).

Grounded tasks: use exact page names, routes, entity fields, nav labels, and data values from the evidence. Add "evidence_based": true to these tasks.
Synthetic tasks: infer unobserved but typical functionality for this website type — examples include user profile management, order history, wishlist/favorites, review submission, coupon/voucher redemption, search on unobserved categories, account/payment settings, notifications, cross-category browsing, etc. Add "synthetic_rationale" explaining which unobserved feature this covers.

Hard constraints:
1. Keep all real facts unchanged — do not rename pages, fields, or entities.
2. Grounded and synthetic tasks must be roughly equal in count.
3. Prefer tasks that exercise navigation_flows: tasks that cross multiple pages are more valuable.
"""

    def _validate_tasks(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        validated = []

        for i, task in enumerate(tasks):

            if not isinstance(task, dict):
                if self.logger:
                    self.logger.log_warning(f"Skipping invalid task at index {i}: not a dictionary")
                continue


            if not all(key in task for key in ["name", "description", "steps"]):
                if self.logger:
                    self.logger.log_warning(f"Skipping incomplete task at index {i}")
                continue


            if "id" not in task:
                task["id"] = f"task_{i+1}"


            if not isinstance(task["steps"], list) or len(task["steps"]) == 0:
                if self.logger:
                    self.logger.log_warning(f"Task {task['id']} has invalid steps")
                continue


            step_count = len(task["steps"])
            if step_count < self.min_steps:
                if self.logger:
                    self.logger.log_warning(
                        f"Task {task['id']} has only {step_count} steps (minimum {self.min_steps} required). "
                        f"Task may be too simple for effective RL training."
                    )

            elif step_count > self.max_steps:
                if self.logger:
                    self.logger.log_warning(
                        f"Task {task['id']} has {step_count} steps (maximum {self.max_steps} recommended). "
                        f"Truncating to first {self.max_steps} steps."
                    )

                task["steps"] = task["steps"][:self.max_steps]


            valid_steps = []
            for j, step in enumerate(task["steps"]):
                if isinstance(step, str) and len(step.strip()) > 10:
                    valid_steps.append(step)
                else:
                    if self.logger:
                        self.logger.log_warning(
                            f"Task {task['id']}, step {j+1} is too short or invalid, skipping"
                        )


            task["steps"] = valid_steps


            if len(task["steps"]) < self.min_steps:
                if self.logger:
                    self.logger.log_error(
                        f"Task {task['id']} has insufficient valid steps after validation "
                        f"({len(task['steps'])} < {self.min_steps}). Skipping task."
                    )
                continue

            validated.append(task)

        return validated

    def generate_task_summary(self, tasks: List[Dict[str, Any]]) -> str:
        summary = f"Generated {len(tasks)} tasks:\n"
        summary += "=" * 40 + "\n"

        for task in tasks:
            summary += f"\n📌 {task['id']}: {task['name']}\n"
            summary += f"   {task['description']}\n"
            summary += f"   Steps: {len(task.get('steps', []))}\n"

        return summary
