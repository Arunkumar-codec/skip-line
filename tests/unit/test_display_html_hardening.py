"""
tests/unit/test_display_html_hardening.py
────────────────────────────────────────
Phase 6 — public TV display XSS hardening regression.

The public display receives token data over the live WebSocket feed, i.e.
UNTRUSTED input. The renderer must build DOM nodes and use textContent —
never innerHTML/outerHTML interpolation of live payload values.

The test verifies the template contract statically: appearance is preserved
(upcoming-chip class and "Queue empty" state both still rendered) while no
HTML-envelope entry point exists for live data.
"""

from pathlib import Path

import pytest

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "public" / "display.html"


@pytest.fixture(scope="module")
def display_html() -> str:
    assert TEMPLATE_PATH.exists(), f"display template missing: {TEMPLATE_PATH}"
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_display_renderer_has_no_html_interpolation_entry_points(display_html):
    """innerHTML/outerHTML are forbidden in the (public, cookie+key-auth
    only) display page; every dynamic update goes through textContent / DOM."""
    assert "innerHTML" not in display_html
    assert "outerHTML" not in display_html


def test_display_renders_upcoming_tokens_with_dom_text_content(display_html):
    """The upcoming-tokens strip is built with created elements + textContent
    (untrusted WS input), preserving the original visual appearance."""
    assert "upcoming_tokens" in display_html
    # secure DOM construction is in place
    assert "document.createElement('span')" in display_html
    assert "upcoming-chip" in display_html
    assert "chip.textContent = t" in display_html or (
        "chip.textContent = String(t)" in display_html
    )


def test_display_preserves_empty_queue_appearance(display_html):
    """The 'Queue empty' state still renders as a muted, smaller span."""
    assert "Queue empty" in display_html
    assert "text-muted small" in display_html


def test_display_live_payload_only_assigned_via_textcontent(display_html):
    """Current-token updates never pass through HTML parsing either."""
    assert "tokenEl.textContent" in display_html
