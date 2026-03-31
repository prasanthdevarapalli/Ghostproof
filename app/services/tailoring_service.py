"""
Sprint 4 — Resume Tailoring + Cover Letter Generation Service
Two-pass AI pipeline: Haiku 4.5 drafts → Sonnet 4.5 polishes
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import anthropic

from app.core.config import settings
from app.core.database import get_redis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AI client
# ---------------------------------------------------------------------------
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-20250514"

# Max retries for AI calls (rate limits, transient failures)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds


# ---------------------------------------------------------------------------
# Retry wrapper — runs sync Anthropic client in thread pool with backoff
# ---------------------------------------------------------------------------

async def _ai_call_with_retry(model: str, max_tokens: int, system: str, user_content: str) -> str:
    """
    Call Anthropic API with retry + exponential backoff.
    Runs the synchronous client in asyncio thread pool to avoid blocking.
    Returns the text content of the first response block.
    """
    client = _get_client()

    for attempt in range(MAX_RETRIES):
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            return resp.content[0].text.strip()

        except anthropic.RateLimitError as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Rate limited on attempt %d/%d, retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("Rate limit exceeded after %d retries", MAX_RETRIES)
                raise

        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Server error %d on attempt %d/%d, retrying in %.1fs",
                    e.status_code, attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            else:
                raise

    raise RuntimeError("Exhausted retries")

# ---------------------------------------------------------------------------
# Resume Tailoring
# ---------------------------------------------------------------------------

TAILOR_DRAFT_SYSTEM = """You are an expert resume writer and ATS optimization specialist.
Given a candidate's master profile and a target job description, produce a tailored resume in structured JSON.

Rules:
- Reorder and rewrite experience bullets to foreground skills the JD demands.
- Rewrite the professional summary to target this specific role.
- Highlight certifications/education relevant to the role.
- Use strong action verbs and quantified results where possible.
- Keep it to ONE page worth of content (roughly 400-500 words max).
- Do NOT fabricate experience or skills the candidate doesn't have.

Return ONLY valid JSON with this schema:
{
  "summary": "...",
  "experience": [
    {
      "title": "...",
      "company": "...",
      "dates": "...",
      "bullets": ["...", "..."]
    }
  ],
  "skills": ["...", "..."],
  "education": [
    {
      "degree": "...",
      "institution": "...",
      "year": "..."
    }
  ],
  "certifications": ["..."]
}"""

TAILOR_POLISH_SYSTEM = """You are a senior resume editor. You receive a draft tailored resume (JSON)
and the target job description. Your job:
1. Tighten language — every bullet should start with a power verb, be concise, and ATS-friendly.
2. Ensure keywords from the JD appear naturally (not stuffed).
3. Fix any awkward phrasing or redundancy.
4. Keep the JSON schema exactly the same — return ONLY the polished JSON, no commentary."""


async def tailor_resume(
    profile: dict,
    job_data: dict,
    user_id: str,
) -> dict:
    """
    Two-pass resume tailoring.
    Returns: {"tailored_resume": {...}, "job_id": str, "created_at": str}
    """
    job_title = job_data.get("title", "Unknown Role")
    company = job_data.get("company", "Unknown Company")
    jd_text = job_data.get("description", "")
    job_id = job_data.get("job_id", "unknown")

    profile_text = _build_profile_text(profile)

    user_prompt = (
        f"## Target Job\nTitle: {job_title}\nCompany: {company}\n\n"
        f"## Job Description\n{jd_text}\n\n"
        f"## Candidate Profile\n{profile_text}"
    )

    # --- Pass 1: Haiku draft ---
    logger.info("Tailoring resume [Haiku draft] for job %s", job_id)
    draft_json_str = await _ai_call_with_retry(
        model=HAIKU_MODEL,
        max_tokens=2000,
        system=TAILOR_DRAFT_SYSTEM,
        user_content=user_prompt,
    )
    draft_json_str = _strip_json_fences(draft_json_str)
    draft = json.loads(draft_json_str)

    # --- Pass 2: Sonnet polish ---
    logger.info("Tailoring resume [Sonnet polish] for job %s", job_id)
    polish_prompt = (
        f"## Target Job Description\n{jd_text}\n\n"
        f"## Draft Resume JSON\n{json.dumps(draft, indent=2)}"
    )
    polished_str = await _ai_call_with_retry(
        model=SONNET_MODEL,
        max_tokens=2000,
        system=TAILOR_POLISH_SYSTEM,
        user_content=polish_prompt,
    )
    polished_str = _strip_json_fences(polished_str)
    tailored = json.loads(polished_str)

    result = {
        "tailored_resume": tailored,
        "job_id": job_id,
        "job_title": job_title,
        "company": company,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache in Redis for PDF generation
    redis = await get_redis()
    if redis:
        cache_key = f"tailored:{user_id}:{job_id}"
        await redis.setex(cache_key, 3600, json.dumps(result))

    return result


# ---------------------------------------------------------------------------
# Cover Letter Generation
# ---------------------------------------------------------------------------

COVER_DRAFT_SYSTEM = """You are an expert cover letter writer.
Given a candidate profile and target job, write a compelling cover letter.

