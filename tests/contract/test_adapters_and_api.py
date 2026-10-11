"""Adapters are checked against recorded provider responses; the HTTP API against its
OpenAPI contract and error shapes."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.api import Components, create_app
from llm_gateway.config import PROJECT_ROOT
from llm_gateway.providers.adapters import (
    GoogleProvider,
    anthropic_payload,
    anthropic_result,
    google_payload,
    google_result,
    openai_payload,
    openai_result,
)
from llm_gateway.providers.base import ProviderError, ProviderRequest
from llm_gateway.schemas import Message

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REQ = ProviderRequest(
    "m-1",
    "Be brief.",
    [
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello"),
        Message(role="user", content="Summarise X"),
    ],
    128,
)


def _fixture(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads((FIXTURES / name).read_text("utf-8"))
    return data


def test_anthropic_translation() -> None:
    payload = anthropic_payload(REQ)
    assert payload["system"] == "Be brief." and payload["max_tokens"] == 128
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    result = anthropic_result(_fixture("anthropic_response.json"))
    assert (result.input_tokens, result.output_tokens) == (42, 14)
    assert result.content.startswith("The order arrived damaged")
    with pytest.raises(ProviderError):
        anthropic_result({"stop_reason": "refusal", "content": []})


def test_openai_translation() -> None:
    payload = openai_payload(REQ)
    assert payload["messages"][0] == {"role": "system", "content": "Be brief."}
    assert payload["max_completion_tokens"] == 128
    result = openai_result(_fixture("openai_response.json"))
    assert (result.content, result.input_tokens, result.output_tokens) == ("billing", 51, 2)


def test_google_translation_and_transport() -> None:
    payload = google_payload(REQ)
    assert payload["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert [c["role"] for c in payload["contents"]] == ["user", "model", "user"]
    result = google_result(_fixture("google_response.json"))
    assert (result.input_tokens, result.output_tokens) == (37, 11)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json=_fixture("google_response.json"))
        return httpx.Response(503)

    provider = GoogleProvider("test-key", httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.complete(REQ, 5).output_tokens == 11
    assert seen[0].url.path.endswith("/models/m-1:generateContent")
    assert seen[0].headers["x-goog-api-key"] == "test-key"
    with pytest.raises(ProviderError) as err:
        provider.complete(REQ, 5)
    assert err.value.retryable


def test_api_auth_policy_and_error_shape(comps: Components) -> None:
    client = TestClient(create_app(comps))
    key = comps.store.create_app("app-1", "support")
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat", json=body).status_code == 401
    ok = client.post("/v1/chat", json=body, headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200 and ok.headers["x-request-id"] == ok.json()["id"]
    assert ok.json()["usage"]["cost_usd"]
    denied = client.post("/v1/chat", json={**body, "model": "smart"}, headers={"X-API-Key": key})
    assert denied.status_code == 403 and set(denied.json()) == {"id", "error", "detail"}
    models = client.get("/v1/models", headers={"X-API-Key": key}).json()
    assert [m["name"] for m in models] == ["fast", "cheap"]


def test_prompt_reference_is_recorded(comps: Components) -> None:
    client = TestClient(create_app(comps))
    key = comps.store.create_app("app-2", "support")
    r = client.post(
        "/v1/chat",
        headers={"X-API-Key": key},
        json={
            "model": "fast",
            "prompt": {
                "id": "summarise-ticket",
                "version": 1,
                "variables": {"ticket": "Parcel lost."},
            },
        },
    ).json()
    assert r["prompt"] == "summarise-ticket@v1"
    assert comps.store.ledger(1)[0]["prompt"] == "summarise-ticket@v1"
    bad = client.post(
        "/v1/chat",
        headers={"X-API-Key": key},
        json={"model": "fast", "prompt": {"id": "summarise-ticket", "variables": {}}},
    )
    assert bad.status_code == 400


def test_admin_pages(comps: Components) -> None:
    client = TestClient(create_app(comps))
    key = comps.store.create_app("app-3", "analytics")
    client.post(
        "/v1/chat",
        headers={"X-API-Key": key},
        json={"model": "smart", "messages": [{"role": "user", "content": "x"}]},
    )
    usage = client.get("/admin/usage.json").json()
    assert usage["teams"]["analytics"]["spent"] != "0"
    assert "Gateway usage" in client.get("/admin").text


def test_committed_openapi_matches_app(comps: Components) -> None:
    committed = json.loads((PROJECT_ROOT / "openapi.json").read_text("utf-8"))
    assert committed == create_app(comps).openapi(), "run `uv run gw openapi`"
