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


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def seed_default_admin() -> None:
    """Create the default admin account if no admins exist."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count()).select_from(Admin))
        count = result.scalar()
        if count == 0:
            admin = Admin(
                name=settings.admin_name,
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Default admin seeded: {}", settings.admin_email)


# ── Authentication ─────────────────────────────────────────────────────────────

async def authenticate_admin(db: AsyncSession, email: str, password: str) -> Optional[Admin]:
    result = await db.execute(select(Admin).where(Admin.email == email))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


async def change_admin_password(
    db: AsyncSession, admin: Admin, current_password: str, new_password: str
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
    query = query.offset((page - 1) * per_page).limit(per_page).order_by(Organization.created_at.desc())
    orgs = (await db.execute(query)).scalars().all()
    return list(orgs), total


async def approve_organization(db: AsyncSession, org_id: int, admin: Admin) -> Organization:
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


async def reject_organization(db: AsyncSession, org_id: int, admin: Admin) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")
    org.status = OrgStatus.REJECTED
    org.approved_by_admin_id = admin.id
    await db.commit()
    await db.refresh(org)
    return org


async def suspend_organization(db: AsyncSession, org_id: int) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")
    org.status = OrgStatus.SUSPENDED
    await db.commit()
    await db.refresh(org)
    return org


async def delete_organization(db: AsyncSession, org_id: int) -> None:
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")
    await db.delete(org)
    await db.commit()


# ── Dashboard analytics ────────────────────────────────────────────────────────

async def get_dashboard_stats(db: AsyncSession) -> dict:
    """Aggregate statistics for the admin dashboard."""

    total_users = (await db.execute(select(func.count()).select_from(User))).scalar()
    total_orgs = (await db.execute(select(func.count()).select_from(Organization))).scalar()
    pending_orgs = (await db.execute(
        select(func.count()).select_from(Organization).where(Organization.status == OrgStatus.PENDING)
    )).scalar()
    approved_orgs = (await db.execute(
        select(func.count()).select_from(Organization).where(Organization.status == OrgStatus.APPROVED)
    )).scalar()
    total_tokens = (await db.execute(select(func.count()).select_from(Token))).scalar()

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_tokens = (await db.execute(
        select(func.count()).select_from(Token).where(Token.created_at >= today_start)
    )).scalar()

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(and_(Payment.status == PaymentStatus.PAID, Payment.paid_at >= month_start))
    )).scalar()

    total_revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == PaymentStatus.PAID)
    )).scalar()

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


# ── Reports ─────────────────────────────────────────────────────────────────

async def get_revenue_report(db: AsyncSession, days: int = 30) -> dict:
    """Return revenue data aggregated by day for the last N days."""
    labels, revenue_data = [], []
    for i in range(days - 1, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        ds  = day.replace(hour=0, minute=0, second=0, microsecond=0)
        de  = ds + timedelta(days=1)
        rev = (await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                and_(Payment.status == PaymentStatus.PAID,
                     Payment.paid_at >= ds,
                     Payment.paid_at < de)
            )
        )).scalar() or 0
        labels.append(day.strftime("%d %b"))
        revenue_data.append(float(rev))

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            and_(Payment.status == PaymentStatus.PAID, Payment.paid_at >= month_start)
        )
    )).scalar() or 0
    total = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == PaymentStatus.PAID)
    )).scalar() or 0
    recent = (await db.execute(
        select(Payment).where(Payment.status == PaymentStatus.PAID)
        .order_by(Payment.paid_at.desc()).limit(20)
    )).scalars().all()

    return {
        "labels": labels,
        "revenue_data": revenue_data,
        "monthly_revenue": float(monthly),
        "total_revenue": float(total),
        "recent_payments": recent,
    }


async def get_queue_report(db: AsyncSession) -> dict:
    """Return token status breakdown and 7-day issued vs completed data."""
    status_counts: dict[str, int] = {}
    for ts in TokenStatus:
        count = (await db.execute(
            select(func.count()).select_from(Token).where(Token.status == ts)
        )).scalar() or 0
        status_counts[ts.value] = count

    labels, issued_data, completed_data = [], [], []
    for i in range(6, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        ds  = day.replace(hour=0, minute=0, second=0, microsecond=0)
        de  = ds + timedelta(days=1)
        issued = (await db.execute(
            select(func.count()).select_from(Token)
            .where(and_(Token.created_at >= ds, Token.created_at < de))
        )).scalar() or 0
        completed = (await db.execute(
            select(func.count()).select_from(Token)
            .where(and_(Token.status == TokenStatus.COMPLETED,
                        Token.completed_at >= ds, Token.completed_at < de))
        )).scalar() or 0
        labels.append(day.strftime("%a"))
        issued_data.append(issued)
        completed_data.append(completed)

    return {
        "status_counts": status_counts,
        "total_tokens": sum(status_counts.values()),
        "labels": labels,
        "issued_data": issued_data,
        "completed_data": completed_data,
    }


async def get_audit_logs(db: AsyncSession, page: int = 1, per_page: int = 50) -> tuple[list, int]:
    """Paginated admin audit log."""
    from models.audit_log import AuditLog  # local import — model may not be loaded yet
    total = (await db.execute(select(func.count()).select_from(AuditLog))).scalar() or 0
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    return list(result.scalars().all()), total
