import asyncio
import itertools
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from pathly.config import Settings
from pathly.models import Base
from pathly.routing import SqlTarget, current_decision

log = logging.getLogger("pathly.db")


@dataclass
class SqlPool:
    name: str
    role: str
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    healthy: bool = True
    lag_bytes: int | None = None
    last_error: str | None = None
    served: int = 0

    def snapshot(self) -> dict:
        pool = self.engine.pool
        return {
            "name": self.name,
            "role": self.role,
            "healthy": self.healthy,
            "lag_bytes": self.lag_bytes,
            "served": self.served,
            "last_error": self.last_error,
            "checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
            "size": pool.size() if hasattr(pool, "size") else None,
        }


@dataclass
class Database:
    primary: SqlPool
    replicas: list[SqlPool]
    max_lag_bytes: int
    _round_robin: itertools.count = field(default_factory=itertools.count)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        def build(name: str, role: str, dsn: str, size: int) -> SqlPool:
            engine = create_async_engine(
                dsn,
                pool_size=size,
                max_overflow=size,
                pool_pre_ping=True,
                pool_recycle=1800,
                connect_args={"timeout": 3, "server_settings": {"application_name": f"pathly:{name}"}},
            )
            return SqlPool(name, role, engine, async_sessionmaker(engine, expire_on_commit=False))

        primary = build("pool_primary", "primary", settings.primary_dsn, settings.primary_pool_size)
        replicas = [
            build(f"pool_replica_{index}", "replica", dsn, settings.replica_pool_size)
            for index, dsn in enumerate(settings.replica_dsns, start=1)
        ]
        return cls(primary, replicas, settings.replica_max_lag_bytes)

    def pick_replica(self) -> SqlPool:
        healthy = [replica for replica in self.replicas if replica.healthy]
        if not healthy:
            return self.primary
        return healthy[next(self._round_robin) % len(healthy)]

    def pool_for(self, target: SqlTarget) -> SqlPool:
        return self.primary if target is SqlTarget.PRIMARY else self.pick_replica()

    @asynccontextmanager
    async def session(self, target: SqlTarget | None = None) -> AsyncIterator[AsyncSession]:
        pool = self.pool_for(target or current_decision().sql)
        current_decision().record(pool.name)
        pool.served += 1
        async with pool.sessionmaker() as session:
            session.info["pool"] = pool.name
            yield session

    def mark_failed(self, pool_name: str, error: Exception) -> None:
        for replica in self.replicas:
            if replica.name == pool_name:
                replica.healthy = False
                replica.last_error = str(error)[:200]

    async def create_schema(self, attempts: int = 30) -> None:
        for attempt in range(1, attempts + 1):
            try:
                async with self.primary.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                return
            except Exception as error:
                if attempt == attempts:
                    raise
                log.warning("primary not ready (%s), retry %d/%d", error, attempt, attempts)
                await asyncio.sleep(1)

    async def monitor_replicas(self, interval_s: float) -> None:
        while True:
            await self.check_replicas()
            await asyncio.sleep(interval_s)

    async def check_replicas(self) -> None:
        try:
            async with self.primary.engine.connect() as connection:
                primary_lsn = (await connection.execute(text("SELECT pg_current_wal_lsn()::text"))).scalar_one()
            self.primary.healthy = True
            self.primary.last_error = None
        except Exception as error:
            self.primary.healthy = False
            self.primary.last_error = str(error)[:200]
            return

        for replica in self.replicas:
            try:
                async with asyncio.timeout(2):
                    async with replica.engine.connect() as connection:
                        row = (
                            await connection.execute(
                                text(
                                    "SELECT pg_is_in_recovery(), "
                                    "pg_wal_lsn_diff(CAST(:primary_lsn AS pg_lsn), pg_last_wal_replay_lsn())"
                                ),
                                {"primary_lsn": primary_lsn},
                            )
                        ).one()
                in_recovery, lag = row
                replica.lag_bytes = max(int(lag or 0), 0)
                replica.healthy = bool(in_recovery) and replica.lag_bytes <= self.max_lag_bytes
                replica.last_error = None if replica.healthy else f"lag {replica.lag_bytes}B over limit"
            except Exception as error:
                replica.healthy = False
                replica.lag_bytes = None
                replica.last_error = str(error)[:200] or type(error).__name__

    def snapshot(self) -> list[dict]:
        return [self.primary.snapshot(), *(replica.snapshot() for replica in self.replicas)]

    async def dispose(self) -> None:
        await self.primary.engine.dispose()
        for replica in self.replicas:
            await replica.engine.dispose()
