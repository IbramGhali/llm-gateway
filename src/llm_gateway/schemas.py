"""The one normalised request and response every app uses, whatever the provider."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda d: str(d), return_type=str)]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class PromptRef(BaseModel):
    id: str
    version: int | None = Field(None, description="Omit for the latest version")
    variables: dict[str, str] = {}


class ChatRequest(BaseModel):
    model: str = Field(
        description="A route (smart, fast, cheap) or a model name from the catalogue"
    )
    messages: list[Message] = []
    system: str | None = None
    prompt: PromptRef | None = Field(None, description="Use a registered prompt template")
    max_tokens: int = Field(1024, ge=1, le=64000)
    metadata: dict[str, str] = {}


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: Money


class Attempt(BaseModel):
    model: str
    outcome: str  # ok | retryable_error | error | circuit_open | skipped
    detail: str | None = None
    latency_ms: float = 0.0


class ChatResponse(BaseModel):
    id: str
    model: str
    provider: str
    content: str
    usage: Usage
    latency_ms: float
    fallback_used: bool
    prompt: str | None = None
    attempts: list[Attempt]


class ErrorBody(BaseModel):
    id: str
    error: str
    detail: str
