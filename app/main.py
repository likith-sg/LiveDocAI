"""
LiveDocAI — Main Application Entry Point
"""

import asyncio
import logging
import os
import httpx
import random
import jwt
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy import text

# --- Local Imports ---
from app.config import get_settings
from app.database import create_tables, AsyncSessionLocal
from app.middleware.traffic_capture import TrafficCaptureMiddleware
from app.routers import auth, dashboard, docs_router
from app.routers.logs import router as logs_router
from app.routers.endpoints import router as endpoints_router
from app.routers.github import router as github_router
from app.services.background_tasks import start_background_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)
settings = get_settings()

# ─────────────────────────────────────────────────────────────
# 🤖 REALISTIC AUTO-TRAFFIC SIMULATOR (WITH UUID FIX)
# ─────────────────────────────────────────────────────────────
async def simulate_demo_traffic():
    """Fires requests explicitly attached to your user token so they appear in your dashboard."""
    await asyncio.sleep(6) # Wait for server and DB to fully boot
    port = os.environ.get("PORT", "8000")
    
    # 127.0.0.1 is the safest loopback for Railway containers
    base_url = f"http://127.0.0.1:{port}" 
    
    logger.info(f"[Auto-Demo] Preparing to fire requests to {base_url}...")
    
    # 1. Grab the first registered user to attribute traffic to their dashboard
    headers = {}
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("SELECT id, email FROM users LIMIT 1"))
            user = result.fetchone()
            if user:
                # CRITICAL FIX: Cast user.id to string. Neon DB returns UUID objects which crash JWT encoder!
                token = jwt.encode({"sub": str(user.id), "email": user.email}, settings.secret_key, algorithm="HS256")
                headers["Authorization"] = f"Bearer {token}"
                logger.info(f"[Auto-Demo] Attaching traffic to user: {user.email}")
            else:
                logger.warning("[Auto-Demo] No users found! Please create an account in the UI first.")
                return # Abort if no user exists yet
    except Exception as e:
        logger.error(f"[Auto-Demo] Error fetching user for auth: {e}")
        return

    # 2. Fire the traffic with the Authorization header
    async with httpx.AsyncClient(base_url=base_url, headers=headers) as client:
        try:
            # GET Requests (Successful Reads)
            for _ in range(15):
                await client.get(f"/api/v1/products?category=software&limit=20")
                await asyncio.sleep(0.1)
                
            # POST Requests (Successful Creates)
            for i in range(5):
                await client.post(
                    "/api/v1/users", 
                    json={"name": f"Demo User {i}", "email": f"user{i}@acmecorp.com", "role": "customer"}
                )
                await asyncio.sleep(0.1)

            # PUT Requests (Successful Updates)
            for i in range(3):
                await client.put(
                    "/api/v1/users/usr_1001", 
                    json={"name": "Updated Name", "role": "admin"}
                )
                await asyncio.sleep(0.1)

            # DELETE Requests (Successful Deletes)
            await client.delete("/api/v1/users/usr_9999")
            await asyncio.sleep(0.1)

            # Error Requests (404 and 422 to populate the error charts)
            await client.get("/api/v1/products/prod_not_found") # 404 Not Found
            await client.post("/api/v1/users", json={"name": "Bad User"}) # 422 Validation Error
            
            logger.info("[Auto-Demo] Successfully generated all request types! Check Traffic Logs.")
        except Exception as e:
            logger.error(f"[Auto-Demo] Failed to simulate traffic: {e}")

# ─────────────────────────────────────────────────────────────
# APPLICATION LIFESPAN
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    await create_tables()
    logger.info("Database ready ✓")

    # Start normal background tasks (syncing endpoints)
    bg_task = asyncio.create_task(start_background_tasks())
    logger.info("Background tasks started ✓")
    
    # Start the automated demo traffic generator
    demo_task = asyncio.create_task(simulate_demo_traffic())

    yield

    bg_task.cancel()
    demo_task.cancel()
    logger.info(f"{settings.app_name} shut down cleanly.")

# Initialize FastAPI
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────────────────────
# CORS CONFIGURATION
# ─────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = set(settings.get_cors_origins() or [])

# FORCE include frontend dev origins
ALLOWED_ORIGINS.update([
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "https://live-doc-ai.vercel.app"
])

logger.info(f"CORS allowed origins: {ALLOWED_ORIGINS}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# MIDDLEWARE (YOUR TRAFFIC CAPTURE)
# ─────────────────────────────────────────────────────────────
# This intercepts the simulated traffic above and logs it
app.add_middleware(TrafficCaptureMiddleware)

# ─────────────────────────────────────────────────────────────
# ROUTERS (ALL YOUR NORMAL APP FUNCTIONALITY)
# ─────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(logs_router)
app.include_router(endpoints_router)
app.include_router(dashboard.router)
app.include_router(docs_router.router)
app.include_router(github_router)

# ─────────────────────────────────────────────────────────────
# 🚀 DEMO APP ENDPOINTS (TO CATCH THE SIMULATED TRAFFIC)
# ─────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    name: str
    email: str
    role: str = "customer"

class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None

@app.get("/api/v1/products")
async def list_products(category: str = "all", limit: int = 20):
    """Fetch a paginated list of products."""
    return {"category": category, "limit": limit, "items": [{"id": "prod_1", "name": "API Gateway Plugin"}]}

@app.get("/api/v1/products/{product_id}")
async def get_product(product_id: str):
    """Fetch a specific product."""
    if product_id == "prod_not_found":
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": product_id, "name": "API Gateway Plugin", "price": 99.00}

@app.post("/api/v1/users", status_code=201)
async def create_user(user: UserCreate):
    """Register a new user in the system."""
    return {"status": "success", "user_id": "usr_1001", "data": user.model_dump()}

@app.put("/api/v1/users/{user_id}")
async def update_user(user_id: str, user: UserUpdate):
    """Update an existing user's profile."""
    return {"status": "updated", "user_id": user_id, "updates": user.model_dump(exclude_unset=True)}

@app.delete("/api/v1/users/{user_id}", status_code=204)
async def delete_user(user_id: str):
    """Delete a user from the system."""
    return None

# ─────────────────────────────────────────────────────────────
# HEALTH ROUTES (ORIGINAL)
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

