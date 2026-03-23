"""
Job Matching Service — Score how well a job posting matches the user's profile.

Uses Haiku 4.5 to compare the master profile against a job description,
returning a multi-dimensional match score with reasoning.
"""

from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

from app.core.config import settings
from app.core.database import get_supabase_service, get_redis
from app.services.resume_service import get_master_profile

logger = logging.getLogger(__name__)


# ── Match Scoring Prompt ────────────────────────────────────────────

MATCH_PROMPT = """You are an expert career advisor and job matching system. Compare the candidate's profile against the job posting and score the fit.

CANDIDATE PROFILE:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{jd_text}

Score each dimension from 0-100 and provide your reasoning. Return ONLY valid JSON:
{{
  "match_score": <0-100 overall fit>,
  "skill_match": <0-100 how well candidate's skills match requirements>,
  "experience_match": <0-100 years + domain experience fit>,
  "location_match": <0-100 location compatibility>,
  "culture_match": <0-100 company culture/size fit based on candidate history>,
  "match_reasoning": "<2-3 sentence overall assessment>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "gaps": ["<gap 1>", "<gap 2>"],
  "recommendations": "<What to emphasize in resume/cover letter for this specific role>",
  "final_recommendation": "<one of: strong_apply, apply, neutral, skip, avoid>"
}}

Scoring guide:
- strong_apply (80-100): Excellent fit, candidate should prioritize this
- apply (60-79): Good fit with minor gaps, worth applying
- neutral (40-59): Moderate fit, apply if interested in the company
- skip (20-39): Weak fit, significant gaps
- avoid (0-19): Poor fit or likely ghost job, don't waste time"""


async def score_job_match(
    user_id: str,
    job_id: str,
    title: str,
    company: str,
    location: str,
    jd_text: str,
    ghost_score: int = 0,
) -> Optional[dict]:
    """
    Score how well a job matches the user's profile.
    Returns match result dict or None if profile not found.
    """
    # Fetch master profile
    profile = await get_master_profile(user_id)
    if not profile:
        return None

    if not settings.anthropic_api_key or not jd_text.strip():
        return None

    # Check cache
    cache = await get_redis()
    cache_key = f"match:{user_id}:{job_id}"
    if cache:
        try:
            cached = await cache.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    # Build profile summary for the prompt
    profile_summary = _build_profile_summary(profile)

    # Truncate JD
    jd_truncated = jd_text[:4000] if len(jd_text) > 4000 else jd_text

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        prompt = MATCH_PROMPT.format(
            profile=profile_summary,
            title=title,
            company=company,
            location=location,
            jd_text=jd_truncated,
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)

        # Add ghost score context
        result["ghost_score"] = ghost_score

        # Adjust recommendation if ghost score is high
        if ghost_score > 60:
            result["final_recommendation"] = "avoid"
            result["match_reasoning"] += " WARNING: High ghost job probability — this listing may not be genuine."
        elif ghost_score > 40:
            orig = result.get("final_recommendation", "neutral")
            # Downgrade one level
            downgrade = {
                "strong_apply": "apply",
                "apply": "neutral",
                "neutral": "skip",
                "skip": "avoid",
            }
            result["final_recommendation"] = downgrade.get(orig, orig)

        # Persist to DB
        await _persist_match(user_id, job_id, result, ghost_score)

        # Cache for 2 hours
        if cache:
            try:
                await cache.set(cache_key, json.dumps(result), ex=7200)
            except Exception:
                pass

        return result

    except json.JSONDecodeError as e:
        logger.error("Match response not valid JSON: %s", e)
        return None
    except Exception as e:
        logger.error("Job matching failed: %s", e)
        return None


