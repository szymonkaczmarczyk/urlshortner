import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits
HASH_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED = frozenset({"api", "static", "docs", "redoc", "openapi.json", "health", "dashboard", "login", "admin"})


def generate_hash(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_hash(value: str) -> bool:
    return bool(HASH_PATTERN.fullmatch(value))


def is_reserved(value: str) -> bool:
    return value.lower() in RESERVED
