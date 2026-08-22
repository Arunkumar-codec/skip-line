"""
app/utils/qr_generator.py
──────────────────────────
Generates QR code PNG files for tokens.
The QR data encodes a URL to the live token tracking page.
"""

import asyncio
from pathlib import Path
from typing import Optional

import qrcode
from qrcode.image.pure import PyPNGImage
from loguru import logger

from app.config import settings


async def generate_token_qr(
    token_id: int,
    org_id: int,
    counter_id: int,
    user_id: Optional[int],
    base_url: str = "http://localhost:8000",
) -> str:
    """
    Generate a QR code PNG for a token and return the relative file path.
    Runs in a thread executor to avoid blocking the async event loop.
    """
    tracking_url = f"{base_url}/public/track/{token_id}"
    filename = f"token_{token_id}_org_{org_id}.png"
    output_dir = Path(settings.qr_code_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    def _generate():
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(tracking_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(str(output_path))

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _generate)

    relative_path = f"{settings.qr_code_dir}/{filename}"
    logger.debug("QR code generated: {}", relative_path)
    return relative_path
