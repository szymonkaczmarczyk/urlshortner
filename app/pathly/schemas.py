import re
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("invalid email address")
        return value


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


class MeOut(BaseModel):
    id: int
    email: str


class LinkCreate(BaseModel):
    target_url: AnyHttpUrl
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,32}$")
    expires_at: datetime | None = None


class LinkUpdate(BaseModel):
    target_url: AnyHttpUrl | None = None
    is_active: bool | None = None
    expires_at: datetime | None = None


class LinkOut(BaseModel):
    hash: str
    short_url: str
    target_url: str
    is_active: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
