"""
models/admin.py
───────────────
Admin accounts.  Only a small number of admins exist; they are created
by seeding or direct database insert — there is no public registration.
"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    approved_organizations: Mapped[list["Organization"]] = relationship(
        "Organization", back_populates="approved_by", foreign_keys="Organization.approved_by_admin_id"
    )

    def __repr__(self) -> str:
        return f"<Admin id={self.id} email={self.email!r}>"
