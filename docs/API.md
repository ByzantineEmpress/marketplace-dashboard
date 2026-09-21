# API Reference

Base URL: `http://localhost:8000` (or your `APP_BASE_URL`).

**Authentication** — everything under `/api/` (except login) requires a
session. Sessions are set by:

- `POST /api/auth/login` (username + password), or
- the Google flow: `GET /auth/google` → Google → `GET /auth/google/callback`

The session is carried in an `HttpOnly` cookie — API clients can use
`curl -c cookies.txt ...` or any cookie jar. Unauthenticated API calls
return `401`; calls by the wrong team/role return `403`.

## Authentication

| Method | Path | Body / notes |
|---|---|---|
| POST | `/api/auth/login` | `{"username": "...", "password": "..."}` (JSON **or** form-encoded). Accepts the local admin account and any local user |
| POST | `/api/auth/logout` | Ends the session |
| GET | `/auth/google` | Redirects to Google's consent screen |
| GET | `/auth/google/callback` | Google posts back here; the app issues a session and redirects to `/dashboard` |
| GET | `/api/auth/{platform}/callback` | OAuth callback for marketplace accounts (`ebay`, `etsy`) |

## Settings

| Method | Path | Notes |
|---|---|---|
| GET | `/api/settings` | Returns the UI-managed settings (admin, Google, base URL, refresh interval) |
| POST | `/api/settings` | Body: `{"KEY": "value", ...}` — only these keys are accepted: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_EMAILS`, `APP_BASE_URL`, `DEFAULT_REFRESH_INTERVAL_S`. Writes to `.env` so it survives restart |

## Listings

| Method | Path | Notes |
|---|---|---|
| GET | `/api/listings` | Query params: `platform` (`ebay`/`etsy`), `status`, `search` (keyword), `team` (team id — scopes results to that team's listings), `page` (default 1), `page_size` (default 50), `sort` (default `created_at`), `order` (`asc`/`desc`) |
| PUT | `/api/listings/{listing_id}` | Body: `{"team_id": 2}` moves the listing into team 2; `{"team_id": null}` makes it shared (not team-scoped) |

## Stats

| Method | Path | Notes |
|---|---|---|
| GET | `/api/stats` | Optional `?team=<id>` — dashboard header numbers (active, sold, value, stock). Without `team`: the user's own listings; with it: that team's combined figures |

## Marketplace accounts

| Method | Path | Notes |
|---|---|---|
| GET | `/api/accounts` | Connected eBay/Etsy accounts + status |
| POST | `/api/accounts/connect` | Body: `{"platform": "ebay"}` or `{"platform": "etsy"}` — returns the authorisation URL to open in the browser |
| POST | `/api/accounts/sync` | Pulls fresh listings from the connected platform(s) |

## Teams

| Method | Path | Notes |
|---|---|---|
| GET | `/api/teams` | Teams the **current user** belongs to, each with its members (user + role: `owner`/`member`) |
| POST | `/api/teams` | Body: `{"name": "Family Flips"}` — creator becomes owner |
| POST | `/api/teams/{team_id}/members` | Body: `{"user_id": 2}` — add a user to the team |
| DELETE | `/api/teams/{team_id}/members/{user_id}` | Remove a member |

## Plugins

| Method | Path | Notes |
|---|---|---|
| GET | `/api/plugins` | Loaded plugins with name/description/version/status |

## HTML pages (for completeness)

| Path | Purpose |
|---|---|
| `/login` | Login form + Google button |
| `/dashboard` | Main dashboard (team filter, listings, stats) |
| `/admin` | Settings, accounts, users, teams, plugins |

Page routes redirect to `/login` when unauthenticated; API routes return
`401` instead.

## Error shape

```json
{ "detail": "Not a member of this team" }
```

Status codes used: `200` OK · `201` created · `400` bad body · `401` not
authenticated · `403` not a member / wrong role · `404` not found ·
`500` server error.
