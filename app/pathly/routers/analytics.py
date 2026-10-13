import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from pathly.analytics import WINDOWS, Window
from pathly.deps import CurrentUser, Services, ServicesDep, UserDep
from pathly.models import Link
from pathly.routing import current_decision

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

WindowKey = Annotated[str, Query(pattern="^(" + "|".join(WINDOWS) + ")$")]
STREAM_REFRESH_S = 5.0
STREAM_MIN_INTERVAL_S = 1.0


async def scoped_hashes(services: Services, user: CurrentUser, link_hash: str | None) -> list[str]:
    async with services.db.session() as session:
        owned = list(await session.scalars(select(Link.hash).where(Link.owner_id == user.id)))
    if link_hash is None:
        return owned
    if link_hash not in owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    return [link_hash]


def use_analytics_pool(services: Services) -> None:
    current_decision().record(services.analytics.name)


@router.get("/stats")
async def stats(services: ServicesDep, user: UserDep, window: WindowKey = "1h", hash: str | None = None) -> dict:
    hashes = await scoped_hashes(services, user, hash)
    use_analytics_pool(services)
    return await services.analytics.stats_async(hashes, WINDOWS[window])


@router.get("/totals")
async def totals(services: ServicesDep, user: UserDep) -> dict[str, int]:
    hashes = await scoped_hashes(services, user, None)
    use_analytics_pool(services)
    return await services.analytics.totals_by_hash_async(hashes)


@router.get("/stream")
async def stream(
    request: Request, services: ServicesDep, user: UserDep, window: WindowKey = "1h", hash: str | None = None
) -> StreamingResponse:
    hashes = await scoped_hashes(services, user, hash)
    use_analytics_pool(services)
    selected: Window = WINDOWS[window]

    async def events():
        while not await request.is_disconnected():
            payload = await services.analytics.stats_async(hashes, selected)
            yield f"event: stats\ndata: {json.dumps(payload)}\n\n"
            await services.ingestor.flushed.wait(STREAM_REFRESH_S)
            await asyncio.sleep(STREAM_MIN_INTERVAL_S)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
