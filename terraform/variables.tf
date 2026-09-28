###############################################################################
# Variables
#
# Defaults are set so `tofu apply` works with minimal input, but every value
# that is account- or region-specific is overridable.
###############################################################################

# ─── AWS ──────────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for the Lightsail instance."
  type        = string
  default     = "ca-central-1"
}

variable "availability_zone" {
  description = "Availability zone. Must belong to aws_region."
  type        = string
  default     = "ca-central-1a"
}

variable "instance_name" {
  description = "Lightsail instance name."
  type        = string
  default     = "marketplace-dashboard"
}

variable "blueprint_id" {
  description = <<-EOT
    Lightsail OS blueprint. This is a region-independent identifier, not the
    display name shown in the console. `ubuntu_22_04` is verified working.
    List alternatives with:
      aws lightsail get-blueprints --region <region> --query 'blueprints[].blueprintId'
  EOT
  type        = string
  default     = "ubuntu_22_04"
}

variable "bundle_id" {
  description = <<-EOT
    Lightsail instance bundle (memory/CPU/disk/transfer).

    VERIFY THIS BEFORE APPLYING — bundle naming has changed over time and AWS
    may reject a stale value. The smallest useful bundle for this app is 1 GB;
    512 MB will build but is tight. List what your account can actually launch:
      aws lightsail get-bundles --region <region> \
        --query 'bundles[?supportedPlatforms[0]==`LINUX_UNIX`].[bundleId,ramSizeInGb,price]' \
        --output table
  EOT
  type        = string
  default     = "medium_2_0"
}

variable "enable_daily_snapshots" {
  description = <<-EOT
    Lightsail automatic snapshots of the whole instance. This is a second,
    independent safety net alongside the SQLite backups in deploy/ — it covers
    the situation where the login details or config are broken, not just the
    database. Adds a small monthly cost.
  EOT
  type        = bool
  default     = true
}

# ─── Access ───────────────────────────────────────────────────────────────────

variable "ssh_allowed_cidrs" {
  description = <<-EOT
    CIDRs permitted to reach port 22. DELIBERATELY HAS NO PERMISSIVE DEFAULT.

    Leaving SSH open to 0.0.0.0/0 exposes the instance to constant credential
    scanning. Find your address with `curl -s https://api.ipify.org` and use
    "<your-ip>/32".
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.ssh_allowed_cidrs, "0.0.0.0/0")
    error_message = "Refusing to open SSH to the entire internet. Pass your own address as <ip>/32."
  }
}

variable "ssh_public_key" {
  description = <<-EOT
    Existing SSH public key to install (the recommended path if you already
    have a .pem). Produce it from your private key with:
      ssh-keygen -y -f Dash.pem

    Leave empty to have Terraform generate a key pair, whose private key is
    then written to Terraform state — see the README for that trade-off.
  EOT
  type        = string
  default     = ""
}

variable "ssh_private_key_path" {
  description = <<-EOT
    Path to the PRIVATE key used for SSH. Required for `deploy_on_apply`,
    because updating the instance means logging into it.

    If ssh_public_key is empty (Terraform generated the pair), leave this blank
    and Terraform writes the generated key to ./generated_ssh_key.pem and uses
    that instead.
  EOT
  type        = string
  default     = ""
}

# ─── Updates ──────────────────────────────────────────────────────────────────

variable "deploy_version" {
  description = <<-EOT
    The deployment trigger. Change this string and run `apply` to pull the
    latest revision on the instance, rebuild the image and recreate the
    containers.

    Why a manual trigger rather than "always": Terraform only re-runs a
    provisioner when something it depends on changes. Making that explicit
    keeps `plan` honest — an apply that should not touch the server stays
    quiet — and means a deploy is a deliberate act rather than a side effect of
    an unrelated change.

    A date or a git SHA both work.
  EOT
  type        = string
  default     = "v1"
}

variable "deploy_on_apply" {
  description = <<-EOT
    Run the update over SSH when deploy_version changes.

    Requires deploy_on_apply to have somewhere to connect from: the machine
    running `tofu apply` must be allowed through the Lightsail firewall on port
    22 (see ssh_allowed_cidrs).

    Set to false if you would rather deploy outside Terraform — the same script
    is on the instance at /usr/local/bin/marketplace-update.
  EOT
  type        = bool
  default     = true
}

variable "branch" {
  description = "Git branch the instance checks out and updates from."
  type        = string
  default     = "main"
}

# ─── Cloudflare ───────────────────────────────────────────────────────────────

variable "cloudflare_account_id" {
  description = "Cloudflare account ID (dashboard URL: dash.cloudflare.com/<account_id>)."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Zone ID for the domain, from the domain's Overview page."
  type        = string
}

variable "domain" {
  description = "Apex domain to serve the app on."
  type        = string
  default     = "duckduckdeals.ca"
}

variable "create_dns_record" {
  description = <<-EOT
    Whether Terraform should create the proxied CNAME for the apex domain.

    Set to false if the record already exists (for example it was created by
    the tunnel wizard, as it was on the first deployment) — otherwise apply
    fails with "record already exists". Terraform refuses to silently adopt an
    existing record.
  EOT
  type        = bool
  default     = true
}

variable "tunnel_name" {
  description = "Name of the Cloudflare Tunnel."
  type        = string
  default     = "duckduckdeals-tunnel"
}

# ─── Application ──────────────────────────────────────────────────────────────

variable "repository_url" {
  description = "Git URL cloned onto the instance. Use the SSH form so a deploy key can be used."
  type        = string
  default     = "git@github.com:ByzantineEmpress/marketplace-dashboard.git"
}

variable "repository_deploy_key_private" {
  description = <<-EOT
    Optional read-only deploy key (private half) for cloning a private repo.
    Supply it through the environment, never a file in this directory:

      $env:TF_VAR_repository_deploy_key_private = Get-Content -Raw key.txt

    WARNING: anything passed here ends up in Terraform state. If that is
    unacceptable, clone the repository by hand after the instance is up.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "repo_dir" {
  description = "Directory the app is deployed into."
  type        = string
  default     = "/opt/marketplace-dashboard"
}

variable "admin_username" {
  description = "Local administrator username. The login identity becomes <username>@local."
  type        = string
  default     = "klamkin"
}

variable "google_client_id" {
  description = "Google OAuth client ID. Optional at first boot; can be set later in .env."
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = "Google OAuth client secret."
  type        = string
  default     = ""
  sensitive   = true
}

variable "mail_from" {
  description = "From address for outbound email."
  type        = string
  default     = "Marketplace Dashboard <noreply@send.duckduckdeals.ca>"
}

variable "mail_api_key" {
  description = "Provider API key for outbound email. Blank is allowed; set it in Admin → Settings afterwards."
  type        = string
  default     = ""
  sensitive   = true
}
