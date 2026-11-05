"""
app/services/workflow_service.py
──────────────
Configurable multi-stage service workflows (MASTER §10, Phase 9 Workstream B).

Responsibilities:
- Org-admin configuration CRUD (create/update/list/deactivate workflows made
  of ordered, optionally mandatory stages — the illustrative 5-stage
  healthcare example is DATA here, never code).
- Lazily opening a stage-transaction for every token booked onto a service
  that has an active workflow (single-stage/unconfigured services are never
  touched — B15).
- Concurrency-safe stage transitions driven by employees:
    advance → finish the current stage and move to the next one (or complete)
    skip    → over the current stage, ONLY when it is not mandatory (B4/B5)
    recall  → send the customer back to an earlier stage, reason REQUIRED (B8)
    complete/cancel — terminal transitions (B10)
- Stage history with timestamps, employee identity, reasons and point-in-time
  stage snapshots (B9) + an audit event per transition.
- Tenant isolation on every lookup (organization_id scoping, B14) and active
  shift authorization for employee stage operations via the SAME
  require_active_shift machinery used by queue actions (B6).

Concurrency (B13): every transition is a compare-and-swap UPDATE
(    UPDATE token_workflow_states SET current_stage_id = <new>
     WHERE token_id = <t> AND current_stage_id = <expected> AND status='active')
executed inside the same transaction as the stage log insert. Two employees
racing on the same stage produce exactly one winner; the loser's UPDATE
matches zero rows and raises a clean retryable error (HTTP 409 at the API).
"""

import logging
from typing import Any, Optional

from loguru import logger
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from models.counter import Counter
from models.employee import Employee
from models.service import Service
from models.token import Token
from models.workflow import (
    ServiceWorkflow,
    TokenStageLog,
    TokenWorkflowState,
    WorkflowStage,
)

# Transition actions recorded in token_stage_logs.
ACTION_START = "start"
ACTION_ADVANCE = "advance"
ACTION_SKIP = "skip"
ACTION_RECALL = "recall"
ACTION_COMPLETE = "complete"
ACTION_CANCEL = "cancel"

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"


class WorkflowError(ValueError):
    """Domain validation failure — callers map to HTTP 400."""


class StageRaceError(RuntimeError):
    """Compare-and-swap lost: someone else moved the stage first — retryable."""


# ── Configuration (B1–B5, B14) ────────────────────────────────────────────────


