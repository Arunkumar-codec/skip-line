"""
app/services/knowledge_service.py
─────────────────────────────────
Phase 7 — Knowledge Solutions (KB-001..002).

Organizations maintain their own templates; platform admins maintain
global ones (organization_id NULL). Sensitive (financial) items require
the explicit human-review path built into support_service.propose_solution.
"""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings  # noqa: F401 — conventions match other services
from app.services.audit_service import log_audit_event
from models.knowledge import KB_CATEGORIES, KnowledgeSolution


async def create_solution(
    db: AsyncSession,
    organization_id: Optional[int],
    actor_type: str,
    actor_id: Optional[int],
    category: str,
    title: str,
    body: str,
    audience: str = "user",
    sensitive: bool = False,
    is_platform: bool = False,
) -> KnowledgeSolution:
    if category not in KB_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown knowledge category")
    if is_platform and actor_type != "admin":
        raise PermissionError("Only platform admins maintain global templates")
    solution = KnowledgeSolution(
        organization_id=None if is_platform else organization_id,
        category=category,
        title=title,
        body=body,
        audience=audience,
        sensitive=sensitive,
        created_by_type=actor_type,
        created_by_id=actor_id,
    )
    db.add(solution)
    await db.commit()
    await db.refresh(solution)
    await log_audit_event(
        db, action="knowledge_solution_created", actor_type=actor_type,
        actor_id=actor_id, organization_id=organization_id,
        target_type="knowledge_solution", target_id=solution.id,
        details={"category": category, "sensitive": sensitive},
    )
    return solution


async def list_solutions(
    db: AsyncSession,
    organization_id: Optional[int],
    platform_admin: bool = False,
) -> list[KnowledgeSolution]:
    if platform_admin:
        query = select(KnowledgeSolution).where(KnowledgeSolution.is_active.is_(True))
    else:
        # org sees its templates + platform globals
        query = select(KnowledgeSolution).where(
            or_(
                KnowledgeSolution.organization_id == organization_id,
                KnowledgeSolution.organization_id.is_(None),
            ),
            KnowledgeSolution.is_active.is_(True),
        )
    query = query.order_by(KnowledgeSolution.category, KnowledgeSolution.id)
    return list((await db.execute(query)).scalars().all())


async def deactivate_solution(
    db: AsyncSession,
    solution_id: int,
    actor_type: str,
    actor_id: Optional[int],
    organization_id: Optional[int] = None,
    platform_admin: bool = False,
) -> KnowledgeSolution:
    solution = await db.get(KnowledgeSolution, solution_id)
    if solution is None or not solution.is_active:
        raise LookupError("Solution not found")
    if not platform_admin:
        if actor_type != "organization" or solution.organization_id != organization_id:
            raise PermissionError("You can only deactivate your own templates")
    solution.is_active = False
    await db.commit()
    await log_audit_event(
        db, action="knowledge_solution_deactivated", actor_type=actor_type,
        actor_id=actor_id, organization_id=organization_id,
        target_type="knowledge_solution", target_id=solution.id,
    )
    await db.refresh(solution)
    return solution
