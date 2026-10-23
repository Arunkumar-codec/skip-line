"""
app/services/rbac_service.py
────────────────────────────
Role-Based Access Control (RBAC) engine.
Manages permissions, system & custom roles, and employee role assignments.
"""

from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.employee import Employee
from models.employee_role_assignment import EmployeeRoleAssignment
from models.permission import Permission
from models.role import Role
from models.role_permission import RolePermission

# Standard system permission catalog
DEFAULT_PERMISSIONS = [
    # Branches
    {"code": "branches:read", "name": "View Branches", "module": "branches", "description": "View branch details and listings"},
    {"code": "branches:write", "name": "Manage Branches", "module": "branches", "description": "Create, update, and delete branches"},
    # Employees
    {"code": "employees:read", "name": "View Staff", "module": "employees", "description": "View staff profiles and listings"},
    {"code": "employees:write", "name": "Manage Staff", "module": "employees", "description": "Create, update, and deactivate staff accounts"},
    # Roles
    {"code": "roles:read", "name": "View Roles", "module": "roles", "description": "View roles and permissions"},
    {"code": "roles:assign", "name": "Assign Roles", "module": "roles", "description": "Assign or revoke roles from staff"},
    # Queue Operations
    {"code": "queue:read", "name": "View Queue", "module": "queue", "description": "View active tickets and queue status"},
    {"code": "queue:call", "name": "Call Tokens", "module": "queue", "description": "Call next tokens and operate counters"},
    {"code": "queue:operate", "name": "Operate Tokens", "module": "queue", "description": "Complete, recall, or no-show tokens"},
    {"code": "queue:transfer", "name": "Transfer Tokens", "module": "queue", "description": "Transfer tokens between services or counters"},
    # Services & Counters
    {"code": "services:manage", "name": "Manage Services", "module": "services", "description": "Configure services and forms"},
    {"code": "counters:manage", "name": "Manage Counters", "module": "counters", "description": "Configure physical counters and displays"},
    # Reports & Audit
    {"code": "reports:read", "name": "View Reports", "module": "reports", "description": "View analytics and daily performance reports"},
    {"code": "audit:read", "name": "View Audit Logs", "module": "audit", "description": "Inspect tenant security and administrative logs"},
    # Organization Admin
    {"code": "org:admin", "name": "Organization Head Administration", "module": "org", "description": "Full administrative control of the organization"},
]

# Standard default roles and their permission mappings
DEFAULT_ROLES = [
    {
        "code": "org_admin",
        "name": "Organization Administrator",
        "description": "Full access to all organization features and staff management",
        "permissions": [p["code"] for p in DEFAULT_PERMISSIONS],
    },
    {
        "code": "branch_manager",
        "name": "Branch Manager",
        "description": "Manages branch operations, staff, counters, and queues",
        "permissions": [
            "branches:read", "branches:write", "employees:read", "employees:write",
            "roles:read", "roles:assign", "queue:read", "queue:call", "queue:operate",
            "queue:transfer", "services:manage", "counters:manage", "reports:read",
        ],
    },
    {
        "code": "queue_operator",
        "name": "Counter / Queue Operator",
        "description": "Calls and processes customer tokens at assigned counters",
        "permissions": [
            "queue:read", "queue:call", "queue:operate", "queue:transfer",
        ],
    },
    {
        "code": "registration_staff",
        "name": "Front Desk / Registration Staff",
        "description": "Assists walk-in visitors and issues queue tokens",
        "permissions": [
            "queue:read", "queue:operate", "services:manage",
        ],
    },
    {
        "code": "org_auditor",
        "name": "Organization Auditor",
        "description": "Read-only access to analytics, reports, and audit logs",
        "permissions": [
            "reports:read", "audit:read", "queue:read", "branches:read", "employees:read",
        ],
    },
]


