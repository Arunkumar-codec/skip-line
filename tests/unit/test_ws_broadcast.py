"""
tests/unit/test_ws_broadcast.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
QUE-005 â€” WebSocket broadcasts derive from committed database changes:
book_token and perform_token_action address both the org room and the token
tracking room through the event bus, containing only public-safe data.
Failing transitions broadcast nothing (no event before a successful commit).
"""

import json
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import org_service
from app.services import token_service
from app.websocket.bus import InMemoryBus
from app.websocket.manager import WebSocketManager, ws_manager
from models.counter import Counter, CounterStatus
from models.organization import Organization


async def _make_org(db_session: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"WS Org {uid}",
        "registration_number": f"WSO-{uid}",
        "org_type": "hospital",
        "email": f"ws_{uid}@hospital.com",
        "mobile": "9876543210",
        "password": "Password123!",
        "address": "1 Test Road",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _make_counter(db_session: AsyncSession, org: Organization) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name="Counter Q",
        counter_number=1,
        token_prefix="Q",
        daily_limit=50,
        status=CounterStatus.ACTIVE,
    )
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, str]] = []

    async def __call__(self, channel: str, raw) -> None:
        self.events.append((channel, raw if isinstance(raw, str) else raw.decode()))


@pytest.fixture
def recorder():
    """Install a recording handler on the manager's (in-memory) bus."""
    bus = ws_manager._bus
    if not isinstance(bus, InMemoryBus):
        # tests never configure redis; fall back to a fresh in-memory bus
        bus = InMemoryBus()
        ws_manager.set_broadcast_bus(bus)
    rec = _Recorder()
    bus.subscribe(rec)
    yield rec
    bus._handlers.clear()


@pytest.mark.unit
async def test_book_token_broadcasts_display_and_tracking(
    db_session: AsyncSession, recorder
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)

    token = await token_service.book_token(
        db_session, org_id=org.id, counter_id=counter.id, service_id=None, guest_name="Guest A"
    )

    channels = [c for c, _ in recorder.events]
    assert f"skipline:ws:org:{org.id}" in channels
    assert f"skipline:ws:token:{token.id}" in channels

    org_messages = [raw for c, raw in recorder.events if c.endswith(f"org:{org.id}")]
    display = json.loads(org_messages[0])["data"]
    assert any(c["counter_id"] == counter.id for c in display["counters"])
    # DSP-005 allowlisted DTO: no private customer information anywhere.
    assert "guest" not in org_messages[0].lower()

    token_message = json.loads(
        next(raw for c, raw in recorder.events if c.endswith(f"token:{token.id}"))
    )["data"]
    assert token_message["token_number"] == token.token_number
    assert token_message.get("status") == "waiting"


@pytest.mark.unit
async def test_token_action_broadcasts_only_after_commit(
    db_session: AsyncSession, recorder
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    token = await token_service.book_token(
        db_session, org_id=org.id, counter_id=counter.id, service_id=None, guest_name="Guest B"
    )
    assert len(recorder.events) == 2  # creation broadcast (post-commit)

    called = await token_service.perform_token_action(
        db_session, token.id, org.id, "call"
    )
    assert called.status.value == "called"

    tracking = [
        json.loads(raw)["data"]
        for c, raw in recorder.events
        if c.endswith(f"token:{token.id}")
    ][-1]
    assert tracking["status"] == "called"
    assert tracking["token_number"] == token.token_number


@pytest.mark.unit
async def test_failed_transition_broadcasts_nothing(db_session: AsyncSession, recorder):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    token = await token_service.book_token(
        db_session, org_id=org.id, counter_id=counter.id, service_id=None, guest_name="Guest C"
    )
    recorder.events.clear()

    with pytest.raises(ValueError):
        await token_service.perform_token_action(
            db_session, token.id, org.id, "complete"  # invalid from waiting
        )
    assert recorder.events == []  # nothing committed â‡’ nothing broadcast


@pytest.mark.unit
async def test_unknown_action_broadcasts_nothing(db_session: AsyncSession, recorder):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    token = await token_service.book_token(
        db_session, org_id=org.id, counter_id=counter.id, service_id=None, guest_name="Guest D"
    )
    recorder.events.clear()
    with pytest.raises(ValueError):
        await token_service.perform_token_action(
            db_session, token.id, org.id, "explode"
        )
    assert recorder.events == []


@pytest.mark.unit
async def test_isolated_manager_delivers_via_bus_without_websockets():
    """Bus delivery path: channel parsing routes to the right room handler."""
    manager = WebSocketManager(broadcast_bus=InMemoryBus())

    class DummyWS:
        async def send_text(self, text):
            sent.append(text)

    seen: list[tuple[str, int]] = []

    async def recording_send_org(room, *a, **kw):
        seen.append(("org", room))

    async def recording_send_token(room, *a, **kw):
        seen.append(("token", room))

    original_org, original_token = manager._send_org, manager._send_token
    manager._send_org = recording_send_org  # type: ignore[assignment]
    manager._send_token = recording_send_token  # type: ignore[assignment]
    try:
        await manager.start()  # installs the parseâ†’route handler on the bus
        await manager.broadcast_to_org(5, {"type": "display_update"})
        await manager.broadcast_to_token(9, {"type": "tracking_update"})
    finally:
        manager._send_org, manager._send_token = original_org, original_token
        await manager.stop()

    assert ("org", 5) in seen
    assert ("token", 9) in seen
