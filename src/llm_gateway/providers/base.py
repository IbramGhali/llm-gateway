"""Provider interface. Each adapter has two pure functions (normalised request to provider
payload, provider response to normalised result) tested against recorded fixtures, and a
thin transport that sends the payload."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from llm_gateway.schemas import Message


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str | None
    messages: list[Message]
    max_tokens: int


@dataclass(frozen=True)
class ProviderResult:
    content: str
    input_tokens: int
    output_tokens: int


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    name: str

    def complete(self, request: ProviderRequest, timeout: float) -> ProviderResult: ...


def classify_status(status: int) -> bool:
    """Retryable statuses: rate limits, timeouts and server errors. Everything else is not."""
    return status in (408, 409, 429) or status >= 500
