"""
models/__init__.py
──────────────────
Import all ORM models here so that:
  - SQLAlchemy metadata is fully populated before create_all() is called
  - Alembic autogenerate detects every table
  - Circular import issues are avoided (models import Base, not each other)
"""

from models.admin import Admin                              # noqa: F401
from models.organization import Organization               # noqa: F401
from models.organization_verification import OrganizationVerification  # noqa: F401
from models.branch import Branch                           # noqa: F401
from models.employee import Employee                       # noqa: F401
from models.user import User                               # noqa: F401
from models.service import Service                         # noqa: F401
from models.counter import Counter                         # noqa: F401
from models.token import Token                             # noqa: F401
from models.token_history import TokenHistory              # noqa: F401
from models.payment import Payment                         # noqa: F401
from models.notification import Notification               # noqa: F401
from models.queue_log import QueueLog                      # noqa: F401
from models.setting import Setting                         # noqa: F401
from models.permission import Permission                   # noqa: F401
from models.role import Role                               # noqa: F401
from models.role_permission import RolePermission           # noqa: F401
from models.employee_role_assignment import EmployeeRoleAssignment  # noqa: F401
from models.session import Session                         # noqa: F401
from models.audit_log import AuditLog                      # noqa: F401
from models.counter_service import CounterService          # noqa: F401
from models.sub_service import SubService                  # noqa: F401
from models.token_service_item import TokenServiceItem     # noqa: F401
from models.custom_form import (                           # noqa: F401
    CustomForm,
    FormField,
    FormFieldOption,
    FormFieldCondition,
    TokenFormSnapshot,
    TokenFormAnswer,
)
from models.user_identity import UserIdentity              # noqa: F401
from models.weekly_schedule import WeeklySchedule          # noqa: F401
from models.schedule_exception import ScheduleException    # noqa: F401
from models.time_slot import TimeSlot                      # noqa: F401
from models.capacity_rule import CapacityRule              # noqa: F401
from models.schedule_change import ScheduleChangePreview    # noqa: F401
from models.counter_shift import CounterShift               # noqa: F401
from models.kiosk_device import KioskDevice                 # noqa: F401
from models.finance import (                                # noqa: F401
    LedgerEntry,
    CashSession,
    Refund,
    Payout,
    Settlement,
    OrgBankAccount,
    FinancialRiskFlag,
    WebhookEvent,
)

__all__ = [
    "Admin",
    "Organization",
    "OrganizationVerification",
    "Branch",
    "Employee",
    "User",
    "Service",
    "Counter",
    "Token",
    "TokenHistory",
    "Payment",
    "Notification",
    "QueueLog",
    "Setting",
    "Permission",
    "Role",
    "RolePermission",
    "EmployeeRoleAssignment",
    "Session",
    "AuditLog",
    "CounterService",
    "SubService",
    "TokenServiceItem",
    "CustomForm",
    "FormField",
    "FormFieldOption",
    "FormFieldCondition",
    "TokenFormSnapshot",
    "TokenFormAnswer",
    "UserIdentity",
    "WeeklySchedule",
    "ScheduleException",
    "TimeSlot",
    "CapacityRule",
    "ScheduleChangePreview",
    "CounterShift",
    "KioskDevice",
    "LedgerEntry",
    "CashSession",
    "Refund",
    "Payout",
    "Settlement",
    "OrgBankAccount",
    "FinancialRiskFlag",
    "WebhookEvent",
]
