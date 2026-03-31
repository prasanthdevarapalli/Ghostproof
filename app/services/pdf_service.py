"""
Sprint 4 — PDF Generation Service
ATS-friendly resume and cover letter PDF templates using ReportLab.
Single-column, standard fonts, no graphics — optimised for ATS parsing.
"""

import io
import logging
from datetime import datetime, timezone

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
    ListFlowable,
    ListItem,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Color palette (professional, ATS-safe)
# ---------------------------------------------------------------------------
CLR_PRIMARY = HexColor("#1a1a2e")   # Dark navy — headings
CLR_ACCENT = HexColor("#16213e")    # Slightly lighter — subheadings
CLR_BODY = HexColor("#333333")      # Body text
CLR_MUTED = HexColor("#666666")     # Dates, secondary info
CLR_RULE = HexColor("#cccccc")      # Horizontal rules

# ---------------------------------------------------------------------------
# Shared styles
# ---------------------------------------------------------------------------

def _base_styles():
    """Build a reusable stylesheet for both resume and cover letter."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "ResumeName",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=CLR_PRIMARY,
        alignment=TA_CENTER,
        spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        "ResumeContact",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=CLR_MUTED,
        alignment=TA_CENTER,
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=CLR_PRIMARY,
        spaceBefore=10,
        spaceAfter=4,
        # ATS prefers ALL-CAPS section headings
    ))
    styles.add(ParagraphStyle(
        "JobTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=CLR_ACCENT,
        spaceAfter=1,
    ))
    styles.add(ParagraphStyle(
        "JobMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=CLR_MUTED,
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        "BulletText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=CLR_BODY,
        leftIndent=12,
        spaceAfter=2,
        alignment=TA_JUSTIFY,
    ))
    styles.add(ParagraphStyle(
        "BodyText9",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=CLR_BODY,
        alignment=TA_JUSTIFY,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "SkillsText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=CLR_BODY,
        spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        "CoverParagraph",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        textColor=CLR_BODY,
        alignment=TA_JUSTIFY,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        "CoverGreeting",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        textColor=CLR_BODY,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        "CoverClosing",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        textColor=CLR_BODY,
        spaceBefore=6,
        spaceAfter=4,
    ))
    return styles


def _section_rule():
    return HRFlowable(
        width="100%", thickness=0.5, color=CLR_RULE,
        spaceBefore=2, spaceAfter=6,
    )


# ---------------------------------------------------------------------------
# Resume PDF
# ---------------------------------------------------------------------------

def generate_resume_pdf(tailored: dict, profile: dict) -> bytes:
    """
    Generate an ATS-friendly resume PDF from tailored resume JSON.
    Returns raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    styles = _base_styles()
    story = []

    # --- Header: Name + Contact ---
    name = profile.get("name", tailored.get("candidate_name", ""))
    story.append(Paragraph(name, styles["ResumeName"]))

    contact_parts = []
    if email := profile.get("email"):
        contact_parts.append(email)
    if phone := profile.get("phone"):
        contact_parts.append(phone)
    if loc := profile.get("location"):
        contact_parts.append(loc)
    if contact_parts:
        story.append(Paragraph(" · ".join(contact_parts), styles["ResumeContact"]))

    story.append(_section_rule())

    # --- Professional Summary ---
    if summary := tailored.get("summary"):
        story.append(Paragraph("PROFESSIONAL SUMMARY", styles["SectionHeading"]))
        story.append(Paragraph(summary, styles["BodyText9"]))
        story.append(_section_rule())

    # --- Experience ---
    experience = tailored.get("experience", [])
    if experience:
        story.append(Paragraph("EXPERIENCE", styles["SectionHeading"]))
        for i, exp in enumerate(experience):
            title = exp.get("title", "")
            company = exp.get("company", "")
            dates = exp.get("dates", "")
            story.append(Paragraph(f"{title}", styles["JobTitle"]))
            story.append(Paragraph(f"{company}  |  {dates}", styles["JobMeta"]))
            for bullet in exp.get("bullets", []):
                story.append(Paragraph(f"• {bullet}", styles["BulletText"]))
            if i < len(experience) - 1:
                story.append(Spacer(1, 4))
        story.append(_section_rule())

    # --- Skills ---
    skills = tailored.get("skills", [])
    if skills:
        story.append(Paragraph("SKILLS", styles["SectionHeading"]))
        story.append(Paragraph(", ".join(skills), styles["SkillsText"]))
        story.append(_section_rule())

    # --- Education ---
    education = tailored.get("education", [])
    if education:
        story.append(Paragraph("EDUCATION", styles["SectionHeading"]))
        for edu in education:
            if isinstance(edu, dict):
                degree = edu.get("degree", "")
                inst = edu.get("institution", "")
                year = edu.get("year", "")
                story.append(Paragraph(f"<b>{degree}</b>", styles["BulletText"]))
                story.append(Paragraph(f"{inst}  |  {year}", styles["JobMeta"]))
            else:
                story.append(Paragraph(str(edu), styles["BulletText"]))
        story.append(_section_rule())

    # --- Certifications ---
    certs = tailored.get("certifications", [])
    if certs:
        story.append(Paragraph("CERTIFICATIONS", styles["SectionHeading"]))
        story.append(Paragraph(", ".join(certs), styles["SkillsText"]))

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Cover Letter PDF
# ---------------------------------------------------------------------------

def generate_cover_letter_pdf(cover_letter: dict, profile: dict, job_data: dict) -> bytes:
    """
    Generate a professional cover letter PDF.
    Returns raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
    )

    styles = _base_styles()
    story = []

    # --- Sender info ---
    name = profile.get("name", cover_letter.get("candidate_name", ""))
    story.append(Paragraph(f"<b>{name}</b>", styles["CoverParagraph"]))
    contact_parts = []
    if email := profile.get("email"):
        contact_parts.append(email)
    if phone := profile.get("phone"):
        contact_parts.append(phone)
    if loc := profile.get("location"):
        contact_parts.append(loc)
    if contact_parts:
        story.append(Paragraph(" · ".join(contact_parts), styles["JobMeta"]))

    # Date
    story.append(Spacer(1, 12))
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    story.append(Paragraph(today, styles["JobMeta"]))
    story.append(Spacer(1, 12))

    # Company address block (if available)
    company = job_data.get("company", "")
    job_title = job_data.get("title", "")
    if company:
        story.append(Paragraph(f"Re: {job_title} — {company}", styles["JobTitle"]))
        story.append(Spacer(1, 12))

    # Greeting
    greeting = cover_letter.get("greeting", "Dear Hiring Manager,")
    story.append(Paragraph(greeting, styles["CoverGreeting"]))

    # Body paragraphs
    for para in cover_letter.get("paragraphs", []):
        story.append(Paragraph(para, styles["CoverParagraph"]))

    # Closing
    closing = cover_letter.get("closing", "Sincerely,")
    story.append(Paragraph(closing, styles["CoverClosing"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>{name}</b>", styles["CoverParagraph"]))

    doc.build(story)
    return buf.getvalue()
