"""
tests/api/test_workflows.py
──────────────────────────
Phase 9 Workstream B — configurable multi-stage service workflows (MASTER §10).

Covers:
- Org configuration CRUD: create (illustrative 5-stage healthcare example as
  DATA), list, read per service, update (stage replace-all), deactivate.
- Duplicate workflow per service refused; foreign-tenant counter refused.
- Booking hook: a token booked onto a configured service opens its stage
  transaction automatically (stage 1 in_progress + start log).
- Legacy guarantee (B15): unconfigured services keep the single-stage flow —
  no workflow key on tracking, no state row.
- Employee stage lifecycle (B6–B10): advance-to-completion, mandatory-stage
  skip refusal, skippable-stage skip, recall to an earlier stage WITH reason
  (B8), terminal completion through the last stage, audit history entries
  with employee identity (B9).
- Authorization: no shift → 403 (SHIFT-003), foreign-org employee → 404,
  foreign-org admin read → 404.
- Tracking consistency (B11/B12): tracking payload carries stage progress and
  remaining mandatory stage minutes WITHOUT changing queue-metric semantics.
- Queue-action consistency (B10): token completion/cancellation finalizes the
  workflow ledger in the same transaction.
- Concurrency (B13): two simultaneous advances from the same stage — exactly
  one wins, the loser gets a retryable response (HTTP 409).

All data is disposable in-memory test-database data.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import org_service, token_service
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.service import Service
from models.token import TokenStatus
from tests.conftest import TestSessionLocal

# The worker status value text of a shift lifecycle is irrelevant here; we
# drive the full SHIFT-001/002/003 lifecycle through the published API.


def org_headers(org) -> dict:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


def employee_headers(employee) -> dict:
    return {"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"}


async def _make_org(db: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(
        db,
        {
            "name": f"Workflow Org {uid}",
            "registration_number": f"WF-{uid}",
            "org_type": "clinic",
            "email": f"wf_{uid}@test.org",
            "mobile": "9700000000",
            "password": "Password123!",
            "address": "12 Health Road",
            "city": "Mysuru",
            "state": "Karnataka",
        },
    )
    org.status = "approved"
    await db.commit()
    await db.refresh(org)
    return org


async def _make_counter(db: AsyncSession, org, number: int = 1) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Counter {number}",
        counter_number=number,
        token_prefix="WF",
        daily_limit=100,
        status=CounterStatus.ACTIVE,
    )
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def _make_service(db: AsyncSession, org, name="General Consultation") -> Service:
    service = Service(
        organization_id=org.id,
        name=f"{name} {uuid.uuid4().hex[:4]}",
        price=Decimal("0.00"),
        is_active=True,
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


async def _make_employee(db: AsyncSession, org):
    from app.services import employee_service
    from models.branch import Branch

    branch = Branch(
        organization_id=org.id,
        name=f"B {uuid.uuid4().hex[:4]}",
        branch_code=uuid.uuid4().hex[:8],
        address="BF",
        city="Mysuru",
        state="Karnataka",
        pincode="560002",
    )
    db.add(branch)
    await db.flush()
    employee = await employee_service.create_employee(
        db, org.id,
        {
            "name": f"Stage Op {uuid.uuid4().hex[:4]}",
            "email": f"stage_{uuid.uuid4().hex[:8]}@example.com",
            "employee_code": f"ST{uuid.uuid4().hex[:4].upper()}",
            "branch_id": branch.id,
            "password": "Password123!",
        },
    )
    return employee


async def _activate_shift(client: AsyncClient, org, employee, counter) -> None:
    """Full SHIFT-001/002/003 lifecycle so stage operations pass the guard."""
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(employee),
        json={"counter_id": counter.id, "services": [], "device_ready": True,
              "opening_cash": "0.00", "remarks": "workflow duty"},
    )
    assert res.status_code == 200, res.text
    shift_id = res.json()["data"]["id"]
    res = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org),
        json={"decision": "approve", "reason": "Stage operations approved"},
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(employee),
    )
    assert res.status_code == 200, res.text


HEALTHCARE_STAGES = [
    {"code": "registration", "name": "Registration", "is_mandatory": True},
    {"code": "measurement", "name": "Weight & Height", "is_mandatory": True,
     "estimated_minutes": 5},
    {"code": "counselling", "name": "Counselling", "is_mandatory": False,
     "estimated_minutes": 10},
    {"code": "pharmacy", "name": "Pharmacy", "is_mandatory": True,
     "estimated_minutes": 5},
    {"code": "prescription", "name": "Prescription Advice", "is_mandatory": True,
     "estimated_minutes": 5},
]


@pytest.fixture
async def org_with_workflow(client: AsyncClient, db_session: AsyncSession):
    """Org + counter + service + a 5-stage (illustrative healthcare) workflow."""
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org)
    res = await client.post(
        "/org/workflows",
        headers=org_headers(org),
        json={
            "service_id": service.id,
            "name": "Patient Visit Flow",
            "description": "Illustrative multi-stage healthcare flow",
            "stages": HEALTHCARE_STAGES,
        },
    )
    assert res.status_code == 200, res.text
    workflow = res.json()["data"]
    return org, counter, service, workflow


@pytest.fixture
async def staged_token(client: AsyncClient, db_session: AsyncSession, org_with_workflow):
    """A guest token booked onto the configured service (auto workflow start)."""
    org, counter, service, _workflow = org_with_workflow
    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Multi Stage Guest", "guest_mobile": "9876500000",
        "client_reference": f"wf_{uuid.uuid4().hex}",
    })
    assert res.status_code == 200, res.text
    token = res.json()["data"]
    return org, counter, service, token, _wf_state(client, org, token)


def _wf_state(_client, _org, _token):  # small readability helper
    return _token   # non-callable marker (state read via employee API)


@pytest.mark.api
async def test_create_workflow_configuration(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B1/B3: the illustrative 5-stage healthcare workflow round-trips as
    ordered configuration data — with positions normalized and no hospital
    concept in code anywhere."""
    org, counter, service, workflow = org_with_workflow
    assert workflow["name"] == "Patient Visit Flow"
    codes = [s["code"] for s in workflow["stages"]]
    assert codes == ["registration", "measurement", "counselling",
                     "pharmacy", "prescription"]
    assert [s["position"] for s in workflow["stages"]] == [1, 2, 3, 4, 5]
    # B4: mandatory/skippable mix preserved
    by_code = {s["code"]: s for s in workflow["stages"]}
    assert by_code["counselling"]["is_mandatory"] is False
    assert by_code["pharmacy"]["is_mandatory"] is True

    # Read back per service (B2)
    res = await client.get(
        f"/org/services/{service.id}/workflow", headers=org_headers(org)
    )
    assert res.status_code == 200
    assert res.json()["data"]["id"] == workflow["id"]


