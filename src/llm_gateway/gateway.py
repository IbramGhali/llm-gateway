"""The gateway pipeline:

app key -> policy (team may use this route or model) -> prompt registry -> redaction
-> budget reservation -> route with fallbacks, retries and circuit breakers -> usage ledger.

Nothing raw is ever persisted: the ledger keeps a redacted, truncated preview and the counts
of PII found. Requests that would exceed a team's budget are refused before any provider is
called, so nothing is billed silently.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from llm_gateway.config import GatewayConfig, Settings
from llm_gateway.prompts import PromptRegistry
from llm_gateway.providers.base import Provider, ProviderError, ProviderRequest
from llm_gateway.redaction import redact
from llm_gateway.reliability import Breakers
from llm_gateway.schemas import Attempt, ChatRequest, ChatResponse, Message, Usage
from llm_gateway.store import Store

MICRO = Decimal("0.000001")
PREVIEW_CHARS = 160


class GatewayError(Exception):
    def __init__(self, status: int, code: str, detail: str, request_id: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail, self.request_id = status, code, detail, request_id


def cost(input_tokens: int, output_tokens: int, price_in: Decimal, price_out: Decimal) -> Decimal:
    raw = (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / 1_000_000
    return raw.quantize(MICRO, rounding=ROUND_HALF_UP)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4 + 8)


@dataclass
class Gateway:
    settings: Settings
    config: GatewayConfig
    store: Store
    providers: dict[str, Provider]
    prompts: PromptRegistry
    breakers: Breakers

    def chat(self, api_key: str | None, req: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        app = self.store.app_for_key(api_key) if api_key else None
        if app is None:
            raise GatewayError(401, "unauthorised", "missing or invalid API key", request_id)
        team_id = app["team"]
        team = self.config.teams.get(team_id)

        def refuse(
            status: int,
            code: str,
            detail: str,
            preview: str = "",
            pii: dict[str, int] | None = None,
        ) -> GatewayError:
            self.store.record(
                id=request_id,
                app=app["id"],
                team=team_id,
                requested=req.model,
                status=f"refused_{code}",
                error=detail,
                attempts=[],
                redacted_preview=preview,
                pii_found=pii or {},
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            return GatewayError(status, code, detail, request_id)

        if team is None or req.model not in team.allow:
            raise refuse(403, "policy", f"team {team_id} may not use {req.model}")
        chain = self.config.chain(req.model)

        # Build the prompt (registry or raw messages), then redact.
        system, messages, prompt_ref = req.system, list(req.messages), None
        if req.prompt:
            try:
                version = self.prompts.get(req.prompt.id, req.prompt.version)
                text = version.render(req.prompt.variables)
            except (LookupError, ValueError) as exc:
                raise refuse(400, "prompt", str(exc)) from exc
            system, messages, prompt_ref = (
                version.system,
                [*messages, Message(role="user", content=text)],
                version.ref,
            )
        if not messages:
            raise refuse(400, "empty", "no messages and no prompt")
        full_text = (system or "") + "\n" + "\n".join(m.content for m in messages)
        scrubbed = redact(full_text)
        preview = scrubbed.text[:PREVIEW_CHARS]
        if team.redact_prompts:
            system = redact(system).text if system else None
            messages = [Message(role=m.role, content=redact(m.content).text) for m in messages]

        # Reserve the worst case for the most expensive model in the chain.
        est_in = estimate_tokens(full_text)
        worst = max(
            cost(
                est_in,
                req.max_tokens,
                self.config.models[n].input or Decimal(0),
                self.config.models[n].output or Decimal(0),
            )
            for n in chain
        )
        if not self.store.reserve(team_id, worst, team.monthly_budget):
            spent = self.store.team_spend(team_id)[0]
            raise refuse(
                402,
                "budget",
                f"monthly budget {team.monthly_budget} USD would be exceeded "
                f"(spent {spent.quantize(MICRO)}, this request up to {worst})",
                preview,
                scrubbed.found,
            )

        attempts: list[Attempt] = []
        provider_ms = 0.0
        try:
            for position, name in enumerate(chain):
                entry = self.config.models[name]
                provider = self.providers.get(entry.provider)
                if provider is None:
                    attempts.append(
                        Attempt(model=name, outcome="skipped", detail="provider not configured")
                    )
                    continue
                if not self.breakers.allow(name):
                    attempts.append(Attempt(model=name, outcome="circuit_open"))
                    continue
                for _ in range(self.settings.max_attempts_per_model):
                    t0 = time.perf_counter()
                    try:
                        result = provider.complete(
                            ProviderRequest(entry.model, system, messages, req.max_tokens),
                            self.settings.request_timeout,
                        )
                    except ProviderError as exc:
                        ms = (time.perf_counter() - t0) * 1000
                        provider_ms += ms
                        self.breakers.record(name, ok=not exc.retryable)
                        attempts.append(
                            Attempt(
                                model=name,
                                latency_ms=round(ms, 2),
                                outcome="retryable_error" if exc.retryable else "error",
                                detail=str(exc),
                            )
                        )
                        if not exc.retryable:
                            break  # bad request for this model: do not retry it
                        if not self.breakers.allow(name):
                            break
                        continue
                    ms = (time.perf_counter() - t0) * 1000
                    provider_ms += ms
                    self.breakers.record(name, ok=True)
                    attempts.append(Attempt(model=name, outcome="ok", latency_ms=round(ms, 2)))
                    actual = cost(
                        result.input_tokens,
                        result.output_tokens,
                        entry.input or Decimal(0),
                        entry.output or Decimal(0),
                    )
                    self.store.settle(team_id, worst, actual)
                    worst = Decimal(0)
                    latency = (time.perf_counter() - started) * 1000
                    self.store.record(
                        id=request_id,
                        app=app["id"],
                        team=team_id,
                        requested=req.model,
                        model=name,
                        provider=entry.provider,
                        prompt=prompt_ref,
                        status="ok",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cost=actual,
                        latency_ms=latency,
                        overhead_ms=latency - provider_ms,
                        fallback_used=position > 0,
                        attempts=[a.model_dump() for a in attempts],
                        redacted_preview=preview,
                        pii_found=scrubbed.found,
                    )
                    return ChatResponse(
                        id=request_id,
                        model=name,
                        provider=entry.provider,
                        content=result.content,
                        usage=Usage(
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            cost_usd=actual,
                        ),
                        latency_ms=round(latency, 2),
                        fallback_used=position > 0,
                        prompt=prompt_ref,
                        attempts=attempts,
                    )
        finally:
            if worst:  # nothing succeeded: release the reservation, bill nothing
                self.store.settle(team_id, worst, Decimal(0))

        latency = (time.perf_counter() - started) * 1000
        self.store.record(
            id=request_id,
            app=app["id"],
            team=team_id,
            requested=req.model,
            status="failed",
            error="all models in the route failed",
            attempts=[a.model_dump() for a in attempts],
            latency_ms=latency,
            overhead_ms=latency - provider_ms,
            redacted_preview=preview,
            pii_found=scrubbed.found,
        )
        raise GatewayError(502, "upstream", "all models in the route failed", request_id)

    def models(self, team_id: str) -> list[dict[str, Any]]:
        team = self.config.teams[team_id]
        return [{"name": target, "chain": self.config.chain(target)} for target in team.allow]
