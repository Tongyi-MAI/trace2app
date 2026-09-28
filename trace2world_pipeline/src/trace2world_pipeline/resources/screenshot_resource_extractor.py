
import os
import io
import json
import asyncio
import zipfile
import hashlib
import re
import ssl
import tempfile
import time
import aiohttp
import certifi
from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass, field
import fcntl

from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.config_manager import Trace2WorldConfigManager
from trace2world_pipeline.app_spec.trace_loader import TraceLoader, TraceData, TraceStep


DEFAULT_MINERU_API_KEY = os.getenv("MINERU_API_KEY", "")


class MinerUFileRateLimiter:

    def __init__(
        self,
        *,
        max_files: int,
        window_seconds: float,
        state_path: str,
        logger: Trace2WorldLogger,
    ) -> None:
        self.max_files = int(max_files or 0)
        self.window_seconds = float(window_seconds or 60)
        self.state_path = state_path
        self.logger = logger

    async def acquire(self, file_count: int) -> None:
        if self.max_files <= 0 or file_count <= 0:
            return
        await asyncio.to_thread(self._acquire_sync, int(file_count))

    def _acquire_sync(self, file_count: int) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        requested = max(1, int(file_count))
        while True:
            with open(self.state_path, "a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.seek(0)
                    try:
                        payload = json.load(f)
                    except Exception:
                        payload = {}
                    now = time.time()
                    entries = [
                        item
                        for item in payload.get("entries", [])
                        if isinstance(item, dict)
                        and now - float(item.get("ts", 0)) < self.window_seconds
                    ]
                    used = sum(int(item.get("files", 0)) for item in entries)
                    if requested > self.max_files:
                        if not entries:
                            entries.append({"ts": now, "files": requested})
                            self._write_state(f, entries)
                            return
                        wait_seconds = max(
                            1.0,
                            self.window_seconds - (now - min(float(item.get("ts", now)) for item in entries)) + 1.0,
                        )
                    elif used + requested <= self.max_files:
                        entries.append({"ts": now, "files": requested})
                        self._write_state(f, entries)
                        return
                    else:
                        oldest = min(float(item.get("ts", now)) for item in entries)
                        wait_seconds = max(1.0, self.window_seconds - (now - oldest) + 1.0)
                    self.logger.log_info(
                        "[ScreenshotExtractor] MinerU file rate limit waiting "
                        f"{wait_seconds:.1f}s for {requested} file(s); "
                        f"used={used}/{self.max_files} in {self.window_seconds:.0f}s"
                    )
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            time.sleep(wait_seconds)

    @staticmethod
    def _write_state(handle, entries: List[Dict[str, Any]]) -> None:
        handle.seek(0)
        handle.truncate()
        json.dump({"entries": entries}, handle)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass
class ExtractedResource:
    source_screenshot: str
    resource_path: str
    resource_type: str
    bbox: Optional[Dict[str, int]] = None
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceExtractionResult:
    trace_id: str
    resources: List[ExtractedResource]
    screenshot_count: int
    error: Optional[str] = None
    mineru_batch_id: Optional[str] = None
    mineru_request: Dict[str, Any] = field(default_factory=dict)
    mineru_extract_result: List[Dict[str, Any]] = field(default_factory=list)


class ScreenshotResourceExtractor:


    SCREENSHOT_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}


    BATCH_SIZE_LIMIT = 200

    def __init__(self, config: Trace2WorldConfigManager, logger: Trace2WorldLogger = None):
        self.config = config
        self.logger = logger or Trace2WorldLogger()

        self.api_url = self.config.get("mineru_api_url", "https://mineru.net")
        self.api_key = self.config.get("mineru_api_key", "") or DEFAULT_MINERU_API_KEY
        self.timeout = self.config.get("mineru_timeout", 300)
        self.max_concurrent = self.config.get("mineru_max_concurrent", 5)
        self.upload_max_concurrent = self.config.get("mineru_upload_max_concurrent", 10)
        self.download_max_concurrent = self.config.get("mineru_download_max_concurrent", 10)
        self.poll_interval = self.config.get("mineru_poll_interval", 5)
        self.file_rate_limit = int(self.config.get("mineru_file_rate_limit", 50) or 0)
        self.file_rate_window_seconds = float(
            self.config.get("mineru_file_rate_window_seconds", 60) or 60
        )
        self.rate_limit_retry_max_attempts = int(
            self.config.get("mineru_rate_limit_retry_max_attempts", 6) or 1
        )
        self.rate_limit_retry_sleep = float(
            self.config.get("mineru_rate_limit_retry_sleep", self.file_rate_window_seconds + 5) or 65
        )
        self.rate_limiter = MinerUFileRateLimiter(
            max_files=self.file_rate_limit,
            window_seconds=self.file_rate_window_seconds,
            state_path=str(
                self.config.get("mineru_rate_limit_state_path")
                or os.path.join(tempfile.gettempdir(), "ghostweb_mineru_file_rate_limit.json")
            ),
            logger=self.logger,
        )
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())


        self.trace_loader = TraceLoader()


    async def extract_from_trace(
        self,
        trace_dir: str,
        output_dir: str = None,
        allowed_screenshots: Optional[Set[str]] = None,
    ) -> TraceExtractionResult:
        trace_id = os.path.basename(os.path.normpath(trace_dir))
        output_dir = output_dir or os.path.join(trace_dir, "extracted_resources")
        os.makedirs(output_dir, exist_ok=True)

        screenshots = self._discover_screenshots(trace_dir)
        if allowed_screenshots is not None:
            trace_abs = os.path.abspath(trace_dir)
            allowed = {
                str(item).replace("\\", "/")
                for item in allowed_screenshots
                if str(item).strip()
            }
            allowed_tokens = set(allowed)
            for item in allowed:
                allowed_tokens.update(self._screenshot_match_tokens(item, trace_abs))
            filtered = []
            for path in screenshots:
                if self._screenshot_match_tokens(path, trace_abs) & allowed_tokens:
                    filtered.append(path)
            screenshots = filtered
        if not screenshots:
            self.logger.log_info(f"[ScreenshotExtractor] No screenshots found in {trace_dir}")
            return TraceExtractionResult(
                trace_id=trace_id, resources=[], screenshot_count=0
            )

        self.logger.log_info(
            f"[ScreenshotExtractor] Found {len(screenshots)} screenshots in trace {trace_id}"
        )

        try:

            batch_id, screenshot_map, request_payload = await self._submit_extraction(screenshots)
            self.logger.log_info(
                f"[ScreenshotExtractor] Batch {batch_id} submitted with {len(screenshot_map)} files"
            )


            result = await self._poll_result(batch_id)
            self.logger.log_info(
                f"[ScreenshotExtractor] Batch {batch_id} completed"
            )


            resources = await self._download_resources(result, output_dir, screenshot_map)
            self.logger.log_info(
                f"[ScreenshotExtractor] Extracted {len(resources)} resources from trace {trace_id}"
            )

            return TraceExtractionResult(
                trace_id=trace_id,
                resources=resources,
                screenshot_count=len(screenshots),
                mineru_batch_id=batch_id,
                mineru_request=request_payload,
                mineru_extract_result=result.get("extract_result", []),
            )
        except Exception as e:
            self.logger.log_error(
                f"[ScreenshotExtractor] Failed to extract from trace {trace_id}: {e}"
            )
            return TraceExtractionResult(
                trace_id=trace_id,
                resources=[],
                screenshot_count=len(screenshots),
                error=str(e),
            )

    async def extract_all_traces(self, traces_dir: str, output_dir: str = None) -> Dict[str, TraceExtractionResult]:
        output_dir = output_dir or os.path.join(traces_dir, "extracted_resources")
        os.makedirs(output_dir, exist_ok=True)

        trace_dirs = self.trace_loader.discover_trace_dirs(traces_dir)
        if not trace_dirs:
            self.logger.log_warning(
                f"[ScreenshotExtractor] No trace directories found under {traces_dir}"
            )
            return {}

        self.logger.log_info(
            f"[ScreenshotExtractor] Discovered {len(trace_dirs)} trace directories"
        )

        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _process_one(tdir: str) -> TraceExtractionResult:
            async with semaphore:
                tid = os.path.basename(os.path.normpath(tdir))
                trace_output = os.path.join(output_dir, tid)
                return await self.extract_from_trace(tdir, trace_output)

        results_list = await asyncio.gather(
            *[_process_one(d) for d in trace_dirs],
            return_exceptions=True,
        )

        results: Dict[str, TraceExtractionResult] = {}
        total_resources = 0
        failures = 0

        for tdir, res in zip(trace_dirs, results_list):
            tid = os.path.basename(os.path.normpath(tdir))
            if isinstance(res, Exception):
                results[tid] = TraceExtractionResult(
                    trace_id=tid, resources=[], screenshot_count=0, error=str(res)
                )
                failures += 1
            else:
                results[tid] = res
                total_resources += len(res.resources)
                if res.error:
                    failures += 1

        self.logger.log_info(
            f"[ScreenshotExtractor] Summary: {len(trace_dirs)} traces, "
            f"{total_resources} resources extracted, {failures} failures"
        )
        return results


    async def _submit_extraction(self, screenshot_paths: List[str]) -> tuple:
        attempts = max(1, self.rate_limit_retry_max_attempts)
        for attempt in range(1, attempts + 1):
            await self.rate_limiter.acquire(len(screenshot_paths))
            try:
                return await self._submit_extraction_once(screenshot_paths)
            except RuntimeError as exc:
                if not self._is_mineru_rate_limit_error(str(exc)) or attempt >= attempts:
                    raise
                sleep_seconds = self._mineru_rate_limit_retry_sleep(attempt)
                self.logger.log_warning(
                    "[ScreenshotExtractor] MinerU submit rate-limited; "
                    f"retrying attempt {attempt + 1}/{attempts} after {sleep_seconds:.1f}s"
                )
                await asyncio.sleep(sleep_seconds)
        raise RuntimeError("MinerU submit failed after retry attempts")

    async def _submit_extraction_once(self, screenshot_paths: List[str]) -> tuple:
        files_payload = []
        screenshot_map: Dict[int, str] = {}

        for idx, path in enumerate(screenshot_paths):
            fname = os.path.basename(path)
            data_id = self._build_data_id(path)
            files_payload.append({"name": fname, "data_id": data_id, "is_ocr": True})
            screenshot_map[idx] = path

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "files": files_payload,
            "enable_formula": False,
            "enable_table": False,
            "model_version": "vlm",
            "language": "ch",
        }

        url = f"{self.api_url.rstrip('/')}/api/v4/file-urls/batch"

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:

            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"MinerU file-urls/batch failed (HTTP {resp.status}): {text}"
                    )
                resp_json = await resp.json()

            if resp_json.get("code") != 0:
                raise RuntimeError(
                    f"MinerU file-urls/batch returned error: {resp_json}"
                )

            data = resp_json["data"]
            batch_id = data["batch_id"]
            file_urls = data["file_urls"]

            if len(file_urls) != len(screenshot_paths):
                raise RuntimeError(
                    f"MinerU returned {len(file_urls)} URLs for {len(screenshot_paths)} files"
                )


            upload_semaphore = asyncio.Semaphore(self.upload_max_concurrent)

            async def _upload_one(presigned_url: str, file_path: str):
                async with upload_semaphore:
                    with open(file_path, "rb") as f:
                        file_data = f.read()

                    async with aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(ssl=self.ssl_context),
                        skip_auto_headers=["Content-Type"]
                    ) as upload_session:
                        async with upload_session.put(presigned_url, data=file_data) as upload_resp:
                            if upload_resp.status not in (200, 201):
                                text = await upload_resp.text()
                                raise RuntimeError(
                                    f"Upload failed for {file_path} (HTTP {upload_resp.status}): {text}"
                                )

            await asyncio.gather(
                *[
                    _upload_one(file_urls[i], screenshot_paths[i])
                    for i in range(len(screenshot_paths))
                ]
            )

        return batch_id, screenshot_map, body

    @staticmethod
    def _is_mineru_rate_limit_error(message: str) -> bool:
        text = str(message or "").lower()
        return "http 429" in text or "exceeded" in text and "files/min" in text

    def _mineru_rate_limit_retry_sleep(self, attempt: int) -> float:
        return max(self.rate_limit_retry_sleep, self.file_rate_window_seconds + min(attempt, 3))

    async def _poll_result(self, batch_id: str, timeout: int = None) -> Dict[str, Any]:
        timeout = timeout or self.timeout
        url = f"{self.api_url.rstrip('/')}/api/v4/extract-results/batch/{batch_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        elapsed = 0
        poll_interval = self.poll_interval

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
            while elapsed < timeout:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(
                            f"MinerU poll failed (HTTP {resp.status}): {text}"
                        )
                    resp_json = await resp.json()

                extract_results = resp_json.get("data", {}).get("extract_result", [])


                states = [r.get("state", "unknown") for r in extract_results]

                if all(s == "done" for s in states) and extract_results:
                    return resp_json["data"]

                if any(s == "failed" for s in states):
                    failed = [
                        r for r in extract_results if r.get("state") == "failed"
                    ]
                    raise RuntimeError(
                        f"MinerU extraction failed for {len(failed)} file(s): "
                        f"{json.dumps(failed, ensure_ascii=False)[:500]}"
                    )

                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        raise TimeoutError(
            f"MinerU batch {batch_id} did not complete within {timeout}s"
        )

    async def _download_resources(
        self,
        result: Dict[str, Any],
        output_dir: str,
        screenshot_map: Dict[int, str],
    ) -> List[ExtractedResource]:
        extract_results = result.get("extract_result", [])
        os.makedirs(output_dir, exist_ok=True)

        all_resources: List[ExtractedResource] = []
        download_semaphore = asyncio.Semaphore(self.download_max_concurrent)

        async def _process_one_result(idx: int, entry: Dict[str, Any]):
            async with download_semaphore:
                source_screenshot = screenshot_map.get(idx, "unknown")
                zip_url = entry.get("full_zip_url", "")
                if not zip_url:
                    return []


                entry_dir = os.path.join(output_dir, self._resource_step_dir_name(source_screenshot))
                os.makedirs(entry_dir, exist_ok=True)


                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
                    async with session.get(zip_url) as resp:
                        if resp.status != 200:
                            self.logger.log_warning(
                                f"[ScreenshotExtractor] Failed to download ZIP for {source_screenshot}"
                            )
                            return []
                        zip_bytes = await resp.read()


                resources: List[ExtractedResource] = []
                try:
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        zf.extractall(entry_dir)
                except zipfile.BadZipFile:
                    self.logger.log_warning(
                        f"[ScreenshotExtractor] Bad ZIP file for {source_screenshot}"
                    )
                    return []


                content_list = self._parse_content_list(entry_dir)


                image_metadata_map = self._build_image_metadata(content_list)


                images_dir = os.path.join(entry_dir, "images")
                if not os.path.isdir(images_dir):


                    for root, dirs, _files in os.walk(entry_dir):
                        if "images" in dirs:
                            images_dir = os.path.join(root, "images")
                            break

                if os.path.isdir(images_dir):
                    for fname in sorted(os.listdir(images_dir)):
                        fpath = os.path.join(images_dir, fname)
                        if not os.path.isfile(fpath):
                            continue

                        ext = os.path.splitext(fname)[1].lower()
                        if ext not in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.bmp'}:
                            continue

                        file_size = os.path.getsize(fpath)
                        width, height = self._get_image_dimensions(fpath)

                        meta = image_metadata_map.get(fname, {})
                        bbox = meta.get("bbox")
                        nearby_text = meta.get("nearby_text", "")

                        resource_type = self._classify_resource(fname, width, height, bbox)

                        resources.append(ExtractedResource(
                            source_screenshot=source_screenshot,
                            resource_path=fpath,
                            resource_type=resource_type,
                            bbox=bbox,
                            confidence=0.8 if bbox else 0.5,
                            metadata={
                                "width": width,
                                "height": height,
                                "file_size": file_size,
                                "bbox": bbox,
                                "nearby_text": nearby_text,
                            },
                        ))

                return resources

        tasks = [
            _process_one_result(idx, entry)
            for idx, entry in enumerate(extract_results)
        ]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results_nested:
            if isinstance(res, Exception):
                self.logger.log_warning(
                    f"[ScreenshotExtractor] Error processing result: {res}"
                )
            elif isinstance(res, list):
                all_resources.extend(res)

        return all_resources

    @staticmethod
    def _extract_step_index(source_screenshot: str) -> Optional[int]:
        return ScreenshotResourceExtractor._extract_screenshot_step_index(source_screenshot)

    def _resource_step_dir_name(self, source_screenshot: str) -> str:
        step_index = self._extract_step_index(source_screenshot)
        if step_index is not None:
            return f"step_{step_index:04d}"
        return os.path.splitext(os.path.basename(source_screenshot))[0]

    @staticmethod
    def _build_data_id(path: str) -> str:
        normalized = os.path.abspath(path).replace("\\", "/")
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        stem = os.path.splitext(os.path.basename(path))[0]
        safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)
        return f"{safe_stem}_{digest}"

    @staticmethod
    def _is_resource_output_path(root: str) -> bool:
        normalized = root.replace("\\", "/")
        return "/resources/" in normalized or normalized.endswith("/resources")

    @staticmethod
    def _is_extractor_output_path(root: str) -> bool:
        normalized = root.replace("\\", "/")
        return "/extracted_resources" in normalized or normalized.endswith("/extracted_resources")

    @staticmethod
    def _is_screenshot_path(path: str) -> bool:
        normalized = path.replace("\\", "/")
        if "/marked_screenshots/" in normalized or "/marked/" in normalized:
            return False
        if "/screenshots/" in normalized:
            return True
        fname = os.path.basename(path)
        stem = os.path.splitext(fname)[0]
        return stem.startswith("step") or stem.startswith("action_")

    @staticmethod
    def _extract_screenshot_step_index(
        path: str,
        trace_id: str = "",
        *,
        raw_screenshot_index_offset: int = 1,
    ) -> Optional[int]:
        stem = os.path.splitext(os.path.basename(str(path)))[0]
        match = re.fullmatch(r"step_(\d{4})", stem)
        if match:
            return int(match.group(1))
        match = re.fullmatch(r"step(\d+)", stem)
        if match:
            return int(match.group(1))
        match = re.fullmatch(r"action_(\d{4})", stem)
        if match:
            return int(match.group(1))
        if trace_id:
            match = re.fullmatch(re.escape(trace_id) + r"-0-(\d+)", stem)
            if match:
                return max(int(match.group(1)) - int(raw_screenshot_index_offset), 0)
        match = re.search(r"-0-(\d+)$", stem)
        if match:
            return max(int(match.group(1)) - int(raw_screenshot_index_offset), 0)
        return None

    @classmethod
    def _screenshot_match_tokens(cls, path: str, trace_dir_abs: str) -> Set[str]:
        raw = str(path or "").replace("\\", "/")
        trace_dir_abs = os.path.abspath(trace_dir_abs).replace("\\", "/")
        abs_path = os.path.abspath(raw).replace("\\", "/") if raw else ""
        name = os.path.basename(raw)
        stem = os.path.splitext(name)[0]
        tokens = {raw, abs_path, name, stem}
        try:
            rel = os.path.relpath(abs_path, trace_dir_abs).replace("\\", "/")
            tokens.add(rel)
        except ValueError:
            pass
        trace_id = os.path.basename(os.path.normpath(trace_dir_abs))
        raw_offset = TraceLoader._infer_meituan_raw_screenshot_index_offset(trace_dir_abs)
        step_index = cls._extract_screenshot_step_index(
            raw,
            trace_id,
            raw_screenshot_index_offset=raw_offset,
        )
        if step_index is not None:
            tokens.update({
                str(step_index),
                f"step_{step_index:04d}",
                f"step{step_index:02d}",
                f"action_{step_index:04d}",
                f"{trace_id}-0-{step_index + raw_offset}",
                f"screenshots/{trace_id}-0-{step_index + raw_offset}.png",
                f"screenshots/{trace_id}-0-{step_index + raw_offset}.jpg",
                f"screenshots/action_{step_index:04d}.jpg",
                f"screenshots/action_{step_index:04d}.png",
            })
        return {token for token in tokens if token}


    def _parse_content_list(self, entry_dir: str) -> List[Dict[str, Any]]:

        v2_candidates = []
        v1_candidates = []

        search_dirs = [entry_dir]
        for item in os.listdir(entry_dir):
            subdir = os.path.join(entry_dir, item)
            if os.path.isdir(subdir):
                search_dirs.append(subdir)

        for d in search_dirs:
            v2_candidates.append(os.path.join(d, "content_list_v2.json"))

            for f in os.listdir(d) if os.path.isdir(d) else []:
                if f.endswith("_content_list.json"):
                    v1_candidates.append(os.path.join(d, f))


        for path in v2_candidates:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if isinstance(data, list) and data and isinstance(data[0], list):
                        flat = []
                        for page in data:
                            flat.extend(page)
                        return flat
                    return data if isinstance(data, list) else []
                except (json.JSONDecodeError, OSError):
                    pass


        for path in v1_candidates:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

        return []

    def _build_image_metadata(self, content_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        metadata_map: Dict[str, Dict[str, Any]] = {}

        text_items = []
        image_items = []

        for item in content_list:
            item_type = item.get("type", "")
            if item_type == "image" or item_type == "img":
                image_items.append(item)
            elif item_type in ("paragraph", "text", "title"):
                text_items.append(item)

        for img in image_items:

            img_path = img.get("img_path", "")
            if not img_path:
                content = img.get("content", {})
                if isinstance(content, dict):
                    img_path = content.get("image_source", {}).get("path", "")
            fname = os.path.basename(img_path)
            if not fname:
                continue

            raw_bbox = img.get("bbox")
            bbox = None
            if raw_bbox and len(raw_bbox) >= 4:
                x0, y0, x1, y1 = raw_bbox[:4]
                bbox = {
                    "x": int(x0),
                    "y": int(y0),
                    "width": int(x1 - x0),
                    "height": int(y1 - y0),
                }

            nearby_text = self._find_nearby_text(raw_bbox, text_items)

            metadata_map[fname] = {
                "bbox": bbox,
                "nearby_text": nearby_text,
            }

        return metadata_map

    @staticmethod
    def _find_nearby_text(
        image_bbox: Optional[List], text_items: List[Dict[str, Any]], threshold: float = 100.0
    ) -> str:
        if not image_bbox or len(image_bbox) < 4:
            return ""

        ix0, iy0, ix1, iy1 = image_bbox[:4]
        img_cy = (iy0 + iy1) / 2

        nearby = []
        for item in text_items:
            tb = item.get("bbox")
            if not tb or len(tb) < 4:
                continue
            tx0, ty0, tx1, ty1 = tb[:4]
            txt_cy = (ty0 + ty1) / 2


            if abs(img_cy - txt_cy) < threshold or (ty0 <= iy1 + threshold and ty1 >= iy0 - threshold):

                text = item.get("text", "")
                if not text:
                    content = item.get("content", {})
                    if isinstance(content, dict):
                        parts = content.get("paragraph_content", [])
                        text = " ".join(
                            p.get("content", "") for p in parts if isinstance(p, dict)
                        )
                text = text.strip()
                if text:
                    nearby.append(text)

        return " ".join(nearby[:5])

    @staticmethod
    def _classify_resource(fname: str, width: int, height: int, bbox: Optional[Dict]) -> str:
        fname_lower = fname.lower()

        if "icon" in fname_lower or "logo" in fname_lower:
            return "icon"

        if width > 0 and height > 0:
            if width <= 64 and height <= 64:
                return "icon"
            if width <= 128 and height <= 128:
                return "icon"

            aspect = max(width, height) / max(min(width, height), 1)
            if aspect > 3:
                return "banner"
            if width >= 800 and height >= 400:
                return "background"

        return "image"

    @staticmethod
    def _get_image_dimensions(fpath: str) -> tuple:
        try:
            with open(fpath, "rb") as f:
                header = f.read(32)


            if header[:8] == b'\x89PNG\r\n\x1a\n':
                import struct
                w, h = struct.unpack(">II", header[16:24])
                return int(w), int(h)


            if header[:2] == b'\xff\xd8':
                return ScreenshotResourceExtractor._read_jpeg_dimensions(fpath)


            if header[:6] in (b'GIF87a', b'GIF89a'):
                import struct
                w, h = struct.unpack("<HH", header[6:10])
                return int(w), int(h)


            if header[:2] == b'BM':
                import struct
                w, h = struct.unpack("<ii", header[18:26])
                return abs(int(w)), abs(int(h))


            if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                if header[12:16] == b'VP8 ':
                    import struct
                    w = struct.unpack("<H", header[26:28])[0] & 0x3FFF
                    h = struct.unpack("<H", header[28:30])[0] & 0x3FFF
                    return int(w), int(h)

        except Exception:
            pass

        return 0, 0

    @staticmethod
    def _read_jpeg_dimensions(fpath: str) -> tuple:
        import struct
        try:
            with open(fpath, "rb") as f:
                f.read(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2:
                        break
                    if marker[0] != 0xFF:
                        break
                    m = marker[1]

                    if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        return int(w), int(h)
                    else:
                        length = struct.unpack(">H", f.read(2))[0]
                        f.read(length - 2)
        except Exception:
            pass
        return 0, 0


    def _discover_screenshots(self, trace_dir: str) -> List[str]:
        raw_dir = os.path.join(trace_dir, "screenshots")
        if os.path.isdir(raw_dir):
            raw_screenshots = []
            for fname in os.listdir(raw_dir):
                ext = os.path.splitext(fname)[1].lower()
                if ext in self.SCREENSHOT_EXTENSIONS:
                    raw_screenshots.append(os.path.join(raw_dir, fname))
            return sorted(raw_screenshots, key=lambda path: (
                self._extract_screenshot_step_index(path, os.path.basename(os.path.normpath(trace_dir))) is None,
                self._extract_screenshot_step_index(path, os.path.basename(os.path.normpath(trace_dir))) or 0,
                path,
            ))

        screenshots = []


        for root, _dirs, files in os.walk(trace_dir):
            if self._is_extractor_output_path(root) or self._is_resource_output_path(root):
                continue
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in self.SCREENSHOT_EXTENSIONS:
                    continue
                full_path = os.path.join(root, fname)
                if self._is_screenshot_path(full_path):
                    screenshots.append(full_path)

        return sorted(screenshots)
