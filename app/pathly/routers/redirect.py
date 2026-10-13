from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError

from pathly.cache import CachedTarget
from pathly.deps import Services, ServicesDep
from pathly.events import build_click_event
from pathly.models import Link
from pathly.routing import SqlTarget, current_decision
from pathly.shortcodes import is_valid_hash

router = APIRouter(tags=["redirect"])

NOT_FOUND = PlainTextResponse("Link not found", status_code=404)
GONE = PlainTextResponse("Link is no longer active", status_code=410)


async def find_link(services: Services, link_hash: str, target: SqlTarget) -> Link | None:
    async with services.db.session(target) as session:
        return await session.scalar(select(Link).where(Link.hash == link_hash))


async def resolve_from_database(services: Services, link_hash: str) -> CachedTarget:
    preferred = current_decision().sql
    link = None
    try:
        link = await find_link(services, link_hash, preferred)
    except (OperationalError, DBAPIError, OSError) as error:
        if preferred is SqlTarget.PRIMARY:
            raise
        decision = current_decision()
        services.db.mark_failed(decision.pools_used[-1] if decision.pools_used else "", error)

    if link is None and preferred is SqlTarget.REPLICA:
        link = await find_link(services, link_hash, SqlTarget.PRIMARY)

    if link is None:
        await services.cache.remember_missing(link_hash, 404)
        return CachedTarget(hit=False, status=404)

    now = datetime.now(UTC)
    if not link.is_redirectable(now):
        await services.cache.remember_missing(link_hash, 410)
        return CachedTarget(hit=False, status=410)

    ttl = None
    if link.expires_at is not None:
        ttl = int((link.expires_at - now).total_seconds())
    await services.cache.remember(link_hash, link.target_url, ttl)
    return CachedTarget(hit=False, url=link.target_url)


@router.get("/{link_hash}", include_in_schema=False)
async def follow(link_hash: str, request: Request, background: BackgroundTasks, services: ServicesDep) -> Response:
    if not is_valid_hash(link_hash):
        return NOT_FOUND

    resolved = await services.cache.get(link_hash)
    source = "redis"
    if not resolved.hit:
        resolved = await resolve_from_database(services, link_hash)
        source = ",".join(current_decision().pools_used)
    else:
        current_decision().record("redis_cache")

    if resolved.url is None:
        return GONE if resolved.status == 410 else NOT_FOUND

    event = build_click_event(request, link_hash, services.settings.trust_proxy_headers)
    background.add_task(services.publisher.publish, event)
    return RedirectResponse(
        resolved.url,
        status_code=302,
        headers={"Cache-Control": "no-store", "X-Resolved-From": source},
    )
