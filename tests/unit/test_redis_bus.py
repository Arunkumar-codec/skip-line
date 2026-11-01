"""
tests/unit/test_redis_bus.py
────────────────────────────
RedisBus behaviour for multi-worker WebSocket fan-out without a real Redis
server — a fake `redis.asyncio` broker is injected so the bus code paths
(publish, psubscribe loop, local delivery, failure fallback, reconnection)
are exercised for real.

Covers Phase 6 requirements:
- publish → all subscribed workers deliver locally ("two independent workers")
- Redis publish failure → graceful local fallback (never silently lost)
- listener connection drop → reconnection loop resumes delivery
- foreign channels are ignored by the local delivery parser
"""

import asyncio
import sys
import types

import pytest

from app.websocket.bus import RedisBus, parse_channel, CHANNEL_PREFIX


# ── Fake redis broker ────────────────────────────────────────────────────────

class FakePubSub:
    def __init__(self, broker):
        self.broker = broker
        self._queue: asyncio.Queue = asyncio.Queue()
        self.active = True

    async def psubscribe(self, pattern):
        self.broker.subscribers.append(self)

    async def listen(self):
        while True:
            message = await self._queue.get()
            if message is None:  # broker closed the subscription
                self.active = False
                raise ConnectionError("connection lost")
            yield message

    async def aclose(self):
        self.active = False
        if self in self.broker.subscribers:
            self.broker.subscribers.remove(self)


class FakeBroker:
    def __init__(self):
        self.subscribers: list[FakePubSub] = []
        self.fail_publish = False
        self.published: list[tuple[str, bytes]] = []
        self._clients: "asyncio.Queue[FakePubSub]" = asyncio.Queue()

    def new_client(self):
        return _FakeClient(self)

    async def publish(self, channel: str, data: bytes):
        if self.fail_publish:
            raise ConnectionError("redis down")
        self.published.append((channel, data))
        delivery = {"type": "pmessage", "channel": channel, "data": data}
        for sub in self.subscribers:
            sub._queue.put_nowait(delivery)

    def crash_pubsubs(self):
        """Simulate a Redis connection loss for every subscriber."""
        for sub in list(self.subscribers):
            sub._queue.put_nowait(None)


class _FakeClient:
    def __init__(self, broker):
        self.broker = broker
        self.closed = False

    def pubsub(self):
        sub = FakePubSub(self.broker)
        self.broker._clients.put_nowait(sub)
        return sub

    async def publish(self, channel, data):
        if self.broker.fail_publish:
            raise ConnectionError("redis down")
        await self.broker.publish(channel, data)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def broker_and_stubs(monkeypatch):
    broker = FakeBroker()
    fake_asyncio = types.SimpleNamespace(
        from_url=lambda url, decode_responses=False: broker.new_client()
    )
    fake_redis = types.ModuleType("redis")
    fake_redis.asyncio = fake_asyncio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake_asyncio)
    return broker


class _Handler:
    def __init__(self, name: str):
        self.name = name
        self.messages: list[tuple[str, str]] = []

    async def __call__(self, channel: str, raw) -> None:
        self.messages.append((channel, raw if isinstance(raw, str) else raw.decode()))


async def _pump_until(cond, max_seconds: float = 1.0) -> bool:
    """Wait (real time — reconnects use asyncio.sleep timers) until cond()."""
    deadline = asyncio.get_running_loop().time() + max_seconds
    while not cond() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.001)
    return bool(cond())


# ── Channel helpers ──────────────────────────────────────────────────────────

def test_parse_channel_org_and_token():
    assert parse_channel(f"{CHANNEL_PREFIX}org:12") == ("org", 12)
    assert parse_channel(f"{CHANNEL_PREFIX}token:99") == ("token", 99)
    assert parse_channel("other:org:12") is None
    assert parse_channel(f"{CHANNEL_PREFIX}org:abc") is None
    assert parse_channel(CHANNEL_PREFIX) is None


# ── Cross-worker delivery ────────────────────────────────────────────────────

