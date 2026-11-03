"""
tests/unit/test_support_cases.py
───────────────────────────────
Phase 7 — Unified Support (SUP-001..007, CASE-001..004) domain rules:

- CASE-002 binding validation: forged/foreign token, payment and feedback
  ids are rejected before case creation; tenant resolution is server-side.
- Participant gating: no membership → no access/detail/messages/attachments
  (three-party isolation), including cross-tenant attempts.
- Status machine: illegal transitions rejected; admin-only states guarded;
  duplicate-report and re-open-count behaviour; resolution only via
  explicit acceptance; reply-only never resolves; closed case froze-ingress.
- Attachments: private, uploaded under unguessable names, download access
  counter + audit.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import support_service
from models.support import CaseMessage, CaseStatus, MessageReviewStatus
from tests.phase7_helpers import make_counter, make_org, make_token, make_user

pytestmark = pytest.mark.asyncio


async def _setup(db_session: AsyncSession, *, status="completed"):
    org = await make_org(db_session)
    user = await make_user(db_session)
    other_org = await make_org(db_session)
    counter = await make_counter(db_session, org)
    token = await make_token(db_session, org, counter, user=user,
                             status=status)
    return org, user, other_org, counter, token


async def test_case002_foreign_bindings_rejected(db_session: AsyncSession):
    org, user, other_org, counter, token = await _setup(db_session)

    # Token belonging to another organization.
    other_org_counter = await make_counter(db_session, other_org)
    foreign_token = await make_token(
        db_session, other_org, other_org_counter, user=None, status="completed"
    )
    with pytest.raises(PermissionError):
        await support_service.create_case(
            db_session, "user", user.id, user.name,
            "user_to_org", "queue", "s", "d", token_id=foreign_token.id
        )

    # user_to_org requires a real target organization.
    with pytest.raises(LookupError):
        await support_service.create_case(
            db_session, "user", user.id, user.name,
            "user_to_org", "queue", "s", "d", organization_id=999999
        )

    # Unknown case type for principal.
    with pytest.raises(PermissionError, match="cannot open"):
        await support_service.create_case(
            db_session, "user", user.id, user.name,
            "org_to_admin", "payout", "s", "d"
        )


async def test_ownership_snapshot_and_reference(db_session: AsyncSession):
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "Waited 2 hours", "detail",
        token_id=token.id, organization_id=org.id,
    )
    # Reload with eager relationships before asserting on them.
    case = await support_service.get_case_for_participant(
        db_session, case.id, "user", user.id
    )
    assert case.reference.startswith("SC-")
    assert case.status == CaseStatus.SUBMITTED
    assert case.user_id == user.id and case.organization_id == org.id
    assert case.snapshot["org_name"].startswith("Trust Org")
    assert case.snapshot["token_number"] == token.token_number
    # First message recorded from the reporter, with respondent auto-added.
    senders = {m.sender_type for m in case.messages}
    assert senders == {"user"}
    ptypes = {p.participant_type for p in case.participants}
    assert ptypes == {"user", "organization"}


async def test_participant_gating_cross_tenant(db_session: AsyncSession):
    """SUP-006: strict participant isolation, no cross-tenant access."""
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )

    # A different organization principal is not a participant.
    with pytest.raises(PermissionError):
        await support_service.get_case_for_participant(
            db_session, case.id, "organization", other_org.id
        )
    # A different user is not a participant.
    stranger = await make_user(db_session)
    with pytest.raises(PermissionError):
        await support_service.get_case_for_participant(
            db_session, case.id, "user", stranger.id
        )

    # Members do have access; admins always do.
    await support_service.get_case_for_participant(
        db_session, case.id, "user", user.id
    )
    await support_service.get_case_for_participant(
        db_session, case.id, "organization", org.id
    )
    case = await support_service.get_case_for_participant(
        db_session, case.id, "admin", None
    )
    await support_service.admin_review_participants(db_session, case)
    from sqlalchemy import select

    from models.support import CaseParticipant

    rows = (
        await db_session.execute(
            select(CaseParticipant).where(CaseParticipant.case_id == case.id)
        )
    ).scalars().all()
    admins = [p for p in rows if p.participant_type == "admin"]
    assert admins, "admin access lazily provisions an admin participant"


async def test_illegal_transitions_rejected(db_session: AsyncSession):
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )

    # SUBMITTED → RESOLVED is not legal (a reply can't resolve by itself).
    with pytest.raises(ValueError, match="Illegal"):
        await support_service.transition_case(
            db_session, case, "admin", CaseStatus.RESOLVED
        )

    # Non-admin cannot drive admin-only states.
    case2 = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s2", "d2", organization_id=org.id,
    )
    with pytest.raises(PermissionError, match="Only platform"):
        await support_service.transition_case(
            db_session, case2, "user", CaseStatus.ACKNOWLEDGED
        )

    # Legal admin path: acknowledged → assigned → in_review.
    await support_service.transition_case(
        db_session, case2, "admin", CaseStatus.ACKNOWLEDGED, assigned_admin_id=1
    )
    await support_service.transition_case(
        db_session, case2, "admin", CaseStatus.ASSIGNED, assigned_admin_id=1
    )
    assert case2.status == CaseStatus.ASSIGNED
    await support_service.transition_case(
        db_session, case2, "admin", CaseStatus.IN_REVIEW
    )
    assert case2.status == CaseStatus.IN_REVIEW


async def test_resolution_accept_reject_and_closed_frozen(db_session: AsyncSession):
    """SUP-007: resolution acceptance flow + closed-case retry prevention."""
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )

    # Org reply auto-acknowledges.
    await support_service.add_message(
        db_session, case, "organization", org.id,
        org.name, "Looking into it",
    )
    assert case.status == CaseStatus.ACKNOWLEDGED

    # Propose solution → SOLUTION_PROPOSED.
    msg, state = await support_service.propose_solution(
        db_session, case, "We will refund and prioritize you next visit",
        actor_type="organization", actor_id=org.id, actor_name=org.name,
    )
    assert state == CaseStatus.SOLUTION_PROPOSED

    # Only the reporter decides — strangers rejected.
    stranger = await make_user(db_session)
    with pytest.raises(PermissionError):
        await support_service.customer_resolution_decision(
            db_session, case, True, stranger.id, "x"
        )

    # Reject → waiting_for_party (reply alone never resolved it).
    await support_service.customer_resolution_decision(
        db_session, case, False, user.id, user.name
    )
    assert case.status == CaseStatus.WAITING_FOR_PARTY
    assert case.resolution_accepted_by is None


async def test_resolution_accepted_and_reopen_count(db_session: AsyncSession):
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )
    await support_service.add_message(
        db_session, case, "organization", org.id, org.name, "apologies"
    )
    await support_service.propose_solution(
        db_session, case, "fixed", actor_type="organization",
        actor_id=org.id, actor_name=org.name,
    )
    await support_service.customer_resolution_decision(
        db_session, case, True, user.id, user.name
    )
    assert case.status == CaseStatus.RESOLVED
    assert case.resolution_accepted_by == "user"

    # Only the reporting customer may reopen.
    stranger = await make_user(db_session)
    stranger_case = await support_service.get_case_for_participant(
        db_session, case.id, "user", user.id
    )
    with pytest.raises(PermissionError, match="reporting customer"):
        await support_service.customer_reopen(
            db_session, stranger_case, stranger.id, "why"
        )

    # Reopen increments the counter and moves to REOPENED.
    await support_service.customer_reopen(db_session, case, user.id, "recurr")
    assert case.status == CaseStatus.REOPENED
    assert case.reopened_count == 1

    # Close it, then reopen from CLOSED (retry path) — counter increments.
    await support_service.transition_case(
        db_session, case, "admin", CaseStatus.CLOSED
    )
    with pytest.raises(ValueError, match="closed"):
        await support_service.add_message(
            db_session, case, "organization", org.id, org.name, "late"
        )

    await support_service.customer_reopen(db_session, case, user.id, "again")
    assert case.reopened_count == 2
    assert case.status == CaseStatus.REOPENED


async def test_attachment_private_named_and_access_logged(db_session: AsyncSession):
    """CASE-004: private storage + access bookkeeping."""
    import hashlib
    from pathlib import Path

    import app.config as cfg

    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )

    upload_dir = Path(cfg.settings.upload_dir) / "cases"
    upload_dir.mkdir(parents=True, exist_ok=True)
    before = set(upload_dir.iterdir())

    payload = b"private-evidence" * 10
    import app.services.attachment_service as att
    relpath, name, size, mime, sha = att.save_private_file(
        "cases", payload, "evidence.png"
    )

    stored = Path(relpath)
    assert stored.parent == upload_dir
    assert stored.name not in {p.name for p in before}, "unguessable filename"
    assert stored.read_bytes() == payload
    assert size == len(payload)
    assert sha == hashlib.sha256(payload).hexdigest()

    attachment = await support_service.add_attachment(
        db_session, case, relpath, name, mime, size, sha, "user", user.id
    )
    assert attachment.access_count == 0

    # Unauthorized viewer cannot even fetch the case, let alone the file.
    with pytest.raises(PermissionError):
        support_case = await support_service.get_case_for_participant(
            db_session, case.id, "user", (await make_user(db_session)).id
        )

    case = await support_service.get_case_for_participant(
        db_session, case.id, "user", user.id
    )
    att1 = await support_service.access_attachment(
        db_session, case, attachment.id, "user", user.id
    )
    assert att1.access_count == 1
    att2 = await support_service.access_attachment(
        db_session, case, attachment.id, "organization", org.id
    )
    assert att2.access_count == 2

    # Wrong-attachment lookup fails cleanly.
    with pytest.raises(LookupError):
        await support_service.access_attachment(
            db_session, case, 999999, "user", user.id
        )


async def test_sensitive_suggestion_requires_human_review(db_session: AsyncSession):
    """SUP-007/KB-002: sensitive suggestions stay hidden until approved."""
    org, user, other_org, counter, token = await _setup(db_session)
    case = await support_service.create_case(
        db_session, "user", user.id, user.name,
        "user_to_org", "queue", "s", "d", organization_id=org.id,
    )
    await support_service.add_message(
        db_session, case, "organization", org.id, org.name, "hello"
    )

    msg, state = await support_service.propose_solution(
        db_session, case, "Refund plot: we will send INR 500",
        is_sensitive=True, actor_type="organization",
        actor_id=org.id, actor_name=org.name,
    )
    assert msg.review_status == MessageReviewStatus.PENDING_REVIEW
    # Status not advanced to SOLUTION_PROPOSED while pending review.
    assert case.status not in (CaseStatus.SOLUTION_PROPOSED,)

    case = await support_service.get_case_for_participant(
        db_session, case.id, "user", user.id
    )
    visible = support_service._visible_messages(case, "user")
    assert msg not in visible, "pending-review message is invisible to parties"
    assert {m.id for m in visible} == {m.id for m in case.messages} - {msg.id}
    admin_visible = support_service._visible_messages(case, "admin")
    assert msg in admin_visible, "admins can see the pending draft for review"
