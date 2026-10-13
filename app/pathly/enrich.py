import hashlib
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from user_agents import parse as parse_user_agent

log = logging.getLogger("pathly.enrich")

UNKNOWN_COUNTRY = "ZZ"
DIRECT = "(direct)"


class ClickRow(NamedTuple):
    event_id: str
    hash: str
    ts_ms: int
    country: str
    referrer_host: str
    device: str
    browser: str
    os: str
    is_bot: bool
    visitor_id: str


class UserAgentInfo(NamedTuple):
    device: str
    browser: str
    os: str
    is_bot: bool


@lru_cache(maxsize=8192)
def describe_user_agent(raw: str) -> UserAgentInfo:
    if not raw:
        return UserAgentInfo("unknown", "unknown", "unknown", False)
    agent = parse_user_agent(raw)
    if agent.is_bot:
        device = "bot"
    elif agent.is_tablet:
        device = "tablet"
    elif agent.is_mobile:
        device = "mobile"
    elif agent.is_pc:
        device = "desktop"
    else:
        device = "other"
    return UserAgentInfo(device, agent.browser.family or "Other", agent.os.family or "Other", agent.is_bot)


def referrer_host(raw: str) -> str:
    if not raw:
        return DIRECT
    host = (urlsplit(raw).hostname or "").lower()
    return host.removeprefix("www.") or DIRECT


class ClickEnricher:
    def __init__(self, geoip_db_path: str, visitor_salt: str) -> None:
        self.visitor_salt = visitor_salt.encode()
        self.geoip = None
        if Path(geoip_db_path).is_file():
            import geoip2.database

            self.geoip = geoip2.database.Reader(geoip_db_path)
            log.info("GeoIP database loaded from %s", geoip_db_path)

    def country_for(self, ip: str, hint: str) -> str:
        if self.geoip is not None and ip:
            try:
                code = self.geoip.country(ip).country.iso_code
                if code:
                    return code
            except Exception:
                pass
        return hint if len(hint) == 2 and hint.isalpha() else UNKNOWN_COUNTRY

    def visitor_id(self, ip: str, user_agent: str) -> str:
        digest = hashlib.blake2b(f"{ip}|{user_agent}".encode(), key=self.visitor_salt[:64], digest_size=8)
        return digest.hexdigest()

    def enrich(self, event_id: str, fields: dict[str, str]) -> ClickRow:
        user_agent = fields.get("ua", "")
        ip = fields.get("ip", "")
        agent = describe_user_agent(user_agent)
        try:
            ts_ms = int(fields.get("ts", ""))
        except ValueError:
            ts_ms = int(time.time() * 1000)
        return ClickRow(
            event_id=event_id,
            hash=fields.get("hash", ""),
            ts_ms=ts_ms,
            country=self.country_for(ip, fields.get("country", "").upper()),
            referrer_host=referrer_host(fields.get("ref", "")),
            device=agent.device,
            browser=agent.browser,
            os=agent.os,
            is_bot=agent.is_bot,
            visitor_id=self.visitor_id(ip, user_agent),
        )