def _build_profile_summary(profile: dict) -> str:
    """Build a concise text summary of the profile for the AI prompt."""
    parts = []

    if profile.get("full_name"):
        parts.append(f"Name: {profile['full_name']}")
    if profile.get("headline"):
        parts.append(f"Headline: {profile['headline']}")
    if profile.get("summary"):
        parts.append(f"Summary: {profile['summary']}")
    if profile.get("location"):
        parts.append(f"Location: {profile['location']}")

    skills = profile.get("technical_skills", [])
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (json.JSONDecodeError, TypeError):
            skills = []
    if skills:
        parts.append(f"Technical Skills: {', '.join(skills)}")

    certs = profile.get("certifications", [])
    if isinstance(certs, str):
        try:
            certs = json.loads(certs)
        except (json.JSONDecodeError, TypeError):
            certs = []
    if certs:
        parts.append(f"Certifications: {', '.join(certs)}")

    experience = profile.get("experience", [])
    if isinstance(experience, str):
        try:
            experience = json.loads(experience)
        except (json.JSONDecodeError, TypeError):
            experience = []
    if experience:
        exp_lines = []
        for exp in experience[:5]:  # Top 5 most recent
            line = f"- {exp.get('title', '?')} at {exp.get('company', '?')} ({exp.get('start', '?')} - {exp.get('end', '?')})"
            if exp.get("skills_used"):
                line += f" [Skills: {', '.join(exp['skills_used'][:5])}]"
            exp_lines.append(line)
        parts.append(f"Experience:\n" + "\n".join(exp_lines))

    education = profile.get("education", [])
    if isinstance(education, str):
        try:
            education = json.loads(education)
        except (json.JSONDecodeError, TypeError):
            education = []
    if education:
        edu_lines = [
            f"- {e.get('degree', '?')} from {e.get('institution', '?')} ({e.get('year', '?')})"
            for e in education[:3]
        ]
        parts.append(f"Education:\n" + "\n".join(edu_lines))

    targets = profile.get("target_roles", [])
    if isinstance(targets, str):
        try:
            targets = json.loads(targets)
        except (json.JSONDecodeError, TypeError):
            targets = []
    if targets:
        parts.append(f"Target Roles: {', '.join(targets)}")

    return "\n".join(parts)


async def _persist_match(user_id: str, job_id: str, result: dict, ghost_score: int):
    """Save match result to database."""
    if not settings.supabase_url:
        return

    try:
        sb = get_supabase_service()

        # Find the job_analyses entry to link
        analysis_result = (
            sb.table("job_analyses")
            .select("id")
            .eq("user_id", user_id)
            .eq("job_id", job_id)
            .maybe_single()
            .execute()
        )
        analysis_id = analysis_result.data["id"] if analysis_result.data else None

        sb.table("job_matches").upsert(
            {
                "user_id": user_id,
                "job_id": job_id,
                "analysis_id": analysis_id,
                "match_score": result.get("match_score", 0),
                "skill_match": result.get("skill_match", 0),
                "experience_match": result.get("experience_match", 0),
                "location_match": result.get("location_match", 0),
                "culture_match": result.get("culture_match", 0),
                "match_reasoning": result.get("match_reasoning", ""),
                "strengths": json.dumps(result.get("strengths", [])),
                "gaps": json.dumps(result.get("gaps", [])),
                "recommendations": result.get("recommendations", ""),
                "ghost_score": ghost_score,
                "final_recommendation": result.get("final_recommendation", "neutral"),
                "matched_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,job_id",
        ).execute()
    except Exception as e:
        logger.error("Match persist failed: %s", e)


# ── Batch Job Ranking ───────────────────────────────────────────────

async def rank_recent_jobs(user_id: str, limit: int = 20) -> list[dict]:
    """
    Fetch user's recent analyzed jobs that haven't been matched yet,
    score them all, and return ranked by match_score.
    """
    if not settings.supabase_url:
        return []

    profile = await get_master_profile(user_id)
    if not profile:
        return []

    try:
        sb = get_supabase_service()

        # Get recent analyzed jobs
        jobs_result = (
            sb.table("job_analyses")
            .select("job_id, title, company, location, jd_text, combined_score")
            .eq("user_id", user_id)
            .order("analyzed_at", desc=True)
            .limit(limit)
            .execute()
        )

        jobs = jobs_result.data or []
        if not jobs:
            return []

        # Check which ones already have matches
        job_ids = [j["job_id"] for j in jobs]
        existing_result = (
            sb.table("job_matches")
            .select("job_id")
            .eq("user_id", user_id)
            .in_("job_id", job_ids)
            .execute()
        )
        existing_ids = {r["job_id"] for r in (existing_result.data or [])}

        # Score unmatched jobs
        results = []
        for job in jobs:
            if job["job_id"] in existing_ids:
                # Fetch existing match
                match_result = (
                    sb.table("job_matches")
                    .select("*")
                    .eq("user_id", user_id)
                    .eq("job_id", job["job_id"])
                    .maybe_single()
                    .execute()
                )
                if match_result.data:
                    results.append({
                        "job_id": job["job_id"],
                        "title": job["title"],
                        "company": job["company"],
                        **match_result.data,
                    })
                continue

            # Score new match
            match = await score_job_match(
                user_id=user_id,
                job_id=job["job_id"],
                title=job["title"],
                company=job["company"],
                location=job.get("location", ""),
                jd_text=job.get("jd_text", ""),
                ghost_score=job.get("combined_score", 0),
            )
            if match:
                results.append({
                    "job_id": job["job_id"],
                    "title": job["title"],
                    "company": job["company"],
                    **match,
                })

        # Sort by match score descending
        results.sort(key=lambda r: r.get("match_score", 0), reverse=True)
        return results

    except Exception as e:
        logger.error("Batch ranking failed: %s", e)
        return []
