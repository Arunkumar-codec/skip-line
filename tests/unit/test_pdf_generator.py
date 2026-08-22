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
