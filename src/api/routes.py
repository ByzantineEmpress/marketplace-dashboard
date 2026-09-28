"""API and page routes.

Organised into two routers:
- ``api_router`` — JSON endpoints for the front-end to consume.
- ``page_router`` — HTML page renders for the UI.

Every endpoint validates input (prevents injection) and returns
structured JSON so the client can handle errors gracefully.
"""

from datetime import datetime, timedelta
import secrets
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.auth_passwords import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_password_strength,
    verify_password,
)
from src.email_verification import (
    MAX_VERIFICATION_SENDS,
    VERIFICATION_TTL_MINUTES,
    new_token,
    token_fingerprint,
)
from src import mailer, storage
from src.database import SessionLocal, new_invite_code
from src.models import (
    AuthSession,
    Listing,
    PendingSignup,
    RateLimitEvent,
    Team,
    TeamJoinRequest,
    TeamMembership,
    User,
)
from src.config import config, persist_env

from jinja2.ext import Extension
from jinja2 import nodes

class CsrfTokenExtension(Extension):
    """Jinja2 extension providing Django-compatible {% csrf_token %} tag support."""
    tags = {"csrf_token"}

    def parse(self, parser):
        lineno = next(parser.stream).lineno
        call = self.call_method("_render_csrf", [nodes.ContextReference()], lineno=lineno)
        return nodes.Output([nodes.MarkSafe(call)]).set_lineno(lineno)

    def _render_csrf(self, context):
        token = context.get("csrf_token", "")
        return f'<input type="hidden" name="csrf_token" value="{token}">'

# Templates
templates = Jinja2Templates(directory="templates")
templates.env.add_extension(CsrfTokenExtension)

# ------------------------------------------------------------------ #
#  Database helper — every route that needs a DB gets one.
# ------------------------------------------------------------------ #

def get_db():
    """Yield a single DB session for the duration of a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ------------------------------------------------------------------ #
#  AUTH helper — session-based auth, backed by the database.        #
# ------------------------------------------------------------------ #

def get_current_user(request: Request):
    """Resolve the auth cookie to the signed-in user (or None).

    Sessions live in the ``auth_sessions`` table — a server restart
    doesn't log anyone out, and the token doubles as a lookup key for
    Teams (which teams does this person belong to?). Returns a plain
    dict so it's safe to use after the DB session is closed.
    """
    token = request.cookies.get("auth_token")
    if not token or len(token) != 64:
        return None
    db = SessionLocal()
    try:
        row = db.query(AuthSession).filter(AuthSession.token == token).first()
        if not row or row.expires_at < datetime.utcnow():
            return None
        user = db.query(User).filter(User.id == row.user_id).first()
        if not user:
            return None
        team_ids = [
            m.team_id
            for m in db.query(TeamMembership).filter(TeamMembership.user_id == user.id)
        ]
        return {**user.to_dict(), "team_ids": team_ids}
    finally:
        db.close()


def require_auth(request: Request):
    """Return the signed-in user (dict), or None if the cookie is invalid."""
    return get_current_user(request)


def check_auth(request: Request):
    """FastAPI dependency — raises 401 if not signed in; returns the user."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def check_admin(request: Request):
    """FastAPI dependency — raises 403 if user lacks admin privileges; returns user."""
    user = check_auth(request)
    is_admin = (
        user.get("provider") == "local"
        or user.get("email") == f"{config.ADMIN_USERNAME}@local"
        or user.get("is_admin") is True
        or (config.GOOGLE_DEV_MODE and user.get("email") == "alex.rivera@gmail.com")
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def _ensure_admin_team_membership(db: Session, user):
    """Make sure the local administrator has an owned team."""
    team = db.query(Team).filter(Team.name == "Default Team").first()
    if team is None:
        import secrets
        team = Team(name="Default Team", invite_code=_new_invite_code(),
                    created_by_email=f"{config.ADMIN_USERNAME}@local".lower())
        db.add(team)
        db.flush()
    already = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team.id, TeamMembership.user_id == user.id)
        .first()
    )
    if already is None:
        db.add(TeamMembership(team_id=team.id, user_id=user.id, role="owner"))


def _new_invite_code() -> str:
    """Generate a team invite code with full cryptographic entropy.

    16 bytes from ``secrets`` (a CSPRNG) = 128 bits, rendered as 22 URL-safe
    characters. That is far beyond guessing range: even at a million requests
    per second the expected search is ~10^22 years, so the code alone is a
    sufficient bearer credential for joining a team.

    All invite-code creation goes through here so the strength can never
    silently regress in one call site but not another.
    """
    return new_invite_code()


# Minimum acceptable invite-code length. Codes are 22 chars; anything shorter
# is treated as invalid without a database lookup, so a legacy or hand-made
# short code can never be matched — and there is nothing to brute-force.
INVITE_CODE_MIN_LENGTH = 20


def _looks_like_invite_code(code) -> bool:
    """Cheap structural pre-check for an invite code (no DB access)."""
    return isinstance(code, str) and len(code) >= INVITE_CODE_MIN_LENGTH


# How long an unauthenticated visitor's invite is remembered in the
# ``pending_invite`` cookie before it is discarded.
PENDING_INVITE_MAX_AGE_S = 15 * 60


def _process_pending_invite(db: Session, user, invite_code: str):
    """If an invite_code is provided, add user to that team as a member."""
    if not _looks_like_invite_code(invite_code):
        return None
    team = db.query(Team).filter(Team.invite_code == invite_code).first()
    if team:
        exists = db.query(TeamMembership).filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == user.id,
        ).first()
        if not exists:
            db.add(TeamMembership(team_id=team.id, user_id=user.id, role="member"))
            db.flush()
        return team
    return None


def _ensure_user_tenant_membership(db: Session, user, pending_invite: str = ""):
    """Ensure a user has an isolated workspace/team.

    1. If user has a valid pending invite code, join that invited team as a member.
    2. If the user already belongs to at least one team, keep their existing teams.
    3. If the user belongs to no teams and has no invite, create a dedicated
       personal tenant workspace (e.g. "{User's Name}'s Workspace"), where they
       are the sole OWNER.

    This guarantees strict tenant isolation: no new user ever gets dumped
    into another tenant's or the global admin's team!
    """
    if pending_invite:
        joined_team = _process_pending_invite(db, user, pending_invite)
        if joined_team:
            return joined_team

    # Check if user already belongs to any team
    existing_membership = db.query(TeamMembership).filter(TeamMembership.user_id == user.id).first()
    if existing_membership:
        return db.get(Team, existing_membership.team_id)

    # Brand-new tenant: create their own private workspace with a unique name
    import secrets
    base_name = f"{user.name}'s Workspace" if user.name else "My Workspace"
    ws_name = base_name
    counter = 1
    while db.query(Team).filter(Team.name == ws_name).first():
        ws_name = f"{base_name} ({secrets.token_hex(2)})"
        counter += 1
        if counter > 5:
            ws_name = f"{base_name} {secrets.token_hex(4)}"
            break

    new_team = Team(
        name=ws_name,
        invite_code=_new_invite_code(),
        created_by_email=(user.email or "").lower() or None,
    )
    db.add(new_team)
    db.flush()
    db.add(TeamMembership(team_id=new_team.id, user_id=user.id, role="owner"))
    return new_team

def _safe_relative_url(url: str, default: str = "/dashboard") -> str:
    """Ensure a URL is strictly a relative path on the same host to prevent open redirects."""
    if not url or not url.startswith("/") or url.startswith("//") or "://" in url or "\\" in url:
        return default
    return url


# ------------------------------------------------------------------ #
#  PAGE ROUTER — HTML pages served by Jinja2 templates.
# ------------------------------------------------------------------ #

page_router = APIRouter()

@page_router.get("/login")
async def login_page(request: Request):
    """Login page — POSTs to /api/auth/login."""
    import secrets
    error = request.query_params.get("error", "")
    invited_to = request.query_params.get("invited_to", "")
    csrf_token = secrets.token_hex(16)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "invited_to": invited_to,
            "verified": request.query_params.get("verified") == "1",
            "verified_email": request.query_params.get("email", ""),
            "google_enabled": bool(config.GOOGLE_CLIENT_ID or config.GOOGLE_DEV_MODE),
            "csrf_token": csrf_token,
        },
    )


@page_router.get("/signup")
async def signup_page(request: Request):
    """Self-serve account creation (email + password).

    Mirrors the Google path: signing up creates an account with no team, and
    /onboarding then asks whether to start a workspace or request to join one.
    """
    import secrets
    return templates.TemplateResponse(
        request=request,
        name="signup.html",
        context={
            "error": request.query_params.get("error", ""),
            "invited_to": request.query_params.get("invited_to", ""),
            "google_enabled": bool(config.GOOGLE_CLIENT_ID or config.GOOGLE_DEV_MODE),
            "csrf_token": secrets.token_hex(16),
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
    )


@page_router.get("/join/{invite_code}")
async def join_team_page(invite_code: str, request: Request, db: Session = Depends(get_db)):
    """Accept a shareable team invite link."""
    if not _looks_like_invite_code(invite_code):
        return RedirectResponse(url="/login?error=Invalid+or+expired+team+invite+link", status_code=302)
    team = db.query(Team).filter(Team.invite_code == invite_code).first()
    if not team:
        return RedirectResponse(url="/login?error=Invalid+or+expired+team+invite+link", status_code=302)

    current_user = get_current_user(request)
    if current_user:
        exists = db.query(TeamMembership).filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == current_user["id"],
        ).first()
        if not exists:
            db.add(TeamMembership(team_id=team.id, user_id=current_user["id"], role="member"))
            db.commit()
        dest = _safe_relative_url(f"/dashboard?team={int(team.id)}")
        return RedirectResponse(url=dest, status_code=302)

    # User is not logged in: store invite in cookie and prompt sign in.
    # 15 minutes is enough to finish a sign-in (including the Google round
    # trip) but keeps the window in which a forgotten cookie could silently
    # attach a later sign-in to this team as short as practical.
    safe_invited_to = quote(str(team.name)[:50], safe="")
    dest = _safe_relative_url(f"/login?invited_to={safe_invited_to}", default="/login")
    response = RedirectResponse(url=dest, status_code=302)
    response.set_cookie(
        key="pending_invite",
        value=invite_code,
        httponly=True,
        samesite="lax",
        max_age=PENDING_INVITE_MAX_AGE_S,
        secure=config.REQUIRE_HTTPS,
    )
    return response

