# Security Notes

The app is built for **self-hosted use on your own machine or a private
server** — the threat model is "a browser you control, talking to a server
you control". The mapping below is against the **OWASP Top 10 (2021)** so
you can see what's covered and where the honest gaps are.

| OWASP category | How the app handles it |
|---|---|
| **A01 Broken Access Control** | Every `/api/*` route requires a session (`check_auth` dependency); admin-only actions check the user's role; team endpoints check membership (403 otherwise). Verified by `tests/test_teams.py` (e.g. outsider → 403 on a team's listings) |
| **A02 Cryptographic Failures** | Passwords hashed with **bcrypt** (passlib) — never stored in plain text; marketplace tokens stored in the `encrypted_credentials` table using the `cryptography` library; `SECRET_KEY` signs the session cookie (auto-generated if unset) |
| **A03 Injection** | All SQL goes through **SQLAlchemy** (parameterised queries — no string-built SQL); templates use Jinja2 auto-escaping for XSS at render time |
| **A04 Insecure Design** | Secrets live in `.env` (git-ignored) and in the DB, not in code; the UI-managed settings whitelist means the Admin page can only touch known keys; sessions are DB-backed rows, so logout/revocation is real |
| **A05 Security Misconfiguration** | `APP_DEBUG` off by default; CORS/Origin restricted to `ALLOWED_ORIGINS`; `REQUIRE_HTTPS` for reverse-proxy deployments; `.gitignore` keeps `.env`, `venv/`, `*.db` out of the repo |
| **A06 Vulnerable Components** | `requirements.txt` pins exact versions (`fastapi==0.115.0`, …) — no surprise upgrades; update deliberately |
| **A07 Auth Failures** | Two login paths: local password (bcrypt) and Google OIDC (verified against Google's tokens); `GOOGLE_ALLOWED_EMAILS` can restrict sign-in to your accounts; session cookie is `HttpOnly`; API routes return 401, pages redirect to login |
| **A08 Software & Data Integrity** | Plugins are plain local `.py` files you placed in `plugins/` (imported by name); `persist_env()` rewrites `.env` in place, preserving comments; DB is a single local SQLite file |
| **A09 Logging & Monitoring** | Marketplace sync writes a sync log visible in the server console; errors surface in the UI's error states. (No central log aggregation yet — fine at this scale) |
| **A10 SSRF** | Outbound calls go to pinned marketplace endpoints (eBay/Etsy APIs) over HTTPS — the user's config can't point the app at arbitrary hosts |

## Extra hardening already in place

- **CSP + security headers** on HTML responses (XSS defence in depth).
- **Rate limiting** on API endpoints to slow brute force.
- **Origin checks** on state-changing calls (CSRF mitigation for the
  cookie-based sessions).
- **No build step / no CDN** — the frontend ships from your server, so a
  compromised CDN can't inject scripts.

## Honest gaps to know about

- **SQLite** is fine for one server and a handful of users; under heavy
  concurrent writes you'd want Postgres.
- **Plugin code runs with the server's privileges** — like any plugin
  system, a bad plugin can do anything the process can. Only load plugins
  you trust.
- **Plain HTTP on `localhost`** is normal for local use; use
  `REQUIRE_HTTPS=true` + Nginx (bundled) when exposing to the internet.
- **`SECRET_KEY` auto-generation**: if you never set one, sessions reset on
  every server restart (annoying, not a leak). Set it permanently in `.env`
  to avoid it.

## Things to do when you expose it publicly

1. Set a real `SECRET_KEY` and a real admin password.
2. `REQUIRE_HTTPS=true`, serve behind the bundled Nginx config.
3. Set `ALLOWED_ORIGINS` to your real domain.
4. Restrict `GOOGLE_ALLOWED_EMAILS`.
5. Update pinned dependencies periodically.
