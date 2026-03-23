"""Pydantic models for API request/response schemas."""

from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


# ── Request ─────────────────────────────────────────────────────────

class AnalyzeJobRequest(BaseModel):
    """Payload sent by the Chrome extension."""
    job_id: str = Field(..., description="LinkedIn job ID")
    title: str = ""
    company: str = ""
    location: str = ""
    posted_date: str = ""  # raw text like "2 weeks ago"
    posted_days: Optional[int] = None  # parsed days
    applicants: Optional[int] = None
    applicant_text: str = ""
    jd_text: str = ""  # Full job description text
    easy_apply: bool = False
    url: str = ""
    local_score: int = 0  # Extension's local ghost score (0-60)
    local_signals: List[dict] = []  # Local signals already computed


# ── Ghost Analysis Sub-models ───────────────────────────────────────

class NLPResult(BaseModel):
    vagueness_score: float = Field(0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    role_clarity: float = 0.0
    requirements_specificity: float = 0.0
    team_context: float = 0.0
    compensation_signals: float = 0.0
    action_language: float = 0.0
    copy_paste_indicators: float = 0.0


class RepostResult(BaseModel):
    is_repost: bool = False
    repost_count: int = 0
    first_seen: Optional[str] = None
    different_titles: List[str] = []


class LayoffResult(BaseModel):
    has_recent_layoffs: bool = False
    layoff_date: Optional[str] = None
    layoff_size: Optional[str] = None
    source: str = ""


class GhostSignal(BaseModel):
    name: str
    points: int
    reason: str
    category: str = "server"  # "local" | "server"


class GhostAnalysis(BaseModel):
    server_score: int = 0  # 0-60
    server_signals: List[GhostSignal] = []
    combined_score: int = 0  # local + server, capped at 100
    risk_level: str = "safe"  # "safe" | "caution" | "ghost"
    nlp: Optional[NLPResult] = None
    repost: Optional[RepostResult] = None
    layoff: Optional[LayoffResult] = None


# ── Response ────────────────────────────────────────────────────────

class AnalyzeJobResponse(BaseModel):
    job_id: str
    analysis: GhostAnalysis
    cached: bool = False
    analyzed_at: str = ""


class UserProfile(BaseModel):
    user_id: str
    email: str
    tier: str
    trial_remaining: int


class JobHistoryItem(BaseModel):
    job_id: str
    title: str
    company: str
    combined_score: int
    risk_level: str
    analyzed_at: str


class StatsResponse(BaseModel):
    total_analyzed: int = 0
    ghosts_found: int = 0
    caution_found: int = 0
    safe_found: int = 0


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.3.0"
    supabase: bool = False
    redis: bool = False
    anthropic: bool = False


# ── Sprint 3: Resume & Profile ──────────────────────────────────────

class ResumeResponse(BaseModel):
    resume_id: Optional[str] = None
    filename: str = ""
    file_size: int = 0
    is_primary: bool = False
    created_at: str = ""


class MasterProfileResponse(BaseModel):
    full_name: str = ""
    headline: str = ""
    summary: str = ""
    location: str = ""
    technical_skills: List[str] = []
    soft_skills: List[str] = []
    certifications: List[str] = []
    experience: List[dict] = []
    education: List[dict] = []
    target_roles: List[str] = []
    target_locations: List[str] = []
    completeness_score: int = 0
    last_parsed_at: Optional[str] = None


class ResumeUploadResponse(BaseModel):
    resume_id: Optional[str] = None
    profile: MasterProfileResponse
    text_length: int = 0
    message: str = ""


class ProfileUpdateRequest(BaseModel):
    """Allow user to manually edit profile fields."""
    headline: Optional[str] = None
    summary: Optional[str] = None
    target_roles: Optional[List[str]] = None
    target_locations: Optional[List[str]] = None
    min_salary: Optional[int] = None
    preferred_company_size: Optional[str] = None
    remote_preference: Optional[str] = None


# ── Sprint 3: Job Matching ──────────────────────────────────────────

class JobMatchResponse(BaseModel):
    job_id: str
    title: str = ""
    company: str = ""
    match_score: int = 0
    skill_match: int = 0
    experience_match: int = 0
    location_match: int = 0
    culture_match: int = 0
    match_reasoning: str = ""
    strengths: List[str] = []
    gaps: List[str] = []
    recommendations: str = ""
    ghost_score: int = 0
    final_recommendation: str = "neutral"


class BatchRankRequest(BaseModel):
    limit: int = 20

