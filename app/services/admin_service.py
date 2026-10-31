
"""
app/services/admin_service.py
──────────────────────────────
Business logic for admin operations: org approval, analytics, user management.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.security import hash_password, verify_password
from models.admin import Admin
from models.organization import Organization, OrgStatus
from models.token import Token, TokenStatus
from models.user import User
from models.payment import Payment, PaymentStatus

MAX_SUPER_ADMIN_ACCOUNTS = 4


async def get_super_admin_count(db: AsyncSession, active_only: bool = True) -> int:
    """Return the total count of Super Admin accounts."""
    query = select(func.count()).select_from(Admin)
    if active_only:
        query = query.where(Admin.is_active == True)  # noqa: E712
    result = await db.execute(query)
    return result.scalar() or 0


async def create_super_admin(
    db: AsyncSession, name: str, email: str, password: str
) -> Admin:
    """Provision a new Super Admin account adhering strictly to the 4-account limit."""
    active_count = await get_super_admin_count(db, active_only=True)
    if active_count >= MAX_SUPER_ADMIN_ACCOUNTS:
        raise ValueError(
            f"Platform constraint violation: Exactly {MAX_SUPER_ADMIN_ACCOUNTS} authorized "
            "Platform Super Admin accounts permitted. To replace an administrator, deactivate "
            "an existing account first."
        )

    clean_email = email.strip().lower()
    existing = (
        await db.execute(select(Admin).where(Admin.email == clean_email))
    ).scalar_one_or_none()
    if existing:
        raise ValueError("Admin email already registered")

    admin = Admin(
        name=name.strip(),
        email=clean_email,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def deactivate_super_admin(db: AsyncSession, admin_id: int) -> Admin:
    """Deactivate an authorized Super Admin account for replacement or incident recovery."""
    admin = await db.get(Admin, admin_id)
    if not admin:
        raise ValueError("Admin not found")
    admin.is_active = False
    await db.commit()
    await db.refresh(admin)
    return admin


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def seed_default_admin(db: Optional[AsyncSession] = None) -> None:
    """Create the default admin account if no admins exist.

    If an explicit db session is provided (e.g. in tests), it is used directly.
    Otherwise, creates and manages its own session from AsyncSessionLocal.
    """
    if db is not None:
        result = await db.execute(select(func.count()).select_from(Admin))
        count = result.scalar()
        if count == 0:
            if not settings.admin_password:
                logger.warning(
                    "ADMIN_PASSWORD is not configured — skipping default "
                    "admin seeding. Set ADMIN_PASSWORD and restart to "
                    "provision the first Super Admin."
                )
                return
            admin = Admin(
                name=settings.admin_name,
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Default admin seeded: {}", settings.admin_email)
        return

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(Admin))
        count = result.scalar()
        if count == 0:
            if not settings.admin_password:
                logger.warning(
                    "ADMIN_PASSWORD is not configured — skipping default "
                    "admin seeding. Set ADMIN_PASSWORD and restart to "
                    "provision the first Super Admin."
                )
                return
            admin = Admin(
                name=settings.admin_name,
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                is_active=True,
            )
            session.add(admin)
            await session.commit()
            logger.info("Default admin seeded: {}", settings.admin_email)


# ── Authentication ─────────────────────────────────────────────────────────────

async def authenticate_admin(
    db: AsyncSession, email: str, password: str
) -> Optional[Admin]:
    result = await db.execute(select(Admin).where(Admin.email == email))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


async def change_admin_password(
    db: AsyncSession,
    admin: Admin,
    current_password: str,
    new_password: str,
) -> bool:
    if not verify_password(current_password, admin.password_hash):
        return False
    admin.password_hash = hash_password(new_password)
    await db.commit()
    return True


# ── Organization management ───────────────────────────────────────────────────

async def get_all_organizations(
    db: AsyncSession,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Organization], int]:
    query = select(Organization)
    if status:
        query = query.where(Organization.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar()

    query = (
        query.offset((page - 1) * per_page)
        .limit(per_page)
        .order_by(Organization.created_at.desc())
    )
    orgs = (await db.execute(query)).scalars().all()
    return list(orgs), total


async def approve_organization(
    db: AsyncSession, org_id: int, admin: Admin
) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")

    org.status = OrgStatus.APPROVED
    org.approved_by_admin_id = admin.id
    org.approved_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(org)
    logger.info("Admin {} approved org {}", admin.id, org_id)
    return org


async def reject_organization(
    db: AsyncSession, org_id: int, admin: Admin
) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")

    org.status = OrgStatus.REJECTED
    org.approved_by_admin_id = admin.id

    await db.commit()
    await db.refresh(org)
    return org


async def suspend_organization(
    db: AsyncSession, org_id: int
) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")

    org.status = OrgStatus.SUSPENDED

    await db.commit()
    await db.refresh(org)
    return org


async def delete_organization(
    db: AsyncSession, org_id: int
) -> None:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")

    await db.delete(org)
    await db.commit()


# ── Dashboard analytics ────────────────────────────────────────────────────────

async def get_dashboard_stats(db: AsyncSession) -> dict:
    """Aggregate statistics for the admin dashboard."""

    total_users = (
        await db.execute(select(func.count()).select_from(User))
    ).scalar()

    total_orgs = (
        await db.execute(select(func.count()).select_from(Organization))
    ).scalar()

    pending_orgs = (
        await db.execute(
            select(func.count())
            .select_from(Organization)
            .where(Organization.status == OrgStatus.PENDING)
        )
    ).scalar()

    approved_orgs = (
        await db.execute(
            select(func.count())
            .select_from(Organization)
            .where(Organization.status == OrgStatus.APPROVED)
        )
    ).scalar()

    total_tokens = (
        await db.execute(select(func.count()).select_from(Token))
    ).scalar()

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_tokens = (
        await db.execute(
            select(func.count())
            .select_from(Token)
            .where(Token.created_at >= today_start)
        )
    ).scalar()

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    monthly_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                and_(
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= month_start,
                )
            )
        )
    ).scalar()

    total_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.PAID
            )
        )
    ).scalar()

    return {
        "total_users": total_users,
        "total_organizations": total_orgs,
        "pending_organizations": pending_orgs,
        "approved_organizations": approved_orgs,
        "total_tokens": total_tokens,
        "today_tokens": today_tokens,
        "monthly_revenue": float(monthly_revenue),
        "total_revenue": float(total_revenue),
    }
