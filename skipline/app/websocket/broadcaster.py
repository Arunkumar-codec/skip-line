"""
app/websocket/broadcaster.py
─────────────────────────────
High-level broadcast helpers.
Called by token_service after every state-changing action so that
display screens and individual tracking pages receive instant updates.
"""

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.websocket.manager import ws_manager


async def broadcast_queue_update(db: AsyncSession, org_id: int, token_id: int) -> None:
    """
    Fire both update types after a token action:
      1. display_update  → org room  (display screens + operator tabs)
      2. tracking_update → token room (user tracking their specific token)
    Errors are caught and logged — never propagate to the caller.
    """
    try:
        from app.services.token_service import get_display_data, get_token_tracking

        display  = await get_display_data(db, org_id)
        tracking = await get_token_tracking(db, token_id)

        await ws_manager.broadcast_to_org(
            org_id,
            {"type": "display_update", "data": display},
        )
        await ws_manager.broadcast_to_token(
            token_id,
            {"type": "tracking_update", "data": tracking},
        )
    except Exception as exc:
        logger.warning("broadcast_queue_update failed org={} token={}: {}", org_id, token_id, exc)


async def broadcast_announcement(org_id: int, text: str) -> None:
    """Push a new announcement to the display screen."""
    try:
        await ws_manager.broadcast_to_org(
            org_id,
            {"type": "announcement", "data": {"announcement": text}},
        )
    except Exception as exc:
        logger.warning("broadcast_announcement failed org={}: {}", org_id, exc)


# ── Extra helpers kept for backward compatibility ─────────────────────────────

async def broadcast_token_update(db: AsyncSession, token_id: int, org_id: int) -> None:
    """Alias for broadcast_queue_update — used in some routers as broadcast_token_update."""
    await broadcast_queue_update(db, org_id, token_id)
