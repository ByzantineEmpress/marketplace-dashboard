# Infrastructure as Code

Reproduces the whole deployment from scratch: a Lightsail instance running the
app via Docker Compose, reached through a Cloudflare Tunnel, with no inbound
port open except SSH.

This exists because building it by hand surfaced a set of traps worth encoding
rather than rediscovering. The bootstrap script applies every one of those
fixes automatically:

| Trap | What it cost | Handled by |
|---|---|---|
| `data/` owned by root | container restart-loop, `unable to open database file` | cloud-init chowns to uid 100 |
| `static/uploads/` owned by root | every image upload 500s, shown as "network error" | cloud-init chowns to uid 100 |
| No swap on a 1 GB instance | pip build OOM-killed | cloud-init creates 2 GB swap |
| `docker.io` has no Compose v2 | `docker compose` fails | installs from Docker's repo |
| Incomplete `.env` | app boots with CONFIGURATION WARNINGS | cloud-init renders a full one |
| `data/` excluded from `.dockerignore` | every rebuild failed on permissions | fixed in the image |
| SSH open to the world | constant credential scanning | variable refuses `0.0.0.0/0` |

## What it creates

```
aws_lightsail_instance.app          Ubuntu 22.04, static IP, daily snapshots
aws_lightsail_static_ip.app         stable address
aws_lightsail_instance_public_ports SSH only — 80/443 deliberately closed
aws_lightsail_key_pair.*            your key, or a generated one
cloudflare_zero_trust_tunnel_cloudflared.app
cloudflare_dns_record.app           proxied CNAME -> <tunnel>.cfargotunnel.com
```

Cloud-init then installs Docker, creates swap, clones the repository, writes a
complete `.env`, and installs the nightly backup timer.

## What it deliberately does NOT do

**Zone settings are not managed.** Minimum TLS Version and Always Use HTTPS are
one-off security controls that already exist on the zone. Declaring them here
would report permanent drift against dashboard values, and one mistaken
`apply` could switch a security control *off*. They are a checklist item below
instead. (`cloudflare_zone_setting` exists if you want to adopt them — run
`tofu import` first.)

**The tunnel token cannot be created.** Cloudflare issues a tunnel's token only
through the dashboard. This is why `config_src = "cloudflare"` is set: the
public hostname is managed where the token is, so the two cannot drift apart.

**Google OAuth redirect registration is manual** — it lives in Google Cloud
Console, not Cloudflare.

## Before you apply

### 1. Verify the bundle ID

`bundle_id` defaults to `medium_2_0`, which is the 1 GB / 2 vCPU bundle. Bundle
naming has changed over time and AWS rejects a stale value, so confirm it:

```bash
aws lightsail get-bundles --region ca-central-1 \
  --query 'bundles[?supportedPlatforms[0]==`LINUX_UNIX`].[bundleId,ramSizeInGb,price]' \
  --output table
```

512 MB will build but is tight; 1 GB is the practical minimum.

### 2. Credentials

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export CLOUDFLARE_API_TOKEN=...
```

The Cloudflare token needs exactly:

| Scope | Permission |
|---|---|
| Account → Cloudflare Tunnel | Edit |
| Zone → DNS | Edit |

Scope it to the single zone. Nothing account-wide is required. Never put these
in a `.tfvars` file.

### 3. Your own inputs

Copy `terraform.tfvars.example` to `terraform.tfvars` and fill it in. You need
the Cloudflare **account ID** and **zone ID** (both visible in the dashboard
URL / domain Overview page).

Provide your SSH access, or `apply` will fail by design:

```powershell
# Windows
$env:TF_VAR_ssh_allowed_cidrs = '["203.0.113.9/32"]'   # your address
$env:TF_VAR_ssh_public_key    = (ssh-keygen -y -f C:\Users\You\Downloads\Dash.pem)
```

```bash
# Linux / macOS
export TF_VAR_ssh_allowed_cidrs='["203.0.113.9/32"]'
export TF_VAR_ssh_public_key="$(ssh-keygen -y -f ~/Downloads/Dash.pem)"
```

Find your address with `curl -s https://api.ipify.org`.

