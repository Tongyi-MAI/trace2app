
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from trace2world.agent._types import ChatModel, GenerateResult
from trace2world.agent.action_space import ActionSpace
from trace2world.agent.image_resize import ImageResizer, ResizeScreenshotMixin
from trace2world.agent.policy_runtime import (
    MessagesLogger,
    StepRecord,
    _decode_png_size,
)
from trace2world.agent.tool_parser import FunctionCall, ToolParser
from trace2world.utils.messages import elide_images, hide_history_images

logger = logging.getLogger(__name__)


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


_VALID_ACTIONS = frozenset({
    "click", "double_click", "long_press", "type", "open",
    "drag", "swipe", "system_button", "wait", "terminate",
    "answer", "ask_user",
})


_TERMINAL_ACTIONS = frozenset({"terminate", "answer"})


@ToolParser.register("qwen")
class QwenToolParser(ToolParser):

    SCALE_FACTOR = 999


    VALID_ACTIONS = _VALID_ACTIONS

    def __init__(
        self,
        tokenizer: Any,
        original_image_width: int | None = None,
        original_image_height: int | None = None,
    ) -> None:
        super().__init__(tokenizer)
        self.original_image_width = original_image_width
        self.original_image_height = original_image_height

    async def extract_tool_calls(
        self, responses_ids: list[int],
    ) -> tuple[dict[str, Any], list[FunctionCall]]:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(responses_ids, skip_special_tokens=True),
        )

        info: dict[str, Any] = {
            "raw_text": text,
            "text": text,
            "format_reward": 0.0,
            "is_done": True,
            "action_dict": {},
        }

        try:
            parsed = self._parse_output(text)
        except Exception as e:
            info["action_dict"] = {"action_type": "unknown", "error": str(e)}
            return info, []

        arguments = parsed.get("arguments", {})
        if not isinstance(arguments, dict):
            info["action_dict"] = {"action_type": "unknown", "error": "Invalid arguments"}
            return info, []

        format_reward = self._calculate_format_reward(text)

        try:
            action_dict = self._convert_to_action(arguments)
        except Exception as e:
            info["action_dict"] = {"action_type": "unknown", "error": str(e)}
            return info, []

        action_type = action_dict.get("action_type", "unknown")
        is_done = action_type in _TERMINAL_ACTIONS
        arguments_str = json.dumps(action_dict, ensure_ascii=False, sort_keys=True)

        info["action_dict"] = action_dict
        info["format_reward"] = format_reward
        info["is_done"] = is_done
        return info, [FunctionCall(name="mobile_use", arguments=arguments_str)]

    @staticmethod
    def _parse_output(text: str) -> dict[str, Any]:
        text = text.strip()
        if "<think>" not in text:
            text = "<think>" + text

        think_m = _THINK_RE.search(text)
        thinking = think_m.group(1).strip() if think_m else ""

        tool_m = _TOOL_CALL_RE.search(text)
        if not tool_m:
            raise ValueError("No <tool_call> block found")

        body = tool_m.group(1).strip()
        parsed = _parse_tool_call_json(body)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed)}")


        if "name" not in parsed and "action_type" not in parsed:
            raise ValueError("tool_call missing required name")
        if "name" in parsed and parsed.get("name") != "mobile_use":
            raise ValueError(f"unsupported tool name: {parsed.get('name')!r}")
        arguments = parsed.get("arguments")
        if arguments is None and "action_type" in parsed:
            arguments = parsed

        return {
            "thinking": thinking,
            "tool_name": parsed.get("name", "mobile_use"),
            "arguments": arguments if arguments is not None else {},
        }

    def _convert_to_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.original_image_width is None or self.original_image_height is None:
            raise ValueError(
                "QwenToolParser needs original_image_width/height"
            )

        W = self.original_image_width
        H = self.original_image_height
        S = self.SCALE_FACTOR

        action = arguments.get("action_type", arguments.get("action", ""))
        if not isinstance(action, str) or action not in self.VALID_ACTIONS:
            raise ValueError(f"unsupported action_type: {action!r}")

        if action in ("click", "long_press"):
            coord = arguments.get("coordinate", [0, 0])
            return {
                "action_type": action,
                "x": int(float(coord[0]) / S * W),
                "y": int(float(coord[1]) / S * H),
            }

        if action == "double_click":
            coord = arguments.get("coordinate", [0, 0])
            return {
                "action_type": "double_tap",
                "x": int(float(coord[0]) / S * W),
                "y": int(float(coord[1]) / S * H),
            }

        if action == "drag":
            sc = arguments.get("start_coordinate", [0, 0])
            ec = arguments.get("end_coordinate", [0, 0])
            return {
                "action_type": "drag",
                "start_x": int(float(sc[0]) / S * W),
                "start_y": int(float(sc[1]) / S * H),
                "end_x": int(float(ec[0]) / S * W),
                "end_y": int(float(ec[1]) / S * H),
            }

        if action == "swipe":
            sc = arguments.get("start_coordinate")
            ec = arguments.get("end_coordinate")
            if (
                isinstance(sc, (list, tuple))
                and isinstance(ec, (list, tuple))
                and len(sc) >= 2
                and len(ec) >= 2
            ):
                return {
                    "action_type": "swipe",
                    "start_x": int(float(sc[0]) / S * W),
                    "start_y": int(float(sc[1]) / S * H),
                    "end_x": int(float(ec[0]) / S * W),
                    "end_y": int(float(ec[1]) / S * H),
                }
            direction = str(arguments.get("direction") or "up").lower()
            out: dict[str, Any] = {"action_type": "swipe", "direction": direction}
            coord = arguments.get("coordinate")
            if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                out["x"] = int(float(coord[0]) / S * W)
                out["y"] = int(float(coord[1]) / S * H)
            return out

        if action == "type":
            return {"action_type": "input_text", "text": arguments.get("text", "")}

        if action == "open":
            return {"action_type": "open_app", "app_name": arguments.get("text", "")}

        if action == "system_button":
            button = arguments.get("button", "").lower()
            button_map = {
                "back": "navigate_back",
                "home": "navigate_home",
                "enter": "keyboard_enter",
                "menu": "navigate_home",
            }
            mapped = button_map.get(button)
            if mapped:
                return {"action_type": mapped}
            return {"action_type": "unknown", "text": f"Unknown button: {button}"}

        if action == "terminate":


            answer = arguments.get("answer")
            if answer is not None:
                return {"action_type": "answer", "text": str(answer)}
            return {"action_type": "terminate", "status": arguments.get("status", "success")}

        if action == "answer":
            return {"action_type": "answer", "text": str(arguments.get("text", ""))}

        if action == "wait":
            out = {"action_type": "wait"}
            for key in ("waiting_time", "duration", "seconds"):
                if key in arguments:
                    out[key] = arguments[key]
                    break
            return out

        if action == "ask_user":


            return {"action_type": "ask_user", "text": arguments.get("text", "")}

        return {"action_type": "unknown", "text": f"Unknown action: {action}"}

    @classmethod
    def _calculate_format_reward(cls, text: str) -> float:
        try:

            tc_match = _TOOL_CALL_RE.search(text)
            if not tc_match:
                return 0.0
            data = _parse_tool_call_json(tc_match.group(1).strip())
            if not isinstance(data, dict):
                return 0.0
            args = data.get("arguments")
            if args is None and "action_type" in data:
                args = data
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return 0.0


            at = args.get("action_type", args.get("action"))


            return 1.0 if isinstance(at, str) and at in cls.VALID_ACTIONS else 0.0
        except (ValueError, KeyError, TypeError):
            return 0.0


