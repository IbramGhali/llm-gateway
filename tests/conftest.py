from __future__ import annotations

from dataclasses import replace

import pytest

from llm_gateway.api import Components, build
from llm_gateway.config import Settings
from llm_gateway.providers.adapters import FakeProvider
from llm_gateway.reliability import Breakers
from llm_gateway.schemas import ChatRequest, Message
from llm_gateway.store import Store


@pytest.fixture
def fakes() -> dict[str, FakeProvider]:
    return {n: FakeProvider(n) for n in ("anthropic", "openai", "google")}


@pytest.fixture
def comps(fakes: dict[str, FakeProvider]) -> Components:
    settings = replace(Settings(), database_url="sqlite://")
    return build(settings, dict(fakes), Store("sqlite://"), Breakers(threshold=3, cooldown=0.2))


def ask(model: str = "fast", text: str = "hello", max_tokens: int = 64) -> ChatRequest:
    return ChatRequest(
        model=model, messages=[Message(role="user", content=text)], max_tokens=max_tokens
    )
