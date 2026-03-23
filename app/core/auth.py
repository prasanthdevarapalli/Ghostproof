"""Authentication: Supabase JWT verification + dev API key fallback."""

from __future__ import annotations
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import get_supabase_service

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


class AuthUser(BaseModel):
    user_id: str
    email: str
    tier: str = "free"  # "free" | "pro"
    trial_remaining: int = 10


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthUser:
    """
    Authenticate via:
    1. Supabase JWT (Authorization: Bearer <jwt>)
    2. Dev API key (Authorization: Bearer <dev_api_key>) — local testing only
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    token = credentials.credentials

    # ── Dev API key shortcut ────────────────────────────────────────
    if settings.dev_api_key and token == settings.dev_api_key:
        return AuthUser(
            user_id="dev-user-001",
            email="dev@ghostproof.local",
            tier="pro",
            trial_remaining=999,
        )

    # ── Supabase JWT verification ───────────────────────────────────
    if not settings.supabase_url:
        raise HTTPException(status_code=503, detail="Auth service not configured")

    try:
        # Supabase JWTs are signed with the project's JWT secret.
        # We fetch the JWKS from Supabase to verify.
        jwks_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"

        # For simplicity, decode with the anon key as audience check.
        # In production you'd cache the JWKS and verify properly.
        payload = jwt.decode(
            token,
            settings.supabase_key,  # Supabase uses anon key as JWT secret
            algorithms=["HS256"],
            audience="authenticated",
        )
        user_id = payload.get("sub")
        email = payload.get("email", "")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: no subject")

    except JWTError as e:
        logger.warning("JWT verification failed: %s", e)
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # ── Fetch user profile from DB ──────────────────────────────────
    try:
        sb = get_supabase_service()
        result = (
            sb.table("profiles")
            .select("tier, trial_remaining")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        if result.data:
            tier = result.data.get("tier", "free")
            trial_remaining = result.data.get("trial_remaining", 10)
        else:
            tier = "free"
            trial_remaining = 10
    except Exception as e:
        logger.warning("Profile fetch failed, using defaults: %s", e)
        tier = "free"
        trial_remaining = 10

    return AuthUser(
        user_id=user_id,
        email=email,
        tier=tier,
        trial_remaining=trial_remaining,
    )


async def require_quota(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """Dependency that also checks the user still has analysis quota."""
    if user.tier == "free" and user.trial_remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail="Free trial exhausted. Upgrade to Pro for unlimited analyses.",
        )
    return user