@page_router.get("/dashboard")
async def dashboard_page(request: Request):
    """Main dashboard — shows the user's team listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    is_admin = (
        user.get("provider") == "local"
        or user.get("email") == f"{config.ADMIN_USERNAME}@local"
        or user.get("is_admin") is True
        or (config.GOOGLE_DEV_MODE and user.get("email") == "alex.rivera@gmail.com")
    )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"identity": user["name"], "is_admin": is_admin, "active": "dashboard"},
    )

@page_router.get("/admin")
async def admin_page(request: Request):
    """Admin settings page — manage API keys, refresh listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    is_admin = (
        user.get("provider") == "local"
        or user.get("email") == f"{config.ADMIN_USERNAME}@local"
        or user.get("is_admin") is True
        or (config.GOOGLE_DEV_MODE and user.get("email") == "alex.rivera@gmail.com")
    )
    if not is_admin:
        return RedirectResponse(url="/dashboard?error=admin_privileges_required")
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"identity": user["name"], "is_admin": True, "active": "admin"},
    )

# -- Google sign-in (OAuth 2.0 / OpenID Connect) --

@page_router.get("/auth/google")
async def google_login_start(request: Request):
    """Send the browser to Google's account-picker / consent screen."""
    force_live = request.query_params.get("live") == "1"
    force_dev = request.query_params.get("dev") == "1"

    if config.GOOGLE_DEV_MODE:
        if force_dev or not force_live:
            return RedirectResponse(url="/auth/google/dev-picker", status_code=302)
    elif force_dev:
        return RedirectResponse(url="/login?error=Google+dev+mode+is+disabled", status_code=303)

    if not config.GOOGLE_CLIENT_ID:
        return RedirectResponse(
            url="/login?error=Google+sign-in+is+not+configured+yet", status_code=302
        )

    if not force_live and "dummy" in config.GOOGLE_CLIENT_ID.lower():
        if config.GOOGLE_DEV_MODE:
            return RedirectResponse(url="/auth/google/dev-picker", status_code=302)
        return RedirectResponse(url="/login?error=Google+client+id+not+configured", status_code=303)
    import secrets

    # One-time random token: proves the callback came from our own redirect
    # (CSRF protection). Stored in a short-lived cookie, checked on return.
    state = secrets.token_urlsafe(16)
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        "client_id=" + quote(config.GOOGLE_CLIENT_ID)
        + "&redirect_uri=" + quote(f"{config.APP_BASE_URL}/auth/google/callback")
        + "&response_type=code"
        + "&scope=" + quote("openid email profile")
        + "&state=" + quote(state)
        + "&prompt=select_account"
    )
    response = RedirectResponse(url=url, status_code=302)
    # Lax (not strict): the cookie must be sent back when Google navigates
    # the browser to our callback URL, which is a cross-site top-level GET.
    response.set_cookie(
        key="google_oauth_state", value=state,
        httponly=True, samesite="lax", max_age=600,
        secure=config.REQUIRE_HTTPS,
    )
    return response


@page_router.get("/auth/google/dev-picker")
async def google_dev_picker(request: Request):
    """Local development/testing Google Account picker."""
    if not config.GOOGLE_DEV_MODE:
        raise HTTPException(status_code=403, detail="Google dev mode is disabled")
    import secrets
    error = request.query_params.get("error", "")
    csrf_token = secrets.token_hex(16)
    return templates.TemplateResponse(
        request=request,
        name="google_dev_picker.html",
        context={"error": error, "csrf_token": csrf_token},
    )


@page_router.post("/auth/google/dev-login")
async def google_dev_login(request: Request):
    """Simulate Google OAuth callback for local development."""
    if not config.GOOGLE_DEV_MODE:
        raise HTTPException(status_code=403, detail="Google dev mode is disabled")
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    name = (form.get("name") or "").strip() or email.split("@")[0].capitalize()

    if not email or "@" not in email:
        return RedirectResponse(url="/auth/google/dev-picker?error=Please+provide+a+valid+email", status_code=303)

    allowed = [e.strip().lower() for e in config.GOOGLE_ALLOWED_EMAILS if e.strip()]
    if allowed and email not in allowed:
        return RedirectResponse(
            url=f"/auth/google/dev-picker?error=Google+account+{quote(email)}+is+not+allowed+to+sign+in+here",
            status_code=303,
        )

    import secrets
    token = secrets.token_urlsafe(48)  # 64 chars
    is_admin = form.get("is_admin") == "1" or email == "alex.rivera@gmail.com"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, name=name, provider="google", is_admin=is_admin)
            db.add(user)
            db.flush()
        else:
            if name:
                user.name = name
            if is_admin:
                user.is_admin = True
        pending_invite = request.cookies.get("pending_invite", "")
        # Same routing rule as the real Google callback, so the simulator
        # cannot drift from production behaviour.
        dest = _resolve_signin_destination(db, user, pending_invite)
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
        db.commit()
    finally:
        db.close()

    response = RedirectResponse(url=dest, status_code=303)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,
        secure=config.REQUIRE_HTTPS,
    )
    if request.cookies.get("pending_invite"):
        response.delete_cookie("pending_invite")
    return response


@page_router.get("/auth/google/callback")
async def google_login_callback(request: Request):
    """Google redirects back here with ?code=...&state=..."""
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")
    state_cookie = request.cookies.get("google_oauth_state", "")

    def fail(message: str):
        response = RedirectResponse(url=f"/login?error={quote(message)}", status_code=303)
        response.delete_cookie("google_oauth_state")
        return response

    if error:
        return fail(f"Google returned an error: {error}")
    if not code:
        return fail("No authorisation code received from Google")
    if not state_cookie or state != state_cookie:
        return fail("Security state mismatch — please try signing in again")

    # 1. Exchange the one-time code for an access token.
    redirect_uri = f"{config.APP_BASE_URL}/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": config.GOOGLE_CLIENT_ID,
                    "client_secret": config.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        return fail(f"Could not reach Google: {exc}")

    if token_resp.status_code != 200:
        return fail(f"Google token exchange failed (HTTP {token_resp.status_code})")
    access_token = token_resp.json().get("access_token", "")
    if not access_token:
        return fail("Google did not return an access token")

    # 2. Fetch the signed-in user's profile.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            info_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        return fail(f"Could not fetch Google profile: {exc}")

    if info_resp.status_code != 200:
        return fail(f"Could not fetch Google profile (HTTP {info_resp.status_code})")

    info = info_resp.json()
    email = (info.get("email") or "").strip().lower()
    if not email:
        return fail("Google profile has no email address")
    if not info.get("email_verified", False):
        return fail("Email address is not verified on that Google account")

    allowed = [e.strip().lower() for e in config.GOOGLE_ALLOWED_EMAILS if e.strip()]
    if allowed and email not in allowed:
        return fail(f"Google account {email} is not allowed to sign in here")

    # 3. Success: find-or-create the user, make sure they're in the
    # shared (default) team, then issue a database-stored session —
    # exactly like the password login does.
    import secrets

    token = secrets.token_urlsafe(48)  # exactly 64 chars — matches require_auth()
    name = info.get("name") or email

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, name=name, provider="google")
            db.add(user)
            db.flush()
        else:
            user.name = name  # keep the display name up to date
        pending_invite = request.cookies.get("pending_invite", "")
        dest = _resolve_signin_destination(db, user, pending_invite)
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
        db.commit()
    finally:
        db.close()
    response = RedirectResponse(url=dest, status_code=303)
    response.set_cookie(
        key="auth_token", value=token,
        # Lax (not strict): this cookie is set on a response to a navigation
        # arriving from accounts.google.com. With SameSite=Strict the browser
        # withholds it on the immediately following /dashboard request, so the
        # user bounces straight back to /login?error=auth_required.
        httponly=True, samesite="lax", max_age=86400 * 7,
        secure=config.REQUIRE_HTTPS,
    )
    response.delete_cookie("google_oauth_state")
    if request.cookies.get("pending_invite"):
        response.delete_cookie("pending_invite")
    return response


def _resolve_signin_destination(db: Session, user, pending_invite: str = "") -> str:
    """Decide where a freshly signed-in user should land.

    Single source of truth for both the real Google callback and the local
    dev-login simulator, so the two cannot drift apart:

    * an invited user joins that team (explicit invite link);
    * a user who already belongs to a team goes to the dashboard;
    * a genuinely new account goes to /onboarding, where they choose between
      their own private workspace and asking to join someone else's team.

    Returns the destination path. Membership changes are flushed, not
    committed — the caller owns the transaction.
    """
    if pending_invite:
        joined_team = _ensure_user_tenant_membership(db, user, pending_invite)
        if joined_team is not None:
            return _safe_relative_url(f"/dashboard?team={int(joined_team.id)}")

    has_team = (
        db.query(TeamMembership)
        .filter(TeamMembership.user_id == user.id)
        .first()
        is not None
    )
    return "/dashboard" if has_team else "/onboarding"


# -- Onboarding: new accounts pick a team instead of being auto-enrolled --

@page_router.get("/onboarding")
async def onboarding_page(request: Request):
    """First-run chooser for a brand-new account.

    A new sign-in deliberately gets *no* team: they either create their own
    private workspace or ask to join someone else's. Anyone who already
    belongs to a team is sent straight to the dashboard.
    """
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required", status_code=302)
    if user.get("team_ids"):
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html",
        context={
            "identity": user["name"],
            "is_admin": bool(user.get("is_admin")),
            "active": "",
            "description_max": DESCRIPTION_MAX_LENGTH,
            "error": request.query_params.get("error", ""),
        },
    )


# ------------------------------------------------------------------ #
#  API ROUTER — JSON endpoints consumed by the JS front-end.
# ------------------------------------------------------------------ #

api_router = APIRouter()

# -- Auth --

LOGIN_ATTEMPTS = {}  # ip -> list of timestamps
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = LOGIN_ATTEMPTS.get(ip, [])
    valid = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = valid
    return len(valid) >= LOGIN_MAX_ATTEMPTS

def _record_failed_attempt(ip: str):
    now = time.time()
    attempts = LOGIN_ATTEMPTS.get(ip, [])
    attempts.append(now)
    LOGIN_ATTEMPTS[ip] = attempts


# ------------------------------------------------------------------ #
#  Rate limiting for team join requests.
#
#  Join requests are the one place an authenticated stranger can cause
#  something to appear in front of another user, so they are limited on
#  four independent axes. Each axis is checked before anything is written;
#  exceeding any one of them refuses the request.
#
#  NOTE: counters live in the database (rate_limit_events), not in process
#  memory. An in-memory counter is per-worker, so running N uvicorn workers
#  would silently multiply every limit by N. Persisting them keeps the limits
#  honest no matter how many workers or containers are running.
#
#  SQLite serialises writers, so each check is one small transaction. At this
#  app's scale that is negligible; if it ever isn't, swap this module's two
#  functions for Redis and nothing else needs to change.
# ------------------------------------------------------------------ #