## Applying

```bash
cd terraform
tofu init
tofu plan
tofu apply
```

Works with Terraform ≥ 1.6 too; this uses no OpenTofu-only features.

Then, on the instance:

```bash
ssh -i Dash.pem ubuntu@<static-ip>
cat /var/log/marketplace-bootstrap.log      # check the bootstrap succeeded

cd /opt/marketplace-dashboard
read -rsp "Tunnel token: " T && echo
sed -i "s|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=${T}|" .env
unset T

docker compose -f docker-compose.lightsail.yml up -d --build
docker compose -f docker-compose.lightsail.yml logs -f web
```

And in the Cloudflare dashboard, once:

1. **Networks → Tunnels → your tunnel → Public hostname**: domain `<your-domain>`,
   service type **HTTP**, URL **`http://web:8000`**
2. **SSL/TLS → Edge Certificates**: Minimum TLS Version **1.2**, Always Use
   HTTPS **On**

## ⚠️ Handling state

**State contains plaintext secrets.** With this configuration that means the
generated admin password, `SECRET_KEY`, the Google client secret and the mail
API key. `terraform/.gitignore` excludes state, but local state on your laptop
is still a plaintext credential store.

For anything beyond a single operator, use an encrypted remote backend:

```hcl
terraform {
  backend "s3" {
    bucket       = "your-tfstate-bucket"
    key          = "marketplace-dashboard/terraform.tfstate"
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true
  }
}
```

Recover the generated password with `tofu output -raw admin_password`. Change
it in **Admin → Settings** after first login, then treat the state as stale.

If you supplied `ssh_public_key`, no private key ever enters state. If you let
Terraform generate one, it does — save it and consider destroying state:

```bash
tofu output -raw generated_ssh_private_key > dash.pem
```

## Snapshots are a second safety net, not the primary one

`enable_daily_snapshots` (default on) takes whole-instance Lightsail snapshots
at 03:00. Because SQLite is snapshotted while running, a snapshot is **not
guaranteed to be transactionally consistent**.

The authoritative database backup is the nightly job in `deploy/`, which uses
SQLite's online backup API and verifies integrity. Snapshots cover the case
where the *instance* is lost or misconfigured, not the case where one row is
wrong.

Backups currently land in `/opt/marketplace-dashboard/data/backups` — on the
same disk. Configure an off-host copy or they die with the instance:

```bash
sudo tee /etc/default/marketplace-backup >/dev/null <<'EOF'
OFFSITE_CMD='aws s3 cp "$1" s3://your-bucket/marketplace/'
EOF
```

## Verification status

Be aware of what is and isn't proven here:

**Verified**
- `tofu validate` passes
- `tofu fmt` clean
- Both templates render correctly with representative inputs
- The rendered cloud-init passes `bash -n` on Linux

**Not verified**
- **No `apply` has ever been run.** These files were written and validated
  without AWS or Cloudflare credentials, so nothing here has created real
  infrastructure.
- `blueprint_id` / `bundle_id` are not confirmed against a live AWS account —
  check them as described above.
- The Cloudflare tunnel and DNS resources are validated against the provider
  *schema*, not against the live API.

Expect to fix something on the first `apply`. `tofu plan` will tell you more
than this document can.

## If you already deployed by hand

Do **not** run `apply` and expect it to adopt the existing resources — it will
try to create duplicates. Import instead:

```bash
tofu import aws_lightsail_instance.app marketplace-dashboard
tofu import aws_lightsail_static_ip.app marketplace-dashboard-ip
tofu import cloudflare_zero_trust_tunnel_cloudflared.app <account_id>/<tunnel_id>

# The DNS record already exists (the tunnel wizard created it), so either
# import it or set create_dns_record = false.
tofu import cloudflare_dns_record.app[0] <zone_id>/<record_id>
```

Your existing tunnel ID is `00e1189e-db5c-4c97-873a-2d0d3000abff`.
