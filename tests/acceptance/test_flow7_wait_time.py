"""
tests/acceptance/test_flow7_wait_time.py

Flow 7 - Explainable waiting-time estimates (browser acceptance, Phase 10,
MASTER 16.1-16.7 / 24.7).

Real UI: customer books through the #bookModal, the tracking page shows the
estimate, the "Estimated (not guaranteed)" label, an Updated timestamp and an
explanation; a material queue change (operator calls the token ahead) refreshes
the following token's estimate and timestamp; the public TV display (real
display-access-key install, unauthenticated screen context) shows Est. wait +
updated timestamp with ZERO customer PII; the receipt still downloads as a PDF.

Queue isolation note (flow3 precedent): tokens left WAITING by earlier flows
in the same session DB would shift the queue counts, so they are closed via
DB update before booking (harness-only; the flow under test is untouched).
"""
import httpx
import pytest
from playwright.async_api import expect

from tests.acceptance.conftest import api_login_cookie, ui_login

pytestmark = pytest.mark.asyncio


async def _book_token(page, server):
    """Book through the real booking modal (flow2 pattern); return ids."""
    await page.goto(
        "/user/organizations?q=" + server.org_name.split()[0],
        wait_until="domcontentloaded")
    await page.click('[onclick*="openBookModal"]')
    await expect(page.locator("#bookModal")).to_be_visible()
    await page.select_option("#bookServiceId", str(server.service_id))
    await page.select_option("#bookCounterId", str(server.counter_id))
    await page.click("#confirmBookBtn")
    await expect(page.locator("#bookStep2")).to_be_visible()
    token_number = (await page.locator("#bookedTokenNum").inner_text()).strip()
    track_href = await page.locator("#trackLink").get_attribute("href")
    receipt_href = await page.locator("#receiptLinkBook").get_attribute("href")
    return int(track_href.rsplit("/", 1)[-1]), token_number, receipt_href


async def test_flow7_estimate_timestamp_refresh_display_privacy_pdf(page, server):
    # ── Queue isolation: close leftovers so counts are deterministic ────────
    server.db_exec(
        "UPDATE tokens SET status = 'COMPLETED' WHERE counter_id = :c "
        "AND status = 'WAITING'",
        {"c": server.counter_id},
    )

    await ui_login(page, "user", server.user1_email, server.user_password)

    # ── 1. First booking: tracking shows estimate + timestamp + honesty ────
    token1_id, token1_number, _ = await _book_token(page, server)

    await page.click("#trackLink")
    await page.wait_for_url("**/track/**", timeout=20000)
    await expect(page.locator("#tokenNum")).to_have_text(token1_number)

    # First in line: honest 0-minute estimate, never a fabricated default.
    await expect(page.locator("#etaMin")).to_have_text("0")
    # "Estimate (not guaranteed)" labeling (16.4).
    await expect(page.locator("text=Estimated (not guaranteed)")).to_be_visible()
    # Updated timestamp + explanation present (16.3/16.4).
    updated1 = await page.locator("#etaUpdated").inner_text()
    assert "Updated" in updated1 and updated1.strip() != "Updated"
    explanation1 = await page.locator("#etaExplanation").inner_text()
    assert explanation1.strip() != ""

    # 16.6 anchor: the booking persisted the initial prediction snapshot.
    snap = server.db_fetchone(
        "SELECT predicted_wait_minutes, estimation_status, estimated_at "
        "FROM wait_time_estimates WHERE token_id = :t "
        "AND snapshot_kind = 'initial'",
        {"t": token1_id})
    assert snap is not None, "booking must persist the initial snapshot"
    assert snap[0] == 0 and snap[1] == "estimate"
    assert snap[2] is not None

    # ── 2. Second booking: the customer BEHIND sees the real queue wait ────
    token2_id, token2_number, receipt_href = await _book_token(page, server)
    assert token2_id != token1_id

    await page.click("#trackLink")
    await page.wait_for_url("**/track/**", timeout=20000)
    await expect(page.locator("#tokenNum")).to_have_text(token2_number)
    # One person ahead -> 1 x 10 configured minutes.
    await expect(page.locator("#etaMin")).to_have_text("10")
    await expect(page.locator("#peopleAhead")).to_have_text("1")
    updated2 = await page.locator("#etaUpdated").inner_text()
    assert "Updated" in updated2 and updated2.strip() != "Updated"

    snap2 = server.db_fetchone(
        "SELECT predicted_wait_minutes FROM wait_time_estimates "
        "WHERE token_id = :t AND snapshot_kind = 'initial'",
        {"t": token2_id})
    assert snap2 is not None and snap2[0] == 10

    # ── 3. Material queue event refreshes the estimate (16.4 / 24.7) ────────
    # The operator calls token 1 through the real org API; token 2's wait
    # must drop to 0 and its estimate timestamp must refresh.
    cookies = await api_login_cookie(
        server.base_url, "org", server.org_email, server.org_password)
    async with httpx.AsyncClient(base_url=server.base_url, cookies=cookies) as hc:
        call = await hc.post(f"/org/tokens/{token1_id}/action",
                             json={"action": "call"})
        assert call.status_code == 200, call.text

    await page.goto(f"/user/track/{token2_id}", wait_until="domcontentloaded")
    await expect(page.locator("#etaMin")).to_have_text("0")
    await expect(page.locator("#peopleAhead")).to_have_text("0")
    updated2_after = await page.locator("#etaUpdated").inner_text()
    assert updated2_after != updated2, "estimate timestamp must refresh"

    # 16.6: the queue event persisted a recalc snapshot for token 2.
    recalc = server.db_fetchone(
        "SELECT predicted_wait_minutes FROM wait_time_estimates "
        "WHERE token_id = :t AND snapshot_kind = 'recalc'",
        {"t": token2_id})
    assert recalc is not None and recalc[0] == 0

    # ── 4. Receipt: static PDF snapshot still downloads (flow2 contract) ───
    rec = await page.context.request.get(receipt_href)
    assert rec.status == 200, rec.status
    assert rec.headers.get("content-type", "").startswith("application/pdf")

    # ── 5. Public TV display: estimate + timestamp, ZERO PII (16.7) ─────────
    # Real authorization path: org rotates the display-access-key; the
    # screen itself is an UNAUTHENTICATED context carrying only the key.
    async with httpx.AsyncClient(base_url=server.base_url, cookies=cookies) as hc:
        rot = await hc.post("/org/display-key/rotate")
        assert rot.status_code == 200, rot.text
        display_key = rot.json()["data"]["key"]
    assert display_key

    # Customer PII that must NEVER reach the display:
    pii = server.db_fetchone(
        "SELECT name, mobile FROM users WHERE id = :u", {"u": server.user1_id})
    customer_name, customer_mobile = pii[0], pii[1]

    screen_ctx = await page.context.browser.new_context(
        base_url=server.base_url, viewport={"width": 1280, "height": 720})
    screen = await screen_ctx.new_page()
    try:
        await screen.goto(
            f"/public/display-view/{server.org_id}?key={display_key}",
            wait_until="domcontentloaded")
        content = await screen.content()

        # token 2 still WAITING x configured 10 min -> a new arrival sees 10.
        assert "Est. wait" in content
        assert "Est. wait: 10 min" in content, content
        assert "updated" in content.lower()  # estimated_at timestamp shown

        # Zero customer PII on the public screen (16.7 / 24.7).
        assert customer_name not in content
        assert customer_mobile not in content
        # Token NUMBERS are allowed (that is the point of the display).
        assert token1_number in content or token2_number in content
    finally:
        await screen_ctx.close()
