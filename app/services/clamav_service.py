"""
app/services/clamav_service.py
──────────────────────────────
Phase 8 — Malware scanning via local ClamAV over the INSTREAM TCP protocol
(SEC-003 / ORGKYC-007).

The approved approach (user authorization, Nov 2026) is LOCAL CLAMAV:
  - uploads first land in a private quarantine directory;
  - `scan_bytes()` speaks the clamd INSTREAM protocol (4-byte big-endian
    length-prefixed chunks terminated by a zero-length chunk) directly —
    no extra Python dependency is required;
  - clean files are promoted to the final private storage, infected files
    are rejected and the quarantine copy destroyed, and the outcome is
    recorded on the durable rows;
  - FAIL-CLOSED: when scanning is enabled but the scanner is unreachable
    or times out, the upload is REFUSED (HTTP 503) — unscanned bytes are
    never treated as clean;
  - when `CLAMAV_ENABLED=false` (explicitly configured development /
    docker-compose-without-clamav), uploads carry
    `scan_status="disabled"` and the gap is documented rather than hidden.

The harmless EICAR test signature is used in tests to demonstrate
infected-file handling — never real malware.
"""

import asyncio
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from app.config import settings


class ScanStatus:
    CLEAN = "clean"
    INFECTED = "infected"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    OK = "OK"  # raw clamd reply for clean streams


# The harmless EICAR antisivirus test string (industry-standard test file;
# every AV engine detects it by the exact byte signature).
EICAR_TEST_STRING = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


@dataclass
class ScanResult:
    status: str          # one of ScanStatus.{CLEAN, INFECTED, UNAVAILABLE, DISABLED}
    signature: Optional[str] = None   # e.g. "Eicar-Signature" when infected
    detail: Optional[str] = None      # engine reply / error description for evidence

    @property
    def is_clean(self) -> bool:
        return self.status == ScanStatus.CLEAN


async def _instream_scan(data: bytes) -> tuple[str, str]:
    """Speak clamd INSTREAM; returns (clamd_reply, ok_flag).

    Raises any socket OSError / asyncio.TimeoutError to the caller, which
    maps it to the fail-closed UNAVAILABLE outcome.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(settings.clamav_host, settings.clamav_port),
        timeout=settings.clamav_scan_timeout_seconds,
    )
    try:
        writer.write(b"zINSTREAM\0")
        await writer.drain()
        view = memoryview(data)
        chunk_size = 4096
        for start in range(0, len(view), chunk_size):
            chunk = view[start:start + chunk_size]
            writer.write(struct.pack("!L", len(chunk)) + chunk)
        await writer.drain()
        writer.write(struct.pack("!L", 0))  # terminator
        await writer.drain()

        reply = await asyncio.wait_for(
            reader.read(1024), timeout=settings.clamav_scan_timeout_seconds
        )
        return reply.decode("utf-8", errors="replace").strip(), ""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # pragma: no cover - close is best effort
            pass


def scan_disabled() -> bool:
    return not settings.clamav_enabled


async def scan_bytes(data: bytes, *, filename: str = "") -> ScanResult:
    """Scan one upload payload.

    Fail-closed policy: any transport error / timeout yields
    UNAVAILABLE (the caller must refuse the upload rather than accept it
    unscanned). A FOUND reply carries the detected signature name.
    """
    if not settings.clamav_enabled:
        return ScanResult(ScanStatus.DISABLED, detail="malware scanning is disabled by configuration")

    started = time.monotonic()
    try:
        reply, _ = await _instream_scan(data)
    except asyncio.TimeoutError:
        logger.warning("ClamAV scan timed out after {}s (file {})",
                       settings.clamav_scan_timeout_seconds, filename)
        return ScanResult(
            ScanStatus.UNAVAILABLE,
            detail=f"scanner timed out after {settings.clamav_scan_timeout_seconds}s",
        )
    except (OSError, socket.gaierror) as exc:
        logger.warning("ClamAV unreachable at {}:{}: {}",
                       settings.clamav_host, settings.clamav_port, exc)
        return ScanResult(
            ScanStatus.UNAVAILABLE,
            detail=f"scanner unreachable at {settings.clamav_host}:{settings.clamav_port}",
        )

    elapsed = round(time.monotonic() - started, 3)
    if "FOUND" in reply.upper():
        # clamd replies "stream: <SIGNATURE> FOUND"
        signature = reply.split(":", 1)[-1].rsplit("FOUND", 1)[0].strip()
        logger.warning("Malware signature {}: file {}", signature, filename)
        return ScanResult(ScanStatus.INFECTED, signature=signature, detail=reply)
    if "OK" in reply.upper():
        return ScanResult(ScanStatus.CLEAN, detail=f"{reply} ({elapsed}s)")

    # Non-standard reply — do not accept the file (fail closed)
    return ScanResult(
        ScanStatus.UNAVAILABLE,
        detail=f"unrecognized scanner reply: {reply[:120]!r}",
    )


def scan_record_for(result: ScanResult) -> tuple[str, str | None]:
    """Return the (scan_status, scan_signature) pair persisted on durable
    rows for evidence (organization_verifications / case_attachments);
    the row's scanned_at timestamp is applied by the caller."""
    signature = result.signature if result.status == ScanStatus.INFECTED else None
    return result.status, signature
