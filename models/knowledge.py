"""
models/knowledge.py
──────────────────
Phase 7 — Suggested Solutions & Knowledge Base (KB-001..002).

Categorized FAQ / troubleshooting / refund / service-response templates.
`organization_id` NULL = platform-global template maintained by admins;
otherwise the template belongs to one organization. `sensitive=True`
marks financial/sensitive replies whose case-message delivery requires
explicit human review (KB-002).
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin

KB_CATEGORIES = {
    "faq", "troubleshooting", "refund", "service_response", "other",
}


class KnowledgeSolution(Base, TimestampMixin):
    __tablename__ = "knowledge_solutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 'user' | 'organization' | 'agent' — who the authored voice targets.
    audience: Mapped[str] = mapped_column(String(40), nullable=False, default="user")
    # Financial/sensitive content requires human review before sending.
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<KnowledgeSolution id={self.id} cat={self.category} org={self.organization_id}>"
