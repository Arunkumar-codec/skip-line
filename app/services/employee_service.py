"""
app/services/employee_service.py
────────────────────────────────
Business logic and tenant isolation for employee accounts.
Enforces strict organization and branch ownership boundaries.
"""

from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.security import hash_password, verify_password
from models.branch import Branch
from models.employee import Employee


async def list_employees(
    db: AsyncSession,
    org_id: int,
    branch_id: Optional[int] = None,
    active_only: bool = False,
) -> list[Employee]:
    """List all employees belonging strictly to the specified organization."""
    query = select(Employee).where(Employee.organization_id == org_id)
    if branch_id is not None:
        query = query.where(Employee.branch_id == branch_id)
    if active_only:
        query = query.where(Employee.is_active == True)  # noqa: E712
    query = query.order_by(Employee.name)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_employee(db: AsyncSession, employee_id: int, org_id: int) -> Employee:
    """Retrieve an employee, strictly verifying tenant ownership."""
    employee = await db.get(Employee, employee_id)
    if not employee or employee.organization_id != org_id:
        raise ValueError("Employee not found")
    return employee


async def create_employee(db: AsyncSession, org_id: int, data: dict) -> Employee:
    """Create a new employee with branch verification and tenant-scoped unique checks."""
    branch_id = data.get("branch_id")
    if not branch_id:
        raise ValueError("branch_id is required")

    branch = await db.get(Branch, branch_id)
    if not branch or branch.organization_id != org_id:
        raise ValueError("Branch not found or belongs to another organization")

    email = data.get("email", "").strip().lower()
    existing_email = (
        await db.execute(select(Employee).where(Employee.email == email))
    ).scalar_one_or_none()
    if existing_email:
        raise ValueError("Email already registered")

    code = data.get("employee_code", "").strip()
    existing_code = (
        await db.execute(
            select(Employee).where(
                and_(Employee.organization_id == org_id, Employee.employee_code == code)
            )
        )
    ).scalar_one_or_none()
    if existing_code:
        raise ValueError("Employee code already in use for this organization")

    password = data.get("password")
    if not password:
        raise ValueError("Password is required")

    employee = Employee(
        organization_id=org_id,
        branch_id=branch_id,
        name=data["name"].strip(),
        email=email,
        employee_code=code,
        password_hash=hash_password(password),
        is_active=data.get("is_active", True),
    )
    db.add(employee)
    await db.commit()
    await db.refresh(employee)
    return employee


async def update_employee(
    db: AsyncSession, employee_id: int, org_id: int, data: dict
) -> Employee:
    """Update employee details, strictly verifying tenant ownership and uniqueness."""
    employee = await get_employee(db, employee_id, org_id)

    new_branch_id = data.get("branch_id")
    if new_branch_id and new_branch_id != employee.branch_id:
        branch = await db.get(Branch, new_branch_id)
        if not branch or branch.organization_id != org_id:
            raise ValueError("Branch not found or belongs to another organization")
        employee.branch_id = new_branch_id

    new_code = data.get("employee_code")
    if new_code and new_code != employee.employee_code:
        conflict = (
            await db.execute(
                select(Employee).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.employee_code == new_code,
                        Employee.id != employee_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if conflict:
            raise ValueError("Employee code already in use for this organization")
        employee.employee_code = new_code

    new_email = data.get("email")
    if new_email and new_email.strip().lower() != employee.email.lower():
        conflict_email = (
            await db.execute(
                select(Employee).where(
                    and_(
                        Employee.email == new_email.strip().lower(),
                        Employee.id != employee_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if conflict_email:
            raise ValueError("Email already registered")
        employee.email = new_email.strip().lower()

    if "password" in data and data["password"]:
        employee.password_hash = hash_password(data["password"])

    if "name" in data and data["name"] is not None:
        employee.name = data["name"].strip()

    if "is_active" in data and data["is_active"] is not None:
        employee.is_active = data["is_active"]

    await db.commit()
    await db.refresh(employee)
    return employee


async def delete_employee(db: AsyncSession, employee_id: int, org_id: int) -> None:
    """Delete an employee, strictly verifying tenant ownership."""
    employee = await get_employee(db, employee_id, org_id)
    await db.delete(employee)
    await db.commit()


async def authenticate_employee(
    db: AsyncSession, email: str, password: str
) -> Optional[Employee]:
    """Authenticate an employee by email and password, ensuring account is active."""
    result = await db.execute(
        select(Employee).where(Employee.email == email.strip().lower())
    )
    employee = result.scalar_one_or_none()
    if not employee:
        return None
    if not verify_password(password, employee.password_hash):
        return None
    if not employee.is_active:
        return None
    return employee
