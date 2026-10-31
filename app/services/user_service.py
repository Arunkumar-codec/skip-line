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

from datetime import date
from math import asin, cos, radians, sin, sqrt
from typing import Optional

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.utils.security import hash_password, verify_password
from models.branch import Branch
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import Organization, OrgStatus
from models.service import Service
from models.time_slot import TimeSlot
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

_EARTH_RADIUS_KM = 6371.0088
_DISTANCE_FETCH_CAP = 1000   # safety cap for in-Python distance handling


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres between two (lat, lng) points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(min(sqrt(a), 1.0))


async def search_organizations(
    db: AsyncSession,
    query: Optional[str] = None,
    org_type: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    service_id: Optional[int] = None,
    pincode: Optional[str] = None,
    slot_date: Optional[date] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_km: Optional[float] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Organization], int]:
    """Approved-organization discovery (SRCH-001/002, Decision 15).

    Base filters (query, org_type, city, state) are SQL-backed. Optional
    Phase 3 filters:
    - `service_id` — organization offers this active service.
    - `pincode` — organization has an active branch at this PIN code.
    - `slot_date` — organization has an active slot-mode counter with at
      least one slot with remaining capacity on this date.
    - `lat`/`lng` (+ optional `radius_km`) — distance from the caller to
      each organization's nearest geocoded active branch (Haversine).
      When `radius_km` is given, organizations with no geocoded branch are
      excluded; otherwise they are returned with `distance_km = None`.
      With coordinates supplied, results are sorted nearest-first.
    """
    stmt = select(Organization).where(Organization.status == OrgStatus.APPROVED)

    if query:
        stmt = stmt.where(Organization.name.ilike(f"%{query}%"))
    if org_type:
        stmt = stmt.where(Organization.org_type == org_type)
    if city:
        stmt = stmt.where(Organization.city.ilike(f"%{city}%"))
    if state:
        stmt = stmt.where(Organization.state.ilike(f"%{state}%"))
    if service_id is not None:
        stmt = stmt.where(
            Organization.services.any(
                and_(Service.id == service_id, Service.is_active == True)  # noqa: E712
            )
        )
    if pincode:
        stmt = stmt.where(
            Organization.branches.any(
                and_(Branch.pincode == pincode, Branch.is_active == True)  # noqa: E712
            )
        )
    if slot_date is not None:
        stmt = stmt.where(
            select(TimeSlot.id)
            .join(Counter, Counter.id == TimeSlot.counter_id)
            .where(
                TimeSlot.organization_id == Organization.id,
                TimeSlot.slot_date == slot_date,
                TimeSlot.is_active.is_(True),
                TimeSlot.booked_count + TimeSlot.held_count < TimeSlot.capacity,
                Counter.queue_mode == QueueMode.SLOTS,
                Counter.status == CounterStatus.ACTIVE,
            )
            .exists()
        )

    distance_mode = lat is not None and lng is not None
    if distance_mode and radius_km is not None and radius_km <= 0:
        raise ValueError("radius_km must be greater than 0")
    if not distance_mode:
        count_q = select(func.count()).select_from(stmt.subquery())
        total = (await db.execute(count_q)).scalar()
        stmt = stmt.offset((page - 1) * per_page).limit(per_page).order_by(Organization.name)
        orgs = list((await db.execute(stmt)).scalars().all())
        for org in orgs:
            org.distance_km = None  # transient attribute (schema reads it)
        return orgs, total

    # Distance mode: fetch candidates (capped), compute the nearest geocoded
    # branch distance per organization in Python, then radius-filter, sort
    # nearest-first and paginate.
    candidates = list(
        (
            await db.execute(
                stmt.order_by(Organization.name).limit(_DISTANCE_FETCH_CAP)
            )
        ).scalars().all()
    )
    if candidates:
        branch_rows = (
            await db.execute(
                select(
                    Branch.organization_id, Branch.geo_lat, Branch.geo_lng
                ).where(
                    Branch.organization_id.in_([o.id for o in candidates]),
                    Branch.is_active == True,  # noqa: E712
                    Branch.geo_lat.is_not(None),
                    Branch.geo_lng.is_not(None),
                )
            )
        ).all()
        distances: dict[int, float] = {}
        for org_id, b_lat, b_lng in branch_rows:
            d = _haversine_km(lat, lng, b_lat, b_lng)
            if org_id not in distances or d < distances[org_id]:
                distances[org_id] = d
    else:
        distances = {}

    for org in candidates:
        org.distance_km = distances.get(org.id)  # transient attribute (schema reads it)

    if radius_km is not None:
        candidates = [o for o in candidates if o.distance_km is not None and o.distance_km <= radius_km]

    candidates.sort(key=lambda o: (o.distance_km is None, o.distance_km or 0.0, o.name))
    total = len(candidates)
    start = (page - 1) * per_page
    orgs = candidates[start:start + per_page]
    return orgs, total


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
