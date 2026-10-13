import asyncio
import logging
import socket
import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from pathly.analytics import AnalyticsStore
from pathly.enrich import ClickEnricher

log = logging.getLogger("pathly.ingestor")


class FlushSignal:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except TimeoutError:
            return False


class ClickIngestor:
    def __init__(
        self,
        redis: Redis,
        store: AnalyticsStore,
        enricher: ClickEnricher,
        stream: str,
        group: str,
        batch_size: int,
        block_ms: int,
        reclaim_idle_ms: int,
    ) -> None:
        self.redis = redis
        self.store = store
        self.enricher = enricher
        self.stream = stream
        self.group = group
        self.consumer = socket.gethostname()
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.reclaim_idle_ms = reclaim_idle_ms
        self.flushed = FlushSignal()
        self.ingested_total = 0
        self.duplicates_skipped = 0
        self.batches = 0
        self.errors = 0
        self.last_flush_at: float | None = None
        self.last_batch_size = 0
        self.last_batch_ms = 0.0

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def run(self) -> None:
        while True:
            try:
                await self.ensure_group()
                break
            except Exception as error:
                log.warning("redis not ready for consumer group (%s), retrying", error)
                await asyncio.sleep(1)
        read_from = "0"
        last_reclaim = 0.0
        while True:
            try:
                if time.monotonic() - last_reclaim > 30:
                    await self.reclaim_abandoned()
                    last_reclaim = time.monotonic()

                response = await self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: read_from},
                    count=self.batch_size,
                    block=None if read_from == "0" else self.block_ms,
                )
                entries = response[0][1] if response else []
                if read_from == "0" and not entries:
                    read_from = ">"
                    continue
                if entries:
                    await self.process(entries)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.exception("ingest batch failed, pending entries will be retried")
                read_from = "0"
                await asyncio.sleep(1)

    async def reclaim_abandoned(self) -> None:
        _, entries, *_ = await self.redis.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_time=self.reclaim_idle_ms, count=self.batch_size
        )
        if entries:
            log.info("reclaimed %d abandoned click events", len(entries))
            await self.process(entries)

    async def process(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        started = time.perf_counter()
        valid = [(event_id, fields) for event_id, fields in entries if fields]
        rows = [self.enricher.enrich(event_id, fields) for event_id, fields in valid]
        inserted = await asyncio.to_thread(self.store.insert_batch, rows)
        await self.redis.xack(self.stream, self.group, *[event_id for event_id, _ in entries])

        self.batches += 1
        self.ingested_total += inserted
        self.duplicates_skipped += len(rows) - inserted
        self.last_batch_size = len(rows)
        self.last_batch_ms = (time.perf_counter() - started) * 1000
        self.last_flush_at = time.time()
        self.flushed.notify()

    async def backlog(self) -> dict:
        try:
            length = await self.redis.xlen(self.stream)
            groups = await self.redis.xinfo_groups(self.stream)
        except Exception:
            return {"length": None, "pending": None, "lag": None}
        group = next((item for item in groups if item["name"] == self.group), {})
        return {"length": length, "pending": group.get("pending"), "lag": group.get("lag")}

    async def snapshot(self) -> dict:
        return {
            "consumer": self.consumer,
            "group": self.group,
            "stream": self.stream,
            "ingested_total": self.ingested_total,
            "duplicates_skipped": self.duplicates_skipped,
            "batches": self.batches,
            "errors": self.errors,
            "last_flush_at": self.last_flush_at,
            "last_batch_size": self.last_batch_size,
            "last_batch_ms": round(self.last_batch_ms, 2),
            **await self.backlog(),
        }
