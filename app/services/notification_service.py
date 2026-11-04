"""
app/services/notification_service.py
────────────────────────────────────
Phase 8 — Reliable notification delivery (NTF-001..005).

Architecture:
  - IN-APP: `Notification` rows remain the user-visible record, stored
    at event time (delivery_state="delivered").
  - DURABLE OUTBOUND: email / SMS go through the `notification_outbox`
    queue (`NotificationOutbox`) with delivery state, attempts, retry
    backoff and timestamps (NTF-002). Failed deliveries are NEVER marked
    sent; SMS is never reported as delivered when the provider is
    unconfigured (the row records the gap instead).
  - DEDUP (NTF-005): queue-call and queue-nearing notifications carry a
    `dedup_key`; the same key inside the dedup window is collapsed to one
    in-app row and one outbox delivery.
  - PREFERENCES (NTF-004): users manage optional types; essential types
    (security, payment, service-disruption — ESSENTIAL_TYPES) can never
    be disabled.
  - MINIMIZATION (NTF-003): email subjects and SMS bodies pass through
    `minimal_preview_text`, which redacts mobile numbers, emails and long
    digit runs before any lock-screen / preview transmission.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from models.notification import (
    ESSENTIAL_TYPES,
    DeliveryState,
    Notification,
    NotificationChannel,
    NotificationPreference,
    NotificationType,
)
from models.notification_outbox import NotificationOutbox, OutboxStatus
from models.token import Token
from models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── In-app ────────────────────────────────────────────────────────────────────

async def create_in_app(
    db: AsyncSession,
    user_id: int,
    notif_type: NotificationType,
    message: str,
    token_id: Optional[int] = None,
    subject: Optional[str] = None,
    dedup_key: Optional[str] = None,
) -> Optional[Notification]:
    """Record an in-app notification.

    Returns None when the type is opted out by user preference (NTF-004)
    or when the same `dedup_key` was already sent inside the dedup window
    (NTF-005).
    """
    prefs = await get_preferences(db, user_id)
    if not type_allowed(prefs, notif_type):
        return None

    if dedup_key and await _recent_in_app_duplicate(db, user_id, dedup_key):
        return None

    notif = Notification(
        user_id=user_id,
        token_id=token_id,
        type=notif_type,
        channel=NotificationChannel.IN_APP,
        subject=subject,
        message=message,
        delivery_state=DeliveryState.DELIVERED,
        dedup_key=dedup_key,
        template_version=TEMPLATE_VERSION,
    )
    db.add(notif)
    await db.flush()
    return notif


async def _recent_in_app_duplicate(
    db: AsyncSession, user_id: int, dedup_key: str
) -> bool:
    since = _utcnow() - timedelta(seconds=DEDUP_WINDOW_SECONDS)
    count = (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.dedup_key == dedup_key,
                Notification.sent_at >= since,
            )
        )
    ).scalar() or 0
    return count > 0


async def get_user_notifications(
    db: AsyncSession, user_id: int, unread_only: bool = False
) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read == False)  # noqa: E712
    stmt = stmt.order_by(Notification.sent_at.desc()).limit(50)
    return list((await db.execute(stmt)).scalars().all())


async def mark_notifications_read(db: AsyncSession, user_id: int) -> None:
    notifs = (await db.execute(
        select(Notification).where(
            Notification.user_id == user_id, Notification.is_read == False  # noqa: E712
        )
    )).scalars().all()
    for n in notifs:
        n.is_read = True
    await db.commit()


# ── Preferences (NTF-004) ────────────────────────────────────────────────────

async def get_preferences(
    db: AsyncSession, user_id: int
) -> Optional[NotificationPreference]:
    return (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def set_preferences(
    db: AsyncSession, user_id: int, disabled_types: list[str]
) -> NotificationPreference:
    """Create/update the user's optional-type opt-out list. Essential
    types (security, payment, service-disruption) are REJECTED — NTF-004
    forbids silently disabling them."""
    clean = []
    for value in disabled_types or []:
        if not hasattr(NotificationType, value.upper()):
            raise ValueError(f"Unknown notification type '{value}'")
        if value in ESSENTIAL_TYPES:
            raise ValueError(
                f"'{value}' is an essential notice and cannot be disabled"
            )
        clean.append(value)
    prefs = await get_preferences(db, user_id)
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)
        db.add(prefs)
    prefs.disabled_types = clean
    await db.flush()
    return prefs


def type_allowed(
    prefs: Optional[NotificationPreference], notif_type: str
) -> bool:
    value = getattr(notif_type, "value", str(notif_type))
    if value in ESSENTIAL_TYPES:
        return True
    if prefs is None or not prefs.disabled_types:
        return True
    return value not in prefs.disabled_types


# ── Sensitive-detail minimisation (NTF-003) ─────────────────────────────────

_MOBILE_RE = re.compile(r"(\+?\d[\d\s-]{8,13}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

SUBJECT_MAX_LENGTH = 78  # classic secure email-subject line length


def minimal_preview_text(value: str, max_length: int = 160) -> str:
    """Redact potentially sensitive personal/payment details from text
    headed to lock screens, SMS previews or email subjects. Mobile-like
    number runs, emails and long digit runs become affixes; whitespace is
    collapsed and the result is capped."""
    redacted = _EMAIL_RE.sub("[contact redacted]", value or "")
    redacted = _MOBILE_RE.sub("[number redacted]", redacted)
    redacted = re.sub(r"\d{7,}", "[redacted]", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    if len(redacted) > max_length:
        redacted = redacted[: max_length - 3].rstrip() + "…"
    return redacted


# ── Email ────────────────────────────────────────────────────────────────────

async def send_email(to: str, subject: str, body_html: str) -> bool:
    """
    Send an email via SMTP (thread-executor friendly async client).
    Returns True on success and False on failure/impossibility — the
    outbox layer converts that into attempt state; nothing is claimed as
    delivered when the provider is unconfigured.
    """
    if not settings.mail_username:
        logger.warning("Email not configured — skipping send to {}", to)
        return False

    try:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = minimal_preview_text(subject, SUBJECT_MAX_LENGTH)
        msg["From"] = f"{settings.mail_from_name} <{settings.mail_from}>"
        msg["To"] = to
        msg.attach(MIMEText(body_html, "html"))

        await aiosmtplib.send(
            msg,
            hostname=settings.mail_server,
            port=settings.mail_port,
            start_tls=settings.mail_starttls,
            username=settings.mail_username,
            password=settings.mail_password,
        )
        logger.info("Email sent to {}", to)
        return True
    except Exception as exc:
        logger.error("Email send failed to {}: {}", to, exc)
        return False


# ── SMS ──────────────────────────────────────────────────────────────────────

async def send_sms(to_mobile: str, message: str) -> bool:
    """
    SMS via Twilio — activate by filling TWILIO_* env vars.
    Returns True only when a provider actually accepted the message.
    Returns False when unconfigured (or on the documented placeholder
    path where no real provider call is made) — delivery is NEVER
    claimed without a real provider round-trip.
    """
    if not settings.twilio_account_sid:
        logger.debug("SMS not configured — skipping to {}", to_mobile)
        return False
    # Placeholder body stays until a provider integration is approved;
    # the durable outbox row records the pending state honestly (NTF-002).
    logger.info("SMS (placeholder, provider not wired) → {}", to_mobile)
    return False


# ── Background Task Management ────────────────────────────────────────────────

_background_tasks: set[asyncio.Task] = set()


def _track_background_task(coro, name: Optional[str] = None) -> asyncio.Task:
    """Create and track a background task with exception logging."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error("Background task '{}' failed: {}", t.get_name(), exc)

    task.add_done_callback(_on_done)
    return task


