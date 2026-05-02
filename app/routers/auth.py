"""
GitHub OAuth Router
────────────────────
GET  /api/auth/github          — redirect user to GitHub OAuth
GET  /api/auth/github/callback — GitHub calls this after user authorizes
GET  /api/auth/github/user     — get current OAuth user info
"""

import hashlib
import secrets
import uuid
import logging
import httpx
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import jwt

from app.database import get_db
from app.config import get_settings

router   = APIRouter(prefix="/api/auth", tags=["Auth"])
logger   = logging.getLogger(__name__)
settings = get_settings()

JWT_ALGORITHM   = "HS256"
JWT_EXPIRY_DAYS = 7

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL     = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL      = "https://api.github.com/user"
GITHUB_SCOPE         = "repo,read:user,user:email"


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_jwt(user_id: str, email: str) -> str:
    payload = {
        "sub":   user_id,
        "email": email,
        "iat":   datetime.utcnow(),
        "exp":   datetime.utcnow() + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)

def generate_api_key() -> str:
    return f"ldai_{secrets.token_hex(32)}"


# ── Existing email/password routes ────────────────────────────────────────────

from pydantic import BaseModel

class SignupRequest(BaseModel):
    name:     str
    org:      str = ""
    email:    str
    password: str

class SigninRequest(BaseModel):
    email:    str
    password: str

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
bearer = HTTPBearer(auto_error=False)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    payload = decode_jwt(credentials.credentials)
    return {"id": payload["sub"], "email": payload["email"]}


@router.post("/signup")
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": body.email.lower().strip()}
    )
    if result.fetchone():
        raise HTTPException(status_code=400, detail="Email already registered.")

    user_id = str(uuid.uuid4())
    api_key = generate_api_key()
    token   = create_jwt(user_id, body.email.lower().strip())

    try:
        await db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key VARCHAR(100)"))
        await db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_token TEXT"))
        await db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username VARCHAR(100)"))
        await db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)"))
        await db.commit()
    except Exception:
        await db.rollback()

    await db.execute(text("""
        INSERT INTO users (id, name, org, email, password, token, api_key)
        VALUES (:id, :name, :org, :email, :password, :token, :api_key)
    """), {
        "id": user_id, "name": body.name.strip(), "org": body.org.strip(),
        "email": body.email.lower().strip(), "password": hash_password(body.password),
        "token": token, "api_key": api_key,
    })
    await db.commit()

    return {
        "token": token, "api_key": api_key,
        "name": body.name.strip(), "org": body.org.strip(),
        "email": body.email.lower().strip(), "user_id": user_id,
    }


@router.post("/signin")
async def signin(body: SigninRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("SELECT id, name, org, email, password, api_key FROM users WHERE email = :email"),
        {"email": body.email.lower().strip()}
    )
    row = result.fetchone()
    if not row or row.password != hash_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token   = create_jwt(row.id, row.email)
    api_key = row.api_key or generate_api_key()

    await db.execute(
        text("UPDATE users SET token = :token, api_key = COALESCE(api_key, :api_key) WHERE id = :id"),
        {"token": token, "api_key": api_key, "id": row.id}
    )
    await db.commit()

    return {
        "token": token, "api_key": api_key,
        "name": row.name, "org": row.org or "",
        "email": row.email, "user_id": row.id,
    }


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("SELECT id, name, org, email, api_key, github_username, avatar_url FROM users WHERE id = :id"),
        {"id": current_user["id"]}
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return {
        "user_id": row.id, "name": row.name, "org": row.org or "",
        "email": row.email, "api_key": row.api_key,
        "github_username": getattr(row, 'github_username', None),
        "avatar_url": getattr(row, 'avatar_url', None),
    }


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@router.get("/github")
async def github_oauth_start():
    """Redirect user to GitHub OAuth authorization page."""
    if not settings.github_client_id:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured.")

    state  = secrets.token_hex(16)
    params = (
        f"client_id={settings.github_client_id}"
        f"&redirect_uri={settings.github_oauth_redirect}"
        f"&scope={GITHUB_SCOPE}"
        f"&state={state}"
    )
    return RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{params}")


