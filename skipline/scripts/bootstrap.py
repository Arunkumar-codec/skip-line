"""
scripts/bootstrap.py
─────────────────────
Creates all database tables and seeds the default admin account.
Run once on fresh setup:  python scripts/bootstrap.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from app.database import engine, Base
    import models  # registers all ORM models with Base.metadata  # noqa: F401

    print("Creating tables…")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables created")

    from app.services.admin_service import seed_default_admin
    await seed_default_admin()
    print("✓ Default admin seeded")
    print("\nSetup complete. Start the server with:")
    print("  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")


if __name__ == "__main__":
    asyncio.run(main())
