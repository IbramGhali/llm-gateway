"""Command line: gw serve | keys | traffic | eval | openapi."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from llm_gateway import evaluation
from llm_gateway.api import build, create_app
from llm_gateway.config import PROJECT_ROOT, Settings
from llm_gateway.store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Internal LLM gateway with policies, budgets and redaction.",
)
keys_app = typer.Typer(no_args_is_help=True, help="Manage app API keys.")
app.add_typer(keys_app, name="keys")
console = Console()
DEMO_APPS = {
    "helpdesk-bot": "support",
    "insights-notebook": "analytics",
    "intern-experiments": "sandbox",
}


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    demo: Annotated[bool, typer.Option(help="Create demo apps, save keys")] = False,
) -> None:
    """Run the gateway and admin page (http://host:port/admin)."""
    import uvicorn

    settings = Settings.from_env()
    if demo:
        _demo_keys(settings)
    uvicorn.run(create_app(build(settings)), host=host, port=port)


def _demo_keys(settings: Settings) -> Path:
    path = settings.state_dir / "demo-keys.json"
    if path.is_file():
        return path
    store = Store(settings.db_url)
    keys = {
        app_id: {"team": team, "key": store.create_app(app_id, team)}
        for app_id, team in DEMO_APPS.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    console.print(f"Demo keys written to {path} (local only, git-ignored)")
    return path


@keys_app.command("create")
def keys_create(app_id: str, team: str) -> None:
    """Create an API key for an app. The key is shown once; only its hash is stored."""
    settings = Settings.from_env()
    comps = build(settings)
    if team not in comps.config.teams:
        console.print(f"[red]Unknown team {team}[/red]")
        raise typer.Exit(2)
    console.print(comps.store.create_app(app_id, team))


@keys_app.command("revoke")
def keys_revoke(app_id: str) -> None:
    console.print("revoked" if Store(Settings.from_env().db_url).revoke(app_id) else "not found")


@app.command()
def traffic(
    url: str = "http://127.0.0.1:8080",
    requests: int = 60,
    keys: Annotated[Path | None, typer.Option(help="demo-keys.json")] = None,
) -> None:
    """Send demo traffic from the demo apps (including policy and budget refusals)."""
    import httpx

    settings = Settings.from_env()
    path = keys or _demo_keys(settings)
    demo = json.loads(path.read_text("utf-8"))
    rng = random.Random(3)
    outcomes: dict[int, int] = {}
    asks = {
        "support": ["fast", "cheap", "smart"],
        "analytics": ["smart", "fast"],
        "sandbox": ["cheap"],
    }
    for i in range(requests):
        app_id = rng.choice(list(demo))
        team = demo[app_id]["team"]
        body = {
            "model": rng.choice(asks[team]),
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": f"Ticket {i}: customer jo.{i}@example.com called from 0412 345 678.",
                }
            ],
        }
        if i % 5 == 0:
            body = {
                "model": "fast",
                "prompt": {
                    "id": "summarise-ticket",
                    "variables": {"ticket": f"Order {i} arrived damaged; refund requested."},
                },
            }
        r = httpx.post(
            f"{url}/v1/chat",
            json=body,
            headers={"Authorization": f"Bearer {demo[app_id]['key']}"},
            timeout=30,
        )
        outcomes[r.status_code] = outcomes.get(r.status_code, 0) + 1
    console.print(f"Sent {requests} requests: {outcomes}. Open {url}/admin")


@app.command("eval")
def eval_cmd(
    gate: Annotated[bool, typer.Option(help="Exit 1 if any target is missed")] = False,
) -> None:
    """Overhead under load, fallback correctness, budget enforcement and PII leakage."""
    settings = Settings.from_env()
    results = evaluation.run_all(settings)
    o, f, b, p = (results[k] for k in ("overhead", "fallbacks", "budgets", "pii"))
    for label, run in (("one at a time", o["serial"]), ("concurrent", o["concurrent"])):
        console.print(
            f"Gateway overhead, zero-latency provider, {label} (concurrency "
            f"{run['concurrency']}, {run['requests']} requests): p50 "
            f"{run['overhead_ms_p50']} ms, p95 {run['overhead_ms_p95']} ms, "
            f"{run['throughput_rps']} req/s"
        )
    table = Table(title="Fallback under injected provider failures (route: smart)")
    for col in (
        "Case",
        "Served by",
        "Failed (502)",
        "Correct",
        "Breaker trips",
        "Calls to primary",
    ):
        table.add_column(col)
    for name, r in f["cases"].items():
        table.add_row(
            name,
            json.dumps(r["served_by"]),
            str(r["failed"]),
            f"{r['correct']}/{r['requests']}",
            json.dumps(r["breaker_trips"]),
            str(r["primary_calls"]),
        )
    console.print(table)
    console.print(
        f"Budget: {b['requests']} concurrent requests against a "
        f"${b['budget_usd']} budget: {b['served']} served, {b['refused_budget']} "
        f"refused; spent ${b['spent_usd']} (within budget: {b['within_budget']}); "
        f"refused requests reached a provider: "
        f"{not b['refused_without_provider_call']}"
    )
    console.print(
        f"PII: {p['prompts']} prompts with {p['planted_values']} planted values; "
        f"{p['prompts_fully_redacted']} fully redacted; leaks into logs: "
        f"{p['leaks_into_logs']}; amounts wrongly removed: "
        f"{p['amounts_wrongly_removed']}"
    )
    out = PROJECT_ROOT / "eval" / "results" / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    if gate:
        problems = evaluation.gate(results)
        for prob in problems:
            console.print(f"[red]Gate:[/red] {prob}")
        console.print("[green]Gate passed[/green]" if not problems else "[red]Gate failed[/red]")
        if problems:
            raise typer.Exit(1)


@app.command()
def openapi() -> None:
    """Write openapi.json from the running app definition."""
    spec = create_app(build(Settings(database_url="sqlite://"))).openapi()
    (PROJECT_ROOT / "openapi.json").write_text(
        json.dumps(spec, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    console.print("openapi.json written")


if __name__ == "__main__":
    app()
