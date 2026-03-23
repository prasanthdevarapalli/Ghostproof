-- GhostProof Database Schema for Supabase
-- Run this in the Supabase SQL Editor

-- ════════════════════════════════════════════════════════════════════
-- Extensions
-- ════════════════════════════════════════════════════════════════════
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy text matching for company names

-- ════════════════════════════════════════════════════════════════════
-- Profiles (extends Supabase Auth)
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro')),
    trial_remaining INTEGER NOT NULL DEFAULT 10,
    stripe_customer_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO profiles (id, email, tier, trial_remaining)
    VALUES (NEW.id, NEW.email, 'free', 10)
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- ════════════════════════════════════════════════════════════════════
-- Job Analyses
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS job_analyses (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    company TEXT DEFAULT '',
    location TEXT DEFAULT '',
    url TEXT DEFAULT '',
    jd_text TEXT DEFAULT '',
    local_score INTEGER DEFAULT 0,
    server_score INTEGER DEFAULT 0,
    combined_score INTEGER DEFAULT 0,
    risk_level TEXT DEFAULT 'safe' CHECK (risk_level IN ('safe', 'caution', 'ghost')),
    nlp_vagueness REAL DEFAULT 0,
    nlp_reasoning TEXT DEFAULT '',
    is_repost BOOLEAN DEFAULT FALSE,
    has_layoffs BOOLEAN DEFAULT FALSE,
    signals_json JSONB DEFAULT '[]',
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_analyses_user ON job_analyses(user_id);
CREATE INDEX IF NOT EXISTS idx_job_analyses_job ON job_analyses(job_id);
CREATE INDEX IF NOT EXISTS idx_job_analyses_risk ON job_analyses(risk_level);

-- ════════════════════════════════════════════════════════════════════
-- JD Hashes (for repost detection)
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS jd_hashes (
    id BIGSERIAL PRIMARY KEY,
    jd_hash TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    company TEXT DEFAULT '',
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(jd_hash, job_id)
);

CREATE INDEX IF NOT EXISTS idx_jd_hashes_hash ON jd_hashes(jd_hash);

-- ════════════════════════════════════════════════════════════════════
-- Layoff Events (seeded from layoffs.fyi)
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS layoff_events (
    id BIGSERIAL PRIMARY KEY,
    company TEXT NOT NULL,
    layoff_date DATE NOT NULL,
    size TEXT DEFAULT '',  -- e.g. "1200", "10%", "unknown"
    source TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_layoff_company ON layoff_events USING gin (company gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_layoff_date ON layoff_events(layoff_date);

-- ════════════════════════════════════════════════════════════════════
-- RPC: Atomic trial decrement
-- ════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION decrement_trial(p_user_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE profiles
    SET trial_remaining = GREATEST(trial_remaining - 1, 0),
        updated_at = now()
    WHERE id = p_user_id AND tier = 'free' AND trial_remaining > 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ════════════════════════════════════════════════════════════════════
-- Row Level Security
-- ════════════════════════════════════════════════════════════════════
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_analyses ENABLE ROW LEVEL SECURITY;

-- Profiles: users can read their own profile
CREATE POLICY profiles_select ON profiles
    FOR SELECT USING (auth.uid() = id);

CREATE POLICY profiles_update ON profiles
    FOR UPDATE USING (auth.uid() = id);

-- Job analyses: users can read their own analyses
CREATE POLICY job_analyses_select ON job_analyses
    FOR SELECT USING (auth.uid() = user_id);

-- Service role can do everything (used by backend)
-- No policy needed — service key bypasses RLS

-- ════════════════════════════════════════════════════════════════════
-- Seed Data: Recent Layoff Events (2025-2026)
-- ════════════════════════════════════════════════════════════════════
INSERT INTO layoff_events (company, layoff_date, size, source, notes) VALUES
    ('Meta', '2025-01-15', '3600', 'layoffs.fyi', 'Lowest performers across all orgs'),
    ('Microsoft', '2025-01-22', '1900', 'layoffs.fyi', 'Gaming division restructuring'),
    ('Google', '2025-01-30', '1000+', 'layoffs.fyi', 'Multiple divisions affected'),
    ('Amazon', '2025-02-05', '1500', 'layoffs.fyi', 'AWS and Twitch cuts'),
    ('Salesforce', '2025-01-28', '700', 'layoffs.fyi', 'Post-acquisition consolidation'),
    ('SAP', '2025-01-25', '8000', 'layoffs.fyi', 'AI-driven restructuring'),
    ('Intel', '2025-08-01', '15000', 'layoffs.fyi', 'Major cost reduction plan'),
    ('Dell', '2025-02-10', '6000', 'layoffs.fyi', 'AI pivot restructuring'),
    ('Cisco', '2025-02-14', '4000', 'layoffs.fyi', 'Shift to AI and security'),
    ('PayPal', '2025-01-30', '2500', 'layoffs.fyi', 'Efficiency measures'),
    ('Snap', '2025-02-05', '500', 'layoffs.fyi', 'Cost optimization'),
    ('Unity', '2025-01-08', '1800', 'layoffs.fyi', 'Reset plan'),
    ('Riot Games', '2025-01-22', '530', 'layoffs.fyi', 'Studio restructuring'),
    ('eBay', '2025-01-24', '1000', 'layoffs.fyi', 'Workforce reduction'),
    ('Block', '2025-01-10', '1000', 'layoffs.fyi', 'Square/Cash App reorg'),
    ('Spotify', '2025-06-15', '200', 'layoffs.fyi', 'Podcast division cuts'),
    ('TikTok', '2025-03-01', '1000', 'layoffs.fyi', 'Content moderation automation'),
    ('Tesla', '2025-04-15', '2700', 'layoffs.fyi', 'Charging and public policy teams'),
    ('Apple', '2025-03-28', '600', 'layoffs.fyi', 'Special projects group'),
    ('Stripe', '2025-07-01', '300', 'layoffs.fyi', 'Operational efficiency')
ON CONFLICT DO NOTHING;
