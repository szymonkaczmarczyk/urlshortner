from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from pathly.analytics import AnalyticsStore
from pathly.cache import LinkCache
from pathly.config import Settings
from pathly.db import Database
from pathly.events import ClickPublisher
from pathly.ingestor import ClickIngestor
from pathly.security import read_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class Services:
    settings: Settings
    db: Database
    cache: LinkCache
    publisher: ClickPublisher
    analytics: AnalyticsStore
    ingestor: ClickIngestor


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


async def routed_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    async with services.db.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(routed_session)]


def current_user(
    services: ServicesDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = read_token(credentials.credentials, services.settings.jwt_secret)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return CurrentUser(id=int(claims["sub"]), email=claims.get("email", ""))


UserDep = Annotated[CurrentUser, Depends(current_user)]
