"""The evaluation suites with the same hard targets as `gw eval --gate`."""

from __future__ import annotations

from typing import Any

import pytest

from llm_gateway import evaluation
from llm_gateway.config import Settings


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    settings = Settings()
    return {
        "overhead": evaluation.overhead(settings, requests=400, concurrency=8),
        "fallbacks": evaluation.fallbacks(settings, per_case=20),
        "budgets": evaluation.budgets(settings, requests=120),
        "pii": evaluation.pii(settings),
    }


def test_correctness_gates_pass(results: dict[str, Any]) -> None:
    # Latency depends on the machine, so it is gated by `gw eval --gate`, not by unit tests.
    assert evaluation.gate(results, max_p95_ms=float("inf")) == []


def test_no_pii_leaks(results: dict[str, Any]) -> None:
    assert results["pii"]["leaks_into_logs"] == 0
    assert results["pii"]["prompts_fully_redacted"] == results["pii"]["prompts"]


def test_budget_holds_under_concurrency(results: dict[str, Any]) -> None:
    b = results["budgets"]
    assert b["within_budget"] and b["refused_budget"] > 0 and b["refused_without_provider_call"]


def test_every_fallback_case_is_correct(results: dict[str, Any]) -> None:
    assert results["fallbacks"]["correct_rate"] == 1.0
