from dataclasses import dataclass

from redis.asyncio import Redis

MISSING = "!404"
GONE = "!410"


@dataclass(frozen=True)
class CachedTarget:
    hit: bool
    url: str | None = None
    status: int = 302


@dataclass
class CacheStats:
    hits: int = 0
    negative_hits: int = 0
    misses: int = 0
    errors: int = 0

    def snapshot(self) -> dict:
        lookups = self.hits + self.negative_hits + self.misses
        ratio = (self.hits + self.negative_hits) / lookups if lookups else None
        return {**self.__dict__, "lookups": lookups, "hit_ratio": ratio}


class LinkCache:
    def __init__(self, redis: Redis, ttl_s: int, negative_ttl_s: int) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s
        self.stats = CacheStats()

    @staticmethod
    def key(link_hash: str) -> str:
        return f"link:{link_hash}"

    async def get(self, link_hash: str) -> CachedTarget:
        try:
            value = await self.redis.get(self.key(link_hash))
        except Exception:
            self.stats.errors += 1
            return CachedTarget(hit=False)
        if value is None:
            self.stats.misses += 1
            return CachedTarget(hit=False)
        if value == MISSING:
            self.stats.negative_hits += 1
            return CachedTarget(hit=True, status=404)
        if value == GONE:
            self.stats.negative_hits += 1
            return CachedTarget(hit=True, status=410)
        self.stats.hits += 1
        return CachedTarget(hit=True, url=value)

    async def remember(self, link_hash: str, url: str, ttl_s: int | None = None) -> None:
        ttl = max(1, min(ttl_s or self.ttl_s, self.ttl_s))
        await self._safe_set(link_hash, url, ttl)

    async def remember_missing(self, link_hash: str, status: int) -> None:
        await self._safe_set(link_hash, GONE if status == 410 else MISSING, self.negative_ttl_s)

    async def invalidate(self, link_hash: str) -> None:
        try:
            await self.redis.delete(self.key(link_hash))
        except Exception:
            self.stats.errors += 1

    async def _safe_set(self, link_hash: str, value: str, ttl: int) -> None:
        try:
            await self.redis.set(self.key(link_hash), value, ex=ttl)
        except Exception:
            self.stats.errors += 1
