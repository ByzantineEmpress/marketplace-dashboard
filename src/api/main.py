"""Main FastAPI application — entry point.

Wire together routers, middleware, startup/shutdown logic,
and configure security headers for OWASP compliance.
"""

import json
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
os.makedirs("static/uploads", exist_ok=True)

def _startup_warnings() -> list[str]:
    """Return configuration warnings worth surfacing at boot.

    These are the mistakes that are easy to ship and hard to notice: the app
    keeps working, but a security control is silently off. They are logged
    rather than fatal so a local dev run is never blocked.
    """
    problems = []
    base = (config.APP_BASE_URL or "").rstrip("/")

    if base and not base.startswith("https://") and config.REQUIRE_HTTPS:
        problems.append(
            f"APP_BASE_URL is {base!r} but REQUIRE_HTTPS is on — OAuth "
            f"callbacks and secure cookies will not work behind TLS."
        )
    if base.startswith("http://localhost") and config.REQUIRE_HTTPS:
        problems.append(
            "APP_BASE_URL still points at localhost while REQUIRE_HTTPS is on."
        )
    if not config.REQUIRE_HTTPS:
        problems.append(
            "REQUIRE_HTTPS is off: the auth cookie is not marked Secure and "
            "HSTS is disabled. Fine locally, not for a public deployment."
        )
    if config.ADMIN_PASSWORD == "changeme123":
        problems.append(
            "ADMIN_PASSWORD is still the shipped default — change it before "
            "exposing this instance."
        )
    if config.GOOGLE_DEV_MODE:
        problems.append(
            "GOOGLE_DEV_MODE is ON: the local test account picker is enabled "
            "and can mint admin sessions. Turn it off in production."
        )
    if "your-domain.com" in (config.ALLOWED_ORIGINS or ""):
        problems.append(
            f"ALLOWED_ORIGINS still contains the example placeholder: "
            f"{config.ALLOWED_ORIGINS!r}"
        )
    elif config.REQUIRE_HTTPS and "localhost" in (config.ALLOWED_ORIGINS or ""):
        # Easy to miss: APP_BASE_URL is often fixed while this is left behind,
        # which silently blocks the real origin from calling the API.
        problems.append(
            f"ALLOWED_ORIGINS still lists localhost ({config.ALLOWED_ORIGINS!r}) "
            f"while REQUIRE_HTTPS is on — the production origin will be blocked "
            f"by CORS."
        )
    # Marketplace OAuth callback URLs.
    #
    # Etsy reads ETSY_REDIRECT_URI from the environment BEFORE falling back to
    # APP_BASE_URL, so a localhost value left in a deployed .env silently
    # overrides the public URL and Etsy refuses the redirect with "The requested
    # redirect URL is not permitted". Nothing else surfaces it: the error appears
    # on the marketplace's own page, not in our logs.
    for var, platform in (("ETSY_REDIRECT_URI", "Etsy"),):
        value = os.environ.get(var, "")
        if not value:
            continue  # unset is correct: it derives from APP_BASE_URL
        if config.REQUIRE_HTTPS and "localhost" in value:
            problems.append(
                f"{var} points at localhost ({value!r}) while REQUIRE_HTTPS is on. "
                f"{platform} will reject the redirect. Unset it to derive the "
                f"callback from APP_BASE_URL, or set the public URL."
            )
        elif config.APP_BASE_URL and not value.startswith(config.APP_BASE_URL.rstrip("/")):
            problems.append(
                f"{var} ({value!r}) does not start with APP_BASE_URL "
                f"({config.APP_BASE_URL!r}). It takes precedence over "
                f"APP_BASE_URL, and must match a callback registered in your "
                f"{platform} app settings or the redirect is refused."
            )

    # eBay is different: its authorize URL takes the RuName, not a callback URL.
    # Without one, Connect fails with eBay's opaque "temporarily_unavailable",
    # so say plainly that it is missing.
    if not (config.EBAY_RUNAME or os.environ.get("EBAY_RUNAME")):
        if config.EBAY_CLIENT_ID or os.environ.get("EBAY_CLIENT_ID"):
            # Only warn when eBay is actually configured; an instance that has
            # never touched eBay should not be nagged about it.
            problems.append(
                "EBAY_RUNAME is not set. eBay OAuth needs the RuName (the "
                "'redirect URL name' from your eBay app's User Tokens page); "
                "without it Connect fails with eBay's 'temporarily_unavailable'. "
                "Each user can also save their own on the My Accounts page."
            )

    if not config.MAIL_BACKEND:
        problems.append(
            "Outbound email is not configured — email+password signup cannot "
            "complete (Google sign-in still works)."
        )
    if not config.SECRET_KEY or config.SECRET_KEY.startswith("change-this"):
        problems.append(
            "SECRET_KEY is unset or the placeholder, so it is regenerated on "
            "every start."
        )
    return problems


