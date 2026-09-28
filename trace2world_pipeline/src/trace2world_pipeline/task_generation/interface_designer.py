
import json
import asyncio
import os
import re
from typing import List, Dict, Any, Optional
from trace2world_pipeline.core.llm_caller import call_openai_api_json, call_openai_api_json_async
from trace2world_pipeline.core.phase_output import PhaseOutputManager


class Trace2WorldInterfaceDesigner:

    def __init__(self, logger=None, model=None, reasoning_effort=None):
        self.logger = logger
        self.model = model
        self.reasoning_effort = reasoning_effort

    def _interface_design_report_dir(self) -> str:
        if not self.logger or not getattr(self.logger, "output_dir", None):
            return ""
        return str(PhaseOutputManager(self.logger.output_dir).debug("interface_design", "design_interfaces"))

    def design_interfaces(self, tasks: List[Dict[str, Any]], data_models: Dict[str, Any],
                          website_type: str, primary_architecture: Any = None,
                          observed_evidence: Dict[str, Any] = None,
                          site_model=None,
                          trace_context: Dict[str, Any] = None,
                          trace_prompt_context: str = "") -> Dict[str, Any]:
        if site_model is not None:
            data_models = {
                "entities": site_model.content.entity_schemas,
                "relationships": site_model.content.entity_relationships,
                "metadata": {"source": "site_model"}
            }
            primary_architecture = {
                "pages": [
                    {
                        "name": p.page_name,
                        "filename": p.filename,
                        "primary_functions": p.primary_functions or []
                    }
                    for p in site_model.structure.page_prototypes
                    if p.confidence >= 0.2
                ]
            }
            observed_evidence = [
                {"page": p.filename, "page_name": p.page_name, "function": fn}
                for p in site_model.structure.page_prototypes
                for fn in (p.primary_functions or [])
            ]
        prompt_data_models = self._compact_data_models_for_prompt(data_models)

        architecture_context = ""
        if primary_architecture:
            pages_info = []
            if hasattr(primary_architecture, 'pages'):
                pages = primary_architecture.pages
            elif isinstance(primary_architecture, dict):
                pages = primary_architecture.get('pages', [])
            else:
                pages = []

            for page in pages:
                page_name = page.get('name', 'Unknown') if isinstance(page, dict) else getattr(page, 'name', 'Unknown')
                page_functions = page.get('primary_functions', []) if isinstance(page, dict) else getattr(page, 'primary_functions', [])
                pages_info.append(f"- {page_name}: {', '.join(page_functions)}")

            if pages_info:
                architecture_context = f"""

        Website Pages and Functions:
        {chr(10).join(pages_info)}
        """
        hybrid_context = self._build_hybrid_context(observed_evidence)


        prompt = f"""
        You are a software architect. Design comprehensive interfaces for both user tasks AND page functionality.

        Website Type: {website_type}
        User Tasks: {json.dumps(tasks, indent=2)}
        Data Models: {json.dumps(prompt_data_models, indent=2)}
        {architecture_context}

        IMPORTANT REQUIREMENTS:
        1. Design USER-FACING interfaces that will be directly called from UI
        2. This is for SINGLE-USER agent training - NO userId, sessionId parameters
        3. System state (cart, session) should be managed internally, not passed as parameters
        4. External/System UI Exclusion: ignore OS and non-target-app surfaces in trace screenshots, including verification SMS previews, OS notification banners, incoming call bars, system permission sheets, app switcher surfaces, keyboard/IME chrome, and status-bar-only system UI. Do NOT create interfaces, return fields, helper functions, page assignments, or task actions for those regions.
        5. Search Entry Dynamic State: for search-entry or suggestion-entry pages within the same stable search scope, input text, search history, recommendation chips, recommended queries, and autocomplete/typeahead rows are dynamic state within the same page instance. A different visible scope anchor that changes query interpretation or result/action applicability is not input text state; keep it represented as a distinct page instance and pass it as an explicit scope/context parameter. Design query-driven interfaces such as getSearchSuggestions(scope, query), getSearchHistory(scope), and submitSearch(scope, query). Do NOT create per-query interfaces or page assignments for individual observed query strings. Search result pages may still use submitted query/filter parameters such as searchProducts(query, filters).

        CRITICAL: Design interfaces for TWO purposes:

        A. TASK EXECUTION INTERFACES - For user tasks:
           - What information must be shown BEFORE the user can act (display interfaces)
           - What action the user performs (action interfaces)
           - What feedback/results need to be shown AFTER the action (result interfaces)
           - CRITICAL: Trace EVERY step in EVERY task. If a step says "select coupon", "choose payment method", "apply discount", "pick a package variant" — there MUST be:
             a) A "get list" interface to retrieve available options (e.g., getApplicableCoupons, getPaymentMethods, getBookingPackageVariants)
             b) A "select/apply" interface to make the choice (e.g., applyCoupon, selectPaymentMethod, selectBookingPackage)

        B. PAGE FUNCTIONALITY INTERFACES - For each page's primary_functions:
           - Review EVERY primary_function in the Website Pages list above
           - Ensure there's an interface to support EACH function
           - Examples:
             * "Navigate to featured product categories" → needs getCategories()
             * "Display featured products" → needs getFeaturedProducts()
             * "Show product filters" → needs getFilterOptions()
             * "View product specifications" → needs getProductDetails(productId)

        4. Interfaces should handle complete operations (e.g., addToCart handles cart creation if needed)
        5. Do NOT create unnecessary CRUD, but DO create interfaces needed for page display
        6. Do not create interfaces that include URL in return values such as getNavigationLinks, as these are handled separately
        7. For interfaces that get data for display, return ALL fields the page needs to render the complete UI — not just IDs:
           - Include display-ready fields: names, descriptions, formatted prices, image URLs, ratings, status labels
           - Include counts and aggregates: total_items, subtotal, average_rating, review_count
           - Include relationship fields: category_name (not just categoryId), merchant_name, etc.
           - For list interfaces: return rich item objects with all fields visible in the UI (name, price, image, badge, subtitle)
           - CRITICAL: If a trace screenshot shows specific data (prices, ratings, distances, sales counts), the corresponding interface MUST return those fields
        8. CRITICAL - Entity Reference Parameters: For parameters that reference another data entity (foreign key pattern):
           - Identify by naming: parameter name like "xxxId" that matches an entity name (e.g., locationId → Location, categoryId → Category, fromLocationId → Location)
           - Add "entityReference" metadata to indicate this parameter should use a selector UI (not plain text input):
             {{"entity": "EntityName", "displayField": "displayName or name", "valueField": "id"}}
           - This tells the UI to render a dropdown/autocomplete showing user-friendly names, not expect users to type internal IDs
           - Examples:
             * fromLocationId, toLocationId → entityReference: {{entity: "Location", displayField: "displayName", valueField: "id"}}
             * categoryId → entityReference: {{entity: "Category", displayField: "name", valueField: "id"}}
           - Do NOT add entityReference for parameters that come from URL (already selected on previous page)
        9. CRITICAL - Array Return Types Must Specify Items:
           - For any return type with "type": "array", ALWAYS include "items" to specify element type
           - If the array contains objects from a data model, use: "items": "EntityName"
           - If the array contains simple values, explicitly define the structure with object format
           - NEVER leave array types without items specification
           - Examples:
             * Correct: {{"type": "array", "items": "Product"}}
             * Correct: {{"type": "array", "items": {{"type": "object", "properties": {{"id": {{"type": "string"}}, "name": {{"type": "string"}}}}}}}}
             * WRONG: {{"type": "array", "description": "list of categories"}} - missing items!

        Example task interfaces:
        - getProductDetails(productId) - show product info
        - addToCart(productId, quantity) - add to cart
        - getCartItems() - show cart contents
        - selectBookingPackage(packageId) - select/add a booking package or service variant
        - applyDiscount(orderId, couponId) - apply coupon to order

        Example page functionality interfaces:
        - getFeaturedProducts() - for homepage featured section
        - getCategories() - for navigation menu
        - getFilterOptions(category) - for product listing filters
        - searchProducts(query, filters) - for search functionality
        - getOrderHistory() - for order listing page

        10. CRITICAL: Ensure every user action visible in trace screenshots has a corresponding interface:
           - Selecting/adding items (packages, variants, combos) → needs a selection/add interface
           - Applying coupons/discounts → needs apply interface
           - Choosing payment methods → needs payment selection interface
           - Rating/reviewing → needs submission interface
           - Do NOT assume these are handled by generic CRUD — each distinct user action needs an explicit interface

Return JSON in this format:
        {{
            "interfaces": [
                {{
                    "name": "addToCart",
                    "description": "Add a product to cart (cart managed internally)",
                    "parameters": [
                        {{"name": "productId", "type": "string", "required": true}},
                        {{"name": "quantity", "type": "number", "required": true, "default": 1}}
                    ],
                    "returns": {{
                        "type": "object",
                        "properties": {{
                            "success": {{"type": "boolean"}},
                            "cartId": {{"type": "string"}},
                            "message": {{"type": "string"}}
                        }}
                    }},
                    "relatedTasks": ["task_1", "task_2"]
                }}
            ],
            "helperFunctions": [
                {{
                    "name": "_getOrCreateCart",
                    "description": "Internal helper to get or create cart from localStorage",
                    "visibility": "private"
                }}
            ],
            "page_assignments": {{
                "homepage.html": ["getCategories", "getFeaturedDeals"],
                "detail-3.html": ["getVenueDetail", "getBookingPackages", "selectBookingPackage"],
                "cart-11.html": ["getCartItems", "addToCart", "removeFromCart"]
            }}
        }}

        CRITICAL: The "page_assignments" field is REQUIRED. For each page filename from the Website Pages list,
        assign the interface names that page needs. A page needs an interface if:
        - The page displays data that interface returns (display interfaces)
        - The page has a button/action that calls that interface (action interfaces)
        - The user task navigates through that page and performs that action
        """ + hybrid_context


        if trace_prompt_context:
            prompt += f"""

TRACE CONTEXT (from real user traces — these are GROUND TRUTH facts about how users actually interact with the app):

{trace_prompt_context}
"""

        if self.logger:
            self.logger.log_info("Designing interfaces from tasks and data models...")


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                "Design Interfaces",
                prompt
            )

        try:
            response, usage = call_openai_api_json(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="design_interfaces",
            )


            if self.logger:
                self.logger.log_api_response(
                    "Design Interfaces",
                    success=True,
                    response=response,
                    usage_info=usage,
                    call_id=call_id
                )


            if isinstance(response, str):
                interfaces = json.loads(response)
            else:
                interfaces = response

            if self.logger:
                core_count = len(interfaces.get('interfaces', []))
                helper_count = len(interfaces.get('helperFunctions', []))
                self.logger.log_info(f"Designed {core_count} core interfaces and {helper_count} helper functions")

            return interfaces

        except Exception as e:
            import traceback
            if self.logger:
                self.logger.log_error(f"Failed to design interfaces: {str(e)}")
                self.logger.log_error(f"Stack trace:\n{traceback.format_exc()}")

                self.logger.log_api_response(
                    "Design Interfaces",
                    success=False,
                    error=str(e),
                    call_id=call_id
                )
            raise

    def _build_hybrid_context(self,
                              observed_evidence: Dict[str, Any] = None) -> str:
        if not observed_evidence:
            return ""
        return f"""

Hybrid Pipeline Inputs:
ObservedEvidence (REAL facts; do not rewrite):
{json.dumps(observed_evidence or {}, indent=2, ensure_ascii=False)}

Hard constraints:
1. Keep REAL facts unchanged.
2. For synthetic interfaces, include "synthetic_rationale".
"""

    def generate_interface_contract(self, interfaces: Dict[str, Any]) -> str:
        contract = []
        contract.append("// Core Business Logic Interfaces")
        contract.append("interface BusinessLogic {")


        for interface in interfaces.get('interfaces', []):
            params = []
            for param in interface['parameters']:
                param_type = self._ts_type(param['type'])
                required = "" if param.get('required', True) else "?"
                params.append(f"{param['name']}{required}: {param_type}")

            return_type = self._format_return_type(interface['returns'])
            contract.append(f"  // {interface['description']}")
            contract.append(f"  {interface['name']}({', '.join(params)}): {return_type};")
            contract.append("")


        if interfaces.get('helperFunctions'):
            contract.append("  // Helper Functions (internal use)")
            for helper in interfaces['helperFunctions']:
                contract.append(f"  private {helper['name']}(...args: any[]): any;")

        contract.append("}")

        return "\n".join(contract)

    def _ts_type(self, type_str: str) -> str:
        type_map = {
            "string": "string",
            "number": "number",
            "boolean": "boolean",
            "datetime": "Date",
            "array": "any[]",
            "object": "any"
        }
        return type_map.get(type_str, "any")

    def _format_return_type(self, returns: Dict[str, Any]) -> str:
        if not returns:
            return "void"

        if returns.get('type') == 'object' and 'properties' in returns:
            props = []
            for prop, details in returns['properties'].items():

                if isinstance(details, str):
                    prop_type = self._ts_type(details)
                elif isinstance(details, dict):
                    prop_type = self._ts_type(details.get('type', 'any'))
                else:
                    prop_type = 'any'
                props.append(f"{prop}: {prop_type}")

            return "{" + ", ".join(props) + "}"

        return self._ts_type(returns.get('type', 'any'))

    def design_missing_interfaces(self, missing_interfaces: Dict[str, List[Dict[str, str]]],
                                   tasks: List[Dict[str, Any]],
                                   data_models: Dict[str, Any],
                                   existing_interfaces: Dict[str, Any],
                                   website_type: str) -> Dict[str, Any]:

        missing_list = []
        for page_name, interfaces in missing_interfaces.items():
            for interface in interfaces:
                missing_list.append({
                    "page": page_name,
                    "name": interface["name"],
                    "reason": interface["reason"]
                })


        existing_names = [i["name"] for i in existing_interfaces.get("interfaces", [])]

        prompt = f"""
        You are a software architect. Design the missing interfaces that were identified during architecture review.

        Website Type: {website_type}

        Missing Interfaces Needed:
        {json.dumps(missing_list, indent=2)}

        Existing Interfaces (already designed):
        {json.dumps(existing_names, indent=2)}

        Data Models Available:
        {json.dumps(data_models, indent=2)}

        User Tasks:
        {json.dumps(tasks, indent=2)}

        REQUIREMENTS:
        1. Design ONLY the missing interfaces listed above
        2. Each interface should fulfill the specific reason provided
        3. Follow the same pattern as existing interfaces
        4. Focus on data retrieval and display operations
        5. Keep interfaces simple and focused on their specific purpose

        Common patterns for missing interfaces:
        - getFeaturedProducts(): Return a curated list of products for homepage
        - getCategories(): Return all available categories for navigation
        - getCategoryProducts(categoryId): Return products in a specific category
        - updateCartQuantity(cartItemId, quantity): Update quantity of item in cart

        Return JSON in this format:
        {{
            "interfaces": [
                {{
                    "name": "getFeaturedProducts",
                    "description": "Get featured products for homepage display",
                    "parameters": [],
                    "returns": {{
                        "type": "object",
                        "properties": {{
                            "products": {{"type": "array"}},
                            "totalCount": {{"type": "number"}}
                        }}
                    }},
                    "sideEffects": [],
                    "relatedTasks": []
                }}
            ]
        }}
        """

        if self.logger:
            self.logger.log_info(f"Designing {len(missing_list)} missing interfaces...")


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                "Design Missing Interfaces",
                prompt,
                additional_args={"missing_count": len(missing_list)}
            )

        try:
            response, usage = call_openai_api_json(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="design_missing_interfaces",
            )


            if self.logger:
                self.logger.log_api_response(
                    "Design Missing Interfaces",
                    success=True,
                    response=response,
                    usage_info=usage,
                    call_id=call_id
                )


            if isinstance(response, str):
                new_interfaces = json.loads(response)
            else:
                new_interfaces = response

            if self.logger:
                count = len(new_interfaces.get('interfaces', []))
                self.logger.log_info(f"Designed {count} missing interfaces")

            return new_interfaces

        except Exception as e:
            import traceback
            if self.logger:
                self.logger.log_error(f"Failed to design missing interfaces: {str(e)}")
                self.logger.log_error(f"Stack trace:\n{traceback.format_exc()}")

                self.logger.log_api_response(
                    "Design Missing Interfaces",
                    success=False,
                    error=str(e),
                    call_id=call_id
                )
            raise


    async def design_interfaces_parallel(self, tasks: List[Dict[str, Any]],
                                          data_models: Dict[str, Any],
                                          website_type: str,
                                          site_model=None,
                                          max_concurrent: int = 8,
                                          trace_context: Dict[str, Any] = None,
                                          trace_prompt_context: str = "") -> Dict[str, Any]:
        if site_model is not None:
            data_models = {
                "entities": site_model.content.entity_schemas,
                "relationships": site_model.content.entity_relationships,
                "metadata": {"source": "site_model"}
            }
        prompt_data_models = self._compact_data_models_for_prompt(data_models)


        dispatch_plan = await self._dispatch_interfaces(
            tasks,
            prompt_data_models,
            website_type,
            site_model,
            trace_context,
            trace_prompt_context,
            max_concurrent=max_concurrent,
        )


        group_tasks = self._build_generation_groups(dispatch_plan, prompt_data_models, website_type)
        self._write_interface_semantic_budget_report(group_tasks, trace_prompt_context)

        generate_coros = [
            self._generate_interface_group(group, trace_context, trace_prompt_context)
            for group in group_tasks
        ]
        group_results = await asyncio.gather(*generate_coros)


        assembled = self._assemble_interface_results(dispatch_plan, group_results)
        self._reconcile_interface_page_assignments(site_model, assembled)
        return assembled

    async def _dispatch_interfaces(self, tasks: List[Dict[str, Any]],
                                    data_models: Dict[str, Any],
                                    website_type: str,
                                    site_model=None,
                                    trace_context: Dict[str, Any] = None,
                                    trace_prompt_context: str = "",
                                    max_concurrent: int = 8) -> Dict[str, Any]:
        pages_info = []
        if site_model:
            for p in site_model.structure.page_prototypes:
                if p.confidence >= 0.2 and p.filename:
                    pages_info.append({
                        "filename": p.filename,
                        "page_name": p.page_name,
                        "page_type": p.page_type,
                        "primary_functions": p.primary_functions or [],
                        "description": p.description or "",
                    })
        elif 'entities' in data_models:
            pass

        max_pages_per_call = max(
            1,
            int(os.environ.get("T2W_INTERFACE_DISPATCH_MAX_PAGES_PER_CALL", "6")),
        )
        if len(pages_info) > max_pages_per_call:
            chunks = [
                pages_info[index:index + max_pages_per_call]
                for index in range(0, len(pages_info), max_pages_per_call)
            ]
            semaphore = asyncio.Semaphore(max(1, int(max_concurrent or 1)))
            max_repair_rounds = max(
                1,
                int(os.environ.get("T2W_INTERFACE_DISPATCH_MAX_REPAIR_ROUNDS", "3")),
            )

            async def run_chunk(chunk_index: int, chunk_pages: List[Dict[str, Any]]) -> Dict[str, Any]:
                async with semaphore:
                    plan = await self._dispatch_interfaces_for_pages(
                        tasks=tasks,
                        data_models=data_models,
                        website_type=website_type,
                        pages_info=chunk_pages,
                        trace_prompt_context=trace_prompt_context,
                        stage="dispatch_interfaces_shard",
                    )
                    self._validate_dispatch_plan_coverage(plan, chunk_pages, label=f"shard_{chunk_index + 1}")
                    return plan

            plans_by_index: Dict[int, Dict[str, Any]] = {}
            errors_by_index: Dict[int, str] = {}
            for _round in range(max_repair_rounds):
                missing_indices = [
                    index
                    for index in range(len(chunks))
                    if index not in plans_by_index
                ]
                if not missing_indices:
                    break
                results = await asyncio.gather(
                    *[
                        run_chunk(index, chunks[index])
                        for index in missing_indices
                    ],
                    return_exceptions=True,
                )
                for index, result in zip(missing_indices, results):
                    if isinstance(result, Exception):
                        errors_by_index[index] = f"{type(result).__name__}: {result}"
                        continue
                    plans_by_index[index] = result
                    errors_by_index.pop(index, None)

            missing_indices = [
                index
                for index in range(len(chunks))
                if index not in plans_by_index
            ]
            if missing_indices:
                errors = {
                    f"shard_{index + 1}": errors_by_index.get(index, "unknown_error")
                    for index in missing_indices
                }
                raise ValueError(f"dispatch_interfaces_shard: incomplete_shards {errors}")

            plans = [plans_by_index[index] for index in range(len(chunks))]
            return self._merge_dispatch_plans(plans)

        return await self._dispatch_interfaces_for_pages(
            tasks=tasks,
            data_models=data_models,
            website_type=website_type,
            pages_info=pages_info,
            trace_prompt_context=trace_prompt_context,
            stage="dispatch_interfaces",
        )

    async def _dispatch_interfaces_for_pages(self,
                                             *,
                                             tasks: List[Dict[str, Any]],
                                             data_models: Dict[str, Any],
                                             website_type: str,
                                             pages_info: List[Dict[str, Any]],
                                             trace_prompt_context: str = "",
                                             stage: str = "dispatch_interfaces") -> Dict[str, Any]:
        task_summaries = self._compact_tasks_for_dispatch(tasks)
        data_model_summary = self._compact_data_models_for_dispatch(data_models)
        pages_payload = [
            {
                "filename": page.get("filename", ""),
                "page_name": page.get("page_name", ""),
                "page_type": page.get("page_type", ""),
                "primary_functions": page.get("primary_functions", []) or [],
                "description": self._truncate_text(str(page.get("description", "") or ""), 220),
            }
            for page in pages_info
        ]
        prompt = f"""You are a software architect. Create a DISPATCH PLAN for interface generation.

Website Type: {website_type}
Task Summaries: {self._compact_json(task_summaries)}
Data Model Summary: {self._compact_json(data_model_summary)}
Shard Pages: {self._compact_json(pages_payload)}

Task: Analyze only the pages in Shard Pages, then output a dispatch plan that groups
their interfaces by semantic capability for parallel generation. Each interface gets
EXACTLY one semantic group.

Rules:
1. Group interfaces by semantic capability, not by page. Examples: shared entity/state boundary, action family, collection/display capability, transaction capability, or cross-page reusable page model.
2. Page filenames are evidence/assignment targets only; do not create one group per page unless that page has a genuinely unique semantic capability.
3. Output ONLY name + description + param_names + used_by + related_entities — NO full type definitions
4. Cover every primary_function and task hint that is relevant to Shard Pages.
5. Include helperFunctions for cross-cutting concerns (_getOrCreateCart, etc.)
6. Do NOT create interfaces that return URLs (navigation links are handled separately)
7. External/System UI Exclusion: ignore OS and non-target-app surfaces in trace evidence, including verification SMS previews, OS notification banners, incoming call bars, system permission sheets, app switcher surfaces, keyboard/IME chrome, and status-bar-only system UI. Do NOT create interfaces or groups for those regions.
8. Search Entry Dynamic State: for search-entry or suggestion-entry pages within the same stable search scope, input text, search history, recommendation chips, recommended queries, and autocomplete/typeahead rows are dynamic state within the same page instance. A different visible scope anchor that changes query interpretation or result/action applicability is not input text state; keep it represented as a distinct page group/assignment and pass it as an explicit scope/context parameter. Dispatch only query-driven interfaces such as getSearchSuggestions(scope, query), getSearchHistory(scope), and submitSearch(scope, query). Do NOT create per-query interfaces or groups for individual query strings.
9. This dispatch phase is a lightweight grouping pass. Do not attempt full step-by-step trace coverage here; detailed trace/spec evidence is consumed by the later interface generation phase.

Return JSON:
{{
  "capability_groups": [
    {{
      "group_id": "merchant_detail",
      "semantic_contract": "Merchant detail and menu read APIs shared by store/detail pages.",
      "used_by": ["leisure-ktv-store.html", "beauty-salon-store.html"],
      "related_entities": ["Merchant", "MenuItem"],
      "interfaces": [
        {{
          "name": "getMerchantDetail",
          "description": "Get merchant detail with ratings, address, hours, images",
          "param_names": ["merchantId"],
          "used_by": ["leisure-ktv-store.html", "beauty-salon-store.html"],
          "related_entities": ["Merchant"]
        }}
      ]
    }}
  ],
  "helper_functions": [
    {{
      "name": "_getOrCreateCart",
      "description": "Internal: get or create cart in localStorage"
    }}
  ]
}}"""

        response, _ = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            stage=stage,
        )
        if isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict):
            raise ValueError(f"{stage}: null_or_non_object_response")
        return response

    def _truncate_text(self, value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _compact_tasks_for_dispatch(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact_tasks: List[Dict[str, Any]] = []
        for index, task in enumerate(tasks or []):
            if not isinstance(task, dict):
                continue
            hints: List[str] = []
            for step in task.get("steps", []) or []:
                if isinstance(step, dict):
                    raw = " ".join(
                        str(step.get(key, "") or "")
                        for key in ("action", "target", "description", "source_page", "target_page")
                    )
                else:
                    raw = str(step or "")
                text = " ".join(raw.split())
                if not text or len(text) > 120:
                    continue
                hints.append(text)
                if len(hints) >= 8:
                    break
            compact_tasks.append({
                "id": str(task.get("id") or f"task_{index}"),
                "intent": self._truncate_text(str(task.get("name") or task.get("description") or ""), 180),
                "action_hints": hints,
            })
            if len(compact_tasks) >= 40:
                break
        return compact_tasks

    def _compact_data_models_for_dispatch(self, data_models: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data_models, dict):
            return {}
        entities = data_models.get("entities", [])
        if isinstance(entities, dict):
            iterable = [
                dict(value if isinstance(value, dict) else {}, name=str(name))
                for name, value in entities.items()
            ]
        elif isinstance(entities, list):
            iterable = [item for item in entities if isinstance(item, dict)]
        else:
            iterable = []

        compact_entities: List[Dict[str, Any]] = []
        for entity in iterable[:40]:
            name = str(entity.get("name") or entity.get("entity") or "")
            fields = entity.get("fields", [])
            if isinstance(fields, dict):
                field_names = [str(key) for key in fields.keys()]
            elif isinstance(fields, list):
                field_names = [
                    str(field.get("name") or field.get("field") or "")
                    for field in fields
                    if isinstance(field, dict)
                ]
            else:
                field_names = []
            compact_entities.append({
                key: value
                for key, value in {
                    "name": name,
                    "kind": entity.get("kind") or entity.get("type"),
                    "field_names": [
                        field
                        for field in field_names
                        if field and not (len(field) > 24 and field.upper() == field)
                    ][:8],
                }.items()
                if value not in (None, "", [], {})
            })

        relationships = data_models.get("relationships", [])
        compact_relationships = []
        for rel in relationships if isinstance(relationships, list) else []:
            if not isinstance(rel, dict):
                continue
            compact_relationships.append({
                key: rel.get(key)
                for key in ("from", "to", "type", "source", "target", "relationship")
                if rel.get(key) not in (None, "", [], {})
            })
            if len(compact_relationships) >= 40:
                break
        return {
            "entities": compact_entities,
            "relationships": compact_relationships,
            "prompt_projection": "dispatch_schema_summary",
        }

    def _merge_unique(self, existing: List[Any], incoming: List[Any]) -> List[Any]:
        merged = list(existing or [])
        for item in incoming or []:
            if item not in merged:
                merged.append(item)
        return merged

    def _validate_dispatch_plan_coverage(self,
                                         plan: Dict[str, Any],
                                         pages_info: List[Dict[str, Any]],
                                         *,
                                         label: str) -> None:
        if not isinstance(plan, dict):
            raise ValueError(f"dispatch_interfaces {label}: null_or_non_object_response")
        groups = plan.get("capability_groups", [])
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"dispatch_interfaces {label}: missing_capability_groups")
        covered = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            interfaces = group.get("interfaces", [])
            if not isinstance(interfaces, list) or not interfaces:
                raise ValueError(f"dispatch_interfaces {label}: group_missing_interfaces")
            for page in group.get("used_by", []) or []:
                if isinstance(page, str) and page:
                    covered.add(page)
            for iface in interfaces:
                if not isinstance(iface, dict):
                    continue
                for page in iface.get("used_by", []) or []:
                    if isinstance(page, str) and page:
                        covered.add(page)
        expected = {
            str(page.get("filename") or "")
            for page in pages_info
            if str(page.get("filename") or "")
        }
        missing = sorted(expected - covered)
        if missing:
            raise ValueError(f"dispatch_interfaces {label}: missing_page_coverage {missing}")

    def _merge_dispatch_plans(self, plans: List[Dict[str, Any]]) -> Dict[str, Any]:
        groups_by_id: Dict[str, Dict[str, Any]] = {}
        helper_by_name: Dict[str, Dict[str, Any]] = {}
        for plan in plans:
            for helper in plan.get("helper_functions", []) or []:
                if not isinstance(helper, dict):
                    continue
                name = str(helper.get("name") or "")
                if name and name not in helper_by_name:
                    helper_by_name[name] = dict(helper)
            groups = plan.get("capability_groups", []) or []
            for index, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("group_id") or f"capability_{index + 1}")
                dest = groups_by_id.setdefault(group_id, {
                    "group_id": group_id,
                    "semantic_contract": str(group.get("semantic_contract") or ""),
                    "used_by": [],
                    "related_entities": [],
                    "interfaces": [],
                })
                if not dest.get("semantic_contract") and group.get("semantic_contract"):
                    dest["semantic_contract"] = str(group.get("semantic_contract") or "")
                dest["used_by"] = self._merge_unique(dest.get("used_by", []), group.get("used_by", []) or [])
                dest["related_entities"] = self._merge_unique(
                    dest.get("related_entities", []),
                    group.get("related_entities", []) or [],
                )
                interfaces_by_name = {
                    str(iface.get("name") or ""): iface
                    for iface in dest.get("interfaces", [])
                    if isinstance(iface, dict) and str(iface.get("name") or "")
                }
                for iface in group.get("interfaces", []) or []:
                    if not isinstance(iface, dict):
                        continue
                    name = str(iface.get("name") or "")
                    if not name:
                        continue
                    existing = interfaces_by_name.get(name)
                    if existing is None:
                        copied = dict(iface)
                        dest["interfaces"].append(copied)
                        interfaces_by_name[name] = copied
                    else:
                        existing["used_by"] = self._merge_unique(
                            existing.get("used_by", []),
                            iface.get("used_by", []) or [],
                        )
                        existing["related_entities"] = self._merge_unique(
                            existing.get("related_entities", []),
                            iface.get("related_entities", []) or [],
                        )
        return {
            "capability_groups": list(groups_by_id.values()),
            "helper_functions": list(helper_by_name.values()),
        }

    def _build_generation_groups(self, dispatch_plan: Dict[str, Any],
                                  data_models: Dict[str, Any],
                                  website_type: str) -> List[Dict[str, Any]]:
        groups = []

        capability_groups = dispatch_plan.get("capability_groups", [])
        if isinstance(capability_groups, dict):
            capability_groups = [
                dict(value, group_id=key) if isinstance(value, dict) else {"group_id": key}
                for key, value in capability_groups.items()
            ]
        if capability_groups:
            for index, group in enumerate(capability_groups):
                if not isinstance(group, dict):
                    continue
                ifaces = group.get("interfaces", [])
                if not ifaces:
                    continue
                group_id = group.get("group_id") or "capability_{}".format(index + 1)
                groups.append({
                    "group_id": group_id,
                    "interfaces": ifaces,
                    "data_models": data_models,
                    "website_type": website_type,
                    "is_shared": False,
                    "is_capability": True,
                    "semantic_contract": group.get("semantic_contract", ""),
                    "used_by": group.get("used_by", []),
                    "related_entities": group.get("related_entities", []),
                })
            return groups


        shared = dispatch_plan.get("shared_interfaces", [])
        if shared:
            groups.append({
                "group_id": "shared",
                "interfaces": shared,
                "data_models": data_models,
                "website_type": website_type,
                "is_shared": True,
            })


        page_groups = dispatch_plan.get("page_groups", {})
        for page_fn, pg in page_groups.items():
            ifaces = pg.get("interfaces", [])
            if not ifaces:
                continue
            groups.append({
                "group_id": page_fn,
                "interfaces": ifaces,
                "data_models": data_models,
                "website_type": website_type,
                "is_shared": False,
                "page_filename": page_fn,
                "primary_functions": pg.get("primary_functions", []),
            })

        return groups

    def _normalize_return_properties(self, properties: Any) -> Dict[str, Any]:
        if isinstance(properties, dict):
            return properties
        normalized: Dict[str, Any] = {}
        for prop in properties if isinstance(properties, list) else []:
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or "").strip()
            if not name:
                continue
            normalized[name] = {
                key: value
                for key, value in {
                    "type": prop.get("type") or "string",
                    "description": prop.get("description") or "",
                    "required": bool(prop.get("required")),
                }.items()
                if value not in ("", [], {})
            }
        return normalized

    def _normalize_generated_interface_contracts(self, interfaces: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for iface in interfaces if isinstance(interfaces, list) else []:
            if not isinstance(iface, dict):
                continue
            item = dict(iface)
            returns = item.get("returns")
            if isinstance(returns, dict):
                returns = dict(returns)
                returns["properties"] = self._normalize_return_properties(returns.get("properties", {}))
                items = returns.get("items")
                if isinstance(items, list):
                    returns["items"] = {
                        "type": "object",
                        "properties": self._normalize_return_properties(items),
                    }
                item["returns"] = returns
            normalized.append(item)
        return normalized

    async def _generate_interface_group(self, group: Dict[str, Any],
                                         trace_context: Dict[str, Any] = None,
                                         trace_prompt_context: str = "") -> Dict[str, Any]:
        ifaces_skeleton = group["interfaces"]
        iface_names = [i["name"] for i in ifaces_skeleton]
        group_label = group["group_id"]

        skeleton_json = json.dumps(ifaces_skeleton, indent=2, ensure_ascii=False)
        data_models = group.get("data_models", {})

        page_context = ""
        if group.get("is_capability"):
            page_context = (
                "\nSemantic Capability Group: {}\n"
                "Semantic Contract: {}\n"
                "Used By Pages: {}\n"
                "Related Entities: {}\n"
            ).format(
                group.get("group_id", ""),
                group.get("semantic_contract", ""),
                json.dumps(group.get("used_by", []), ensure_ascii=False),
                json.dumps(group.get("related_entities", []), ensure_ascii=False),
            )
        elif not group.get("is_shared"):
            page_fn = group.get("page_filename", "")
            funcs = group.get("primary_functions", [])
            page_context = f"\nPage: {page_fn}\nPrimary Functions: {json.dumps(funcs, ensure_ascii=False)}\n"

        prompt = f"""You are a JavaScript/TypeScript interface designer. Generate FULL interface definitions for the interfaces listed below.

Website Type: {group["website_type"]}
Data Models: {json.dumps(data_models, indent=2, ensure_ascii=False)}
{page_context}
Interface Skeleton (name + description + param_names — you MUST fill in full types):
{skeleton_json}

RULES:
1. For each interface, generate the COMPLETE definition including:
   - parameters: full list with name, type, required, and entityReference for FK params
   - returns: full type definition with properties (for arrays, include "items")
   - relatedTasks: list of task IDs this interface serves
2. Entity Reference Parameters: for params ending with "Id" that match an entity name, add:
   "entityReference": {{"entity": "EntityName", "displayField": "name", "valueField": "id"}}
3. Array return types MUST include "items" specification
4. Return display-ready fields (names, prices, images, ratings, not just IDs)
5. Do NOT change interface names from the skeleton
6. Do NOT create interfaces beyond what's listed in the skeleton
7. External/System UI Exclusion: if skeleton descriptions mention OS notifications, verification SMS previews, incoming call bars, system permission sheets, keyboard/IME chrome, app switcher, or status-bar-only UI, keep the interface focused on the target-app action/data only and omit system-overlay fields.
8. Search Entry Dynamic State: if a skeleton targets a search-entry or suggestion-entry page within the same stable search scope, model input text, search history, recommendation chips, recommended queries, and autocomplete/typeahead rows as state of the same page instance. If a visible scope anchor changes query interpretation or result/action applicability, expose it as a scope/context parameter instead of folding it into query text. Use generic query-driven parameters like scope and query. Do NOT specialize return types or interface definitions for one observed query string.
9. Keep definitions scoped to this semantic capability group. Reuse method names across pages through page assignments; do not duplicate one method per page when the capability is the same.

	Return JSON. Use arrays for return properties so the response is compatible with strict structured output:
	{{
	  "interfaces": [
	    {{
	      "name": "interfaceName",
	      "description": "Full description",
	      "parameters": [
	        {{"name": "paramName", "type": "string", "required": true, "entityReference": {{"entity": "", "displayField": "", "valueField": ""}}}}
	      ],
	      "returns": {{
	        "type": "object",
	        "properties": [
	          {{"name": "field", "type": "string", "description": "display-ready field", "required": true}}
	        ],
	        "items": []
	      }},
	      "relatedTasks": ["task_id"]
	    }}
  ]
}}"""

        response, _ = await call_openai_api_json_async(
            [{"role": "user", "content": prompt}],
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            stage="generate_interface_group",
        )
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception as exc:
                raise ValueError(
                    f"generate_interface_group {group_label}: invalid_json_response {exc}"
                ) from exc
        if not isinstance(response, dict):
            raise ValueError(f"generate_interface_group {group_label}: null_or_non_object_response")
        response_interfaces = self._normalize_generated_interface_contracts(response.get("interfaces", []))
        if not isinstance(response_interfaces, list) or not response_interfaces:
            raise ValueError(f"generate_interface_group {group_label}: missing_interfaces_array")

        return {
            "group_id": group_label,
            "interfaces": response_interfaces,
            "is_shared": group.get("is_shared", False),
            "is_capability": group.get("is_capability", False),
            "semantic_contract": group.get("semantic_contract", ""),
            "used_by": group.get("used_by", []),
            "related_entities": group.get("related_entities", []),
            "page_filename": group.get("page_filename", ""),
            "generation_status": "llm_generated",
        }

    def _assemble_interface_results(self, dispatch_plan: Dict[str, Any],
                                    group_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_interfaces = []
        helper_functions = dispatch_plan.get("helper_functions", [])
        page_assignments = {}

        if dispatch_plan.get("capability_groups"):
            interfaces_by_name = {}
            semantic_shards = []
            fallback_groups = []
            for result in group_results:
                if not result.get("is_capability"):
                    continue
                method_names = []
                for iface in result.get("interfaces", []) or []:
                    name = iface.get("name", "")
                    if not name:
                        continue
                    method_names.append(name)
                    if name not in interfaces_by_name:
                        interfaces_by_name[name] = iface
                used_by = result.get("used_by", []) or []
                for page_fn in used_by:
                    assigned = page_assignments.setdefault(page_fn, [])
                    for method_name in method_names:
                        if method_name not in assigned:
                            assigned.append(method_name)
                semantic_shards.append({
                    "semantic_shard_id": result.get("group_id", ""),
                    "semantic_contract": result.get("semantic_contract", ""),
                    "methods": method_names,
                    "used_by": used_by,
                    "related_entities": result.get("related_entities", []),
                })
            return {
                "interfaces": list(interfaces_by_name.values()),
                "helperFunctions": helper_functions,
                "page_assignments": page_assignments,
                "interface_capability_registry": {
                    "version": "interface_capability_registry",
                    "semantic_shards": semantic_shards,
                    "capabilities": {
                        name: {
                            "semantic_shard_id": shard.get("semantic_shard_id", ""),
                            "used_by": shard.get("used_by", []),
                        }
                        for shard in semantic_shards
                        for name in shard.get("methods", [])
                    },
                },
                "interface_generation_report": {
                    "fallback_group_count": len(fallback_groups),
                    "fallback_groups": fallback_groups,
                },
            }


        shared_result = None
        for r in group_results:
            if r.get("is_shared"):
                shared_result = r
                break


        page_groups = dispatch_plan.get("page_groups", {})
        for page_fn, pg in page_groups.items():
            assigned = []


            page_result = None
            for r in group_results:
                if r.get("page_filename") == page_fn:
                    page_result = r
                    break


            if page_result:
                for iface in page_result["interfaces"]:
                    all_interfaces.append(iface)
                    assigned.append(iface["name"])


            if shared_result:
                for iface in shared_result["interfaces"]:

                    if iface["name"] not in assigned:

                        shared_defs = dispatch_plan.get("shared_interfaces", [])
                        for sd in shared_defs:
                            if sd["name"] == iface["name"] and page_fn in sd.get("used_by", []):
                                all_interfaces.append(iface)
                                assigned.append(iface["name"])
                                break

            if assigned:
                page_assignments[page_fn] = assigned

        return {
            "interfaces": all_interfaces,
            "helperFunctions": helper_functions,
            "page_assignments": page_assignments,
        }

    @staticmethod
    def _interface_assignment_tokens(value: Any) -> set:
        text = str(value or "").lower()
        tokens = set(re.findall(r"[a-z0-9]+", text))
        for raw in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(value or "")):
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", raw)
            for part in parts:
                if part:
                    tokens.add(part.lower())
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                tokens.add(char)
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
        for index in range(0, max(0, len(cjk_chars) - 1)):
            tokens.add("".join(cjk_chars[index:index + 2]))
        return tokens

    def _score_page_against_semantic_shard(self, page_text: str, shard: Dict[str, Any]) -> int:
        page_tokens = self._interface_assignment_tokens(page_text)
        if not page_tokens:
            return 0
        shard_text = " ".join([
            str(shard.get("semantic_contract", "") or ""),
            " ".join(str(item or "") for item in shard.get("related_entities", []) if isinstance(shard.get("related_entities", []), list)),
            " ".join(str(item or "") for item in shard.get("used_by", []) if isinstance(shard.get("used_by", []), list)),
            " ".join(str(item or "") for item in shard.get("methods", []) if isinstance(shard.get("methods", []), list)),
        ])
        shard_tokens = self._interface_assignment_tokens(shard_text)
        if not shard_tokens:
            return 0
        overlap = page_tokens & shard_tokens
        score = len(overlap)
        filename = str(page_text or "").lower()
        detail_tokens = {"detail", "详情"}
        listing_tokens = {"listing", "list", "feed", "home", "collection", "列表", "首页"}
        if page_tokens & detail_tokens:
            if shard_tokens & detail_tokens:
                score += 8
            if (shard_tokens & listing_tokens) and not (shard_tokens & detail_tokens):
                score -= 4
            item_tokens = {"product", "item", "商品", "单品"}
            if page_tokens & item_tokens and shard_tokens & item_tokens and shard_tokens & detail_tokens:
                score += 6
            method_text = " ".join(str(item or "") for item in shard.get("methods", []) if isinstance(shard.get("methods", []), list))
            method_tokens = self._interface_assignment_tokens(method_text)
            if method_tokens & detail_tokens and method_tokens & item_tokens:
                score += 6
        if page_tokens & listing_tokens and shard_tokens & listing_tokens:
            score += 3
        for used_by in shard.get("used_by", []) if isinstance(shard.get("used_by", []), list) else []:
            used_tokens = self._interface_assignment_tokens(used_by)
            if used_tokens and len(page_tokens & used_tokens) >= max(2, min(4, len(used_tokens))):
                score += 3
            used_stem = os.path.splitext(os.path.basename(str(used_by or "")))[0].lower()
            if used_stem and used_stem in filename:
                score += 5
        return score

    def _reconcile_interface_page_assignments(self, site_model: Any, assembled: Dict[str, Any]) -> Dict[str, Any]:
        if not site_model or not isinstance(assembled, dict):
            return {}
        registry = assembled.get("interface_capability_registry", {})
        shards = registry.get("semantic_shards", []) if isinstance(registry, dict) else []
        if not isinstance(shards, list) or not shards:
            return {}

        page_assignments = assembled.setdefault("page_assignments", {})
        if not isinstance(page_assignments, dict):
            page_assignments = {}
            assembled["page_assignments"] = page_assignments

        page_prototypes = getattr(getattr(site_model, "structure", None), "page_prototypes", []) or []
        filenames = {
            str(getattr(page, "filename", "") or "")
            for page in page_prototypes
            if str(getattr(page, "filename", "") or "")
        }
        missing_before = []
        stale_assignment_keys = [
            key for key in sorted(page_assignments)
            if key not in filenames
        ]
        reconciled_pages = []

        for page in page_prototypes:
            filename = str(getattr(page, "filename", "") or "")
            if not filename:
                continue
            existing = page_assignments.get(filename, [])
            if isinstance(existing, list) and existing:
                continue
            missing_before.append(filename)
            page_text = " ".join([
                filename,
                str(getattr(page, "page_name", "") or ""),
                str(getattr(page, "page_name_full", "") or ""),
                str(getattr(page, "page_type", "") or ""),
                str(getattr(page, "description", "") or ""),
                " ".join(str(item or "") for item in (getattr(page, "primary_functions", []) or [])),
            ])
            scored = []
            for shard in shards:
                if not isinstance(shard, dict):
                    continue
                methods = [str(item) for item in shard.get("methods", []) if str(item)] if isinstance(shard.get("methods", []), list) else []
                if not methods:
                    continue
                score = self._score_page_against_semantic_shard(page_text, shard)
                if score > 0:
                    scored.append((score, shard, methods))
            if not scored:
                continue
            scored.sort(key=lambda item: (-item[0], str(item[1].get("semantic_shard_id", ""))))
            score, shard, methods = scored[0]
            page_assignments[filename] = methods
            reconciled_pages.append({
                "filename": filename,
                "semantic_shard_id": shard.get("semantic_shard_id", ""),
                "score": score,
                "assigned_methods": methods,
                "reason": "matched page prototype text against semantic shard contract/usage evidence",
            })

        still_unassigned = [
            str(getattr(page, "filename", "") or "")
            for page in page_prototypes
            if str(getattr(page, "filename", "") or "")
            and not page_assignments.get(str(getattr(page, "filename", "") or ""))
        ]
        report = {
            "version": "interface_assignment_reconciliation",
            "missing_before": missing_before,
            "stale_assignment_keys": stale_assignment_keys,
            "reconciled_pages": reconciled_pages,
            "still_unassigned": still_unassigned,
        }
        registry["assignment_reconciliation"] = report

        report_dir = self._interface_design_report_dir()
        if report_dir:
            try:
                os.makedirs(report_dir, exist_ok=True)
                with open(os.path.join(report_dir, "interface_assignment_reconciliation.json"), "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                if self.logger:
                    self.logger.log_warning("Failed to write interface assignment reconciliation report: {}".format(exc))
        if self.logger and reconciled_pages:
            self.logger.log_info("Reconciled {} interface page assignments from semantic registry".format(len(reconciled_pages)))
        return report

    def _write_interface_semantic_budget_report(self,
                                                groups: List[Dict[str, Any]],
                                                trace_prompt_context: str = "") -> None:
        if not self.logger or not getattr(self.logger, "output_dir", None):
            return
        budget = int(os.environ.get("T2W_INTERFACE_SEMANTIC_CONTEXT_BUDGET", "48000"))
        semantic_groups = [group for group in groups if group.get("is_capability")]
        if not semantic_groups:
            semantic_groups = groups
        shards = []
        for group in semantic_groups:
            fields = self._interface_group_context_fields(group, trace_prompt_context)
            total_text = "".join(fields.values())
            prompt_fields = {}
            for key, value in fields.items():
                prompt_fields[key] = {
                    "chars": len(value),
                    "bytes": len(value.encode("utf-8")),
                    "estimated_tokens": self._estimate_prompt_tokens(value),
                }
            estimated_tokens = self._estimate_prompt_tokens(total_text)
            shards.append({
                "semantic_shard_id": group.get("group_id", ""),
                "semantic_contract": group.get("semantic_contract", ""),
                "method_count": len(group.get("interfaces", []) or []),
                "methods": [
                    iface.get("name", "")
                    for iface in group.get("interfaces", []) or []
                    if isinstance(iface, dict)
                ],
                "used_by": group.get("used_by", []) or ([group.get("page_filename")] if group.get("page_filename") else []),
                "related_entities": group.get("related_entities", []),
                "chars": len(total_text),
                "estimated_tokens": estimated_tokens,
                "budget": budget,
                "over_budget": estimated_tokens > budget,
                "prompt_fields": prompt_fields,
            })
        report = {
            "version": "interface_semantic_shard_budget_report",
            "semantic_shard_count": len(shards),
            "total_methods": sum(shard.get("method_count", 0) for shard in shards),
            "total_estimated_tokens": sum(shard.get("estimated_tokens", 0) for shard in shards),
            "overloaded_semantic_shards": [
                shard.get("semantic_shard_id")
                for shard in shards
                if shard.get("over_budget")
            ],
            "semantic_shards": shards,
        }
        try:
            report_dir = self._interface_design_report_dir()
            if not report_dir:
                return
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, "interface_semantic_shard_budget_report.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self.logger.log_info("Wrote interface semantic shard budget report: {}".format(path))
        except Exception as exc:
            self.logger.log_warning("Failed to write interface semantic shard budget report: {}".format(exc))

    def _interface_group_context_fields(self,
                                        group: Dict[str, Any],
                                        trace_prompt_context: str = "") -> Dict[str, str]:
        return {
            "semantic_contract": self._compact_json({
                "group_id": group.get("group_id", ""),
                "semantic_contract": group.get("semantic_contract", ""),
                "used_by": group.get("used_by", []),
                "related_entities": group.get("related_entities", []),
                "page_filename": group.get("page_filename", ""),
                "primary_functions": group.get("primary_functions", []),
            }) + "\n",
            "data_models_json": self._compact_json(group.get("data_models", {})) + "\n",
            "interface_skeleton_json": self._compact_json(group.get("interfaces", [])) + "\n",
            "trace_context": (trace_prompt_context or "") + "\n",
        }

    def _compact_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _compact_data_models_for_prompt(self, data_models: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data_models, dict):
            return {}

        def compact_schema(name: str, schema: Any) -> Dict[str, Any]:
            if not isinstance(schema, dict):
                return {"name": name}
            fields: List[Dict[str, Any]] = []
            source_fields = (
                schema.get("fields")
                or schema.get("properties")
                or schema.get("attributes")
                or {}
            )
            if isinstance(source_fields, dict):
                iterable = source_fields.items()
            elif isinstance(source_fields, list):
                iterable = [
                    (
                        str(item.get("name") or item.get("field") or item.get("key") or idx),
                        item,
                    )
                    for idx, item in enumerate(source_fields)
                    if isinstance(item, dict)
                ]
            else:
                iterable = []
            for field_name, field_spec in iterable:
                if len(fields) >= 16:
                    break
                if isinstance(field_spec, dict):
                    fields.append({
                        key: field_spec.get(key)
                        for key in ("name", "type", "required", "semantic_role", "description")
                        if field_spec.get(key) not in (None, "", [], {})
                    } | ({"name": str(field_name)} if not field_spec.get("name") else {}))
                else:
                    fields.append({"name": str(field_name), "type": str(field_spec)})
            return {
                key: value
                for key, value in {
                    "name": name,
                    "kind": schema.get("kind") or schema.get("type"),
                    "description": schema.get("description"),
                    "fields": fields,
                }.items()
                if value not in (None, "", [], {})
            }

        entities = data_models.get("entities", {})
        if isinstance(entities, dict):
            compact_entities = [
                compact_schema(str(name), schema)
                for name, schema in list(entities.items())[:80]
            ]
        elif isinstance(entities, list):
            compact_entities = [
                compact_schema(str(item.get("name") or item.get("entity") or idx), item)
                for idx, item in enumerate(entities[:80])
                if isinstance(item, dict)
            ]
        else:
            compact_entities = []

        relationships = data_models.get("relationships", [])
        compact_relationships: List[Dict[str, Any]] = []
        for rel in relationships if isinstance(relationships, list) else []:
            if not isinstance(rel, dict):
                continue
            compact_relationships.append({
                key: rel.get(key)
                for key in ("from", "to", "type", "source", "target", "relationship")
                if rel.get(key) not in (None, "", [], {})
            })
            if len(compact_relationships) >= 80:
                break

        return {
            "entities": compact_entities,
            "relationships": compact_relationships,
            "metadata": data_models.get("metadata", {}),
            "prompt_projection": "schema_shape_only",
        }

    def _estimate_prompt_tokens(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        non_cjk = re.sub(r"[\u4e00-\u9fff]", " ", text)
        pieces = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_]", non_cjk)
        return cjk + len(pieces)

    def validate_interfaces(self, interfaces: Dict[str, Any], tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        validation = {
            "valid": True,
            "coverage": {},
            "missing": [],
            "warnings": []
        }


        task_ids = [task.get('id', f"task_{i}") for i, task in enumerate(tasks)]
        covered_tasks = set()

        for interface in interfaces.get('interfaces', []):
            for task_id in interface.get('relatedTasks', []):
                covered_tasks.add(task_id)


        uncovered = set(task_ids) - covered_tasks
        if uncovered:
            validation['valid'] = False
            validation['missing'] = list(uncovered)
            validation['warnings'].append(f"Tasks not covered by interfaces: {', '.join(uncovered)}")


        validation['coverage'] = {
            "total_tasks": len(task_ids),
            "covered_tasks": len(covered_tasks),
            "coverage_percentage": (len(covered_tasks) / len(task_ids) * 100) if task_ids else 0
        }

        return validation
