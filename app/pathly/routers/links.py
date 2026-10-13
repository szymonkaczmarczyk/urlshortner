from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pathly.deps import CurrentUser, ServicesDep, SessionDep, UserDep
from pathly.models import Link
from pathly.schemas import LinkCreate, LinkOut, LinkUpdate
from pathly.shortcodes import generate_hash, is_reserved

router = APIRouter(prefix="/api/links", tags=["links"])

MAX_HASH_ATTEMPTS = 6


def to_out(link: Link, base_url: str) -> LinkOut:
    return LinkOut(
        hash=link.hash,
        short_url=f"{base_url.rstrip('/')}/{link.hash}",
        target_url=link.target_url,
        is_active=link.is_active,
        expires_at=link.expires_at,
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


async def owned_link(session: AsyncSession, link_hash: str, user: CurrentUser) -> Link:
    link = await session.scalar(select(Link).where(Link.hash == link_hash))
    if link is None or link.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    return link


async def insert_link(session: AsyncSession, link_hash: str, payload: LinkCreate, user: CurrentUser) -> Link | None:
    statement = (
        insert(Link)
        .values(
            hash=link_hash,
            target_url=str(payload.target_url),
            owner_id=user.id,
            expires_at=payload.expires_at,
        )
        .on_conflict_do_nothing(index_elements=[Link.hash])
        .returning(Link)
    )
    return (await session.execute(statement)).scalar_one_or_none()


@router.post("", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
async def create_link(payload: LinkCreate, session: SessionDep, services: ServicesDep, user: UserDep) -> LinkOut:
    if payload.alias:
        if is_reserved(payload.alias):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "alias is reserved")
        link = await insert_link(session, payload.alias, payload, user)
        if link is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "alias already taken")
    else:
        link = None
        for _ in range(MAX_HASH_ATTEMPTS):
            link = await insert_link(session, generate_hash(services.settings.hash_length), payload, user)
            if link is not None:
                break
        if link is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not allocate a unique hash")

    await session.commit()
    await services.cache.invalidate(link.hash)
    return to_out(link, services.settings.public_base_url)


@router.get("", response_model=list[LinkOut])
async def list_links(session: SessionDep, services: ServicesDep, user: UserDep) -> list[LinkOut]:
    links = await session.scalars(
        select(Link).where(Link.owner_id == user.id).order_by(Link.created_at.desc()).limit(500)
    )
    return [to_out(link, services.settings.public_base_url) for link in links]


@router.get("/{link_hash}", response_model=LinkOut)
async def get_link(link_hash: str, session: SessionDep, services: ServicesDep, user: UserDep) -> LinkOut:
    return to_out(await owned_link(session, link_hash, user), services.settings.public_base_url)


@router.patch("/{link_hash}", response_model=LinkOut)
async def update_link(
    link_hash: str, payload: LinkUpdate, session: SessionDep, services: ServicesDep, user: UserDep
) -> LinkOut:
    link = await owned_link(session, link_hash, user)
    changes = payload.model_dump(exclude_unset=True)
    if "target_url" in changes and changes["target_url"] is not None:
        link.target_url = str(payload.target_url)
    if "is_active" in changes and changes["is_active"] is not None:
        link.is_active = changes["is_active"]
    if "expires_at" in changes:
        link.expires_at = changes["expires_at"]
    await session.commit()
    await session.refresh(link)
    await services.cache.invalidate(link.hash)
    return to_out(link, services.settings.public_base_url)


@router.delete("/{link_hash}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(link_hash: str, session: SessionDep, services: ServicesDep, user: UserDep) -> Response:
    link = await owned_link(session, link_hash, user)
    await session.delete(link)
    await session.commit()
    await services.cache.invalidate(link_hash)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
