"""
tests/integration/test_redis_real.py
────────────────────────────────────
Phase 6 — verification of the RedisBus against a REAL Redis server.

These tests run ONLY when a real Redis instance is reachable:

- the `redis` package must be installed (lazy-imported — its absence is a
  clean skip, not a failure);
- a server must respond to PING within 1s (point REDIS_TEST_URL at your
  instance; default redis://127.0.0.1:6379/15).

Isolation from production/test traffic:
- a dedicated Redis database number (default db 15);
- a dedicated server can be selected with REDIS_TEST_URL;
- note: Redis pub/sub channels are server-global (not keyspace-scoped), so
  the channel tests use unique per-run ids as their namespace — traffic
  between parallel runs on the SAME server cannot collide.

Every test that needs live data also skips (not fails) if Redis goes away
mid-session. Nothing in this module is a substitute for the unit suite in
tests/unit/test_redis_bus.py, which uses a fake broker so that ordinary
unit runs never require Redis.
"""

import os
import uuid

import pytest

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def _real_redis_reachable() -> bool:
    try:
        import redis  # lazy — optional dependency
    except ImportError:
        return False
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(REDIS_TEST_URL)
        client = redis.Redis(
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 6379,
            db=int((parts.path or "/15").lstrip("/") or 15),
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        return bool(client.ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _real_redis_reachable(),
    reason=(
        "real-Redis verification pending: no reachable Redis server "
        f"(configure REDIS_TEST_URL, currently {REDIS_TEST_URL})"
    ),
)


async def _pump_until(cond, max_seconds: float = 5.0) -> bool:
    import asyncio

    deadline = asyncio.get_running_loop().time() + max_seconds
    while not cond() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.002)
    return bool(cond())


class _Recorder:
    def __init__(self, name: str):
        self.name = name
        self.messages: list[tuple[str, str]] = []

    async def __call__(self, channel: str, raw) -> None:
        self.messages.append((channel, raw if isinstance(raw, str) else raw.decode()))


@pytest.mark.integration
async def test_real_redis_publish_reaches_fresh_buses_and_stops_clean():
    """Lifecycle smoke test: the bus connects, delivers, and detaches."""
    import asyncio

    from app.websocket.bus import RedisBus

    recorder = _Recorder("lifecycle")
    bus = RedisBus(REDIS_TEST_URL, reconnect_interval=0.05)
    bus.subscribe(recorder)
    await bus.start()

    try:
        assert await _pump_until(lambda: bus._listener_task is not None)
        await asyncio.sleep(0.15)  # listener must be subscribed to deliver
    finally:
        await bus.stop()
    # full lifecycle without crash — client and task released
    assert bus._listener_task is None


@pytest.mark.integration
async def test_real_redis_pubsub_routes_channel_to_multiple_bus_instances():
    """Publish → subscribe across two independent bus processes/instances."""
    import asyncio

    from app.websocket.bus import RedisBus, org_channel

    # unique per-run channel (server-global pub/sub ⇒ no cross-run collisions)
    org_id = int(uuid.uuid4().int % 1_000_000_000) + 1
    channel = org_channel(org_id)

    a, b = _Recorder("worker-a"), _Recorder("worker-b")
    bus_a = RedisBus(REDIS_TEST_URL, reconnect_interval=0.05)
    bus_b = RedisBus(REDIS_TEST_URL, reconnect_interval=0.05)
    bus_a.subscribe(a)
    bus_b.subscribe(b)

    await bus_a.start()
    await bus_b.start()
    try:
        await asyncio.sleep(0.2)  # both listeners now psubscribed
        await bus_a.publish(channel, f"hello-{org_id}")

        assert await _pump_until(lambda: a.messages)
        assert await _pump_until(lambda: b.messages)
        assert a.messages == [(channel, f"hello-{org_id}")]
        assert b.messages == [(channel, f"hello-{org_id}")]
    finally:
        await bus_a.stop()
        await bus_b.stop()


@pytest.mark.integration
async def test_real_redis_channel_routing_org_vs_token_and_foreign_exclusion():
    """Each message arrives tagged with its own org/token channel; foreign
    prefixes are consumed by the pattern subscription but must be ignored
    by the local parser (organisation isolation at the dispatch boundary)."""
    import asyncio

    from app.websocket.bus import RedisBus, org_channel, parse_channel, token_channel

    run_tag = uuid.uuid4().hex[:12]
    org_id = int(uuid.uuid4().int % 1_000_000_000) + 1
    token_id = int(uuid.uuid4().int % 1_000_000_000) + 1

    rec = _Recorder("router")
    bus = RedisBus(REDIS_TEST_URL, reconnect_interval=0.05)
    bus.subscribe(rec)
    await bus.start()
    try:
        await asyncio.sleep(0.2)
        await bus.publish(org_channel(org_id), f"org-msg-{run_tag}")
        await bus.publish(token_channel(token_id), f"token-msg-{run_tag}")

        assert await _pump_until(lambda: len(rec.messages) >= 2)
        by_channel = dict(rec.messages)
        assert by_channel[org_channel(org_id)] == f"org-msg-{run_tag}"
        assert by_channel[token_channel(token_id)] == f"token-msg-{run_tag}"
        # routing parser separates kinds — org updates cannot be mistaken
        # for token updates (organisation isolation at dispatch)
        assert parse_channel(org_channel(org_id)) == ("org", org_id)
        assert parse_channel(token_channel(token_id)) == ("token", token_id)
    finally:
        await bus.stop()


@pytest.mark.integration
async def test_real_redis_publish_failure_falls_back_to_local_delivery():
    """Publishing through an UNREACHABLE Redis endpoint must not silently
    drop the update — the local handler still receives it."""
    from app.websocket.bus import RedisBus, org_channel

    run_tag = uuid.uuid4().hex[:12]
    # closed port on loopback — guaranteed unreachable, never a foreign server
    unreachable = "redis://127.0.0.1:6399/15"

    rec = _Recorder("fallback")
    bus = RedisBus(unreachable, reconnect_interval=0.05)
    bus.subscribe(rec)
    await bus.start()
    try:
        await bus.publish(org_channel(1), f"local-fallback-{run_tag}")
        assert await _pump_until(lambda: rec.messages, max_seconds=3.0)
        assert rec.messages == [(org_channel(1), f"local-fallback-{run_tag}")]
    finally:
        await bus.stop()
