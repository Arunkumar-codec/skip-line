"""
tests/unit/test_phase8_clamav.py
────────────────────────────────
SEC-003 / ORGKYC-007 — malware-scanning pipeline (local ClamAV INSTREAM):

- protocol client: clean / infected (EICAR test signature) / unrecognized
  replies, transport failure and timeout all map to the documented
  outcomes;
- fail-closed: scanner unreachable ⇒ upload REFUSED with 503 and the
  quarantined bytes retained as evidence; scanning never treats
  unscanned bytes as clean;
- infected files: 422 rejection and the quarantine copy destroyed;
- disabled mode: uploads record scan_status="disabled" (documented gap,
  never "clean").
"""

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import attachment_service, clamav_service

# The industry-standard EICAR antivirus test string (harmless by design).
EICAR = clamav_service.EICAR_TEST_STRING.encode("ascii")
PDF_BODY = b"%PDF-1.4\nMinimal test document for the scanner pipeline.\n"


class FakeClamd:
    """Minimal clamd INSTREAM server: reads length-prefixed chunks, then
    replies with the configured message (or hangs, for timeout tests)."""

    def __init__(self, reply: bytes, *, hang: bool = False):
        self.reply = reply
        self.hang = hang
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()

    async def stop(self):
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            handshake = await reader.readexactly(10)  # b"zINSTREAM\0"
            if not handshake.startswith(b"zINSTREAM"):
                return
            while True:
                header = await reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length == 0:
                    break
                await reader.readexactly(length)
            if self.hang:
                await asyncio.sleep(30)  # never replies — feeds timeout tests
            writer.write(self.reply)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    """Isolated scanner + upload configuration."""
    monkeypatch.setattr(settings, "clamav_enabled", True)
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(settings, "clamav_scan_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    return monkeypatch


@pytest.fixture
async def clamd(scan_env):
    """Yields `await clamd(reply=..., hang=...)` to boot a fake clamd."""
    fake = FakeClamd(b"stream: OK\x00")

    async def _boot(reply=b"stream: OK\x00", *, hang: bool = False):
        fake.reply = reply
        fake.hang = hang
        host, port, *_ = await fake.start()
        scan_env.setattr(settings, "clamav_port", port)
        return fake

    try:
        yield _boot
    finally:
        await fake.stop()


class TestScanByteOutcomes:
    async def test_clean_reply(self, clamd):
        await clamd(b"stream: OK\x00")
        result = await clamav_service.scan_bytes(PDF_BODY, filename="a.pdf")
        assert result.is_clean
        assert result.signature is None

    async def test_infected_eicar_reply_carries_signature(self, clamd):
        await clamd(b"stream: Eicar-Signature FOUND\x00")
        result = await clamav_service.scan_bytes(EICAR, filename="b.pdf")
        assert result.status == clamav_service.ScanStatus.INFECTED
        assert "Eicar" in result.signature
        assert not result.is_clean

    async def test_unrecognized_reply_fails_closed(self, clamd):
        await clamd(b"weird nonsense reply")
        result = await clamav_service.scan_bytes(PDF_BODY, filename="c.pdf")
        assert result.status == clamav_service.ScanStatus.UNAVAILABLE

    async def test_timeout_fails_closed(self, clamd):
        await clamd(b"stream: OK\x00", hang=True)
        result = await clamav_service.scan_bytes(PDF_BODY, filename="t.pdf")
        assert result.status == clamav_service.ScanStatus.UNAVAILABLE
        assert "timed out" in result.detail

    async def test_unreachable_scanner_fails_closed(self, scan_env):
        # grab a free port, release it, then point the scanner there — a
        # guaranteed-closed listener slot (never maps to CLEAN)
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        scan_env.setattr(settings, "clamav_port", dead_port)
        result = await clamav_service.scan_bytes(PDF_BODY, filename="d.pdf")
        assert result.status == clamav_service.ScanStatus.UNAVAILABLE

    async def test_disabled_mode_is_not_clean(self, monkeypatch):
        monkeypatch.setattr(settings, "clamav_enabled", False)
        result = await clamav_service.scan_bytes(PDF_BODY, filename="e.pdf")
        assert result.status == clamav_service.ScanStatus.DISABLED
        assert result.is_clean is False

    async def test_scan_record_mapping(self):
        infected = clamav_service.ScanResult(
            clamav_service.ScanStatus.INFECTED, signature="Eicar-Signature"
        )
        status, signature = clamav_service.scan_record_for(infected)
        assert status == "infected"
        assert signature == "Eicar-Signature"
        clean = clamav_service.ScanResult(clamav_service.ScanStatus.CLEAN)
        assert clamav_service.scan_record_for(clean) == ("clean", None)


class TestUploadPipeline:
    async def test_clean_file_promoted_to_final_storage(self, clamd):
        await clamd(b"stream: OK\x00")
        scanned = await attachment_service.save_scanned_file(
            "cases/pipeline", PDF_BODY, "report.pdf"
        )
        assert scanned["scan_status"] == clamav_service.ScanStatus.CLEAN
        final_path = Path(scanned["relpath"])
        assert final_path.exists()
        assert final_path.read_bytes() == PDF_BODY
        assert "quarantine" not in scanned["relpath"]
        # quarantine is empty for clean files (copy promoted, not copied)
        quarantine = Path(settings.upload_dir) / "quarantine" / "cases" / "pipeline"
        assert not any(quarantine.iterdir())

    async def test_infected_file_rejected_and_copies_destroyed(self, clamd):
        await clamd(b"stream: Eicar-Signature FOUND\x00")
        # a genuine PDF payload scanned as infected (the fake clamd always
        # answers with the configured verdict — no real EICAR upload needed
        # to exercise the promote/reject logic)
        with pytest.raises(HTTPException) as exc:
            await attachment_service.save_scanned_file("cases/pipeline", PDF_BODY, "evil.pdf")
        assert exc.value.status_code == 422
        assert "Eicar" in exc.value.detail
        # no infected bytes survive anywhere (approved storage or quarantine)
        for folder in (
            Path(settings.upload_dir) / "cases" / "pipeline",
            Path(settings.upload_dir) / "quarantine" / "cases" / "pipeline",
        ):
            assert folder.exists() is False or not any(
                name.name.endswith(".pdf") for name in folder.iterdir()
            )

    async def test_scanner_unavailable_mid_upload_fails_closed(self, scan_env):
        # scanner unreachable at upload time: refuse the upload (503);
        # the quarantined bytes remain behind as security evidence
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        scan_env.setattr(settings, "clamav_port", dead_port)
        with pytest.raises(HTTPException) as exc:
            await attachment_service.save_scanned_file(
                "cases/pipeline", PDF_BODY, "unscanned.pdf"
            )
        assert exc.value.status_code == 503
        quarantine = Path(settings.upload_dir) / "quarantine" / "cases" / "pipeline"
        assert any(
            q.suffix == ".pdf" for q in quarantine.iterdir()
        ), "quarantined bytes must be retained as evidence on fail-closed"

    async def test_disabled_scanner_records_disabled_status(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "clamav_enabled", False)
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
        scanned = await attachment_service.save_scanned_file(
            "cases/pipeline", PDF_BODY, "report.pdf"
        )
        # honest evidence: the file was NOT scanned — never claim "clean"
        assert scanned["scan_status"] == clamav_service.ScanStatus.DISABLED
        assert scanned["scan_signature"] is None
        assert scanned["scanned_at"] is not None
        # disabled mode skips the magic-byte content gate by configuration
        assert Path(scanned["relpath"]).exists()

    async def test_extension_content_mismatch_rejected(self, clamd):
        await clamd(b"stream: OK\x00")
        with pytest.raises(HTTPException) as exc:
            # EICAR bytes claimed to be a PDF — the declared extension does
            # not match the payload's magic content
            await attachment_service.save_scanned_file("cases/pipeline", EICAR, "fake.pdf")
        assert exc.value.status_code == 400
        assert "does not match" in exc.value.detail