@pytest.mark.api
async def test_duplicate_workflow_per_service_refused(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B2/B15: one workflow per service — the second attempt is a 400."""
    org, counter, service, _workflow = org_with_workflow
    res = await client.post(
        "/org/workflows", headers=org_headers(org),
        json={"service_id": service.id, "name": "Duplicate",
              "stages": [{"code": "a", "name": "A"}]},
    )
    assert res.status_code == 400
    assert "already has a workflow" in res.json()["detail"]


@pytest.mark.api
async def test_org_workflow_config_page_renders(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B16: the org UI configuration page renders with the workflow listed."""
    org, counter, service, workflow = org_with_workflow
    # The org portal authenticates via the org_token cookie — the same JWT
    # issued for Bearer auth (views_org._get_org_or_redirect).
    page = await client.get(
        "/org/workflows",
        cookies={"org_token": create_access_token(org.id, "organization")},
    )
    assert page.status_code == 200
    assert "Service Workflows" in page.text
    assert "Patient Visit Flow" in page.text
    assert "Prescription Advice" in page.text


@pytest.mark.api
async def test_foreign_counter_in_stage_refused(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B14 tenant isolation: a stage may only bind a counter in THIS org."""
    org, counter, _service, _workflow = org_with_workflow
    other_org = await _make_org(db_session)
    foreign_counter = await _make_counter(db_session, other_org)
    fresh_service = await _make_service(db_session, org)
    res = await client.post(
        "/org/workflows", headers=org_headers(org),
        json={"service_id": fresh_service.id, "name": "X", "stages": [
            {"code": "x", "name": "X", "counter_id": foreign_counter.id},
        ]},
    )
    assert res.status_code == 400
    assert "not found in this organization" in res.json()["detail"]


@pytest.mark.api
async def test_update_workflow_replaces_stages_and_deactivates(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B1: rename, replace stage list, then delete-as-deactivate."""
    org, counter, service, workflow = org_with_workflow
    wid = workflow["id"]

    res = await client.put(
        f"/org/workflows/{wid}", headers=org_headers(org),
        json={"name": "Updated Flow",
              "stages": [{"code": "solo", "name": "Single Stage"}]},
    )
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["name"] == "Updated Flow"
    assert len(body["stages"]) == 1
    assert body["stages"][0]["code"] == "solo"

    res = await client.delete(f"/org/workflows/{wid}", headers=org_headers(org))
    assert res.status_code == 200
    res = await client.get(f"/org/workflows/{wid}", headers=org_headers(org))
    assert res.status_code == 200
    assert res.json()["data"]["is_active"] is False


@pytest.mark.api
async def test_foreign_org_cannot_read_workflow(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B14: another org's admin gets a normalized 404 — no leakage."""
    org, counter, service, workflow = org_with_workflow
    other_org = await _make_org(db_session)
    res = await client.get(
        f"/org/workflows/{workflow['id']}", headers=org_headers(other_org)
    )
    assert res.status_code == 404
    res = await client.get(
        f"/org/services/{service.id}/workflow", headers=org_headers(other_org)
    )
    assert res.status_code == 404


@pytest.mark.api
async def test_booking_auto_starts_stage_transaction(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """Integration: booking on a configured service opens stage 1 (start log)."""
    org, counter, service, workflow = org_with_workflow
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "First Stage Guest", "guest_mobile": "9876500100",
    })
    assert res.status_code == 200, res.text
    token = res.json()["data"]

    prog = await client.get(
        f"/employee/tokens/{token['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.status_code == 200, prog.text
    data = prog.json()["data"]
    assert data["status"] == "active"
    assert data["current_stage"]["code"] == "registration"
    states = {s["code"]: s["state"] for s in data["stages"]}
    assert states["registration"] == "in_progress"
    assert states["prescription"] == "pending"

    hist = await client.get(
        f"/employee/tokens/{token['id']}/workflow/history",
        headers=employee_headers(employee),
    )
    actions = [h["action"] for h in hist.json()["data"]]
    assert actions == ["start"]


@pytest.mark.api
async def test_legacy_single_stage_service_untouched(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B15: services without a workflow keep the legacy single-stage flow."""
    org, counter, _service, _workflow = org_with_workflow
    legacy_service = await _make_service(db_session, org, "Legacy Service")
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": legacy_service.id,
        "guest_name": "Legacy Guest", "guest_mobile": "9876500200",
    })
    assert res.status_code == 200
    token = res.json()["data"]

    prog = await client.get(
        f"/employee/tokens/{token['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.status_code == 200
    assert prog.json()["data"] is None  # no workflow view at all

    track_data = await token_service.get_token_tracking(db_session, token["id"])
    assert track_data["workflow"] == {}


@pytest.mark.api
async def test_full_lifecycle_advance_through_all_stages(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B7/B10: advancing every stage completes the workflow automatically."""
    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    codes = ["registration", "measurement", "counselling",
             "pharmacy", "prescription"]
    for expected_to in codes[1:]:
        res = await client.post(
            f"/employee/tokens/{token['id']}/workflow/advance",
            headers=employee_headers(employee), json={},
        )
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert data["current_stage"]["code"] == expected_to

    # Final advance: past the last stage → workflow completed (B10)
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "completed"
    assert data["current_stage"] is None
    assert {s["state"] for s in data["stages"]} == {"completed"}

    hist = await client.get(
        f"/employee/tokens/{token['id']}/workflow/history",
        headers=employee_headers(employee),
    )
    actions = [h["action"] for h in hist.json()["data"]]
    assert actions == ["start"] + ["advance"] * 5
    # B9: every transition carries the employee identity
    assert all(h["employee"] == employee.name for h in hist.json()["data"][1:])


@pytest.mark.api
async def test_mandatory_stage_cannot_be_skipped_but_skippable_can(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B4/B5: skipping a mandatory stage is refused; a skippable stage works."""
    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    # registration is mandatory
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/skip",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 400
    assert "mandatory" in res.json()["detail"]

    # advance registration → measurement (also mandatory)
    await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )
    # measurement is mandatory → refuse
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/skip",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 400

    # advance measurement → counselling (skippable, is_mandatory False)
    await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/skip",
        headers=employee_headers(employee),
        json={"reason": "No counselling needed today"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    # counselling skipped, pharma is now the stage
    assert data["current_stage"]["code"] == "pharmacy"
    states = {s["code"]: s["state"] for s in data["stages"]}
    assert states["counselling"] == "skipped"
    assert states["registration"] == "completed"
    assert states["measurement"] == "completed"


@pytest.mark.api
async def test_recall_to_earlier_stage_requires_reason(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B8: recall back to an earlier stage with a recorded reason + audit."""
    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    # registration -> measurement
    await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )

    # recall without reason → refused
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/recall",
        headers=employee_headers(employee),
        json={"target_stage_code": "registration"},
    )
    assert res.status_code == 400

    # recall FORWARD (not earlier) → refused
    await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )  # counselling
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/recall",
        headers=employee_headers(employee),
        json={"target_stage_code": "pharmacy",
              "reason": "Wrong direction attempt"},
    )
    assert res.status_code == 400

    # recall BACK with reason → allowed
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/recall",
        headers=employee_headers(employee),
        json={"target_stage_code": "registration",
              "reason": "Registration details were incomplete"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["current_stage"]["code"] == "registration"
    states = {s["code"]: s["state"] for s in data["stages"]}
    assert states["registration"] == "in_progress"
    assert states["measurement"] in ("completed", "in_progress")

    hist = await client.get(
        f"/employee/tokens/{token['id']}/workflow/history",
        headers=employee_headers(employee),
    )
    entries = hist.json()["data"]
    recall_entries = [h for h in entries if h["action"] == "recall"]
    assert len(recall_entries) == 1
    assert recall_entries[0]["reason"] == "Registration details were incomplete"
    assert recall_entries[0]["from"] == "counselling"
    assert recall_entries[0]["to"] == "registration"


@pytest.mark.api
async def test_stage_operations_require_active_shift(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B6 (SHIFT-003): no approved+activated shift → 403 on every action."""
    org, counter, service, token, _ = staged_token
    shiftless = await _make_employee(db_session, org)

    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(shiftless), json={},
    )
    assert res.status_code == 403
    assert "shift" in res.json()["detail"].lower()


@pytest.mark.api
async def test_foreign_org_employee_cannot_touch_stage(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B14: another org's employee gets a normalized 404 for the token."""
    org, counter, service, token, _ = staged_token
    other_org = await _make_org(db_session)
    intruder = await _make_employee(db_session, other_org)

    res = await client.get(
        f"/employee/tokens/{token['id']}/workflow",
        headers=employee_headers(intruder),
    )
    assert res.status_code == 404
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(intruder), json={},
    )
    assert res.status_code == 404


@pytest.mark.api
async def test_tracking_carries_stage_progress_and_minutes(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B11/B12: customers see workflow progress; stage minutes are surfaced
    WITHOUT changing the queue-metric semantics of estimated_wait_minutes."""
    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    track = await token_service.get_token_tracking(db_session, token["id"])
    workflow = track["workflow"]
    assert workflow["workflow_name"] == "Patient Visit Flow"
    assert workflow["current_stage"]["code"] == "registration"
    # remaining mandatory minutes: registration(0) + measurement(5)
    #   + pharmacy(5) + prescription(5)   [counselling optional, excluded]
    assert workflow["stage_minutes_remaining"] == 15

    # B12: the queue estimate is UNCHANGED for the same data
    assert track["estimated_wait_minutes"] == track["people_ahead"] * (
        service.estimated_minutes or 10
    )


@pytest.mark.api
async def test_token_completion_and_cancellation_finalize_workflow(
    client: AsyncClient, db_session: AsyncSession, org_with_workflow
):
    """B10: the queue actions auto-close the stage ledger — same transaction."""
    org, counter, service, workflow = org_with_workflow

    # completion path
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)
    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Complete Path", "guest_mobile": "9876500300",
    })
    token_c = res.json()["data"]
    for _ in range(3):
        await client.post(
            f"/employee/tokens/{token_c['id']}/workflow/advance",
            headers=employee_headers(employee), json={},
        )
    # Current stage is now 'pharmacy'. The queue action finalizes the ledger
    # from the CURRENT stage onward.
    # Org queue action: call → complete (matches _VALID_TRANSITIONS)
    res = await client.post(
        f"/org/tokens/{token_c['id']}/action", headers=org_headers(org),
        json={"action": "call"},
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        f"/org/tokens/{token_c['id']}/action", headers=org_headers(org),
        json={"action": "progress"},
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        f"/org/tokens/{token_c['id']}/action", headers=org_headers(org),
        json={"action": "complete"},
    )
    assert res.status_code == 200, res.text

    prog = await client.get(
        f"/employee/tokens/{token_c['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.json()["data"]["status"] == "completed"
    states = {s["code"]: s["state"] for s in prog.json()["data"]["stages"]}
    assert states["pharmacy"] == "completed"

    # cancellation path
    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Cancel Path", "guest_mobile": "9876500400",
    })
    token_x = res.json()["data"]
    res = await client.post(
        f"/org/tokens/{token_x['id']}/action", headers=org_headers(org),
        json={"action": "cancel"},
    )
    assert res.status_code == 200, res.text
    prog = await client.get(
        f"/employee/tokens/{token_x['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.json()["data"]["status"] == "cancelled"

    # post-terminal transitions are refused
    res = await client.post(
        f"/employee/tokens/{token_x['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 400
    assert "cancelled" in res.json()["detail"]


@pytest.mark.api
async def test_workflow_terminal_explicit_complete_and_cancel(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B10: explicit complete/cancel endpoints exist for exceptional exits."""
    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/cancel",
        headers=employee_headers(employee),
        json={"reason": "Customer left before service started"},
    )
    assert res.status_code == 200
    prog = await client.get(
        f"/employee/tokens/{token['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.json()["data"]["status"] == "cancelled"

    # A second token: explicit complete at stage 1
    res = await client.post("/public/kiosk/book", json={
        "organization_id": org.id, "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Explicit Complete", "guest_mobile": "9876500500",
    })
    token2 = res.json()["data"]
    res = await client.post(
        f"/employee/tokens/{token2['id']}/workflow/complete",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 200
    prog = await client.get(
        f"/employee/tokens/{token2['id']}/workflow",
        headers=employee_headers(employee),
    )
    assert prog.json()["data"]["status"] == "completed"


@pytest.mark.api
async def test_concurrent_stage_advance_one_wins(
    client: AsyncClient, db_session: AsyncSession, staged_token
):
    """B13: two operators advancing the SAME stage race — the CAS UPDATE
    (current_stage_id = expected) guarantees exactly one winner; the loser
    (whose expectation no longer matches the row) gets StageRaceError → HTTP
    409, and no stage is ever double-advanced or double-logged.

    Deterministic simulation of the two-worker race: worker 2 captured the
    stage pointer BEFORE worker 1 acted, then re-plays its CAS against a row
    worker 1 already moved — exactly the conflict window the CAS closes.
    """
    from app.services import workflow_service

    org, counter, service, token, _ = staged_token
    emp1 = await _make_employee(db_session, org)
    emp2 = await _make_employee(db_session, org)
    await _activate_shift(client, org, emp1, counter)
    await _activate_shift(client, org, emp2, counter)

    # ── Worker 2's stale read (before the transition) ──
    stale_state = await workflow_service.get_state_for_token(
        db_session, token["id"], org.id
    )
    version_before = stale_state.version
    expected_stage_id = stale_state.current_stage_id  # stage 1 — captured NOW

    # ── Worker 1 wins the race (advance stage 1 → 2) ──
    progress = await workflow_service.advance_stage(
        db_session, token["id"], org.id, emp1
    )
    assert progress["current_stage"]["code"] == "measurement"

    # ── Worker 2 replays its CAS with its STALE expectation ──
    from app.services.workflow_service import StageRaceError

    with pytest.raises(StageRaceError):
        await workflow_service._cas_transition(
            db_session, stale_state,
            expected_stage_id=expected_stage_id,  # stage 1 (stale)
            new_stage_id=None, new_status=workflow_service.STATUS_COMPLETED,
        )

    # Worker 1's transition is intact; no double-advance, no duplicate log.
    fresh = await token_service.get_token_tracking(db_session, token["id"])
    assert fresh["workflow"]["current_stage"]["code"] == "measurement"

    hist = await workflow_service.get_token_stage_history(
        db_session, token["id"], org.id
    )
    advances = [h for h in hist if h.action == "advance"]
    assert len(advances) == 1, "the losing transition must not be logged"
    assert progress["version"] != version_before  # version monotonic


@pytest.mark.api
async def test_stage_race_maps_to_http_409_retryable(
    client: AsyncClient, db_session: AsyncSession, staged_token, monkeypatch
):
    """B13 API contract: a lost CAS race surfaces as a retryable 409, not a
    500 or a silent double-advance."""
    from app.services import workflow_service

    org, counter, service, token, _ = staged_token
    employee = await _make_employee(db_session, org)
    await _activate_shift(client, org, employee, counter)

    async def lose_race(*args, **kwargs):
        raise workflow_service.StageRaceError(
            "Stage changed concurrently — reload the token state and retry"
        )

    monkeypatch.setattr(workflow_service, "_cas_transition", lose_race)
    res = await client.post(
        f"/employee/tokens/{token['id']}/workflow/advance",
        headers=employee_headers(employee), json={},
    )
    assert res.status_code == 409
    assert "changed concurrently" in res.json()["detail"]
