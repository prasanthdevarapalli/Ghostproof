-- Sprint 4: Generated Documents table
-- Tracks all AI-generated resumes and cover letters per user.
-- Run this in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS generated_docs (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    doc_type      TEXT NOT NULL CHECK (doc_type IN ('resume', 'cover')),
    storage_path  TEXT NOT NULL,
    job_title     TEXT DEFAULT '',
    company       TEXT DEFAULT '',
    created_at    TIMESTAMPTZ DEFAULT now(),

    -- One doc per type per job per user (upsert on re-generation)
    UNIQUE (user_id, job_id, doc_type)
);

-- Index for listing a user's documents (most recent first)
CREATE INDEX IF NOT EXISTS idx_generated_docs_user_created
    ON generated_docs (user_id, created_at DESC);

-- RLS: users can only see their own documents
ALTER TABLE generated_docs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own documents"
    ON generated_docs
    FOR SELECT
    USING (auth.uid()::text = user_id);

CREATE POLICY "Users insert own documents"
    ON generated_docs
    FOR INSERT
    WITH CHECK (auth.uid()::text = user_id);

CREATE POLICY "Users update own documents"
    ON generated_docs
    FOR UPDATE
    USING (auth.uid()::text = user_id);

-- Dev user bypass: allow service role full access (for dev-user-001 etc.)
-- The backend uses the service role key, so RLS is bypassed by default.
-- These policies only matter for direct client-side access.

-- Create storage bucket for generated PDFs (run once)
-- NOTE: Do this via Supabase Dashboard > Storage or via the API.
-- Bucket name: generated-docs
-- Public: false
-- File size limit: 5MB
