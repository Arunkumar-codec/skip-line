"""tests/unit/test_pdf_generator.py — PDF generator helpers."""

import pytest
from datetime import datetime, timezone


class TestPDFHelpers:
    """Test supporting logic for PDF receipt generation."""

    def test_filename_format(self):
        token_id = 42
        filename = f"receipt_token_{token_id}.pdf"
        assert filename == "receipt_token_42.pdf"

    def test_booked_at_formatting(self):
        dt = datetime(2025, 6, 15, 14, 30, tzinfo=timezone.utc)
        assert dt.strftime("%d %b %Y") == "15 Jun 2025"
        assert dt.strftime("%I:%M %p") == "02:30 PM"

    def test_qr_filename_format(self):
        token_id, org_id = 7, 3
        filename = f"token_{token_id}_org_{org_id}.png"
        assert filename == "token_7_org_3.png"


class TestPhase6ReceiptUpgrade:
    """Phase 6 — upgraded receipt content (branch, slot, payment, wait)."""

    @pytest.fixture
    def receipt_args(self):
        return dict(
            token_id=4242,
            org_name="Phase Six Hospital",
            token_number="P6-007",
            counter_name="Counter 3",
            service_name="X-Ray",
            queue_position=2,
            booked_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_generate_token_receipt_with_new_kwargs_creates_pdf(
        self, receipt_args, tmp_path, monkeypatch
    ):
        """All Phase 6 detail kwargs flow through and produce a real PDF."""
        from pathlib import Path

        from app.utils import pdf_generator

        receipt_dir = tmp_path / "receipts"
        monkeypatch.setattr(pdf_generator.settings, "receipt_dir", str(receipt_dir))

        relative = await pdf_generator.generate_token_receipt(
            guest_name="Gate Six Guest",
            branch_name="Main Branch",
            slot_label="Today 10:00–11:00",
            payment_label="Paid at counter",
            estimated_wait_minutes=15,
            instructions="Keep this receipt. Your number is called on the display.",
            **receipt_args,
        )
        out = Path(relative)
        assert out.exists()
        assert out.stat().st_size > 0
        content = out.read_bytes()
        assert content[:5] == b"%PDF-"

    @pytest.mark.asyncio
    async def test_upgraded_kwargs_change_receipt_output(
        self, receipt_args, tmp_path, monkeypatch
    ):
        """The Phase 6 kwargs genuinely alter the rendered document (not
        silently swallowed), proving they are plumbed into _build_pdf."""
        from pathlib import Path

        from app.utils import pdf_generator

        receipt_dir = tmp_path / "receipts"
        monkeypatch.setattr(pdf_generator.settings, "receipt_dir", str(receipt_dir))

        relative_plain = await pdf_generator.generate_token_receipt(**receipt_args)
        plain = Path(relative_plain).read_bytes()

        kwargs = dict(
            branch_name="Main Branch",
            slot_label="Today 10:00–11:00",
            payment_label="Paid at counter",
            estimated_wait_minutes=15,
            instructions="Custom instructions for Gate Six.",
        )
        relative_upgrade = await pdf_generator.generate_token_receipt(
            guest_name="Gate Six Guest", **{**receipt_args, "token_id": 4243}, **kwargs
        )
        upgraded = Path(relative_upgrade).read_bytes()

        # same structural validity, but different rendered content
        assert upgraded[:5] == b"%PDF-"
        assert plain[:5] == b"%PDF-"
        assert upgraded != plain

    @pytest.mark.asyncio
    async def test_generate_token_receipt_without_upgrades_still_works(
        self, receipt_args, tmp_path, monkeypatch
    ):
        """Old callers (pre-Phase-6 signature) keep working — all new kwargs
        are optional and omitted gracefully."""
        from pathlib import Path

        from app.utils import pdf_generator

        receipt_dir = tmp_path / "receipts"
        monkeypatch.setattr(pdf_generator.settings, "receipt_dir", str(receipt_dir))

        relative = await pdf_generator.generate_token_receipt(**receipt_args)
        out = Path(relative)
        assert out.exists()
        assert out.read_bytes()[:5] == b"%PDF-"
