"""API routes for GhostProof backend."""

from __future__ import annotations
import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response

from app.core.auth import AuthUser, get_current_user, require_quota
from app.core.config import settings
from app.core.database import get_supabase_service, get_redis
from app.models.schemas import (
    AnalyzeJobRequest,
    AnalyzeJobResponse,
    UserProfile,
    JobHistoryItem,
    StatsResponse,
    HealthResponse,
    ResumeUploadResponse,
    MasterProfileResponse,
    ProfileUpdateRequest,
    ResumeResponse,
    JobMatchResponse,
    BatchRankRequest,
    TailorResumeRequest,
    TailorResumeResponse,
    CoverLetterRequest,
    CoverLetterResponse,
)
from app.services.ghost_analysis import analyze_job, persist_analysis, decrement_trial
from app.services.resume_service import process_resume, get_master_profile, get_resumes
from app.services.job_matching import score_job_match, rank_recent_jobs
from app.services.tailoring_service import tailor_resume, generate_cover_letter, generate_interview_prep
from app.services.pdf_service import generate_resume_pdf, generate_cover_letter_pdf
from app.services.document_storage import store_pdf, get_stored_pdf, list_user_documents

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Health ──────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Service health + dependency status."""
    supabase_ok = bool(settings.supabase_url and settings.supabase_key)
    anthropic_ok = bool(settings.anthropic_api_key)

    redis_ok = False
    redis_client = await get_redis()
    if redis_client:
        try:
            await redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    return HealthResponse(
        status="ok",
        version="0.2.0",
        supabase=supabase_ok,
        redis=redis_ok,
        anthropic=anthropic_ok,
    )


# ── Analyze Job ─────────────────────────────────────────────────────

@router.post("/analyze-job", response_model=AnalyzeJobResponse)
async def analyze_job_endpoint(
    request: AnalyzeJobRequest,
    user: AuthUser = Depends(require_quota),
):
    """
    Run server-side ghost analysis on a job posting.
    Requires authentication and available quota.
    """
    if not request.jd_text.strip():
        raise HTTPException(status_code=400, detail="Job description text is required")

    logger.info(
        "Analyzing job %s for user %s (%s)",
        request.job_id, user.user_id, user.tier,
    )

    # Run the analysis
    analysis = await analyze_job(request)

    # Persist to DB (fire-and-forget, don't block response)
    import asyncio
    asyncio.create_task(persist_analysis(user.user_id, request, analysis))

    # Decrement trial for free users
    if user.tier == "free":
        asyncio.create_task(decrement_trial(user.user_id))

    return AnalyzeJobResponse(
        job_id=request.job_id,
        analysis=analysis,
        cached=False,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
    )


# ── User Profile ────────────────────────────────────────────────────

@router.get("/me", response_model=UserProfile)
async def get_profile(user: AuthUser = Depends(get_current_user)):
    """Return authenticated user's profile."""
    return UserProfile(
        user_id=user.user_id,
        email=user.email,
        tier=user.tier,
        trial_remaining=user.trial_remaining,
    )


# ── Job History ─────────────────────────────────────────────────────

