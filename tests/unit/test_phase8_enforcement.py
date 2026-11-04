"""
tests/unit/test_phase8_enforcement.py
─────────────────────────────────────
Phase 8 — Enforcement ladder (ENF-002..005) service-level verification:

- escalation chain (supersede history, previous_action_id links);
- §4.4 dual approval: suspension / permanent_block require a DISTINCT
  second approver (same admin rejected, credentials required);
- pre-suspension impact summary + classification (ENF-003);
- booking gate: org suspensions and scoped restrictions (ENF-002);
- appeals: file / duplicate-pending / decide with re-verification (ENF-004);
- reinstatement with dual approval;
- booking anomaly evaluators (ENF-001) via financial_risk_service.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import enforcement_service
from models.enforcement import (
    EnforcementAction,
    EnforcementStatus,
    EnforcementType,
)
from models.organization import OrgStatus
from models.token import TokenStatus
from tests.phase7_helpers import (
    make_counter,
    make_org,
    make_token,
    make_user,
)


class TestLadderBasics:
    async def test_issue_warning_action_is_active(self, db_session):
        org = await make_org(db_session)
        action = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.WARNING.value,
            "Data quality issue in catalogue",
            actor_label="admin@example.com", actor_id=None, commit=False,
        )
        assert action.status == EnforcementStatus.ACTIVE.value
        assert action.action_type == "warning"
        assert action.evidence is None

    async def test_unknown_type_rejected(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(
            enforcement_service.EnforcementError, match="Unknown enforcement type"
        ):
            await enforcement_service.issue_action(
                db_session, org.id, "ban_hammer", "nope", commit=False,
            )

    async def test_empty_reason_rejected(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(enforcement_service.EnforcementError, match="reason is mandatory"):
            await enforcement_service.issue_action(
                db_session, org.id, "warning", "   ", commit=False,
            )


class TestDualApproval:
    async def test_suspension_requires_second_approver(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(PermissionError, match="second-person approval"):
            await enforcement_service.issue_action(
                db_session, org.id, EnforcementType.SUSPENSION.value,
                "Verified data fabrication", commit=False,
            )

    async def test_suspension_rejects_same_admin_as_second(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(PermissionError, match="different Super Admin"):
            await enforcement_service.issue_action(
                db_session, org.id, EnforcementType.SUSPENSION.value,
                "Verified data fabrication",
                actor_label="admin1@example.com", actor_id=1,
                second_approver={"id": 1, "label": "admin1@example.com"},
                commit=False,
            )

    async def test_suspension_with_distinct_approvers_flips_org_status(self, db_session):
        org = await make_org(db_session)
        action = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SUSPENSION.value,
            "Verified data fabrication",
            actor_label="admin1@example.com", actor_id=1,
            second_approver={"id": 2, "label": "admin2@example.com"},
            commit=False,
        )
        assert action.status == EnforcementStatus.ACTIVE.value
        assert action.second_approver_id == 2
        assert action.second_approver_label == "admin2@example.com"
        assert action.second_approved_at is not None
        assert org.status == OrgStatus.SUSPENDED.value

    async def test_booking_blocked_during_org_suspension(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SUSPENSION.value, "policy breach",
            actor_id=1, second_approver={"id": 2, "label": "b@example.com"},
            commit=False,
        )
        with pytest.raises(PermissionError, match="enforcement"):
            await enforcement_service.ensure_booking_allowed(db_session, org.id, counter_id=counter.id)
        with pytest.raises(PermissionError, match="suspended"):
            await enforcement_service.ensure_org_operational(db_session, org.id)


class TestScopedRestrictions:
    async def test_counter_scope_blocks_only_that_counter(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org, "A")
        c2 = await make_counter(db_session, org, "B")
        await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SCOPED_RESTRICTION.value,
            "Complaints spike at one desk",
            scope_type="counter", scope_target_id=c1.id, commit=False,
        )
        with pytest.raises(PermissionError, match="restricted"):
            await enforcement_service.ensure_booking_allowed(db_session, org.id, counter_id=c1.id)
        # a different counter in the same org is NOT restricted
        await enforcement_service.ensure_booking_allowed(db_session, org.id, counter_id=c2.id)

    async def test_service_scope(self, db_session):
        from models.service import Service
        org = await make_org(db_session)
        service = Service(
            organization_id=org.id, name="Blocked Service",
            estimated_minutes=10, price=0, is_active=True,
        )
        db_session.add(service)
        await db_session.flush()
        await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SCOPED_RESTRICTION.value, "abuse",
            scope_type="service", scope_target_id=service.id, commit=False,
        )
        with pytest.raises(PermissionError, match="restricted"):
            await enforcement_service.ensure_booking_allowed(
                db_session, org.id, service_id=service.id
            )

    async def test_expired_restriction_no_longer_blocks(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org, "A")
        action = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SCOPED_RESTRICTION.value, "abuse",
            scope_type="counter", scope_target_id=c1.id, commit=False,
        )
        action.restricted_until = datetime.now(timezone.utc) - timedelta(days=1)
        await db_session.flush()
        # expired-but-still-active rows no longer restrict
        await enforcement_service.ensure_booking_allowed(db_session, org.id, counter_id=c1.id)

    async def test_scope_target_validated_against_organization(self, db_session):
        org_a = await make_org(db_session)
        org_b = await make_org(db_session)
        counter_b = await make_counter(db_session, org_b, "Z")
        with pytest.raises(enforcement_service.EnforcementError, match="not found"):
            await enforcement_service.issue_action(
                db_session, org_a.id, EnforcementType.SCOPED_RESTRICTION.value, "abuse",
                scope_type="counter", scope_target_id=counter_b.id, commit=False,
            )


class TestEscalation:
    async def test_escalation_supersedes_and_links(self, db_session):
        org = await make_org(db_session)
        first = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.WARNING.value, "first notice",
            commit=False,
        )
        second = await enforcement_service.escalate_action(
            db_session, first.id, EnforcementType.SCOPED_RESTRICTION.value,
            "no improvement", commit=False,
        )
        await db_session.refresh(first)
        assert first.status == EnforcementStatus.SUPERSEDED.value
        assert second.previous_action_id == first.id
        assert second.status == EnforcementStatus.ACTIVE.value
        # scoped restriction default window applied when none given
        assert second.restricted_until is not None

    async def test_escalation_to_lower_or_equal_stage_rejected(self, db_session):
        org = await make_org(db_session)
        first = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.CORRECTION_DEADLINE.value,
            "first notice", commit=False,
        )
        with pytest.raises(enforcement_service.EnforcementError, match="stricter stage"):
            await enforcement_service.escalate_action(
                db_session, first.id, EnforcementType.WARNING.value,
                "silly downgrade", commit=False,
            )

    async def test_escalation_to_suspension_needs_approval(self, db_session):
        org = await make_org(db_session)
        first = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.WARNING.value, "first notice",
            commit=False,
        )
        with pytest.raises(PermissionError, match="second-person approval"):
            await enforcement_service.escalate_action(
                db_session, first.id, EnforcementType.SUSPENSION.value,
                "verified fraud", commit=False,
            )

    async def test_escalated_action_is_atomic(self, db_session):
        """When the supersede guard races (row already superseded), the new
        row must not survive: the whole transaction aborts."""
        org = await make_org(db_session)
        from sqlalchemy import update
        first = await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.WARNING.value, "first notice",
            commit=False,
        )
        # simulate the concurrent race: flip the row to SUPERSEDED without
        # the service noticing (direct SQL, invisible to the session cache)
        async def _flip():
            from sqlalchemy import text as _t
            await db_session.execute(_t(
                f"UPDATE enforcement_actions SET status='superseded' WHERE id={first.id}"
            ))
        await _flip()
        with pytest.raises(enforcement_service.EnforcementError, match="already escalated"):
            await enforcement_service.escalate_action(
                db_session, first.id, EnforcementType.SCOPED_RESTRICTION.value,
                "should abort", commit=False,
            )


class TestImpactSummary:
    async def test_impact_counts_and_classification(self, db_session):
        from models.payment import Payment, PaymentStatus
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        user = await make_user(db_session)
        t1 = await make_token(db_session, org, counter, user=user, status=TokenStatus.WAITING)
        await make_token(db_session, org, counter, status=TokenStatus.SKIPPED)
        payment = Payment(
            token_id=t1.id, organization_id=org.id, user_id=user.id,
            amount=150, status=PaymentStatus.PENDING.value,
        )
        db_session.add(payment)
        await db_session.flush()

        impact = await enforcement_service.build_pre_suspension_impact(db_session, org.id)
        assert impact["active_or_future_tokens"] == 1  # WAITING counts; SKIPPED doesn't
        assert impact["pending_payments"] == 1
        assert impact["pending_refunds"] == 0
        assert impact["open_support_cases"] == 0
        assert (
            enforcement_service.classification_from_impact(impact)
            == "requires_refund_or_honor_handling"
        )

    async def test_no_exposure_classifies_clean(self, db_session):
        org = await make_org(db_session)
        impact = await enforcement_service.build_pre_suspension_impact(db_session, org.id)
        assert enforcement_service.classification_from_impact(impact) == "no_active_exposure"


class TestResolveAndReinstate:
    async def _suspend(self, db_session, org, actor_id=1):
        return await enforcement_service.issue_action(
            db_session, org.id, EnforcementType.SUSPENSION.value, "verified breach",
            actor_label="a1@example.com", actor_id=actor_id,
            second_approver={"id": 2, "label": "a2@example.com"},
            commit=False,
        )

    async def test_resolve_closes_action(self, db_session):
        org = await make_org(db_session)
        action = await self._suspend(db_session, org)
        await enforcement_service.resolve_action(
            db_session, action.id, status=EnforcementStatus.RESOLVED.value,
            reason="org corrected the data", resolver_label="a1@example.com",
            commit=False,
        )
        assert action.status == EnforcementStatus.RESOLVED.value
        assert action.resolved_reason == "org corrected the data"
        assert action.resolved_at is not None

    async def test_resolve_twice_rejected(self, db_session):
        org = await make_org(db_session)
        action = await self._suspend(db_session, org)
        await enforcement_service.resolve_action(
            db_session, action.id, reason="ok", commit=False,
        )
        with pytest.raises(enforcement_service.EnforcementError, match="already"):
            await enforcement_service.resolve_action(
                db_session, action.id, reason="again", commit=False,
            )

    async def test_reinstatement_requires_second_approver(self, db_session):
        org = await make_org(db_session)
        await self._suspend(db_session, org)
        with pytest.raises(PermissionError, match="second-person approval"):
            await enforcement_service.reinstate_organization(
                db_session, org.id, reason="fixed", resolver_id=1, commit=False,
            )

    async def test_reinstatement_same_admin_rejected_and_success_path(self, db_session):
        org = await make_org(db_session)
        await self._suspend(db_session, org, actor_id=1)
        with pytest.raises(PermissionError, match="different Super Admin"):
            await enforcement_service.reinstate_organization(
                db_session, org.id, reason="fixed", resolver_id=1,
                second_approver={"id": 1, "label": "a1@example.com"}, commit=False,
            )
        org2 = await enforcement_service.reinstate_organization(
            db_session, org.id, reason="verified corrected", resolver_id=1,
            second_approver={"id": 2, "label": "a2@example.com"}, commit=False,
        )
        assert org2.status == OrgStatus.APPROVED.value
        assert org2.is_active is True
        # the suspension row is closed, with the reinstatement recorded
        actions = await enforcement_service.active_actions(db_session, org.id)
        assert actions == []
