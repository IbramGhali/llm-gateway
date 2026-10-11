"""One real request through the gateway to Anthropic. Excluded by default; costs money.

Run with: GW_LIVE_TESTS=1 uv run --extra anthropic pytest -m live
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from llm_gateway.api import build
from llm_gateway.config import Settings
from llm_gateway.providers.adapters import AnthropicProvider
from llm_gateway.store import Store
from tests.conftest import ask


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GW_LIVE_TESTS") != "1", reason="set GW_LIVE_TESTS=1")
def test_live_anthropic_request_is_costed_and_logged() -> None:
    comps = build(
        replace(Settings(), database_url="sqlite://"),
        {"anthropic": AnthropicProvider()},
        Store("sqlite://"),
    )
    key = comps.store.create_app("live", "support")
    response = comps.gateway.chat(key, ask("fast", "Reply with the word ok."))
    assert response.model == "claude-haiku" and response.usage.output_tokens > 0
    assert comps.store.ledger(1)[0]["status"] == "ok"
