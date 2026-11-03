"""
tests/phase7_helpers.py
──────────────────────
Shared model-row factories for Phase 7 (feedback / support / KYC) tests.
Rows are created directly on the transactional in-memory session, mirroring
the project's fixture style (no HTTP round-trips).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from models.branch import Branch
from models.counter import Counter
from models.organization import Organization, OrgStatus
from models.token import Token, TokenStatus
from models.user import User


def unique_suffix() -> str:
    return uuid.uuid4().hex[:10]


async def make_org(db: AsyncSession, *, status: str = OrgStatus.APPROVED) -> Organization:
    uid = unique_suffix()
    org = Organization(
        name=f"Trust Org {uid}",
        registration_number=f"TO-{uid}",
        org_type="hospital",
        address="1 Trust Street",
        city="Metro",
        state="ST",
        email=f"trust_{uid}@example.com",
        mobile="9800000000",
        password_hash="test-hash-not-real",
        status=status,
        is_active=True,
    )
    db.add(org)
    await db.flush()
    return org


async def make_user(db: AsyncSession, name: str = "Jane Doe") -> User:
    uid = unique_suffix()
    user = User(
        name=name,
        email=f"user_{uid}@example.com",
        mobile=f"9{uuid.uuid4().int % 10**9:09d}",
        password_hash="test-hash-not-real",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def make_counter(db: AsyncSession, org: Organization, name: str = "C1") -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=name,
        counter_number=int(datetime.now().timestamp() * 1000) % 100000,
        token_prefix=name[0],
    )
    db.add(counter)
    await db.flush()
    return counter


async def make_token(
    db: AsyncSession,
    org: Organization,
    counter: Counter,
    *,
    user: User | None = None,
    status: str = TokenStatus.COMPLETED,
) -> Token:
    token = Token(
        organization_id=org.id,
        user_id=user.id if user else None,
        counter_id=counter.id,
        token_number=f"X-{uuid.uuid4().hex[:6].upper()}",
        sequence=int(datetime.now().timestamp() * 1000) % 100000,
        status=status,
        queue_position=0,
        booked_at=datetime.now(timezone.utc),
    )
    db.add(token)
    await db.flush()
    return token


async def make_branch(db: AsyncSession, org: Organization, code: str = "MAIN") -> Branch:
    uid = unique_suffix()
    branch = Branch(
        organization_id=org.id,
        name=f"Branch {uid}",
        branch_code=f"{code}-{uid}",
        address=f"{uid} Branch Road",
        city="Metro",
        state="ST",
        pincode="560001",
        timezone="Asia/Kolkata",
    )
    db.add(branch)
    await db.flush()
    return branch