@router.get("/github/callback")
async def github_oauth_callback(
    code:  str   = Query(...),
    state: str   = Query(None),
    db:    AsyncSession = Depends(get_db),
):
    """GitHub OAuth callback — exchange code for token, create/update user, redirect to frontend."""

    if not settings.github_client_id or not settings.github_client_secret:
        return RedirectResponse(f"{settings.frontend_url}?auth_error=GitHub+OAuth+not+configured")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:

            # Step 1 — Exchange code for access token
            token_res = await client.post(
                GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id":     settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code":          code,
                    "redirect_uri":  settings.github_oauth_redirect,
                }
            )
            token_data   = token_res.json()
            github_token = token_data.get("access_token")

            if not github_token:
                error = token_data.get("error_description", "Failed to get GitHub token")
                return RedirectResponse(f"{settings.frontend_url}?auth_error={error}")

            # Step 2 — Get GitHub user info
            gh_headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept":        "application/vnd.github+json",
            }
            user_res = await client.get(GITHUB_USER_URL, headers=gh_headers)
            gh_user  = user_res.json()

            # Step 3 — Get primary email
            email = gh_user.get("email")
            if not email:
                email_res = await client.get(
                    "https://api.github.com/user/emails", headers=gh_headers
                )
                if email_res.is_success:
                    emails  = email_res.json()
                    primary = next((e for e in emails if e.get("primary")), None)
                    email   = primary["email"] if primary else None
            if not email:
                email = f"{gh_user.get('login', 'user')}@github.local"

        github_id       = str(gh_user.get("id", uuid.uuid4()))
        github_username = gh_user.get("login", "")
        name            = gh_user.get("name") or github_username or "GitHub User"
        avatar_url      = gh_user.get("avatar_url", "")

        # Step 4 — Ensure all columns exist (run once, ignore if already exist)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id VARCHAR(50)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_token TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username VARCHAR(100)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key VARCHAR(100)",
        ]:
            try:
                await db.execute(text(col_sql))
                await db.commit()
            except Exception:
                await db.rollback()

        # Step 5 — Find user by email first (safer than github_id which may not exist yet)
        user_id = None
        api_key = None

        try:
            result = await db.execute(
                text("SELECT id, api_key FROM users WHERE email = :email"),
                {"email": email}
            )
            row = result.fetchone()
            if row:
                user_id = str(row.id)
                api_key = row.api_key or generate_api_key()
        except Exception:
            await db.rollback()

        if user_id:
            # Update existing user
            try:
                await db.execute(text("""
                    UPDATE users SET
                        github_token    = :gt,
                        github_username = :un,
                        avatar_url      = :av,
                        api_key         = COALESCE(api_key, :ak)
                    WHERE id = :id
                """), {
                    "gt": github_token, "un": github_username,
                    "av": avatar_url, "ak": api_key, "id": user_id,
                })
                await db.commit()
            except Exception:
                await db.rollback()
        else:
            # Create new user
            user_id = str(uuid.uuid4())
            api_key = generate_api_key()
            try:
                await db.execute(text("""
                    INSERT INTO users
                        (id, name, org, email, password, token, api_key,
                         github_token, github_username, avatar_url)
                    VALUES
                        (:id, :name, '', :email, '', '', :api_key,
                         :github_token, :username, :avatar_url)
                """), {
                    "id": user_id, "name": name, "email": email,
                    "api_key": api_key, "github_token": github_token,
                    "username": github_username, "avatar_url": avatar_url,
                })
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"[OAuth] Failed to create user: {e}")
                return RedirectResponse(
                    f"{settings.frontend_url}?auth_error=Failed+to+create+account"
                )

        # Step 6 — Create JWT
        jwt_token = create_jwt(user_id, email)
        try:
            await db.execute(
                text("UPDATE users SET token = :token WHERE id = :id"),
                {"token": jwt_token, "id": user_id}
            )
            await db.commit()
        except Exception:
            await db.rollback()

        # Step 7 — Redirect to frontend with all params
        import urllib.parse
        params = urllib.parse.urlencode({
            "oauth_token":        jwt_token,
            "oauth_name":         name,
            "oauth_email":        email,
            "oauth_api_key":      api_key or "",
            "oauth_user_id":      user_id,
            "oauth_github_token": github_token,
            "oauth_avatar":       avatar_url,
            "oauth_username":     github_username,
        })
        return RedirectResponse(f"{settings.frontend_url}?{params}")

    except Exception as e:
        logger.error(f"[OAuth] Callback error: {e}", exc_info=True)
        return RedirectResponse(
            f"{settings.frontend_url}?auth_error=Authentication+failed.+Please+try+again."
        )
