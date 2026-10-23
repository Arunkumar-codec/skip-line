"""
app/routers/org.py
───────────────────
Organization REST API endpoints (JSON).
GET routes that conflicted with views_org.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_org
from app.schemas.auth import ChangePasswordRequest
from app.schemas.branch import BranchCreate, BranchRead, BranchUpdate
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.counter import CounterCreate, CounterRead, CounterUpdate
from app.schemas.employee import EmployeeCreate, EmployeeRead, EmployeeUpdate
from app.schemas.organization import OrgRead, OrgRegisterRequest, OrgUpdateRequest
from app.schemas.rbac import PermissionRead, RoleAssignmentRead, RoleAssignRequest, RoleRead
from app.schemas.service import ServiceCreate, ServiceRead, ServiceUpdate
from app.schemas.token import TokenActionRequest, TokenRead
from app.services import (
    admin_service,
    audit_service,
    branch_service,
    employee_service,
    org_service,
    rbac_service,
    token_service,
)
from app.websocket.manager import ws_manager
from models.organization import Organization

router = APIRouter()


# ── Registration (public) ─────────────────────────────────────────────────────

@router.post("/register", response_model=APIResponse)
async def register(body: OrgRegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        org = await org_service.register_organization(db, body.model_dump())
        return APIResponse(message="Registration submitted. Pending admin approval.", data=OrgRead.model_validate(org))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Dashboard (JSON API) ──────────────────────────────────────────────────────
# Renamed from /dashboard to /api/dashboard to avoid shadowing the HTML view.

@router.get("/api/dashboard", dependencies=[Depends(get_current_org)])
async def dashboard_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    stats = await org_service.get_org_dashboard_stats(db, org.id)
    return APIResponse(data={"org": OrgRead.model_validate(org), "stats": stats})


# ── Profile ───────────────────────────────────────────────────────────────────
# Renamed GET /profile to /api/profile to avoid shadowing the HTML view.

@router.get("/api/profile", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_profile_api(org: Organization = Depends(get_current_org)):
    return APIResponse(data=OrgRead.model_validate(org))


@router.put("/profile", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_profile(
    body: OrgUpdateRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(org, k, v)
    await db.commit()
    await db.refresh(org)
    return APIResponse(data=OrgRead.model_validate(org))


@router.post("/change-password", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def change_password(
    body: ChangePasswordRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await org_service.change_org_password(db, org, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password incorrect")
    return MessageResponse(message="Password changed")


# ── Branches ──────────────────────────────────────────────────────────────────
# Renamed GET /branches to /api/branches to avoid shadowing any future HTML view.

@router.get("/api/branches", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_branches_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    branches = await branch_service.list_branches(db, org.id)
    return APIResponse(data=[BranchRead.model_validate(b) for b in branches])


@router.post("/branches", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_branch(
    body: BranchCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        branch = await branch_service.create_branch(db, org.id, body.model_dump())
        return APIResponse(message="Branch created", data=BranchRead.model_validate(branch))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/branches/{branch_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_branch_api(
    branch_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        branch = await branch_service.get_branch(db, branch_id, org.id)
        return APIResponse(data=BranchRead.model_validate(branch))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/branches/{branch_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_branch(
    branch_id: int,
    body: BranchUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        branch = await branch_service.update_branch(db, branch_id, org.id, body.model_dump(exclude_none=True))
        return APIResponse(data=BranchRead.model_validate(branch))
    except ValueError as e:
        msg = str(e)
        status_code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status_code, detail=msg)


@router.delete("/branches/{branch_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_branch(
    branch_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await branch_service.delete_branch(db, branch_id, org.id)
        await audit_service.log_audit_event(
            db,
            action="branch.delete",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="branch",
            target_id=str(branch_id),
        )
        return MessageResponse(message="Branch deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Employees ─────────────────────────────────────────────────────────────────
# Renamed GET /employees to /api/employees to avoid shadowing any future HTML view.

@router.get("/api/employees", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_employees_api(
    branch_id: int | None = None,
    active_only: bool = False,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    employees = await employee_service.list_employees(
        db, org.id, branch_id=branch_id, active_only=active_only
    )
    return APIResponse(data=[EmployeeRead.model_validate(e) for e in employees])


@router.post("/employees", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_employee(
    body: EmployeeCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        employee = await employee_service.create_employee(db, org.id, body.model_dump())
        await audit_service.log_audit_event(
            db,
            action="employee.create",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="employee",
            target_id=str(employee.id),
            details={"email": employee.email, "code": employee.employee_code, "branch_id": employee.branch_id},
        )
        return APIResponse(message="Employee created", data=EmployeeRead.model_validate(employee))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/employees/{employee_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_employee_api(
    employee_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        employee = await employee_service.get_employee(db, employee_id, org.id)
        return APIResponse(data=EmployeeRead.model_validate(employee))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/employees/{employee_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        employee = await employee_service.update_employee(
            db, employee_id, org.id, body.model_dump(exclude_none=True)
        )
        await audit_service.log_audit_event(
            db,
            action="employee.update",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="employee",
            target_id=str(employee.id),
            details={"is_active": employee.is_active, "branch_id": employee.branch_id},
        )
        return APIResponse(data=EmployeeRead.model_validate(employee))
    except ValueError as e:
        msg = str(e)
        status_code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status_code, detail=msg)


@router.delete("/employees/{employee_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_employee(
    employee_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await employee_service.delete_employee(db, employee_id, org.id)
        await audit_service.log_audit_event(
            db,
            action="employee.delete",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="employee",
            target_id=str(employee_id),
        )
        return MessageResponse(message="Employee deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── RBAC & Permissions ────────────────────────────────────────────────────────

@router.get("/api/roles", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_roles_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    roles = await rbac_service.list_roles(db, org.id)
    return APIResponse(data=[RoleRead.model_validate(r) for r in roles])


@router.get("/api/permissions", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_permissions_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    perms = await rbac_service.list_permissions(db)
    return APIResponse(data=[PermissionRead.model_validate(p) for p in perms])


@router.get("/employees/{employee_id}/roles", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_employee_roles_api(
    employee_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        roles = await rbac_service.get_employee_roles(db, org.id, employee_id)
        return APIResponse(data=[RoleRead.model_validate(r) for r in roles])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/employees/{employee_id}/roles", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def assign_employee_role_api(
    employee_id: int,
    body: RoleAssignRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        assignment = await rbac_service.assign_role_to_employee(
            db, org.id, employee_id, body.role_id, assigned_by_id=org.id
        )
        await audit_service.log_audit_event(
            db,
            action="role.assigned",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="employee",
            target_id=str(employee_id),
            details={"role_id": body.role_id},
        )
        return APIResponse(message="Role assigned", data=RoleAssignmentRead.model_validate(assignment))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/employees/{employee_id}/roles/{role_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def remove_employee_role_api(
    employee_id: int,
    role_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await rbac_service.remove_role_from_employee(db, org.id, employee_id, role_id)
        await audit_service.log_audit_event(
            db,
            action="role.removed",
            actor_type="organization",
            actor_id=org.id,
            actor_email=org.email,
            organization_id=org.id,
            target_type="employee",
            target_id=str(employee_id),
            details={"role_id": role_id},
        )
        return MessageResponse(message="Role removed")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Audit Logs ────────────────────────────────────────────────────────────────

@router.get("/api/audit-logs", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_audit_logs_api(
    limit: int = 50,
    offset: int = 0,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    logs = await audit_service.list_audit_logs(db, organization_id=org.id, limit=limit, offset=offset)
    return APIResponse(data=[
        {
            "id": l.id,
            "organization_id": l.organization_id,
            "action": l.action,
            "actor_type": l.actor_type,
            "actor_email": l.actor_email,
            "target_type": l.target_type,
            "target_id": l.target_id,
            "outcome": l.outcome,
            "details": l.details,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ])


# ── Counters ──────────────────────────────────────────────────────────────────
# Renamed GET /counters to /api/counters to avoid shadowing the HTML view.

@router.get("/api/counters", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_counters_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    counters = await org_service.list_counters(db, org.id)
    return APIResponse(data=[CounterRead.model_validate(c) for c in counters])


@router.post("/counters", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_counter(
    body: CounterCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    counter = await org_service.create_counter(db, org.id, body.model_dump())
    return APIResponse(message="Counter created", data=CounterRead.model_validate(counter))


@router.put("/counters/{counter_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_counter(
    counter_id: int,
    body: CounterUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        counter = await org_service.update_counter(db, counter_id, org.id, body.model_dump(exclude_none=True))
        return APIResponse(data=CounterRead.model_validate(counter))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/counters/{counter_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_counter(
    counter_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await org_service.delete_counter(db, counter_id, org.id)
        return MessageResponse(message="Counter deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Services ──────────────────────────────────────────────────────────────────
# Renamed GET /services to /api/services to avoid shadowing the HTML view.

@router.get("/api/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_services_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    services = await org_service.list_services(db, org.id)
    return APIResponse(data=[ServiceRead.model_validate(s) for s in services])


@router.post("/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_service(
    body: ServiceCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    service = await org_service.create_service(db, org.id, body.model_dump())
    return APIResponse(message="Service created", data=ServiceRead.model_validate(service))


@router.put("/services/{service_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_service(
    service_id: int,
    body: ServiceUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = await org_service.update_service(db, service_id, org.id, body.model_dump(exclude_none=True))
        return APIResponse(data=ServiceRead.model_validate(service))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/services/{service_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_service(
    service_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await org_service.delete_service(db, service_id, org.id)
        return MessageResponse(message="Service deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Token actions ─────────────────────────────────────────────────────────────

@router.post("/tokens/{token_id}/action", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def token_action(
    token_id: int,
    body: TokenActionRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        token = await token_service.perform_token_action(
            db, token_id, org.id, body.action, "organization", org.id, body.notes
        )
        display = await token_service.get_display_data(db, org.id)
        await ws_manager.broadcast_to_org(org.id, {"type": "display_update", "data": display})
        tracking = await token_service.get_token_tracking(db, token_id)
        await ws_manager.broadcast_to_token(token_id, {"type": "tracking_update", "data": tracking})
        return APIResponse(message=f"Token {body.action}ed", data=TokenRead.model_validate(token))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Settings (JSON API) ───────────────────────────────────────────────────────
# Renamed GET /settings to /api/settings to avoid shadowing the HTML view.

@router.get("/api/settings", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_settings_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    settings_dict = await org_service.get_org_settings(db, org.id)
    return APIResponse(data=settings_dict)


@router.put("/settings/{key}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def update_setting(
    key: str,
    value: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    await org_service.upsert_setting(db, org.id, key, value)
    return MessageResponse(message="Setting updated")