Structure (3-4 paragraphs):
1. Opening — specific reason for interest in THIS company and role (not generic).
2. Body — 1-2 paragraphs mapping candidate's top qualifications to JD requirements with concrete examples.
3. Closing — enthusiasm, availability, call-to-action.

Rules:
- Professional but personable tone. Not stiff.
- Reference specific company details if available.
- Under 350 words.
- Do NOT fabricate experience.

Return ONLY valid JSON:
{
  "greeting": "Dear Hiring Manager,",
  "paragraphs": ["...", "...", "..."],
  "closing": "Sincerely,",
  "candidate_name": "..."
}"""

COVER_POLISH_SYSTEM = """You are a senior editor polishing a cover letter draft (JSON).
Improve flow, eliminate clichés, strengthen transitions, ensure professional tone.
Keep under 350 words total. Return ONLY the polished JSON with the same schema."""


async def generate_cover_letter(
    profile: dict,
    job_data: dict,
    user_id: str,
) -> dict:
    """
    Two-pass cover letter generation.
    Returns: {"cover_letter": {...}, "job_id": str, "created_at": str}
    """
    job_title = job_data.get("title", "Unknown Role")
    company = job_data.get("company", "Unknown Company")
    jd_text = job_data.get("description", "")
    job_id = job_data.get("job_id", "unknown")

    profile_text = _build_profile_text(profile)

    user_prompt = (
        f"## Target Job\nTitle: {job_title}\nCompany: {company}\n\n"
        f"## Job Description\n{jd_text}\n\n"
        f"## Candidate Profile\n{profile_text}"
    )

    # --- Pass 1: Haiku draft ---
    logger.info("Cover letter [Haiku draft] for job %s", job_id)
    draft_str = await _ai_call_with_retry(
        model=HAIKU_MODEL,
        max_tokens=1500,
        system=COVER_DRAFT_SYSTEM,
        user_content=user_prompt,
    )
    draft_str = _strip_json_fences(draft_str)
    draft = json.loads(draft_str)

    # --- Pass 2: Sonnet polish ---
    logger.info("Cover letter [Sonnet polish] for job %s", job_id)
    polish_prompt = (
        f"## Target Job Description\n{jd_text}\n\n"
        f"## Draft Cover Letter JSON\n{json.dumps(draft, indent=2)}"
    )
    polished_str = await _ai_call_with_retry(
        model=SONNET_MODEL,
        max_tokens=1500,
        system=COVER_POLISH_SYSTEM,
        user_content=polish_prompt,
    )
    polished_str = _strip_json_fences(polished_str)
    cover_letter = json.loads(polished_str)

    result = {
        "cover_letter": cover_letter,
        "job_id": job_id,
        "job_title": job_title,
        "company": company,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache
    redis = await get_redis()
    if redis:
        cache_key = f"cover:{user_id}:{job_id}"
        await redis.setex(cache_key, 3600, json.dumps(result))

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_profile_text(profile: dict) -> str:
    """Flatten master_profile dict into readable text for the AI prompt."""
    sections = []

    if name := profile.get("name"):
        sections.append(f"Name: {name}")
    if email := profile.get("email"):
        sections.append(f"Email: {email}")
    if phone := profile.get("phone"):
        sections.append(f"Phone: {phone}")
    if loc := profile.get("location"):
        sections.append(f"Location: {loc}")

    if summary := profile.get("summary"):
        sections.append(f"\nProfessional Summary:\n{summary}")

    if skills := profile.get("skills"):
        if isinstance(skills, list):
            sections.append(f"\nSkills: {', '.join(skills)}")
        else:
            sections.append(f"\nSkills: {skills}")

    if experience := profile.get("experience"):
        sections.append("\nExperience:")
        for exp in experience:
            if isinstance(exp, dict):
                title = exp.get("title", "")
                comp = exp.get("company", "")
                dates = exp.get("dates", "")
                sections.append(f"  {title} at {comp} ({dates})")
                for bullet in exp.get("bullets", []):
                    sections.append(f"    - {bullet}")
            else:
                sections.append(f"  {exp}")

    if education := profile.get("education"):
        sections.append("\nEducation:")
        for edu in education:
            if isinstance(edu, dict):
                sections.append(
                    f"  {edu.get('degree', '')} — {edu.get('institution', '')} ({edu.get('year', '')})"
                )
            else:
                sections.append(f"  {edu}")

    if certs := profile.get("certifications"):
        sections.append(f"\nCertifications: {', '.join(certs) if isinstance(certs, list) else certs}")

    if roles := profile.get("target_roles"):
        sections.append(
            f"\nTarget Roles: {', '.join(roles) if isinstance(roles, list) else roles}"
        )

    return "\n".join(sections)


def _strip_json_fences(text: str) -> str:
    """Extract JSON from AI response, handling fences and trailing text."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.index("\n")
        text = text[first_nl + 1:]
    if "```" in text:
        text = text[:text.index("```")]
    text = text.strip()

    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


