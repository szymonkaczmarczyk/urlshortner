import asyncio

from fastapi import APIRouter

from pathly.deps import ServicesDep

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("")
async def system(services: ServicesDep) -> dict:
    analytics_rows = await asyncio.to_thread(services.analytics.row_count)
    return {
        "pools": [
            *services.db.snapshot(),
            {
                "name": services.analytics.name,
                "role": "olap",
                "healthy": True,
                "served": services.analytics.queries,
                "rows": analytics_rows,
                "engine": "duckdb",
            },
        ],
        "cache": services.cache.stats.snapshot(),
        "events": {"published": services.publisher.published, "dropped": services.publisher.dropped},
        "ingestor": await services.ingestor.snapshot(),
    }


@router.get("/health")
async def health(services: ServicesDep) -> dict:
    return {
        "primary": services.db.primary.healthy,
        "replicas": {replica.name: replica.healthy for replica in services.db.replicas},
    }
