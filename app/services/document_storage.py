"""
Sprint 4 — Document Storage Service
Persists generated PDFs to Supabase Storage for long-term access.
Redis cache is the fast path (1hr TTL); Supabase Storage is the durable path.
"""

import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.core.database import get_supabase_service

logger = logging.getLogger(__name__)

BUCKET_NAME = "generated-docs"


def _ensure_bucket():
    """Create the storage bucket if it doesn't exist (best effort)."""
    if not settings.supabase_url:
        return
    try:
        sb = get_supabase_service()
        sb.storage.get_bucket(BUCKET_NAME)
    except Exception:
        try:
            sb = get_supabase_service()
            sb.storage.create_bucket(
                BUCKET_NAME,
                options={"public": False, "file_size_limit": 5 * 1024 * 1024},
            )
            logger.info("Created storage bucket: %s", BUCKET_NAME)
        except Exception as e:
            logger.warning("Could not create bucket (may already exist): %s", e)


async def store_pdf(
    user_id: str,
    job_id: str,
    doc_type: str,
    pdf_bytes: bytes,
    metadata: dict | None = None,
) -> str | None:
    """Upload PDF to Supabase Storage and record in generated_docs table."""
    if not settings.supabase_url:
        return None

    _ensure_bucket()
    sb = get_supabase_service()
    storage_path = f"{user_id}/{doc_type}_{job_id}.pdf"

    try:
        sb.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
        logger.info("Stored PDF: %s/%s", BUCKET_NAME, storage_path)
    except Exception as e:
        logger.error("Failed to upload PDF to storage: %s", e)
        return None

    try:
        sb.table("generated_docs").upsert(
            {
                "user_id": user_id,
                "job_id": job_id,
                "doc_type": doc_type,
                "storage_path": storage_path,
                "job_title": (metadata or {}).get("job_title", ""),
                "company": (metadata or {}).get("company", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,job_id,doc_type",
        ).execute()
    except Exception as e:
        logger.warning("Failed to record doc in DB: %s", e)

    return storage_path


async def get_stored_pdf(user_id: str, job_id: str, doc_type: str) -> bytes | None:
    """Retrieve PDF from Supabase Storage (fallback when Redis expires)."""
    if not settings.supabase_url:
        return None

    storage_path = f"{user_id}/{doc_type}_{job_id}.pdf"
    try:
        sb = get_supabase_service()
        data = sb.storage.from_(BUCKET_NAME).download(storage_path)
        return data
    except Exception as e:
        logger.debug("PDF not in storage: %s — %s", storage_path, e)
        return None


async def list_user_documents(user_id: str) -> list[dict]:
    """List all generated documents for a user."""
    if not settings.supabase_url:
        return []

    try:
        sb = get_supabase_service()
        resp = (
            sb.table("generated_docs")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        docs = resp.data or []
        for doc in docs:
            doc["download_url"] = f"/api/v1/download/{doc['doc_type']}/{user_id}/{doc['job_id']}"
        return docs
    except Exception as e:
        logger.error("Failed to list documents: %s", e)
        return []