async def _validate_stage_payload(
    db: AsyncSession, org_id: int, stages_in: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalize/validate a stage list: contiguous 1..n positions, unique
    codes, and any bound counter belonging to this org (tenant isolation)."""
    if not stages_in:
        raise WorkflowError("A workflow needs at least one stage")
    if len(stages_in) > 50:
        raise WorkflowError("A workflow may have at most 50 stages")

    normalized: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    seen_positions: set[int] = set()
    for i, raw in enumerate(stages_in, start=1):
        code = str(raw.get("code", "")).strip().lower()
        name = str(raw.get("name", "")).strip()
        if not code or len(code) > 50:
            raise WorkflowError(f"Stage {i}: a stable code of 1-50 chars is required")
        if not name or len(name) > 200:
            raise WorkflowError(f"Stage {i}: a name of 1-200 chars is required")
        if not code.replace("_", "").replace("-", "").isalnum():
            raise WorkflowError(f"Stage {i}: code may contain letters, digits, '-' and '_' only")
        if code in seen_codes:
            raise WorkflowError(f"Duplicate stage code: {code}")
        seen_codes.add(code)

        position = raw.get("position") or i
        if not isinstance(position, int) or position != i:
            raise WorkflowError(
                "Stage positions must be contiguous and ordered starting at 1"
            )
        if position in seen_positions:
            raise WorkflowError(f"Duplicate stage position: {position}")
        seen_positions.add(position)

        counter_id = raw.get("counter_id")
        if counter_id is not None:
            counter = await db.get(Counter, counter_id)
            if not counter or counter.organization_id != org_id:
                raise WorkflowError(
                    f"Stage {i}: counter {counter_id} not found in this organization"
                )

        try:
            est = int(raw.get("estimated_minutes", 0) or 0)
        except (TypeError, ValueError):
            raise WorkflowError(f"Stage {i}: estimated_minutes must be an integer")
        if est < 0 or est > 1440:
            raise WorkflowError(f"Stage {i}: estimated_minutes must be 0..1440")

        normalized.append({
            "code": code,
            "name": name,
            "position": position,
            "description": raw.get("description") or None,
            "is_mandatory": bool(raw.get("is_mandatory", True)),
            "estimated_minutes": est,
            "counter_id": counter_id,
        })
    return normalized


async def create_workflow(
    db: AsyncSession, org_id: int, data: dict[str, Any]
) -> ServiceWorkflow:
    """Create a workflow for a service (B1). One per service (B2/B15)."""
    service_id = data.get("service_id")
    if not service_id:
        raise WorkflowError("service_id is required")

    service = await db.get(Service, service_id)
    if not service or service.organization_id != org_id:
        raise WorkflowError("Service not found in this organization")

    existing = (await db.execute(
        select(ServiceWorkflow).where(ServiceWorkflow.service_id == service_id)
    )).scalar_one_or_none()
    if existing:
        raise WorkflowError("This service already has a workflow configured")

    name = str(data.get("name", "")).strip()
    if not name or len(name) > 200:
        raise WorkflowError("A workflow name of 1-200 chars is required")

    stages_in = data.get("stages") or []
    normalized = await _validate_stage_payload(db, org_id, stages_in)

    workflow = ServiceWorkflow(
        organization_id=org_id,
        service_id=service_id,
        name=name,
        description=data.get("description"),
        is_active=bool(data.get("is_active", True)),
    )
    for s in normalized:
        workflow.stages.append(WorkflowStage(organization_id=org_id, **s))
    db.add(workflow)
    await db.commit()
    await db.refresh(workflow)

    await audit_service.log_audit_event(
        db, "workflow.created", "organization", actor_id=org_id,
        organization_id=org_id, target_type="service_workflow",
        target_id=workflow.id, commit=False,
        details={"service_id": service_id, "stages": len(normalized)},
    )
    await db.commit()
    return workflow


async def get_workflow(
    db: AsyncSession, workflow_id: int, org_id: int
) -> ServiceWorkflow:
    workflow = await db.get(ServiceWorkflow, workflow_id)
    if not workflow or workflow.organization_id != org_id:
        raise WorkflowError("Workflow not found")
    return workflow


async def list_workflows(
    db: AsyncSession, org_id: int, active_only: bool = False
) -> list[ServiceWorkflow]:
    query = select(ServiceWorkflow).where(ServiceWorkflow.organization_id == org_id)
    if active_only:
        query = query.where(ServiceWorkflow.is_active == True)  # noqa: E712
    result = await db.execute(query.order_by(ServiceWorkflow.id))
    return list(result.scalars().all())


async def get_for_service(
    db: AsyncSession, org_id: int, service_id: int, active_only: bool = True
) -> Optional[ServiceWorkflow]:
    query = select(ServiceWorkflow).where(
        and_(ServiceWorkflow.organization_id == org_id,
             ServiceWorkflow.service_id == service_id)
    )
    if active_only:
        query = query.where(ServiceWorkflow.is_active == True)  # noqa: E712
    result = await db.execute(query)
    return result.scalars().first()


async def update_workflow(
    db: AsyncSession, workflow_id: int, org_id: int, data: dict[str, Any]
) -> ServiceWorkflow:
    """Update workflow config and/or replace the stage list (B1).

    Historical integrity (B9): stage semantics are snapshotted onto every
    stage log at transition time, so reconfiguration never rewrites the past.
    """
    workflow = await get_workflow(db, workflow_id, org_id)

    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name or len(name) > 200:
            raise WorkflowError("A workflow name of 1-200 chars is required")
        workflow.name = name

    if "description" in data:
        workflow.description = data["description"]

    if "is_active" in data:
        workflow.is_active = bool(data["is_active"])

    if "stages" in data and data["stages"] is not None:
        normalized = await _validate_stage_payload(db, org_id, data["stages"])
        # Replace-all semantics for stages (codes are the stable identity).
        # Flush the orphans FIRST so the unique (workflow_id, position)
        # constraint never sees old and new rows at the same time (B1).
        workflow.stages.clear()
        await db.flush()
        workflow.stages = [WorkflowStage(organization_id=org_id, **s) for s in normalized]

    await db.commit()
    await db.refresh(workflow)

    await audit_service.log_audit_event(
        db, "workflow.updated", "organization", actor_id=org_id,
        organization_id=org_id, target_type="service_workflow",
        target_id=workflow.id,
        details={"fields": [k for k in data.keys()]},
    )
    return workflow


# ── Token lifecycle integration (B15) ────────────────────────────────────────


async def maybe_start_for_token(db: AsyncSession, token: Token) -> None:
    """Open the stage transaction for a just-booked token when its service
    has an active workflow. Idempotent (unique token state). Must be called
    INSIDE the booking transaction — a failure here must never fail the
    booking itself (callers wrap defensively)."""
    if token.service_id is None:
        return  # single-counter/legacy token — nothing to configure (B15)

    workflow = await get_for_service(db, token.organization_id, token.service_id)
    if not workflow:
        return  # unconfigured service → legacy single-stage behaviour (B15)

    existing = (await db.execute(
        select(TokenWorkflowState).where(TokenWorkflowState.token_id == token.id)
    )).scalar_one_or_none()
    if existing:
        return  # already started (retries keep this idempotent)

    first_stage = workflow.stages[0]
    state = TokenWorkflowState(
        token_id=token.id,
        organization_id=token.organization_id,
        workflow_id=workflow.id,
        current_stage_id=first_stage.id,
        status=STATUS_ACTIVE,
    )
    db.add(state)
    await db.flush()  # need state.id for the log row

    db.add(TokenStageLog(
        token_id=token.id,
        organization_id=token.organization_id,
        state_id=state.id,
        workflow_id=workflow.id,
        action=ACTION_START,
        to_stage_code=first_stage.code,
        stage_code=first_stage.code,
        stage_name=first_stage.name,
        stage_position=first_stage.position,
        stage_mandatory=first_stage.is_mandatory,
    ))
    logger.info(
        "Workflow '{}' opened for token {} at stage '{}'",
        workflow.name, token.id, first_stage.code,
    )


async def get_state_for_token(
    db: AsyncSession, token_id: int, org_id: int
) -> Optional[TokenWorkflowState]:
    result = await db.execute(
        select(TokenWorkflowState).where(
            and_(TokenWorkflowState.token_id == token_id,
                 TokenWorkflowState.organization_id == org_id)
        )
    )
    return result.scalars().first()


# ── Core transition engine ───────────────────────────────────────────────────


async def _load_transition_context(
    db: AsyncSession, token_id: int, org_id: int
) -> tuple[Token, ServiceWorkflow, TokenWorkflowState, WorkflowStage]:
    token = await db.get(Token, token_id)
    if not token or token.organization_id != org_id:
        raise WorkflowError("Token not found")

    state = await get_state_for_token(db, token_id, org_id)
    if not state:
        raise WorkflowError("This token has no multi-stage workflow")

    workflow = await db.get(ServiceWorkflow, state.workflow_id)
    if not workflow:
        raise WorkflowError("Workflow configuration no longer exists")

    if state.status != STATUS_ACTIVE:
        raise WorkflowError(f"Workflow is already {state.status}")

    stages = (await db.execute(
        select(WorkflowStage).where(
            WorkflowStage.workflow_id == workflow.id
        ).order_by(WorkflowStage.position)
    )).scalars().all()

    current = next((s for s in stages if s.id == state.current_stage_id), None)
    if current is None:
        raise WorkflowError("The workflow's current stage no longer exists")

    return token, workflow, state, current, list(stages)


def _snapshot(stage: WorkflowStage) -> dict[str, Any]:
    return {
        "stage_code": stage.code,
        "stage_name": stage.name,
        "stage_position": stage.position,
        "stage_mandatory": stage.is_mandatory,
    }


async def _cas_transition(
    db: AsyncSession,
    state: TokenWorkflowState,
    expected_stage_id: int,
    new_stage_id: Optional[int],
    new_status: str = STATUS_ACTIVE,
    terminal_at: Optional[str] = None,
) -> None:
    """Optimistic compare-and-swap on the current stage pointer (B13).

    `UPDATE ... WHERE current_stage_id = expected AND status='active'` —
    a concurrent transition that already moved the pointer makes this UPDATE
    match zero rows and we raise StageRaceError instead of double-advancing.
    """
    values: dict[str, Any] = {"version": state.version + 1}
    if new_status != state.status:
        values["status"] = new_status
    if new_stage_id != state.current_stage_id:
        values["current_stage_id"] = new_stage_id
    if terminal_at == "completed":
        values["completed_at"] = func_now()
    if terminal_at == "cancelled":
        values["cancelled_at"] = func_now()

    result = await db.execute(
        update(TokenWorkflowState)
        .where(
            TokenWorkflowState.id == state.id,
            TokenWorkflowState.current_stage_id == expected_stage_id,
            TokenWorkflowState.status == STATUS_ACTIVE,
        )
        .values(**values)
    )
    if result.rowcount != 1:  # the CAS failed — someone else transitioned first
        raise StageRaceError(
            "Stage changed concurrently — reload the token state and retry"
        )
    state.version += 1
    state.current_stage_id = new_stage_id
    state.status = new_status


def func_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def _record(
    db: AsyncSession,
    token: Token,
    workflow: ServiceWorkflow,
    state: TokenWorkflowState,
    action: str,
    from_stage: Optional[WorkflowStage],
    to_stage: Optional[WorkflowStage],
    employee: Optional[Employee],
    reason: Optional[str],
) -> None:
    log = TokenStageLog(
        token_id=token.id,
        organization_id=token.organization_id,
        state_id=state.id,
        workflow_id=workflow.id,
        action=action,
        from_stage_code=from_stage.code if from_stage else None,
        to_stage_code=to_stage.code if to_stage else None,
        employee_id=employee.id if employee else None,
        employee_name=employee.name if employee else None,
        reason=reason,
    )
    # Snapshot the MOST MEANINGFUL stage at transition time: the destination
    # for advance/recall/complete/cancel, the skipped stage itself for skips.
    snapshot_stage = to_stage if (action != ACTION_SKIP and to_stage is not None) \
        else from_stage
    if snapshot_stage is not None:
        for key, value in _snapshot(snapshot_stage).items():
            setattr(log, key, value)
    db.add(log)

    await audit_service.log_audit_event(
        db, f"workflow.{action}", "employee" if employee else "system",
        actor_id=employee.id if employee else None,
        organization_id=token.organization_id,
        target_type="token", target_id=token.id,
        details={
            "from_stage": from_stage.code if from_stage else None,
            "to_stage": to_stage.code if to_stage else None,
            "workflow_id": workflow.id,
            **({"reason": reason} if reason else {}),
        },
        commit=False,
    )


async def _authorize_employee(
    db: AsyncSession, org_id: int, employee: Employee,
    stage: Optional[WorkflowStage], token: Token,
) -> None:
    """Shift authorization via the same queue-action machinery (B6): the
    employee must hold an approved active shift at the stage's bound counter
    (when the stage declares one) or at the token's counter otherwise."""
    from app.services.shift_service import require_active_shift

    counter_id = (stage.counter_id if stage and stage.counter_id else token.counter_id)
    await require_active_shift(db, org_id, counter_id, employee.id)


async def advance_stage(
    db: AsyncSession, token_id: int, org_id: int, employee: Employee,
) -> dict[str, Any]:
    """Finish the current stage and move the customer to the next one (B7).
    Advancing past the last stage completes the workflow (B10)."""
    token, workflow, state, current, stages = await _load_transition_context(
        db, token_id, org_id
    )
    await _authorize_employee(db, org_id, employee, current, token)

    idx = current.position - 1
    nxt = stages[idx + 1] if idx + 1 < len(stages) else None

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=nxt.id if nxt else None,
        new_status=STATUS_ACTIVE if nxt else STATUS_COMPLETED,
        terminal_at=None if nxt else "completed",
    )
    await _record(db, token, workflow, state, ACTION_ADVANCE, current, nxt,
                  employee, None)
    await db.commit()

    return await get_progress(db, token_id, org_id)