async def seed_rbac_defaults(db: Optional[AsyncSession] = None) -> None:
    """Idempotently seed standard permissions and system role templates."""
    if db is None:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await seed_rbac_defaults(session)
        return

    # 1. Seed Permissions
    for perm_data in DEFAULT_PERMISSIONS:
        existing = (
            await db.execute(select(Permission).where(Permission.code == perm_data["code"]))
        ).scalar_one_or_none()
        if not existing:
            perm = Permission(**perm_data)
            db.add(perm)
    await db.commit()

    # 2. Seed Default Roles
    for role_def in DEFAULT_ROLES:
        existing_role = (
            await db.execute(
                select(Role).where(
                    and_(Role.code == role_def["code"], Role.is_system == True)  # noqa: E712
                )
            )
        ).scalar_one_or_none()

        if not existing_role:
            role = Role(
                organization_id=None,
                code=role_def["code"],
                name=role_def["name"],
                description=role_def["description"],
                is_system=True,
            )
            db.add(role)
            await db.flush()

            # Attach permissions
            for pcode in role_def["permissions"]:
                perm = (
                    await db.execute(select(Permission).where(Permission.code == pcode))
                ).scalar_one_or_none()
                if perm:
                    db.add(RolePermission(role_id=role.id, permission_id=perm.id))

    await db.commit()


async def list_permissions(db: AsyncSession) -> list[Permission]:
    """Retrieve all available permissions in the system."""
    result = await db.execute(select(Permission).order_by(Permission.module, Permission.name))
    return list(result.scalars().all())


async def list_roles(db: AsyncSession, org_id: Optional[int] = None) -> list[Role]:
    """Retrieve all system roles plus custom roles belonging to the organization."""
    query = select(Role).options(selectinload(Role.permissions))
    if org_id is not None:
        query = query.where(or_(Role.is_system == True, Role.organization_id == org_id))  # noqa: E712
    else:
        query = query.where(Role.is_system == True)  # noqa: E712
    query = query.order_by(Role.is_system.desc(), Role.name)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_role(db: AsyncSession, role_id: int, org_id: Optional[int] = None) -> Role:
    """Retrieve a role ensuring tenant accessibility."""
    role = await db.get(Role, role_id)
    if not role:
        raise ValueError("Role not found")
    if not role.is_system and role.organization_id != org_id:
        raise ValueError("Role not found or belongs to another organization")
    return role


async def assign_role_to_employee(
    db: AsyncSession,
    org_id: int,
    employee_id: int,
    role_id: int,
    assigned_by_id: Optional[int] = None,
) -> EmployeeRoleAssignment:
    """Assign a role to an employee with strict tenant boundary checks."""
    # Verify employee belongs to this org
    employee = await db.get(Employee, employee_id)
    if not employee or employee.organization_id != org_id:
        raise ValueError("Employee not found or belongs to another organization")

    # Verify role exists and is either system or belongs to this org
    role = await get_role(db, role_id, org_id)

    # Check if already assigned
    existing = (
        await db.execute(
            select(EmployeeRoleAssignment).where(
                and_(
                    EmployeeRoleAssignment.employee_id == employee_id,
                    EmployeeRoleAssignment.role_id == role_id,
                )
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    assignment = EmployeeRoleAssignment(
        employee_id=employee_id,
        role_id=role.id,
        assigned_by_id=assigned_by_id,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return assignment


async def remove_role_from_employee(
    db: AsyncSession, org_id: int, employee_id: int, role_id: int
) -> None:
    """Remove a role assignment from an employee with tenant boundary enforcement."""
    employee = await db.get(Employee, employee_id)
    if not employee or employee.organization_id != org_id:
        raise ValueError("Employee not found or belongs to another organization")

    assignment = (
        await db.execute(
            select(EmployeeRoleAssignment).where(
                and_(
                    EmployeeRoleAssignment.employee_id == employee_id,
                    EmployeeRoleAssignment.role_id == role_id,
                )
            )
        )
    ).scalar_one_or_none()

    if not assignment:
        raise ValueError("Role assignment not found")

    await db.delete(assignment)
    await db.commit()


async def get_employee_roles(
    db: AsyncSession, org_id: int, employee_id: int
) -> list[Role]:
    """List all roles currently assigned to an employee."""
    employee = await db.get(Employee, employee_id)
    if not employee or employee.organization_id != org_id:
        raise ValueError("Employee not found or belongs to another organization")

    query = (
        select(Role)
        .join(EmployeeRoleAssignment, EmployeeRoleAssignment.role_id == Role.id)
        .where(EmployeeRoleAssignment.employee_id == employee_id)
        .options(selectinload(Role.permissions))
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_employee_permissions(db: AsyncSession, employee_id: int) -> set[str]:
    """Resolve the union of all permission codes granted to an employee across all assigned roles."""
    query = (
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(
            EmployeeRoleAssignment,
            EmployeeRoleAssignment.role_id == RolePermission.role_id,
        )
        .where(EmployeeRoleAssignment.employee_id == employee_id)
    )
    result = await db.execute(query)
    return set(result.scalars().all())