# (label, max events, window seconds) — the window is also the cooldown.
RATE_LIMITS = {
    "user_burst": (1, 10 * 60),          # one attempt per 10 minutes
    "user_daily": (5, 24 * 60 * 60),     # 5 per day per account
    "team_hourly": (3, 60 * 60),         # 3 per hour per target team
    "team_daily": (10, 24 * 60 * 60),    # 10 per day per target team
    "ip_daily": (10, 24 * 60 * 60),      # 10 per day per source address
    "global_daily": (200, 24 * 60 * 60),  # ceiling for the whole instance

    # Self-serve signup. Creating an account is cheap for us but expensive in
    # bcrypt CPU, and it is the front door to the whole app, so it is capped
    # per source address and overall.
    "signup_ip_hourly": (3, 60 * 60),         # 3 signups per hour per IP
    "signup_ip_daily": (10, 24 * 60 * 60),    # 10 per day per IP
    "signup_global_daily": (50, 24 * 60 * 60),  # 50 per day for the instance

    # Resending a confirmation mail is an amplifier aimed at a third party's
    # inbox, so it is capped per address as well as per source.
    "resend_email_hourly": (3, 60 * 60),
    "resend_ip_hourly": (5, 60 * 60),
}

# Longest window in RATE_LIMITS; anything older is pruned on each write.
_RATE_MAX_WINDOW_S = max(window for _, window in RATE_LIMITS.values())

# Kept so existing tests that call `_RATE_BUCKETS.clear()` keep working; the
# real counters now live in the database (see _rate_prune()).
_RATE_BUCKETS: dict[str, list[float]] = {}


def _rate_check(key: str, limit: str, db: Session | None = None) -> bool:
    """Record an event for ``key`` if it is within the named limit.

    Returns True when allowed (and records it), False when the limit is hit.
    """
    max_events, window = RATE_LIMITS[limit]
    bucket = f"{limit}:{key}"
    cutoff = datetime.utcnow() - timedelta(seconds=window)

    own_session = db is None
    session = None
    try:
        session = db if db is not None else SessionLocal()
        # Prune expired rows opportunistically so the table stays small.
        session.query(RateLimitEvent).filter(
            RateLimitEvent.created_at < datetime.utcnow() - timedelta(seconds=_RATE_MAX_WINDOW_S)
        ).delete(synchronize_session=False)

        used = (
            session.query(RateLimitEvent)
            .filter(RateLimitEvent.bucket == bucket, RateLimitEvent.created_at >= cutoff)
            .count()
        )
        if used >= max_events:
            session.commit()
            return False

        session.add(RateLimitEvent(bucket=bucket, created_at=datetime.utcnow()))
        session.commit()
        return True
    except Exception:
        # Never let a limiter failure take down login/signup. Fail open, but
        # say so in the log rather than silently. Note `session` may still be
        # None here if even opening the session failed.
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        print(f"[ratelimit] check failed for {bucket!r}; allowing request")
        return True
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass


def _rate_retry_after(key: str, limit: str) -> int:
    """Seconds until this key may act again (for the error message)."""
    max_events, window = RATE_LIMITS[limit]
    bucket = f"{limit}:{key}"
    cutoff = datetime.utcnow() - timedelta(seconds=window)
    session = None
    try:
        session = SessionLocal()
        oldest = (
            session.query(RateLimitEvent.created_at)
            .filter(RateLimitEvent.bucket == bucket, RateLimitEvent.created_at >= cutoff)
            .order_by(RateLimitEvent.created_at.asc())
            .first()
        )
    except Exception:
        return 0
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    if not oldest or oldest[0] is None:
        return 0
    elapsed = (datetime.utcnow() - oldest[0]).total_seconds()
    return max(1, int(window - elapsed))


def _rate_prune() -> int:
    """Delete every recorded event. Used by tests and maintenance."""
    session = SessionLocal()
    try:
        removed = session.query(RateLimitEvent).delete(synchronize_session=False)
        session.commit()
        return removed
    finally:
        session.close()


def client_ip(request: Request) -> str:
    """Best available client address, honouring the reverse proxy.

    Behind Cloudflare every request arrives from the same edge IP, so raw
    ``request.client.host`` would put *all* visitors in one rate-limit bucket
    — the IP limits would throttle everyone together and an attacker could
    exhaust them for everybody.

    ``CF-Connecting-IP`` is preferred because Cloudflare sets it to the true
    caller and overwrites anything the client sent. ``X-Forwarded-For`` is the
    fallback for other proxies; only its first (left-most) entry is trusted,
    since the rest are client-supplied and trivially spoofed.

    Spoofing is only possible if a request reaches the origin *without* going
    through the proxy, which is why the origin should be firewalled to your
    proxy's addresses.
    """
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


# Free-text limits for a join request description.
DESCRIPTION_MAX_LENGTH = 300
DESCRIPTION_MIN_LENGTH = 3


def _sanitize_description(raw) -> str:
    """Return a safe one-line description, or "" if unusable.

    Deliberately strict — this text is stored and later displayed to another
    user (the team owner):

    * non-strings are coerced, never trusted as objects;
    * control characters (including NUL, CR, LF and tabs) are stripped so the
      value cannot forge log lines or break out of the UI element;
    * angle brackets are removed so no markup — script, img, svg — survives to
      the stored value even before Jinja's autoescaping is applied;
    * the result is collapsed to a single spaced line and hard-truncated.
    """
    if not isinstance(raw, str):
        return ""
    # Drop C0/C1 control characters and DEL.
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n\t")
    # Remove markup delimiters entirely rather than relying on escaping alone.
    cleaned = cleaned.replace("<", "").replace(">", "")
    # Collapse runs of whitespace into single spaces.
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > DESCRIPTION_MAX_LENGTH:
        cleaned = cleaned[:DESCRIPTION_MAX_LENGTH].rstrip()
    return cleaned


def _authenticate_credentials(db, username: str, password: str):
    """Resolve a username/password attempt to a User, or None.

    Two kinds of account can sign in this way:

    * the bootstrap administrator from .env (ADMIN_USERNAME / ADMIN_PASSWORD,
      stored as provider "local"), and
    * a self-serve account created at /signup (provider "password"), which
      matches on **email** and is verified against its bcrypt hash.

    Google accounts are deliberately not reachable here — a password cannot
    be set on an account whose identity was proven by Google.
    """
    import hmac

    # --- 1. bootstrap administrator ---
    if hmac.compare_digest(username, config.ADMIN_USERNAME.strip()) and \
            hmac.compare_digest(password, config.ADMIN_PASSWORD):
        admin = db.query(User).filter(User.provider == "local").first()
        if admin is None:
            admin = User(
                email=f"{config.ADMIN_USERNAME}@local",
                name=username,
                provider="local",
                is_admin=True,
            )
            db.add(admin)
            db.flush()
        else:
            admin.is_admin = True
        return admin

    # --- 2. self-serve password account ---
    if not username or not password:
        return None
    candidate = (
        db.query(User)
        .filter(User.email == username.lower(), User.provider == "password")
        .first()
    )
    if candidate is None:
        # Spend roughly the same time as a real check so the response does not
        # reveal whether the address exists.
        verify_password(password, "$2b$12$" + "." * 53)
        return None
    if verify_password(password, candidate.password_hash or ""):
        return candidate
    return None


@api_router.post("/auth/login")
async def login(request: Request):
    """Authenticate a local account.

    Accepts BOTH JSON bodies (API clients) and classic HTML form posts
    (the login page) — the form has no way to send JSON.
    Protected with rate limiting and constant-time password check.
    """
    peer_ip = client_ip(request)
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    if _is_rate_limited(peer_ip):
        if not is_json:
            return RedirectResponse(url="/login?error=Too+many+failed+attempts.+Please+wait+60+seconds", status_code=303)
        return JSONResponse(status_code=429, content={"ok": False, "error": "Too many failed login attempts. Please wait 60 seconds."})

    try:
        body = await request.json() if is_json else dict(await request.form())
    except Exception:
        body = {}

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    db = SessionLocal()
    try:
        user = _authenticate_credentials(db, username, password)
        if user is None:
            _record_failed_attempt(peer_ip)
            if not is_json:
                # Deliberately generic about *which* part was wrong (that would
                # leak whether an address is registered), but point at the two
                # things people actually get wrong.
                return RedirectResponse(
                    url="/login?error=That+email+address+and+password+didn%27t+match"
                        "+an+account.+If+you+signed+up+with+Google%2C+use+the"
                        "+Google+button+below.",
                    status_code=303,
                )
            return JSONResponse(status_code=401, content={"ok": False, "error": "Invalid credentials"})

        LOGIN_ATTEMPTS.pop(peer_ip, None)
        token = secrets.token_urlsafe(48)  # exactly 64 chars — matches require_auth()

        # The bootstrap admin signs in with a username, not an email, and must
        # always own its team. Self-serve accounts go through the same routing
        # as a Google sign-in.
        if user.provider == "local":
            _ensure_admin_team_membership(db, user)
            already_has_team = True
        else:
            already_has_team = (
                db.query(TeamMembership)
                .filter(TeamMembership.user_id == user.id)
                .first()
                is not None
            )

        if already_has_team:
            joined = _process_pending_invite(
                db, user, request.cookies.get("pending_invite", ""))
            dest = (
                _safe_relative_url(f"/dashboard?team={int(joined.id)}")
                if joined is not None
                else "/dashboard"
            )
        else:
            dest = _resolve_signin_destination(db, user, "")

        db.add(AuthSession(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=7),
        ))
        db.commit()
    finally:
        db.close()

    response = (
        RedirectResponse(url=dest, status_code=303)
        if not is_json
        else JSONResponse(content={"ok": True, "token": token})
    )
    # SameSite=lax (not strict): the cookie is set on a response to a
    # navigation that may have arrived cross-site, and strict would withhold
    # it on the very next request.
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,  # 7 days
        secure=config.REQUIRE_HTTPS,
    )
    if request.cookies.get("pending_invite"):
        response.delete_cookie("pending_invite")
    return response


