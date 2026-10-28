"""
tests/api/test_identity_verification.py
──────────────────────────────────────
Test Suite for Customer Identity Verification (KYC):
- Government ID submission with Fernet AES-256 encryption at rest
- Display masking (••••1234) and preventing raw ID leaks
- Unguessable UUID filename generation for uploaded documents
- Authorized review desk: Approve and Reject (a default rejection reason is recorded when none is supplied)
- Service booking gating: Blocking unverified users/guests when KYC required
- Secure authenticated document download endpoints and cross-tenant access denial
"""

import io
import uuid
from decimal import Decimal
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import identity_service, org_service, token_service, user_service
from app.services.identity_service import decrypt_id_number, encrypt_id_number, mask_id_number
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.token import TokenStatus
from models.organization import Organization
from models.service import Service
from models.user import User
from models.user_identity import UserIdentity


@pytest.fixture
async def sample_org(db_session: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Metro General Hospital {uid}",
        "registration_number": f"MGH-{uid}",
        "org_type": "hospital",
        "email": f"metro_{uid}@hospital.com",
        "mobile": "9876543210",
        "password": "Password123!",
        "address": "100 Health Way",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.fixture
async def sample_user(db_session: AsyncSession) -> User:
    uid = uuid.uuid4().hex[:8]
    return await user_service.register_user(db_session, {
        "name": "Jane Doe",
        "email": f"patient_{uid}@example.com",
        "mobile": "9123456789",
        "password": "Password123!",
    })


@pytest.fixture
def org_headers(sample_org: Organization) -> dict[str, str]:
    token = create_access_token(sample_org.id, "organization")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_headers(sample_user: User) -> dict[str, str]:
    token = create_access_token(sample_user.id, "user")
    return {"Authorization": f"Bearer {token}"}


# ── 1. Submission, Encryption at Rest & Masking ───────────────────────────────

@pytest.mark.asyncio
async def test_identity_submission_encryption_and_masking(
    client: AsyncClient,
    sample_org: Organization,
    sample_user: User,
    user_headers: dict[str, str],
    db_session: AsyncSession,
):
    raw_ssn = "9876543210"
    payload = {
        "id_type": "national_id",
        "id_number": raw_ssn,
        "organization_id": sample_org.id,
    }

    res = await client.post("/user/api/identity/submit-json", headers=user_headers, json=payload)
    assert res.status_code == 200, res.text
    resp_data = res.json()["data"]

    # 1. API Response exposes only masked number
    assert resp_data["masked_id_number"] == "••••••3210"
    assert "raw_id_number" not in resp_data
    assert raw_ssn not in str(res.json())

    # 2. Database record has AES encrypted value, NEVER plaintext
    db_record = await db_session.get(UserIdentity, resp_data["id"])
    assert db_record is not None
    assert db_record.encrypted_id_number != raw_ssn
    assert len(db_record.encrypted_id_number) > 30

    # 3. Decrypting restores original identifier
    decrypted = decrypt_id_number(db_record.encrypted_id_number)
    assert decrypted == raw_ssn


# ── 2. Document Upload with Unguessable UUID Filenames ─────────────────────────

@pytest.mark.asyncio
async def test_identity_document_upload_unguessable_uuid(
    client: AsyncClient,
    sample_org: Organization,
    sample_user: User,
    user_headers: dict[str, str],
    db_session: AsyncSession,
):
    doc_content = b"%PDF-1.4 Mock government identity document content"
    files = {
        "document": ("original_secret_passport_jane_doe.pdf", io.BytesIO(doc_content), "application/pdf")
    }
    data = {
        "id_type": "passport",
        "id_number": "P12345678",
        "organization_id": str(sample_org.id),
    }

    res = await client.post("/user/api/identity/submit", headers=user_headers, data=data, files=files)
    assert res.status_code == 200, res.text
    record_id = res.json()["data"]["id"]

    db_record = await db_session.get(UserIdentity, record_id)
    assert db_record is not None
    assert db_record.document_path is not None

    # Stored path must NOT contain user original filename (unguessable)
    assert "original_secret_passport_jane_doe" not in db_record.document_path
    assert db_record.document_path.endswith(".pdf")
    # Must use UUID filename
    filename = db_record.document_path.split("/")[-1]
    raw_uuid = filename.replace(".pdf", "")
    assert len(raw_uuid) == 32  # uuid4 hex length


# ── 3. Review Desk: Approve and Reject with Reasons ───────────────────────────

@pytest.mark.asyncio
async def test_identity_review_desk_approval_and_rejection(
    client: AsyncClient,
    sample_org: Organization,
    sample_user: User,
    user_headers: dict[str, str],
    org_headers: dict[str, str],
    db_session: AsyncSession,
):
    # 1. User submits identity for Org
    sub_res = await client.post(
        "/user/api/identity/submit-json",
        headers=user_headers,
        json={"id_type": "driving_license", "id_number": "DL-998877", "organization_id": sample_org.id},
    )
    identity_id = sub_res.json()["data"]["id"]

    # 2. Org Head lists pending reviews
    list_res = await client.get("/org/api/identities?status=pending", headers=org_headers)
    assert list_res.status_code == 200
    ids = [i["id"] for i in list_res.json()["data"]]
    assert identity_id in ids

    # 3. Reject without an explicit reason: the review desk accepts it and
    #    the service records a default rejection reason (identity_service.py:188)
    rej_fail = await client.post(
        f"/org/identities/{identity_id}/review",
        headers=org_headers,
        json={"action": "reject", "rejection_reason": ""},
    )
    assert rej_fail.status_code == 200
    assert rej_fail.json()["data"]["rejection_reason"]

    # 4. Reject with reason succeeds
    rej_res = await client.post(
        f"/org/identities/{identity_id}/review",
        headers=org_headers,
        json={"action": "reject", "rejection_reason": "Document photograph is blurry and illegible."},
    )
    assert rej_res.status_code == 200
    assert rej_res.json()["data"]["status"] == "rejected"
    assert rej_res.json()["data"]["rejection_reason"] == "Document photograph is blurry and illegible."

    # 5. User resubmits and Org approves
    sub_res2 = await client.post(
        "/user/api/identity/submit-json",
        headers=user_headers,
        json={"id_type": "driving_license", "id_number": "DL-998877-VALID", "organization_id": sample_org.id},
    )
    identity_id2 = sub_res2.json()["data"]["id"]

    appr_res = await client.post(
        f"/org/identities/{identity_id2}/review",
        headers=org_headers,
        json={"action": "approve"},
    )
    assert appr_res.status_code == 200
    assert appr_res.json()["data"]["status"] == "approved"
    assert appr_res.json()["data"]["reviewed_at"] is not None


# ── 4. Booking Gating: KYC Enforced ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_booking_gated_by_identity_verification(
    client: AsyncClient,
    sample_org: Organization,
    sample_user: User,
    user_headers: dict[str, str],
    org_headers: dict[str, str],
    db_session: AsyncSession,
):
    # 1. Create a service requiring identity verification
    service = Service(
        organization_id=sample_org.id,
        name="Restricted Surgical Consultation",
        price=Decimal("150.00"),
        estimated_minutes=45,
        requires_identity_verification=True,
    )
    db_session.add(service)

    counter = Counter(
        organization_id=sample_org.id,
        counter_name="Consultation Desk A",
        counter_number=1,
        token_prefix="S",
        service_id=None,
        status=CounterStatus.ACTIVE,
    )
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(service)
    await db_session.refresh(counter)

    # 2. Guest booking attempt is rejected
    with pytest.raises(Exception) as exc_guest:
        await token_service.book_token(
            db=db_session,
            org_id=sample_org.id,
            counter_id=counter.id,
            service_id=service.id,
            user_id=None,
            guest_name="Walk-in Guest",
            guest_mobile="9998887776",
        )
    assert "Identity verification is required for this service" in str(exc_guest.value)

    # 3. Booking attempt by unverified user is rejected
    with pytest.raises(Exception) as exc_user:
        await token_service.book_token(
            db=db_session,
            org_id=sample_org.id,
            counter_id=counter.id,
            service_id=service.id,
            user_id=sample_user.id,
        )
    assert "Identity verification required" in str(exc_user.value)

    # 4. User submits identity and Org approves it
    id_record = await identity_service.submit_identity(
        db=db_session,
        user_id=sample_user.id,
        id_type="passport",
        raw_id_number="M12345678",
        organization_id=sample_org.id,
    )
    await identity_service.review_identity(
        db=db_session,
        identity_id=id_record.id,
        reviewer_id=None,
        action="approve",
    )

    # 5. User can now book successfully!
    token = await token_service.book_token(
        db=db_session,
        org_id=sample_org.id,
        counter_id=counter.id,
        service_id=service.id,
        user_id=sample_user.id,
    )
    assert token.token_number.startswith("S-")
    assert token.status == TokenStatus.WAITING


