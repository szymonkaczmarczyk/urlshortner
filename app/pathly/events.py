import logging
import time

from redis.asyncio import Redis
from starlette.requests import Request

log = logging.getLogger("pathly.events")

COUNTRY_HINT_HEADERS = ("cf-ipcountry", "x-country")


def client_ip(request: Request, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def build_click_event(request: Request, link_hash: str, trust_proxy_headers: bool) -> dict[str, str]:
    country_hint = ""
    if trust_proxy_headers:
        country_hint = next(
            (request.headers[name] for name in COUNTRY_HINT_HEADERS if name in request.headers), ""
        )
    return {
        "hash": link_hash,
        "ts": str(int(time.time() * 1000)),
        "ip": client_ip(request, trust_proxy_headers),
        "ua": request.headers.get("user-agent", "")[:512],
        "ref": request.headers.get("referer", "")[:1024],
        "country": country_hint[:2].upper(),
    }


class ClickPublisher:
    def __init__(self, redis: Redis, stream: str, maxlen: int) -> None:
        self.redis = redis
        self.stream = stream
        self.maxlen = maxlen
        self.published = 0
        self.dropped = 0

    async def publish(self, event: dict[str, str]) -> None:
        try:
            await self.redis.xadd(self.stream, event, maxlen=self.maxlen, approximate=True)
            self.published += 1
        except Exception as error:
            self.dropped += 1
            log.warning("click event dropped: %s", error)
