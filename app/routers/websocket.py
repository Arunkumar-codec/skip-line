"""
app/routers/websocket.py
────────────────────────
WebSocket endpoints.

  /ws/org/{org_id}       → display screens and operator dashboards
  /ws/token/{ref}        → individual user tracking their token (signed ref)

Authorization (Decision 20 — DSP-001):
  /ws/org requires either a valid display-access-key (public TV screens,
  installed via ?key=...) or an authenticated organization session cookie
  (operator dashboards). This prevents cross-tenant data discovery by
  connecting to another organization's id.
  /ws/token requires a HMAC-signed tracking reference — the internal token
  id is never accepted in plaintext (Release Gate 6).
"""

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils import signed_qr
from app.websocket.manager import ws_manager
from app.utils.jwt import decode_token

router = APIRouter()


async def _authorize_org_room(db: AsyncSession, org_id: int, websocket: WebSocket) -> bool:
    """Display key OR organization session must match this exact org."""
    key = websocket.query_params.get("key")
    if key:
        from app.services import display_access_service

        if await display_access_service.is_key_installed(db, org_id, key):
            return True
        return False

    from app.utils.jwt import decode_token

    auth_header = websocket.headers.get("authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = websocket.cookies.get("org_token")
    if not token:
        return False
    payload = decode_token(token)
    return bool(payload) and payload.get("role") == "organization" \
        and str(payload.get("sub")) == str(org_id)


@router.websocket("/org/{org_id}")
async def org_display_ws(
    org_id: int,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    WebSocket for the public display screen and org operator dashboard.
    Clients receive `display_update` events whenever a token action occurs.
    """
    if not await _authorize_org_room(db, org_id, websocket):
        # Accept-then-close pattern: the WebSocket handshake cannot be
        # rejected with an HTTP status before accept; 4001 = policy code.
        await websocket.accept()
        await websocket.close(code=4001, reason="display authorization required")
        logger.warning("Unauthorized WS attempt for org room {}", org_id)
        return

    await ws_manager.connect_org(org_id, websocket)
    try:
        while True:
            # Keep connection alive; client may send ping
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("Org WS disconnected org={}", org_id)
    finally:
        await ws_manager.disconnect_org(org_id, websocket)


@router.websocket("/token/{ref}")
async def token_tracking_ws(
    ref: str,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    Live tracking for one token room. The path carries a signed opaque
    reference; the internal token id is resolved server-side (Gate 6).
    """
    status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_TRACK)
    if status != signed_qr.STATUS_VALID:
        await websocket.accept()
        await websocket.close(code=4403, reason="invalid token reference")
        logger.warning("Token WS rejected: ref status={}", status)
        return

    token_id = payload.token_id

    # The token may have been deleted or moved tenant; resolve so we never
    # join another organization's room by id.
    from models.token import Token

    token = await db.get(Token, token_id)
    if not token or token.organization_id != payload.org_id:
        await websocket.accept()
        await websocket.close(code=4404, reason="token not found")
        return

    await ws_manager.connect_token(token_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("Token WS disconnected token={}", token_id)
    finally:
        await ws_manager.disconnect_token(token_id, websocket)