@api_router.post("/auth/signup")
async def signup(request: Request):
    """Create a self-serve account with an email address and password.

    The new account is deliberately given NO team: like the Google flow, it is
    routed to /onboarding to choose between its own workspace and asking to
    join an existing team.
    """
    peer_ip = client_ip(request)
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    try:
        body = await request.json() if is_json else dict(await request.form())
    except Exception:
        body = {}

    def failure(message: str, status: int = 400):
        if is_json:
            return JSONResponse(status_code=status, content={"ok": False, "error": message})
        return RedirectResponse(url=f"/signup?error={quote(message)}", status_code=303)

    email = str(body.get("email", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    password = str(body.get("password", ""))
    confirm = str(body.get("confirm_password", body.get("password_confirm", "")))

    # --- validation (before consuming rate-limit budget) ---
    if not email or "@" not in email or len(email) > 255 or " " in email or email.startswith("@"):
        return failure("Enter a valid email address")
    if name and len(name) > 200:
        return failure("That name is too long")
    strength_error = validate_password_strength(password)
    if strength_error:
        return failure(strength_error)
    if confirm and confirm != password:
        return failure("Those passwords do not match")
    # Never allow a self-serve account to collide with the bootstrap admin's
    # synthetic address.
    if email == f"{config.ADMIN_USERNAME}@local".lower():
        return failure("That address is reserved")

    # --- rate limits ---
    for limit in ("signup_ip_hourly", "signup_ip_daily", "signup_global_daily"):
        key = peer_ip if limit != "signup_global_daily" else "all"
        if not _rate_check(key, limit):
            retry = _rate_retry_after(key, limit)
            if is_json:
                return JSONResponse(
                    status_code=429,
                    content={
                        "ok": False,
                        "error": "Too many signups. Please try again later.",
                        "retry_after_seconds": retry,
                    },
                    headers={"Retry-After": str(retry)},
                )
            return RedirectResponse(
                url="/signup?error=Too+many+signups.+Please+try+again+later",
                status_code=303,
            )

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing is not None:
            # Deliberately vague: do not confirm which addresses are taken.
            return failure("That email address cannot be used to sign up")

        # Nothing is created yet — no user, no team, no membership. Only a
        # pending record holding the (hashed) password and the confirmation
        # token. The account is created when the emailed link is opened.
        token = new_token()
        now = datetime.utcnow()
        pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
        if pending is None:
            pending = PendingSignup(email=email, send_count=0)
            db.add(pending)
        pending.name = name or email.split("@")[0]
        pending.password_hash = hash_password(password)
        pending.token_hash = token_fingerprint(token)
        pending.expires_at = now + timedelta(minutes=VERIFICATION_TTL_MINUTES)
        pending.send_count = (pending.send_count or 0) + 1
        pending.last_sent_at = now
        db.commit()
        pending_id = pending.id
    finally:
        db.close()

    verify_url = f"{config.APP_BASE_URL}/verify-email?token={quote(token)}"
    sent, send_error = mailer.send_verification_email(
        email, name or "", verify_url, VERIFICATION_TTL_MINUTES)

    if not sent:
        # Without a delivered link the signup cannot complete, so say so
        # plainly rather than showing a "check your inbox" wall.
        detail = send_error or "Outbound email is not configured"
        return failure(
            f"We could not send the confirmation email ({detail}). "
            f"Please try again later."
        )

    if is_json:
        return JSONResponse(content={
            "ok": True,
            "pending": True,
            "redirect": f"/verify-pending?email={quote(email)}",
            "expires_minutes": VERIFICATION_TTL_MINUTES,
        })
    return RedirectResponse(
        url=f"/verify-pending?email={quote(email)}&sent=1", status_code=303)


@api_router.post("/auth/resend-verification")
async def resend_verification(request: Request):
    """Re-send the confirmation email for a pending signup."""
    peer_ip = client_ip(request)
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    try:
        body = await request.json() if is_json else dict(await request.form())
    except Exception:
        body = {}

    email = str(body.get("email", "")).strip().lower()
    if not email:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Email is required"})

    # A resend is a mail amplifier, so it is limited tightly per address and
    # per source. The response is the same whether or not anything matched.
    generic = {"ok": True, "message": "If that signup is pending, a new link is on its way."}
    for limit, key in (("resend_email_hourly", email), ("resend_ip_hourly", peer_ip)):
        if not _rate_check(key, limit):
            retry = _rate_retry_after(key, limit)
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "Too many resend attempts. Please wait.",
                         "retry_after_seconds": retry},
                headers={"Retry-After": str(retry)},
            )

    db = SessionLocal()
    try:
        pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
        if pending is None:
            return generic if is_json else RedirectResponse(
                url=f"/verify-pending?email={quote(email)}&sent=1", status_code=303)
        if (pending.send_count or 0) >= MAX_VERIFICATION_SENDS:
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "Too many confirmation emails sent for this address. Please sign up again later."},
            )
        token = new_token()
        pending.token_hash = token_fingerprint(token)
        pending.expires_at = datetime.utcnow() + timedelta(minutes=VERIFICATION_TTL_MINUTES)
        pending.send_count = (pending.send_count or 0) + 1
        pending.last_sent_at = datetime.utcnow()
        name = pending.name or ""
        db.commit()
    finally:
        db.close()

    verify_url = f"{config.APP_BASE_URL}/verify-email?token={quote(token)}"
    mailer.send_verification_email(email, name, verify_url, VERIFICATION_TTL_MINUTES)

    if is_json:
        return generic
    return RedirectResponse(
        url=f"/verify-pending?email={quote(email)}&sent=1", status_code=303)


@page_router.get("/verify-email")
async def verify_email(token: str = "", request: Request = None):
    """Confirm an address and only now create the account."""
    if not token:
        return RedirectResponse(url="/login?error=That+confirmation+link+is+invalid", status_code=302)

    fingerprint = token_fingerprint(token)
    created_user_id = None
    db = SessionLocal()
    try:
        pending = (
            db.query(PendingSignup)
            .filter(PendingSignup.token_hash == fingerprint)
            .first()
        )
        if pending is None:
            return RedirectResponse(
                url="/login?error=That+confirmation+link+is+invalid+or+has+already+been+used",
                status_code=302,
            )
        if pending.is_expired():
            email = pending.email
            db.delete(pending)
            db.commit()
            return RedirectResponse(
                url=f"/verify-pending?email={quote(email)}&expired=1", status_code=302
            )
        # Someone else may have completed a signup for this address in the
        # meantime; never overwrite a real account.
        if db.query(User).filter(User.email == pending.email).first() is not None:
            db.delete(pending)
            db.commit()
            return RedirectResponse(
                url="/login?error=An+account+already+exists+for+that+address.+Please+sign+in",
                status_code=302,
            )

        user = User(
            email=pending.email,
            name=pending.name,
            # NOT "local": that provider is treated as admin by check_admin().
            provider="password",
            password_hash=pending.password_hash,
            is_admin=False,
        )
        db.add(user)
        db.flush()
        created_user_id = user.id
        email = pending.email
        # Single use: the pending row is consumed.
        db.delete(pending)
        db.commit()
    finally:
        db.close()

    return RedirectResponse(
        url=f"/login?verified=1&email={quote(email)}", status_code=302)


@page_router.get("/verify-pending")
async def verify_pending_page(request: Request):
    """'Check your inbox' holding page for an unconfirmed signup."""
    import secrets
    return templates.TemplateResponse(
        request=request,
        name="verify_pending.html",
        context={
            "email": request.query_params.get("email", ""),
            "sent": request.query_params.get("sent") == "1",
            "expired": request.query_params.get("expired") == "1",
            "smtp_configured": mailer.smtp_configured(),
            "expires_minutes": VERIFICATION_TTL_MINUTES,
            "csrf_token": secrets.token_hex(16),
        },
    )

# -- OAuth callbacks (marketplace redirect targets) --

@page_router.get("/api/auth/{platform}/callback")
async def oauth_callback(platform: str, request: Request):
    """Handle the marketplace OAuth redirect.

    After authorising on eBay/Etsy the marketplace redirects the user's
    browser back here with ``?code=...&state=...``. We exchange the code
    for tokens, then show a simple confirmation page.
    """
    from src.adapters import get_adapter

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    try:
        adapter = get_adapter(platform)
    except KeyError:
        return templates.TemplateResponse(
            request=request,
            name="oauth_callback.html",
            context={
                "success": False,
                "platform_name": platform,
                "error": f"No adapter registered for '{platform}'.",
            },
            status_code=404,
        )

    if error:
        result = {"success": False, "error": f"Marketplace returned an error: {error}"}
    elif code:
        result = adapter.handle_callback(code, state)
    else:
        result = {"success": False, "error": "No authorization code received."}

    return templates.TemplateResponse(
        request=request,
        name="oauth_callback.html",
        context={
            "success": bool(result.get("success")),
            "platform_name": adapter.get_platform_name() if result.get("success") else platform.capitalize(),
            "error": result.get("error", "Unknown error"),
        },
        status_code=200,
    )

@api_router.post("/auth/logout")
async def logout(request: Request):
    """Clear the auth cookie and drop the session from the database."""
    token = request.cookies.get("auth_token", "")
    if token:
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.token == token).delete()
            db.commit()
        finally:
            db.close()
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="auth_token", httponly=True)
    return response

# -- Settings (saved from the Admin page so no one has to edit .env) --

# Keys the Admin page is allowed to read/write. Values are type-coerced to
# match the existing Config attribute before being stored.
SETTINGS_KEYS = (
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_ALLOWED_EMAILS",
    "GOOGLE_DEV_MODE",
    "APP_BASE_URL",
    "DEFAULT_REFRESH_INTERVAL_S",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
    "ETSY_API_KEY",
    "ETSY_API_SECRET",
    "DEFAULT_CURRENCY",
    "DEFAULT_CURRENCY_SYMBOL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
    "POSHMARK_USERNAME",
    "POSHMARK_API_KEY",
    "AMAZON_SELLER_ID",
    "AMAZON_CLIENT_ID",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_REFRESH_TOKEN",
    "AMAZON_MARKETPLACE_ID",
    # Outbound email: transport choice and provider API
    "MAIL_BACKEND",
    "MAIL_PROVIDER",
    "MAIL_API_KEY",
    "MAIL_API_URL",
    "MAIL_FROM",
    # Upload storage
    "UPLOAD_BACKEND",
    "S3_BUCKET",
    "S3_REGION",
    "S3_ENDPOINT_URL",
    "S3_PUBLIC_BASE_URL",
    "S3_FORCE_DOWNLOAD",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)

SECRET_KEYS = {
    "ADMIN_PASSWORD",
    "GOOGLE_CLIENT_SECRET",
    "SMTP_PASSWORD",
    "MAIL_API_KEY",
    "S3_SECRET_ACCESS_KEY",
    "EBAY_CLIENT_SECRET",
    "ETSY_API_SECRET",
    "POSHMARK_API_KEY",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_REFRESH_TOKEN",
}

def _mask_secret(val: str) -> str:
    return "••••••••" if val else ""

