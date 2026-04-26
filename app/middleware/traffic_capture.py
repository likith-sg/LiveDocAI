"""
Traffic Capture Middleware
──────────────────────────
Captures every HTTP request/response and logs it to Neon.

Key changes for user isolation:
- Skips LiveDocAI's own internal routes (no point monitoring ourselves)
- Extracts user_id from JWT Bearer token if present
- Tags each log with the user_id
"""

import json
import time
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# ── Routes to SKIP — LiveDocAI's own internal API ────────────────────────────
SKIP_PREFIXES = (
    "/api/logs",
    "/api/endpoints",
    "/api/dashboard",
    "/api/auth",
    "/api/github",
    "/api/docs",
    "/api/ingest",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
    "/favicon",
)


def _should_skip(path: str) -> bool:
    return any(path.startswith(p) for p in SKIP_PREFIXES)


def _extract_user_id(request: Request) -> str | None:
    """Extract user_id from JWT Bearer token without blocking."""
    try:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth.split(" ", 1)[1]
        import jwt
        from app.config import get_settings
        settings = get_settings()
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload.get("sub")
    except Exception:
        return None


class TrafficCaptureMiddleware(BaseHTTPMiddleware):

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # ✅ FIX: Skip HEAD requests + existing skips
        if _should_skip(path) or request.method in ("OPTIONS", "HEAD"):
            return await call_next(request)

        # Capture request body
        try:
            body_bytes = await request.body()
            request_body = body_bytes.decode("utf-8", errors="replace")[:2000] if body_bytes else None
        except Exception:
            body_bytes = None
            request_body = None

        start_ms = time.time()

        # Process request
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(f"[Middleware] Unhandled error: {exc}")
            raise

        latency_ms = (time.time() - start_ms) * 1000

        # ✅ FIX: Safe response body capture
        response_body = None
        try:
            if hasattr(response, "body_iterator") and response.body_iterator:
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)

                response_body_bytes = b"".join(chunks)
                response_body = response_body_bytes.decode("utf-8", errors="replace")[:2000]

                from starlette.responses import Response as StarResponse
                response = StarResponse(
                    content=response_body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
        except Exception as e:
            logger.debug(f"[Middleware] Response capture failed: {e}")

        # Extract user_id from JWT
        user_id = _extract_user_id(request)

        # Save to DB asynchronously (fire and forget)
        try:
            from app.database import AsyncSessionLocal
            from app.models.api_log import APILog

            async with AsyncSessionLocal() as db:
                log = APILog(
                    method              = request.method,
                    path                = path,
                    query_params        = dict(request.query_params),
                    request_body        = request_body,
                    status_code         = response.status_code,
                    response_body       = response_body,
                    latency_ms          = round(latency_ms, 2),
                    request_size_bytes  = len(body_bytes) if body_bytes else 0,
                    response_size_bytes = len(response_body.encode()) if response_body else 0,
                    client_ip           = request.client.host if request.client else None,
                    user_agent          = request.headers.get("user-agent"),
                    user_id             = user_id,
                )
                db.add(log)
                await db.commit()
        except Exception as exc:
            logger.debug(f"[Middleware] Log save failed: {exc}")

        return response
