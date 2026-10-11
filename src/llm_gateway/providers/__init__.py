"""Provider registry. In fake mode every provider is a deterministic stand-in, so routing,
fallbacks, pricing and budgets run exactly as in production, offline."""

from __future__ import annotations

import os

from llm_gateway.providers.adapters import (
    AnthropicProvider,
    FakeProvider,
    GoogleProvider,
    OpenAIProvider,
)
from llm_gateway.providers.base import Provider

PROVIDERS = ("anthropic", "openai", "google")


def make_providers(mode: str) -> dict[str, Provider]:
    if mode == "fake":
        return {name: FakeProvider(name) for name in PROVIDERS}
    if mode != "live":
        raise ValueError("GW_PROVIDER_MODE must be fake or live")
    providers: dict[str, Provider] = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers["anthropic"] = AnthropicProvider()
    if os.environ.get("OPENAI_API_KEY"):
        providers["openai"] = OpenAIProvider()
    if key := os.environ.get("GOOGLE_API_KEY"):
        providers["google"] = GoogleProvider(key)
    return providers