# ---------------------------------------------------------------------------
# Interview Prep
# ---------------------------------------------------------------------------

INTERVIEW_PREP_SYSTEM = """You are an expert interview coach and hiring manager.
Given a candidate profile and target job description, generate likely interview questions
the candidate will face, along with suggested answer strategies.

Generate 3 categories:
1. Technical questions (specific to the tech stack and role)
2. Behavioral questions (STAR method scenarios relevant to their experience)
3. Company/role-fit questions (why this company, career goals, culture)

For each question, provide:
- The question itself
- Why it's likely to be asked (what the interviewer is looking for)
- A suggested answer strategy using the candidate's actual experience

Return ONLY valid JSON:
{
  "technical": [
    {"question": "...", "why_asked": "...", "answer_strategy": "..."}
  ],
  "behavioral": [
    {"question": "...", "why_asked": "...", "answer_strategy": "..."}
  ],
  "company_fit": [
    {"question": "...", "why_asked": "...", "answer_strategy": "..."}
  ]
}

Generate 3-4 questions per category. Use the candidate's ACTUAL experience for answer strategies.
Do NOT fabricate achievements."""


async def generate_interview_prep(
    profile: dict,
    job_data: dict,
    user_id: str,
) -> dict:
    """
    Single-pass interview prep using Haiku (fast + cheap).
    Returns structured interview questions with answer strategies.
    """
    job_title = job_data.get("title", "Unknown Role")
    company = job_data.get("company", "Unknown Company")
    jd_text = job_data.get("description", "")
    job_id = job_data.get("job_id", "unknown")

    profile_text = _build_profile_text(profile)

    user_prompt = (
        f"## Target Job\nTitle: {job_title}\nCompany: {company}\n\n"
        f"## Job Description\n{jd_text}\n\n"
        f"## Candidate Profile\n{profile_text}"
    )

    logger.info("Interview prep [Haiku] for job %s", job_id)
    raw = await _ai_call_with_retry(
        model=HAIKU_MODEL,
        max_tokens=3000,
        system=INTERVIEW_PREP_SYSTEM,
        user_content=user_prompt,
    )
    raw = _strip_json_fences(raw)
    prep = json.loads(raw)

    result = {
        "interview_prep": prep,
        "job_id": job_id,
        "job_title": job_title,
        "company": company,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache
    redis = await get_redis()
    if redis:
        cache_key = f"interview:{user_id}:{job_id}"
        await redis.set(cache_key, json.dumps(result), ex=3600)

    return result
