# Deploying

The app is a long-running Python process with a **writable filesystem**, so it
needs a real container host — not a static/serverless platform.

## Why not Cloudflare Pages / Workers

Cloudflare Pages serves static files plus JavaScript Workers functions, and
Workers' Python support is Pyodide/WASM, not CPython. Neither offers a
persistent filesystem. This app needs:

| Requirement | Where it is used |
|---|---|
| SQLite file + WAL | `marketplace.db`, `-wal`, `-shm` |
| Uploaded images | `static/uploads/` (or S3 — see below) |
| Writable `.env` | **Admin → Settings saves credentials via `persist_env()`** |
| Backups | `backups/` |

That third row catches people out: saving API keys from the Admin page writes
a file. On App Runner / Fargate / Lambda the filesystem is ephemeral (read-only
on Lambda), so settings would silently revert on every restart.

**Cloudflare is still the right front door** — keep the domain there for DNS,
TLS, caching and DDoS protection, and point it at an origin that can run this.

## Recommended: Lightsail (or any small VPS / EC2)

1. Create an instance (the smallest bundle is plenty) with a static IP.
2. Install Docker and the Compose plugin.
3. Clone the repo and create `.env` from `.env.example`.
4. Set at minimum:
   ```
   APP_BASE_URL=https://your-domain.com
   ALLOWED_ORIGINS=https://your-domain.com
   REQUIRE_HTTPS=true
   ADMIN_PASSWORD=<something strong>
   SECRET_KEY=<64 hex chars>
   MAIL_BACKEND=http
   MAIL_PROVIDER=resend
   MAIL_API_KEY=re_...
   MAIL_FROM=Marketplace Dashboard <noreply@send.yourdomain.com>
   UPLOAD_BACKEND=local      # or s3, see below
   ```
5. `docker compose up -d --build`

### ⚠️ Restrict the origin to Cloudflare

The app trusts `CF-Connecting-IP` / `X-Forwarded-For` to identify clients (that
is what makes rate limiting work behind a proxy). If the origin is reachable
directly, anyone can forge those headers and **evade every rate limit**.

Either:

* use a **Cloudflare Tunnel** (origin has no public inbound port), or
* firewall port 8000 to [Cloudflare's published IP ranges](https://www.cloudflare.com/ips/).

Do not skip this. The app cannot protect you from it.

## Uploads: local disk vs S3

`UPLOAD_BACKEND=local` writes to `static/uploads/`. That is fine when the path
is on a persistent volume, as it is with the compose bind-mount.

On any host with an ephemeral filesystem, use S3:

```
UPLOAD_BACKEND=s3
S3_BUCKET=my-marketplace-images
S3_REGION=us-east-1
# Leave the credentials blank on EC2/Lightsail to use an instance role.
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_PUBLIC_BASE_URL=          # optional CDN / custom domain
```

The bucket needs `s3:PutObject` for the app, and objects must be readable by
the browser — either a public-read bucket policy or a CDN in front. Prefer an
**instance role** over long-lived keys: they cannot leak from `.env`.

## Rate limiting and worker count

Counters are stored in the `rate_limit_events` table, not in process memory,
so limits hold with any number of uvicorn workers or containers. This matters:
an in-memory counter is per-worker, which silently multiplies every limit by
the worker count.

If SQLite write contention ever becomes a problem, replace the two functions
`_rate_check` / `_rate_retry_after` in `src/api/routes.py` with Redis — nothing
else depends on the storage mechanism.

## Updating a deployment

The compose file bind-mounts the checkout at `/app`, so the **host files** are
what run. To deploy: `git pull` then `docker compose restart web`. The image's
own copy of the code is not used in that setup; rebuild only if you remove the
bind mount.

## Backups

SQLite is a single file, and on a volume **you** own the backups.

```bash
sqlite3 marketplace.db ".backup 'backups/$(date +%F).db'"
```

Schedule that (cron or a systemd timer) and copy the result off the host. Losing
the volume loses every listing, user and team.

## TLS: minimum version 1.2

**This must be set at the edge. The application cannot enforce it.**

Worth understanding why, because it's easy to assume app code can do it:
Cloudflare terminates TLS, then forwards the request over the tunnel as plain
HTTP. Uvicorn's ASGI scope carries **no** TLS information — no `ssl_object`,
no cipher, no protocol version. So by the time a request reaches this app, the
client's TLS version is simply not knowable. (The tunnel itself is encrypted
separately; the app just never sees it.)