async def wait_for_background_tasks(timeout: float = 3.0) -> None:
    """Await or cancel pending background tasks gracefully during shutdown/teardown."""
    if not _background_tasks:
        return
    tasks = list(_background_tasks)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for {} background task(s); cancelling...", len(tasks))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ── Durable outbox (NTF-002 / NTF-005) ───────────────────────────────────────

TEMPLATE_VERSION = "v1"
# NTF-005: duplicate-suppression window for queue-call style events
# (provisional — Decision 24): repeated events for the same dedup key
# inside 5 minutes collapse to one delivery.
DEDUP_WINDOW_SECONDS = 300


async def queue_outbox(
    db: AsyncSession,
    *,
    channel: str,
    recipient: str,
    body: str,
    subject: Optional[str] = None,
    notification_type: str = "",
    token_id: Optional[int] = None,
    notification_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    dedup_key: Optional[str] = None,
    template_version: str = TEMPLATE_VERSION,
    max_attempts: int = 3,
) -> Optional[NotificationOutbox]:
    """Enqueue one durable outbound delivery. Returns None when the
    delivery is suppressed (duplicate inside the dedup window or the
    provider is unconfigured for that channel — in that case a
    SUPPRESSED row records the gap; no silent pretending)."""
    provider_ready = (
        bool(settings.mail_username) if channel == "email"
        else bool(settings.twilio_account_sid) if channel == "sms"
        else False
    )

    if dedup_key:
        dup = (
            await db.execute(
                select(NotificationOutbox.id).where(
                    NotificationOutbox.channel == channel,
                    NotificationOutbox.dedup_key == dedup_key,
                    NotificationOutbox.status.in_(
                        (OutboxStatus.PENDING, OutboxStatus.SENDING, OutboxStatus.SENT)
                    ),
                    NotificationOutbox.created_at >= (
                        _utcnow() - timedelta(seconds=DEDUP_WINDOW_SECONDS)
                    ),
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            return None

    row = NotificationOutbox(
        channel=channel,
        recipient=recipient,
        subject=minimal_preview_text(subject, SUBJECT_MAX_LENGTH) if subject else None,
        body=(
            minimal_preview_text(body)
            if channel == "sms" else body
        ),
        notification_type=notification_type,
        template_version=template_version,
        token_id=token_id,
        notification_id=notification_id,
        organization_id=organization_id,
        dedup_key=dedup_key,
        status=(
            OutboxStatus.SUPPRESSED if not provider_ready else OutboxStatus.PENDING
        ),
        max_attempts=max_attempts,
        last_error=None if provider_ready else "provider unconfigured",
    )
    db.add(row)
    await db.flush()

    if row.status == OutboxStatus.PENDING:
        outbox_id = row.id
        if settings.app_env != "testing":
            # Production: dispatch immediately from an independent session
            # (row_wait polling covers the commit race). In the testing env
            # the app is wired against a transient test database and tests
            # drive `dispatch_outbox_row(..., session=...)` explicitly; the
            # durable sweeper (dispatch_pending_outbox) is the safety net.
            _track_background_task(
                dispatch_outbox_row(outbox_id),
                name=f"outbox_{row.channel}_{outbox_id}",
            )
    return row


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 30s, 120s, 480s… after each failed attempt."""
    return 30 * (2 ** max(attempts - 1, 0))


async def dispatch_outbox_row(
    outbox_id: int,
    *,
    session: Optional[AsyncSession] = None,
    row_wait_seconds: float = 5.0,
) -> None:
    """Drive ONE outbox delivery to completion. Marks the row sent/failed
    honestly: failed rows keep an error trail and are retried with
    exponential backoff up to max_attempts; they never become 'sent'.

    The delivery runs in a database session independent of the enqueuing
    transaction. Because the enqueueing request commits AFTER `queue_outbox`
    returns, the background dispatch briefly polls for row visibility
    (row_wait_seconds) instead of skipping-and-losing the delivery.
    Tests / workers may pass their own `session` — the row is then assumed
    visible (flushed) and no polling occurs.
    """
    owns_session = session is None
    deadline = _utcnow() + timedelta(seconds=row_wait_seconds)
    row = None
    while True:
        try:
            if session is None:
                session = AsyncSessionLocal()
            row = await session.get(NotificationOutbox, outbox_id)
        except Exception:
            # e.g. database briefly unavailable — keep polling to the deadline
            row = None
        if row is not None:
            break
        if not owns_session or _utcnow() >= deadline:
            if owns_session and session is not None:
                await session.close()
            return
        # enqueuing transaction not committed yet — brief poll
        await asyncio.sleep(0.1)

    if row.status != OutboxStatus.PENDING:
        # already dispatched / suppressed / failed — never double-send
        if owns_session:
            await session.close()
        return

    # expiry-safe queue-call guard (NTF-005): if the related token has
    # progressed past the callable stage, skip the notification.
    if row.token_id is not None:
        token = await session.get(Token, row.token_id)
        if token is not None and token.status not in ("waiting", "called"):
            row.status = OutboxStatus.SUPPRESSED
            row.last_error = "token no longer callable"
            await session.commit()
            if owns_session:
                await session.close()
            return

    row.status = OutboxStatus.SENDING
    row.attempts = (row.attempts or 0) + 1
    if row.first_attempt_at is None:
        row.first_attempt_at = _utcnow()
    await session.commit()

    try:
        if row.channel == "email":
            delivered = await send_email(row.recipient, row.subject or "", row.body)
        else:
            delivered = await send_sms(row.recipient, row.body)
    except Exception as exc:  # provider client raised
        logger.error("Outbox delivery {} raised: {}", outbox_id, exc)
        delivered = False

    if delivered is True:
        row.status = OutboxStatus.SENT
        row.sent_at = _utcnow()
        row.last_error = None
    else:
        row.last_error = (
            (row.last_error or "delivery failed")
            if row.last_error == "provider unconfigured"
            else "delivery failed (attempt {})".format(row.attempts)
        )
        if row.attempts >= row.max_attempts:
            row.status = OutboxStatus.FAILED
            row.last_error = (
                "delivery failed after {} attempts".format(row.attempts)
            )
        else:
            row.status = OutboxStatus.PENDING
            row.next_attempt_at = _utcnow() + timedelta(
                seconds=_backoff_seconds(row.attempts)
            )
    await session.commit()
    if owns_session:
        await session.close()


async def dispatch_pending_outbox(batch_limit: int = 25) -> int:
    """Durable sweeper (NTF-002): dispatch due PENDING outbox rows —
    the crash-recovery net for any delivery whose immediate background
    task was lost (process restart before commit/dispatch) plus the
    scheduled backoff retries. Runs one session for the whole batch.
    Returns the number of rows processed. Started from the app lifespan
    in non-testing environments; safe to call concurrently."""
    select_stmt = (
        select(NotificationOutbox.id)
        .where(
            NotificationOutbox.status == OutboxStatus.PENDING,
            (NotificationOutbox.next_attempt_at.is_(None))
            | (NotificationOutbox.next_attempt_at <= _utcnow()),
        )
        .order_by(NotificationOutbox.id)
        .limit(batch_limit)
    )
    processed = 0
    async with AsyncSessionLocal() as session:
        ids = (await session.execute(select_stmt)).scalars().all()
        for outbox_id in ids:
            try:
                await dispatch_outbox_row(outbox_id, session=session)
                processed += 1
            except Exception as exc:  # keep sweeping the rest of the batch
                logger.error("Outbox sweep {} failed: {}", outbox_id, exc)
    return processed


async def outbox_sweeper_loop(interval_seconds: int = 30) -> None:
    """Periodic background loop dispatching due outbox rows. Cancelled on
    shutdown via wait_for_background_tasks."""
    while True:
        try:
            await dispatch_pending_outbox()
        except Exception as exc:  # never let the loop die silently
            logger.error("Outbox sweeper cycle failed: {}", exc)
        await asyncio.sleep(interval_seconds)


def start_outbox_sweeper(interval_seconds: int = 30) -> None:
    """Idempotent starter used by the app lifespan (non-testing only)."""
    for task in _background_tasks:
        if task.get_name() == "outbox_sweeper":
            return
    _track_background_task(
        outbox_sweeper_loop(interval_seconds), name="outbox_sweeper"
    )


async def mark_outbox_failure(row: NotificationOutbox, error: str) -> None:
    """Persistence helper used by direct (non-queue) callers that must
    record an honest failure — never marks a failed delivery as sent."""
    row.status = OutboxStatus.FAILED
    row.last_error = error
    row.attempts = (row.attempts or 0) + 1


# ── Notification templates ────────────────────────────────────────────────────

async def notify_token_created(db: AsyncSession, token: Token, user: Optional[User]) -> None:
    if not user:
        return
    msg = (
        f"Your token <strong>{token.token_number}</strong> has been booked. "
        f"Current queue position: {token.queue_position}."
    )
    notif = await create_in_app(
        db, user.id, NotificationType.TOKEN_CREATED, msg, token.id, "Token Booked",
    )
    await queue_outbox(
        db,
        channel="email",
        recipient=user.email,
        subject="[Skip Line] Token Confirmed",
        body=f"<p>Hi,</p><p>{msg}</p><p>Track your token in the app.</p>",
        notification_type=NotificationType.TOKEN_CREATED.value,
        token_id=token.id,
        notification_id=notif.id if notif else None,
        organization_id=token.organization_id,
        dedup_key=f"tcreated:{token.id}",
    )


async def notify_token_called(db: AsyncSession, token: Token, user: Optional[User]) -> None:
    # NTF-005: queue calls are dedup'd per token; guests (no user account)
    # rely on SMS only, and only when a provider is configured.
    if user is None:
        if token.guest_mobile:
            await queue_outbox(
                db,
                channel="sms",
                recipient=token.guest_mobile,
                body="Your token has been called. Please proceed to the counter.",
                subject=None,
                notification_type=NotificationType.TOKEN_CALLED.value,
                token_id=token.id,
                organization_id=token.organization_id,
                dedup_key=f"qcall:{token.id}",
            )
        return

    msg = f"Your token <strong>{token.token_number}</strong> has been called. Please proceed to the counter."
    notif = await create_in_app(
        db, user.id, NotificationType.TOKEN_CALLED, msg, token.id, "Your Turn!",
        dedup_key=f"inapp:qcall:{token.id}",
    )
    await queue_outbox(
        db,
        channel="email",
        recipient=user.email,
        subject=f"[Skip Line] {token.token_number} is being called",
        body=f"<p>Your token is up. Please proceed to the counter.</p>",
        notification_type=NotificationType.TOKEN_CALLED.value,
        token_id=token.id,
        notification_id=notif.id if notif else None,
        organization_id=token.organization_id,
        dedup_key=f"qcall:{token.id}",
    )


async def notify_queue_nearing(db: AsyncSession, token: Token, user: Optional[User], ahead: int) -> None:
    if not user:
        return
    msg = f"Only <strong>{ahead} person(s)</strong> ahead of token {token.token_number}. Get ready!"
    await create_in_app(
        db, user.id, NotificationType.QUEUE_NEARING, msg, token.id, "Almost Your Turn",
        dedup_key=f"inapp:qnearing:{token.id}:{ahead}",
    )
