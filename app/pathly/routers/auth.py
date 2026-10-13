from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pathly.deps import ServicesDep, SessionDep, UserDep
from pathly.models import User
from pathly.schemas import Credentials, MeOut, TokenOut
from pathly.security import hash_password, issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def token_for(user_id: int, email: str, services: ServicesDep) -> TokenOut:
    settings = services.settings
    return TokenOut(
        access_token=issue_token(user_id, email, settings.jwt_secret, settings.jwt_ttl_minutes),
        email=email,
    )


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(payload: Credentials, session: SessionDep, services: ServicesDep) -> TokenOut:
    statement = (
        insert(User)
        .values(email=payload.email, password_hash=hash_password(payload.password))
        .on_conflict_do_nothing(index_elements=[User.email])
        .returning(User.id)
    )
    user_id = (await session.execute(statement)).scalar_one_or_none()
    if user_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    await session.commit()
    return token_for(user_id, payload.email, services)


@router.post("/login", response_model=TokenOut)
async def login(payload: Credentials, session: SessionDep, services: ServicesDep) -> TokenOut:
    user = await session.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    return token_for(user.id, user.email, services)


@router.get("/me", response_model=MeOut)
async def me(user: UserDep) -> MeOut:
    return MeOut(id=user.id, email=user.email)
