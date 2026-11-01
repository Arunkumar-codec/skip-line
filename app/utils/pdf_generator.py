"""
app/utils/pdf_generator.py
───────────────────────────
Generates professional PDF receipts for tokens using ReportLab.
Includes org name, token number, counter, date/time, queue position, and QR code.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger
from reportlab.lib import colors
from reportlab.lib.pagesizes import A6
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import settings


def _build_pdf(
    output_path: str,
    org_name: str,
    token_number: str,
    counter_name: str,
    service_name: str,
    queue_position: int,
    booked_at: datetime,
    qr_code_path: Optional[str],
    guest_name: Optional[str],
    branch_name: Optional[str] = None,
    slot_label: Optional[str] = None,
    payment_label: Optional[str] = None,
    estimated_wait_minutes: Optional[int] = None,
    instructions: Optional[str] = None,
) -> None:
    """Synchronous PDF generation — called in a thread executor."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A6,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Heading1"],
        fontSize=14,
        textColor=colors.HexColor("#1a56db"),
        spaceAfter=4,
        alignment=1,  # center
    )
    subtitle_style = ParagraphStyle(
        "Sub",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#6b7280"),
        alignment=1,
    )
    token_style = ParagraphStyle(
        "Token",
        parent=styles["Normal"],
        fontSize=32,
        textColor=colors.HexColor("#111827"),
        fontName="Helvetica-Bold",
        alignment=1,
        spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#6b7280"),
    )
    value_style = ParagraphStyle(
        "Value",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#111827"),
        fontName="Helvetica-Bold",
    )

    elements = []

    # Header
    elements.append(Paragraph("⬛ SKIP LINE", title_style))
    elements.append(Paragraph("Digital Queue Token", subtitle_style))
    elements.append(Spacer(1, 6 * mm))

    # Org name
    elements.append(Paragraph(org_name.upper(), ParagraphStyle(
        "Org", parent=styles["Normal"], fontSize=11,
        fontName="Helvetica-Bold", alignment=1,
        textColor=colors.HexColor("#374151"),
    )))
    elements.append(Spacer(1, 4 * mm))

    # Token number
    elements.append(Paragraph(token_number, token_style))
    elements.append(Spacer(1, 4 * mm))

    # Details table (QR-001: org, branch, service, date/slot, queue,
    # payment state — all from authoritative booking data)
    data = []
    if branch_name:
        data.append([Paragraph("Branch", label_style), Paragraph(branch_name, value_style)])
    data.extend([
        [Paragraph("Counter", label_style), Paragraph(counter_name, value_style)],
        [Paragraph("Service", label_style), Paragraph(service_name, value_style)],
        [Paragraph("Position", label_style), Paragraph(str(queue_position), value_style)],
        [Paragraph("Date", label_style), Paragraph(booked_at.strftime("%d %b %Y"), value_style)],
        [Paragraph("Time", label_style), Paragraph(booked_at.strftime("%I:%M %p"), value_style)],
    ])
    if slot_label:
        data.append([Paragraph("Appointment", label_style), Paragraph(slot_label, value_style)])
    if payment_label:
        data.append([Paragraph("Payment", label_style), Paragraph(payment_label, value_style)])
    if estimated_wait_minutes is not None:
        data.append([
            Paragraph("Est. Wait", label_style),
            Paragraph(f"{int(estimated_wait_minutes)} min (snapshot)", value_style),
        ])
    if guest_name:
        data.insert(0, [Paragraph("Name", label_style), Paragraph(guest_name, value_style)])

    table = Table(data, colWidths=[25 * mm, 55 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 5 * mm))

    # QR code
    if qr_code_path and Path(qr_code_path).exists():
        img = Image(qr_code_path, width=30 * mm, height=30 * mm)
        img.hAlign = "CENTER"
        elements.append(img)
        elements.append(Paragraph("Scan to track live", subtitle_style))

    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(
        instructions
        or "Thank you for using Skip Line. Please proceed when your number is called.",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7,
                       textColor=colors.HexColor("#9ca3af"), alignment=1),
    ))

    doc.build(elements)


async def generate_token_receipt(
    token_id: int,
    org_name: str,
    token_number: str,
    counter_name: str,
    service_name: str,
    queue_position: int,
    booked_at: datetime,
    qr_code_path: Optional[str] = None,
    guest_name: Optional[str] = None,
    *,
    branch_name: Optional[str] = None,
    slot_label: Optional[str] = None,
    payment_label: Optional[str] = None,
    estimated_wait_minutes: Optional[int] = None,
    instructions: Optional[str] = None,
) -> str:
    """
    Async wrapper — runs synchronous PDF generation in a thread executor.
    Returns the relative path to the generated PDF.
    """
    output_dir = Path(settings.receipt_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"receipt_token_{token_id}.pdf"
    output_path = str(output_dir / filename)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: _build_pdf(
            output_path,
            org_name,
            token_number,
            counter_name,
            service_name,
            queue_position,
            booked_at,
            qr_code_path,
            guest_name,
            branch_name=branch_name,
            slot_label=slot_label,
            payment_label=payment_label,
            estimated_wait_minutes=estimated_wait_minutes,
            instructions=instructions,
        ),
    )

    relative = f"{settings.receipt_dir}/{filename}"
    logger.debug("PDF receipt generated: {}", relative)
    return relative
