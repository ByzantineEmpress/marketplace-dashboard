# Architecture

A single-process FastAPI app: Python backend + Jinja2 pages + vanilla JS
frontend, all served by one Uvicorn process on port 8000. No build step,
no separate database server.

## Directory map

```
src/
├── config.py        # Config dataclass; loads .env at import time
├── database.py      # SQLAlchemy engine/session (SQLite by default)
├── models.py        # All ORM models (the whole data model, ~360 lines)
├── api/
│   ├── main.py      # FastAPI app factory: middleware, routers, startup
│   └── routes.py    # ALL routes — pages + /api/* (one readable file)
├── adapters/
│   ├── base.py      # MarketplaceAdapter ABC: OAuth, sync, listing shape
│   ├── ebay.py      # eBay implementation
│   └── etsy.py      # Etsy implementation
└── plugins/         # Plugin loader (importlib, scans plugins/)

static/              # css/ + js/ — plain files, no bundler
templates/           # base.html, login.html, dashboard.html, admin.html
plugins/             # user plugins (*.py); price_alerts.py ships as example
tests/               # test scripts (run against the live server)
nginx/               # reverse-proxy config for Docker deployment
```

## Data model (`src/models.py`)

| Table | Purpose |
|---|---|
| `users` | App users (local or Google-created); `provider` marks the login path |
| `auth_sessions` | DB-backed session rows (token ↔ user) — logout/revocation is real |
| `app_settings` | Settings persisted in the DB alongside `.env` |
| `encrypted_credentials` | Marketplace API tokens, encrypted at rest |
| `listings` | Every listing from every marketplace; `owner` (user) and `team` (nullable) scopes it |
| `marketplace_accounts` | Per-platform account connections (status, tokens) |
| `teams` | Teams (name, owner) |
| `team_members` | user ↔ team rows with a `role` (`owner`/`member`) |
| `run_state` | Last-sync timestamps and run bookkeeping |

**Team semantics in one line:** a listing is *personal* when it belongs to
a user's own team, and *shared* when its `team` points to a team the user
is a member of — dashboard stats sum over whichever set the team filter
selects.

## Request flow

```
Browser
  │  GET /dashboard            (Jinja2 page, needs session cookie)
  ▼
FastAPI page route ──► templates/dashboard.html + static/js/*.js
  │
  │  GET /api/listings?team=2  (JSON, session cookie)
  ▼
routes.py  ── check_auth → team membership check ──► models → JSON
  │
  │  POST /api/accounts/sync
  ▼
adapters/{platform}.py ──► marketplace REST API (httpx) ──► listings table
```

## The adapter pattern

`src/adapters/base.py` defines `MarketplateAdapter`: `PLATFORM`,
`PLATFORM_LABEL`, OAuth connect/callback handling, and a `sync()` that must
return listings in one common shape. `ebay.py` and `etsy.py` implement it.
A new marketplace = one new subclass + registration in `main.py`.

## Plugin loading

`src/plugins/__init__.py` scans `plugins/` for `.py` files (or the
`ENABLED_PLUGINS` whitelist). A plugin is a module defining
`PLUGIN_NAME`, `PLUGIN_DESC`, `PLUGIN_VERSION` and optionally
`register(app)`, which may add routes/templates. Loading is deliberately
dumb (import + call) — no sandbox, so plugins run with the server's
privileges. See PLUGINS.md.

## Configuration

`config.py` calls `load_dotenv()` at import time, then copies any env vars
matching `Config` attributes into them (with type coercion: bool/int/list).
Real environment variables always win over `.env`. The Admin page writes
back to `.env` through `persist_env()`, so UI edits survive restarts.

## Where the tests fit

| Test | What it proves |
|---|---|
| `tests/test_ui_wiring.py` | Static: every `getElementById` in the JS has a matching `id=` in the templates (the #1 UI wiring bug) |
| `tests/test_teams.py` | API end-to-end against the live server: login, teams, membership, team-scoped listings/stats, 403s for outsiders, session persistence |
| `tests/ui_smoke_test.js` | Real Chromium (Playwright): pages render, Google button present, admin + dashboard work, zero JS console errors; saves screenshots to `docs/images/` |
