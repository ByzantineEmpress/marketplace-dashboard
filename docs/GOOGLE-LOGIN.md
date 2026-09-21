# Google Sign-In Setup

The login page has a **"Sign in with Google"** button. It works out of the
box with placeholder credentials (handy for testing the flow), but to sign
in with *your* Google account you need one OAuth client in your Google
Cloud project. It takes about 10 minutes, free tier.

## 1. Create a Google Cloud project

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and
   sign in with the Google account that will own the project.
2. Click the project dropdown (top bar) → **New Project** → any name
   (e.g. "Marketplace Dashboard") → **Create**.
3. Make sure the new project is selected in the dropdown.

## 2. Configure the OAuth consent screen

1. In the left menu: **APIs & Services → OAuth consent screen**
   (or **Google Auth Platform** in the new console).
2. User type: **External** (fine for personal use).
3. Fill in: app name ("Marketplace Dashboard"), your email, and a
   developer contact email.
4. Scopes: add **openid**, **email**, **profile** (if prompted — some
   console versions ask per scope).
5. **Publish App**: pick **Publishing App** (Production). If you keep it in
   **Testing**, only accounts you add as *Test users* can sign in, and
   sessions expire after 8 hours.

## 3. Create the OAuth client

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Name: anything (e.g. "Marketplace Dashboard Web").
4. **Authorized redirect URIs** — add exactly:

   ```
   http://localhost:8000/auth/google/callback
   ```

   (If `APP_BASE_URL` in your `.env` is different, use
   `APP_BASE_URL` + `/auth/google/callback` instead.)
5. Click **Create** → copy both the **Client ID** and the **Client
   secret**.

## 4. Enter the credentials in the app

Either:

- **Admin → Settings → Google Sign-In** — paste Client ID and Secret,
  optionally add `GOOGLE_ALLOWED_EMAILS` (comma-separated emails allowed
  to sign in). The page writes them to `.env` for you.
- or edit `.env` yourself:

  ```
  GOOGLE_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
  GOOGLE_CLIENT_SECRET=GOCSPX-...
  GOOGLE_ALLOWED_EMAILS=you@gmail.com,partner@gmail.com
  ```

  then restart the server.

## 5. Try it

Refresh the login page → **Sign in with Google** → pick your account →
you land on the dashboard as that user. Each Google account becomes a
separate user in the app (add them to a Team in Admin to share inventory).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error 400: redirect_uri_mismatch` | The callback URL the app sends (`APP_BASE_URL/auth/google/callback`) is not on the client's redirect-URI list — must match character for character, including `http` vs `https` and the port |
| `Error 401: invalid_client` | Wrong client ID or secret in `.env` — copy again from Credentials |
| `Error 403: access_denied` / consent screen loop | App is in **Testing** and this Google account isn't a Test user — add it under **Auth Platform → Test users**, or publish the app |
| Signed in, then "invalid_session" after restart | `SECRET_KEY` was auto-generated per startup — set a permanent one in `.env` (see CONFIGURATION.md) |
| Button not visible at all | `GOOGLE_CLIENT_ID` is empty in `.env` |
| Another Google account got in that shouldn't | Set `GOOGLE_ALLOWED_EMAILS` to your list |