@api_router.get("/settings")
async def get_settings(_admin: dict = Depends(check_admin)):
    """Return the settings the Admin page manages (requires admin privileges)."""
    return {
        "settings": {
            "ADMIN_USERNAME": config.ADMIN_USERNAME,
            "ADMIN_PASSWORD": _mask_secret(config.ADMIN_PASSWORD),
            "GOOGLE_CLIENT_ID": config.GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": _mask_secret(config.GOOGLE_CLIENT_SECRET),
            "GOOGLE_ALLOWED_EMAILS": ",".join(config.GOOGLE_ALLOWED_EMAILS),
            "GOOGLE_DEV_MODE": config.GOOGLE_DEV_MODE,
            "APP_BASE_URL": config.APP_BASE_URL,
            "DEFAULT_CURRENCY": config.DEFAULT_CURRENCY,
            "DEFAULT_CURRENCY_SYMBOL": config.DEFAULT_CURRENCY_SYMBOL,
            "SMTP_HOST": config.SMTP_HOST,
            "SMTP_PORT": config.SMTP_PORT,
            "SMTP_USER": config.SMTP_USER,
            "SMTP_PASSWORD": _mask_secret(config.SMTP_PASSWORD),
            "SMTP_FROM": config.SMTP_FROM,
            "SMTP_USE_TLS": config.SMTP_USE_TLS,
            "MAIL_BACKEND": config.MAIL_BACKEND,
            "MAIL_PROVIDER": config.MAIL_PROVIDER,
            "MAIL_API_KEY": _mask_secret(config.MAIL_API_KEY),
            "MAIL_API_URL": config.MAIL_API_URL,
            "MAIL_FROM": config.MAIL_FROM,
            "UPLOAD_BACKEND": config.UPLOAD_BACKEND,
            "S3_BUCKET": config.S3_BUCKET,
            "S3_REGION": config.S3_REGION,
            "S3_ENDPOINT_URL": config.S3_ENDPOINT_URL,
            "S3_PUBLIC_BASE_URL": config.S3_PUBLIC_BASE_URL,
            "S3_FORCE_DOWNLOAD": config.S3_FORCE_DOWNLOAD,
            "S3_ACCESS_KEY_ID": config.S3_ACCESS_KEY_ID,
            "S3_SECRET_ACCESS_KEY": _mask_secret(config.S3_SECRET_ACCESS_KEY),
            "DEFAULT_REFRESH_INTERVAL_S": config.DEFAULT_REFRESH_INTERVAL_S,
            "EBAY_CLIENT_ID": config.EBAY_CLIENT_ID,
            "EBAY_CLIENT_SECRET": _mask_secret(config.EBAY_CLIENT_SECRET),
            "ETSY_API_KEY": config.ETSY_API_KEY,
            "ETSY_API_SECRET": _mask_secret(config.ETSY_API_SECRET),
            "POSHMARK_USERNAME": config.POSHMARK_USERNAME,
            "POSHMARK_API_KEY": _mask_secret(config.POSHMARK_API_KEY),
            "AMAZON_SELLER_ID": config.AMAZON_SELLER_ID,
            "AMAZON_CLIENT_ID": config.AMAZON_CLIENT_ID,
            "AMAZON_CLIENT_SECRET": _mask_secret(config.AMAZON_CLIENT_SECRET),
            "AMAZON_REFRESH_TOKEN": _mask_secret(config.AMAZON_REFRESH_TOKEN),
            "AMAZON_MARKETPLACE_ID": config.AMAZON_MARKETPLACE_ID,
        },
        "google_redirect_uri": f"{config.APP_BASE_URL}/auth/google/callback",
        "ebay_redirect_uri": f"{config.APP_BASE_URL}/api/auth/ebay/callback",
        "etsy_redirect_uri": f"{config.APP_BASE_URL}/api/auth/etsy/callback",
        "poshmark_redirect_uri": f"{config.APP_BASE_URL}/api/auth/poshmark/callback",
        "amazon_redirect_uri": f"{config.APP_BASE_URL}/api/auth/amazon/callback",
    }

@api_router.post("/settings")
async def update_settings(body: dict, _admin: dict = Depends(check_admin)):
    """Update settings from the Admin page and persist them to .env (admin only)."""
    updated = []
    for key, value in body.items():
        if key not in SETTINGS_KEYS:
            continue  # ignore unknown keys (forward-compatible)
        # Never overwrite sensitive secret fields if masked with dots or empty
        if key in SECRET_KEYS:
            val_str = str(value).strip()
            if not val_str or "••" in val_str or "****" in val_str:
                continue

        existing = getattr(config, key)
        if isinstance(existing, bool):
            value = value in (True, "true", "1", 1)
        elif isinstance(existing, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(existing, list):
            value = [v.strip() for v in str(value).split(",") if v.strip()]
        else:
            value = str(value).strip()
        setattr(config, key, value)
        if key == "EBAY_CLIENT_ID": config.EBUY_CLIENT_ID = value
        elif key == "EBAY_CLIENT_SECRET": config.EBUY_CLIENT_SECRET = value
        elif key == "ETSY_API_KEY": config.ESY_API_KEY = value
        elif key == "ETSY_API_SECRET": config.ESY_API_SECRET = value
        updated.append(key)

    if updated:
        persist_env({k: getattr(config, k) for k in updated})

    return {"ok": True, "updated": updated}


@api_router.get("/settings/mail-usage")
async def get_mail_usage(_admin: dict = Depends(check_admin)):
    """Report the email provider's sending quota (admin only).

    Signup mail silently stops working once the daily or monthly cap is hit,
    so surfacing it here means you find out before a real signup does.
    """
    usage, reason = mailer.fetch_provider_usage()
    if usage is None:
        return {
            "ok": False,
            "available": False,
            "reason": reason,
            "backend": mailer.mail_backend() or "disabled",
        }
    return {"ok": True, "available": True, "usage": usage}


@api_router.post("/settings/test-email")
async def send_test_email(body: dict, _admin: dict = Depends(check_admin)):
    """Send a test message so an admin can prove SMTP works.

    Saving settings alone cannot tell you whether delivery succeeds, so this
    reports the actual SMTP result instead of pretending.
    """
    to = (body.get("to") or "").strip().lower()
    if not to or "@" not in to:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Enter a valid recipient address"})
    if not mailer.smtp_configured():
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Set an SMTP host and username (or From address) first, then save."},
        )
    ok, error = mailer.send_test_email(to)
    if not ok:
        return JSONResponse(status_code=502, content={"ok": False, "error": f"Send failed — {error}"})
    return {"ok": True, "message": f"Test email sent to {to}"}

# -- Users & admin assignment (admin only) --

@api_router.get("/users")
async def list_users(db: Session = Depends(get_db), _admin: dict = Depends(check_admin)):
    """List the accounts on this instance so an admin can grant admin rights.

    Deliberately admin-only: it exposes every user's email address.
    """
    users = db.query(User).order_by(User.id).all()
    return {
        "users": [u.to_dict() for u in users],
        "total": len(users),
        "current_user_id": _admin["id"],
    }


@api_router.post("/users/{user_id}/admin")
async def set_user_admin(
    user_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _admin: dict = Depends(check_admin),
):
    """Grant or revoke admin privileges for a user (admin only)."""
    target = db.get(User, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})

    make_admin = bool(body.get("is_admin"))

    # A local-provider account is always treated as an admin (see check_admin),
    # so revoking it would be a lie — the flag would have no effect.
    if target.provider == "local" and not make_admin:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "The local administrator always has admin access"},
        )

    # Don't let an admin lock themselves out of the Admin page.
    if target.id == _admin["id"] and not make_admin:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "You cannot revoke your own admin access"},
        )

    # Never remove the last remaining admin.
    if not make_admin:
        remaining = (
            db.query(User)
            .filter(User.is_admin == True)  # noqa: E712
            .filter(User.id != target.id)
            .count()
        )
        if remaining == 0:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "At least one admin must remain"},
            )

    target.is_admin = make_admin
    db.commit()
    db.refresh(target)
    return {"ok": True, "user": target.to_dict()}


# -- Listings --

