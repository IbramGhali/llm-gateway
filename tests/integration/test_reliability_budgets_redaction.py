"""Fault injection, concurrent budget enforcement, and redaction end to end."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from llm_gateway.api import Components
from llm_gateway.gateway import GatewayError
from llm_gateway.providers.adapters import FakeProvider, Faults
from tests.conftest import ask


def test_outage_falls_back_and_breaker_stops_calling_the_dead_provider(
    comps: Components, fakes: dict[str, FakeProvider]
) -> None:
    fakes["anthropic"].faults = Faults(outage=True)
    key = comps.store.create_app("a", "analytics")
    served = [comps.gateway.chat(key, ask("smart")).model for _ in range(20)]
    assert set(served) == {"demo-large"}
    assert fakes["anthropic"].calls == 3  # breaker opened after 3 retryable failures
    assert comps.gateway.breakers.trips() == {"claude-opus": 1}


def test_breaker_half_opens_and_recovers(comps: Components, fakes: dict[str, FakeProvider]) -> None:
    fakes["anthropic"].faults = Faults(outage=True)
    key = comps.store.create_app("a", "analytics")
    for _ in range(5):
        comps.gateway.chat(key, ask("smart"))
    fakes["anthropic"].faults = Faults()  # provider recovers
    time.sleep(0.5)  # cool-down in the test fixture is 0.2 s
    assert comps.gateway.chat(key, ask("smart")).model == "claude-opus"


def test_non_retryable_error_is_not_retried_on_the_same_model(
    comps: Components, fakes: dict[str, FakeProvider]
) -> None:
    fakes["anthropic"].faults = Faults(fatal=True)
    key = comps.store.create_app("a", "analytics")
    response = comps.gateway.chat(key, ask("smart"))
    assert response.model == "demo-large" and fakes["anthropic"].calls == 1
    assert [a.outcome for a in response.attempts] == ["error", "ok"]


def test_whole_route_down_fails_cleanly_and_bills_nothing(
    comps: Components, fakes: dict[str, FakeProvider]
) -> None:
    fakes["anthropic"].faults = Faults(outage=True)
    fakes["openai"].faults = Faults(outage=True)
    key = comps.store.create_app("a", "analytics")
    with pytest.raises(GatewayError) as err:
        comps.gateway.chat(key, ask("smart"))
    assert err.value.status == 502
    assert comps.store.team_spend("analytics") == (Decimal(0), Decimal(0))
    assert comps.store.ledger(1)[0]["status"] == "failed"


def test_concurrent_requests_never_exceed_the_budget(
    comps: Components, fakes: dict[str, FakeProvider]
) -> None:
    key = comps.store.create_app("s", "sandbox")  # $0.05 budget
    big = ask("cheap", "x " * 400, max_tokens=2000)

    def one(_: int) -> str:
        try:
            comps.gateway.chat(key, big)
            return "ok"
        except GatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(16) as pool:
        outcomes = list(pool.map(one, range(150)))
    spent, reserved = comps.store.team_spend("sandbox")
    assert outcomes.count("budget") > 0 and outcomes.count("ok") > 0
    assert spent <= Decimal("0.05") and reserved == 0
    assert sum(f.calls for f in fakes.values()) == outcomes.count("ok")


def test_policy_refusals_are_logged_without_calling_a_provider(
    comps: Components, fakes: dict[str, FakeProvider]
) -> None:
    key = comps.store.create_app("s", "support")
    with pytest.raises(GatewayError) as err:
        comps.gateway.chat(key, ask("smart"))
    assert err.value.status == 403 and sum(f.calls for f in fakes.values()) == 0
    assert comps.store.ledger(1)[0]["status"] == "refused_policy"


def test_revoked_keys_stop_working(comps: Components) -> None:
    key = comps.store.create_app("r", "support")
    comps.gateway.chat(key, ask())
    comps.store.revoke("r")
    with pytest.raises(GatewayError) as err:
        comps.gateway.chat(key, ask())
    assert err.value.status == 401


PII = "Refund for jo.smith@example.com, card 4111 1111 1111 1111, mobile 0412 345 678."


def test_pii_never_reaches_the_logs(comps: Components) -> None:
    key = comps.store.create_app("p", "analytics")  # this team does not redact prompts
    comps.gateway.chat(key, ask("fast", PII))
    logged = comps.store.all_logged_text()
    for value in ("jo.smith@example.com", "4111 1111 1111 1111", "0412 345 678"):
        assert value not in logged
    assert "[EMAIL]" in logged


def test_redacting_teams_never_send_pii_to_the_provider(comps: Components) -> None:
    key = comps.store.create_app("p", "support")  # support redacts prompts
    response = comps.gateway.chat(key, ask("fast", PII))
    assert "jo.smith@example.com" not in response.content  # the fake echoes what it received
    assert "[EMAIL]" in response.content
