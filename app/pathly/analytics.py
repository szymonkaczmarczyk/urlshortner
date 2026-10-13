import asyncio
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from pathly.enrich import ClickRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS clicks (
    event_id      VARCHAR PRIMARY KEY,
    hash          VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,
    country       VARCHAR NOT NULL,
    referrer_host VARCHAR NOT NULL,
    device        VARCHAR NOT NULL,
    browser       VARCHAR NOT NULL,
    os            VARCHAR NOT NULL,
    is_bot        BOOLEAN NOT NULL,
    visitor_id    VARCHAR NOT NULL
)
"""

BREAKDOWNS = ("referrer_host", "device", "browser", "os", "country")

ROW_JSON_TYPES = {
    "event_id": "VARCHAR",
    "hash": "VARCHAR",
    "ts_ms": "BIGINT",
    "country": "VARCHAR",
    "referrer_host": "VARCHAR",
    "device": "VARCHAR",
    "browser": "VARCHAR",
    "os": "VARCHAR",
    "is_bot": "BOOLEAN",
    "visitor_id": "VARCHAR",
}

INSERT_FROM_JSON = f"""
INSERT OR IGNORE INTO clicks
SELECT r.event_id, r.hash, epoch_ms(r.ts_ms), r.country, r.referrer_host,
       r.device, r.browser, r.os, r.is_bot, r.visitor_id
FROM (SELECT unnest(from_json(?::JSON, '{json.dumps([ROW_JSON_TYPES])}')) AS r)
"""

HASH_SCOPE = """hash IN (SELECT unnest(from_json(?::JSON, '["VARCHAR"]')))"""


@dataclass(frozen=True)
class Window:
    key: str
    span: timedelta
    bucket: timedelta


WINDOWS = {
    window.key: window
    for window in (
        Window("15m", timedelta(minutes=15), timedelta(seconds=15)),
        Window("1h", timedelta(hours=1), timedelta(minutes=1)),
        Window("24h", timedelta(hours=24), timedelta(minutes=30)),
        Window("7d", timedelta(days=7), timedelta(hours=6)),
        Window("30d", timedelta(days=30), timedelta(days=1)),
    )
}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def floor_to(moment: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1)
    seconds = int((moment - epoch).total_seconds())
    step_s = int(step.total_seconds())
    return epoch + timedelta(seconds=seconds - seconds % step_s)


class AnalyticsStore:
    name = "pool_analytics"

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = duckdb.connect(path)
        self._connection.execute(SCHEMA)
        self._writer = self._connection.cursor()
        self._write_lock = threading.Lock()
        self.queries = 0

    def close(self) -> None:
        self._writer.close()
        self._connection.close()

    def insert_batch(self, rows: Sequence[ClickRow]) -> int:
        if not rows:
            return 0
        payload = json.dumps([row._asdict() for row in rows])
        with self._write_lock:
            before = self._writer.execute("SELECT count(*) FROM clicks").fetchone()[0]
            self._writer.execute(INSERT_FROM_JSON, [payload])
            after = self._writer.execute("SELECT count(*) FROM clicks").fetchone()[0]
        return after - before

    def row_count(self) -> int:
        with self._connection.cursor() as cursor:
            return cursor.execute("SELECT count(*) FROM clicks").fetchone()[0]

    def totals_by_hash(self, hashes: list[str]) -> dict[str, int]:
        if not hashes:
            return {}
        self.queries += 1
        with self._connection.cursor() as cursor:
            rows = cursor.execute(
                f"SELECT hash, count(*) FROM clicks WHERE {HASH_SCOPE} AND NOT is_bot GROUP BY hash",
                [json.dumps(hashes)],
            ).fetchall()
        return {link_hash: clicks for link_hash, clicks in rows}

    def stats(self, link_hashes: list[str], window: Window, top: int = 8) -> dict:
        self.queries += 1
        now = utc_now()
        since = now - window.span
        previous_since = since - window.span
        first_bucket = floor_to(since, window.bucket)
        last_bucket = floor_to(now, window.bucket)

        scope = HASH_SCOPE
        hashes = json.dumps(link_hashes)
        with self._connection.cursor() as cursor:

            totals = cursor.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE ts >= ? AND NOT is_bot),
                    count(DISTINCT visitor_id) FILTER (WHERE ts >= ? AND NOT is_bot),
                    count(*) FILTER (WHERE ts >= ? AND is_bot),
                    count(*) FILTER (WHERE ts < ? AND NOT is_bot),
                    count(*) FILTER (WHERE ts >= ? AND NOT is_bot)
                FROM clicks
                WHERE {scope} AND ts >= ?
                """,
                [since, since, since, since, now - timedelta(seconds=60), hashes, previous_since],
            ).fetchone()

            series = cursor.execute(
                f"""
                WITH buckets AS (
                    SELECT generate_series AS bucket
                    FROM generate_series(?::TIMESTAMP, ?::TIMESTAMP, ?::INTERVAL)
                ),
                agg AS (
                    SELECT time_bucket(?::INTERVAL, ts) AS bucket,
                           count(*) AS clicks,
                           count(DISTINCT visitor_id) AS visitors
                    FROM clicks
                    WHERE {scope} AND ts >= ? AND NOT is_bot
                    GROUP BY 1
                )
                SELECT b.bucket, coalesce(a.clicks, 0), coalesce(a.visitors, 0)
                FROM buckets b LEFT JOIN agg a USING (bucket)
                ORDER BY b.bucket
                """,
                [first_bucket, last_bucket, window.bucket, window.bucket, hashes, first_bucket],
            ).fetchall()

            breakdowns = {}
            for column in BREAKDOWNS:
                breakdowns[column] = [
                    {"key": key, "clicks": clicks}
                    for key, clicks in cursor.execute(
                        f"""
                        SELECT {column}, count(*) FROM clicks
                        WHERE {scope} AND ts >= ? AND NOT is_bot
                        GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?
                        """,
                        [hashes, since, top],
                    ).fetchall()
                ]

            top_links = [
                {"key": key, "clicks": clicks, "visitors": visitors}
                for key, clicks, visitors in cursor.execute(
                    f"""
                    SELECT hash, count(*), count(DISTINCT visitor_id) FROM clicks
                    WHERE {scope} AND ts >= ? AND NOT is_bot
                    GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?
                    """,
                    [hashes, since, top],
                ).fetchall()
            ]

        clicks, visitors, bot_clicks, previous_clicks, last_minute = totals
        return {
            "window": window.key,
            "bucket_seconds": int(window.bucket.total_seconds()),
            "generated_at": now.isoformat() + "Z",
            "totals": {
                "clicks": clicks,
                "visitors": visitors,
                "bot_clicks": bot_clicks,
                "previous_clicks": previous_clicks,
                "last_minute": last_minute,
            },
            "series": [
                {"t": bucket.isoformat() + "Z", "clicks": bucket_clicks, "visitors": bucket_visitors}
                for bucket, bucket_clicks, bucket_visitors in series
            ],
            "referrers": breakdowns["referrer_host"],
            "devices": breakdowns["device"],
            "browsers": breakdowns["browser"],
            "os": breakdowns["os"],
            "countries": breakdowns["country"],
            "top_links": top_links,
        }

    async def stats_async(self, hashes: list[str], window: Window) -> dict:
        return await asyncio.to_thread(self.stats, hashes, window)

    async def totals_by_hash_async(self, hashes: list[str]) -> dict[str, int]:
        return await asyncio.to_thread(self.totals_by_hash, hashes)
