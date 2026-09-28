
import json
import re
from typing import List, Dict, Any, Optional, Tuple
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async


class Trace2WorldTaskRewriter:

    def __init__(self, logger: Trace2WorldLogger = None, model: str = None, reasoning_effort: str = None):
        self.logger = logger or Trace2WorldLogger()
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def rewrite_tasks(self,
                           original_tasks: List[Dict[str, Any]],
                           website_data: Dict[str, Any],
                           datadict: Dict[str, Any],
                           include_full_data: bool = True) -> List[Dict[str, Any]]:
        self.logger.log_info(f"📝 Starting task rewriting to match actual data... (full_data={include_full_data})")


        data_analysis = self._analyze_data(website_data)


        context = {
            "data_analysis": data_analysis,
            "available_entities": list(website_data.keys()),
            "sample_data": self._get_sample_data(website_data, include_full_data=include_full_data),
            "include_full_data": include_full_data
        }


        rewritten_tasks = await self._call_llm_for_rewriting(
            original_tasks,
            context,
            website_data
        )


        baseline_date = website_data.get("_metadata", {}).get("baselineDate")
        if baseline_date:
            rewritten_tasks = self._add_time_config_to_tasks(rewritten_tasks, baseline_date)
            self.logger.log_info(f"  ⏰ Added set_system_time config with baselineDate: {baseline_date}")

        self.logger.log_info(f"✅ Successfully rewrote {len(rewritten_tasks)} tasks")

        return rewritten_tasks

    def _add_time_config_to_tasks(self, tasks: List[Dict[str, Any]], baseline_date: str) -> List[Dict[str, Any]]:
        for task in tasks:
            if "config" not in task:
                task["config"] = []


            task["config"].insert(0, {
                "type": "set_system_time",
                "parameters": {
                    "date": baseline_date,
                    "time": "09:00:00"
                }
            })

        return tasks

    def _analyze_data(self, website_data: Dict[str, Any]) -> Dict[str, Any]:
        analysis = {}


        if "products" in website_data and isinstance(website_data["products"], list):
            products = website_data["products"]
            if products:

                prices = [float(p.get("price", 0)) for p in products if p.get("price")]
                if prices:
                    analysis["price_range"] = {
                        "min": min(prices),
                        "max": max(prices),
                        "median": sorted(prices)[len(prices)//2]
                    }


                ratings = [float(p.get("rating", 0)) for p in products if p.get("rating")]
                if ratings:
                    analysis["rating_range"] = {
                        "min": min(ratings),
                        "max": max(ratings),
                        "high_rated_count": sum(1 for r in ratings if r >= 4.0)
                    }


                features = {}
                for p in products:

                    if p.get("free_shipping"):
                        features["free_shipping_count"] = features.get("free_shipping_count", 0) + 1
                    if "wireless" in str(p).lower():
                        features["wireless_count"] = features.get("wireless_count", 0) + 1
                    if p.get("in_stock"):
                        features["in_stock_count"] = features.get("in_stock_count", 0) + 1

                if features:
                    analysis["features"] = features


                categories = {}
                for p in products:
                    cat = p.get("category", p.get("type", "unknown"))
                    categories[cat] = categories.get(cat, 0) + 1

                analysis["categories"] = categories
                analysis["total_products"] = len(products)


        for entity_type, items in website_data.items():
            if entity_type != "products" and isinstance(items, list) and items:
                analysis[f"{entity_type}_count"] = len(items)

        return analysis

    def _get_sample_data(self, website_data: Dict[str, Any], limit: int = 3, include_full_data: bool = False) -> Dict[str, Any]:
        samples = {}

        for entity_type, items in website_data.items():
            if isinstance(items, list) and items:
                if include_full_data:
                    samples[entity_type] = items
                else:
                    samples[entity_type] = items[:limit]
            else:
                samples[entity_type] = items

        return samples

    async def _call_llm_for_rewriting(self,
                                      original_tasks: List[Dict[str, Any]],
                                      context: Dict[str, Any],
                                      website_data: Dict[str, Any]) -> List[Dict[str, Any]]:

        data_label = "COMPLETE DATA (all items):" if context.get("include_full_data", False) else "SAMPLE DATA (first 3 items of each entity):"

        prompt = f"""You are rewriting tasks to create clean instructions for RL agent training.

The goal is to separate:
- **instruction**: What the user wants to do (generic, no answers revealed)
- **ground_truth**: Specific data for evaluation (IDs, names, exact values)

ORIGINAL TASKS:
{json.dumps(original_tasks, indent=2)}

DATA ANALYSIS:
{json.dumps(context["data_analysis"], indent=2)}

{data_label}
{json.dumps(context["sample_data"], indent=2)}

## CRITICAL RULES FOR INSTRUCTION FIELD

The instruction field is what an RL agent will see. It must:

1. **NEVER include internal IDs** - Users cannot see IDs like prod_001, art_002, mr_003, b1, p5
2. **NEVER include specific item names that reveal the answer** - Users should discover these
3. **ONLY include filtering criteria**: category, price range, rating threshold, quantity, date ranges
4. **Be concise** - Single sentence preferred, no redundant explanations
5. **CRITICAL: ADJUST QUANTITY based on actual data availability** - Count items in the provided data that match the task criteria. If the original task asks for N items but only M items exist, YOU MUST change the instruction quantity from N to M. Example: if task says "find 5 AI podcasts" but data only has 1 AI podcast, change instruction to "find an AI podcast" (singular). The instruction MUST be achievable with the available data - never ask for more items than exist!

## EXAMPLE TRANSFORMATIONS

| BAD instruction (reveals answer) | GOOD instruction (only criteria) |
|----------------------------------|----------------------------------|
| Add Silicone Spatula Set (p1) at $12.99, Steel Cups (p2) at $19.49 to cart | Add any 3 Kitchen items under $25 with 4+ stars to cart |
| Book Cedar Meeting Hub (mr_003) for Tomorrow Afternoon at $39 | Book any meeting room available tomorrow afternoon with free cancellation |
| Download art_001, art_002, art_003 (Climate Change articles) | Download 3 climate change articles published after 2023 |
| Compare PaceFlow 2 (comfort 4.8) vs AeroRun X (comfort 4.6) and add PaceFlow 2 | Compare two running shoes over $50 and add the one with higher comfort rating to cart |
| Favorite the F-15EX review (rev_002) with rating 4.8 | Favorite the fighter jet review with the highest user rating |
| Add 'Circuit Dreams' (b4) Hardcover Sci-Fi to wishlist | Add any hardcover Science Fiction book from 2019 to your wishlist |

## OUTPUT FORMAT

Return JSON with this structure:
{{
  "rewritten_tasks": [
    {{
      "id": "task_1",
      "instruction": "Concise task description with ONLY filtering criteria (no IDs, no specific names)",
      "ground_truth": {{
        "target_ids": ["exact_id_1", "exact_id_2"],
        "target_names": ["Exact Name 1", "Exact Name 2"],
        "expected_values": {{
          "prices": [12.99, 19.49],
          "ratings": [4.5, 4.6]
        }},
        "criteria": {{
          "category": "Kitchen",
          "max_price": 25,
          "min_rating": 4.0,
          "quantity": 3
        }}
      }}
    }}
  ]
}}

## VALIDATION CHECKLIST FOR EACH INSTRUCTION

Before finalizing each instruction, verify:
- [ ] No parenthetical IDs like (p1), (prod_003), (art_001)
- [ ] No exact product/item names that give away the answer
- [ ] No exact prices like $12.99, only thresholds like "under $25"
- [ ] No exact dates like "2025-11-28", only relative terms like "this Friday" or ranges like "after 2023"
- [ ] The instruction reads naturally as if a human user wrote it
- [ ] An agent must actually search/filter to find the right items

IMPORTANT: The instruction should be something a real user would type, NOT an answer key.
"""


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                "Rewrite Tasks",
                prompt
            )

        try:
            result, usage_info = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="rewrite_tasks",
            )


            if self.logger:
                self.logger.log_api_response(
                    "Rewrite Tasks",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    call_id=call_id
                )


            if isinstance(result, str):
                result = json.loads(result)

            rewritten_tasks = result.get("rewritten_tasks", [])


            for task in rewritten_tasks:

                if "id" not in task:
                    task["id"] = task.get("original_id", "task_unknown")


                if "instruction" not in task:

                    name = task.get("name", "")
                    desc = task.get("description", "")
                    task["instruction"] = name if name else desc


                if "ground_truth" not in task:

                    task["ground_truth"] = task.get("data_mapping", {})


                if "name" not in task:
                    task["name"] = task.get("instruction", "")
                if "description" not in task:
                    task["description"] = task.get("instruction", "")

            return rewritten_tasks

        except Exception as e:

            if self.logger:
                self.logger.log_api_response(
                    "Rewrite Tasks",
                    success=False,
                    error=str(e),
                    call_id=call_id
                )
                self.logger.log_error(f"Failed to rewrite tasks: {str(e)}")


            return original_tasks

    def validate_rewritten_tasks(self,
                                 rewritten_tasks: List[Dict[str, Any]],
                                 website_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        issues = []

        for task in rewritten_tasks:
            task_id = task.get("id", "unknown")
            data_mapping = task.get("data_mapping", {})


            if "target_product_id" in data_mapping:
                product_id = data_mapping["target_product_id"]
                if "products" in website_data:
                    product_found = any(
                        p.get("id") == product_id
                        for p in website_data["products"]
                        if isinstance(p, dict)
                    )
                    if not product_found:
                        issues.append(f"Task {task_id}: Product ID '{product_id}' not found in data")


            if "expected_price" in data_mapping and "target_product_id" in data_mapping:
                product_id = data_mapping["target_product_id"]
                expected_price = data_mapping["expected_price"]

                if "products" in website_data:
                    for p in website_data["products"]:
                        if isinstance(p, dict) and p.get("id") == product_id:
                            actual_price = p.get("price")
                            if actual_price and float(actual_price) != float(expected_price):
                                issues.append(
                                    f"Task {task_id}: Price mismatch for {product_id} "
                                    f"(expected: {expected_price}, actual: {actual_price})"
                                )
                            break

        is_valid = len(issues) == 0
        return is_valid, issues
