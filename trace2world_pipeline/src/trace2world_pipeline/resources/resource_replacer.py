
import os
import json
import re
import shutil
import asyncio
import base64
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.llm_caller import (
    call_openai_api_json,
    get_async_client_for_endpoint,
    get_next_endpoint,
)


@dataclass
class ResourceReplacement:
    original_url: str
    resource_type: str
    search_query: str
    new_url: str


@dataclass
class DataResourceResult:
    updated_data: Dict[str, Any]
    replacements: List[ResourceReplacement]


@dataclass
class PageResourceResult:
    updated_pages: Dict[str, str]
    replacements: Dict[str, List[ResourceReplacement]]


def load_screenshot_library(results_dir: str, traces: List[str] = None, min_confidence: float = 0.5) -> List[Dict[str, Any]]:
    library = []
    if traces is None:
        traces = [d for d in os.listdir(results_dir)
                  if os.path.isdir(os.path.join(results_dir, d))]

    for trace in traces:
        results_file = os.path.join(results_dir, trace, "e2e_results.json")
        if not os.path.isfile(results_file):
            continue

        with open(results_file, "r", encoding="utf-8") as f:
            data = json.load(f)


        resource_map = {}
        for r in data.get("resources", []):
            resource_map[r["path"]] = r

        for j in data.get("judgments", []):
            if not j.get("usable"):
                continue
            if j.get("confidence", 0) < min_confidence:
                continue

            path = j["path"]
            if not os.path.isfile(path):
                continue


            res_meta = resource_map.get(path, {}).get("metadata", {})
            library.append({
                "path": path,
                "category": j.get("category", ""),
                "nearby_text": res_meta.get("nearby_text", ""),
                "width": res_meta.get("width", 0),
                "height": res_meta.get("height", 0),
                "confidence": j.get("confidence", 0),
                "suggested_usage": j.get("suggested_usage", ""),
                "source_screenshot": resource_map.get(path, {}).get("source", ""),
                "trace": trace,
            })

    return library


