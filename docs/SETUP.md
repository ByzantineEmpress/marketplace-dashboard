# Setup Guide

Three ways to run the dashboard: one-click (recommended on Windows),
manual, or Docker. After installation, connect your marketplace accounts
and (optionally) enable Google Sign-In.

---

## Option 1 — One-click launcher (Windows)

1. Make sure **start-marketplace-dashboard.bat** is on your Desktop.
2. Double-click it. The launcher will:
   - find a real Python install (skips the Microsoft Store stubs; falls
     back to the Python bundled with Hermes Agent),
   - create a virtual environment inside the project,
   - install dependencies from `requirements.txt`,
   - copy `.env.example` → `.env` if `.env` doesn't exist yet,
   - start the server on port 8000.
3. Open **http://localhost:8000**.

Log in with `admin` / `changeme123` (defaults — change them in
**Admin → Settings**).

Press **Ctrl+C** in the launcher window to stop the server.

---

## Option 2 — Manual install (any OS)

Prerequisites: Python 3.11+.

```bash
cd ~/Projects/marketplace-dashboard     # (or wherever the project lives)

python -m venv venv
venv\Scripts\activate                   # Windows
# source venv/bin/activate              # macOS/Linux

pip install -r requirements.txt

copy .env.example .env                  # Windows (cp on macOS/Linux)
# edit .env with your settings — see CONFIGURATION.md

uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Then open **http://localhost:8000**.

---

## Option 3 — Docker

```bash
cp .env.example .env        # edit with your settings
docker compose up --build
```

Serves on port 8000 through the bundled Nginx config
(`nginx/nginx.conf`).

---

## Connecting eBay

1. Go to the [eBay Developer Portal](https://developer.ebay.com/) and
   sign in with your eBay seller account.
2. Create an app (**My Apps → New App**) of type **Selling API**:
   - **App Name:** anything (e.g. "Marketplace Dashboard")
   - **Redirect URI:** `http://localhost:8000/api/auth/ebay/callback`
     (change to your server URL if you deploy remotely)
3. Copy the **Client ID** and **Client Secret** into `.env`:

   ```
   EBAY_CLIENT_ID=your_ebay_client_id
   EBAY_CLIENT_SECRET=your_ebay_client_secret
   EBAY_REDIRECT_URI=http://localhost:8000/api/auth/ebay/callback
   ```

   (or paste them into **Admin → Marketplace Accounts → eBay** — the
   admin page writes them to `.env` for you).

---

## Connecting Etsy

1. Go to [Etsy Developers](https://www.etsy.com/developers) and sign in
   with your Etsy seller account → **Your Apps**.
2. **Create a New App**, choose **Seller App** (access to your own shop):
   - **App Name:** anything
   - **Redirect URI:** `http://localhost:8000/api/auth/etsy/callback`
3. Copy the credentials into `.env`:

   ```
   ETSY_API_KEY=your_etsy_keystring
   ETSY_API_SECRET=your_etsy_shared_secret
   ETSY_REDIRECT_URI=http://localhost:8000/api/auth/etsy/callback
   ```

---

## Finishing up

1. Restart the app after any `.env` change.
2. Open **Admin → Marketplace Accounts** and click **Connect** for each
   marketplace, then authorise on the marketplace's page.
3. Click **Sync** — your listings appear on the dashboard.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "No valid access token" | Click **Connect** in Admin and finish the OAuth flow |
| OAuth redirect fails | The `*_REDIRECT_URI` in `.env` must exactly match the developer portal (including http/https and port) |
| Listings not appearing | Click **Sync** in Admin, check the sync log in the server console |
| API rate limits hit | The app backs off automatically — wait a few minutes |
| `EBAY_CLIENT_SECRET not found` | eBay's developer portal moved — re-check the app in the new portal |
| Launcher says "Python not found" | Install Python 3.11+ from python.org with "Add to PATH" checked |
| Port 8000 already in use | Change `APP_PORT` in `.env` and use the new port |

---

## Future marketplaces

New marketplaces can be added as plugins (drop a `.py` in `plugins/`):
Poshmark, Facebook Marketplace, Mercari, Depop are the candidates.
See [PLUGINS.md](PLUGINS.md).
