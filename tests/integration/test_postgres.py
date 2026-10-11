"""Budget enforcement on Postgres, where no in-process lock exists: the conditional UPDATE
alone must stop concurrent overspend. Runs when GW_TEST_DATABASE_URL is set (CI does)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from llm_gateway.api import build
from llm_gateway.config import Settings
from llm_gateway.gateway import GatewayError
from llm_gateway.providers.adapters import FakeProvider
from llm_gateway.reliability import Breakers
from llm_gateway.store import Store, metadata
from tests.conftest import ask

URL = os.environ.get("GW_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not URL, reason="set GW_TEST_DATABASE_URL to a Postgres database")
def test_postgres_budget_holds_under_concurrency() -> None:
    store = Store(URL)
    metadata.drop_all(store.engine)
    metadata.create_all(store.engine)
    fakes = {n: FakeProvider(n) for n in ("anthropic", "openai", "google")}
    comps = build(replace(Settings(), database_url=URL), dict(fakes), store, Breakers())
    key = store.create_app("pg-sandbox", "sandbox")
    big = ask("cheap", "x " * 400, max_tokens=2000)

    def one(_: int) -> str:
        try:
            comps.gateway.chat(key, big)
            return "ok"
        except GatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(24) as pool:
        outcomes = list(pool.map(one, range(200)))
    spent, reserved = store.team_spend("sandbox")
    assert spent <= Decimal("0.05") and reserved == 0
    assert outcomes.count("budget") > 0
    assert sum(f.calls for f in fakes.values()) == outcomes.count("ok")
