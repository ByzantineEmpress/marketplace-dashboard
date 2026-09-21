# Configuration Reference

Everything the app reads comes from the `.env` file in the project root
(`C:\Users\YourUser\Projects\marketplace-dashboard\.env`).

Two ways to edit settings:

- **Admin → Settings page** — edits the "UI-managed" keys below and writes
  them straight into `.env` (survives restarts, no file editing).
- **Edit `.env` by hand** — for every other key. Restart the server after
  a change.

The app loads `.env` at startup and never overrides real environment
variables (useful for Docker).

## Key reference

| Key | Default | UI-managed | What it does |
|---|---|---|---|
| `ADMIN_USERNAME` | `admin` | ✅ | Username for the local password login |
| `ADMIN_PASSWORD` | `changeme123` | ✅ | Password for the local login (hashed with bcrypt) |
| `GOOGLE_CLIENT_ID` | *(empty)* | ✅ | Google OAuth client ID — empty = button disabled |
| `GOOGLE_CLIENT_SECRET` | *(empty)* | ✅ | Google OAuth client secret |
| `GOOGLE_ALLOWED_EMAILS` | *(empty = all)* | ✅ | Comma-separated Google emails allowed to sign in |
| `APP_BASE_URL` | `http://localhost:8000` | ✅ | Base URL the browser uses; Google's redirect URI must be `APP_BASE_URL/auth/google/callback` |
| `DEFAULT_REFRESH_INTERVAL_S` | `300` | ✅ | How often the dashboard auto-refreshes (seconds) |
| `APP_HOST` | `0.0.0.0` | | Bind address for the server |
| `APP_PORT` | `8000` | | Port the server listens on |
| `APP_DEBUG` | `false` | | Debug mode (verbose errors — keep off in production) |
| `DATABASE_URL` | `sqlite:///./marketplace.db` | | Database connection string (SQLite file in project root) |
| `SECRET_KEY` | *(auto-generated)* | | Signs the session cookie. Auto-generated randomly if left at the default — set it explicitly to keep sessions valid across restarts |
| `EBUY_CLIENT_ID` | *(empty)* | | eBay OAuth client ID |
| `EBUY_CLIENT_SECRET` | *(empty)* | | eBay OAuth client secret |
| `EBUY_APP_ID` | *(empty)* | | eBay APP ID (needed by some v1 API calls) |
| `ESY_API_KEY` | *(empty)* | | Etsy keystring |
| `ESY_API_SECRET` | *(empty)* | | Etsy shared secret |
| `ALLOWED_ORIGINS` | `http://localhost` | | Comma-separated origins allowed by CORS/Origin checks |
| `REQUIRE_HTTPS` | `false` | | Set `true` when serving behind an Nginx proxy over HTTPS |
| `PLUGIN_DIR` | `plugins` | | Folder scanned for plugin `.py` files |
| `ENABLED_PLUGINS` | *(empty = all)* | | Comma-separated plugin names to load (whitelist) |
| `MAX_CACHED_LISTINGS` | `10000` | | Cap on cached listings |

## Notes

- **`GOOGLE_ALLOWED_EMAILS`** — the cleanest way to keep your Google
  sign-in private: list only your own email (and your team's). Empty means
  *any* Google account can log in and be added as a user.
- **`SECRET_KEY`** — if you leave it unset the app generates a random one
  at each startup; sessions then die on restart. Generate one permanently
  with: `python -c "import secrets; print(secrets.token_hex(32))"` and put
  it in `.env`.
- **Keeping Google in sync** — if you change `APP_BASE_URL` (e.g. deploy
  to a server with a domain), add the new
  `https://yourdomain/auth/google/callback` to the Google OAuth client's
  redirect URIs (see GOOGLE-LOGIN.md).
- Keys are matched case-sensitively; `APP_PORT` is read as an integer,
  booleans accept `true/1/yes`, lists are comma-separated.
