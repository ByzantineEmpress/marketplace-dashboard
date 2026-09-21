"""Main FastAPI application — entry point.

Wire together routers, middleware, startup/shutdown logic,
and configure security headers for OWASP compliance.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import config
from src.database import init_db
from src.api.routes import api_router, page_router

# ---------- Templates & static ----------
templates = Jinja2Templates(directory="templates")
# static/ folder for CSS/JS/images
os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
os.makedirs("static/img", exist_ok=True)

# ---------- Application factory ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks once when the app starts.

    * Initialise the database tables.
    * Discover and load plugins from the plugins/ directory.
    """
    # 1. Create tables
    init_db()

    # 2. Discover & load plugins (imported here to avoid a circular import:
    #    src/__init__.py imports this module, which would then import plugins)
    from src.plugins import discover_and_load_plugins
    loaded = discover_and_load_plugins(app)
    for name in loaded:
        print(f"[plugin] Loaded: {name}")

    yield  # application runs while we're in the "with" block

    # Shutdown cleanup can go here if needed.


app = FastAPI(
    title=config.APP_NAME,
    lifespan=lifespan,
    docs_url="/api/docs",       # Swagger UI (auto-generated OpenAPI)
    redoc_url="/api/redoc",     # Alternative docs
)

# ---------- Middleware ----------

# CORS — only allow the origins we configure (OWASP A05)
allowed_origins = config.ALLOWED_ORIGINS.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZIP — compress responses for smaller payloads (performance)
app.add_middleware(GZipMiddleware, minimum_size=500)

# ---------- Custom security headers (OWASP A04/A07) ----------
# These headers protect against common browser-side attacks.
@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains; preload" if config.REQUIRE_HTTPS else "max-age=0"
    )
    # 'self' only: all page scripts live in /static/js/* (no inline scripts),
    # so we don't need 'unsafe-inline'. Google Fonts origins are allowed
    # because base.html loads the Inter font from there.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https://fonts.gstatic.com"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

# ---------- Mount static files & routers ----------
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(api_router, prefix="/api")
app.include_router(page_router)


# ---------- Helper to serve the main UI ----------
@app.get("/")
async def index(request: Request):
    """Landing redirect: dashboard if authenticated, login page otherwise.

    Done server-side instead of with an inline <script>: the auth cookie is
    HttpOnly (invisible to document.cookie) and the CSP blocks inline scripts.
    """
    from src.api.routes import require_auth
    target = "/dashboard" if require_auth(request) else "/login"
    return RedirectResponse(url=target, status_code=302)
