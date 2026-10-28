"""
app/services/catalogue_service.py
─────────────────────────────────
Service Catalogue, Sub-services hierarchy, and Multi-Service Counter Routing engine.
Features:
- Sub-service management and server-side pricing/duration rollups
- Counter-Service multi-mapping (counter_services M:N) with backward-compatible fallback
- Routing algorithm to find active eligible counters for any service
- Branch-scoped catalogue filtering
"""

from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.counter import Counter, CounterStatus
from models.counter_service import CounterService
from models.service import Service
from models.sub_service import SubService


# ── Sub-services CRUD & Calculations ──────────────────────────────────────────

async def get_sub_service_by_id(db: AsyncSession, sub_service_id: int) -> Optional[SubService]:
    """Retrieve a sub-service by ID."""
    return await db.get(SubService, sub_service_id)


async def list_sub_services(
    db: AsyncSession, service_id: int, active_only: bool = False
) -> list[SubService]:
    """List all sub-services belonging to a service."""
    stmt = select(SubService).where(SubService.service_id == service_id)
    if active_only:
        stmt = stmt.where(SubService.is_active.is_(True))
    stmt = stmt.order_by(SubService.display_order, SubService.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_sub_service(
    db: AsyncSession,
    service_id: int,
    name: str,
    code: str,
    description: Optional[str] = None,
    additional_price: Decimal = Decimal("0.00"),
    additional_minutes: int = 0,
    is_active: bool = True,
    display_order: int = 0,
) -> SubService:
    """Create a new sub-service under a parent service."""
    service = await db.get(Service, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parent service not found"
        )

    # Check unique code within service
    existing_stmt = select(SubService).where(
        SubService.service_id == service_id,
        SubService.code == code.strip().upper(),
    )
    existing = await db.execute(existing_stmt)
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sub-service code '{code}' already exists for this service.",
        )

    sub = SubService(
        service_id=service_id,
        name=name.strip(),
        code=code.strip().upper(),
        description=description,
        additional_price=additional_price,
        additional_minutes=additional_minutes,
        is_active=is_active,
        display_order=display_order,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def update_sub_service(
    db: AsyncSession,
    sub_service_id: int,
    name: Optional[str] = None,
    code: Optional[str] = None,
    description: Optional[str] = None,
    additional_price: Optional[Decimal] = None,
    additional_minutes: Optional[int] = None,
    is_active: Optional[bool] = None,
    display_order: Optional[int] = None,
) -> SubService:
    """Update an existing sub-service."""
    sub = await db.get(SubService, sub_service_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sub-service not found"
        )

    if code is not None and code.strip().upper() != sub.code:
        # Check uniqueness
        existing_stmt = select(SubService).where(
            SubService.service_id == sub.service_id,
            SubService.code == code.strip().upper(),
            SubService.id != sub.id,
        )
        existing = await db.execute(existing_stmt)
        if existing.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Sub-service code '{code}' already exists.",
            )
        sub.code = code.strip().upper()

    if name is not None:
        sub.name = name.strip()
    if description is not None:
        sub.description = description
    if additional_price is not None:
        sub.additional_price = additional_price
    if additional_minutes is not None:
        sub.additional_minutes = additional_minutes
    if is_active is not None:
        sub.is_active = is_active
    if display_order is not None:
        sub.display_order = display_order

    await db.commit()
    await db.refresh(sub)
    return sub


async def delete_sub_service(db: AsyncSession, sub_service_id: int) -> bool:
    """Delete a sub-service."""
    sub = await db.get(SubService, sub_service_id)
    if not sub:
        return False
    await db.delete(sub)
    await db.commit()
    return True


def calculate_service_rollup(
    base_service: Service,
    selected_sub_services: list[SubService],
) -> dict:
    """
    Compute total price and estimated duration for a service + selected sub-services.
    Rollup formula:
      total_price = base_service.price + sum(sub.additional_price)
      total_minutes = base_service.estimated_minutes + sum(sub.additional_minutes)
    """
    total_price = base_service.price
    total_minutes = base_service.estimated_minutes

    for sub in selected_sub_services:
        total_price += sub.additional_price
        total_minutes += sub.additional_minutes

    return {
        "service_id": base_service.id,
        "service_name": base_service.name,
        "base_price": float(base_service.price),
        "base_minutes": base_service.estimated_minutes,
        "total_price": float(total_price),
        "total_minutes": total_minutes,
        "sub_services_count": len(selected_sub_services),
    }


