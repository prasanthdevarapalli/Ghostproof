"""
Resume Service — Parse uploaded resumes and build master profiles.

Flow:
1. User uploads PDF/DOCX via API
2. Store file in Supabase Storage
3. Extract text from file (PyPDF2 / python-docx)
4. Send text to Haiku 4.5 to extract structured profile
5. Save master_profile to DB
"""

from __future__ import annotations
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

from app.core.config import settings
from app.core.database import get_supabase_service

logger = logging.getLogger(__name__)


# ── Text Extraction ─────────────────────────────────────────────────

async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except Exception as e:
        logger.error("PDF text extraction failed: %s", e)
        return ""


async def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract text from a DOCX file."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.error("DOCX text extraction failed: %s", e)
        return ""


async def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """Route to the correct extractor based on MIME type."""
    if "pdf" in mime_type:
        return await extract_text_from_pdf(file_bytes)
    elif "wordprocessingml" in mime_type or "docx" in mime_type or "msword" in mime_type:
        return await extract_text_from_docx(file_bytes)
    elif "text/plain" in mime_type:
        return file_bytes.decode("utf-8", errors="replace")
    else:
        logger.warning("Unsupported MIME type for text extraction: %s", mime_type)
        return ""


# ── Haiku Profile Extraction ───────────────────────────────────────

PROFILE_EXTRACTION_PROMPT = """You are a professional resume parser. Extract structured information from the resume text below.

Return ONLY valid JSON with no markdown, no code fences:
{
  "full_name": "<string>",
  "headline": "<concise professional headline, e.g. 'Senior Data Engineer | AWS | Spark'>",
  "summary": "<2-3 sentence professional summary>",
  "location": "<city, state/country>",
  "technical_skills": ["<skill1>", "<skill2>", ...],
  "soft_skills": ["<skill1>", "<skill2>", ...],
  "certifications": ["<cert1>", "<cert2>", ...],
  "experience": [
    {
      "title": "<job title>",
      "company": "<company name>",
      "start": "<start date>",
      "end": "<end date or 'Present'>",
      "bullets": ["<achievement/responsibility 1>", ...],
      "skills_used": ["<skill1>", ...]
    }
  ],
  "education": [
    {
      "degree": "<degree name>",
      "institution": "<university/school>",
      "year": "<graduation year>",
      "gpa": "<GPA if mentioned, else empty>"
    }
  ],
  "target_roles": ["<inferred target role 1>", "<role 2>"],
  "years_of_experience": <integer>,
  "completeness_score": <0-100 how complete/well-structured the resume is>
}

Be thorough with skills — extract every technology, tool, framework, language, and platform mentioned.
For target_roles, infer from the person's most recent experience and skills.
For experience bullets, summarize each in ONE short sentence (max 15 words) — do NOT copy verbatim.
Keep the total JSON response under 3500 tokens.
If information is missing, use empty strings or empty arrays — never omit keys."""


async def parse_resume_with_ai(resume_text: str) -> dict:
    """Use Haiku 4.5 to extract structured profile from resume text."""
    if not settings.anthropic_api_key or not resume_text.strip():
        return {}

    # Truncate very long resumes
    truncated = resume_text[:8000] if len(resume_text) > 8000 else resume_text

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=PROFILE_EXTRACTION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Parse this resume:\n\n{truncated}",
                }
            ],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Resume parse response not valid JSON: %s", e)
        return {}
    except Exception as e:
        logger.error("Resume AI parsing failed: %s", e)
        return {}


# ── Storage ─────────────────────────────────────────────────────────

async def store_resume_file(
    user_id: str, filename: str, file_bytes: bytes, mime_type: str
) -> Optional[str]:
    """Upload resume file to Supabase Storage. Returns the file URL."""
    if not settings.supabase_url:
        return None

    try:
        sb = get_supabase_service()
        storage_path = f"{user_id}/{filename}"

        # Upload to 'resumes' bucket
        sb.storage.from_("resumes").upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )

        # Get public URL (bucket is private, but service role can generate signed URL)
        url = sb.storage.from_("resumes").get_public_url(storage_path)
        return url
    except Exception as e:
        logger.error("Resume file upload failed: %s", e)
        return None


# ── Main Resume Processing Pipeline ────────────────────────────────