### Configure it in Cloudflare

Dashboard → your domain → **SSL/TLS → Edge Certificates**:

| Setting | Value | Why |
|---|---|---|
| **Minimum TLS Version** | **TLS 1.2** | refuses TLS 1.0/1.1 handshakes at the edge |
| TLS 1.3 | **On** | 1.3 is stronger and faster than 1.2 for capable clients |
| **Always Use HTTPS** | **On** | redirects plain HTTP before it ever reaches the app |
| **HTTP Strict Transport Security (HSTS)** | On, `max-age` ≥ 6 months | prevents downgrade attempts |
| Opportunistic Encryption | On | harmless, helps with legacy clients |
| Automatic HTTPS Rewrites | On | avoids mixed-content breakage |

The **Minimum TLS Version** dropdown is the setting that implements the
requirement. Available on all plans including Free.

Under **SSL/TLS → Overview**, set the encryption mode to **Full (strict)** if
you ever put a certificate on the origin. With a Cloudflare Tunnel this is
moot — the tunnel is already encrypted end to end.

### Verify it

```bash
# Should FAIL (handshake refused):
openssl s_client -connect duckduckdeals.ca:443 -tls1_1 </dev/null

# Should SUCCEED:
openssl s_client -connect duckduckdeals.ca:443 -tls1_2 </dev/null
```

Or use Cloudflare's **SSL/TLS → Edge Certificates** panel, which shows the
minimum version currently in force.

### What the app does check

Because a request arriving at the origin without TLS is the signature of
something bypassing Cloudflare — which is also the only way to forge the
client-IP header that rate limiting relies on — the app logs a one-time
warning when `REQUIRE_HTTPS=true` and a request arrives over plain HTTP:

```
[security] A request arrived over plain HTTP while REQUIRE_HTTPS is on.
           Either the proxy is not setting X-Forwarded-Proto, or something is
           reaching the origin directly and bypassing Cloudflare.
```

It warns rather than blocks, so a proxy that simply omits the header cannot
take the site down. Keep port 80/443 closed on the instance and this should
never appear.

## Step-by-step: Lightsail + Cloudflare Tunnel

The tunnel route is recommended because the app then has **no publicly
reachable port**, so nobody can bypass Cloudflare and forge the client-IP
header that rate limiting depends on.

### 1. Create the instance

* Lightsail → Create instance → **Linux/Unix**, OS-only blueprint
  (**Ubuntu 22.04 LTS**). The smallest bundle (512 MB) is enough.
* Attach a **static IP**.
* In **Networking → IPv4 Firewall**, allow only:
  * SSH (22) — ideally restricted to your own IP
  * **Do not open 80 or 443.** The tunnel is outbound-only; nothing needs to
    be reachable.