class Trace2WorldResourceReplacer:


    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico'}
    AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac', '.wma'}
    VIDEO_EXTENSIONS = {'.mp4', '.webm', '.avi', '.mov', '.wmv', '.flv', '.mkv'}
    FILE_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv'}

    def __init__(self, logger: Trace2WorldLogger = None,
                 image_mode: str = None,
                 output_dir: str = None,
                 model: str = None,
                 reasoning_effort: str = "medium"):
        self.logger = logger or Trace2WorldLogger()
        self.image_mode = image_mode or "Real"
        self.output_dir = output_dir or "results/generated"
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.image_generation_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")


        if self.image_mode == "Generate" and not os.getenv("OPENAI_API_KEY"):
            error_msg = "❌ OPENAI_API_KEY is required for 'Generate' image mode."
            self.logger.log_error(error_msg)
            raise ValueError(error_msg)


        self._screenshot_library: List[Dict[str, Any]] = []
        self._screenshot_used: set = set()

    def set_screenshot_library(self, resources: List[Dict[str, Any]]):
        self._screenshot_library = [r for r in resources if os.path.isfile(r.get("path", ""))]
        self._screenshot_used = set()
        self.logger.log_info(
            f"📸 Screenshot library loaded: {len(self._screenshot_library)} resources "
            f"({sum(1 for r in self._screenshot_library if r.get('category') == 'icon')} icons, "
            f"{sum(1 for r in self._screenshot_library if r.get('category') == 'photo')} photos, "
            f"{sum(1 for r in self._screenshot_library if r.get('category') == 'logo')} logos)"
        )

    def _match_screenshot_resource(self, query: str, width: int = None, height: int = None) -> Optional[str]:
        if not self._screenshot_library:
            return None

        query_lower = query.lower()
        query_tokens = set(re.findall(r'\w+', query_lower))


        desired_category = None
        if any(w in query_lower for w in ["icon", "symbol", "pictogram"]):
            desired_category = "icon"
        elif any(w in query_lower for w in ["photo", "food", "product", "dish", "meal", "image", "picture", "thumbnail"]):
            desired_category = "photo"
        elif any(w in query_lower for w in ["logo", "brand", "emblem"]):
            desired_category = "logo"
        elif any(w in query_lower for w in ["background", "banner", "hero", "cover"]):
            desired_category = "background"

        candidates = []
        for res in self._screenshot_library:
            path = res["path"]
            if path in self._screenshot_used:
                continue

            cat = res.get("category", "")
            nearby = res.get("nearby_text", "").lower()
            usage = res.get("suggested_usage", "").lower()
            nearby_tokens = set(re.findall(r'\w+', nearby + " " + usage))


            score = 0.0
            if desired_category and cat == desired_category:
                score += 5.0
            elif desired_category is None:
                score += 1.0


            overlap = query_tokens & nearby_tokens
            score += len(overlap) * 2.0


            w = res.get("width", 0)
            h = res.get("height", 0)
            if desired_category == "photo" and w >= 200 and h >= 200:
                score += 2.0
            elif desired_category == "icon" and w < 200 and h < 200:
                score += 1.0

            if score > 0:
                candidates.append((score, res))

        if not candidates:
            return None


        candidates.sort(key=lambda x: -x[0])
        best_score, best_res = candidates[0]


        if best_score < 3.0:
            return None


        src_path = best_res["path"]
        self._screenshot_used.add(src_path)


        dest_dir = os.path.join(self.output_dir, "screenshot_resources")
        os.makedirs(dest_dir, exist_ok=True)

        filename = os.path.basename(src_path)
        dest_path = os.path.join(dest_dir, filename)
        shutil.copy2(src_path, dest_path)

        relative_path = f"screenshot_resources/{filename}"
        self.logger.log_info(
            f"  📸 Screenshot match: '{query[:40]}' → {filename} "
            f"(cat={best_res.get('category')}, score={best_score:.1f})"
        )
        return relative_path

    def _detect_resource_type(self, url: str) -> str:
        url_lower = url.lower()


        for ext in self.IMAGE_EXTENSIONS:
            if ext in url_lower:
                return "image"
        for ext in self.AUDIO_EXTENSIONS:
            if ext in url_lower:
                return "audio"
        for ext in self.VIDEO_EXTENSIONS:
            if ext in url_lower:
                return "video"
        for ext in self.FILE_EXTENSIONS:
            if ext in url_lower:
                return "file"


        if any(pattern in url_lower for pattern in ['image', 'img', 'photo', 'picture', 'avatar', 'logo', 'icon']):
            return "image"
        if any(pattern in url_lower for pattern in ['audio', 'sound', 'music', 'song']):
            return "audio"
        if any(pattern in url_lower for pattern in ['video', 'movie', 'clip']):
            return "video"
        if any(pattern in url_lower for pattern in ['document', 'file', 'pdf']):
            return "file"

        return "unknown"

    async def _generate_image_openai(self, query: str, width: int = 800, height: int = 600) -> Optional[str]:
        try:
            if width > height * 1.2:
                size = "1536x1024"
            elif height > width * 1.2:
                size = "1024x1536"
            else:
                size = "1024x1024"

            self.logger.log_info(f"  🎨 Generating image with OpenAI Images API for '{query}' (size: {size})")
            client = get_async_client_for_endpoint(get_next_endpoint())
            response = await client.images.generate(
                model=self.image_generation_model,
                prompt=query,
                size=size,
                quality="medium",
                n=1,
                output_format="png",
                timeout=180,
            )
            images = getattr(response, "data", None) or []
            b64_data = getattr(images[0], "b64_json", None) if images else None
            if not b64_data:
                self.logger.log_warning(f"  ⚠️ No image data in OpenAI response for '{query}'")
                return None
            img_bytes = base64.b64decode(b64_data)

            images_dir = os.path.join(self.output_dir, "generated_images")
            os.makedirs(images_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_query = re.sub(r'[^a-zA-Z0-9_-]', '_', query[:50])
            filename = f"{safe_query}_{timestamp}.png"
            filepath = os.path.join(images_dir, filename)
            with open(filepath, "wb") as f:
                f.write(img_bytes)

            relative_path = f"generated_images/{filename}"
            self.logger.log_info(f"  ✅ Generated image saved to {relative_path}")
            return relative_path

        except Exception as e:
            self.logger.log_error(f"  ❌ OpenAI image generation failed for '{query}': {str(e)}")
            return None

    async def _search_resource(self, resource_type: str, query: str, width: int = None, height: int = None) -> Optional[str]:

        if resource_type == "image" and self._screenshot_library:
            match = self._match_screenshot_resource(query, width, height)
            if match:
                return match

        if resource_type == "image":
            if self.image_mode == "Generate":
                return await self._generate_image_openai(query, width or 800, height or 600)
            self.logger.log_info(f"  ⚠️ No screenshot-library match for image '{query}'; leaving placeholder")
            return None
        self.logger.log_warning(f"  ⚠️ Unsupported resource type '{resource_type}' for '{query}'; leaving placeholder")
        return None

    def _identify_data_resources(self, data: Dict[str, Any], website_type: str) -> List[Dict[str, Any]]:

        prompt = f"""You are analyzing data for a {website_type} website. Identify ALL media resources (images, audio, video, documents) and generate search queries.

RESOURCE TYPES TO IDENTIFY:
1. IMAGES: jpg, png, gif, webp, svg, or fields like image, img, photo, picture, avatar, logo, icon, banner, thumbnail, cover
2. AUDIO: mp3, wav, ogg, m4a, or fields like audio, sound, music, song, track, podcast
3. VIDEO (direct files): mp4, webm, avi, mov, or fields like video, movie, clip - use type "video"
4. IFRAME_VIDEO (streaming): YouTube URLs, Vimeo, or fields like stream, tutorial, demo_video - use type "iframe_video"
5. FILES: pdf, doc, xls, ppt, or fields like document, file, report, whitepaper, ebook, guide

RULES:
1. Check ALL fields that might contain media URLs
2. Identify placeholder services: picsum.photos, placeholder.com, unsplash.com, dummyimage.com, example.com
3. Detect resource type from URL extension or field name
4. Generate specific, contextual search queries
5. IMPORTANT: Distinguish between "video" (needs direct MP4 link) and "iframe_video" (needs YouTube/streaming embed)

For each resource:
1. Determine the resource type (image/audio/video/iframe_video/file)
2. Analyze context to create relevant search query
3. For images: include dimensions if available
4. For audio: focus on background music, effects, or ambient sounds
5. For video: focus on stock footage, backgrounds (will get direct MP4 links)
6. For iframe_video: focus on tutorials, demonstrations, explanations (will get YouTube embeds)
7. For files: always search for PDFs regardless of original extension

IMAGE SIZE CATEGORY (for images only):
For each image, determine its size_category:
- "large": ONLY full-width background images, hero banners that span the entire viewport width, or page-level decorative backgrounds
- "small": ALL other images - product images, thumbnails, avatars, icons, logos, cover images, card images, list images, profile pictures, etc.

Return JSON format:
{{
    "resources": [
        {{"url": "https://example.com/hero-bg.jpg", "type": "image", "description": "hero background cityscape", "width": 1920, "height": 800, "size_category": "large"}},
        {{"url": "https://example.com/product.jpg", "type": "image", "description": "product photo laptop", "width": 800, "height": 600, "size_category": "small"}},
        {{"url": "https://example.com/bg-music.mp3", "type": "audio", "description": "background music upbeat corporate"}},
        {{"url": "https://example.com/background.mp4", "type": "video", "description": "nature landscape background"}},
        {{"url": "https://youtube.com/watch?v=xxx", "type": "iframe_video", "description": "tutorial how to use product"}},
        {{"url": "https://example.com/guide.pdf", "type": "file", "description": "user guide manual documentation"}}
    ]
}}

Data to analyze:
{json.dumps(data, indent=2)}"""

        messages = [{"role": "user", "content": prompt}]

        call_id = self.logger.log_api_call(
            api_name="Identify Data Resources",
            prompt=prompt
        )

        result, usage_info = call_openai_api_json(messages, model=self.model, reasoning_effort=self.reasoning_effort, stage="identify_data_resources")

        if isinstance(result, str):
            result = json.loads(result)

        self.logger.log_api_response(
            api_name="Identify Data Resources",
            success=True,
            response=result,
            usage_info=usage_info,
            call_id=call_id
        )

        return result.get("resources", [])

    def _identify_page_resources(self, html: str, page_name: str) -> List[Dict[str, Any]]:

        prompt = f"""Analyze this HTML page ({page_name}) and identify ONLY STATIC resources that need replacement.

RESOURCE TYPES TO IDENTIFY:
1. IMAGES: static image link, like "https://placeholder.com/1200x400"
2. AUDIO: static audio link, like "audio/background.mp3"
3. VIDEO: static video link in <video> tags, <source> tags with video types (mp4, webm) - use type "video"
4. IFRAME_VIDEO: static video link in <iframe> tags with YouTube/Vimeo URLs - use type "iframe_video"
5. FILES: static file links (only with type of pdf, doc, xls) - use type "file"

**CRITICAL**:
1. Identify ONLY STATIC resources links that can be replaced with real content.
2. File Type should Exclude business_logic.js , css files, font links(e.g., "https://fonts.gstatic.com"), html files, svg files .

EXCLUDE these resources:
- Resources with dynamic data binding (${{...}}, template syntax)
- Resources inside loops or data templates
- Resources with empty src/href (will be filled by JS)
- Resources referencing variables or properties

For each static resource:
1. Identify the type (image/audio/video/iframe_video/file)
2. Extract the complete URL (src, href, etc.)
3. Generate a descriptive search query based on context
4. For images: include width/height if available
5. IMPORTANT: Distinguish between:
   - "video": <video> tags needing direct MP4 links (stock footage, backgrounds)
   - "iframe_video": <iframe> tags needing YouTube embeds (tutorials, demonstrations)

IMAGE SIZE CATEGORY (for images only):
For each image, determine its size_category:
- "large": ONLY full-width background images, hero banners that span the entire viewport width, or page-level decorative backgrounds
- "small": ALL other images - product images, thumbnails, avatars, icons, logos, cover images, card images, list images, profile pictures, etc.

Return JSON format:
{{
    "resources": [
        {{"src": "https://placeholder.com/1920x800", "type": "image", "description": "hero background cityscape", "width": 1920, "height": 800, "size_category": "large"}},
        {{"src": "https://placeholder.com/400x300", "type": "image", "description": "product card image laptop", "width": 400, "height": 300, "size_category": "small"}},
        {{"src": "audio/background.mp3", "type": "audio", "description": "ambient background music relaxing"}},
        {{"src": "videos/background.mp4", "type": "video", "description": "nature landscape background video"}},
        {{"src": "https://www.youtube.com/embed/dQw4w9WgXcQ", "type": "iframe_video", "description": "tutorial product demonstration"}},
        {{"src": "docs/guide.pdf", "type": "file", "description": "user guide manual pdf"}}
    ]
}}

HTML to analyze:
{html}"""

        messages = [{"role": "user", "content": prompt}]


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                api_name="Identify Page Resources",
                prompt=prompt,
                additional_args={"page_name": page_name}
            )

        try:
            result, usage_info = call_openai_api_json(messages, model=self.model, reasoning_effort=self.reasoning_effort, stage="identify_page_resources")

            if isinstance(result, str):
                result = json.loads(result)


            if self.logger:
                self.logger.log_api_response(
                    api_name="Identify Page Resources",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    call_id=call_id
                )

            return result.get("resources", [])

        except Exception as e:

            if self.logger:
                self.logger.log_api_response(
                    api_name="Identify Page Resources",
                    success=False,
                    error=str(e),
                    call_id=call_id
                )
            raise

    async def replace_data_resources(self, data: Dict[str, Any], website_type: str) -> DataResourceResult:
        self.logger.start_stage("Replace Data Resources", "backend")
        self.logger.log_info(f"🎯 Starting data resource replacement for {website_type}...")


        resources_to_replace = self._identify_data_resources(data, website_type)


        if not resources_to_replace:
            self.logger.log_info("No resources found in data to replace")
            return DataResourceResult(updated_data=data, replacements=[])


        type_counts = {}
        for res in resources_to_replace:
            res_type = res.get('type', 'unknown')
            type_counts[res_type] = type_counts.get(res_type, 0) + 1

        self.logger.log_info(f"Found {len(resources_to_replace)} resources to replace: {type_counts}")


        path_map = {}

        def build_paths(obj, parent_path=[]):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if isinstance(value, str) and value.startswith(('http://', 'https://')):
                        path_map[value] = (obj, key)
                    else:
                        build_paths(value, parent_path + [key])
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    build_paths(item, parent_path + [i])

        build_paths(data)


        replacements = []

        for res_info in resources_to_replace:
            url = res_info.get('url', '')
            res_type = res_info.get('type', 'unknown')
            description = res_info.get('description', 'placeholder')
            width = res_info.get('width') or 0
            height = res_info.get('height') or 0

            if url in path_map:
                obj, key = path_map[url]


                new_url = await self._search_resource(res_type, description, width, height)

                if new_url is not None:
                    obj[key] = new_url

                    replacement = ResourceReplacement(
                        original_url=url,
                        resource_type=res_type,
                        search_query=description,
                        new_url=new_url
                    )
                    replacements.append(replacement)
                    self.logger.log_info(f"  Replaced {res_type}: {url[:50]}... -> {new_url[:50]}...")
                else:

                    self.logger.log_warning(f"  Skipped {res_type}: {url[:50]}... (search failed, keeping original)")


        replaced_counts = {}
        for repl in replacements:
            replaced_counts[repl.resource_type] = replaced_counts.get(repl.resource_type, 0) + 1

        self.logger.log_info(f"✅ Replaced {len(replacements)} resources: {replaced_counts}")
        self.logger.end_stage("Replace Data Resources")

        return DataResourceResult(updated_data=data, replacements=replacements)

    async def replace_page_resources_async(self, pages: Dict[str, str], website_type: str) -> PageResourceResult:
        self.logger.start_stage("Replace Page Resources")
        self.logger.log_info(f"🎯 Starting page resource replacement for {len(pages)} pages...")


        updated_pages = {}
        all_replacements = {}


        tasks = []
        for filename, html in pages.items():
            tasks.append(self._replace_page_resources_single(filename, html))

        results = await asyncio.gather(*tasks)


        for filename, (updated_html, replacements) in zip(pages.keys(), results):
            updated_pages[filename] = updated_html
            if replacements:
                all_replacements[filename] = replacements


        type_counts = {}
        for page_repls in all_replacements.values():
            for repl in page_repls:
                type_counts[repl.resource_type] = type_counts.get(repl.resource_type, 0) + 1

        total_replaced = sum(len(r) for r in all_replacements.values())
        self.logger.log_info(f"✅ Replaced {total_replaced} resources across {len(all_replacements)} pages: {type_counts}")
        self.logger.end_stage("Replace Page Resources")

        return PageResourceResult(updated_pages=updated_pages, replacements=all_replacements)

    async def _replace_page_resources_single(self, filename: str, html: str) -> Tuple[str, List[ResourceReplacement]]:
        self.logger.log_info(f"  Processing {filename}...")


        resources_to_replace = self._identify_page_resources(html, filename)


        if not resources_to_replace:
            self.logger.log_info(f"    No static resources to replace in {filename}")
            return html, []

        replacements = []
        updated_html = html

        for res_info in resources_to_replace:
            src = res_info.get('src', '')
            res_type = res_info.get('type', 'unknown')
            description = res_info.get('description', 'placeholder')
            width = res_info.get('width') or 0
            height = res_info.get('height') or 0

            if src:

                new_url = await self._search_resource(res_type, description, width, height)


                if new_url and src in updated_html:
                    updated_html = updated_html.replace(src, new_url)
                    replacement = ResourceReplacement(
                        original_url=src,
                        resource_type=res_type,
                        search_query=description,
                        new_url=new_url
                    )
                    replacements.append(replacement)
                    self.logger.log_info(f"    Replaced {res_type}: {src[:40]}...")
                elif not new_url:
                    self.logger.log_info(f"    Keeping original {res_type}: {src[:40]}... (no replacement found)")

        self.logger.log_info(f"    ✅ Replaced {len(replacements)} resources in {filename}")
        return updated_html, replacements
