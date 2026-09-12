"""The text-cleanup stage: filler removal, punctuation, formatting.

This is the slot where the LLM goes. It sits behind a one-method interface so
a local model or a hosted API can be dropped in without touching the pipeline,
and so the whole stage can be switched off when latency matters more than
polish.

The skeleton ships Passthrough: deterministic, sub-millisecond tidying only.
"""

from __future__ import annotations

import re
from typing import Protocol


class Cleaner(Protocol):
    name: str

    def clean(self, text: str) -> str: ...


class Passthrough:
    """Whitespace and capitalisation only. No model, no latency."""

    name = "passthrough"

    _SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
    _COLLAPSE = re.compile(r"[ \t]{2,}")

    def clean(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        text = self._COLLAPSE.sub(" ", text)
        text = self._SPACE_BEFORE_PUNCT.sub(r"\1", text)
        if text[0].islower():
            text = text[0].upper() + text[1:]
        return text


class LocalLlmCleaner:
    """Planned: a small instruct model on the spare VRAM.

    Must stream, so injection can begin on the first token rather than waiting
    for the full rewrite.
    """

    name = "local-llm"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError("local LLM cleanup is not wired up yet")

    def clean(self, text: str) -> str:  # pragma: no cover
        raise NotImplementedError


class ClaudeCleaner:
    """Planned: hosted cleanup and Command Mode."""

    name = "claude"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError("Claude cleanup is not wired up yet")

    def clean(self, text: str) -> str:  # pragma: no cover
        raise NotImplementedError


def build_cleaner(name: str) -> Cleaner:
    if name == "passthrough":
        return Passthrough()
    if name == "local-llm":
        return LocalLlmCleaner()
    if name == "claude":
        return ClaudeCleaner()
    raise ValueError(f"unknown cleaner: {name!r}")
