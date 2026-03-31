"""
Ghost Analysis Service — the brain of GhostProof's backend.

Three server-side signals:
  7. NLP vagueness scoring (Haiku 4.5)
  8. JD repost detection (SHA-256 hash matching)
  9. Company layoff cross-check (layoff_events table)

Combined scoring merges local (extension) + server signals, capped at 100.
"""

from __future__ import annotations
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

from app.core.config import settings
from app.core.database import get_supabase_service, get_redis
from app.models.schemas import (
    AnalyzeJobRequest,
    GhostAnalysis,
    GhostSignal,
    NLPResult,
    RepostResult,
    LayoffResult,
)

logger = logging.getLogger(__name__)

# ── Anthropic client ────────────────────────────────────────────────
_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


# ── NLP Vagueness Scoring (Signal 7) ───────────────────────────────

NLP_SYSTEM_PROMPT = """You are a job posting analyst. Your task is to evaluate whether a job description is vague, generic, or likely a "ghost job" (posted with no real intent to hire).

Score the following dimensions from 0.0 (very specific/clear) to 1.0 (very vague/generic):

1. role_clarity: Is the actual role well-defined? What will the person DO day-to-day?
2. requirements_specificity: Are requirements concrete (e.g. "5 years Python, AWS certified") or generic ("strong communicator, team player")?
3. team_context: Does it mention the team, manager, projects, or org structure?
4. compensation_signals: Any mention of salary, equity, benefits, or total comp?
5. action_language: Does it use specific action verbs about deliverables, or vague language about "driving impact"?
6. copy_paste_indicators: Does it look like a template? Generic phrases like "fast-paced environment", "competitive salary", "equal opportunity" without specifics?

Return ONLY valid JSON with no markdown, no code fences:
{
  "vagueness_score": <float 0.0-1.0 overall>,
  "role_clarity": <float>,
  "requirements_specificity": <float>,
  "team_context": <float>,
  "compensation_signals": <float>,
  "action_language": <float>,
  "copy_paste_indicators": <float>,
  "reasoning": "<1-2 sentence summary>"
}"""


async def analyze_nlp_vagueness(jd_text: str) -> NLPResult:
    """Call Haiku 4.5 to score JD vagueness."""
    if not settings.anthropic_api_key or not jd_text.strip():
        return NLPResult()

    # Truncate very long JDs to save tokens
    truncated = jd_text[:4000] if len(jd_text) > 4000 else jd_text

    try:
        client = _get_anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=NLP_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyze this job description:\n\n{truncated}",
                }
            ],
        )
        text = response.content[0].text.strip()
        # Strip any accidental code fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        return NLPResult(
            vagueness_score=float(data.get("vagueness_score", 0.0)),
            reasoning=data.get("reasoning", ""),
            role_clarity=float(data.get("role_clarity", 0.0)),
            requirements_specificity=float(
                data.get("requirements_specificity", 0.0)
            ),
            team_context=float(data.get("team_context", 0.0)),
            compensation_signals=float(data.get("compensation_signals", 0.0)),
            action_language=float(data.get("action_language", 0.0)),
            copy_paste_indicators=float(data.get("copy_paste_indicators", 0.0)),
        )
    except json.JSONDecodeError as e:
        logger.error("NLP response not valid JSON: %s", e)
        return NLPResult(reasoning="NLP parse error")
    except Exception as e:
        logger.error("NLP analysis failed: %s", e)
        return NLPResult(reasoning=f"NLP error: {type(e).__name__}")


# ── JD Repost Detection (Signal 8) ─────────────────────────────────

def _normalize_jd(text: str) -> str:
    """Normalize JD text for consistent hashing."""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)  # collapse whitespace
    text = re.sub(r"[^\w\s]", "", text)  # remove punctuation
    return text


def _hash_jd(text: str) -> str:
    """SHA-256 hash of normalized JD."""
    return hashlib.sha256(_normalize_jd(text).encode("utf-8")).hexdigest()