@api_router.get("/listings")
async def get_listings(
    platform: str = None,
    status: str = None,
    search: str = None,
    team: str = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "created_at",
    order: str = "desc",
    db: Session = Depends(get_db),
    _user: dict = Depends(check_auth),
):
    """Return a paginated, filtered list of listings."""
    from sqlalchemy import or_

    query = db.query(Listing)

    # Team scoping: strictly isolated to caller's verified teams.
    # A user only ever sees listings belonging to their own teams.
    user_team_ids = _user.get("team_ids") or []
    # For local admin, auto-claim any legacy unassigned listings into the admin's team
    if (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        unassigned = db.query(Listing).filter(Listing.team_id == None).all()
        if unassigned:
            for item in unassigned:
                item.team_id = user_team_ids[0]
            db.commit()

    if not user_team_ids:
        query = query.filter(Listing.id == None)  # Matches nothing
    elif team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        if team_id in user_team_ids:
            query = query.filter(Listing.team_id == team_id)
        else:
            query = query.filter(Listing.id == None)  # Matches nothing
    else:
        query = query.filter(Listing.team_id.in_(user_team_ids))

    # Filter by platform (primary platform or cross-listed in platforms_json)
    if platform:
        query = query.filter(
            or_(
                Listing.platform == platform,
                Listing.platforms_json.like(f'%"{platform}"%'),
            )
        )

    # Filter by status
    if status:
        query = query.filter(Listing.status == status)

    # Full-text search on title (LIKE with wildcards — SQLAlchemy escapes)
    if search:
        like_pattern = f"%{search}%"
        query = query.filter(Listing.title.ilike(like_pattern))

    # Sort
    sort_col = getattr(Listing, sort, Listing.created_at)
    if order == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    # Count total (for pagination UI)
    total = query.count()

    # Paginate
    listings = query.offset((page - 1) * page_size).limit(page_size).all()

    # Attach team name from caller's teams only (prevents leaking other tenant team names)
    team_names = {t.id: t.name for t in db.query(Team).filter(Team.id.in_(user_team_ids)).all()}
    items = []
    for l in listings:
        item = l.to_dict()
        item["team_name"] = team_names.get(l.team_id) if l.team_id else "Unassigned"
        items.append(item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "listings": items,
    }

@api_router.get("/stats")
async def get_stats(team: str = None, days: str = None, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Return summary stats for the dashboard header (team-scoped, optional time-filtered)."""
    from sqlalchemy import func, or_, and_
    from sqlalchemy.sql.functions import coalesce

    # Same strict team scoping as the listings endpoint.
    user_team_ids = _user.get("team_ids") or []
    # For local admin, auto-claim any legacy unassigned listings into the admin's team
    if (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        unassigned = db.query(Listing).filter(Listing.team_id == None).all()
        if unassigned:
            for item in unassigned:
                item.team_id = user_team_ids[0]
            db.commit()

    if not user_team_ids:
        scope = (Listing.id == None)
    elif team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        scope = (Listing.team_id == team_id) if team_id in user_team_ids else (Listing.id == None)
    else:
        scope = Listing.team_id.in_(user_team_ids)

    days_int = None
    if days and str(days).lower() not in ("all", "0", "none", ""):
        try:
            days_int = int(days)
        except (TypeError, ValueError):
            days_int = None

    cutoff = (datetime.utcnow() - timedelta(days=days_int)) if days_int else None

    if cutoff:
        sold_time = coalesce(Listing.sold_at, Listing.updated_at)
        sold_scope = and_(scope, Listing.is_sold == True, sold_time >= cutoff)  # noqa: E712
        active_scope = and_(scope, Listing.status == "active", Listing.created_at >= cutoff)
        total_scope = and_(scope, Listing.created_at >= cutoff)
        write_off_time = coalesce(Listing.written_off_at, Listing.updated_at)
        written_off_scope = and_(scope, Listing.status == "written_off", write_off_time >= cutoff)
    else:
        sold_scope = and_(scope, Listing.is_sold == True)  # noqa: E712
        active_scope = and_(scope, Listing.status == "active")
        total_scope = scope
        written_off_scope = and_(scope, Listing.status == "written_off")

    total = db.query(func.count(Listing.id)).filter(total_scope).scalar() or 0
    active = db.query(func.count(Listing.id)).filter(active_scope).scalar() or 0
    sold = db.query(func.count(Listing.id)).filter(sold_scope).scalar() or 0
    written_off = db.query(func.count(Listing.id)).filter(written_off_scope).scalar() or 0

    # Count per platform
    platform_counts = (
        db.query(Listing.platform, func.count(Listing.id))
        .filter(total_scope)
        .group_by(Listing.platform)
        .all()
    )

    platforms = {p: c for p, c in platform_counts}

    # Total value (sum of active listings in scope)
    total_value = db.query(func.sum(Listing.price_cents)).filter(active_scope).scalar() or 0

    # Total investment / cost across active listings in scope
    total_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(active_scope).scalar() or 0

    # Total profit realized from sold items in scope (revenue - costs)
    sold_revenue = db.query(func.sum(Listing.price_cents)).filter(sold_scope).scalar() or 0
    sold_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(sold_scope).scalar() or 0
    sold_profit = sold_revenue - sold_cost

    # Written-off inventory loss cost
    written_off_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(written_off_scope).scalar() or 0

    # Build timeline buckets for the chart
    from datetime import date
    now = datetime.utcnow()
    buckets = []
    bucket_map = {}

    # Query sold items in scope
    sold_rows = db.query(
        Listing.price_cents,
        Listing.purchase_price_cents,
        Listing.parts_cost_cents,
        coalesce(Listing.sold_at, Listing.updated_at).label("sold_date")
    ).filter(sold_scope).all()

    if days_int:
        # Daily buckets for 7 or 30 days
        for i in reversed(range(days_int)):
            d = (now - timedelta(days=i)).date()
            key = d.isoformat()
            label = d.strftime("%b %d")
            b = {
                "key": key,
                "label": label,
                "date_str": key,
                "revenue_cents": 0,
                "cost_cents": 0,
                "profit_cents": 0,
                "sold_count": 0,
            }
            buckets.append(b)
            bucket_map[key] = b
    else:
        # Monthly buckets for all time (at least 6 months up to current month)
        current_year = now.year
        current_month = now.month
        all_month_keys = set()
        for offset in reversed(range(6)):
            m = current_month - offset
            y = current_year
            while m <= 0:
                m += 12
                y -= 1
            all_month_keys.add(f"{y:04d}-{m:02d}")

        for row in sold_rows:
            dt = row.sold_date
            if isinstance(dt, str):
                try:
                    dt = datetime.fromisoformat(dt)
                except Exception:
                    continue
            if dt:
                all_month_keys.add(dt.strftime("%Y-%m"))

        min_key = min(all_month_keys)
        max_key = max(all_month_keys)
        min_y, min_m = int(min_key[:4]), int(min_key[5:7])
        max_y, max_m = int(max_key[:4]), int(max_key[5:7])

        cy, cm = min_y, min_m
        while (cy < max_y) or (cy == max_y and cm <= max_m):
            key = f"{cy:04d}-{cm:02d}"
            d = date(cy, cm, 1)
            b = {
                "key": key,
                "label": d.strftime("%b '%y"),
                "date_str": key,
                "revenue_cents": 0,
                "cost_cents": 0,
                "profit_cents": 0,
                "sold_count": 0,
            }
            buckets.append(b)
            bucket_map[key] = b
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1

    # Populate buckets from sold items
    for row in sold_rows:
        dt = row.sold_date
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                continue
        if not dt:
            continue

        b_key = dt.strftime("%Y-%m-%d") if days_int else dt.strftime("%Y-%m")
        if b_key in bucket_map:
            target_b = bucket_map[b_key]
            rev = row.price_cents or 0
            cost = (row.purchase_price_cents or 0) + (row.parts_cost_cents or 0)
            target_b["revenue_cents"] += rev
            target_b["cost_cents"] += cost
            target_b["profit_cents"] += (rev - cost)
            target_b["sold_count"] += 1

    timeline_points = [
        {
            "key": b["key"],
            "label": b["label"],
            "date": b["date_str"],
            "revenue_cents": b["revenue_cents"],
            "cost_cents": b["cost_cents"],
            "profit_cents": b["profit_cents"],
            "revenue": round(b["revenue_cents"] / 100.0, 2),
            "profit": round(b["profit_cents"] / 100.0, 2),
            "sold_count": b["sold_count"],
        }
        for b in buckets
    ]

    timeline = {
        "type": "daily" if days_int else "monthly",
        "period": f"{days_int}d" if days_int else "all",
        "points": timeline_points,
    }

    return {
        "period": f"{days_int}d" if days_int else "all",
        "days": days_int,
        "total_listings": total,
        "active_listings": active,
        "sold_listings": sold,
        "written_off_listings": written_off,
        "written_off_cost_cents": written_off_cost,
        "total_value_cents": total_value,
        "total_cost_cents": total_cost,
        "sold_revenue_cents": sold_revenue,
        "sold_cost_cents": sold_cost,
        "sold_profit_cents": sold_profit,
        "currency": config.DEFAULT_CURRENCY or "CAD",
        "currency_symbol": config.DEFAULT_CURRENCY_SYMBOL or "$",
        "platforms": platforms,
        "timeline": timeline,
    }

# -- Marketplace accounts (credentials) --

@api_router.get("/accounts")
async def get_accounts(db: Session = Depends(get_db), _admin: dict = Depends(check_admin)):
    """Return the list of configured marketplace accounts."""
    from src.models import MarketplaceAccount

    accounts = db.query(MarketplaceAccount).all()
    return [
        {
            "platform": a.platform,
            "shop_name": a.shop_name,
            "is_connected": a.is_connected,
            "last_synced": a.last_synced.isoformat() if a.last_synced else None,
        }
        for a in accounts
    ]

@api_router.post("/accounts/connect")
async def connect_account(body: dict, _admin: dict = Depends(check_admin)):
    """Start an OAuth flow.  Returns the authorisation URL to redirect to.

    Body should contain: ``platform`` (e.g. "ebay", "etsy").
    """
    from src.adapters import get_adapter

    platform = body.get("platform", "")
    try:
        adapter = get_adapter(platform)
        auth_url = adapter.get_authorization_url()
        return {"auth_url": auth_url}
    except KeyError:
        return JSONResponse(status_code=400, content={"error": f"Platform '{platform}' not supported yet"})

@api_router.post("/accounts/sync")
async def sync_account(body: dict, db: Session = Depends(get_db), _admin: dict = Depends(check_admin)):
    """Manually trigger a sync for a given marketplace.

    Fetches new listings and stores them in the database.
    Body: { "platform": "ebay" } or { "platform": "etsy" }
    """
    from src.adapters import get_adapter

    platform = body.get("platform", "")
    try:
        adapter = get_adapter(platform)
    except KeyError:
        return JSONResponse(status_code=400, content={"error": f"Platform '{platform}' not supported yet"})

    try:
        result = adapter.sync_all(db)
        return {"ok": True, "platform": platform, "result": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# -- Teams (shared inventory) --

@api_router.get("/teams")
async def get_teams(db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """The teams the current user belongs to, with each team's members and invite links."""
    teams = db.query(Team).filter(Team.id.in_(_user["team_ids"] or [0])).all()
    import secrets
    result = []
    for t in teams:
        if not t.invite_code:
            t.invite_code = _new_invite_code()
            db.commit()
        roles = {
            m.user_id: m.role
            for m in db.query(TeamMembership).filter(TeamMembership.team_id == t.id)
        }
        members = (
            db.query(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .filter(TeamMembership.team_id == t.id)
            .order_by(User.id)
            .all()
        )
        result.append({
            "id": t.id,
            "name": t.name,
            "invite_code": t.invite_code,
            "invite_url": f"{config.APP_BASE_URL}/join/{t.invite_code}",
            "role": roles.get(_user["id"], "member"),
            "members": [
                {**u.to_dict(), "role": roles.get(u.id, "member")}
                for u in members
            ],
        })
    return result


@api_router.post("/teams")
async def create_team(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Create a new team; the creator becomes its owner."""
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Team name is required"})

    # Check duplicate team name within this user's teams
    user_team_names = {
        t.name.lower()
        for t in db.query(Team).join(TeamMembership, TeamMembership.team_id == Team.id)
        .filter(TeamMembership.user_id == _user["id"]).all()
    }
    if name.lower() in user_team_names:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"You already have a team named '{name}'"})

    import secrets
    invite_code = _new_invite_code()
    team = Team(name=name, invite_code=invite_code,
                created_by_email=(_user.get("email") or "").lower() or None)
    db.add(team)
    db.flush()
    db.add(TeamMembership(team_id=team.id, user_id=_user["id"], role="owner"))
    db.commit()
    t_dict = team.to_dict()
    t_dict["invite_url"] = f"{config.APP_BASE_URL}/join/{team.invite_code}"
    return {"ok": True, "team": t_dict}


@api_router.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Add a member to a team by email (owner or admin only).

    If that person hasn't signed in yet, their user row is created up
    front — when they later sign in (Google) they land in the team
    automatically, so sharing just works.
    """
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})

    caller_m = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"]
    ).first()
    is_owner = caller_m and caller_m.role == "owner"
    is_admin = bool(_user.get("is_admin") or _user.get("provider") == "local")
    if not (is_owner or is_admin):
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners or administrators can invite new members"})

    email = (body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Email is required"})
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, name=email.split("@")[0], provider="google")
        db.add(user)
        db.flush()
    exists = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user.id)
        .first()
    )
    if exists:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Already a member of that team"})
    db.add(TeamMembership(team_id=team_id, user_id=user.id, role="member"))
    team = db.get(Team, team_id)
    if not team.invite_code:
        import secrets
        team.invite_code = _new_invite_code()
    db.commit()

    invite_url = f"{config.APP_BASE_URL}/join/{team.invite_code}"
    inviter_name = _user.get("name") or "A team member"
    invite_subject = f"Invitation to join team '{team.name}' on {config.APP_NAME}"
    invite_body = (
        f"Hi,\n\n"
        f"{inviter_name} has invited you to collaborate on team '{team.name}' in {config.APP_NAME}.\n\n"
        f"Click the link below to accept the invitation and sign in:\n"
        f"{invite_url}\n\n"
        f"Welcome to the team!"
    )

    email_sent = False
    email_error = ""
    if mailer.smtp_configured():
        email_sent, email_error = mailer.send_email(email, invite_subject, invite_body)

    return {
        "ok": True,
        "email_sent": email_sent,
        "email_error": email_error,
        "invite_url": invite_url,
        "invite_subject": invite_subject,
        "invite_body": invite_body,
        "team_name": team.name,
    }