async def skip_stage(
    db: AsyncSession, token_id: int, org_id: int, employee: Employee,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Skip the current stage — REFUSED for mandatory stages (B4/B5)."""
    token, workflow, state, current, stages = await _load_transition_context(
        db, token_id, org_id
    )
    if current.is_mandatory:
        raise WorkflowError(
            f"Stage '{current.code}' is mandatory and cannot be skipped"
        )
    await _authorize_employee(db, org_id, employee, current, token)

    idx = current.position - 1
    nxt = stages[idx + 1] if idx + 1 < len(stages) else None

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=nxt.id if nxt else None,
        new_status=STATUS_ACTIVE if nxt else STATUS_COMPLETED,
        terminal_at=None if nxt else "completed",
    )
    await _record(db, token, workflow, state, ACTION_SKIP, current, nxt,
                  employee, reason)
    await db.commit()

    return await get_progress(db, token_id, org_id)


async def recall_stage(
    db: AsyncSession, token_id: int, org_id: int, employee: Employee,
    target_stage_code: str, reason: str,
) -> dict[str, Any]:
    """Send the customer back to an EARLIER stage with a recorded reason (B8)."""
    if not reason or not str(reason).strip():
        raise WorkflowError("A reason is required to recall a customer to an earlier stage")

    token, workflow, state, current, stages = await _load_transition_context(
        db, token_id, org_id
    )
    await _authorize_employee(db, org_id, employee, current, token)

    target = next(
        (s for s in stages if s.code == str(target_stage_code).strip().lower()),
        None,
    )
    if target is None:
        raise WorkflowError("Target stage not found in this workflow")
    if target.position >= current.position:
        raise WorkflowError(
            "Recall must point to an EARLIER stage — use advance to move forward"
        )

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=target.id, new_status=STATUS_ACTIVE,
    )
    await _record(db, token, workflow, state, ACTION_RECALL, current, target,
                  employee, reason)
    await db.commit()

    return await get_progress(db, token_id, org_id)


async def complete_workflow(
    db: AsyncSession, token_id: int, org_id: int, employee: Optional[Employee],
    reason: Optional[str] = None,
):
    """Terminal: declare the service completed at the current stage (B10)."""
    token, workflow, state, current, _stages = await _load_transition_context(
        db, token_id, org_id
    )
    if employee is not None:
        await _authorize_employee(db, org_id, employee, current, token)

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=None, new_status=STATUS_COMPLETED,
        terminal_at="completed",
    )
    await _record(db, token, workflow, state, ACTION_COMPLETE, current, None,
                  employee, reason)
    await db.commit()


async def cancel_workflow(
    db: AsyncSession, token_id: int, org_id: int, employee: Optional[Employee],
    reason: Optional[str] = None,
):
    """Terminal: abandon the remaining stages (mirrors token cancellation, B10)."""
    token, workflow, state, current, _stages = await _load_transition_context(
        db, token_id, org_id
    )
    if employee is not None:
        await _authorize_employee(db, org_id, employee, current, token)

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=None, new_status=STATUS_CANCELLED,
        terminal_at="cancelled",
    )
    await _record(db, token, workflow, state, ACTION_CANCEL, current, None,
                  employee, reason)
    await db.commit()


async def get_token_stage_history(
    db: AsyncSession, token_id: int, org_id: int
) -> list[TokenStageLog]:
    result = await db.execute(
        select(TokenStageLog).where(
            TokenStageLog.token_id == token_id,
            TokenStageLog.organization_id == org_id,
        ).order_by(TokenStageLog.id)
    )
    return list(result.scalars().all())


async def get_progress(db: AsyncSession, token_id: int, org_id: int) -> dict[str, Any]:
    """Meaningful stage progress for customers and employees (B11).

    Shape:
    {
        status, version,
        current_stage: {code, name, position, description, is_mandatory,
                        estimated_minutes} | None,
        stages: [ {code, name, position, is_mandatory, state} ]  # per stage:
                  pending | in_progress | completed | skipped
        stage_minutes_remaining: int,   # sum of pending mandatory stage minutes
        history: [ {action, stage_code, from, to, employee, reason, at} ]
    }
    """
    state = await get_state_for_token(db, token_id, org_id)
    if not state:
        return {}  # legacy single-stage token — no workflow view (B15)

    workflow = await db.get(ServiceWorkflow, state.workflow_id)
    stages = (await db.execute(
        select(WorkflowStage).where(WorkflowStage.workflow_id == workflow.id)
        .order_by(WorkflowStage.position)
    )).scalars().all()
    history = (await db.execute(
        select(TokenStageLog).where(TokenStageLog.state_id == state.id)
        .order_by(TokenStageLog.id)
    )).scalars().all()

    # Latest terminal outcome per stage code from the (immutable) history.
    stage_state: dict[str, str] = {}
    for h in history:
        if h.action in (ACTION_START,):
            if h.to_stage_code:
                stage_state[h.to_stage_code] = "in_progress"
        elif h.action == ACTION_SKIP:
            stage_state[h.stage_code] = "skipped"      # the skipped stage itself
            if h.to_stage_code:
                stage_state[h.to_stage_code] = "in_progress"
        elif h.action == ACTION_RECALL:
            if h.to_stage_code:
                stage_state[h.to_stage_code] = "in_progress"
        elif h.action in (ACTION_ADVANCE, ACTION_COMPLETE):
            if h.from_stage_code:
                stage_state[h.from_stage_code] = "completed"
            if h.to_stage_code:
                stage_state[h.to_stage_code] = "in_progress"
        elif h.action == ACTION_CANCEL:
            pass  # remaining stages surface via terminal status below
    for s in stages:
        stage_state.setdefault(s.code, "pending")
    if state.current_stage_id:
        cur = next((s for s in stages if s.id == state.current_stage_id), None)
        if cur:
            stage_state[cur.code] = "in_progress"

    def _stage_view(s: WorkflowStage) -> dict[str, Any]:
        return {
            "code": s.code, "name": s.name, "position": s.position,
            "description": s.description, "is_mandatory": s.is_mandatory,
            "estimated_minutes": s.estimated_minutes,
            "state": stage_state.get(s.code, "pending"),
        }

    current_stage = next(
        (s for s in stages if s.id == state.current_stage_id), None
    )

    minutes_remaining = sum(
        s.estimated_minutes for s in stages
        if stage_state.get(s.code) in (None, "pending", "in_progress")
        and s.is_mandatory
    )

    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "status": state.status,
        "version": state.version,
        "current_stage": (
            {"code": current_stage.code, "name": current_stage.name,
             "position": current_stage.position,
             "is_mandatory": current_stage.is_mandatory,
             "estimated_minutes": current_stage.estimated_minutes}
            if current_stage else None
        ),
        "stages": [_stage_view(s) for s in stages],
        "stage_minutes_remaining": minutes_remaining,
        "history": [
            {
                "action": h.action,
                "from": h.from_stage_code,
                "to": h.to_stage_code,
                "stage": h.stage_code or h.to_stage_code,
                "employee": h.employee_name,
                "reason": h.reason,
                "at": h.created_at.isoformat() if h.created_at else None,
            }
            for h in history
        ],
    }


async def finalize_for_token_status(
    db: AsyncSession, token: Token, token_status: str
) -> Optional[TokenStageLog]:
    """Queue-action hook (called from perform_token_action BEFORE its commit —
    same transaction): keep the workflow ledger consistent when the token
    itself completes or is cancelled (B10)."""
    if token_status not in ("completed", "cancelled"):
        return None

    state = await get_state_for_token(db, token.id, token.organization_id)
    if not state or state.status != STATUS_ACTIVE:
        return None

    workflow = await db.get(ServiceWorkflow, state.workflow_id)
    current = await db.get(WorkflowStage, state.current_stage_id) \
        if state.current_stage_id else None
    if current is None:
        return None

    new_status = STATUS_COMPLETED if token_status == "completed" else STATUS_CANCELLED

    await _cas_transition(
        db, state, expected_stage_id=current.id,
        new_stage_id=None, new_status=new_status,
        terminal_at="completed" if new_status == STATUS_COMPLETED else "cancelled",
    )
    log = TokenStageLog(
        token_id=token.id,
        organization_id=token.organization_id,
        state_id=state.id,
        workflow_id=workflow.id,
        action=ACTION_COMPLETE if new_status == STATUS_COMPLETED else ACTION_CANCEL,
        from_stage_code=current.code,
        stage_code=current.code,
        stage_name=current.name,
        stage_position=current.position,
        stage_mandatory=current.is_mandatory,
        employee_name="system (token " + token_status + ")",
        reason=f"token {token_status}",
    )
    db.add(log)
    logger.info(
        "Workflow auto-{} for token {} (queue action)",
        new_status, token.id,
    )
    return log
