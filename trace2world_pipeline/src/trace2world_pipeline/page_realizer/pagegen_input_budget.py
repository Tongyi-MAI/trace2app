
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, List


DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET = 40000
DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY = 2200
DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY = 500
PAGEGEN_TEXT_TOKENIZER = "o200k_base"


@dataclass(frozen=True)
class PageGenInputBudgetEstimate:
    text_tokens: int
    image_count: int
    image_tokens: int
    overhead_tokens: int
    estimated_input_tokens: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text_tokens": self.text_tokens,
            "text_tokenizer": PAGEGEN_TEXT_TOKENIZER,
            "image_count": self.image_count,
            "image_tokens": self.image_tokens,
            "overhead_tokens": self.overhead_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
        }


class PageGenInputBudgetError(RuntimeError):
    ...


def normalize_pagegen_input_token_budget(value: Any) -> int:
    try:
        configured = int(value)
    except (TypeError, ValueError):
        configured = DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET
    return max(DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET, configured)


def _iter_message_blocks(messages: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            yield {"type": "input_text", "text": content}
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield block


@lru_cache(maxsize=1)
def _pagegen_text_encoding():
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError(
            "PageGen token preflight requires tiktoken; character-count fallback is forbidden"
        ) from exc
    return tiktoken.get_encoding(PAGEGEN_TEXT_TOKENIZER)


def count_pagegen_text_tokens(text: str) -> int:
    return len(
        _pagegen_text_encoding().encode(
            str(text or ""),
            disallowed_special=(),
        )
    )


def estimate_pagegen_message_input_tokens(
    messages: List[Dict[str, Any]],
    *,
    image_token_proxy: int = DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
    overhead_token_proxy: int = DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
) -> PageGenInputBudgetEstimate:
    text_tokens = 0
    image_count = 0
    for block in _iter_message_blocks(messages):
        block_type = str(block.get("type") or "")
        if block_type == "input_text":
            text_tokens += count_pagegen_text_tokens(str(block.get("text") or ""))
        elif block_type == "input_image":
            image_count += 1
    image_tokens = image_count * max(0, int(image_token_proxy or 0))
    overhead_tokens = max(0, int(overhead_token_proxy or 0))
    return PageGenInputBudgetEstimate(
        text_tokens=text_tokens,
        image_count=image_count,
        image_tokens=image_tokens,
        overhead_tokens=overhead_tokens,
        estimated_input_tokens=text_tokens + image_tokens + overhead_tokens,
    )


def provider_prompt_tokens(usage_info: Any) -> int:
    if isinstance(usage_info, dict):
        return int(usage_info.get("prompt_tokens") or usage_info.get("input_tokens") or 0)
    return int(
        getattr(usage_info, "prompt_tokens", 0)
        or getattr(usage_info, "input_tokens", 0)
        or 0
    )


def enforce_pagegen_preflight_budget(
    messages: List[Dict[str, Any]],
    *,
    stage: str,
    input_token_budget: int = DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
    image_token_proxy: int = DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
    overhead_token_proxy: int = DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
) -> PageGenInputBudgetEstimate:
    estimate = estimate_pagegen_message_input_tokens(
        messages,
        image_token_proxy=image_token_proxy,
        overhead_token_proxy=overhead_token_proxy,
    )
    budget = normalize_pagegen_input_token_budget(input_token_budget)
    if estimate.estimated_input_tokens > budget:
        raise PageGenInputBudgetError(
            f"{stage} estimated_input_tokens={estimate.estimated_input_tokens} "
            f"exceeds hard input budget {budget}; "
            f"text_tokens={estimate.text_tokens}, images={estimate.image_count}, "
            f"text_tokenizer={PAGEGEN_TEXT_TOKENIZER}, image_token_proxy={image_token_proxy}. "
            "Partition the authoritative PageGen input without truncating semantic fields before retrying."
        )
    return estimate


def enforce_pagegen_provider_budget(
    usage_info: Any,
    *,
    stage: str,
    input_token_budget: int = DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
) -> int:
    prompt_tokens = provider_prompt_tokens(usage_info)
    budget = normalize_pagegen_input_token_budget(input_token_budget)
    if prompt_tokens > budget:
        raise PageGenInputBudgetError(
            f"{stage} provider_prompt_tokens={prompt_tokens} exceeds hard input budget {budget}; "
            "the returned candidate is invalid and must not be admitted."
        )
    return prompt_tokens
