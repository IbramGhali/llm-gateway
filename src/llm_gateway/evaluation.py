"""Evaluation suites: gateway overhead under load, fallback correctness under injected
failures, budget enforcement, and PII leakage into logs."""

from __future__ import annotations

import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from llm_gateway.api import Components, build
from llm_gateway.config import Settings
from llm_gateway.gateway import GatewayError
from llm_gateway.providers.adapters import FakeProvider, Faults
from llm_gateway.redaction import redact
from llm_gateway.reliability import Breakers
from llm_gateway.schemas import ChatRequest, Message
from llm_gateway.store import Store


def _fresh(
    settings: Settings, providers: dict[str, FakeProvider] | None = None, cooldown: float = 10.0
) -> tuple[Components, dict[str, FakeProvider]]:
    fakes = providers or {n: FakeProvider(n) for n in ("anthropic", "openai", "google")}
    comps = build(
        replace(settings, database_url="sqlite://"),
        dict(fakes),
        Store("sqlite://"),
        Breakers(threshold=3, cooldown=cooldown),
    )
    return comps, fakes


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))], 3)


# --- overhead -------------------------------------------------------------------------------


def overhead(settings: Settings, requests: int = 2000, concurrency: int = 16) -> dict[str, Any]:
    """Gateway time excluding the provider, with a zero-latency fake provider: once one request
    at a time (the per-request cost) and once under concurrency (includes queueing)."""
    serial = _overhead_run(settings, requests // 4, 1)
    concurrent = _overhead_run(settings, requests, concurrency)
    return {"serial": serial, "concurrent": concurrent}


def _overhead_run(settings: Settings, requests: int, concurrency: int) -> dict[str, Any]:
    comps, _ = _fresh(settings)
    key = comps.store.create_app("load-test", "analytics")
    body = ChatRequest(
        model="fast",
        messages=[Message(role="user", content="Summarise: " + "lorem ipsum " * 40)],
        max_tokens=64,
    )
    started = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        list(pool.map(lambda _: comps.gateway.chat(key, body), range(requests)))
    wall = time.perf_counter() - started
    rows = comps.store.ledger(limit=requests)
    over = [r["overhead_ms"] for r in rows]
    return {
        "requests": requests,
        "concurrency": concurrency,
        "overhead_ms_p50": _pct(over, 0.5),
        "overhead_ms_p95": _pct(over, 0.95),
        "throughput_rps": round(requests / wall),
    }


# --- fallbacks ------------------------------------------------------------------------------


@dataclass
class FallbackCase:
    name: str
    faults: dict[str, Faults]
    expect_model: str | None  # None means the request must fail cleanly (502)


FALLBACK_CASES = [
    FallbackCase("primary healthy", {}, "claude-opus"),
    FallbackCase("primary outage", {"anthropic": Faults(outage=True)}, "demo-large"),
    FallbackCase(
        "primary rejects request (non-retryable)", {"anthropic": Faults(fatal=True)}, "demo-large"
    ),
    FallbackCase(
        "first two providers down",
        {"anthropic": Faults(outage=True), "openai": Faults(outage=True)},
        None,
    ),
    FallbackCase(
        "primary flaky (40% errors)", {"anthropic": Faults(error_rate=0.4)}, "claude-opus"
    ),
]


def fallbacks(settings: Settings, per_case: int = 50) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    for case in FALLBACK_CASES:
        fakes = {
            n: FakeProvider(n, case.faults.get(n, Faults()))
            for n in ("anthropic", "openai", "google")
        }
        comps, _ = _fresh(settings, fakes)
        key = comps.store.create_app("fb", "analytics")
        served: dict[str, int] = {}
        failed = 0
        for i in range(per_case):
            try:
                r = comps.gateway.chat(
                    key,
                    ChatRequest(
                        model="smart",
                        messages=[Message(role="user", content=f"q{i}")],
                        max_tokens=32,
                    ),
                )
                served[r.model] = served.get(r.model, 0) + 1
            except GatewayError as exc:
                assert exc.status == 502
                failed += 1
        if case.expect_model is None:
            correct = failed  # claude-sonnet is anthropic too, so the whole route is down
        elif case.name.startswith("primary flaky"):
            correct = per_case - failed  # any successful answer within the route is correct
        else:
            correct = served.get(case.expect_model, 0)
        results[case.name] = {
            "requests": per_case,
            "served_by": served,
            "failed": failed,
            "correct": correct,
            "breaker_trips": comps.gateway.breakers.trips(),
            "primary_calls": fakes["anthropic"].calls,
        }
    total = sum(r["requests"] for r in results.values())
    return {
        "cases": results,
        "correct_rate": round(sum(r["correct"] for r in results.values()) / total, 4),
    }


# --- budgets --------------------------------------------------------------------------------


def budgets(settings: Settings, requests: int = 200, concurrency: int = 16) -> dict[str, Any]:
    """Fire many concurrent requests at a team with a tiny budget. Spend must never exceed it,
    and every refusal must happen before a provider is called."""
    comps, fakes = _fresh(settings)
    key = comps.store.create_app("sandbox-app", "sandbox")
    budget = comps.config.teams["sandbox"].monthly_budget
    body = ChatRequest(
        model="cheap", messages=[Message(role="user", content="x " * 400)], max_tokens=2000
    )

    def one(_: int) -> str:
        try:
            comps.gateway.chat(key, body)
            return "ok"
        except GatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(concurrency) as pool:
        outcomes = list(pool.map(one, range(requests)))
    spent, reserved = comps.store.team_spend("sandbox")
    provider_calls = sum(f.calls for f in fakes.values())
    ok = outcomes.count("ok")
    return {
        "requests": requests,
        "served": ok,
        "refused_budget": outcomes.count("budget"),
        "budget_usd": str(budget),
        "spent_usd": str(spent),
        "reserved_after": str(reserved),
        "within_budget": spent <= budget,
        "provider_calls": provider_calls,
        "refused_without_provider_call": provider_calls == ok,
    }


# --- PII ------------------------------------------------------------------------------------

FIRST = ["Avery", "Blake", "Charlie", "Dana", "Eden", "Frankie", "Harper", "Indy"]
DOMAINS = ["example.com", "mail.example.org", "corp.example.net"]


def _card(rng: random.Random) -> str:
    digits = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    digits.append((10 - total % 10) % 10)
    s = "".join(map(str, digits))
    return " ".join(s[i : i + 4] for i in range(0, 16, 4))


def pii_dataset(n: int = 200, seed: int = 5) -> list[tuple[str, list[str]]]:
    """Synthetic prompts with planted PII. Returns (text, planted values)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        name = rng.choice(FIRST)
        kinds = rng.sample(["email", "phone", "card", "tax", "ip", "iban", "mobile"], k=2)
        values = []
        for kind in kinds:
            if kind == "email":
                values.append(f"{name.lower()}.{i}@{rng.choice(DOMAINS)}")
            elif kind == "phone":
                values.append(f"+61 3 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}")
            elif kind == "mobile":
                values.append(
                    f"04{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}"
                )
            elif kind == "card":
                values.append(_card(rng))
            elif kind == "tax":
                values.append(
                    f"{rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(100, 999)}"
                )
            elif kind == "ip":
                values.append(".".join(str(rng.randint(1, 254)) for _ in range(4)))
            else:
                values.append(
                    f"GB{rng.randint(10, 99)} NWBK {rng.randint(1000, 9999)} "
                    f"{rng.randint(1000, 9999)} {rng.randint(10, 99)}"
                )
        text = (
            f"Customer {name} wrote in about ticket {i}. Contact: {values[0]}. "
            f"They also gave {values[1]} and asked for a refund of ${rng.randint(5, 300)}."
        )
        rows.append((text, values))
    return rows


def pii(settings: Settings) -> dict[str, Any]:
    comps, _ = _fresh(settings)
    key = comps.store.create_app("pii-app", "analytics")  # analytics does NOT redact prompts
    rows = pii_dataset()
    detected = 0
    for text, values in rows:
        comps.gateway.chat(
            key,
            ChatRequest(model="fast", messages=[Message(role="user", content=text)], max_tokens=64),
        )
        scrubbed = redact(text).text
        detected += all(v not in scrubbed for v in values)
    logged = comps.store.all_logged_text()
    leaks = sum(1 for _, values in rows for v in values if v in logged)
    false_positive_amounts = sum(
        1 for text, _ in rows if "$" in text and "$" not in redact(text).text
    )
    return {
        "prompts": len(rows),
        "planted_values": sum(len(v) for _, v in rows),
        "prompts_fully_redacted": detected,
        "leaks_into_logs": leaks,
        "amounts_wrongly_removed": false_positive_amounts,
    }


def run_all(settings: Settings) -> dict[str, Any]:
    return {
        "overhead": overhead(settings),
        "fallbacks": fallbacks(settings),
        "budgets": budgets(settings),
        "pii": pii(settings),
    }


def gate(results: dict[str, Any], max_p95_ms: float = 25.0) -> list[str]:
    problems = []
    if results["pii"]["leaks_into_logs"]:
        problems.append(f"{results['pii']['leaks_into_logs']} PII values leaked into logs")
    if not results["budgets"]["within_budget"]:
        problems.append("spend exceeded the budget")
    if not results["budgets"]["refused_without_provider_call"]:
        problems.append("a refused request reached a provider")
    if results["fallbacks"]["correct_rate"] < 1.0:
        problems.append(f"fallback correctness {results['fallbacks']['correct_rate']}")
    serial = results["overhead"]["serial"]
    if serial["overhead_ms_p95"] > max_p95_ms:
        problems.append(f"per-request overhead p95 {serial['overhead_ms_p95']} ms")
    return problems


_ = Decimal, statistics  # used by callers formatting results
