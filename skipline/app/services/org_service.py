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
    result = await db.execute(
        select(Counter).where(Counter.organization_id == org_id).order_by(Counter.counter_number)
    )
    return list(result.scalars().all())


async def create_counter(db: AsyncSession, org_id: int, data: dict) -> Counter:
    counter = Counter(organization_id=org_id, **data)
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def update_counter(db: AsyncSession, counter_id: int, org_id: int, data: dict) -> Counter:
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != org_id:
        raise ValueError("Counter not found")
    for key, val in data.items():
        if val is not None:
            setattr(counter, key, val)
    await db.commit()
    await db.refresh(counter)
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


# ── Staff authentication & management ─────────────────────────────────────────

async def authenticate_staff(
    db: AsyncSession, email: str, password: str
) -> Optional["OrgStaff"]:
    """Authenticate a staff member (counter employee / kiosk / owner)."""
    from models.organization import OrgStaff
    from sqlalchemy import and_
    result = await db.execute(
        select(OrgStaff).where(
            and_(OrgStaff.email == email, OrgStaff.is_active == True)  # noqa: E712
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        return None
    if not verify_password(password, staff.password_hash):
        return None
    return staff


async def list_staff(db: AsyncSession, org_id: int) -> list:
    """Return all active staff for an organisation."""
    from models.organization import OrgStaff
    result = await db.execute(
        select(OrgStaff).where(OrgStaff.organization_id == org_id)
        .order_by(OrgStaff.name)
    )
    return list(result.scalars().all())


async def create_staff(db: AsyncSession, org_id: int, data: dict):
    """Create a new staff member."""
    from models.organization import OrgStaff, StaffRole
    existing = (await db.execute(
        select(OrgStaff).where(OrgStaff.email == data["email"])
    )).scalar_one_or_none()
    if existing:
        raise ValueError("Email already in use by another staff member")
    staff = OrgStaff(
        organization_id=org_id,
        name=data["name"],
        email=data["email"],
        mobile=data.get("mobile"),
        password_hash=hash_password(data["password"]),
        role=data.get("role", StaffRole.COUNTER_EMPLOYEE),
        counter_id=data.get("counter_id"),
    )
    db.add(staff)
    await db.commit()
    await db.refresh(staff)
    return staff


async def update_staff(db: AsyncSession, staff_id: int, org_id: int, data: dict):
    """Update an existing staff member."""
    from models.organization import OrgStaff
    staff = await db.get(OrgStaff, staff_id)
    if not staff or staff.organization_id != org_id:
        raise ValueError("Staff member not found")
    for k, v in data.items():
        if k == "password" and v:
            staff.password_hash = hash_password(v)
        elif v is not None:
            setattr(staff, k, v)
    await db.commit()
    await db.refresh(staff)
    return staff


async def delete_staff(db: AsyncSession, staff_id: int, org_id: int) -> None:
    from models.organization import OrgStaff
    staff = await db.get(OrgStaff, staff_id)
    if not staff or staff.organization_id != org_id:
        raise ValueError("Staff member not found")
    await db.delete(staff)
    await db.commit()


# ── Holiday / schedule management ─────────────────────────────────────────────

async def list_holidays(db: AsyncSession, org_id: int) -> list:
    from models.organization import OrgHoliday
    result = await db.execute(
        select(OrgHoliday).where(OrgHoliday.organization_id == org_id)
        .order_by(OrgHoliday.holiday_date)
    )
    return list(result.scalars().all())


async def create_holiday(db: AsyncSession, org_id: int, data: dict):
    from models.organization import OrgHoliday
    holiday = OrgHoliday(organization_id=org_id, **data)
    db.add(holiday)
    await db.commit()
    await db.refresh(holiday)
    return holiday


async def delete_holiday(db: AsyncSession, holiday_id: int, org_id: int) -> None:
    from models.organization import OrgHoliday
    holiday = await db.get(OrgHoliday, holiday_id)
    if not holiday or holiday.organization_id != org_id:
        raise ValueError("Holiday not found")
    await db.delete(holiday)
    await db.commit()


async def list_schedules(db: AsyncSession, org_id: int) -> list:
    from models.organization import OrgSchedule
    result = await db.execute(
        select(OrgSchedule).where(OrgSchedule.organization_id == org_id)
        .order_by(OrgSchedule.schedule_date)
    )
    return list(result.scalars().all())


async def create_schedule(db: AsyncSession, org_id: int, data: dict):
    from models.organization import OrgSchedule
    schedule = OrgSchedule(organization_id=org_id, **data)
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return schedule