### 2. Install Docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"    # then log out and back in
docker compose version             # confirm the plugin is present
```

### 3. Get the code and write `.env`

```bash
sudo mkdir -p /opt/marketplace-dashboard && sudo chown "$USER" /opt/marketplace-dashboard
git clone <your-repo-url> /opt/marketplace-dashboard
cd /opt/marketplace-dashboard
cp .env.example .env
mkdir -p data
nano .env
```

At minimum set:

```
APP_BASE_URL=https://duckduckdeals.ca
ALLOWED_ORIGINS=https://duckduckdeals.ca
REQUIRE_HTTPS=true
ADMIN_PASSWORD=<strong, unique>
SECRET_KEY=<64 hex chars: python3 -c "import secrets;print(secrets.token_hex(32))">
MAIL_BACKEND=http
MAIL_PROVIDER=resend
MAIL_API_KEY=re_...
MAIL_FROM=Marketplace Dashboard <noreply@send.duckduckdeals.ca>
UPLOAD_BACKEND=local        # or s3
CLOUDFLARE_TUNNEL_TOKEN=    # from step 4
```

### 4. Create the tunnel

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a
tunnel → Cloudflared**, name it, then copy the **token** it shows.

Still in the tunnel config, add a **Public hostname**:

| Field | Value |
|---|---|
| Subdomain | *(blank for the apex)* |
| Domain | `duckduckdeals.ca` |
| Service type | `HTTP` |
| URL | `web:8000` |

`web:8000` resolves over the Compose network — that is the container name, and
it is why the app publishes no host port. See Cloudflare's
[remote tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/).

Paste the token into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

### 5. Start it

```bash
docker compose -f docker-compose.lightsail.yml up -d --build
docker compose -f docker-compose.lightsail.yml logs -f web
```

Watch the startup log for a **CONFIGURATION WARNINGS** block — it lists every
setting above that is wrong or missing. If it is absent, the config is clean.

### 6. Point Google OAuth at the new URL

In Google Cloud Console → Credentials → your OAuth client, add:

```
https://duckduckdeals.ca/auth/google/callback
```

Keep the `http://localhost:8000/...` entry if you still develop locally.
Sign-in fails with `redirect_uri_mismatch` until this is done.

### 7. Verify

| Check | Expected |
|---|---|
| `https://duckduckdeals.ca/login` | login page over HTTPS |
| `openssl s_client -tls1_1` | refused (min TLS 1.2) |
| Browser devtools → cookie `auth_token` | `Secure`, `HttpOnly`, `SameSite=Lax` |
| `http://` request | redirected to HTTPS by Cloudflare |
| Admin → Send test email | real email arrives |
| `/signup` with a real address | confirmation mail, link works |
| `docker compose ... ps` | both services healthy |
| `docker compose ... logs web` | no CONFIGURATION WARNINGS, no plain-HTTP security warning |

## Backups

`deploy/backup-db.sh` uses SQLite's `.backup` API (not `cp`, which can capture
a torn database), verifies `PRAGMA integrity_check`, rotates, and can copy
off-host.

```bash
sudo cp deploy/backup-db.sh /usr/local/bin/marketplace-backup
sudo chmod +x /usr/local/bin/marketplace-backup
sudo cp deploy/marketplace-backup.service deploy/marketplace-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marketplace-backup.timer

# Prove it works now rather than discovering it broken later:
sudo systemctl start marketplace-backup.service
journalctl -u marketplace-backup.service -n 20
```

For an off-host copy (essential — a backup on the same disk is not a backup),
create `/etc/default/marketplace-backup`:

```bash
OFFSITE_CMD='aws s3 cp "$1" s3://my-backups/marketplace/'
```

then uncomment the `EnvironmentFile` line in the service unit.

## Updating a deployment

The image bakes in the code, and the compose file mounts only data paths — so
a code change needs a rebuild:

```bash
cd /opt/marketplace-dashboard
git pull
docker compose -f docker-compose.lightsail.yml up -d --build
```

`./.env` is bind-mounted, so Admin → Settings changes survive restarts and
rebuilds.

## Pre-deploy checklist

- [ ] `REQUIRE_HTTPS=true`
- [ ] **Cloudflare Minimum TLS Version = 1.2**, TLS 1.3 on, Always Use HTTPS on
- [ ] `APP_BASE_URL` is the real HTTPS URL **and** the matching redirect URI is
      registered in Google Cloud Console
- [ ] `ADMIN_PASSWORD` changed from the default
- [ ] `SECRET_KEY` set
- [ ] `GOOGLE_DEV_MODE=false`
- [ ] `GOOGLE_ALLOWED_EMAILS` set if you do not want open signups
- [ ] Outbound email works (use **Send test email** in Admin)
- [ ] `UPLOAD_BACKEND` suits the filesystem
- [ ] **No inbound ports open except SSH** (tunnel does the rest)
- [ ] Backup timer installed, run once manually, copied off-host
- [ ] Startup log shows no CONFIGURATION WARNINGS