@router.get("/jobs", response_model=list[JobHistoryItem])
async def get_job_history(
    limit: int = 50,
    user: AuthUser = Depends(get_current_user),
):
    """Return user's analyzed job history."""
    if not settings.supabase_url:
        return []

    try:
        sb = get_supabase_service()
        result = (
            sb.table("job_analyses")
            .select("job_id, title, company, combined_score, risk_level, analyzed_at")
            .eq("user_id", user.user_id)
            .order("analyzed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [JobHistoryItem(**row) for row in (result.data or [])]
    except Exception as e:
        logger.error("Job history fetch failed: %s", e)
        return []


# ── Stats ───────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def get_stats(user: AuthUser = Depends(get_current_user)):
    """Return user's analysis statistics."""
    if not settings.supabase_url:
        return StatsResponse()

    try:
        sb = get_supabase_service()
        result = (
            sb.table("job_analyses")
            .select("risk_level")
            .eq("user_id", user.user_id)
            .execute()
        )
        rows = result.data or []
        return StatsResponse(
            total_analyzed=len(rows),
            ghosts_found=sum(1 for r in rows if r["risk_level"] == "ghost"),
            caution_found=sum(1 for r in rows if r["risk_level"] == "caution"),
            safe_found=sum(1 for r in rows if r["risk_level"] == "safe"),
        )
    except Exception as e:
        logger.error("Stats fetch failed: %s", e)
        return StatsResponse()


# ════════════════════════════════════════════════════════════════════
# SPRINT 3: Resume & Profile
# ════════════════════════════════════════════════════════════════════

# ── Upload Resume ───────────────────────────────────────────────────

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


@router.post("/resume/upload", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
):
    """
    Upload a resume (PDF, DOCX, or TXT). Extracts text, parses with AI,
    and creates/updates the master profile.
    """
    # Validate file type
    mime = file.content_type or ""
    if mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {mime}. Upload PDF, DOCX, or TXT.",
        )

    # Read and validate size
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Max 10 MB.")

    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    logger.info(
        "Resume upload: %s (%d bytes, %s) from user %s",
        file.filename, len(file_bytes), mime, user.user_id,
    )

    try:
        result = await process_resume(
            user_id=user.user_id,
            filename=file.filename or "resume",
            file_bytes=file_bytes,
            mime_type=mime,
        )

        profile_data = result.get("profile", {})
        return ResumeUploadResponse(
            resume_id=result.get("resume_id"),
            profile=MasterProfileResponse(
                full_name=profile_data.get("full_name", ""),
                headline=profile_data.get("headline", ""),
                summary=profile_data.get("summary", ""),
                location=profile_data.get("location", ""),
                technical_skills=profile_data.get("technical_skills", []),
                soft_skills=profile_data.get("soft_skills", []),
                certifications=profile_data.get("certifications", []),
                experience=profile_data.get("experience", []),
                education=profile_data.get("education", []),
                target_roles=profile_data.get("target_roles", []),
                completeness_score=profile_data.get("completeness_score", 0),
            ),
            text_length=result.get("text_length", 0),
            message="Resume parsed and profile updated successfully",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Resume upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Resume processing failed")


# ── Get Profile ─────────────────────────────────────────────────────

@router.get("/profile", response_model=MasterProfileResponse)
async def get_profile_endpoint(user: AuthUser = Depends(get_current_user)):
    """Return the user's master profile."""
    profile = await get_master_profile(user.user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found. Upload a resume first.")

    return MasterProfileResponse(
        full_name=profile.get("full_name", ""),
        headline=profile.get("headline", ""),
        summary=profile.get("summary", ""),
        location=profile.get("location", ""),
        technical_skills=profile.get("technical_skills", []),
        soft_skills=profile.get("soft_skills", []),
        certifications=profile.get("certifications", []),
        experience=profile.get("experience", []),
        education=profile.get("education", []),
        target_roles=profile.get("target_roles", []),
        target_locations=profile.get("target_locations", []),
        completeness_score=profile.get("completeness_score", 0),
        last_parsed_at=profile.get("last_parsed_at"),
    )


# ── Update Profile ──────────────────────────────────────────────────

@router.patch("/profile")
async def update_profile(
    updates: ProfileUpdateRequest,
    user: AuthUser = Depends(get_current_user),
):
    """Manually update profile fields (preferences, target roles, etc.)."""
    if not settings.supabase_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    profile = await get_master_profile(user.user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found. Upload a resume first.")

    try:
        sb = get_supabase_service()
        update_data = {"updated_at": datetime.now(timezone.utc).isoformat()}

        if updates.headline is not None:
            update_data["headline"] = updates.headline
        if updates.summary is not None:
            update_data["summary"] = updates.summary
        if updates.target_roles is not None:
            update_data["target_roles"] = json.dumps(updates.target_roles)
        if updates.target_locations is not None:
            update_data["target_locations"] = json.dumps(updates.target_locations)
        if updates.min_salary is not None:
            update_data["min_salary"] = updates.min_salary
        if updates.preferred_company_size is not None:
            update_data["preferred_company_size"] = updates.preferred_company_size
        if updates.remote_preference is not None:
            update_data["remote_preference"] = updates.remote_preference

        sb.table("master_profiles").update(update_data).eq("user_id", user.user_id).execute()
        return {"message": "Profile updated", "updated_fields": list(update_data.keys())}
    except Exception as e:
        logger.error("Profile update failed: %s", e)
        raise HTTPException(status_code=500, detail="Profile update failed")


# ── List Resumes ────────────────────────────────────────────────────

@router.get("/resumes", response_model=list[ResumeResponse])
async def list_resumes(user: AuthUser = Depends(get_current_user)):
    """List all uploaded resumes."""
    resumes = await get_resumes(user.user_id)
    return [
        ResumeResponse(
            resume_id=r.get("id"),
            filename=r.get("filename", ""),
            file_size=r.get("file_size", 0),
            is_primary=r.get("is_primary", False),
            created_at=r.get("created_at", ""),
        )
        for r in resumes
    ]


# ════════════════════════════════════════════════════════════════════
# SPRINT 3: Job Matching
# ════════════════════════════════════════════════════════════════════

# ── Match Single Job ────────────────────────────────────────────────

@router.post("/match-job", response_model=JobMatchResponse)
async def match_job_endpoint(
    request: AnalyzeJobRequest,
    user: AuthUser = Depends(require_quota),
):
    """Score how well a job matches the user's profile."""
    result = await score_job_match(
        user_id=user.user_id,
        job_id=request.job_id,
        title=request.title,
        company=request.company,
        location=request.location,
        jd_text=request.jd_text,
        ghost_score=request.local_score,
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No profile found. Upload a resume first to get job matching.",
        )

    return JobMatchResponse(
        job_id=request.job_id,
        title=request.title,
        company=request.company,
        match_score=result.get("match_score", 0),
        skill_match=result.get("skill_match", 0),
        experience_match=result.get("experience_match", 0),
        location_match=result.get("location_match", 0),
        culture_match=result.get("culture_match", 0),
        match_reasoning=result.get("match_reasoning", ""),
        strengths=result.get("strengths", []),
        gaps=result.get("gaps", []),
        recommendations=result.get("recommendations", ""),
        ghost_score=result.get("ghost_score", 0),
        final_recommendation=result.get("final_recommendation", "neutral"),
    )


# ── Batch Rank Jobs ─────────────────────────────────────────────────

@router.post("/rank-jobs", response_model=list[JobMatchResponse])
async def rank_jobs_endpoint(
    request: BatchRankRequest,
    user: AuthUser = Depends(get_current_user),
):
    """
    Rank user's recently analyzed jobs by profile match.
    Scores any unmatched jobs and returns all sorted by fit.
    """
    results = await rank_recent_jobs(user.user_id, limit=request.limit)

    return [
        JobMatchResponse(
            job_id=r.get("job_id", ""),
            title=r.get("title", ""),
            company=r.get("company", ""),
            match_score=r.get("match_score", 0),
            skill_match=r.get("skill_match", 0),
            experience_match=r.get("experience_match", 0),
            location_match=r.get("location_match", 0),
            culture_match=r.get("culture_match", 0),
            match_reasoning=r.get("match_reasoning", ""),
            strengths=r.get("strengths", []),
            gaps=r.get("gaps", []),
            recommendations=r.get("recommendations", ""),
            ghost_score=r.get("ghost_score", 0),
            final_recommendation=r.get("final_recommendation", "neutral"),
        )
        for r in results
    ]


# ════════════════════════════════════════════════════════════════════
# SPRINT 4: Resume Tailoring + Cover Letter + PDF
# ════════════════════════════════════════════════════════════════════

# ── Tailor Resume ──────────────────────────────────────────────────

@router.post("/tailor-resume", response_model=TailorResumeResponse)
async def tailor_resume_endpoint(
    req: TailorResumeRequest,
    user: AuthUser = Depends(require_quota),
):
    """Generate an AI-tailored resume for a specific job posting."""
    profile = await get_master_profile(user.user_id)
    if not profile:
        raise HTTPException(status_code=400, detail="No master profile found. Upload a resume first.")

    job_data = {
        "job_id": req.job_id,
        "title": req.title,
        "company": req.company,
        "description": req.description,
        "location": req.location,
    }

    try:
        result = await tailor_resume(profile, job_data, user.user_id)
    except json.JSONDecodeError as e:
        logger.error("AI returned invalid JSON during tailoring: %s", e)
        raise HTTPException(status_code=502, detail="AI response parsing failed. Try again.")
    except Exception as e:
        logger.error("Resume tailoring failed: %s", e)
        raise HTTPException(status_code=500, detail="Resume tailoring failed.")

    try:
        pdf_bytes = generate_resume_pdf(result["tailored_resume"], profile)
    except Exception as e:
        logger.error("PDF generation failed: %s", e)
        raise HTTPException(status_code=500, detail="PDF generation failed.")

    # Cache in Redis (fast path)
    redis = await get_redis()
    if redis:
        pdf_key = f"pdf:resume:{user.user_id}:{req.job_id}"
        await redis.set(pdf_key, base64.b64encode(pdf_bytes).decode(), ex=3600)

    # Persist to Supabase Storage (durable path)
    await store_pdf(user.user_id, req.job_id, "resume", pdf_bytes,
                    {"job_title": req.title, "company": req.company})

    pdf_url = f"/api/v1/download/resume/{user.user_id}/{req.job_id}"

    return TailorResumeResponse(
        tailored_resume=result["tailored_resume"],
        job_id=result["job_id"],
        job_title=result["job_title"],
        company=result["company"],
        pdf_url=pdf_url,
        created_at=result["created_at"],
    )


# ── Cover Letter ───────────────────────────────────────────────────

@router.post("/cover-letter", response_model=CoverLetterResponse)
async def cover_letter_endpoint(
    req: CoverLetterRequest,
    user: AuthUser = Depends(require_quota),
):
    """Generate an AI cover letter for a specific job posting."""
    profile = await get_master_profile(user.user_id)
    if not profile:
        raise HTTPException(status_code=400, detail="No master profile found. Upload a resume first.")

    job_data = {
        "job_id": req.job_id,
        "title": req.title,
        "company": req.company,
        "description": req.description,
        "location": req.location,
    }

    try:
        result = await generate_cover_letter(profile, job_data, user.user_id)
    except json.JSONDecodeError as e:
        logger.error("AI returned invalid JSON for cover letter: %s", e)
        raise HTTPException(status_code=502, detail="AI response parsing failed. Try again.")
    except Exception as e:
        logger.error("Cover letter generation failed: %s", e)
        raise HTTPException(status_code=500, detail="Cover letter generation failed.")

    try:
        pdf_bytes = generate_cover_letter_pdf(result["cover_letter"], profile, job_data)
    except Exception as e:
        logger.error("Cover letter PDF failed: %s", e)
        raise HTTPException(status_code=500, detail="PDF generation failed.")

    redis = await get_redis()
    if redis:
        pdf_key = f"pdf:cover:{user.user_id}:{req.job_id}"
        await redis.set(pdf_key, base64.b64encode(pdf_bytes).decode(), ex=3600)

    await store_pdf(user.user_id, req.job_id, "cover", pdf_bytes,
                    {"job_title": req.title, "company": req.company})

    pdf_url = f"/api/v1/download/cover/{user.user_id}/{req.job_id}"

    return CoverLetterResponse(
        cover_letter=result["cover_letter"],
        job_id=result["job_id"],
        job_title=result["job_title"],
        company=result["company"],
        pdf_url=pdf_url,
        created_at=result["created_at"],
    )


# ── PDF Download ───────────────────────────────────────────────────

@router.get("/download/{doc_type}/{user_id}/{job_id}")
async def download_pdf(
    doc_type: str,
    user_id: str,
    job_id: str,
    user: AuthUser = Depends(get_current_user),
):
    """Download a generated PDF. Tries Redis cache first, falls back to Supabase Storage."""
    if doc_type not in ("resume", "cover"):
        raise HTTPException(status_code=400, detail="doc_type must be 'resume' or 'cover'")
    if user.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    pdf_bytes = None

    # Try Redis first
    redis = await get_redis()
    if redis:
        cached = await redis.get(f"pdf:{doc_type}:{user_id}:{job_id}")
        if cached:
            pdf_bytes = base64.b64decode(cached)

    # Fallback to Supabase Storage
    if pdf_bytes is None:
        pdf_bytes = await get_stored_pdf(user_id, job_id, doc_type)

    if pdf_bytes is None:
        raise HTTPException(status_code=404, detail="PDF not found or expired. Generate it again.")

    filename = f"ghostproof_{doc_type}_{job_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Document List ──────────────────────────────────────────────────

@router.get("/documents")
async def list_documents_endpoint(user: AuthUser = Depends(get_current_user)):
    """List all generated resumes and cover letters for the current user."""
    docs = await list_user_documents(user.user_id)
    return {"documents": docs, "count": len(docs)}


# ── Interview Prep ─────────────────────────────────────────────────

@router.post("/interview-prep")
async def interview_prep_endpoint(
    req: TailorResumeRequest,
    user: AuthUser = Depends(require_quota),
):
    """Generate likely interview questions with answer strategies."""
    profile = await get_master_profile(user.user_id)
    if not profile:
        raise HTTPException(status_code=400, detail="No master profile found. Upload a resume first.")

    job_data = {
        "job_id": req.job_id,
        "title": req.title,
        "company": req.company,
        "description": req.description,
        "location": req.location,
    }

    try:
        result = await generate_interview_prep(profile, job_data, user.user_id)
    except json.JSONDecodeError as e:
        logger.error("AI returned invalid JSON for interview prep: %s", e)
        raise HTTPException(status_code=502, detail="AI response parsing failed. Try again.")
    except Exception as e:
        logger.error("Interview prep failed: %s", e)
        raise HTTPException(status_code=500, detail="Interview prep generation failed.")

    return result
