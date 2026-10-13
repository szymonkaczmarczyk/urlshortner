import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from pathly.analytics import AnalyticsStore
from pathly.cache import LinkCache
from pathly.config import get_settings
from pathly.db import Database
from pathly.deps import Services
from pathly.enrich import ClickEnricher
from pathly.events import ClickPublisher
from pathly.ingestor import ClickIngestor
from pathly.routers import analytics, auth, links, redirect, system
from pathly.routing import SmartRouterMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pathly")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database.from_settings(settings)
    await db.create_schema()
    await db.check_replicas()

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = AnalyticsStore(settings.duckdb_path)
    ingestor = ClickIngestor(
        redis=redis,
        store=store,
        enricher=ClickEnricher(settings.geoip_db_path, settings.visitor_salt),
        stream=settings.clicks_stream,
        group=settings.ingestor_group,
        batch_size=settings.ingestor_batch_size,
        block_ms=settings.ingestor_block_ms,
        reclaim_idle_ms=settings.ingestor_reclaim_idle_ms,
    )
    app.state.services = Services(
        settings=settings,
        db=db,
        cache=LinkCache(redis, settings.cache_ttl_s, settings.negative_cache_ttl_s),
        publisher=ClickPublisher(redis, settings.clicks_stream, settings.clicks_stream_maxlen),
        analytics=store,
        ingestor=ingestor,
    )

    background = [
        asyncio.create_task(db.monitor_replicas(settings.replica_health_interval_s), name="replica-monitor"),
        asyncio.create_task(ingestor.run(), name="click-ingestor"),
    ]
    log.info("pathly ready: %d replica pool(s), analytics at %s", len(db.replicas), settings.duckdb_path)
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError):
                await task
        await redis.aclose()
        store.close()
        await db.dispose()


app = FastAPI(title="Pathly", version="0.1.0", lifespan=lifespan)
app.add_middleware(SmartRouterMiddleware, sticky_seconds=get_settings().sticky_primary_after_write_s)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(auth.router)
app.include_router(links.router)
app.include_router(analytics.router)
app.include_router(system.router)


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.include_router(redirect.router)