@api_router.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: int, user_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Remove a member from a team (must be team owner, the user leaving, or an administrator)."""
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    membership = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        .first()
    )
    if not membership:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Member not found"})

    caller_membership = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"]
    ).first()
    is_owner = caller_membership and caller_membership.role == "owner"
    is_self = _user["id"] == user_id
    is_admin = bool(_user.get("is_admin") or _user.get("provider") == "local")
    if not (is_owner or is_self or is_admin):
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners or administrators can remove other members"})

    if membership.role == "owner":
        owner_count = (
            db.query(TeamMembership)
            .filter(TeamMembership.team_id == team_id, TeamMembership.role == "owner")
            .count()
        )
        if owner_count <= 1:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Cannot remove the last owner"})
    db.delete(membership)
    db.commit()
    return {"ok": True}


@api_router.post("/teams/{team_id}/invite-code")
async def regenerate_invite_code(team_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Regenerate the shareable invite link for a team (owner only)."""
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    membership = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"],
    ).first()
    if not membership or membership.role != "owner":
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners can regenerate invite links"})
    import secrets
    team = db.get(Team, team_id)
    team.invite_code = _new_invite_code()
    db.commit()
    return {
        "ok": True,
        "invite_code": team.invite_code,
        "invite_url": f"{config.APP_BASE_URL}/join/{team.invite_code}",
    }


@api_router.post("/teams/join")
async def join_team_api(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Join a team via invite code."""
    code = (body.get("invite_code") or "").strip()
    if not _looks_like_invite_code(code):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invite code is required"})
    team = db.query(Team).filter(Team.invite_code == code).first()
    if not team:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Invalid team invite code"})
    exists = db.query(TeamMembership).filter(
        TeamMembership.team_id == team.id,
        TeamMembership.user_id == _user["id"],
    ).first()
    if exists:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Already a member of this team"})
    db.add(TeamMembership(team_id=team.id, user_id=_user["id"], role="member"))
    db.commit()
    return {"ok": True, "team": team.to_dict()}


# -- Team join requests (ask to join by naming the owner's email) --
#
# Deliberately never reveals a team name to a requester before approval, so
# knowing an owner's address proves nothing about the team behind it.

@api_router.post("/onboarding/create-team")
async def onboarding_create_team(db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Give a new account its own private workspace."""
    if _user.get("team_ids"):
        return {"ok": True, "already_onboarded": True, "redirect": "/dashboard"}
    db_user = db.query(User).filter(User.id == _user["id"]).first()
    if db_user is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})
    team = _ensure_user_tenant_membership(db, db_user, "")
    db.commit()
    return {"ok": True, "redirect": "/dashboard", "team_id": team.id if team else None}


@api_router.post("/onboarding/request-access")
async def onboarding_request_access(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    _user: dict = Depends(check_auth),
):
    """Ask to join a team by naming its creator's email address.

    Rate limited per requester, per target team, per source IP and globally.
    The response never discloses whether the address matched a team.
    """
    # Generic response used for both "sent" and "no match", so this endpoint
    # cannot be used to enumerate which emails own teams.
    generic_ok = {
        "ok": True,
        "message": "Request sent. The team owner will review it.",
    }

    owner_email = (body.get("owner_email") or "").strip().lower()
    description = _sanitize_description(body.get("description"))

    if not owner_email or "@" not in owner_email or len(owner_email) > 255:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Enter a valid email address"})
    if len(description) < DESCRIPTION_MIN_LENGTH:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Please add a short note (at least {DESCRIPTION_MIN_LENGTH} characters)"},
        )

    # --- rate limits (all four axes, checked before any write) ---
    requester_key = str(_user["id"])
    peer_ip = client_ip(request)

    for limit, key in (
        ("user_burst", requester_key),
        ("user_daily", requester_key),
        ("ip_daily", peer_ip),
        ("global_daily", "all"),
    ):
        if not _rate_check(key, limit):
            retry = _rate_retry_after(key, limit)
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error": "Too many join requests. Please try again later.",
                    "retry_after_seconds": retry,
                },
                headers={"Retry-After": str(retry)},
            )

    # Find teams whose creator matches. No name is returned to the caller.
    candidate_teams = (
        db.query(Team)
        .filter(Team.created_by_email == owner_email)
        .all()
    )

    # A requester may not ask to join a team they are already in.
    my_team_ids = set(_user.get("team_ids") or [])
    created = 0
    for team in candidate_teams:
        if team.id in my_team_ids:
            continue
        # Per-target-team limits, applied only to teams that actually exist so
        # a wrong address cannot consume another team's budget.
        if not _rate_check(str(team.id), "team_hourly"):
            retry = _rate_retry_after(str(team.id), "team_hourly")
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error": "This team has received too many requests. Please try again later.",
                    "retry_after_seconds": retry,
                },
                headers={"Retry-After": str(retry)},
            )
        if not _rate_check(str(team.id), "team_daily"):
            retry = _rate_retry_after(str(team.id), "team_daily")
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error": "This team has received too many requests. Please try again later.",
                    "retry_after_seconds": retry,
                },
                headers={"Retry-After": str(retry)},
            )

        # One open request per (team, requester): re-submitting updates the note.
        existing = (
            db.query(TeamJoinRequest)
            .filter(
                TeamJoinRequest.team_id == team.id,
                TeamJoinRequest.requester_user_id == _user["id"],
                TeamJoinRequest.status == TeamJoinRequest.STATUS_PENDING,
            )
            .first()
        )
        if existing is not None:
            existing.description = description
            existing.owner_email = owner_email
            created += 1
            continue

        db.add(TeamJoinRequest(
            team_id=team.id,
            requester_user_id=_user["id"],
            owner_email=owner_email,
            description=description,
            status=TeamJoinRequest.STATUS_PENDING,
        ))
        created += 1

    db.commit()
    # Same shape whether or not anything matched.
    return generic_ok


@api_router.get("/join-requests")
async def list_join_requests(
    team: str = None,
    db: Session = Depends(get_db),
    _user: dict = Depends(check_auth),
):
    """Pending join requests for teams the caller owns (owners only)."""
    owned_ids = [
        m.team_id
        for m in db.query(TeamMembership).filter(
            TeamMembership.user_id == _user["id"],
            TeamMembership.role == "owner",
        )
    ]
    if not owned_ids:
        return {"requests": [], "pending_count": 0}

    query = db.query(TeamJoinRequest).filter(TeamJoinRequest.team_id.in_(owned_ids))
    if team:
        try:
            wanted = int(team)
        except (TypeError, ValueError):
            wanted = -1
        if wanted not in owned_ids:
            return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
        query = query.filter(TeamJoinRequest.team_id == wanted)

    rows = query.order_by(TeamJoinRequest.created_at.desc()).limit(200).all()

    requester_ids = {r.requester_user_id for r in rows}
    requesters = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(requester_ids or [0])).all()
    }

    items = []
    for r in rows:
        requester = requesters.get(r.requester_user_id)
        items.append({
            **r.to_dict(),
            "requester_name": (requester.name or requester.email) if requester else "(removed user)",
            "requester_email": requester.email if requester else "",
        })
    pending = sum(1 for r in rows if r.status == TeamJoinRequest.STATUS_PENDING)
    return {"requests": items, "pending_count": pending}


@api_router.post("/join-requests/{request_id}/{decision}")
async def decide_join_request(
    request_id: int,
    decision: str,
    db: Session = Depends(get_db),
    _user: dict = Depends(check_auth),
):
    """Approve or deny a join request. Only an owner of that team may decide."""
    if decision not in ("approve", "deny"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Unknown decision"})

    join_request = db.get(TeamJoinRequest, request_id)
    if join_request is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Request not found"})

    # Authorisation is re-derived from the DB, never from the request body.
    membership = (
        db.query(TeamMembership)
        .filter(
            TeamMembership.team_id == join_request.team_id,
            TeamMembership.user_id == _user["id"],
            TeamMembership.role == "owner",
        )
        .first()
    )
    if membership is None:
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only the team owner can decide this request"})

    if join_request.status != TeamJoinRequest.STATUS_PENDING:
        return JSONResponse(status_code=400, content={"ok": False, "error": "That request has already been decided"})

    if decision == "approve":
        already = (
            db.query(TeamMembership)
            .filter(
                TeamMembership.team_id == join_request.team_id,
                TeamMembership.user_id == join_request.requester_user_id,
            )
            .first()
        )
        if already is None:
            db.add(TeamMembership(
                team_id=join_request.team_id,
                user_id=join_request.requester_user_id,
                role="member",
            ))
        join_request.status = TeamJoinRequest.STATUS_APPROVED
    else:
        join_request.status = TeamJoinRequest.STATUS_DENIED

    join_request.decided_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": join_request.status}


# -- Image uploads --

@api_router.post("/upload")
async def upload_image(file: UploadFile = File(...), _user: dict = Depends(check_auth)):
    """Upload an image for a listing.

    Stored to local disk or an S3 bucket depending on UPLOAD_BACKEND. The
    returned URL is what the front-end should use, so callers do not need to
    know which backend is active.
    """
    import os

    if not file.filename:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No file uploaded"})

    sanitized_filename = os.path.basename(file.filename)
    ext = os.path.splitext(sanitized_filename)[1].lower()
    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if ext not in allowed_exts:
        # HEIC is called out separately because it is the most common real
        # case: an iPhone set to "High Efficiency" photographs in HEIC, which
        # the standard library cannot decode, so the file must be converted
        # before it can be accepted. Without this the user just sees a generic
        # "unsupported extension" and has no idea why.
        if ext in {".heic", ".heif"}:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "HEIC photos aren't supported. On iOS, Settings → Camera → "
                             "Formats → 'Most Compatible' makes the camera shoot JPEG. "
                             "Otherwise convert the photo before uploading.",
                },
            )
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Unsupported image extension '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}"},
        )

    MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
    contents = await file.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        return JSONResponse(status_code=413, content={"ok": False, "error": "File size exceeds the 15 MB limit"})
    if len(contents) == 0:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Uploaded file is empty"})

    # Validate image header magic bytes to prevent polyglot / malicious payloads
    def is_valid_image(data: bytes, extension: str) -> bool:
        if extension in {".jpg", ".jpeg"}:
            return data.startswith(b"\xff\xd8\xff")
        if extension == ".png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == ".gif":
            return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")
        if extension == ".webp":
            return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        if extension == ".bmp":
            return data.startswith(b"BM")
        return False

    if not is_valid_image(contents, ext):
        return JSONResponse(status_code=400, content={"ok": False, "error": "File content does not match a valid image format"})

    # Storage location is pluggable: local disk, or an S3 bucket. Validation
    # above is unchanged either way.
    try:
        url = storage.save_image(contents, ext)
    except storage.StorageError as exc:
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"Could not store the image — {exc}"},
        )

    return {"ok": True, "url": url}