@pytest.mark.unit
async def test_publish_reaches_two_independent_workers(broker_and_stubs):
    broker = broker_and_stubs
    worker_a_bus = RedisBus("redis://fake/0", reconnect_interval=0.01)
    worker_b_bus = RedisBus("redis://fake/0", reconnect_interval=0.01)
    handler_a, handler_b = _Handler("A"), _Handler("B")
    worker_a_bus.subscribe(handler_a)
    worker_b_bus.subscribe(handler_b)

    try:
        await worker_a_bus.start()
        await worker_b_bus.start()

        # drain until broker has both subscribers (psubscribe happens async)
        assert await _pump_until(lambda: len(broker.subscribers) >= 2)

        await worker_a_bus.publish(f"{CHANNEL_PREFIX}org:1", "hello")

        # give each listener a chance to process
        assert await _pump_until(
            lambda: handler_a.messages and handler_b.messages
        )
        assert handler_a.messages == [(f"{CHANNEL_PREFIX}org:1", "hello")]
        assert handler_b.messages == [(f"{CHANNEL_PREFIX}org:1", "hello")]
    finally:
        await worker_a_bus.stop()
        await worker_b_bus.stop()


@pytest.mark.unit
async def test_publish_failure_falls_back_to_local_delivery(broker_and_stubs):
    broker = broker_and_stubs
    bus = RedisBus("redis://fake/0", reconnect_interval=0.01)
    handler = _Handler("local")
    bus.subscribe(handler)
    await bus.start()

    try:
        broker.fail_publish = True
        await bus.publish(f"{CHANNEL_PREFIX}token:7", "local-copy")
        # Publish failed at Redis, but the publishing worker still delivers
        # locally instead of silently dropping the update.
        assert handler.messages == [(f"{CHANNEL_PREFIX}token:7", "local-copy")]
        assert broker.published == []
    finally:
        broker.fail_publish = False
        await bus.stop()


@pytest.mark.unit
async def test_listener_reconnects_after_connection_drop(broker_and_stubs):
    broker = broker_and_stubs
    bus = RedisBus("redis://fake/0", reconnect_interval=0.005, reconnect_max_interval=0.01)
    handler = _Handler("reconnected")
    bus.subscribe(handler)
    await bus.start()

    try:
        assert await _pump_until(lambda: bool(broker.subscribers))
        assert broker.subscribers, "listener must psubscribe at startup"

        broker.crash_pubsubs()  # listeners die with ConnectionError

        # allow the reconnect loop to reestablish a subscription: wait for a
        # FRESH ACTIVE subscription (the doomed one is marked inactive when
        # its generator raises)
        assert await _pump_until(lambda: any(s.active for s in broker.subscribers))
        assert broker.subscribers, "listener must reconnect after drop"

        await bus.publish(f"{CHANNEL_PREFIX}org:3", "after-reconnect")
        # publishing may race the reconnect (message lands on the dying
        # subscription) — realistic degraded-publish pattern: retry until
        # the reinstalled subscription delivers.
        for _ in range(20):
            if handler.messages:
                break
            await bus.publish(f"{CHANNEL_PREFIX}org:3", "after-reconnect")
            await asyncio.sleep(0.01)
        assert await _pump_until(lambda: handler.messages)
        assert handler.messages == [(f"{CHANNEL_PREFIX}org:3", "after-reconnect")]
    finally:
        await bus.stop()


@pytest.mark.unit
async def test_foreign_channel_message_is_ignored(broker_and_stubs):
    broker = broker_and_stubs
    bus = RedisBus("redis://fake/0", reconnect_interval=0.01)
    handler = _Handler("ignore")
    bus.subscribe(handler)
    await bus.start()

    try:
        assert await _pump_until(lambda: bool(broker.subscribers))
        # foreign channel delivered over the bus — parser rejects it
        await broker.publish("unrelated:channel", "spam")
        await asyncio.sleep(0.01)
        assert handler.messages == []
        assert all("unrelated" not in c for c, _ in handler.messages)
    finally:
        await bus.stop()


@pytest.mark.unit
async def test_stop_releases_resources(broker_and_stubs):
    bus = RedisBus("redis://fake/0", reconnect_interval=0.01)
    await bus.start()
    client_task = bus._listener_task
    assert client_task is not None
    await bus.stop()
    assert bus._listener_task is None
    assert bus._client is None
    assert bus._handlers == []


# ── Credential redaction in logs (URL hardening) ─────────────────────────────

