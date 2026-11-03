"""create_all metadata smoke for Phase 7 models (disposable SQLite db)."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base


async def go():
    path = os.path.join(tempfile.gettempdir(), "smoke_trust.db")
    eng = create_async_engine("sqlite+aiosqlite:///" + path)
    import models  # noqa: F401 — populate metadata
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    print("CREATE_ALL OK", len(Base.metadata.tables), "tables")


asyncio.run(go())
