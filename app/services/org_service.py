"""
app/services/org_service.py
────────────────────────────
Business logic for organization operations.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.security import hash_password, verify_password
from models.counter import Counter, CounterStatus
from models.organization import Organization, OrgStatus
from models.service import Service
from models.setting import Setting
from models.token import Token, TokenStatus
from models.payment import Payment, PaymentStatus


# ── Authentication ────────────────────────────────────────────────────────────

async def authenticate_org(db: AsyncSession, email: str, password: str) -> Optional[Organization]:
    result = await db.execute(
        select(Organization).where(
            and_(Organization.email == email, Organization.status == OrgStatus.APPROVED)
        )
    )
    org = result.scalar_one_or_none()
    if not org or not verify_password(password, org.password_hash):
        return None
    return org


# ── Registration ──────────────────────────────────────────────────────────────

async def register_organization(db: AsyncSession, data: dict) -> Organization:
    # Check uniqueness
    existing = (await db.execute(
        select(Organization).where(Organization.email == data["email"])
    )).scalar_one_or_none()
    if existing:
        raise ValueError("Email already registered")

    reg_exists = (await db.execute(
        select(Organization).where(Organization.registration_number == data["registration_number"])
    )).scalar_one_or_none()
    if reg_exists:
        raise ValueError("Registration number already in use")

    org = Organization(
        name=data["name"],
        registration_number=data["registration_number"],
        org_type=data["org_type"],
        address=data["address"],
        city=data["city"],
        state=data["state"],
        email=data["email"],
        mobile=data["mobile"],
        password_hash=hash_password(data["password"]),
        description=data.get("description"),
        status=OrgStatus.PENDING,
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def change_org_password(
    db: AsyncSession, org: Organization, current: str, new: str
) -> bool:
    if not verify_password(current, org.password_hash):
        return False
    org.password_hash = hash_password(new)
    await db.commit()
    return True


# ── Dashboard analytics ───────────────────────────────────────────────────────

async def get_org_dashboard_stats(db: AsyncSession, org_id: int) -> dict:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total_tokens = (await db.execute(
        select(func.count()).select_from(Token).where(Token.organization_id == org_id)
    )).scalar()

    active_tokens = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(Token.organization_id == org_id,
                 Token.status.in_([TokenStatus.WAITING, TokenStatus.CALLED, TokenStatus.IN_PROGRESS]))
        )
    )).scalar()

    completed_today = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(Token.organization_id == org_id,
                 Token.status == TokenStatus.COMPLETED,
                 Token.completed_at >= today_start)
        )
    )).scalar()

    revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(and_(Payment.organization_id == org_id, Payment.status == PaymentStatus.PAID))
    )).scalar()

    # Average service time for completed tokens (minutes)
    avg_result = (await db.execute(
        select(func.avg(
            func.timestampdiff(
                text("MINUTE"),
                Token.called_at,
                Token.completed_at
            )
        )).where(
            and_(Token.organization_id == org_id,
                 Token.status == TokenStatus.COMPLETED,
                 Token.called_at.isnot(None),
                 Token.completed_at.isnot(None))
        )
    )).scalar()

    return {
        "total_tokens": total_tokens,
        "active_tokens": active_tokens,
        "completed_today": completed_today,
        "revenue": float(revenue),
        "avg_wait_minutes": round(float(avg_result or 0), 1),
    }


# ── Counter management ────────────────────────────────────────────────────────

async def list_counters(db: AsyncSession, org_id: int) -> list[Counter]:
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Counter)
        .where(Counter.organization_id == org_id)
        .options(selectinload(Counter.counter_services))
        .order_by(Counter.counter_number)
    )
    counters = list(result.scalars().all())
    for c in counters:
        c.service_ids = [cs.service_id for cs in c.counter_services]
    return counters


async def create_counter(db: AsyncSession, org_id: int, data: dict) -> Counter:
    from app.services import catalogue_service
    service_ids = data.pop("service_ids", None)
    counter = Counter(organization_id=org_id, **data)
    db.add(counter)
    await db.commit()
    await db.refresh(counter)

    # Assign multi-service mappings
    if service_ids:
        await catalogue_service.assign_services_to_counter(
            db, counter.id, service_ids, primary_service_id=counter.service_id
        )
        counter.service_ids = service_ids
    elif counter.service_id:
        await catalogue_service.assign_services_to_counter(
            db, counter.id, [counter.service_id], primary_service_id=counter.service_id
        )
        counter.service_ids = [counter.service_id]
    else:
        counter.service_ids = []

    return counter


async def update_counter(db: AsyncSession, counter_id: int, org_id: int, data: dict) -> Counter:
    from app.services import catalogue_service
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != org_id:
        raise ValueError("Counter not found")

    service_ids = data.pop("service_ids", None)
    for key, val in data.items():
        if val is not None:
            setattr(counter, key, val)
    await db.commit()
    await db.refresh(counter)

    if service_ids is not None:
        await catalogue_service.assign_services_to_counter(
            db, counter.id, service_ids, primary_service_id=counter.service_id
        )
        counter.service_ids = service_ids
    elif counter.service_id:
        await catalogue_service.assign_services_to_counter(
            db, counter.id, [counter.service_id], primary_service_id=counter.service_id
        )
        counter.service_ids = [counter.service_id]
    else:
        counter.service_ids = []

    return counter


async def delete_counter(db: AsyncSession, counter_id: int, org_id: int) -> None:
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != org_id:
        raise ValueError("Counter not found")
    await db.delete(counter)
    await db.commit()


# ── Service management ────────────────────────────────────────────────────────

async def list_services(db: AsyncSession, org_id: int) -> list[Service]:
    result = await db.execute(
        select(Service).where(Service.organization_id == org_id).order_by(Service.name)
    )
    return list(result.scalars().all())


async def create_service(db: AsyncSession, org_id: int, data: dict) -> Service:
    service = Service(organization_id=org_id, **data)
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


async def update_service(db: AsyncSession, service_id: int, org_id: int, data: dict) -> Service:
    service = await db.get(Service, service_id)
    if not service or service.organization_id != org_id:
        raise ValueError("Service not found")
    for key, val in data.items():
        if val is not None:
            setattr(service, key, val)
    await db.commit()
    await db.refresh(service)
    return service


async def delete_service(db: AsyncSession, service_id: int, org_id: int) -> None:
    service = await db.get(Service, service_id)
    if not service or service.organization_id != org_id:
        raise ValueError("Service not found")
    await db.delete(service)
    await db.commit()


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_org_settings(db: AsyncSession, org_id: int) -> dict:
    result = await db.execute(
        select(Setting).where(Setting.organization_id == org_id)
    )
    return {s.key: s.value for s in result.scalars().all()}


async def upsert_setting(db: AsyncSession, org_id: int, key: str, value: str) -> None:
    result = await db.execute(
        select(Setting).where(and_(Setting.organization_id == org_id, Setting.key == key))
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(Setting(organization_id=org_id, key=key, value=value))
    await db.commit()
