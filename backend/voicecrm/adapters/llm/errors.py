"""Errors raised by the LLM adapters. Messages never contain prompt, transcript or model-output text."""
from __future__ import annotations


class LlmError(RuntimeError):
    """Base class for LLM adapter failures."""


class LlmOutputError(LlmError):
    """The model answered but not with usable JSON for the requested schema (after the one repair retry)."""

    def __init__(self, message: str, *, finish_reason: str | None = None, attempts: int = 1) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason
        self.attempts = attempts


class LlmRequestError(LlmError):
    """The model service rejected the request, refused access, or stayed unavailable after retries."""

    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None, retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