def _parse_tool_call_json(body: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        repaired = _repair_single_extra_closing_brace(body, exc)
        if repaired is None:
            repaired = _repair_malformed_terminate_tool_call(body)
        if repaired is None:
            raise ValueError(str(exc)) from exc
        return repaired
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed)}")
    return parsed


def _repair_single_extra_closing_brace(
    body: str,
    error: json.JSONDecodeError,
) -> dict[str, Any] | None:
    if error.msg != "Extra data":
        return None
    prefix = body[: error.pos].rstrip()
    suffix = body[error.pos :].strip()
    if suffix != "}":
        return None
    try:
        parsed = json.loads(prefix)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _repair_malformed_terminate_tool_call(body: str) -> dict[str, Any] | None:
    action_key: str | None = None
    if re.search(r'"action_type"\s*:\s*"terminate"', body):
        action_key = "action_type"
    elif '"arguments"' in body and re.search(
        r'"action"\s*:\s*"terminate"', body
    ):
        action_key = "action"
    if action_key is None:
        return None

    status_match = re.search(r'"status"\s*:\s*"([^"]*)"', body)
    status = (status_match.group(1) if status_match else "success").strip() or "success"
    arguments: dict[str, Any] = {action_key: "terminate", "status": status}

    answer_match = re.search(r'"answer"\s*:\s*"', body)
    if answer_match:
        answer_start = answer_match.end()
        tail = body[answer_start:]
        answer_end = re.search(r'"\s*,\s*"status"\s*:', tail)
        if answer_end is None:
            answer_end = re.search(r'"\s*}\s*}\s*$', tail)
        if answer_end is not None:
            answer = tail[: answer_end.start()]
            arguments["answer"] = answer.replace(r"\"", '"')

    return {"name": "mobile_use", "arguments": arguments}


