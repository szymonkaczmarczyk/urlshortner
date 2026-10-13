import argparse
import asyncio
import os
import random
import statistics
import time
from collections import Counter

import httpx

DEMO_LINKS = [
    ("launch", "https://example.com/product/launch"),
    ("pricing", "https://example.com/pricing"),
    ("blog-ddb", "https://example.com/blog/read-replicas-in-practice"),
    ("careers", "https://example.com/careers"),
    ("docs-api", "https://example.com/docs/api"),
    ("podcast", "https://example.com/podcast/episode-42"),
]

USER_AGENTS = [
    (30, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    (22, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"),
    (18, "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"),
    (10, "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"),
    (7, "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"),
    (5, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"),
    (5, "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"),
    (3, "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
]

REFERRERS = [
    (28, ""),
    (24, "https://www.google.com/"),
    (14, "https://t.co/abc123"),
    (11, "https://www.linkedin.com/feed/"),
    (9, "https://news.ycombinator.com/item?id=1"),
    (7, "https://www.reddit.com/r/programming/"),
    (4, "https://duckduckgo.com/"),
    (3, "https://github.com/"),
]

COUNTRIES = [(30, "PL"), (20, "US"), (12, "DE"), (9, "GB"), (7, "FR"), (6, "NL"), (5, "IN"), (4, "BR"), (4, "JP"), (3, "CA")]


def weighted(options: list[tuple[int, str]]) -> str:
    weights, values = zip(*options)
    return random.choices(values, weights=weights, k=1)[0]


class Visitor:
    def __init__(self) -> None:
        self.ip = ".".join(str(random.randint(1, 254)) for _ in range(4))
        self.user_agent = weighted(USER_AGENTS)
        self.country = weighted(COUNTRIES)


async def authenticate(client: httpx.AsyncClient, email: str, password: str) -> str:
    credentials = {"email": email, "password": password}
    response = await client.post("/api/auth/login", json=credentials)
    if response.status_code == 401:
        response = await client.post("/api/auth/register", json=credentials)
    response.raise_for_status()
    return response.json()["access_token"]


async def ensure_links(client: httpx.AsyncClient, token: str) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"}
    existing = {link["hash"] for link in (await client.get("/api/links", headers=headers)).json()}
    for alias, target in DEMO_LINKS:
        if alias not in existing:
            response = await client.post("/api/links", json={"target_url": target, "alias": alias}, headers=headers)
            if response.status_code not in (201, 409):
                response.raise_for_status()
    return [alias for alias, _ in DEMO_LINKS]


async def wait_for_api(client: httpx.AsyncClient) -> None:
    for _ in range(60):
        try:
            if (await client.get("/api/system/health")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise SystemExit("API did not become ready")


async def run(args: argparse.Namespace) -> None:
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=10, limits=limits, follow_redirects=False) as client:
        await wait_for_api(client)
        token = await authenticate(client, args.email, args.password)
        hashes = await ensure_links(client, token)
        link_weights = [1 / (rank + 1) for rank in range(len(hashes))]
        visitors = [Visitor() for _ in range(args.visitors)]

        print(f"simulating ~{args.rps} rps on {len(hashes)} links as {args.email} -> {args.base_url}")
        latencies: list[float] = []
        outcomes: Counter[str] = Counter()
        sources: Counter[str] = Counter()
        semaphore = asyncio.Semaphore(args.concurrency)

        async def click() -> None:
            visitor = random.choice(visitors)
            link_hash = random.choices(hashes, weights=link_weights, k=1)[0]
            headers = {
                "User-Agent": visitor.user_agent,
                "X-Forwarded-For": visitor.ip,
                "X-Country": visitor.country,
            }
            referrer = weighted(REFERRERS)
            if referrer:
                headers["Referer"] = referrer
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(f"/{link_hash}", headers=headers)
                    latencies.append((time.perf_counter() - started) * 1000)
                    outcomes[str(response.status_code)] += 1
                    sources[response.headers.get("x-resolved-from", "-")] += 1
                except httpx.HTTPError as error:
                    outcomes[type(error).__name__] += 1

        started_at = time.monotonic()
        next_report = started_at + 5
        pending: set[asyncio.Task] = set()
        while args.duration <= 0 or time.monotonic() - started_at < args.duration:
            in_burst = (time.monotonic() - started_at) % 60 > 52
            rate = args.rps * (args.burst if in_burst else 1)
            task = asyncio.create_task(click())
            pending.add(task)
            task.add_done_callback(pending.discard)
            await asyncio.sleep(random.expovariate(rate))
            if time.monotonic() >= next_report:
                window = sorted(latencies) or [0.0]
                p99 = window[min(len(window) - 1, int(len(window) * 0.99))]
                print(
                    f"sent={sum(outcomes.values())} status={dict(outcomes)} "
                    f"p50={statistics.median(window):.1f}ms p99={p99:.1f}ms resolved_from={dict(sources)}"
                )
                latencies.clear()
                sources.clear()
                next_report = time.monotonic() + 5
        await asyncio.gather(*pending)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pathly click traffic simulator")
    parser.add_argument("--base-url", default=os.getenv("SIM_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--email", default=os.getenv("DEMO_EMAIL", "demo@pathly.test"))
    parser.add_argument("--password", default=os.getenv("DEMO_PASSWORD", "pathly-demo-123"))
    parser.add_argument("--rps", type=float, default=float(os.getenv("SIM_RPS", "40")))
    parser.add_argument("--burst", type=float, default=4.0, help="rate multiplier for 8 s of every minute")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--visitors", type=int, default=1500)
    parser.add_argument("--duration", type=float, default=0, help="seconds, 0 = run forever")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
