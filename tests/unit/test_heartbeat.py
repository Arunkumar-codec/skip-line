"""tests/unit/test_heartbeat.py — WebSocket heartbeat lifecycle unit tests."""

import pytest

from app.websocket import heartbeat
from app.websocket.heartbeat import start_heartbeat, stop_heartbeat


@pytest.mark.unit
async def test_start_heartbeat_starts_task():
    await stop_heartbeat()
    task = await start_heartbeat()
    assert task is not None
    assert not task.done()
    assert task.get_name() == "websocket_heartbeat"
    await stop_heartbeat()


@pytest.mark.unit
async def test_start_heartbeat_prevents_duplicate():
    await stop_heartbeat()
    task1 = await start_heartbeat()
    task2 = await start_heartbeat()
    assert task1 is task2
    await stop_heartbeat()


@pytest.mark.unit
async def test_stop_heartbeat_cancels_and_awaits():
    task = await start_heartbeat()
    assert not task.done()
    await stop_heartbeat()
    assert task.done()
    assert task.cancelled()
    assert heartbeat._heartbeat_task is None
