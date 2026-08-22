"""
app/services/user_service.py
─────────────────────────────
Business logic for user operations.

Fix applied:
  - get_user_token_history: added selectinload(Token.organization) and
    selectinload(Token.counter) so that token_history.html can access
    token.organization.name and token.counter.counter_name without
    triggering async lazy-load errors (MissingGreenlet / DetachedInstanceError).
"""

from typing import Optional

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.utils.security import hash_password, verify_password
from models.organization import Organization, OrgStatus
from models.token import Token, TokenStatus
from models.user import User


# ── Auth ──────────────────────────────────────────────────────────────────────

async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
    result = await db.execute(
        select(User).where(and_(User.email == email, User.is_active == True))  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def register_user(db: AsyncSession, data: dict) -> User:
    existing = (await db.execute(select(User).where(User.email == data["email"]))).scalar_one_or_none()
    if existing:
        raise ValueError("Email already registered")

    user = User(
        name=data["name"],
        email=data["email"],
        mobile=data["mobile"],
        address=data.get("address"),
        password_hash=hash_password(data["password"]),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(db: AsyncSession, user: User, data: dict) -> User:
    for key, val in data.items():
        if val is not None:
            setattr(user, key, val)
    await db.commit()
    await db.refresh(user)
    return user


async def change_user_password(db: AsyncSession, user: User, current: str, new: str) -> bool:
    if not verify_password(current, user.password_hash):
        return False
    user.password_hash = hash_password(new)
    await db.commit()
    return True


# ── Organization search ───────────────────────────────────────────────────────

async def search_organizations(
    db: AsyncSession,
    query: Optional[str] = None,
    org_type: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Organization], int]:
    stmt = select(Organization).where(Organization.status == OrgStatus.APPROVED)

    if query:
        stmt = stmt.where(Organization.name.ilike(f"%{query}%"))
    if org_type:
        stmt = stmt.where(Organization.org_type == org_type)
    if city:
        stmt = stmt.where(Organization.city.ilike(f"%{city}%"))
    if state:
        stmt = stmt.where(Organization.state.ilike(f"%{state}%"))

    count_q = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_q)).scalar()
    stmt = stmt.offset((page - 1) * per_page).limit(per_page).order_by(Organization.name)
    orgs = (await db.execute(stmt)).scalars().all()
    return list(orgs), total


# ── Token history ─────────────────────────────────────────────────────────────

async def get_user_token_history(
    db: AsyncSession, user_id: int, page: int = 1, per_page: int = 20
) -> tuple[list[Token], int]:
    """
    FIX (ISSUE-04): Added selectinload for Token.organization and Token.counter.
    The token_history.html template accesses token.organization.name and
    token.counter.counter_name. Without eager loading these relationships,
    SQLAlchemy async raises MissingGreenlet when Jinja2 traverses the lazy attrs.
    """
    from models.counter import Counter
    stmt = select(Token).where(Token.user_id == user_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    stmt = (
        stmt
        .options(
            selectinload(Token.organization),
            selectinload(Token.counter),
        )
        .order_by(Token.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    tokens = (await db.execute(stmt)).scalars().all()
    return list(tokens), total


async def get_user_active_tokens(db: AsyncSession, user_id: int) -> list[Token]:
    result = await db.execute(
        select(Token).where(
            and_(
                Token.user_id == user_id,
                Token.status.in_([TokenStatus.WAITING, TokenStatus.CALLED,
                                   TokenStatus.IN_PROGRESS, TokenStatus.ON_HOLD])
            )
        ).order_by(Token.created_at.desc())
    )
    return list(result.scalars().all())
