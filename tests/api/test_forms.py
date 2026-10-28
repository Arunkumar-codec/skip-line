"""
tests/api/test_forms.py
───────────────────────
Comprehensive test suite for Phase 2 Dynamic Form Builder:
- Form creation with configurable fields, options, and conditional logic
- FORM-007 security rule: Automatic rejection of passwords, PINs, OTPs, CVVs, card credentials (422)
- Form publishing, unpublishing, retirement, and duplication
- Versioning increment on field modifications
- Form submission validation on booking (required fields, regex constraints, conditional visibility)
- Immutable form snapshots: historical token answers and frozen JSON schema remain unchanged by later form edits
- Tenant isolation and cross-tenant mutation rejection
"""

import json
import uuid
from decimal import Decimal
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import org_service, user_service, token_service, form_service
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.custom_form import CustomForm, TokenFormSnapshot
from models.service import Service


@pytest.fixture
async def sample_org(db_session: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Dental Care {uid}",
        "registration_number": f"DENT-{uid}",
        "org_type": "clinic",
        "email": f"dental_{uid}@test.com",
        "mobile": "9876543211",
        "password": "Password123!",
        "address": "45 Clinic Road",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.fixture
async def other_org(db_session: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"City Hospital {uid}",
        "registration_number": f"HOSP-{uid}",
        "org_type": "hospital",
        "email": f"hosp_{uid}@test.com",
        "mobile": "9876543212",
        "password": "Password123!",
        "address": "100 Main Ave",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.fixture
async def sample_user(db_session: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    return await user_service.register_user(db_session, {
        "name": "Priya Sharma",
        "email": f"priya_{uid}@test.com",
        "mobile": "9876501234",
        "password": "Password123!",
    })


@pytest.fixture
def org_headers(sample_org):
    token = create_access_token(sample_org.id, "organization")
    return {"Authorization": f"Bearer {token}", "Cookie": f"org_token={token}"}


@pytest.fixture
def other_org_headers(other_org):
    token = create_access_token(other_org.id, "organization")
    return {"Authorization": f"Bearer {token}", "Cookie": f"org_token={token}"}


@pytest.fixture
def user_headers(sample_user):
    token = create_access_token(sample_user.id, "user")
    return {"Authorization": f"Bearer {token}", "Cookie": f"user_token={token}"}


# ── 1. Form Creation & Forbidden Fields Rejection (FORM-007) ──────────────────

@pytest.mark.asyncio
async def test_form_creation_and_field_definitions(client: AsyncClient, sample_org, org_headers):
    payload = {
        "title": "Dental Consultation Intake",
        "description": "Patient preliminary assessment questionnaire",
        "is_published": False,
        "fields": [
            {
                "field_key": "has_allergies",
                "label": "Do you have any drug allergies?",
                "field_type": "select",
                "is_required": True,
                "options": [
                    {"option_label": "Yes", "option_value": "yes", "order_index": 1},
                    {"option_label": "No", "option_value": "no", "order_index": 2},
                ],
            },
            {
                "field_key": "allergy_details",
                "label": "Specify allergic medications",
                "field_type": "text",
                "placeholder": "e.g. Penicillin, Aspirin",
                "is_required": False,
                "conditions": [
                    {
                        "depends_on_field_key": "has_allergies",
                        "operator": "equals",
                        "trigger_value": "yes",
                        "action": "show",
                    }
                ],
            },
            {
                "field_key": "emergency_contact",
                "label": "Emergency Contact Number",
                "field_type": "text",
                "is_required": True,
                "validation_regex": r"^\d{10}$",
            },
        ],
    }

    res = await client.post("/org/forms", headers=org_headers, json=payload)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["title"] == "Dental Consultation Intake"
    assert data["version"] == 1
    assert data["is_published"] is False
    assert len(data["fields"]) == 3

    # Check options and conditions persisted
    f_allergies = next(f for f in data["fields"] if f["field_key"] == "has_allergies")
    assert len(f_allergies["options"]) == 2

    f_details = next(f for f in data["fields"] if f["field_key"] == "allergy_details")
    assert len(f_details["conditions"]) == 1
    assert f_details["conditions"][0]["operator"] == "equals"
    assert f_details["conditions"][0]["trigger_value"] == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden_term", [
    "password", "user_password", "card_pin", "netbanking_password",
    "enter_otp", "cvv_code", "credit_card_number", "debit_card",
])
async def test_form_builder_rejects_prohibited_fields(client: AsyncClient, sample_org, org_headers, forbidden_term):
    """FORM-007: Prohibit fields attempting to capture credentials, passwords, OTPs, or payment card details."""
    payload = {
        "title": "Insecure Intake Form",
        "is_published": False,
        "fields": [
            {
                "field_key": forbidden_term,
                "label": f"Please enter your {forbidden_term}",
                "field_type": "text",
                "is_required": True,
            }
        ],
    }
    res = await client.post("/org/forms", headers=org_headers, json=payload)
    assert res.status_code == 422
    assert "Prohibited field detected" in res.json()["detail"]


# ── 2. Lifecycle: Publish, Retire, Duplicate, Versioning ─────────────────────

@pytest.mark.asyncio
async def test_form_lifecycle_publish_retire_duplicate(client: AsyncClient, sample_org, org_headers):
    # 1. Create Draft Form
    c_res = await client.post("/org/forms", headers=org_headers, json={
        "title": "General Intake",
        "description": "Standard registration form",
        "fields": [{"field_key": "symptom", "label": "Primary Symptom", "field_type": "text", "is_required": True}],
    })
    form_id = c_res.json()["data"]["id"]

    # 2. Publish Form
    pub_res = await client.post(f"/org/forms/{form_id}/publish?is_published=true", headers=org_headers)
    assert pub_res.status_code == 200
    assert pub_res.json()["data"]["is_published"] is True

    # 3. Duplicate Form
    dup_res = await client.post(f"/org/forms/{form_id}/duplicate", headers=org_headers)
    assert dup_res.status_code == 200
    dup_data = dup_res.json()["data"]
    assert dup_data["id"] != form_id
    assert dup_data["title"] == "General Intake (Copy)"
    assert dup_data["is_published"] is False
    assert len(dup_data["fields"]) == 1

    # 4. Retire Original Form
    ret_res = await client.post(f"/org/forms/{form_id}/retire", headers=org_headers)
    assert ret_res.status_code == 200
    ret_data = ret_res.json()["data"]
    assert ret_data["is_active"] is False
    assert ret_data["is_published"] is False


@pytest.mark.asyncio
async def test_form_versioning_on_field_update(client: AsyncClient, sample_org, org_headers):
    c_res = await client.post("/org/forms", headers=org_headers, json={
        "title": "Consultation Form",
        "fields": [{"field_key": "reason", "label": "Visit Reason", "field_type": "text"}],
    })
    form_id = c_res.json()["data"]["id"]
    assert c_res.json()["data"]["version"] == 1

    # Update fields -> version increments to 2
    u_res = await client.put(f"/org/forms/{form_id}", headers=org_headers, json={
        "title": "Consultation Form Updated",
        "fields": [
            {"field_key": "reason", "label": "Visit Reason", "field_type": "text"},
            {"field_key": "notes", "label": "Additional Notes", "field_type": "text"},
        ],
    })
    assert u_res.status_code == 200
    assert u_res.json()["data"]["version"] == 2
    assert len(u_res.json()["data"]["fields"]) == 2


# ── 3. Submission Validation & Immutable Snapshots ────────────────────────────

@pytest.mark.asyncio
async def test_form_validation_and_immutable_snapshots_on_booking(
    db_session: AsyncSession,
    sample_org,
    sample_user,
    org_headers,
    user_headers,
    client: AsyncClient,
):
    # 1. Create a Service and Counter
    service = Service(organization_id=sample_org.id, name="Cardiology Consult", price=Decimal("750.00"), estimated_minutes=25)
    counter = Counter(organization_id=sample_org.id, counter_name="Cardio Desk", counter_number=1, token_prefix="H")
    db_session.add_all([service, counter])
    await db_session.commit()
    await db_session.refresh(service)
    await db_session.refresh(counter)

    # 2. Create and Publish Form attached to this service
    form = await form_service.create_form(
        db=db_session,
        organization_id=sample_org.id,
        title="Cardio Pre-Screening",
        service_id=service.id,
        is_published=True,
        fields_data=[
            {
                "field_key": "chest_pain",
                "label": "Are you currently experiencing chest pain?",
                "field_type": "select",
                "is_required": True,
                "options": [
                    {"option_label": "Yes", "option_value": "yes"},
                    {"option_label": "No", "option_value": "no"},
                ],
            },
            {
                "field_key": "pain_severity",
                "label": "Pain Severity (1-10)",
                "field_type": "number",
                "is_required": False,
                "conditions": [
                    {
                        "depends_on_field_key": "chest_pain",
                        "operator": "equals",
                        "trigger_value": "yes",
                        "action": "require",
                    }
                ],
            },
            {
                "field_key": "contact_phone",
                "label": "Contact Phone Number",
                "field_type": "text",
                "is_required": True,
                "validation_regex": r"^\d{10}$",
            },
        ],
    )

    # 3. Missing required field -> booking rejected
    with pytest.raises(Exception) as exc_missing:
        await token_service.book_token(
            db=db_session,
            org_id=sample_org.id,
            counter_id=counter.id,
            service_id=service.id,
            user_id=sample_user.id,
            form_answers={"contact_phone": "9876543210"},  # Missing chest_pain
        )
    assert "chest_pain' is required" in str(exc_missing.value)

    # 4. Regex mismatch -> booking rejected
    with pytest.raises(Exception) as exc_regex:
        await token_service.book_token(
            db=db_session,
            org_id=sample_org.id,
            counter_id=counter.id,
            service_id=service.id,
            user_id=sample_user.id,
            form_answers={"chest_pain": "no", "contact_phone": "invalid_phone"},
        )
    assert "does not match required format" in str(exc_regex.value)

    # 5. Conditional requirement: chest_pain="yes" requires pain_severity
    with pytest.raises(Exception) as exc_cond:
        await token_service.book_token(
            db=db_session,
            org_id=sample_org.id,
            counter_id=counter.id,
            service_id=service.id,
            user_id=sample_user.id,
            form_answers={"chest_pain": "yes", "contact_phone": "9876543210"},  # Missing pain_severity
        )
    assert "Pain Severity" in str(exc_cond.value)

    # 6. Valid answers -> Booking succeeds with immutable snapshot
    token = await token_service.book_token(
        db=db_session,
        org_id=sample_org.id,
        counter_id=counter.id,
        service_id=service.id,
        user_id=sample_user.id,
        form_answers={
            "chest_pain": "yes",
            "pain_severity": "8",
            "contact_phone": "9876543210",
        },
    )
    assert token.id is not None

    # Verify snapshot in DB
    snap_stmt = select(TokenFormSnapshot).where(TokenFormSnapshot.token_id == token.id)
    snapshot = (await db_session.execute(snap_stmt)).scalar_one()
    assert snapshot.form_title == "Cardio Pre-Screening"
    assert snapshot.form_version == 1

    # Check answers in DB
    assert len(snapshot.answers) == 3
    ans_map = {a.field_key: a.answer_value for a in snapshot.answers}
    assert ans_map["chest_pain"] == "yes"
    assert ans_map["pain_severity"] == "8"
    assert ans_map["contact_phone"] == "9876543210"

    # 7. Modify Form to Version 2 (change title, delete pain_severity, add new field)
    await form_service.update_form(
        db=db_session,
        organization_id=sample_org.id,
        form_id=form.id,
        title="Cardio Intake V2 — Modified",
        fields_data=[
            {"field_key": "blood_group", "label": "Blood Group", "field_type": "text"},
        ],
    )

    # 8. Historical Snapshot Verification: Token 1's snapshot remains 100% untouched
    await db_session.refresh(snapshot)
    assert snapshot.form_title == "Cardio Pre-Screening"
    assert snapshot.form_version == 1
    ans_map_after = {a.field_key: a.answer_value for a in snapshot.answers}
    assert ans_map_after["chest_pain"] == "yes"
    assert "blood_group" not in ans_map_after

    # 9. Test API view for user and org
    user_res = await client.get(f"/user/api/tokens/{token.id}/form", headers=user_headers)
    assert user_res.status_code == 200
    assert user_res.json()["data"]["form_version"] == 1
    assert len(user_res.json()["data"]["answers"]) == 3

    org_res = await client.get(f"/org/tokens/{token.id}/form", headers=org_headers)
    assert org_res.status_code == 200
    assert org_res.json()["data"]["form_title"] == "Cardio Pre-Screening"


# ── 4. Tenant Isolation ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_form_tenant_isolation(
    client: AsyncClient, sample_org, other_org, org_headers, other_org_headers, db_session: AsyncSession
):
    # Org A creates a form
    form_a = await form_service.create_form(
        db=db_session,
        organization_id=sample_org.id,
        title="Org A Form",
        fields_data=[{"field_key": "field1", "label": "Label 1", "field_type": "text"}],
    )

    # Org B attempts to read Org A's form -> 404
    get_res = await client.get(f"/org/forms/{form_a.id}", headers=other_org_headers)
    assert get_res.status_code == 404

    # Org B attempts to update Org A's form -> 404
    upd_res = await client.put(f"/org/forms/{form_a.id}", headers=other_org_headers, json={"title": "Hacked Form"})
    assert upd_res.status_code == 404

    # Org B attempts to delete Org A's form -> 404
    del_res = await client.delete(f"/org/forms/{form_a.id}", headers=other_org_headers)
    assert del_res.status_code == 404

    # Org B attempts to link Org A's service to its form -> 400
    svc_a = Service(organization_id=sample_org.id, name="Secret Service", price=Decimal("10.00"))
    db_session.add(svc_a)
    await db_session.commit()
    await db_session.refresh(svc_a)

    bad_link_res = await client.post("/org/forms", headers=other_org_headers, json={
        "title": "Org B Malicious Form",
        "service_id": svc_a.id,
    })
    assert bad_link_res.status_code == 400
    assert "does not belong to this organization" in bad_link_res.json()["detail"]