async def process_resume(
    user_id: str,
    filename: str,
    file_bytes: bytes,
    mime_type: str,
) -> dict:
    """
    Full pipeline:
    1. Extract text from file
    2. Upload to storage
    3. Parse with AI
    4. Save resume record
    5. Create/update master profile
    Returns the master profile dict.
    """
    sb = get_supabase_service()

    # Step 1: Extract text
    extracted_text = await extract_text(file_bytes, mime_type)
    if not extracted_text:
        raise ValueError("Could not extract text from the uploaded file")

    logger.info("Extracted %d chars from %s", len(extracted_text), filename)

    # Step 2: Upload to storage (skip for dev users)
    file_url = ""
    is_dev_user = user_id.startswith("dev-")
    if not is_dev_user:
        file_url = await store_resume_file(user_id, filename, file_bytes, mime_type) or ""

    # Step 3: Parse with AI
    parsed = await parse_resume_with_ai(extracted_text)
    if not parsed:
        raise ValueError("AI could not parse the resume content")

    logger.info("AI parsed profile: %s — %s", parsed.get("full_name", "?"), parsed.get("headline", "?"))

    # For dev users, skip DB writes and cache profile in Redis instead
    if is_dev_user:
        from app.core.database import get_redis
        cache = await get_redis()
        if cache:
            profile_json = json.dumps(parsed)
            await cache.set(f"profile:{user_id}", profile_json, ex=86400)  # 24h
        return {
            "resume_id": None,
            "profile": parsed,
            "text_length": len(extracted_text),
        }

    # Step 4: Unset any existing primary resume, then save new one
    try:
        sb.table("resumes").update({"is_primary": False}).eq("user_id", user_id).eq("is_primary", True).execute()
    except Exception:
        pass

    resume_result = sb.table("resumes").insert({
        "user_id": user_id,
        "filename": filename,
        "file_url": file_url or "",
        "file_size": len(file_bytes),
        "mime_type": mime_type,
        "extracted_text": extracted_text[:50000],  # cap storage
        "is_primary": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    resume_id = resume_result.data[0]["id"] if resume_result.data else None

    # Step 5: Create/update master profile
    profile_data = {
        "user_id": user_id,
        "resume_id": resume_id,
        "full_name": parsed.get("full_name", ""),
        "headline": parsed.get("headline", ""),
        "summary": parsed.get("summary", ""),
        "location": parsed.get("location", ""),
        "technical_skills": json.dumps(parsed.get("technical_skills", [])),
        "soft_skills": json.dumps(parsed.get("soft_skills", [])),
        "certifications": json.dumps(parsed.get("certifications", [])),
        "experience": json.dumps(parsed.get("experience", [])),
        "education": json.dumps(parsed.get("education", [])),
        "target_roles": json.dumps(parsed.get("target_roles", [])),
        "target_locations": json.dumps([]),
        "completeness_score": parsed.get("completeness_score", 0),
        "last_parsed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    sb.table("master_profiles").upsert(
        profile_data,
        on_conflict="user_id",
    ).execute()

    return {
        "resume_id": resume_id,
        "profile": parsed,
        "text_length": len(extracted_text),
    }


# ── Fetch Profile ──────────────────────────────────────────────────

async def get_master_profile(user_id: str) -> Optional[dict]:
    """Fetch the user's master profile."""
    # Dev users: check Redis cache first
    if user_id.startswith("dev-"):
        from app.core.database import get_redis
        cache = await get_redis()
        if cache:
            try:
                cached = await cache.get(f"profile:{user_id}")
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        return None

    if not settings.supabase_url:
        return None

    try:
        sb = get_supabase_service()
        result = (
            sb.table("master_profiles")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if result.data:
            # Parse JSONB fields back
            data = result.data
            for field in ["technical_skills", "soft_skills", "certifications",
                          "experience", "education", "target_roles", "target_locations"]:
                if isinstance(data.get(field), str):
                    try:
                        data[field] = json.loads(data[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            return data
        return None
    except Exception as e:
        logger.error("Profile fetch failed: %s", e)
        return None


async def get_resumes(user_id: str) -> list:
    """Fetch all resumes for a user."""
    if not settings.supabase_url:
        return []

    try:
        sb = get_supabase_service()
        result = (
            sb.table("resumes")
            .select("id, filename, file_size, mime_type, is_primary, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error("Resumes fetch failed: %s", e)
        return []