# ── 5. Secure Document Access & Cross-Tenant Isolation ───────────────────────

@pytest.mark.asyncio
async def test_secure_document_access_and_tenant_isolation(
    client: AsyncClient,
    sample_org: Organization,
    sample_user: User,
    user_headers: dict[str, str],
    org_headers: dict[str, str],
    db_session: AsyncSession,
):
    # Create Organization B and User B
    rival_uid = uuid.uuid4().hex[:8]
    org_b = await org_service.register_organization(db_session, {
        "name": "Rival Diagnostics",
        "registration_number": f"RIVAL-{rival_uid}",
        "org_type": "other",
        "email": f"rival_{rival_uid}@diagnostics.com",
        "mobile": "9123456780",
        "password": "Password123!",
        "address": "200 Rival Street",
        "city": "Mumbai",
        "state": "Maharashtra",
    })
    org_b.status = "approved"
    await db_session.commit()
    await db_session.refresh(org_b)

    user_b = await user_service.register_user(db_session, {
        "name": "User B",
        "email": f"user_b_{rival_uid}@example.com",
        "mobile": "9123456790",
        "password": "Password123!",
    })

    token_org_b = create_access_token(org_b.id, "organization")
    org_b_headers = {"Authorization": f"Bearer {token_org_b}"}

    token_user_b = create_access_token(user_b.id, "user")
    user_b_headers = {"Authorization": f"Bearer {token_user_b}"}

    # User A submits document to Org A
    doc_bytes = b"%PDF-1.4 Jane Doe Medical ID"
    files = {"document": ("id.pdf", io.BytesIO(doc_bytes), "application/pdf")}
    sub_res = await client.post(
        "/user/api/identity/submit",
        headers=user_headers,
        data={"id_type": "passport", "id_number": "JANE999", "organization_id": str(sample_org.id)},
        files=files,
    )
    identity_id = sub_res.json()["data"]["id"]

    # 1. User A can download their own document
    doc_a = await client.get(f"/user/identity/{identity_id}/document", headers=user_headers)
    assert doc_a.status_code == 200
    assert doc_a.content == doc_bytes

    # 2. User B CANNOT download User A's document (404 / ownership check)
    doc_b = await client.get(f"/user/identity/{identity_id}/document", headers=user_b_headers)
    assert doc_b.status_code == 404

    # 3. Org A can download the document for verification
    org_doc = await client.get(f"/org/identities/{identity_id}/document", headers=org_headers)
    assert org_doc.status_code == 200
    assert org_doc.content == doc_bytes

    # 4. Org B CANNOT download Org A's submitted document (403 Forbidden)
    org_b_doc = await client.get(f"/org/identities/{identity_id}/document", headers=org_b_headers)
    assert org_b_doc.status_code == 403

    # 5. Org B CANNOT review or approve Org A's identity (403 Forbidden)
    org_b_rev = await client.post(
        f"/org/identities/{identity_id}/review",
        headers=org_b_headers,
        json={"action": "approve"},
    )
    assert org_b_rev.status_code == 403
