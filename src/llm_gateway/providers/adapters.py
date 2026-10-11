"""Provider adapters: Anthropic (official SDK), OpenAI (official SDK), Google Gemini (REST),
and a deterministic fake with fault injection for offline runs and tests."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from llm_gateway.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderResult,
    classify_status,
)

# --- Anthropic -----------------------------------------------------------------------------


def anthropic_payload(req: ProviderRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "messages": [{"role": m.role, "content": m.content} for m in req.messages],
    }
    if req.system:
        payload["system"] = req.system
    return payload


def anthropic_result(body: dict[str, Any]) -> ProviderResult:
    if body.get("stop_reason") == "refusal":
        raise ProviderError("model declined the request", retryable=False)
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    usage = body.get("usage", {})
    return ProviderResult(
        text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=0)  # the gateway owns retries

    def complete(self, request: ProviderRequest, timeout: float) -> ProviderResult:
        a = self._anthropic
        try:
            response = self.client.with_options(timeout=timeout).messages.create(
                **anthropic_payload(request)
            )
        except a.APIStatusError as exc:
            raise ProviderError(
                f"anthropic {exc.status_code}", classify_status(exc.status_code)
            ) from exc
        except (a.APIConnectionError, a.APITimeoutError) as exc:
            raise ProviderError("anthropic connection error", retryable=True) from exc
        result: ProviderResult = anthropic_result(response.model_dump())
        return result


# --- OpenAI --------------------------------------------------------------------------------


def openai_payload(req: ProviderRequest) -> dict[str, Any]:
    messages = [{"role": "system", "content": req.system}] if req.system else []
    messages += [{"role": m.role, "content": m.content} for m in req.messages]
    return {"model": req.model, "messages": messages, "max_completion_tokens": req.max_tokens}


def openai_result(body: dict[str, Any]) -> ProviderResult:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise ProviderError("model declined the request", retryable=False)
    usage = body.get("usage") or {}
    return ProviderResult(
        message.get("content") or "",
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
    )


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        import openai

        self._openai = openai
        self.client = openai.OpenAI(max_retries=0)

    def complete(self, request: ProviderRequest, timeout: float) -> ProviderResult:
        o = self._openai
        try:
            response = self.client.with_options(timeout=timeout).chat.completions.create(
                **openai_payload(request)
            )
        except o.APIStatusError as exc:
            raise ProviderError(
                f"openai {exc.status_code}", classify_status(exc.status_code)
            ) from exc
        except (o.APIConnectionError, o.APITimeoutError) as exc:
            raise ProviderError("openai connection error", retryable=True) from exc
        result: ProviderResult = openai_result(response.model_dump())
        return result


# --- Google Gemini (REST) ------------------------------------------------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def google_payload(req: ProviderRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contents": [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in req.messages
        ],
        "generationConfig": {"maxOutputTokens": req.max_tokens},
    }
    if req.system:
        payload["systemInstruction"] = {"parts": [{"text": req.system}]}
    return payload


def google_result(body: dict[str, Any]) -> ProviderResult:
    candidates = body.get("candidates") or []
    if not candidates:
        raise ProviderError("no candidates returned", retryable=False)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    usage = body.get("usageMetadata") or {}
    return ProviderResult(
        "".join(p.get("text", "") for p in parts),
        int(usage.get("promptTokenCount", 0)),
        int(usage.get("candidatesTokenCount", 0)),
    )


class GoogleProvider:
    name = "google"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client()

    def complete(self, request: ProviderRequest, timeout: float) -> ProviderResult:
        try:
            response = self.client.post(
                GEMINI_URL.format(model=request.model),
                headers={"x-goog-api-key": self.api_key},
                json=google_payload(request),
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            raise ProviderError("google connection error", retryable=True) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"google {response.status_code}", classify_status(response.status_code)
            )
        return google_result(response.json())


# --- Fake with fault injection -------------------------------------------------------------


@dataclass
class Faults:
    error_rate: float = 0.0  # share of calls that fail with a retryable 503
    fatal: bool = False  # every call fails with a non-retryable 400
    outage: bool = False  # every call fails with a retryable 503
    latency_ms: float = 0.0


@dataclass
class FakeProvider:
    """Deterministic stand-in. Output and token counts depend only on the input; faults are
    decided by a counter, so a test can predict exactly which calls fail."""

    name: str
    faults: Faults = field(default_factory=Faults)
    calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def complete(self, request: ProviderRequest, timeout: float) -> ProviderResult:
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.faults.latency_ms:
            time.sleep(self.faults.latency_ms / 1000)
        if self.faults.fatal:
            raise ProviderError(f"{self.name} 400", retryable=False)
        if self.faults.outage:
            raise ProviderError(f"{self.name} 503", retryable=True)
        if self.faults.error_rate and (n * 0.6180339887) % 1 < self.faults.error_rate:
            raise ProviderError(f"{self.name} 503", retryable=True)
        prompt = (request.system or "") + "".join(m.content for m in request.messages)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        last = request.messages[-1].content if request.messages else ""
        content = f"[{self.name}:{request.model}] reply {digest} to: {last[:60]}"
        return ProviderResult(
            content, max(1, len(prompt) // 4), min(request.max_tokens, 20 + len(last) // 8)
        )
