"""
models/employee.py
──────────────────
Individual employee/staff account belonging to an organization and assigned to a branch.
Supports dedicated authentication, branch-scoped operations, and RBAC preparation.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("organization_id", "employee_code", name="uq_org_employee_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    employee_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="employees")
    branch: Mapped["Branch"] = relationship("Branch", back_populates="employees")
    role_assignments: Mapped[list["EmployeeRoleAssignment"]] = relationship(
        "EmployeeRoleAssignment", back_populates="employee", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Employee id={self.id} org_id={self.organization_id} "
            f"branch_id={self.branch_id} code={self.employee_code!r}>"
        )
