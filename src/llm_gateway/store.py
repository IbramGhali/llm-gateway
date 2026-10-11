"""Persistence: apps and API keys, the usage ledger, and per-team monthly spend.

Budgets use a reservation: before a provider is called, the worst-case cost of the request is
reserved with a single conditional UPDATE that only succeeds if spent + reserved + estimate
stays within the budget. Concurrent requests therefore cannot overspend between them. After
the call the reservation is replaced by the actual cost.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

apps = Table(
    "apps",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("team", String(64), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("key_prefix", String(12), nullable=False),
    Column("revoked", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

spend = Table(
    "team_spend",
    metadata,
    Column("team", String(64), primary_key=True),
    Column("month", String(7), primary_key=True),
    Column("spent", Numeric(14, 6), nullable=False, default=0),
    Column("reserved", Numeric(14, 6), nullable=False, default=0),
)

requests = Table(
    "requests",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("app", String(64), nullable=False),
    Column("team", String(64), nullable=False),
    Column("requested", String(64), nullable=False),
    Column("model", String(64)),
    Column("provider", String(32)),
    Column("prompt", String(64)),
    Column("status", String(24), nullable=False),  # ok | refused_* | failed
    Column("error", Text),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("cost", Numeric(14, 6), nullable=False, default=0),
    Column("latency_ms", Float, nullable=False, default=0),
    Column("overhead_ms", Float, nullable=False, default=0),
    Column("fallback_used", Boolean, nullable=False, default=False),
    Column("attempts", Text, nullable=False),
    Column("redacted_preview", Text, nullable=False),  # redacted, truncated; never raw text
    Column("pii_found", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def now() -> datetime:
    return datetime.now(UTC)


def month() -> str:
    return now().strftime("%Y-%m")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            self.engine: Engine = create_engine(
                url, connect_args={"check_same_thread": False, "timeout": 30}
            )
        elif url == "sqlite://":
            from sqlalchemy.pool import StaticPool

            self.engine = create_engine(
                url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        else:
            self.engine = create_engine(url, pool_size=20, max_overflow=20, pool_pre_ping=True)
        # SQLite allows one writer at a time, and the in-memory database shares one connection,
        # so access is serialised in-process. Postgres needs no lock: the conditional UPDATE
        # in `reserve` is atomic on its own.
        self._write_lock: threading.Lock | None = (
            threading.Lock() if self.engine.dialect.name == "sqlite" else None
        )
        metadata.create_all(self.engine)
        # Short-lived key cache: a revoked key stops working within KEY_CACHE_SECONDS.
        self._keys: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._spend_rows: set[tuple[str, str]] = set()

    KEY_CACHE_SECONDS = 5.0

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self._write_lock or nullcontext(), self.engine.begin() as c:
            yield c

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        with self._write_lock or nullcontext(), self.engine.connect() as c:
            yield c

    # keys -------------------------------------------------------------------------------
    def create_app(self, app_id: str, team: str) -> str:
        """Create an app and return its API key. Only the hash is stored."""
        key = "gw_" + secrets.token_urlsafe(32)
        with self._tx() as c:
            c.execute(
                insert(apps).values(
                    id=app_id,
                    team=team,
                    key_hash=hash_key(key),
                    key_prefix=key[:10],
                    revoked=False,
                    created_at=now(),
                )
            )
        return key

    def revoke(self, app_id: str) -> bool:
        self._keys.clear()
        with self._tx() as c:
            return bool(
                c.execute(update(apps).where(apps.c.id == app_id).values(revoked=True)).rowcount
            )

    def app_for_key(self, key: str) -> dict[str, Any] | None:
        digest = hash_key(key)
        cached = self._keys.get(digest)
        if cached and time.monotonic() - cached[0] < self.KEY_CACHE_SECONDS:
            return cached[1]
        app = self._lookup(digest)
        self._keys[digest] = (time.monotonic(), app)
        return app

    def _lookup(self, digest: str) -> dict[str, Any] | None:
        with self._read() as c:
            row = c.execute(
                select(apps).where(and_(apps.c.key_hash == digest, apps.c.revoked.is_(False)))
            ).first()
        return dict(row._mapping) if row else None

    # budgets ----------------------------------------------------------------------------
    def _ensure_row(self, team: str, m: str) -> None:
        if (team, m) in self._spend_rows:
            return
        with self._tx() as c:
            if not c.execute(
                select(spend.c.team).where(and_(spend.c.team == team, spend.c.month == m))
            ).first():
                try:
                    with c.begin_nested():
                        c.execute(insert(spend).values(team=team, month=m, spent=0, reserved=0))
                except IntegrityError:  # another request created it first
                    pass
        self._spend_rows.add((team, m))

    def reserve(self, team: str, amount: Decimal, budget: Decimal) -> bool:
        m = month()
        self._ensure_row(team, m)
        with self._tx() as c:
            result = c.execute(
                update(spend)
                .where(
                    and_(
                        spend.c.team == team,
                        spend.c.month == m,
                        spend.c.spent + spend.c.reserved + amount <= budget,
                    )
                )
                .values(reserved=spend.c.reserved + amount)
            )
        return bool(result.rowcount)

    def settle(self, team: str, reserved: Decimal, actual: Decimal) -> None:
        with self._tx() as c:
            c.execute(
                update(spend)
                .where(and_(spend.c.team == team, spend.c.month == month()))
                .values(reserved=spend.c.reserved - reserved, spent=spend.c.spent + actual)
            )

    def team_spend(self, team: str) -> tuple[Decimal, Decimal]:
        with self._read() as c:
            row = c.execute(
                select(spend.c.spent, spend.c.reserved).where(
                    and_(spend.c.team == team, spend.c.month == month())
                )
            ).first()
        return (Decimal(row.spent), Decimal(row.reserved)) if row else (Decimal(0), Decimal(0))

    # ledger -----------------------------------------------------------------------------
    def record(self, **row: Any) -> None:
        row["attempts"] = json.dumps(row.get("attempts", []))
        row["pii_found"] = json.dumps(row.get("pii_found", {}))
        with self._tx() as c:
            c.execute(insert(requests).values(created_at=now(), **row))

    def ledger(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._read() as c:
            rows = c.execute(
                select(requests).order_by(requests.c.created_at.desc()).limit(limit)
            ).all()
        return [dict(r._mapping) for r in rows]

    def all_logged_text(self) -> str:
        """Everything the gateway has persisted about requests, for leak checks."""
        with self._read() as c:
            rows = c.execute(select(requests)).all()
        return "\n".join(json.dumps({k: str(v) for k, v in r._mapping.items()}) for r in rows)

    def usage(self) -> list[dict[str, Any]]:
        with self._read() as c:
            rows = c.execute(
                select(
                    requests.c.team,
                    requests.c.model,
                    requests.c.status,
                    func.count().label("requests"),
                    func.sum(requests.c.input_tokens).label("input_tokens"),
                    func.sum(requests.c.output_tokens).label("output_tokens"),
                    func.sum(requests.c.cost).label("cost"),
                )
                .group_by(requests.c.team, requests.c.model, requests.c.status)
                .order_by(requests.c.team, requests.c.model)
            ).all()
            latencies = c.execute(
                select(requests.c.team, requests.c.latency_ms).where(requests.c.status == "ok")
            ).all()
        by_team: dict[str, list[float]] = {}
        for team, ms in latencies:
            by_team.setdefault(team, []).append(ms)
        out = []
        for r in rows:
            lat = sorted(by_team.get(r.team, [0.0]))
            out.append(
                {
                    **dict(r._mapping),
                    "cost": Decimal(r.cost or 0),
                    "p50_ms": round(lat[len(lat) // 2], 1),
                    "p95_ms": round(lat[int(0.95 * (len(lat) - 1))], 1),
                }
            )
        return out