# -- Listings (creation, update, delete, local sales) --

@api_router.post("/listings/manual")
async def create_manual_listing(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Create a manual listing or local sale entry directly in the dashboard."""
    import secrets
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Title is required"})

    price_cents = body.get("price_cents")
    if price_cents is None:
        try:
            price_val = float(body.get("price") or body.get("price_usd") or 0)
            price_cents = int(round(price_val * 100))
        except (ValueError, TypeError):
            price_cents = 0

    currency = (body.get("currency") or config.DEFAULT_CURRENCY or "CAD").upper().strip()

    # Platforms: array of cross-listed channels (e.g. ["ebay", "etsy", "facebook"])
    platforms_raw = body.get("platforms")
    cleaned_platforms = []
    if isinstance(platforms_raw, list):
        for p in platforms_raw:
            if isinstance(p, str) and p.strip():
                cleaned_platforms.append(p.strip().lower())
    if not cleaned_platforms:
        cleaned_platforms = [(body.get("platform") or "local").lower().strip()]
    platform = cleaned_platforms[0]

    status = (body.get("status") or "active").lower().strip()
    is_sold = status == "sold" or bool(body.get("is_sold"))
    quantity = int(body.get("quantity") or (0 if is_sold else 1))

    # Cost & Investment tracking (what we bought it for + parts/repairs)
    purchase_price_cents = body.get("purchase_price_cents")
    if purchase_price_cents is None:
        try:
            purchase_val = float(body.get("purchase_price") or body.get("cost") or 0)
            purchase_price_cents = int(round(purchase_val * 100))
        except (ValueError, TypeError):
            purchase_price_cents = 0

    parts_raw = body.get("parts") or []
    cleaned_parts = []
    parts_cost_cents = 0
    if isinstance(parts_raw, list):
        for p in parts_raw:
            if not isinstance(p, dict):
                continue
            desc = (p.get("description") or p.get("name") or "").strip()
            if not desc:
                continue
            c_cents = p.get("cost_cents")
            if c_cents is None:
                try:
                    c_cents = int(round(float(p.get("cost") or 0) * 100))
                except (ValueError, TypeError):
                    c_cents = 0
            else:
                c_cents = int(c_cents)
            cleaned_parts.append({
                "description": desc,
                "cost_cents": c_cents,
                "cost": round(c_cents / 100, 2),
            })
            parts_cost_cents += c_cents

    user_team_ids = _user.get("team_ids") or []
    if not user_team_ids:
        return JSONResponse(status_code=400, content={"ok": False, "error": "You must belong to a team to create listings"})

    team_id = body.get("team_id")
    if team_id:
        try:
            team_id = int(team_id)
            if team_id not in user_team_ids:
                return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
        except (ValueError, TypeError):
            team_id = None
    if not team_id:
        team_id = user_team_ids[0]

    sku = (body.get("sku") or "").strip()
    description = (body.get("description") or "").strip()
    category = (body.get("category") or "Local Sales").strip()
    image_url = (body.get("image_url") or "").strip() or "/static/img/placeholder.svg"

    platform_listing_id = sku or f"LOCAL-{secrets.token_hex(4).upper()}"

    listing = Listing(
        platform=platform,
        platforms_json=cleaned_platforms,
        platform_listing_id=platform_listing_id,
        title=title,
        description=description,
        price_cents=price_cents,
        price_raw=f"{currency} {price_cents / 100:.2f}",
        currency=currency,
        purchase_price_cents=purchase_price_cents,
        parts_cost_cents=parts_cost_cents,
        parts_json=cleaned_parts,
        status="sold" if is_sold else status,
        is_sold=is_sold,
        sold_at=datetime.utcnow() if is_sold else None,
        available_quantity=quantity,
        views_count=1,
        image_url=image_url,
        images_json=[image_url],
        category=category,
        sku=sku,
        team_id=team_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {"ok": True, "listing": listing.to_dict()}


def _check_listing_access(listing, _user: dict, db: Session) -> bool:
    """Return True if caller has authorized access to this listing within their tenant teams."""
    if not listing:
        return False
    user_team_ids = _user.get("team_ids") or []
    # If unassigned legacy listing and caller is admin, claim it into admin team
    if not listing.team_id and (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        listing.team_id = user_team_ids[0]
        db.commit()
    return bool(listing.team_id and listing.team_id in user_team_ids)


@api_router.post("/listings/{listing_id}/mark-sold")
async def mark_listing_sold(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Quickly mark a listing as sold (e.g. for a local in-person cash sale)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "sold"
    listing.is_sold = True
    listing.sold_at = datetime.utcnow()
    listing.available_quantity = max(0, listing.available_quantity - 1)
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.post("/listings/{listing_id}/write-off")
async def write_off_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Mark a listing as written off (unsellable, damaged, obsolete inventory loss)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "written_off"
    listing.is_sold = False
    listing.written_off_at = datetime.utcnow()
    listing.available_quantity = 0
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.post("/listings/{listing_id}/restore")
async def restore_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Restore a written-off or ended listing back to active status."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "active"
    listing.is_sold = False
    listing.written_off_at = None
    if listing.available_quantity <= 0:
        listing.available_quantity = 1
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.delete("/listings/{listing_id}")
async def delete_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Delete a listing from the dashboard inventory."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    db.delete(listing)
    db.commit()
    return {"ok": True}


@api_router.put("/listings/{listing_id}")
async def update_listing(listing_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Update a listing (team assignment, purchase price, parts & repairs, price)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})
    user_team_ids = _user.get("team_ids") or []

    if "team_id" in body:
        new_team = body["team_id"]
        if new_team is None:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Listings must be assigned to a valid team"})
        try:
            new_team = int(new_team)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"ok": False, "error": "team_id must be a number"})
        if new_team not in user_team_ids:
            return JSONResponse(status_code=404, content={"ok": False, "error": "Target team not found or unauthorized"})
        listing.team_id = new_team

    if "purchase_price" in body or "purchase_price_cents" in body:
        purchase_cents = body.get("purchase_price_cents")
        if purchase_cents is None:
            try:
                purchase_val = float(body.get("purchase_price") or 0)
                purchase_cents = int(round(purchase_val * 100))
            except (ValueError, TypeError):
                purchase_cents = 0
        listing.purchase_price_cents = max(0, int(purchase_cents))

    if "parts" in body:
        parts_raw = body.get("parts") or []
        cleaned_parts = []
        parts_cost_cents = 0
        if isinstance(parts_raw, list):
            for p in parts_raw:
                if not isinstance(p, dict):
                    continue
                desc = (p.get("description") or p.get("name") or "").strip()
                if not desc:
                    continue
                c_cents = p.get("cost_cents")
                if c_cents is None:
                    try:
                        c_cents = int(round(float(p.get("cost") or 0) * 100))
                    except (ValueError, TypeError):
                        c_cents = 0
                else:
                    c_cents = int(c_cents)
                cleaned_parts.append({
                    "description": desc,
                    "cost_cents": c_cents,
                    "cost": round(c_cents / 100, 2),
                })
                parts_cost_cents += c_cents
        listing.parts_cost_cents = parts_cost_cents
        listing.parts_json = cleaned_parts

    if "price" in body or "price_cents" in body:
        p_cents = body.get("price_cents")
        if p_cents is None:
            try:
                p_val = float(body.get("price") or 0)
                p_cents = int(round(p_val * 100))
            except (ValueError, TypeError):
                p_cents = listing.price_cents
        listing.price_cents = max(0, int(p_cents))
        curr = listing.currency or config.DEFAULT_CURRENCY or "CAD"
        listing.price_raw = f"{curr} {listing.price_cents / 100:.2f}"

    if "platforms" in body:
        platforms_raw = body.get("platforms") or []
        cleaned_platforms = []
        if isinstance(platforms_raw, list):
            for p in platforms_raw:
                if isinstance(p, str) and p.strip():
                    cleaned_platforms.append(p.strip().lower())
        listing.platforms_json = cleaned_platforms
        if cleaned_platforms:
            listing.platform = cleaned_platforms[0]

    if "image_url" in body:
        img_val = (body.get("image_url") or "").strip()
        listing.image_url = img_val or None

    if "status" in body:
        new_status = (body.get("status") or "").lower().strip()
        if new_status:
            listing.status = new_status
            if new_status == "sold":
                if not listing.is_sold:
                    listing.is_sold = True
                    listing.sold_at = datetime.utcnow()
            elif listing.is_sold:
                listing.is_sold = False
                listing.sold_at = None

    if "is_sold" in body:
        new_is_sold = bool(body["is_sold"])
        if new_is_sold and not listing.is_sold:
            listing.is_sold = True
            listing.sold_at = datetime.utcnow()
            listing.status = "sold"
        elif not new_is_sold and listing.is_sold:
            listing.is_sold = False
            listing.sold_at = None
            if listing.status == "sold":
                listing.status = "active"

    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}

# -- Plugins info --

@api_router.get("/plugins")
async def get_plugins(_auth: bool = Depends(check_auth)):
    """Return metadata about all loaded plugins."""
    from src.plugins import get_loaded_plugins
    return get_loaded_plugins()