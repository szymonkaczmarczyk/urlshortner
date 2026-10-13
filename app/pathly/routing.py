from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ANALYTICS_PREFIX = "/api/analytics"
STICKY_COOKIE = "pathly_rw"
CONSISTENCY_HEADER = "x-consistency"


class SqlTarget(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"


@dataclass
class RouteDecision:
    sql: SqlTarget
    reason: str
    olap: bool = False
    pools_used: list[str] = field(default_factory=list)

    def record(self, pool_name: str) -> None:
        if pool_name not in self.pools_used:
            self.pools_used.append(pool_name)

    def describe(self) -> str:
        olap = "; olap=analytics" if self.olap else ""
        return f"sql={self.sql.value} ({self.reason}){olap}"


_current: ContextVar[RouteDecision | None] = ContextVar("pathly_route", default=None)


def current_decision() -> RouteDecision:
    decision = _current.get()
    if decision is None:
        decision = RouteDecision(sql=SqlTarget.PRIMARY, reason="no-request-context")
        _current.set(decision)
    return decision


def decide(method: str, path: str, headers: Headers, cookies: dict[str, str]) -> RouteDecision:
    olap = path.startswith(ANALYTICS_PREFIX)
    if method in WRITE_METHODS:
        return RouteDecision(SqlTarget.PRIMARY, "write-method", olap)
    if headers.get(CONSISTENCY_HEADER, "").lower() == "strong":
        return RouteDecision(SqlTarget.PRIMARY, "x-consistency:strong", olap)
    if STICKY_COOKIE in cookies:
        return RouteDecision(SqlTarget.PRIMARY, "sticky-after-write", olap)
    return RouteDecision(SqlTarget.REPLICA, "read-method", olap)


def parse_cookies(headers: Headers) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in headers.get("cookie", "").split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep:
            cookies[name] = value
    return cookies


class SmartRouterMiddleware:
    def __init__(self, app: ASGIApp, sticky_seconds: int) -> None:
        self.app = app
        self.sticky_seconds = sticky_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        method = scope["method"]
        decision = decide(method, scope["path"], headers, parse_cookies(headers))
        token = _current.set(decision)

        async def send_with_route(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.append("X-DB-Route", decision.describe())
                response_headers.append("X-DB-Pools", ",".join(decision.pools_used) or "none")
                if method in WRITE_METHODS and message["status"] < 400:
                    response_headers.append(
                        "Set-Cookie",
                        f"{STICKY_COOKIE}=1; Max-Age={self.sticky_seconds}; Path=/; HttpOnly; SameSite=Lax",
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_route)
        finally:
            _current.reset(token)
