"""
app/services/branch_service.py
──────────────────────────────
Business logic and tenant isolation for physical organization branches.
All operations strictly enforce organization_id ownership boundaries.
"""

from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.branch import Branch


async def list_branches(
    db: AsyncSession, org_id: int, active_only: bool = False
) -> list[Branch]:
    """List all branches belonging strictly to the specified organization."""
    query = select(Branch).where(Branch.organization_id == org_id)
    if active_only:
        query = query.where(Branch.is_active == True)  # noqa: E712
    query = query.order_by(Branch.name)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_branch(db: AsyncSession, branch_id: int, org_id: int) -> Branch:
    """Retrieve a branch, verifying tenant ownership."""
    branch = await db.get(Branch, branch_id)
    if not branch or branch.organization_id != org_id:
        raise ValueError("Branch not found")
    return branch


async def create_branch(db: AsyncSession, org_id: int, data: dict) -> Branch:
    """Create a new physical branch for an organization with unique code check."""
    code = data.get("branch_code", "").strip()
    existing = (
        await db.execute(
            select(Branch).where(
                and_(Branch.organization_id == org_id, Branch.branch_code == code)
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise ValueError("Branch code already in use for this organization")

    branch = Branch(organization_id=org_id, **data)
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


async def update_branch(
    db: AsyncSession, branch_id: int, org_id: int, data: dict
) -> Branch:
    """Update a branch, strictly verifying tenant ownership and code uniqueness."""
    branch = await get_branch(db, branch_id, org_id)

    new_code = data.get("branch_code")
    if new_code and new_code != branch.branch_code:
        conflict = (
            await db.execute(
                select(Branch).where(
                    and_(
                        Branch.organization_id == org_id,
                        Branch.branch_code == new_code,
                        Branch.id != branch_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if conflict:
            raise ValueError("Branch code already in use for this organization")

    for key, val in data.items():
        if val is not None:
            setattr(branch, key, val)

    await db.commit()
    await db.refresh(branch)
    return branch


async def delete_branch(db: AsyncSession, branch_id: int, org_id: int) -> None:
    """Delete a branch, strictly verifying tenant ownership."""
    branch = await get_branch(db, branch_id, org_id)
    await db.delete(branch)
    await db.commit()
