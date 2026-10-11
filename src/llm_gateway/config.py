"""Settings (environment) and gateway configuration (YAML)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelEntry(BaseModel):
    provider: str
    model: str
    input: Decimal | None
    output: Decimal | None

    @property
    def priced(self) -> bool:
        return self.input is not None and self.output is not None


class Team(BaseModel):
    allow: list[str]
    monthly_budget: Decimal
    redact_prompts: bool = True


class GatewayConfig(BaseModel):
    version: str
    models: dict[str, ModelEntry]
    routes: dict[str, list[str]]
    teams: dict[str, Team]

    @model_validator(mode="after")
    def _check(self) -> GatewayConfig:
        for route, chain in self.routes.items():
            for name in chain:
                if name not in self.models:
                    raise ValueError(f"route {route}: unknown model {name}")
        for team_id, team in self.teams.items():
            for target in team.allow:
                if target not in self.routes and target not in self.models:
                    raise ValueError(f"team {team_id}: unknown route or model {target}")
                for name in self.routes.get(target, [target]):
                    if not self.models[name].priced:
                        raise ValueError(
                            f"team {team_id}: model {name} has no price; budgets cannot be enforced"
                        )
        return self

    def chain(self, target: str) -> list[str]:
        return self.routes.get(target, [target] if target in self.models else [])


def load_config(path: Path) -> GatewayConfig:
    with path.open(encoding="utf-8") as fh:
        return GatewayConfig.model_validate(yaml.safe_load(fh))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


@dataclass(frozen=True)
class Settings:
    provider_mode: str = "fake"  # fake: every provider is a deterministic stand-in
    database_url: str = ""
    request_timeout: float = 30.0
    max_attempts_per_model: int = 2
    state_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".gw")
    config_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "gateway.yaml")
    prompts_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "prompts")

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.state_dir / 'gateway.db').as_posix()}"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(PROJECT_ROOT / ".env")
        d = cls()
        return cls(
            provider_mode=_env("GW_PROVIDER_MODE", d.provider_mode),
            database_url=_env("GW_DATABASE_URL"),
            request_timeout=float(_env("GW_REQUEST_TIMEOUT", str(d.request_timeout))),
            max_attempts_per_model=int(_env("GW_MAX_ATTEMPTS", str(d.max_attempts_per_model))),
            state_dir=Path(_env("GW_STATE_DIR", str(d.state_dir))),
        )
