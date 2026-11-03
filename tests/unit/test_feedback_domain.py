"""
tests/unit/test_feedback_domain.py
─────────────────────────────────
Phase 7 — Verified Feedback (FDB-001..005) domain rules:

- Eligibility: only COMPLETED tokens, owner-only, guest tokens excluded.
- Uniqueness: one feedback row per token; REMOVED row re-activated in place.
- Public reviews: published-only, anonymous display, no PII.
- Organization actions: respond / report in own tenant only; orgs can
  never edit or delete feedback content.
- Moderation: state machine + audit trail, illegal transitions rejected.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import feedback_service
from models.counter import Counter  # noqa: F401 (FK target registration)
from models.feedback import Feedback, ModerationStatus
from models.token import Token, TokenStatus
from tests.phase7_helpers import make_counter, make_org, make_token, make_user

pytestmark = pytest.mark.asyncio


async def _setup(db_session: AsyncSession):
    org = await make_org(db_session)
    user = await make_user(db_session)
    counter = await make_counter(db_session, org)
    token = await make_token(db_session, org, counter, user=user,
                             status=TokenStatus.COMPLETED)
    return org, user, counter, token


async def test_feedback_requires_completed_token(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    token.status = TokenStatus.WAITING
    await db_session.flush()

    with pytest.raises(ValueError, match="completed"):
        await feedback_service.submit_feedback(db_session, token.id, user.id, 5)


async def test_feedback_ownership_enforced(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    stranger = await make_user(db_session, "Stranger")
    with pytest.raises(PermissionError, match="own"):
        await feedback_service.submit_feedback(db_session, token.id, stranger.id, 4)


async def test_feedback_one_row_per_token_and_resubmission_reactivates(
    db_session: AsyncSession,
):
    org, user, counter, token = await _setup(db_session)

    fb = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 2, comment="bad", is_anonymous=True
    )
    assert fb.moderation_status == ModerationStatus.PENDING

    with pytest.raises(ValueError, match="already been rated"):
        await feedback_service.submit_feedback(db_session, token.id, user.id, 5)

    # Moderate → REMOVE, then resubmission re-activates the SAME row.
    await feedback_service.moderate(db_session, fb.id, admin_id=None or 1,
                                    action="hide", reason="ok")
    await feedback_service.moderate(db_session, fb.id, 1, action="remove",
                                    reason="clean")
    assert fb.moderation_status == ModerationStatus.REMOVED

    again = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 5, comment="fixed"
    )
    assert again.id == fb.id, "re-submission must reuse the single token slot"
    assert again.moderation_status == ModerationStatus.PENDING
    assert again.rating_overall == 5

    rows = (await db_session.execute(
        select(Feedback).where(Feedback.token_id == token.id)
    )).scalars().all()
    assert len(rows) == 1


async def test_public_reviews_published_only_and_no_pii(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    token2 = await make_token(db_session, org, counter, user=user,
                              status=TokenStatus.COMPLETED)

    fb1 = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 5, comment="Great service",
        is_anonymous=True,
    )
    fb2 = await feedback_service.submit_feedback(
        db_session, token2.id, user.id, 1, comment="Poor", is_anonymous=False
    )
    # fb1 still PENDING → invisible; fb2 published.
    await feedback_service.moderate(db_session, fb2.id, 1, action="publish",
                                    reason=None)

    # Second org must see nothing (tenant scoping).
    other_org = await make_org(db_session)
    assert await feedback_service.list_public_reviews(db_session, other_org.id) == []

    reviews = await feedback_service.list_public_reviews(db_session, org.id)
    assert len(reviews) == 1
    review = reviews[0]
    assert review["comment"] == "Poor"
    assert review["verified_token"] is True
    # No PII leak: anonymous / display-safe author only, no raw user names
    # of hidden items, no token ids.
    assert "Great" not in str(reviews)
    assert review["display_author"] == "Jane Doe"
    assert "token" not in review

    summary = await feedback_service.public_summary(db_session, org.id)
    assert summary["count"] == 1 and summary["average"] == 1

    # Anonymous published review shows the badge, never the name.
    await feedback_service.moderate(db_session, fb1.id, 1, action="publish",
                                    reason=None)
    reviews = await feedback_service.list_public_reviews(db_session, org.id)
    anon_visibility = [r for r in reviews if r["comment"] == "Great service"][0]
    assert anon_visibility["display_author"] == "Verified customer"


async def test_org_response_and_report_stay_in_tenant(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    fb = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 3, comment="mixed"
    )

    # Another organization cannot respond to or report this feedback.
    other_org = await make_org(db_session)
    with pytest.raises(LookupError):
        await feedback_service.org_respond(db_session, fb.id, other_org.id, "hi")
    with pytest.raises(LookupError):
        await feedback_service.org_report(db_session, fb.id, other_org.id, "no")

    await feedback_service.org_respond(db_session, fb.id, org.id, "We apologize")
    reported = await feedback_service.org_report(db_session, fb.id, org.id, "abuse")
    assert reported.moderation_status == ModerationStatus.REPORTED

    # Double-report rejected.
    with pytest.raises(ValueError, match="already"):
        await feedback_service.org_report(db_session, fb.id, org.id, "again")


async def test_org_cannot_edit_or_delete_feedback(db_session: AsyncSession):
    """FDB-004: the service surface has no org mutation on feedback content —
    content is set once by submit_feedback (user-owned) and only admins can
    change moderation state. Verify by (a) mutating helpers exist only for
    moderation, and (b) the content stays untouched after org actions."""
    org, user, counter, token = await _setup(db_session)
    fb = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 1, comment="negative stays"
    )
    await feedback_service.org_respond(db_session, fb.id, org.id, "sorry")
    await db_session.refresh(fb)
    assert fb.rating_overall == 1
    assert fb.comment == "negative stays"

    # Org-triggered flow can only reach org_respond / org_report; both are
    # restricted to the owning org (proven above). No delete entry point:
    # the service exposes no delete function at all.
    public_api = [n for n in dir(feedback_service) if not n.startswith("_")]
    assert not any("delete" in n for n in public_api)


async def test_moderation_state_machine_and_audit(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    fb = await feedback_service.submit_feedback(
        db_session, token.id, user.id, 4, comment="ok"
    )

    # PENDING → hidden directly with reason (audited).
    await feedback_service.moderate(db_session, fb.id, 7, action="hide",
                                    reason="policy")
    assert fb.moderation_status == ModerationStatus.HIDDEN
    assert fb.moderation_reason == "policy"
    assert fb.moderated_by == 7

    # HIDDEN → publish allowed (restored).
    await feedback_service.moderate(db_session, fb.id, 7, action="publish",
                                    reason=None)
    assert fb.moderation_status == ModerationStatus.PUBLISHED

    # PUBLISHED → removed rejected? MODERATION_ACTIONS: remove from
    # HIDDEN/REPORTED/PENDING only — prevents silent deletion of live
    # published content without prior hide/report.
    with pytest.raises(ValueError, match="state"):
        await feedback_service.moderate(db_session, fb.id, 7, action="remove",
                                        reason=None)

    # Unknown action.
    with pytest.raises(ValueError, match="Unknown"):
        await feedback_service.moderate(db_session, fb.id, 7, action="explode",
                                        reason=None)


async def test_rating_bounds_validated(db_session: AsyncSession):
    org, user, counter, token = await _setup(db_session)
    with pytest.raises(ValueError, match="overall"):
        await feedback_service.submit_feedback(db_session, token.id, user.id, 6)
    with pytest.raises(ValueError, match="staff"):
        await feedback_service.submit_feedback(
            db_session, token.id, user.id, 4, rating_staff=0
        )
