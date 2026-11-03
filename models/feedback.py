"""
models/feedback.py
─────────────────
Phase 7 — Verified Token Feedback & Ratings (FDB-001..005).

By construction every feedback row is token-verified: `token_id` is NOT
NULL and unique (one row per eligible token — a resubmission after
moderation REMOved re-activates the same row instead of inserting a new
one), so the "verified-token" badge shown publicly is factual, not
decorative. Eligibility is enforced by `feedback_service`.
"""

import enum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class ModerationStatus(str, enum.Enum):
    PENDING = "pending"          # awaiting moderation; not shown publicly
    PUBLISHED = "published"      # visible to the public
    HIDDEN = "hidden"            # removed from public view by moderation
    REPORTED = "reported"        # organization reported the feedback
    REMOVED = "removed"          # fully removed; token slot is re-ratable


class Feedback(Base, TimestampMixin):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"),
        nullable=False, index=True, unique=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="SET NULL"), nullable=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )

    # Ratings 1..5. overall is mandatory; category ratings are optional
    # (FDB-002 configurable categories: waiting time / staff behaviour /
    # service quality). Extra categories may be added later as columns.
    rating_overall: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_waiting: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_staff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_service: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Completion result as reported by the customer (optional).
    result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # FDB-002: anonymous feedback shows "Verified customer" publicly and
    # never the user's name — the DB link is kept for auditability.
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Optional single attachment (FDB-002), stored like identity docs:
    # unguessable filename under the uploads dir; served through an
    # authorized endpoint only.
    attachment_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attachment_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # FDB-005 moderation state machine with reason + auditor.
    moderation_status: Mapped[str] = mapped_column(
        Enum(ModerationStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=ModerationStatus.PENDING, index=True,
    )
    moderation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    moderated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Relationships (one-sided; other models are not modified) ──
    responses: Mapped[list["FeedbackResponse"]] = relationship(
        "FeedbackResponse", back_populates="feedback", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} token={self.token_id} {self.moderation_status}>"


class FeedbackResponse(Base, TimestampMixin):
    __tablename__ = "feedback_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feedback_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("feedback.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # kind: 'response' (public reply) or 'report' (raise for moderation).
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    feedback: Mapped["Feedback"] = relationship("Feedback", back_populates="responses")

    def __repr__(self) -> str:
        return f"<FeedbackResponse id={self.id} kind={self.kind} feedback={self.feedback_id}>"
