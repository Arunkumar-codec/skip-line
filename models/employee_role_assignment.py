"""
models/employee_role_assignment.py
──────────────────────────────────
Assigns one or more roles to an employee.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmployeeRoleAssignment(Base):
    __tablename__ = "employee_role_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "role_id", name="uq_emp_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee", back_populates="role_assignments")
    role: Mapped["Role"] = relationship("Role", back_populates="employee_assignments")

    def __repr__(self) -> str:
        return f"<EmployeeRoleAssignment emp={self.employee_id} role={self.role_id}>"