MIN_TRAJ_PER_GROUP = 2


class QwenMobileUsePolicy(ResizeScreenshotMixin):


    NATIVE_ACTION_SPACE = ActionSpace.MOBILE_USE

    def __init__(
        self,
        chat_model: ChatModel,
        sampling_params: dict[str, Any],
        *,
        tools: list[dict] | None = None,
        original_image_width: int | None = None,
        original_image_height: int | None = None,
        resize_width: int | None = None,
        resize_height: int | None = None,
        resize_longest_edge: int | None = None,
        max_pixels: int | None = None,
        min_pixels: int | None = None,
        image_factor: int | None = None,
        system_prompt: str | None = None,
        image_retention: int = 3,
        messages_log_dir: Any = None,
    ) -> None:
        self.chat_model = chat_model
        self.sampling_params = sampling_params
        self.tools = tools
        self.original_image_width = original_image_width
        self.original_image_height = original_image_height
        # Screenshot size is not uniform across the portfolio, so a size sniffed
        # from one episode must not be reused for the next one.  Only an
        # explicitly configured size is pinned; otherwise it is re-derived from
        # every observation before the tool call is mapped back to pixels.
        self._image_size_pinned = (
            original_image_width is not None and original_image_height is not None
        )


        self._resizer = ImageResizer.from_config(
            resize_width=resize_width, resize_height=resize_height,
            resize_longest_edge=resize_longest_edge, max_pixels=max_pixels,
            min_pixels=min_pixels, image_factor=image_factor,
        )
        self.system_prompt = system_prompt
        self.image_retention = image_retention

        self._msg_log = MessagesLogger(messages_log_dir)

        self.timing: Any = None
        self.step_records: list[StepRecord] = []
        self._instruction: str = ""
        self._messages: list[dict[str, Any]] = []
        self.last_prediction: str | None = None
        self.last_step_log: dict[str, Any] | None = None
        self._last_generation_repair: dict[str, Any] | None = None


    def initialize(self, instruction: str) -> None:
        self.step_records.clear()
        self.chat_model.reset()
        assert instruction is not None, "instruction is None"
        self._instruction = instruction
        self._messages = []
        self.last_step_log = None
        self._last_generation_repair = None
        if self.system_prompt:
            self._messages.append({"role": "system", "content": self.system_prompt})

        if self._instruction:
            self._messages.append({
                "role": "user",
                "content": [{"type": "text", "text": self._instruction}],
            })
        self._msg_log.ensure_dir()

    def done(self) -> None:
        if not self.step_records:
            return
        actions = [r.action_dict.get("action_type", "?") for r in self.step_records]
        n_fmt_ok = sum(1 for r in self.step_records if r.format_reward >= 1.0)
        n = len(self.step_records)
        last_finish = self.step_records[-1].finish_reason
        logger.info(
            "[QwenMobileUsePolicy] traj done task=%s n_steps=%d fmt_ok=%d/%d "
            "last_finish=%s actions=[%s] last_response=%r",
            (self._instruction or "?")[:50], n, n_fmt_ok, n,
            last_finish, ", ".join(actions), (self.last_prediction or "")[:300],
        )


    def _prune_messages(self) -> list[dict[str, Any]]:
        return hide_history_images(self._messages, self.image_retention)

    def _tools_for_step(self) -> list[dict] | None:
        return self.tools if len(self.step_records) == 0 else None

    async def _repair_generation_result(
        self,
        *,
        result: GenerateResult,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
    ) -> tuple[GenerateResult, dict[str, Any] | None]:
        del messages, tools
        return result, None


    async def select_action(self, observation: dict[str, Any]) -> dict[str, Any]:

        tool_call_result = observation.get("tool_call")
        ask_user_response = observation.get("ask_user_response")

        if tool_call_result is not None:
            self._messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "Tool call result: "
                        f"{json.dumps(tool_call_result, ensure_ascii=False)}"
                    ),
                }],
            })
        elif ask_user_response is not None:
            self._messages.append({
                "role": "user",
                "content": [{"type": "text", "text": ask_user_response}],
            })

        suppress_image_turn = (
            observation.get("observation_kind") == "text"
            and not observation.get("image")
        )


        screenshot_b64 = observation.get("image", "") or ""
        if not screenshot_b64:
            if self.step_records:
                screenshot_b64 = self.step_records[-1].observation_screenshot
                if suppress_image_turn:
                    logger.info(
                        "[QwenMobileUsePolicy] text-only obs lacks image — retaining "
                        "prior screenshot for parser state"
                    )
                else:
                    logger.info(
                        "[QwenMobileUsePolicy] obs lacks image — reusing last screenshot"
                    )
            else:
                logger.warning("[QwenMobileUsePolicy] first obs has no image; emitting wait")
                return {"action_type": "wait"}


        if not self._image_size_pinned:
            size = _decode_png_size(screenshot_b64)
            if size is not None and size != (
                self.original_image_width,
                self.original_image_height,
            ):
                self.original_image_width, self.original_image_height = size
                logger.info("[QwenMobileUsePolicy] screenshot size now %dx%d", *size)


        screenshot_b64 = await self._resize_screenshot_for_model(screenshot_b64)


        if (
            tool_call_result is None
            and ask_user_response is None
            and not suppress_image_turn
        ):
            self._messages.append({
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": f"data:image/png;base64,{screenshot_b64}",
                }],
            })


        pruned = self._prune_messages()


        tools_for_step = self._tools_for_step()
        result: GenerateResult = await self.chat_model.generate(
            pruned,
            self.sampling_params,
            tools=tools_for_step,
            original_image_width=self.original_image_width,
            original_image_height=self.original_image_height,
            timing=self.timing,
        )
        result, self._last_generation_repair = await self._repair_generation_result(
            result=result,
            messages=pruned,
            tools=tools_for_step,
        )

        info = result.info
        action_dict = info.get("action_dict", {})
        is_done = info.get("is_done", False)
        format_reward = info.get("format_reward", 0.0)

        logging_enabled = self._msg_log.enabled
        rendered = elide_images(pruned)

        record = StepRecord(
            response_text=result.content,
            observation_screenshot=screenshot_b64,
            action_dict=action_dict,
            format_reward=format_reward,
            is_terminal=is_done,
            rendered_messages=rendered,
            finish_reason=result.finish_reason,
            action_text="",
            observation_in_prompt=(
                tool_call_result is None
                and ask_user_response is None
                and not suppress_image_turn
            ),
            observation_kind=str(
                observation.get("_prompt_observation_kind")
                or (
                    "image"
                    if (
                        tool_call_result is None
                        and ask_user_response is None
                        and not suppress_image_turn
                    )
                    else observation.get("observation_kind")
                    or "context_only"
                )
            ),
            observation_text=str(
                observation.get("_prompt_observation_text") or ""
            ),
        )
        self.step_records.append(record)
        self.last_prediction = result.content
        self.last_step_log = self._build_step_log(len(self.step_records) - 1, record)

        if logging_enabled:
            self._write_step_log(len(self.step_records) - 1, record)


        self._messages.append({
            "role": "assistant",
            "content": result.content,
        })

        return action_dict


    def _write_step_log(self, step_idx: int, record: StepRecord) -> None:
        payload = self._build_step_log(step_idx, record)
        self._msg_log.write_step(step_idx, payload)

    def _build_step_log(self, step_idx: int, record: StepRecord) -> dict[str, Any]:
        payload = {
            "step": step_idx,
            "system_prompt": self.system_prompt,
            "instruction": self._instruction,
            "rendered_messages": record.rendered_messages,
            "raw_response_text": record.response_text,
            "parsed_action_dict": record.action_dict,
            "format_reward": record.format_reward,
            "is_done": record.is_terminal,
            "finish_reason": record.finish_reason,
        }
        if self._last_generation_repair is not None:
            payload["generation_repair"] = self._last_generation_repair
        return payload

