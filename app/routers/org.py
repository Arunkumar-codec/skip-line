"""
app/routers/org.py
───────────────────
Organization REST API endpoints (JSON).
GET routes that conflicted with views_org.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import (
    AuthenticatedPrincipal,
    get_current_org,
    get_current_principal,
    require_permission,
)
from app.schemas.auth import ChangePasswordRequest
from app.schemas.branch import BranchCreate, BranchRead, BranchUpdate
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.counter import CounterCreate, CounterRead, CounterUpdate
from app.schemas.employee import EmployeeCreate, EmployeeRead, EmployeeUpdate
from app.schemas.catalogue import (
    CounterServicesAssignRequest,
    ServiceDetailRead,
    ServiceRollup,
    SubServiceCreate,
    SubServiceRead,
    SubServiceUpdate,
)
from app.schemas.form import (
    CustomFormCreate,
    CustomFormRead,
    CustomFormUpdate,
    TokenFormSnapshotRead,
)
from app.schemas.identity import IdentityReviewRequest, UserIdentityRead
from app.schemas.organization import OrgRead, OrgRegisterRequest, OrgUpdateRequest
from app.schemas.rbac import PermissionRead, RoleAssignmentRead, RoleAssignRequest, RoleRead
from app.schemas.service import ServiceCreate, ServiceRead, ServiceUpdate
from app.schemas.token import TokenActionRequest, TokenRead
from app.services import (
    admin_service,
    audit_service,
    branch_service,
    catalogue_service,
    employee_service,
    form_service,
    identity_service,
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


# ── Phase 2: Sub-Services & Hierarchy ─────────────────────────────────────────

@router.post("/services/{service_id}/sub-services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
@router.post("/sub-services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_sub_service_api(
    body: SubServiceCreate,
    service_id: Optional[int] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    target_service_id = service_id or body.service_id
    if not target_service_id:
        raise HTTPException(status_code=400, detail="service_id is required")

    # Verify service belongs to organization
    from models.service import Service
    service = await db.get(Service, target_service_id)
    if not service or service.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Service not found")

    sub = await catalogue_service.create_sub_service(
        db=db,
        service_id=target_service_id,
        name=body.name,
        code=body.code,
        description=body.description,
        additional_price=body.additional_price,
        additional_minutes=body.additional_minutes,
        is_active=body.is_active,
        display_order=body.display_order,
    )
    return APIResponse(message="Sub-service created", data=SubServiceRead.model_validate(sub))


@router.get("/services/{service_id}/sub-services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_sub_services_api(
    service_id: int,
    active_only: bool = False,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.service import Service
    service = await db.get(Service, service_id)
    if not service or service.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Service not found")

    sub_services = await catalogue_service.list_sub_services(db, service_id, active_only=active_only)
    return APIResponse(data=[SubServiceRead.model_validate(s) for s in sub_services])


@router.put("/sub-services/{sub_service_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_sub_service_api(
    sub_service_id: int,
    body: SubServiceUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.sub_service import SubService
    from models.service import Service
    sub = await db.get(SubService, sub_service_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Sub-service not found")
    parent_service = await db.get(Service, sub.service_id)
    if not parent_service or parent_service.organization_id != org.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    updated = await catalogue_service.update_sub_service(
        db=db,
        sub_service_id=sub_service_id,
        name=body.name,
        code=body.code,
        description=body.description,
        additional_price=body.additional_price,
        additional_minutes=body.additional_minutes,
        is_active=body.is_active,
        display_order=body.display_order,
    )
    return APIResponse(message="Sub-service updated", data=SubServiceRead.model_validate(updated))


@router.delete("/sub-services/{sub_service_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_sub_service_api(
    sub_service_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.sub_service import SubService
    from models.service import Service
    sub = await db.get(SubService, sub_service_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Sub-service not found")
    parent_service = await db.get(Service, sub.service_id)
    if not parent_service or parent_service.organization_id != org.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    deleted = await catalogue_service.delete_sub_service(db, sub_service_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Sub-service not found")
    return MessageResponse(message="Sub-service deleted")


@router.get("/services/{service_id}/rollup", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_service_rollup_api(
    service_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.service import Service
    service = await db.get(Service, service_id)
    if not service or service.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Service not found")

    subs = await catalogue_service.list_sub_services(db, service_id, active_only=True)
    rollup = catalogue_service.calculate_service_rollup(service, subs)
    return APIResponse(data=ServiceRollup.model_validate(rollup))


# ── Phase 2: Counter Services (Many-to-Many Routing) ──────────────────────────

@router.post("/counters/{counter_id}/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def assign_counter_services_api(
    counter_id: int,
    body: CounterServicesAssignRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.counter import Counter
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Counter not found")

    mappings = await catalogue_service.assign_services_to_counter(
        db, counter_id, body.service_ids, body.primary_service_id
    )
    return APIResponse(message="Services assigned to counter successfully", data={"assigned_count": len(mappings)})


@router.get("/counters/{counter_id}/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_counter_services_api(
    counter_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.counter import Counter
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Counter not found")

    services = await catalogue_service.get_counter_services(db, counter_id)
    return APIResponse(data=[ServiceRead.model_validate(s) for s in services])


# ── Phase 2: Dynamic Custom Forms ─────────────────────────────────────────────

@router.post("/forms", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_custom_form_api(
    body: CustomFormCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    fields_dict = [f.model_dump() for f in body.fields]
    form = await form_service.create_form(
        db=db,
        organization_id=org.id,
        title=body.title,
        description=body.description,
        service_id=body.service_id,
        fields_data=fields_dict,
        is_published=body.is_published,
    )
    return APIResponse(message="Custom form created", data=CustomFormRead.model_validate(form))


@router.get("/api/forms", response_model=APIResponse, dependencies=[Depends(get_current_org)])
@router.get("/forms/list", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_custom_forms_api(
    service_id: Optional[int] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    forms = await form_service.list_forms_by_org(db, org.id, service_id=service_id)
    return APIResponse(data=[CustomFormRead.model_validate(f) for f in forms])


@router.get("/api/forms/{form_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
@router.get("/forms/{form_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_custom_form_api(
    form_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    form = await form_service.get_form_by_id(db, form_id)
    if not form or form.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Form not found")
    return APIResponse(data=CustomFormRead.model_validate(form))


@router.put("/forms/{form_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_custom_form_api(
    form_id: int,
    body: CustomFormUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    fields_dict = [f.model_dump() for f in body.fields] if body.fields is not None else None
    form = await form_service.update_form(
        db=db,
        organization_id=org.id,
        form_id=form_id,
        title=body.title,
        description=body.description,
        service_id=body.service_id,
        fields_data=fields_dict,
        is_published=body.is_published,
    )
    return APIResponse(message="Custom form updated", data=CustomFormRead.model_validate(form))


@router.delete("/forms/{form_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_custom_form_api(
    form_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    deleted = await form_service.delete_form(db, org.id, form_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Form not found")
    return MessageResponse(message="Custom form deleted")


@router.post("/forms/{form_id}/publish", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def publish_custom_form_api(
    form_id: int,
    is_published: bool = True,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    form = await form_service.update_form(
        db=db,
        organization_id=org.id,
        form_id=form_id,
        is_published=is_published,
    )
    msg = "Form published" if is_published else "Form unpublished"
    return APIResponse(message=msg, data=CustomFormRead.model_validate(form))


@router.post("/forms/{form_id}/retire", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def retire_custom_form_api(
    form_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    form = await form_service.retire_form(db=db, organization_id=org.id, form_id=form_id)
    return APIResponse(message="Form retired successfully", data=CustomFormRead.model_validate(form))


@router.post("/forms/{form_id}/duplicate", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def duplicate_custom_form_api(
    form_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    duplicated = await form_service.duplicate_form(db=db, organization_id=org.id, form_id=form_id)
    return APIResponse(message="Form duplicated successfully", data=CustomFormRead.model_validate(duplicated))


@router.get("/tokens/{token_id}/form", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_org_token_form_api(
    token_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from models.token import Token
    from models.custom_form import TokenFormSnapshot

    token = await db.get(Token, token_id)
    if not token or token.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Token not found")

    snap_stmt = (
        select(TokenFormSnapshot)
        .where(TokenFormSnapshot.token_id == token_id)
        .options(selectinload(TokenFormSnapshot.answers))
    )
    snap_res = await db.execute(snap_stmt)
    snapshot = snap_res.scalar_one_or_none()
    if not snapshot:
        return APIResponse(data=None)

    return APIResponse(data=TokenFormSnapshotRead.model_validate(snapshot))


# ── Phase 2: Customer Identity Verification (KYC) Desk ───────────────────────

@router.get("/api/identities", response_model=APIResponse)
@router.get("/identities/review", response_model=APIResponse)
async def list_identities_for_review_api(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    principal: AuthenticatedPrincipal = Depends(require_permission("identity:read")),
    db: AsyncSession = Depends(get_db),
):
    records = await identity_service.list_identities_for_review(
        db, organization_id=principal.organization_id, status_filter=status, limit=limit, offset=offset
    )
    return APIResponse(data=[UserIdentityRead.model_validate(r) for r in records])


@router.post("/identities/{identity_id}/review", response_model=APIResponse)
async def review_identity_api(
    identity_id: int,
    body: IdentityReviewRequest,
    principal: AuthenticatedPrincipal = Depends(require_permission("identity:review")),
    db: AsyncSession = Depends(get_db),
):
    from models.user_identity import UserIdentity
    identity = await db.get(UserIdentity, identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail="Identity record not found")
    if identity.organization_id is not None and identity.organization_id != principal.organization_id:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-tenant identity access denied")

    reviewer_employee_id = principal.id if principal.role == "employee" else None

    updated = await identity_service.review_identity(
        db=db,
        identity_id=identity_id,
        reviewer_id=reviewer_employee_id,
        action=body.action,
        rejection_reason=body.rejection_reason,
        expires_at=body.expires_at,
    )
    return APIResponse(
        message=f"Identity {body.action}ed successfully",
        data=UserIdentityRead.model_validate(updated),
    )


@router.get("/identities/{identity_id}/document")
async def get_identity_document_org_api(
    identity_id: int,
    principal: AuthenticatedPrincipal = Depends(require_permission("identity:read")),
    db: AsyncSession = Depends(get_db),
):
    from models.user_identity import UserIdentity
    identity = await db.get(UserIdentity, identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail="Identity record not found")
    if identity.organization_id is not None and identity.organization_id != principal.organization_id:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-tenant document access denied")
    if not identity.document_path or not Path(identity.document_path).exists():
        raise HTTPException(status_code=404, detail="Document file not found")

    ext = Path(identity.document_path).suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else f"image/{ext.replace('.', '')}"
    return FileResponse(identity.document_path, media_type=media_type)
