-- GhostProof Sprint 3 Migration
-- Run this in Supabase SQL Editor AFTER the Sprint 2 schema

-- ════════════════════════════════════════════════════════════════════
-- Resumes — stores uploaded resume files and extracted text
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    filename TEXT NOT NULL DEFAULT '',
    file_url TEXT DEFAULT '',           -- Supabase Storage URL
    file_size INTEGER DEFAULT 0,
    mime_type TEXT DEFAULT '',
    extracted_text TEXT DEFAULT '',      -- Full text extracted from PDF/DOCX
    is_primary BOOLEAN DEFAULT FALSE,   -- User's active resume
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_resumes_user ON resumes(user_id);

-- Only one primary resume per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_resumes_primary
    ON resumes(user_id) WHERE is_primary = TRUE;

-- ════════════════════════════════════════════════════════════════════
-- Master Profiles — structured profile extracted from resume by AI
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS master_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    resume_id UUID REFERENCES resumes(id) ON DELETE SET NULL,

    -- Structured fields
    full_name TEXT DEFAULT '',
    headline TEXT DEFAULT '',           -- e.g. "Senior Data Engineer | AWS | Spark"
    summary TEXT DEFAULT '',            -- Professional summary paragraph
    location TEXT DEFAULT '',

    -- Skills (stored as JSONB arrays for flexible querying)
    technical_skills JSONB DEFAULT '[]',    -- ["Python", "Spark", "AWS", "Kafka"]
    soft_skills JSONB DEFAULT '[]',         -- ["Leadership", "Communication"]
    certifications JSONB DEFAULT '[]',      -- ["AWS Solutions Architect", "CKA"]

    -- Experience (array of objects)
    experience JSONB DEFAULT '[]',
    -- Each: { "title": "", "company": "", "start": "", "end": "", "bullets": [], "skills_used": [] }

    -- Education (array of objects)
    education JSONB DEFAULT '[]',
    -- Each: { "degree": "", "institution": "", "year": "", "gpa": "" }

    -- Preferences
    target_roles JSONB DEFAULT '[]',        -- ["Data Engineer", "ML Engineer"]
    target_locations JSONB DEFAULT '[]',    -- ["Bengaluru", "Remote"]
    min_salary INTEGER DEFAULT 0,
    preferred_company_size TEXT DEFAULT '',  -- "startup" | "mid" | "enterprise" | "any"
    remote_preference TEXT DEFAULT 'any',   -- "remote" | "hybrid" | "onsite" | "any"

    -- Metadata
    completeness_score INTEGER DEFAULT 0,   -- 0-100, how complete the profile is
    last_parsed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(user_id)
);

-- ════════════════════════════════════════════════════════════════════
-- Job Matches — Haiku-scored match between profile and job
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS job_matches (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,                -- LinkedIn job ID
    analysis_id BIGINT REFERENCES job_analyses(id) ON DELETE SET NULL,

    -- Match scoring
    match_score INTEGER DEFAULT 0,      -- 0-100 overall fit
    skill_match INTEGER DEFAULT 0,      -- 0-100 skills overlap
    experience_match INTEGER DEFAULT 0, -- 0-100 experience fit
    location_match INTEGER DEFAULT 0,   -- 0-100 location fit
    culture_match INTEGER DEFAULT 0,    -- 0-100 company culture fit

    -- AI reasoning
    match_reasoning TEXT DEFAULT '',
    strengths JSONB DEFAULT '[]',       -- ["Strong Spark experience", "AWS certified"]
    gaps JSONB DEFAULT '[]',            -- ["Missing Kubernetes", "Needs 2 more years"]
    recommendations TEXT DEFAULT '',    -- What to highlight in resume/cover letter

    -- Combined with ghost score for final recommendation
    ghost_score INTEGER DEFAULT 0,
    final_recommendation TEXT DEFAULT 'neutral',  -- "strong_apply" | "apply" | "neutral" | "skip" | "avoid"

    matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_matches_user ON job_matches(user_id);
CREATE INDEX IF NOT EXISTS idx_job_matches_score ON job_matches(match_score DESC);
CREATE INDEX IF NOT EXISTS idx_job_matches_rec ON job_matches(final_recommendation);

-- ════════════════════════════════════════════════════════════════════
-- RLS Policies for new tables
-- ════════════════════════════════════════════════════════════════════
ALTER TABLE resumes ENABLE ROW LEVEL SECURITY;
ALTER TABLE master_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_matches ENABLE ROW LEVEL SECURITY;

CREATE POLICY resumes_select ON resumes
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY master_profiles_select ON master_profiles
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY job_matches_select ON job_matches
    FOR SELECT USING (auth.uid() = user_id);

-- ════════════════════════════════════════════════════════════════════
-- Supabase Storage bucket for resume files
-- ════════════════════════════════════════════════════════════════════
-- Run this separately or via Supabase Dashboard → Storage:
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('resumes', 'resumes', false);
--
-- CREATE POLICY resume_upload ON storage.objects
--   FOR INSERT WITH CHECK (
--     bucket_id = 'resumes' AND auth.uid()::text = (storage.foldername(name))[1]
--   );
--
-- CREATE POLICY resume_read ON storage.objects
--   FOR SELECT USING (
--     bucket_id = 'resumes' AND auth.uid()::text = (storage.foldername(name))[1]
--   );