def test_safe_redis_endpoint_redacts_every_credential_component():
    from app.websocket.bus import safe_redis_endpoint

    with_password = safe_redis_endpoint("redis://:s3cret@redis.internal:6380/7")
    assert with_password == "redis://redis.internal:6380/7"
    assert "s3cret" not in with_password

    with_user_and_password = safe_redis_endpoint(
        "rediss://admin:p4sswowrd!@cache.example.com:6379/0"
    )
    assert "admin" not in with_user_and_password
    assert "p4sswowrd" not in with_user_and_password
    assert with_user_and_password == "rediss://cache.example.com:6379/0"

    with_query = safe_redis_endpoint("redis://:pw@h:6379/2?ssl_cert_reqs=none")
    assert "pw" not in with_query and "ssl_cert_reqs" not in with_query
    assert with_query == "redis://h:6379/2"


def test_safe_redis_endpoint_handles_malformed_urls_safely():
    from app.websocket.bus import safe_redis_endpoint

    # malformed inputs must never raise and never echo secrets
    assert safe_redis_endpoint("") == "redis://<unspecified-host>"
    assert safe_redis_endpoint("not a url") == "redis://<unspecified-host>"
    # schemeless input whose "path" contains credential text must never echo it
    leaked = safe_redis_endpoint("user:pw@host/x")
    assert "pw" not in leaked
    assert safe_redis_endpoint("redis://:pass18346464646@h:99999999/0") ==\
        "<invalid-redis-url>"
    assert safe_redis_endpoint("redis://:pw@h:abc") == "<invalid-redis-url>"


def test_redact_redis_credentials_removes_url_with_secrets():
    from app.websocket.bus import redact_redis_credentials

    leaked = "ConnectionError('Error 111 connecting to redis://:topsecret@10.0.0.5:6379. None')"
    redacted = redact_redis_credentials(leaked)
    assert "topsecret" not in redacted
    assert "10.0.0.5" not in redacted
    assert "<redacted-url>" in redacted
    # plain URLs without credentials pass through untouched
    assert redact_redis_credentials("redis://host:6379") == "redis://host:6379"


async def test_connection_log_never_contains_the_configured_url(broker_and_stubs):
    """Regression: the listener's 'connected' log uses the redacted endpoint."""
    import loguru

    broker = broker_and_stubs
    secret_url = "redis://bus_user:super_secret@fake-internal/0"
    lines: list[str] = []

    sink_id = loguru.logger.add(
        lambda msg: lines.append(str(msg).strip())
    )
    bus = RedisBus(secret_url, reconnect_interval=0.01)
    try:
        await bus.start()
        assert await _pump_until(lambda: bool(broker.subscribers))
        assert await _pump_until(
            lambda: any("Redis bus connected" in line for line in lines)
        )
        connected = next(line for line in lines if "Redis bus connected" in line)
        # the endpoint is logged, but neither credentials nor the raw URL
        assert "redis://fake-internal/0" in connected
        assert "super_secret" not in connected
        assert "bus_user" not in connected
        assert secret_url not in connected
    finally:
        loguru.logger.remove(sink_id)
        await bus.stop()


async def test_listener_failure_log_never_contains_the_configured_url(
    broker_and_stubs,
):
    """Regression: fallback/reconnect failure logs never echo secrets."""
    import loguru

    broker = broker_and_stubs
    secret_url = "redis://bus_user:super_secret@fake-internal/0"
    lines: list[str] = []

    sink_id = loguru.logger.add(lambda msg: lines.append(str(msg).strip()))
    bus = RedisBus(secret_url, reconnect_interval=0.01)

    class _ExplodingBroker(FakeBroker):
        async def publish(self, channel, data):
            # mimic redis-py raising with the credential-bearing URL embedded
            raise ConnectionError(
                "Error connecting to redis://bus_user:super_secret@fake-internal"
            )

    broker.__class__ = _ExplodingBroker
    try:
        await bus.start()
        # publishing path that raises → logged inside connection error
        await bus.publish(f"{CHANNEL_PREFIX}org:1", "boom")
        await bus.stop()
        await asyncio.sleep(0.05)
        joined = "\n".join(lines)
        assert "super_secret" not in joined, joined
    finally:
        loguru.logger.remove(sink_id)
        broker.__class__ = FakeBroker
