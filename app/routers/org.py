"""
app/routers/org.py
───────────────────
Organization REST API endpoints (JSON).
GET routes that conflicted with views_org.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from datetime import date
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.schemas.shift import (
    CounterShiftAdjustRequest,
    CounterShiftDecisionRequest,
    CounterShiftRead,
    CounterShiftReconcileRequest,
)
from app.schemas.kiosk import KioskDeviceRead, KioskPairConfirmRequest
from app.schemas.finance import (
    CashSessionCloseRequest,
    CashSessionRead,
    LedgerEntryRead,
    LedgerTotalsRead,
    OrgBankAccountRead,
    OrgBankAccountUpdate,
    PayoutRead,
    RefundRead,
)
from app.services import (
    bank_account_service,
    cash_service,
    ledger_service,
    payout_service,
    refund_service,
)
from app.schemas.schedule import (
    CapacityRuleCreate,
    CapacityRuleRead,
    CapacityRuleUpdate,
    ScheduleChangeDecisionIn,
    ScheduleChangeDecisionsRequest,
    ScheduleChangePreviewRead,
    ScheduleChangePreviewRequest,
    ScheduleExceptionCreate,
    ScheduleExceptionRead,
    ScheduleExceptionUpdate,
    WeeklyScheduleCreate,
    WeeklyScheduleRead,
    WeeklyScheduleUpdate,
)
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
    kiosk_service,
    org_service,
    rbac_service,
    schedule_change_service,
    schedule_service,
    shift_service,
    token_service,
)
from app.websocket.manager import ws_manager
from models.organization import Organization
from models.weekly_schedule import ScheduleScopeLevel

router = APIRouter()


def _schedule_error_status(message: str) -> int:
    """404 for unknown/cross-tenant targets, 400 for validation conflicts."""
    return 404 if "not found" in message.lower() else 400


def _shift_decision_error_status(e: ValueError) -> HTTPException:
    """Shift decisions: 404 for unknown/cross-tenant rows, 409 for
    state conflicts, otherwise 400 (validation)."""
    message = str(e)
    if "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    if "only a pending" in message.lower():
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


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


# ── Phase 3: Schedule administration (weekly hours, exceptions, capacity) ─────
# All routes require an authenticated, approved organization principal and
# enforce strict tenant isolation in the service layer (SCH-001/002/003/004).
# Configuration rows only — generated time_slots and existing bookings are
# never modified here (OPEN_DECISIONS "Decision 17" — Decision 11 workflow).

# ── Weekly schedules ──────────────────────────────────────────────────────────

@router.get("/api/schedules/weekly", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_weekly_schedules_api(
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    rows = await schedule_service.list_weekly_schedules(
        db, org.id, scope_level=scope_level, scope_id=scope_id
    )
    return APIResponse(data=[WeeklyScheduleRead.model_validate(r) for r in rows])


@router.post("/schedules/weekly", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_weekly_schedule_api(
    body: WeeklyScheduleCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.create_weekly_schedule(db, org.id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.weekly.create",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="weekly_schedule",
        target_id=str(row.id),
        details={"scope_level": str(row.scope_level), "scope_id": row.scope_id,
                 "day_of_week": row.day_of_week, "is_closed": row.is_closed},
    )
    return APIResponse(message="Weekly schedule created", data=WeeklyScheduleRead.model_validate(row))


@router.get("/schedules/weekly/{schedule_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_weekly_schedule_api(
    schedule_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.get_weekly_schedule(db, org.id, schedule_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(data=WeeklyScheduleRead.model_validate(row))


@router.put("/schedules/weekly/{schedule_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_weekly_schedule_api(
    schedule_id: int,
    body: WeeklyScheduleUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.update_weekly_schedule(
            db, org.id, schedule_id, body.model_dump(exclude_none=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.weekly.update",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="weekly_schedule",
        target_id=str(row.id),
        details={"scope_level": str(row.scope_level), "scope_id": row.scope_id,
                 "day_of_week": row.day_of_week, "is_closed": row.is_closed},
    )
    return APIResponse(message="Weekly schedule updated", data=WeeklyScheduleRead.model_validate(row))


@router.delete("/schedules/weekly/{schedule_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_weekly_schedule_api(
    schedule_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await schedule_service.delete_weekly_schedule(db, org.id, schedule_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.weekly.delete",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="weekly_schedule",
        target_id=str(schedule_id),
    )
    return MessageResponse(message="Weekly schedule deleted")


# ── Schedule exceptions ───────────────────────────────────────────────────────

@router.get("/api/schedules/exceptions", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_schedule_exceptions_api(
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    rows = await schedule_service.list_schedule_exceptions(
        db, org.id, scope_level=scope_level, scope_id=scope_id,
        date_from=date_from, date_to=date_to,
    )
    return APIResponse(data=[ScheduleExceptionRead.model_validate(r) for r in rows])


@router.post("/schedules/exceptions", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_schedule_exception_api(
    body: ScheduleExceptionCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.create_schedule_exception(db, org.id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.exception.create",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="schedule_exception",
        target_id=str(row.id),
        details={"scope_level": str(row.scope_level), "scope_id": row.scope_id,
                 "exception_date": row.exception_date.isoformat(),
                 "exception_type": str(row.exception_type)},
    )
    return APIResponse(message="Schedule exception created", data=ScheduleExceptionRead.model_validate(row))


@router.get("/schedules/exceptions/{exception_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_schedule_exception_api(
    exception_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.get_schedule_exception(db, org.id, exception_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(data=ScheduleExceptionRead.model_validate(row))


@router.put("/schedules/exceptions/{exception_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_schedule_exception_api(
    exception_id: int,
    body: ScheduleExceptionUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.update_schedule_exception(
            db, org.id, exception_id, body.model_dump(exclude_none=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.exception.update",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="schedule_exception",
        target_id=str(row.id),
        details={"exception_date": row.exception_date.isoformat(),
                 "exception_type": str(row.exception_type), "is_active": row.is_active},
    )
    return APIResponse(message="Schedule exception updated", data=ScheduleExceptionRead.model_validate(row))


@router.delete("/schedules/exceptions/{exception_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_schedule_exception_api(
    exception_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await schedule_service.delete_schedule_exception(db, org.id, exception_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.exception.delete",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="schedule_exception",
        target_id=str(exception_id),
    )
    return MessageResponse(message="Schedule exception deleted")


# ── Capacity rules ────────────────────────────────────────────────────────────

@router.get("/api/schedules/capacity-rules", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_capacity_rules_api(
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from models.capacity_rule import CapacityLimitType
    rows = await schedule_service.list_capacity_rules(
        db, org.id, scope_level=scope_level, scope_id=scope_id
    )
    return APIResponse(data=[CapacityRuleRead.model_validate(r) for r in rows])


@router.post("/schedules/capacity-rules", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_capacity_rule_api(
    body: CapacityRuleCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.create_capacity_rule(db, org.id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.capacity_rule.create",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="capacity_rule",
        target_id=str(row.id),
        details={"scope_level": str(row.scope_level), "scope_id": row.scope_id,
                 "limit_type": str(row.limit_type), "booking_source": str(row.booking_source),
                 "max_count": row.max_count},
    )
    return APIResponse(message="Capacity rule created", data=CapacityRuleRead.model_validate(row))


@router.get("/schedules/capacity-rules/{rule_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_capacity_rule_api(
    rule_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.get_capacity_rule(db, org.id, rule_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(data=CapacityRuleRead.model_validate(row))


@router.put("/schedules/capacity-rules/{rule_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_capacity_rule_api(
    rule_id: int,
    body: CapacityRuleUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_service.update_capacity_rule(
            db, org.id, rule_id, body.model_dump(exclude_none=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=_schedule_error_status(str(e)), detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.capacity_rule.update",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="capacity_rule",
        target_id=str(row.id),
        details={"limit_type": str(row.limit_type), "booking_source": str(row.booking_source),
                 "max_count": row.max_count},
    )
    return APIResponse(message="Capacity rule updated", data=CapacityRuleRead.model_validate(row))


@router.delete("/schedules/capacity-rules/{rule_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_capacity_rule_api(
    rule_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await schedule_service.delete_capacity_rule(db, org.id, rule_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="schedule.capacity_rule.delete",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="capacity_rule",
        target_id=str(rule_id),
    )
    return MessageResponse(message="Capacity rule deleted")


# ── Schedule-change impact workflow (SCH-007 / OPEN_DECISIONS Decision 11) ────
# Preview → resolve → confirm. Configuration is never applied without an
# explicit, audited confirmation; generated time_slots and confirmed bookings
# are never silently rewritten (Decision 17), and confirmation never releases
# slot capacity (Decision 16 — reschedule decisions are recorded but blocked).

@router.post("/schedules/changes/preview", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_schedule_change_preview_api(
    body: ScheduleChangePreviewRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_change_service.create_preview(
            db, org, body.model_dump(), created_by=org.email
        )
    except ValueError as e:
        raise schedule_change_service.schedule_change_http_error(e)
    await audit_service.log_audit_event(
        db,
        action="schedule.change.preview",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="schedule_change_preview",
        target_id=str(row.id),
        details={"entity": row.entity.value if hasattr(row.entity, "value") else str(row.entity),
                 "edit_type": row.edit_type,
                 "affected_bookings": row.impact.get("summary", {}).get("bookings_affected", 0)},
    )
    return APIResponse(
        message="Schedule change preview created",
        data=ScheduleChangePreviewRead.model_validate(row),
    )


@router.get("/api/schedules/changes", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_schedule_change_previews_api(
    status: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = await schedule_change_service.list_previews(db, org.id, status_filter=status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown preview status filter")
    return APIResponse(data=[ScheduleChangePreviewRead.model_validate(r) for r in rows])


@router.get("/schedules/changes/{preview_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_schedule_change_preview_api(
    preview_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_change_service.get_preview(db, org.id, preview_id)
    except ValueError as e:
        raise schedule_change_service.schedule_change_http_error(e)
    return APIResponse(data=ScheduleChangePreviewRead.model_validate(row))


@router.post("/schedules/changes/{preview_id}/resolve", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def resolve_schedule_change_api(
    preview_id: int,
    body: ScheduleChangeDecisionsRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_change_service.record_decisions(
            db, org.id, preview_id,
            {token_id: d.model_dump(exclude_none=True) for token_id, d in body.decisions.items()},
            resolved_by=org.email,
        )
    except ValueError as e:
        raise schedule_change_service.schedule_change_http_error(e)
    return APIResponse(message="Decisions recorded", data=ScheduleChangePreviewRead.model_validate(row))


@router.post("/schedules/changes/{preview_id}/confirm", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def confirm_schedule_change_api(
    preview_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_change_service.confirm_preview(
            db, org.id, preview_id, confirmed_by=org.email
        )
    except ValueError as e:
        raise schedule_change_service.schedule_change_http_error(e)
    return APIResponse(message="Schedule change confirmed and applied", data=ScheduleChangePreviewRead.model_validate(row))


@router.post("/schedules/changes/{preview_id}/reject", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def reject_schedule_change_api(
    preview_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await schedule_change_service.reject_preview(
            db, org.id, preview_id, rejected_by=org.email
        )
    except ValueError as e:
        raise schedule_change_service.schedule_change_http_error(e)
    return APIResponse(message="Schedule change rejected", data=ScheduleChangePreviewRead.model_validate(row))


# ── Phase 4: Daily counter shifts (SHIFT-001/002) ─────────────────────────────
# The organization head (or an employee explicitly delegated via the RBAC
# `shifts:approve` permission) views pending requests and approves/rejects
# with reason, approved services, optional daily period and payment
# permission. Decisions are audit-logged; cross-tenant rows are not found.

@router.get("/api/shifts", response_model=APIResponse)
@router.get("/shifts")
async def list_counter_shifts_api(
    request: Request,
    status: Optional[str] = None,
    shift_date: Optional[date] = None,
    principal: AuthenticatedPrincipal = Depends(require_permission("shifts:approve")),
    db: AsyncSession = Depends(get_db),
):
    accept = request.headers.get("accept", "")
    auth = request.headers.get("authorization", "")
    if "text/html" in accept and not auth:
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="templates")
        from app.routers.views_org import _get_org_or_redirect
        org, redir = await _get_org_or_redirect(request, db)
        if redir:
            return redir
        return templates.TemplateResponse(
            "org/shifts.html",
            {"request": request, "current_org": org, "active_page": "shifts"},
        )

    shifts = await shift_service.list_org_shifts(
        db, principal.organization_id, status_filter=status, shift_date=shift_date
    )
    return APIResponse(data=[CounterShiftRead.model_validate(s) for s in shifts])


@router.post("/shifts/{shift_id}/decision", response_model=APIResponse)
async def decide_counter_shift_api(
    shift_id: int,
    body: CounterShiftDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_permission("shifts:approve")),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.decide_shift_request(
            db,
            principal.organization_id,
            shift_id,
            decision=body.decision,
            reason=body.reason,
            decided_by_type=principal.role,
            decided_by_id=principal.id,
            approved_services=body.approved_services,
            approved_period_start=body.approved_period_start,
            approved_period_end=body.approved_period_end,
            payment_permitted=body.payment_permitted,
        )
    except ValueError as e:
        raise _shift_decision_error_status(e)
    await audit_service.log_audit_event(
        db,
        action=f"shift.{body.decision}",
        actor_type=principal.role,
        actor_id=principal.id,
        actor_email=principal.email,
        organization_id=principal.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={
            "status": shift.status,
            "approved_services": shift.approved_services,
            "payment_permitted": shift.payment_permitted,
        },
    )
    return APIResponse(
        message=f"Shift request {body.decision}d",
        data=CounterShiftRead.model_validate(shift),
    )


@router.post("/shifts/{shift_id}/reconcile", response_model=APIResponse)
async def reconcile_counter_shift_api(
    shift_id: int,
    body: CounterShiftReconcileRequest,
    principal: AuthenticatedPrincipal = Depends(require_permission("shifts:approve")),
    db: AsyncSession = Depends(get_db),
):
    """Head review of shift closing report (SHIFT-006): approve, request correction, record discrepancy, or escalate."""
    try:
        shift = await shift_service.reconcile_shift(
            db,
            principal.organization_id,
            shift_id,
            action=body.action,
            notes=body.notes,
            decided_by_type=principal.role,
            decided_by_id=principal.id,
        )
    except ValueError as e:
        raise _shift_decision_error_status(e)
    await audit_service.log_audit_event(
        db,
        action=f"shift.reconcile.{body.action}",
        actor_type=principal.role,
        actor_id=principal.id,
        actor_email=principal.email,
        organization_id=principal.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={"reconciliation_status": shift.reconciliation_status, "action": body.action},
    )
    return APIResponse(
        message=f"Shift reconciliation {body.action} processed",
        data=CounterShiftRead.model_validate(shift),
    )


@router.post("/shifts/{shift_id}/adjust", response_model=APIResponse)
async def adjust_counter_shift_api(
    shift_id: int,
    body: CounterShiftAdjustRequest,
    principal: AuthenticatedPrincipal = Depends(require_permission("shifts:approve")),
    db: AsyncSession = Depends(get_db),
):
    """Authorized adjustment to a closed reconciled shift (SHIFT-006)."""
    try:
        shift = await shift_service.adjust_reconciled_shift(
            db,
            principal.organization_id,
            shift_id,
            adjustment_amount=body.adjustment_amount,
            reason=body.reason,
            decided_by_type=principal.role,
            decided_by_id=principal.id,
        )
    except ValueError as e:
        raise _shift_decision_error_status(e)
    await audit_service.log_audit_event(
        db,
        action="shift.adjust",
        actor_type=principal.role,
        actor_id=principal.id,
        actor_email=principal.email,
        organization_id=principal.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={"adjustment_amount": str(body.adjustment_amount), "reason": body.reason},
    )
    return APIResponse(
        message="Shift adjustment recorded",
        data=CounterShiftRead.model_validate(shift),
    )


# ── Phase 4: Kiosk Device Management (KSK-001..006) ───────────────────────────


@router.get("/api/kiosks", response_model=APIResponse)
@router.get("/kiosks")
async def list_kiosks_api(
    request: Request,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    accept = request.headers.get("accept", "")
    auth = request.headers.get("authorization", "")
    if "text/html" in accept and not auth:
        from fastapi.templating import Jinja2Templates
        from app.services import branch_service
        templates = Jinja2Templates(directory="templates")
        branches = await branch_service.list_branches(db, org.id)
        return templates.TemplateResponse(
            "org/kiosks.html",
            {"request": request, "current_org": org, "active_page": "kiosks", "branches": branches},
        )

    kiosks = await kiosk_service.list_kiosks(db, org.id)
    return APIResponse(data=[KioskDeviceRead.model_validate(k) for k in kiosks])


@router.post("/kiosks/pair-confirm", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def confirm_kiosk_pairing_api(
    body: KioskPairConfirmRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        device = await kiosk_service.confirm_pairing_pin(
            db, org.id, body.pairing_code, device_name=body.device_name, branch_id=body.branch_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="kiosk.pair_confirm",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="kiosk_device",
        target_id=str(device.id),
        details={"device_code": device.device_code, "device_name": device.device_name},
    )
    return APIResponse(message="Kiosk device paired successfully", data=KioskDeviceRead.model_validate(device))


@router.post("/kiosks/{kiosk_id}/revoke", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def revoke_kiosk_api(
    kiosk_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        device = await kiosk_service.revoke_kiosk(db, org.id, kiosk_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit_service.log_audit_event(
        db,
        action="kiosk.revoke",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        target_type="kiosk_device",
        target_id=str(device.id),
        details={"device_code": device.device_code},
    )
    return APIResponse(message="Kiosk device access revoked", data=KioskDeviceRead.model_validate(device))


# ── Phase 5: Finance & Ledgering APIs (LED-001..004, CASH-001..003, FIN-001..002) ─

@router.get("/api/finance/ledger", response_model=PaginatedResponse, dependencies=[Depends(get_current_org)])
async def get_ledger_journal_api(
    account: Optional[str] = None,
    entry_type: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve immutable double-entry ledger entries journal (LED-001..003)."""
    entries, total = await ledger_service.get_ledger_entries(
        db, organization_id=org.id, account=account, entry_type=entry_type, page=page, per_page=per_page
    )
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[LedgerEntryRead.model_validate(e) for e in entries],
    )


