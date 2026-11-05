"""
app/routers/workflow.py
──────────────
Configurable multi-stage service workflows API (Phase 9, MASTER §10).

- /org/workflows*:      organization-admin configuration CRUD (org session).
- /employee/tokens/{token_id}/workflow*: employee stage operations
  (employee session + active-shift authorization; service layer).
- Stage progress for CUSTOMERS is delivered through the tracking payload
  (`token_service.get_token_tracking` → `workflow` key, rendered on
  /public/track and the user-portal track page).

Error mapping: WorkflowError → 400, PermissionError → 403,
StageRaceError → 409 (retry after reload), missing tenant objects → 404.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_employee, get_current_org
from app.schemas.common import APIResponse, MessageResponse
from app.schemas.workflow import (
    StageProgressRead,
    StageTransitionRequest,
    WorkflowCreate,
    WorkflowRead,
    WorkflowUpdate,
)
from app.services import workflow_service
from models.employee import Employee
from models.organization import Organization
from models.service import Service
from models.token import Token

router = APIRouter()


def _map_error(e: Exception) -> HTTPException:
    if isinstance(e, workflow_service.StageRaceError):
        return HTTPException(
            status_code=409,
            detail=str(e) + " (the stage was changed by another operator)",
        )
    if isinstance(e, PermissionError):
        return HTTPException(status_code=403, detail=str(e))
    if isinstance(e, workflow_service.WorkflowError):
        # Only tenant-scoped resource lookups ("X not found") surface as 404;
        # validation failures (incl. foreign counter rejection) stay 400.
        msg = str(e).lower()
        if msg.startswith("token not found") or msg.startswith("workflow not found"):
            return HTTPException(status_code=404, detail=str(e))
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


# ── Organization configuration (B1/B2/B3/B5 config, B14) ─────────────────────


@router.get("/org/workflows/list", response_model=APIResponse)
async def list_workflows_api(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    workflows = await workflow_service.list_workflows(db, org.id)
    return APIResponse(
        data=[WorkflowRead.model_validate(w) for w in workflows]
    )


@router.post("/org/workflows", response_model=APIResponse)
async def create_workflow_api(
    body: WorkflowCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        workflow = await workflow_service.create_workflow(
            db, org.id, body.model_dump()
        )
    except workflow_service.WorkflowError as e:
        raise _map_error(e)
    return APIResponse(
        message="Workflow created",
        data=WorkflowRead.model_validate(workflow),
    )


@router.get("/org/services/{service_id}/workflow", response_model=APIResponse)
async def get_service_workflow_api(
    service_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    service = await db.get(Service, service_id)
    if not service or service.organization_id != org.id:
        raise HTTPException(status_code=404, detail="Service not found")
    workflow = await workflow_service.get_for_service(
        db, org.id, service_id, active_only=False
    )
    return APIResponse(
        data=WorkflowRead.model_validate(workflow) if workflow else None
    )


@router.get("/org/workflows/{workflow_id}", response_model=APIResponse)
async def get_workflow_api(
    workflow_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        workflow = await workflow_service.get_workflow(db, workflow_id, org.id)
    except workflow_service.WorkflowError as e:
        raise _map_error(e)
    return APIResponse(data=WorkflowRead.model_validate(workflow))


@router.put("/org/workflows/{workflow_id}", response_model=APIResponse)
async def update_workflow_api(
    workflow_id: int,
    body: WorkflowUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        workflow = await workflow_service.update_workflow(
            db, workflow_id, org.id,
            body.model_dump(exclude_unset=True),
        )
    except workflow_service.WorkflowError as e:
        raise _map_error(e)
    return APIResponse(
        message="Workflow updated",
        data=WorkflowRead.model_validate(workflow),
    )


@router.delete("/org/workflows/{workflow_id}", response_model=APIResponse)
async def deactivate_workflow_api(
    workflow_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate (soft delete) — history and in-flight tokens stay intact."""
    try:
        await workflow_service.get_workflow(db, workflow_id, org.id)
        workflow = await workflow_service.update_workflow(
            db, workflow_id, org.id, {"is_active": False}
        )
    except workflow_service.WorkflowError as e:
        raise _map_error(e)
    return APIResponse(
        message="Workflow deactivated",
        data=WorkflowRead.model_validate(workflow),
    )


# ── Employee stage operations (B6–B10, B13, B14) ─────────────────────────────


