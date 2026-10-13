from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    primary_dsn: str = "postgresql+asyncpg://pathly:pathly@localhost:5432/pathly"
    replica_dsns: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["postgresql+asyncpg://pathly:pathly@localhost:5433/pathly"]
    )
    redis_url: str = "redis://localhost:6379/0"
    duckdb_path: str = "analytics.duckdb"
    geoip_db_path: str = "/data/GeoLite2-Country.mmdb"

    primary_pool_size: int = 10
    replica_pool_size: int = 20
    replica_max_lag_bytes: int = 16 * 1024 * 1024
    replica_health_interval_s: float = 2.0
    sticky_primary_after_write_s: int = 3

    jwt_secret: str = "change-me-in-production-please-32-bytes-min"
    jwt_ttl_minutes: int = 60 * 12

    public_base_url: str = "http://localhost:8000"
    trust_proxy_headers: bool = True
    visitor_salt: str = "change-me"

    hash_length: int = 7
    cache_ttl_s: int = 3600
    negative_cache_ttl_s: int = 30

    clicks_stream: str = "clicks"
    clicks_stream_maxlen: int = 1_000_000
    ingestor_group: str = "olap-ingestor"
    ingestor_batch_size: int = 500
    ingestor_block_ms: int = 1000
    ingestor_reclaim_idle_ms: int = 60_000

    demo_email: str = "demo@pathly.test"
    demo_password: str = "pathly-demo-123"

    @field_validator("replica_dsns", mode="before")
    @classmethod
    def split_replica_dsns(cls, value: object) -> object:
        if isinstance(value, str):
            return [dsn.strip() for dsn in value.split(",") if dsn.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