@router.get("/api/finance/totals", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_ledger_totals_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Derive institutional financial figures directly from ledger entries (LED-004)."""
    totals = await ledger_service.get_ledger_totals(db, organization_id=org.id)
    return APIResponse(data=totals)


@router.get("/api/finance/payment-policy", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_own_payment_policy_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Read-only view of this organization's permitted payment
    methods (null = unrestricted). Policy writes are admin-only."""
    from app.schemas.finance import PaymentPolicyRead
    allowed = list(org.allowed_payment_methods) if org.allowed_payment_methods else None
    return APIResponse(
        data=PaymentPolicyRead(organization_id=org.id, allowed_payment_methods=allowed)
    )


@router.get("/api/finance/reports", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_financial_reports_api(
    service_id: Optional[int] = None,
    counter_id: Optional[int] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """CASH-003: Detailed financial reports by service, counter, and booking source."""
    report = await cash_service.get_financial_breakdown_report(
        db, organization_id=org.id, service_id=service_id, counter_id=counter_id
    )
    return APIResponse(data=report)


@router.get("/api/finance/cash-sessions", response_model=PaginatedResponse, dependencies=[Depends(get_current_org)])
async def list_cash_sessions_api(
    page: int = 1,
    per_page: int = 20,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """CASH-001: List physical cash sessions."""
    from sqlalchemy import select, func
    from models.finance import CashSession
    base = select(CashSession).where(CashSession.organization_id == org.id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    sessions = (
        await db.execute(
            base.order_by(CashSession.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
        )
    ).scalars().all()
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[CashSessionRead.model_validate(s) for s in sessions],
    )


@router.post("/api/finance/cash-sessions/{session_id}/close", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def close_cash_session_api(
    session_id: int,
    body: CashSessionCloseRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """CASH-002: Close cash session with physical denomination count and evaluate discrepancy."""
    try:
        session = await cash_service.close_cash_session(
            db,
            session_id=session_id,
            counted_cash=body.counted_cash,
            denominations=body.denominations,
            notes=body.notes,
        )
        return APIResponse(message="Cash session closed successfully", data=CashSessionRead.model_validate(session))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/finance/refunds", response_model=PaginatedResponse, dependencies=[Depends(get_current_org)])
async def list_org_refunds_api(
    page: int = 1,
    per_page: int = 20,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """List refunds for the organization."""
    refunds, total = await refund_service.list_refunds(db, org.id, page=page, per_page=per_page)
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[RefundRead.model_validate(r) for r in refunds],
    )


@router.post("/api/finance/refunds/{refund_id}/approve", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def approve_refund_api(
    refund_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Approve pending refund."""
    try:
        refund = await refund_service.approve_refund(
            db, organization_id=org.id, refund_id=refund_id, approved_by_type="organization", approved_by_id=org.id
        )
        return APIResponse(message="Refund approved and processed", data=RefundRead.model_validate(refund))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/finance/refunds/{refund_id}/reject", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def reject_refund_api(
    refund_id: int,
    rejection_reason: str = Query(..., min_length=3),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Reject pending refund."""
    try:
        refund = await refund_service.reject_refund(
            db,
            organization_id=org.id,
            refund_id=refund_id,
            rejection_reason=rejection_reason,
            rejected_by_type="organization",
            rejected_by_id=org.id,
        )
        return APIResponse(message="Refund rejected", data=RefundRead.model_validate(refund))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/finance/bank-account", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_org_bank_account_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """FIN-002: Retrieve organization bank details and security hold status."""
    acc = await bank_account_service.get_bank_account(db, org.id)
    return APIResponse(data=OrgBankAccountRead.model_validate(acc) if acc else None)


@router.post("/api/finance/bank-account", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_org_bank_account_api(
    body: OrgBankAccountUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """FIN-002: Register or modify bank account details; applies 72h payout hold."""
    try:
        acc = await bank_account_service.update_bank_account(
            db,
            organization_id=org.id,
            account_number=body.account_number,
            ifsc_code=body.ifsc_code,
            account_holder_name=body.account_holder_name,
            bank_name=body.bank_name,
            updated_by_type="organization",
            updated_by_id=org.id,
        )
        return APIResponse(
            message="Bank account updated; 72-hour payout security hold applied",
            data=OrgBankAccountRead.model_validate(acc),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/finance/payouts", response_model=PaginatedResponse, dependencies=[Depends(get_current_org)])
async def list_org_payouts_api(
    page: int = 1,
    per_page: int = 20,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """List organization payouts."""
    payouts, total = await payout_service.list_payouts(db, organization_id=org.id, page=page, per_page=per_page)
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[PayoutRead.model_validate(p) for p in payouts],
    )