# ── Multi-Service Counter Management ──────────────────────────────────────────

async def assign_services_to_counter(
    db: AsyncSession,
    counter_id: int,
    service_ids: list[int],
    primary_service_id: Optional[int] = None,
) -> list[CounterService]:
    """
    Assign multiple services to a counter.
    Also synchronizes counter.service_id as the primary service for backward compatibility.
    """
    counter = await db.get(Counter, counter_id)
    if not counter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found"
        )

    # Delete existing mappings
    await db.execute(delete(CounterService).where(CounterService.counter_id == counter_id))

    if not service_ids:
        counter.service_id = None
        await db.commit()
        return []

    # Validate that all assigned services belong to the counter's organization
    valid_services_stmt = select(Service.id).where(
        Service.id.in_(service_ids),
        Service.organization_id == counter.organization_id,
    )
    valid_res = await db.execute(valid_services_stmt)
    valid_service_ids = set(valid_res.scalars().all())
    invalid_ids = set(service_ids) - valid_service_ids
    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Services {sorted(list(invalid_ids))} do not belong to this organization.",
        )

    # If primary not specified, use first service
    if primary_service_id is None or primary_service_id not in service_ids:
        primary_service_id = service_ids[0]

    counter.service_id = primary_service_id

    created_mappings = []
    for sid in service_ids:
        cs = CounterService(
            counter_id=counter_id,
            service_id=sid,
            is_primary=(sid == primary_service_id),
        )
        db.add(cs)
        created_mappings.append(cs)

    await db.commit()
    return created_mappings


async def get_counter_services(db: AsyncSession, counter_id: int) -> list[Service]:
    """Retrieve all services handled by a counter."""
    stmt = (
        select(Service)
        .join(CounterService, CounterService.service_id == Service.id)
        .where(CounterService.counter_id == counter_id)
    )
    result = await db.execute(stmt)
    services = list(result.scalars().all())

    # Fallback to single legacy counter.service_id if no M:N rows exist
    if not services:
        counter = await db.get(Counter, counter_id)
        if counter and counter.service_id:
            legacy_service = await db.get(Service, counter.service_id)
            if legacy_service:
                services = [legacy_service]

    return services


async def get_eligible_counters_for_service(
    db: AsyncSession,
    service_id: int,
    organization_id: int,
    branch_id: Optional[int] = None,
) -> list[Counter]:
    """
    Find all active counters eligible to serve a given service.
    Queries counters matched via `counter_services` table OR `counters.service_id`.
    """
    # 1. Counters mapped in counter_services
    mapped_counter_ids_stmt = select(CounterService.counter_id).where(
        CounterService.service_id == service_id
    )

    stmt = (
        select(Counter)
        .where(
            Counter.organization_id == organization_id,
            Counter.status == CounterStatus.ACTIVE,
            or_(
                Counter.id.in_(mapped_counter_ids_stmt),
                Counter.service_id == service_id,
            ),
        )
    )

    if branch_id is not None:
        stmt = stmt.where(
            or_(Counter.branch_id == branch_id, Counter.branch_id.is_(None))
        )

    stmt = stmt.order_by(Counter.counter_number)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_service_catalogue(
    db: AsyncSession,
    organization_id: int,
    branch_id: Optional[int] = None,
    include_sub_services: bool = True,
) -> list[Service]:
    """
    Get full service catalogue for an organization, optionally scoped by branch.
    Includes sub-services hierarchy.
    """
    stmt = select(Service).where(
        Service.organization_id == organization_id,
        Service.is_active.is_(True),
    )
    if branch_id is not None:
        stmt = stmt.where(
            or_(Service.branch_id == branch_id, Service.branch_id.is_(None))
        )

    if include_sub_services:
        stmt = stmt.options(selectinload(Service.sub_services))

    stmt = stmt.order_by(Service.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())