async def _scoped_token(db: AsyncSession, token_id: int, org_id: int) -> Token:
    from sqlalchemy import select
    result = await db.execute(
        select(Token).where(
            Token.id == token_id, Token.organization_id == org_id
        )
    )
    token = result.scalars().first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    return token


@router.get("/employee/tokens/{token_id}/workflow", response_model=APIResponse)
async def token_stage_progress_api(
    token_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Authorized-stage progress view for the serving employee (B6/B11/B14)."""
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        progress = await workflow_service.get_progress(
            db, token_id, employee.organization_id
        )
    except workflow_service.WorkflowError as e:
        raise _map_error(e)
    return APIResponse(data=StageProgressRead.model_validate(progress) if progress else None)


@router.get("/employee/tokens/{token_id}/workflow/history", response_model=APIResponse)
async def token_stage_history_api(
    token_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Full audit trail of stage transitions for this token (B9/B14)."""
    await _scoped_token(db, token_id, employee.organization_id)
    history = await workflow_service.get_token_stage_history(
        db, token_id, employee.organization_id
    )
    return APIResponse(data=[{
        "action": h.action,
        "from": h.from_stage_code,
        "to": h.to_stage_code,
        "stage": h.stage_code or h.to_stage_code,
        "employee": h.employee_name,
        "reason": h.reason,
        "at": h.created_at.isoformat() if h.created_at else None,
    } for h in history])


@router.post("/employee/tokens/{token_id}/workflow/advance", response_model=APIResponse)
async def stage_advance_api(
    token_id: int,
    body: StageTransitionRequest = StageTransitionRequest(),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Finish the current stage and move the customer onward (B7/B13)."""
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        progress = await workflow_service.advance_stage(
            db, token_id, employee.organization_id, employee
        )
    except (workflow_service.WorkflowError,
            workflow_service.StageRaceError, PermissionError) as e:
        raise _map_error(e)
    return APIResponse(
        message="Stage advanced",
        data=StageProgressRead.model_validate(progress),
    )


@router.post("/employee/tokens/{token_id}/workflow/skip", response_model=APIResponse)
async def stage_skip_api(
    token_id: int,
    body: StageTransitionRequest = StageTransitionRequest(),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Skip the current stage — refused for mandatory stages (B4/B5)."""
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        progress = await workflow_service.skip_stage(
            db, token_id, employee.organization_id, employee, body.reason
        )
    except (workflow_service.WorkflowError,
            workflow_service.StageRaceError, PermissionError) as e:
        raise _map_error(e)
    return APIResponse(
        message="Stage skipped",
        data=StageProgressRead.model_validate(progress),
    )


@router.post("/employee/tokens/{token_id}/workflow/recall", response_model=APIResponse)
async def stage_recall_api(
    token_id: int,
    body: StageTransitionRequest = StageTransitionRequest(),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Return the customer to an earlier stage; reason REQUIRED (B8/B9)."""
    if not body.target_stage_code:
        raise HTTPException(
            status_code=400, detail="target_stage_code is required to recall a stage"
        )
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        progress = await workflow_service.recall_stage(
            db, token_id, employee.organization_id, employee,
            body.target_stage_code, body.reason,
        )
    except (workflow_service.WorkflowError,
            workflow_service.StageRaceError, PermissionError) as e:
        raise _map_error(e)
    return APIResponse(
        message="Customer recalled to earlier stage",
        data=StageProgressRead.model_validate(progress),
    )


@router.post("/employee/tokens/{token_id}/workflow/complete", response_model=APIResponse)
async def stage_complete_api(
    token_id: int,
    body: StageTransitionRequest = StageTransitionRequest(),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Exceptional terminal completion at the current stage (B10)."""
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        await workflow_service.complete_workflow(
            db, token_id, employee.organization_id, employee, body.reason
        )
    except (workflow_service.WorkflowError,
            workflow_service.StageRaceError, PermissionError) as e:
        raise _map_error(e)
    return MessageResponse(message="Workflow completed")


@router.post("/employee/tokens/{token_id}/workflow/cancel", response_model=APIResponse)
async def stage_cancel_api(
    token_id: int,
    body: StageTransitionRequest = StageTransitionRequest(),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Abandon remaining stages (mirrors token cancellation, B10)."""
    await _scoped_token(db, token_id, employee.organization_id)
    try:
        await workflow_service.cancel_workflow(
            db, token_id, employee.organization_id, employee, body.reason
        )
    except (workflow_service.WorkflowError,
            workflow_service.StageRaceError, PermissionError) as e:
        raise _map_error(e)
    return MessageResponse(message="Workflow cancelled")
