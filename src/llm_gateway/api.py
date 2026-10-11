"""HTTP API: POST /v1/chat, GET /v1/models, the admin usage page and its JSON."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from llm_gateway.config import GatewayConfig, Settings, load_config
from llm_gateway.gateway import Gateway, GatewayError
from llm_gateway.prompts import PromptRegistry
from llm_gateway.providers import make_providers
from llm_gateway.providers.base import Provider
from llm_gateway.reliability import Breakers
from llm_gateway.schemas import ChatRequest, ChatResponse, ErrorBody
from llm_gateway.store import Store

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class Components:
    settings: Settings
    config: GatewayConfig
    store: Store
    gateway: Gateway


def build(
    settings: Settings,
    providers: dict[str, Provider] | None = None,
    store: Store | None = None,
    breakers: Breakers | None = None,
) -> Components:
    config = load_config(settings.config_path)
    store = store or Store(settings.db_url)
    gateway = Gateway(
        settings,
        config,
        store,
        providers or make_providers(settings.provider_mode),
        PromptRegistry(settings.prompts_dir),
        breakers or Breakers(),
    )
    return Components(settings, config, store, gateway)


def _key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return None


def create_app(components: Components | None = None) -> FastAPI:
    c = components or build(Settings.from_env())
    api = FastAPI(
        title="LLM gateway",
        version="0.1.0",
        description="One API for internal apps across Claude, GPT and Gemini.",
    )

    @api.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        body = ErrorBody(id=exc.request_id, error=exc.code, detail=exc.detail)
        return JSONResponse(
            body.model_dump(), status_code=exc.status, headers={"x-request-id": exc.request_id}
        )

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "config": c.config.version, "mode": c.settings.provider_mode}

    @api.post(
        "/v1/chat",
        response_model=ChatResponse,
        responses={
            401: {"model": ErrorBody},
            402: {"model": ErrorBody},
            403: {"model": ErrorBody},
            502: {"model": ErrorBody},
        },
    )
    def chat(
        body: ChatRequest,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        response = c.gateway.chat(_key(authorization, x_api_key), body)
        return JSONResponse(response.model_dump(mode="json"), headers={"x-request-id": response.id})

    @api.get("/v1/models")
    def models(
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> list[dict[str, Any]]:
        key = _key(authorization, x_api_key)
        app = c.store.app_for_key(key) if key else None
        if app is None:
            raise GatewayError(401, "unauthorised", "missing or invalid API key", "-")
        return c.gateway.models(app["team"])

    @api.get("/admin/usage.json")
    def usage() -> dict[str, Any]:
        teams = {}
        for team_id, team in c.config.teams.items():
            spent, reserved = c.store.team_spend(team_id)
            teams[team_id] = {
                "budget": str(team.monthly_budget),
                "spent": str(spent),
                "reserved": str(reserved),
            }
        return {
            "teams": teams,
            "usage": [{**u, "cost": str(u["cost"])} for u in c.store.usage()],
            "breaker_trips": c.gateway.breakers.trips(),
        }

    @api.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        teams = []
        for team_id, team in c.config.teams.items():
            spent, _ = c.store.team_spend(team_id)
            pct = float(spent / team.monthly_budget * 100) if team.monthly_budget else 0.0
            teams.append(
                {
                    "id": team_id,
                    "budget": team.monthly_budget,
                    "spent": spent.quantize(spent.__class__("0.0001")),
                    "pct": pct,
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "admin.html",
            {
                "teams": teams,
                "usage": c.store.usage(),
                "recent": c.store.ledger(25),
                "mode": c.settings.provider_mode,
            },
        )

    return api
