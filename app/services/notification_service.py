"""
app/services/notification_service.py
──────────────────────────────────────
Sends notifications via email and records in-app notifications.
SMS is architected as a placeholder (Twilio) ready to activate.
"""

import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from models.notification import Notification, NotificationChannel, NotificationType
from models.token import Token
from models.user import User


# ── In-app ────────────────────────────────────────────────────────────────────

async def create_in_app(
    db: AsyncSession,
    user_id: int,
    notif_type: NotificationType,
    message: str,
    token_id: Optional[int] = None,
    subject: Optional[str] = None,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        token_id=token_id,
        type=notif_type,
        channel=NotificationChannel.IN_APP,
        subject=subject,
        message=message,
    )
    db.add(notif)
    await db.flush()
    return notif


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


# ── Email ─────────────────────────────────────────────────────────────────────

async def send_email(to: str, subject: str, body_html: str) -> None:
    """
    Send an email via SMTP.
    Runs in a thread executor to avoid blocking the event loop.
    In production, replace with a proper email queue (Celery + Redis).
    """
    if not settings.mail_username:
        logger.warning("Email not configured — skipping send to {}", to)
        return

    try:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
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
        logger.info("Email sent to {}: {}", to, subject)
    except Exception as exc:
        logger.error("Email send failed to {}: {}", to, exc)


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


# ── Notification templates ────────────────────────────────────────────────────

async def notify_token_created(db: AsyncSession, token: Token, user: Optional[User]) -> None:
    if not user:
        return
    msg = (
        f"Your token <strong>{token.token_number}</strong> has been booked. "
        f"Current queue position: {token.queue_position}."
    )
    await create_in_app(db, user.id, NotificationType.TOKEN_CREATED, msg, token.id, "Token Booked")
    _track_background_task(
        send_email(
            to=user.email,
            subject=f"[Skip Line] Token {token.token_number} Confirmed",
            body_html=f"<p>Hi {user.name},</p><p>{msg}</p><p>Track your token in the app.</p>",
        ),
        name=f"email_token_created_{token.id}",
    )


async def notify_token_called(db: AsyncSession, token: Token, user: Optional[User]) -> None:
    if not user:
        return
    msg = f"Your token <strong>{token.token_number}</strong> has been called. Please proceed to the counter."
    await create_in_app(db, user.id, NotificationType.TOKEN_CALLED, msg, token.id, "Your Turn!")
    _track_background_task(
        send_email(
            to=user.email,
            subject=f"[Skip Line] {token.token_number} — Your Turn!",
            body_html=f"<p>Hi {user.name},</p><p>{msg}</p>",
        ),
        name=f"email_token_called_{token.id}",
    )


async def notify_queue_nearing(db: AsyncSession, token: Token, user: Optional[User], ahead: int) -> None:
    if not user:
        return
    msg = f"Only <strong>{ahead} person(s)</strong> ahead of token {token.token_number}. Get ready!"
    await create_in_app(db, user.id, NotificationType.QUEUE_NEARING, msg, token.id, "Almost Your Turn")


# ── SMS placeholder ───────────────────────────────────────────────────────────

async def send_sms(to_mobile: str, message: str) -> None:
    """
    SMS via Twilio — activate by filling TWILIO_* env vars.
    Architecture placeholder: replace body with actual Twilio client call.
    """
    if not settings.twilio_account_sid:
        logger.debug("SMS not configured — skipping to {}", to_mobile)
        return
    logger.info("SMS → {} : {}", to_mobile, message[:50])
    # from twilio.rest import Client
    # client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    # client.messages.create(body=message, from_=settings.twilio_from_number, to=to_mobile)
