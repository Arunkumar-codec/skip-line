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
from models.user import User                               # noqa: F401
from models.service import Service                         # noqa: F401
from models.counter import Counter                         # noqa: F401
from models.token import Token                             # noqa: F401
from models.token_history import TokenHistory              # noqa: F401
from models.payment import Payment                         # noqa: F401
from models.notification import Notification               # noqa: F401
from models.queue_log import QueueLog                      # noqa: F401
from models.setting import Setting                         # noqa: F401

__all__ = [
    "Admin",
    "Organization",
    "OrganizationVerification",
    "User",
    "Service",
    "Counter",
    "Token",
    "TokenHistory",
    "Payment",
    "Notification",
    "QueueLog",
    "Setting",
]
