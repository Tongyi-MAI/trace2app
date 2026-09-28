
import os
import json
import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Trace2WorldLogger:

    def __init__(self, output_dir: Optional[str] = None,
                 log_level: str = "INFO"):
        self.output_dir = output_dir
        self.log_level = getattr(LogLevel, log_level.upper(), LogLevel.INFO)


        self.lock = threading.Lock()


        self.logs = []
        self.stage_logs = {}


        self.current_stage = None
        self.stage_start_times = {}
        self.stage_end_times = {}


        self.stage_directories = {}
        self.current_stage_category = None


        self.log_file = None
        self.stage_files = {}


        self.multi_page_stages = set()
        self.page_loggers = {}
        self.current_multi_page_stage = None


        self.timing_file = None


        self.llm_call_counters = {}
        self.current_call_id = None


        self._setup_logging()

    def _setup_logging(self):
        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)


            self.log_file = os.path.join(self.output_dir, "tdd_generation.log")
            self._write_to_file(self.log_file, f"TDD Generation Log - Started at {datetime.now()}\n")
            self._write_to_file(self.log_file, "=" * 80 + "\n")


            self.timing_file = os.path.join(self.output_dir, "timing_log.txt")
            if not os.path.exists(self.timing_file):
                self._write_to_file(self.timing_file, f"Timing Log for Parallel Execution Verification\n")
                self._write_to_file(self.timing_file, f"Started at {datetime.now()}\n")
                self._write_to_file(self.timing_file, "=" * 80 + "\n")

    def start_stage(self, stage_name: str, category: str = None):

        if category is None:
            category = self._get_stage_category(stage_name)

        with self.lock:
            self.current_stage = stage_name
            self.current_stage_category = category
            self.stage_start_times[stage_name] = datetime.now()


            self.llm_call_counters[stage_name] = 0


            if self.output_dir:
                stage_folder = stage_name.lower().replace(' ', '_')
                if category:
                    stage_dir = os.path.join(self.output_dir, category, stage_folder)
                else:
                    stage_dir = os.path.join(self.output_dir, stage_folder)
                os.makedirs(stage_dir, exist_ok=True)
                self.stage_directories[stage_name] = stage_dir


                stage_file = os.path.join(stage_dir, "stage.log")
                self.stage_files[stage_name] = stage_file
                self._write_to_file(stage_file, f"Stage: {stage_name}\n")
                self._write_to_file(stage_file, f"Category: {category}\n")
                self._write_to_file(stage_file, f"Started: {self.stage_start_times[stage_name]}\n")
                self._write_to_file(stage_file, "=" * 80 + "\n")


        self.log_info(f"{'='*60}")
        self.log_info(f"Stage Started: {stage_name} ({category})")
        self.log_info(f"Start Time: {self.stage_start_times[stage_name]}")
        self.log_info(f"{'='*60}")

    def end_stage(self, stage_name: str):
        duration = 0.0
        with self.lock:
            if stage_name in self.stage_start_times:
                self.stage_end_times[stage_name] = datetime.now()
                duration = (self.stage_end_times[stage_name] - self.stage_start_times[stage_name]).total_seconds()


                if stage_name in self.stage_files:
                    stage_file = self.stage_files[stage_name]
                    self._write_to_file(stage_file, f"\nCompleted: {self.stage_end_times[stage_name]}\n")
                    self._write_to_file(stage_file, f"Duration: {duration:.2f} seconds\n")

            self.current_stage = None


        if stage_name in self.stage_start_times:
            self.log_info(f"Stage Completed: {stage_name}")
            self.log_info(f"Duration: {duration:.2f} seconds")
            self.log_info(f"{'='*60}\n")

    def log(self, level: str, message: str, stage: Optional[str] = None):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stage = stage or self.current_stage


        if stage:
            log_entry = f"[{timestamp}] [{level}] [{stage}] {message}"
        else:
            log_entry = f"[{timestamp}] [{level}] {message}"


        with self.lock:
            self.logs.append(log_entry)
            if stage:
                if stage not in self.stage_logs:
                    self.stage_logs[stage] = []
                self.stage_logs[stage].append(log_entry)


        if level == "ERROR":
            print(f"❌ {log_entry}")
        elif level == "WARNING":
            print(f"⚠️ {log_entry}")
        else:
            print(log_entry)


        self._write_log_to_files(log_entry, stage)

    def log_info(self, message: str, stage: Optional[str] = None):
        if self._should_log(LogLevel.INFO):
            self.log("INFO", message, stage)

    def log_debug(self, message: str, stage: Optional[str] = None):
        if self._should_log(LogLevel.DEBUG):
            self.log("DEBUG", message, stage)

    def log_warning(self, message: str, stage: Optional[str] = None):
        if self._should_log(LogLevel.WARNING):
            self.log("WARNING", message, stage)

    def log_error(self, message: str, stage: Optional[str] = None):
        if self._should_log(LogLevel.ERROR):
            self.log("ERROR", message, stage)

    def log_critical(self, message: str, stage: Optional[str] = None):
        if self._should_log(LogLevel.CRITICAL):
            self.log("CRITICAL", message, stage)

    def log_api_call(self, api_name: str, prompt: str,
                     additional_args: Optional[Dict[str, Any]] = None,
                     stage: Optional[str] = None) -> str:
        stage_name = stage or self.current_stage


        call_id = None
        if stage_name:
            with self.lock:
                self.llm_call_counters[stage_name] = self.llm_call_counters.get(stage_name, 0) + 1
                call_id = f"call_{self.llm_call_counters[stage_name]:03d}"

        self.log_info("=" * 60, stage)
        self.log_info(f"API Call: {api_name}", stage)
        if stage_name and call_id:
            self.log_info(f"Call ID: {call_id}", stage)

        if additional_args:
            self.log_info(f"Additional Args: {json.dumps(additional_args, indent=2)}", stage)


        self._log_prompt_to_files_only(prompt, stage)
        if call_id:
            self._log_llm_call_input(prompt, stage_name, call_id)

        return call_id

    def log_api_response(self, api_name: str, success: bool,
                        response: Optional[Any] = None,
                        error: Optional[str] = None,
                        usage_info: Optional[Dict[str, Any]] = None,
                        stage: Optional[str] = None,
                        call_id: Optional[str] = None):
        stage_name = stage or self.current_stage

        if success:
            self.log_info(f"✅ Success - API Response: {api_name}", stage)
            if call_id:
                self.log_info(f"Call ID: {call_id}", stage)
            if usage_info:
                self.log_info(f"Token Usage: {json.dumps(usage_info)}", stage)
            if response:

                self._log_response_to_files_only(response, stage)
                if stage_name and call_id:
                    self._log_llm_call_output(response, stage_name, call_id)
        else:
            self.log_error(f"❌ Failed - API Response: {api_name}", stage)
            if call_id:
                self.log_info(f"Call ID: {call_id}", stage)
            if error:
                self.log_error(f"Error: {error}", stage)
                if stage_name and call_id:
                    self._log_llm_call_output(f"ERROR: {error}", stage_name, call_id)

    def log_exception(self, exception: Exception, context: str = "",
                     stage: Optional[str] = None):
        import traceback

        self.log_error(f"Exception in {context}: {str(exception)}", stage)
        self.log_error("Stack Trace:", stage)
        for line in traceback.format_exc().split('\n'):
            self.log_error(line, stage)

    def _should_log(self, level: LogLevel) -> bool:
        level_values = {
            LogLevel.DEBUG: 10,
            LogLevel.INFO: 20,
            LogLevel.WARNING: 30,
            LogLevel.ERROR: 40,
            LogLevel.CRITICAL: 50
        }
        return level_values[level] >= level_values[self.log_level]

    def _write_log_to_files(self, log_entry: str, stage: Optional[str] = None):
        if not self.output_dir:
            return

        with self.lock:

            if self.log_file:
                self._write_to_file(self.log_file, log_entry + '\n', append=True)


            if stage and stage in self.stage_files:
                self._write_to_file(self.stage_files[stage], log_entry + '\n', append=True)

    def _log_prompt_to_files_only(self, prompt: str, stage: Optional[str] = None):
        if not self.output_dir:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stage_name = stage or self.current_stage
        line_count = len((prompt or "").splitlines())
        char_count = len(prompt or "")
        summary = (
            "Prompt omitted from main/stage logs "
            f"(chars={char_count}, lines={line_count}); see per-call input log."
        )
        if stage_name:
            log_entry = f"[{timestamp}] [INFO] [{stage_name}] {summary}"
        else:
            log_entry = f"[{timestamp}] [INFO] {summary}"

        with self.lock:
            if self.log_file:
                self._write_to_file(self.log_file, log_entry + '\n', append=True)
            if stage_name and stage_name in self.stage_files:
                self._write_to_file(self.stage_files[stage_name], log_entry + '\n', append=True)

    def _log_response_to_files_only(self, response: Any, stage: Optional[str] = None):
        if not self.output_dir or not response:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stage_name = stage or self.current_stage
        if isinstance(response, str):
            char_count = len(response)
            line_count = len(response.splitlines())
            payload_type = "str"
        else:
            response_json = json.dumps(response, ensure_ascii=False, default=str)
            char_count = len(response_json)
            line_count = len(response_json.splitlines())
            payload_type = type(response).__name__
        summary = (
            "Response omitted from main/stage logs "
            f"(type={payload_type}, chars={char_count}, lines={line_count}); see per-call output log."
        )
        if stage_name:
            log_entry = f"[{timestamp}] [DEBUG] [{stage_name}] {summary}"
        else:
            log_entry = f"[{timestamp}] [DEBUG] {summary}"

        with self.lock:
            if self.log_file:
                self._write_to_file(self.log_file, log_entry + '\n', append=True)
            if stage_name and stage_name in self.stage_files:
                self._write_to_file(self.stage_files[stage_name], log_entry + '\n', append=True)

    def _write_to_file(self, filepath: str, content: str, append: bool = True):
        mode = 'a' if append else 'w'
        try:

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, mode, encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            print(f"Failed to write to log file {filepath}: {e}")

    def _get_stage_category(self, stage_name: str) -> str:
        stage_lower = stage_name.lower().replace(' ', '_')


        prepare_stages = {
            'generate_tasks', 'design_primary_architecture', 'extract_data_models',
            'design_interfaces'
        }


        backend_stages = {
            'generate_data', 'parallel_generation', 'validate_and_fix',
            'replace_data_images'
        }


        frontend_stages = {
            'design_architecture', 'design_pages', 'analyze_design_image',
            'design_page_layouts', 'generate_page_framework', 'generate_pages',
            'replace_page_images', 'inject_data', 'generate_evaluators'
        }

        if stage_lower in prepare_stages:
            return 'prepare'
        elif stage_lower in backend_stages:
            return 'backend'
        elif stage_lower in frontend_stages:
            return 'frontend'
        else:

            return 'prepare'

    def _log_llm_call_input(self, prompt: str, stage_name: str, call_id: str):
        if not self.output_dir or not stage_name or not call_id:
            return

        stage_dir = self.stage_directories.get(stage_name)
        if not stage_dir:
            return

        input_file = os.path.join(stage_dir, f"{call_id}_input.log")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"[{timestamp}] LLM Call Input\n"
        content += "=" * 80 + "\n"
        content += prompt + "\n"
        content += "=" * 80 + "\n"

        self._write_to_file(input_file, content, append=False)

    def _log_llm_call_output(self, response: Any, stage_name: str, call_id: str):
        if not self.output_dir or not stage_name or not call_id:
            return

        stage_dir = self.stage_directories.get(stage_name)
        if not stage_dir:
            return

        output_file = os.path.join(stage_dir, f"{call_id}_output.log")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"[{timestamp}] LLM Call Output\n"
        content += "=" * 80 + "\n"

        if isinstance(response, str):
            content += response + "\n"
        else:
            content += json.dumps(response, indent=2, ensure_ascii=False) + "\n"

        content += "=" * 80 + "\n"

        self._write_to_file(output_file, content, append=False)

    def get_logs(self, stage: Optional[str] = None) -> List[str]:
        with self.lock:
            if stage and stage in self.stage_logs:
                return self.stage_logs[stage].copy()
            return self.logs.copy()

    def get_summary(self) -> Dict[str, Any]:
        with self.lock:
            summary = {
                "total_logs": len(self.logs),
                "stages": {}
            }

            for stage in self.stage_start_times:
                stage_info = {
                    "start_time": self.stage_start_times[stage].isoformat(),
                    "log_count": len(self.stage_logs.get(stage, []))
                }

                if stage in self.stage_end_times:
                    stage_info["end_time"] = self.stage_end_times[stage].isoformat()
                    duration = (self.stage_end_times[stage] - self.stage_start_times[stage]).total_seconds()
                    stage_info["duration_seconds"] = duration

                summary["stages"][stage] = stage_info

            return summary

    def save_summary(self, filepath: Optional[str] = None):
        if not filepath and self.output_dir:
            filepath = os.path.join(self.output_dir, "logging_summary.json")

        if filepath:
            summary = self.get_summary()
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            self.log_info(f"Logging summary saved to {filepath}")

    def log_step_start(self, step_name: str, pipeline: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_entry = f"[{timestamp}] [{pipeline.upper()}] [START] {step_name}\n"

        with self.lock:
            if self.timing_file:
                self._write_to_file(self.timing_file, log_entry, append=True)


        self.log_info(f"📍 [{pipeline.upper()}] Started: {step_name}")

    def log_step_end(self, step_name: str, pipeline: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_entry = f"[{timestamp}] [{pipeline.upper()}] [END] {step_name}\n"

        with self.lock:
            if self.timing_file:
                self._write_to_file(self.timing_file, log_entry, append=True)


        self.log_info(f"✅ [{pipeline.upper()}] Completed: {step_name}")

    def start_multi_page_stage(self, stage_name: str, category: str = None):

        if category is None:
            category = self._get_stage_category(stage_name)

        with self.lock:
            self.current_stage = stage_name
            self.current_multi_page_stage = stage_name
            self.current_stage_category = category
            self.stage_start_times[stage_name] = datetime.now()
            self.multi_page_stages.add(stage_name)


            self.llm_call_counters[stage_name] = 0


            if self.output_dir:
                stage_folder = stage_name.lower().replace(' ', '_')
                if category:
                    stage_dir = os.path.join(self.output_dir, category, stage_folder)
                else:
                    stage_dir = os.path.join(self.output_dir, stage_folder)
                os.makedirs(stage_dir, exist_ok=True)
                self.stage_directories[stage_name] = stage_dir


                stage_file = os.path.join(stage_dir, "stage.log")
                self.stage_files[stage_name] = stage_file
                self._write_to_file(stage_file, f"Multi-Page Stage: {stage_name}\n")
                self._write_to_file(stage_file, f"Category: {category}\n")
                self._write_to_file(stage_file, f"Started: {self.stage_start_times[stage_name]}\n")
                self._write_to_file(stage_file, "=" * 80 + "\n")


        self.log_info(f"{'='*60}")
        self.log_info(f"Multi-Page Stage Started: {stage_name} ({category})")
        self.log_info(f"Start Time: {self.stage_start_times[stage_name]}")
        self.log_info(f"{'='*60}")

    def end_multi_page_stage(self, stage_name: str):
        duration = 0.0
        with self.lock:
            if stage_name in self.stage_start_times:
                self.stage_end_times[stage_name] = datetime.now()
                duration = (self.stage_end_times[stage_name] - self.stage_start_times[stage_name]).total_seconds()


                if stage_name in self.stage_files:
                    stage_file = self.stage_files[stage_name]
                    self._write_to_file(stage_file, f"\nCompleted: {self.stage_end_times[stage_name]}\n")
                    self._write_to_file(stage_file, f"Duration: {duration:.2f} seconds\n")


                    page_count = len([k for k in self.page_loggers if k.startswith(f"{stage_name}:")])
                    self._write_to_file(stage_file, f"Pages Processed: {page_count}\n")

            self.current_stage = None
            self.current_multi_page_stage = None


        if stage_name in self.stage_start_times:
            self.log_info(f"Multi-Page Stage Completed: {stage_name}")
            self.log_info(f"Duration: {duration:.2f} seconds")
            self.log_info(f"{'='*60}\n")

    def for_page(self, page_name: str) -> 'PageLogger':
        if not self.current_multi_page_stage:
            raise RuntimeError("for_page() called outside of multi-page stage context")


        page_key = f"{self.current_multi_page_stage}:{page_name}"


        if page_key in self.page_loggers:
            return self.page_loggers[page_key]


        page_logger = PageLogger(
            parent=self,
            stage_name=self.current_multi_page_stage,
            page_name=page_name,
            stage_dir=self.stage_directories.get(self.current_multi_page_stage)
        )

        self.page_loggers[page_key] = page_logger
        return page_logger


class PageLogger:

    def __init__(self, parent: Trace2WorldLogger, stage_name: str, page_name: str,
                 stage_dir: Optional[str]):
        self.parent = parent
        self.stage_name = stage_name
        self.page_name = page_name
        self.stage_dir = stage_dir


        self.page_file = None
        if self.stage_dir:

            safe_page_name = page_name.replace('/', '_').replace('\\', '_').replace(':', '_').replace('.', '_')
            self.page_file = os.path.join(self.stage_dir, f"{safe_page_name}.log")
            self._write_header()

    def _write_header(self):
        if self.page_file:
            with open(self.page_file, 'w', encoding='utf-8') as f:
                f.write(f"Page: {self.page_name}\n")
                f.write(f"Stage: {self.stage_name}\n")
                f.write(f"Started: {datetime.now()}\n")
                f.write("=" * 80 + "\n\n")

    def log_info(self, message: str):

        self.parent.log_info(f"[{self.page_name}] {message}", stage=self.stage_name)


        if self.page_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"[{timestamp}] [INFO] {message}\n"
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)

    def log_error(self, message: str):

        self.parent.log_error(f"[{self.page_name}] {message}", stage=self.stage_name)


        if self.page_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"[{timestamp}] [ERROR] {message}\n"
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)

    def log_warning(self, message: str):

        self.parent.log_warning(f"[{self.page_name}] {message}", stage=self.stage_name)


        if self.page_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"[{timestamp}] [WARNING] {message}\n"
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)

    def log_debug(self, message: str):

        self.parent.log_debug(f"[{self.page_name}] {message}", stage=self.stage_name)


        if self.page_file and self.parent._should_log(LogLevel.DEBUG):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"[{timestamp}] [DEBUG] {message}\n"
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)

    def _log_prompt_to_file_only(self, prompt: str):
        if self.page_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line_count = len((prompt or "").splitlines())
            char_count = len(prompt or "")
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(
                    f"[{timestamp}] [INFO] Prompt omitted from page log "
                    f"(chars={char_count}, lines={line_count}).\n"
                )

    def _log_response_to_file_only(self, response: Any):
        if self.page_file and response and self.parent._should_log(LogLevel.DEBUG):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(response, str):
                char_count = len(response)
                line_count = len(response.splitlines())
                payload_type = "str"
            else:
                response_json = json.dumps(response, ensure_ascii=False, default=str)
                char_count = len(response_json)
                line_count = len(response_json.splitlines())
                payload_type = type(response).__name__
            with open(self.page_file, 'a', encoding='utf-8') as f:
                f.write(
                    f"[{timestamp}] [DEBUG] Response omitted from page log "
                    f"(type={payload_type}, chars={char_count}, lines={line_count}).\n"
                )

    def log_api_call(self, api_name: str, prompt: str,
                     additional_args: Optional[Dict[str, Any]] = None):
        self.log_info("=" * 60)
        self.log_info(f"API Call: {api_name}")

        if additional_args:
            self.log_info(f"Additional Args: {json.dumps(additional_args, indent=2)}")


        self._log_prompt_to_file_only(prompt)

    def log_api_response(self, api_name: str, success: bool,
                        response: Optional[Any] = None,
                        error: Optional[str] = None,
                        usage_info: Optional[Dict[str, Any]] = None):
        if success:
            self.log_info(f"✅ Success - API Response: {api_name}")
            if usage_info:
                self.log_info(f"Token Usage: {json.dumps(usage_info)}")
            if response:

                self._log_response_to_file_only(response)
        else:
            self.log_error(f"❌ Failed - API Response: {api_name}")
            if error:
                self.log_error(f"Error: {error}")