# ---------- Application factory ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks once when the app starts.

    * Initialise the database tables.
    * Discover and load plugins from the plugins/ directory.
    """
    # 1. Create tables
    init_db()

    # 2. Convert any session token still stored verbatim into a digest. Done at
    #    startup rather than waiting for each holder's next request: a backup
    #    taken in the meantime would otherwise still carry live sessions.
    from src import auth_sessions
    from src.database import SessionLocal
    _db = SessionLocal()
    try:
        migrated = auth_sessions.hash_existing(_db)
        if migrated:
            print(f"[security] hashed {migrated} stored session token(s) that "
                  f"predated hashing; no one was logged out.")
    finally:
        _db.close()

    # 3. Discover & load plugins (imported here to avoid a circular import:
    #    src/__init__.py imports this module, which would then import plugins)
    from src.plugins import discover_and_load_plugins
    loaded = discover_and_load_plugins(app)
    for name in loaded:
        print(f"[plugin] Loaded: {name}")

    # 4. Report configuration problems loudly, once, at startup.
    problems = _startup_warnings()
    if problems:
        print("\n" + "=" * 68)
        print("CONFIGURATION WARNINGS")
        for item in problems:
            print(f"  ! {item}")
        print("=" * 68 + "\n")

    # 4. Periodic marketplace syncing. Off unless explicitly enabled, so a test
    #    run or a dev server never starts talking to live marketplaces on its own.
    from src import scheduler
    scheduler.start(app)

    yield  # application runs while we're in the "with" block

    # Shutdown: let a sync pass finish rather than tearing it off mid-request.
    await scheduler.shutdown(app)


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


def _client_scheme(request) -> str:
    """The scheme the *client* used, as far as we can tell behind a proxy.

    The app itself speaks plain HTTP: Cloudflare terminates TLS and forwards
    (over a tunnel, or to loopback) without TLS. So ``request.url.scheme`` here
    is always "http" and says nothing about the client's connection. The
    proxy's own headers are the only signal available.
    """
    # X-Forwarded-Proto may be a comma-separated chain; the first entry is the
    # original client's scheme.
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded:
        return forwarded.lower()

    # CF-Visitor is a JSON object, e.g. {"scheme":"https"} — not a bare value.
    # Parsed defensively: a malformed header must never be mistaken for TLS.
    visitor = request.headers.get("cf-visitor") or ""
    if visitor:
        try:
            scheme = (json.loads(visitor) or {}).get("scheme") or ""
            if scheme:
                return str(scheme).strip().lower()
        except (ValueError, TypeError, AttributeError):
            pass
        # Deliberately no fallback to "https" here: an unparseable header must
        # not be read as a secure connection.

    return (request.url.scheme or "").lower()


# Set once so a persistent plain-HTTP request does not flood the log.
_plain_http_seen = False


def _is_internal(request) -> bool:
    """True for a request that never went through the proxy.

    The container healthcheck curls http://localhost:8000 from inside the
    container, which is plain HTTP by definition. Treating that as suspicious
    would fire the warning below on every single deployment and train the
    operator to ignore it — the opposite of the intent.
    """
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def warn_on_plain_http(request, call_next):
    """Warn (not block) when a request reaches the app without TLS.

    TLS *version* enforcement cannot happen here — Cloudflare terminates TLS
    and uvicorn's ASGI scope carries no TLS information at all, so the app
    never learns which protocol version the client used. Minimum TLS version
    is an edge setting (Cloudflare → SSL/TLS → Edge Certificates → Minimum TLS
    Version).

    What this checks instead is whether a request from *outside* arrived
    without HTTPS. In production that means either a misconfiguration or
    someone hitting the origin directly, bypassing Cloudflare — which is also
    the only way the client-IP header rate limiting relies on can be forged.
    Internal loopback requests (the healthcheck) are excluded.
    """
    global _plain_http_seen
    if (
        config.REQUIRE_HTTPS
        and not _is_internal(request)
        and _client_scheme(request) == "http"
    ):
        if not _plain_http_seen:
            _plain_http_seen = True
            print(
                "\n[security] A request arrived over plain HTTP while "
                "REQUIRE_HTTPS is on.\n"
                "           Either the proxy is not setting X-Forwarded-Proto, "
                "or something is\n"
                "           reaching the origin directly and bypassing "
                "Cloudflare. The origin should\n"
                "           accept traffic only from the tunnel.\n"
            )
    return await call_next(request)


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
