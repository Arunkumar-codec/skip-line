"""
tests/conftest.py
─────────────────
Shared pytest fixtures.
Uses an in-memory SQLite database for fast, isolated tests.
"""

import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

# Test-only dummies (NOT real credentials), applied before app.config resolves
# its Settings singleton so test environments without a developer .env behave
# like a configured local setup. setdefault keeps precedence for real values.
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-bootstrap-password!")
os.environ.setdefault("DB_PASSWORD", "test-db-password-dummy")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-0123456789abcdef")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-secret-not-for-production-0123456789abcdef")

from app.config import settings
from app.database import Base, get_db
from app.main import app

# Ensure testing environment mode
settings.app_env = "testing"

# ── In-memory test database ───────────────────────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create all tables once per test session."""
    import models  # noqa: F401
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    """Provide a transactional test session that rolls back after each test."""
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(autouse=True)
async def mock_smtp_and_cleanup_tasks(monkeypatch):
    """Prevent external SMTP network calls and cleanly drain background tasks."""
    from unittest.mock import AsyncMock
    mock_send = AsyncMock(return_value=(None, "250 OK"))
    monkeypatch.setattr("aiosmtplib.send", mock_send)
    yield
    from app.services.notification_service import wait_for_background_tasks
    await wait_for_background_tasks(timeout=1.0)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """Async test client with database dependency overridden."""
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
