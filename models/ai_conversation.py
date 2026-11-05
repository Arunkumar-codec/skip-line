"""
models/ai_conversation.py
────────────────────────
AI booking assistant conversations (Phase 9 Workstreams C & D, MASTER §7/§14/§15).

One row per user conversation with the AI booking assistant. The row is the
DURABLE STATE that makes manual↔AI continuity possible (D1–D7):

- mode:        which surface the conversation is currently driven from
               ("manual" form flow or "ai" chat). Switching modes never
               destroys the collected context (D1/D2).
- status:      lifecycle guard. "booked" conversations can never create a
               second booking regardless of mode switches or further chat
               (D3/duplicate prevention). "abandoned" conversations are
               terminal no-ops.
- context_json: bounded, validated conversation context (collected booking
               fields, history summaries) — the thing that survives switches.
- draft_json:  an UNCONFIRMED booking draft (service/counter/slot chosen but
               not yet confirmed by the user) — retained, never auto-booked,
               and always revalidated against live availability / price
               before any final booking (D5).
- last_booking_id: the booking created through this conversation (set at
               booking time together with status="booked").

The LLM never writes authoritative data directly: bookings flow through
token_service.book_token (existing idempotency/payment/capacity semantics).
This table only stores conversation state.
"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from models.mixins import TimestampMixin


class AiConversation(Base, TimestampMixin):
    """A user's AI booking conversation (mode-aware, continuity-preserving)."""

    __tablename__ = "ai_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # manual | ai — the CURRENT mode (D1/D2); switching keeps context.
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="ai")
    # active | booked | abandoned
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    # Bounded conversation context (collected booking fields + history refs).
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Unconfirmed booking draft (D4): retained until confirmed or superseded.
    draft_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