async def check_repost(job_id: str, jd_text: str, title: str, company: str) -> RepostResult:
    """Check if this JD has been seen before under a different listing."""
    if not jd_text.strip() or not settings.supabase_url:
        return RepostResult()

    jd_hash = _hash_jd(jd_text)

    try:
        sb = get_supabase_service()

        # Look for existing entries with same hash but different job_id
        result = (
            sb.table("jd_hashes")
            .select("job_id, title, company, first_seen")
            .eq("jd_hash", jd_hash)
            .execute()
        )

        existing = result.data or []
        other_posts = [e for e in existing if e["job_id"] != job_id]

        # Upsert current entry
        sb.table("jd_hashes").upsert(
            {
                "jd_hash": jd_hash,
                "job_id": job_id,
                "title": title,
                "company": company,
                "first_seen": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="jd_hash,job_id",
        ).execute()

        if other_posts:
            return RepostResult(
                is_repost=True,
                repost_count=len(other_posts),
                first_seen=other_posts[0].get("first_seen"),
                different_titles=[p.get("title", "") for p in other_posts[:5]],
            )

        return RepostResult()
    except Exception as e:
        logger.error("Repost check failed: %s", e)
        return RepostResult()


# ── Layoff Cross-Check (Signal 9) ──────────────────────────────────

async def check_layoffs(company: str) -> LayoffResult:
    """Check if company has recent layoffs (within 90 days)."""
    if not company.strip() or not settings.supabase_url:
        return LayoffResult()

    try:
        sb = get_supabase_service()

        # Fuzzy match company name (pg_trgm similarity)
        # Fallback to ilike if pg_trgm isn't available
        company_clean = company.strip().lower()
        company_words = company_clean.split()
        primary_name = company_words[0] if company_words else company_clean

        result = (
            sb.table("layoff_events")
            .select("company, layoff_date, size, source")
            .ilike("company", f"%{primary_name}%")
            .gte(
                "layoff_date",
                (
                    datetime.now(timezone.utc)
                    .replace(hour=0, minute=0, second=0)
                    .isoformat()
                ).replace(
                    datetime.now(timezone.utc).strftime("%Y"),
                    str(int(datetime.now(timezone.utc).strftime("%Y")))
                )  # Get events from last 90 days
            )
            .order("layoff_date", desc=True)
            .limit(1)
            .execute()
        )

        if result.data:
            event = result.data[0]
            return LayoffResult(
                has_recent_layoffs=True,
                layoff_date=event.get("layoff_date"),
                layoff_size=event.get("size"),
                source=event.get("source", ""),
            )

        return LayoffResult()
    except Exception as e:
        logger.error("Layoff check failed: %s", e)
        return LayoffResult()


# ── Signal Scoring ──────────────────────────────────────────────────

def _score_nlp(nlp: NLPResult) -> Optional[GhostSignal]:
    """Signal 7: NLP vagueness → points."""
    if nlp.vagueness_score >= 0.7:
        return GhostSignal(
            name="NLP Vagueness (High)",
            points=20,
            reason=nlp.reasoning or f"Vagueness score: {nlp.vagueness_score:.2f}",
        )
    elif nlp.vagueness_score >= 0.5:
        return GhostSignal(
            name="NLP Vagueness (Moderate)",
            points=10,
            reason=nlp.reasoning or f"Vagueness score: {nlp.vagueness_score:.2f}",
        )
    return None


def _score_repost(repost: RepostResult) -> Optional[GhostSignal]:
    """Signal 8: Repost detection → points."""
    if not repost.is_repost:
        return None
    if repost.repost_count >= 3:
        return GhostSignal(
            name="Frequent Repost",
            points=15,
            reason=f"Same JD posted {repost.repost_count + 1} times under different listings",
        )
    return GhostSignal(
        name="JD Repost Detected",
        points=10,
        reason=f"Same JD found in {repost.repost_count} other listing(s)",
    )


def _score_layoff(layoff: LayoffResult) -> Optional[GhostSignal]:
    """Signal 9: Layoff cross-check → points."""
    if not layoff.has_recent_layoffs:
        return None
    size = layoff.layoff_size or "unknown"
    # Big layoffs get more points
    try:
        num = int(re.sub(r"[^\d]", "", size))
        if num >= 1000:
            return GhostSignal(
                name="Major Layoffs While Hiring",
                points=20,
                reason=f"Company laid off ~{size} employees on {layoff.layoff_date}",
            )
    except (ValueError, TypeError):
        pass
    return GhostSignal(
        name="Recent Layoffs While Hiring",
        points=10,
        reason=f"Company had layoffs ({size}) on {layoff.layoff_date}",
    )


def _classify(score: int) -> str:
    """Risk level classification."""
    if score <= 30:
        return "safe"
    elif score <= 60:
        return "caution"
    return "ghost"


# ── Main Analysis Orchestrator ──────────────────────────────────────

async def analyze_job(request: AnalyzeJobRequest) -> GhostAnalysis:
    """
    Run all 3 server signals in parallel, combine with local score.
    Returns a full GhostAnalysis with combined scoring.
    """
    import asyncio

    # Check cache first
    cache = await get_redis()
    cache_key = f"analysis:{request.job_id}"

    if cache:
        try:
            cached = await cache.get(cache_key)
            if cached:
                logger.info("Cache hit for job %s", request.job_id)
                data = json.loads(cached)
                return GhostAnalysis(**data)
        except Exception as e:
            logger.warning("Cache read error: %s", e)

    # Run all three server signals concurrently
    nlp_task = analyze_nlp_vagueness(request.jd_text)
    repost_task = check_repost(
        request.job_id, request.jd_text, request.title, request.company
    )
    layoff_task = check_layoffs(request.company)

    nlp, repost, layoff = await asyncio.gather(
        nlp_task, repost_task, layoff_task, return_exceptions=True
    )

    # Handle exceptions from gather
    if isinstance(nlp, Exception):
        logger.error("NLP task failed: %s", nlp)
        nlp = NLPResult(reasoning="Analysis error")
    if isinstance(repost, Exception):
        logger.error("Repost task failed: %s", repost)
        repost = RepostResult()
    if isinstance(layoff, Exception):
        logger.error("Layoff task failed: %s", layoff)
        layoff = LayoffResult()

    # Compute server signals
    signals: list[GhostSignal] = []
    for scorer, result in [
        (_score_nlp, nlp),
        (_score_repost, repost),
        (_score_layoff, layoff),
    ]:
        signal = scorer(result)
        if signal:
            signals.append(signal)

    server_score = min(sum(s.points for s in signals), 60)
    local_score = min(request.local_score, 60)
    combined = min(local_score + server_score, 100)

    analysis = GhostAnalysis(
        server_score=server_score,
        server_signals=signals,
        combined_score=combined,
        risk_level=_classify(combined),
        nlp=nlp,
        repost=repost,
        layoff=layoff,
    )

    # Cache result for 1 hour
    if cache:
        try:
            await cache.set(cache_key, analysis.model_dump_json(), ex=3600)
        except Exception as e:
            logger.warning("Cache write error: %s", e)

    return analysis


# ── DB Persistence ──────────────────────────────────────────────────

async def persist_analysis(
    user_id: str, request: AnalyzeJobRequest, analysis: GhostAnalysis
):
    """Save analysis to Supabase for history/stats. Skips dev users (no UUID)."""
    if not settings.supabase_url:
        return

    # Dev users don't have real UUIDs in the profiles table
    if user_id.startswith("dev-"):
        # Cache in Redis instead
        cache = await get_redis()
        if cache:
            try:
                key = f"analysis_history:{user_id}:{request.job_id}"
                entry = {
                    "job_id": request.job_id,
                    "title": request.title,
                    "company": request.company,
                    "combined_score": analysis.combined_score,
                    "risk_level": analysis.risk_level,
                    "analyzed_at": datetime.now(timezone.utc).isoformat(),
                }
                await cache.set(key, json.dumps(entry), ex=86400)
            except Exception:
                pass
        return

    try:
        sb = get_supabase_service()
        sb.table("job_analyses").upsert(
            {
                "user_id": user_id,
                "job_id": request.job_id,
                "title": request.title,
                "company": request.company,
                "location": request.location,
                "url": request.url,
                "jd_text": request.jd_text[:5000],  # cap storage
                "local_score": request.local_score,
                "server_score": analysis.server_score,
                "combined_score": analysis.combined_score,
                "risk_level": analysis.risk_level,
                "nlp_vagueness": analysis.nlp.vagueness_score if analysis.nlp else 0,
                "nlp_reasoning": analysis.nlp.reasoning if analysis.nlp else "",
                "is_repost": analysis.repost.is_repost if analysis.repost else False,
                "has_layoffs": analysis.layoff.has_recent_layoffs if analysis.layoff else False,
                "signals_json": json.dumps(
                    [s.model_dump() for s in analysis.server_signals]
                ),
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,job_id",
        ).execute()
    except Exception as e:
        logger.error("Persist analysis failed: %s", e)


async def decrement_trial(user_id: str):
    """Decrement free trial counter. Skips dev users."""
    if not settings.supabase_url or user_id.startswith("dev-"):
        return
    try:
        sb = get_supabase_service()
        # Use RPC for atomic decrement
        sb.rpc(
            "decrement_trial",
            {"p_user_id": user_id},
        ).execute()
    except Exception as e:
        logger.error("Trial decrement failed: %s", e)
