###############################################################################
# Marketplace Dashboard — AWS Lightsail + Cloudflare Tunnel
#
# Reproduces the deployment that was originally built by hand:
#   Lightsail instance -> Docker Compose -> cloudflared -> Cloudflare edge
#
# The origin publishes NO inbound port except SSH. cloudflared dials out, so
# nothing on the internet can reach the app directly and forge the
# CF-Connecting-IP header that rate limiting depends on.
###############################################################################

provider "aws" {
  region = var.aws_region
}

provider "cloudflare" {
  # Reads CLOUDFLARE_API_TOKEN from the environment. Never put it in a .tf
  # file or a tfvars file that could be committed.
  # Token scope needed: Account -> Cloudflare Tunnel -> Edit,
  #                     Zone    -> DNS              -> Edit
}

locals {
  # The app's login identity is derived as <username>@local.
  admin_email = "${var.admin_username}@local"
  app_url     = "https://${var.domain}"

  # Rendered here (rather than inside the cloud-init template) so the .env body
  # never has to survive a trip through bash quoting. See templates/env.tftpl.
  env_b64 = base64encode(templatefile("${path.module}/templates/env.tftpl", {
    admin_username       = var.admin_username
    admin_password       = random_password.admin.result
    domain               = var.domain
    secret_key           = random_id.secret_key.hex
    google_client_id     = var.google_client_id
    google_client_secret = var.google_client_secret
    mail_api_key         = var.mail_api_key
    mail_from            = var.mail_from
    aws_region           = var.aws_region
  }))

  use_existing_key = var.ssh_public_key != ""
}

###############################################################################
# Credentials generated for the instance
###############################################################################

# Session/signing key. Stable across restarts; without this the app regenerates
# it on every boot, invalidating sessions.
resource "random_id" "secret_key" {
  byte_length = 32
}

# The admin password. special = false keeps the value to characters that are
# safe in a .env line and in a shell, avoiding a class of silent breakage.
resource "random_password" "admin" {
  length  = 24
  special = false
}

###############################################################################
# SSH access
###############################################################################

# Path A: you already have a .pem. Pass its public half in ssh_public_key
# (ssh-keygen -y -f Dash.pem). Nothing sensitive enters Terraform state.
resource "aws_lightsail_key_pair" "existing" {
  count      = local.use_existing_key ? 1 : 0
  name       = "${var.instance_name}-key"
  public_key = var.ssh_public_key

  lifecycle {
    # The public key cannot be changed after creation; replace instead.
    create_before_destroy = true
  }
}

# Path B: no key supplied, so Terraform generates one. The private key then
# lives in Terraform state — see the README for why that matters.
#
# Set with no dependencies: the key pair is named by the instance, so if this
# resource also depended on the rendered user_data (which needs the generated
# admin password) the graph would be circular.
resource "tls_private_key" "generated" {
  count     = local.use_existing_key ? 0 : 1
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_lightsail_key_pair" "generated" {
  count      = local.use_existing_key ? 0 : 1
  name       = "${var.instance_name}-key"
  public_key = tls_private_key.generated[0].public_key_openssh
}

locals {
  key_pair_name = local.use_existing_key ? (
    aws_lightsail_key_pair.existing[0].name
    ) : (
    aws_lightsail_key_pair.generated[0].name
  )
}

###############################################################################
# Instance
###############################################################################

resource "aws_lightsail_instance" "app" {
  name              = var.instance_name
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  key_pair_name     = local.key_pair_name

  # Daily instance snapshots. NOTE: these capture the whole disk, and SQLite is
  # snapshotted live, so a snapshot is not guaranteed to be transactionally
  # consistent. The nightly .backup-based job in deploy/ is the authoritative
  # database backup; these snapshots cover losing the login details or a
  # broken configuration, not just the data.
  dynamic "add_on" {
    for_each = var.enable_daily_snapshots ? [1] : []
    content {
      type          = "AutoSnapshot"
      status        = "Enabled"
      snapshot_time = "03:00"
    }
  }

  user_data = templatefile("${path.module}/templates/cloud-init.sh.tftpl", {
    repo_dir           = var.repo_dir
    repository_url     = var.repository_url
    env_b64            = local.env_b64
    deploy_key_private = var.repository_deploy_key_private
  })

  tags = {
    Application = "marketplace-dashboard"
    ManagedBy   = "opentofu"
  }
}

resource "aws_lightsail_static_ip" "app" {
  name = "${var.instance_name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.name
  instance_name  = aws_lightsail_instance.app.name
}

# Only SSH is open. The tunnel is outbound-only, so 80/443 stay closed: opening
# them would give attackers a path around Cloudflare that can forge the client
# IP header and evade every rate limit.
resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  dynamic "port_info" {
    for_each = var.ssh_allowed_cidrs
    content {
      protocol  = "tcp"
      from_port = 22
      to_port   = 22
      cidrs     = [port_info.value]
    }
  }
}

###############################################################################
# Cloudflare Tunnel
###############################################################################

resource "cloudflare_zero_trust_tunnel_cloudflared" "app" {
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name

  # Public hostnames are managed in the dashboard rather than in Terraform.
  #
  # Not a preference: Cloudflare issues a tunnel's token ONLY through the
  # dashboard, so the connector cannot be bootstrapped by Terraform anyway, and
  # keeping the ingress rules alongside the token avoids the two drifting.
  config_src = "cloudflare"
}

resource "cloudflare_dns_record" "app" {
  count   = var.create_dns_record ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = var.domain
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.app.id}.cfargotunnel.com"

  # Must be proxied: the whole point is that traffic lands on Cloudflare, not
  # on the origin.
  proxied = true
  ttl     = 1
  comment = "Managed by OpenTofu - routes to the ${var.tunnel_name} tunnel"
}

###############################################################################
# Zone settings
#
# Deliberately NOT managed here.
#
# Minimum TLS Version and Always Use HTTPS are one-off security settings that
# already exist on the zone. Declaring them in Terraform would report permanent
# drift against values set in the dashboard, and a mistaken `apply` could turn
# a security control off. They are listed in the README as a checklist instead.
#
# To adopt them anyway, add cloudflare_zone_setting resources and run
# `tofu import` first.
###############################################################################

###############################################################################
# Updating an instance that already exists
#
# cloud-init runs exactly once, at first boot. Everything below is the update
# path, so this configuration works both for a first build and for shipping a
# later revision to a running instance.
#
# The update script is idempotent: it detects whether the checkout, the mounted
# directories, the backup units and the containers already exist and only does
# what is missing. Running it repeatedly is safe.
###############################################################################

# The private key used for SSH. When Terraform generated the key pair it has to
# be materialised here, because an SSH connection needs a file. Written 0600 and
# gitignored.
resource "local_sensitive_file" "generated_ssh_key" {
  count           = local.use_existing_key ? 0 : 1
  content         = tls_private_key.generated[0].private_key_pem
  filename        = "${path.module}/generated_ssh_key.pem"
  file_permission = "0600"
}

locals {
  ssh_private_key_path = (
    var.ssh_private_key_path != "" ? var.ssh_private_key_path : (
      local.use_existing_key ? "" : local_sensitive_file.generated_ssh_key[0].filename
    )
  )

  # Refuse to attempt a deploy we cannot connect for, rather than letting the
  # provisioner fail later with an opaque SSH timeout.
  can_deploy = var.deploy_on_apply && local.ssh_private_key_path != ""
}

resource "null_resource" "deploy" {
  count = local.can_deploy ? 1 : 0

  # Change deploy_version to trigger an update.
  triggers = {
    deploy_version = var.deploy_version
    instance_id    = aws_lightsail_instance.app.id
    branch         = var.branch
    repo           = var.repository_url
  }

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_lightsail_static_ip.app.ip_address
    private_key = file(local.ssh_private_key_path)
    timeout     = "5m"
  }

  # Staged then run, kept as two steps so failures stay distinguishable: a copy
  # failure is connectivity or key material, a run failure is the deploy.
  provisioner "file" {
    source      = "${path.module}/templates/update.sh.tftpl"
    destination = "/tmp/marketplace-update.sh"
  }

  provisioner "remote-exec" {
    # The script is written to disk verbatim and only THEN gets its variables.
    #
    # The obvious approach — interpolating repo_dir/branch into the script
    # during rendering — does not work: the script's own bash parameter
    # expansions are parsed as Terraform template expressions, and HCL cannot
    # express a literal dollar-brace (there is no escape for it), so the render
    # fails outright. Substituting after the file is on the instance keeps the
    # two languages completely separate, and the script stays readable as bash.
    inline = [
      "BASE64=$(base64 -w0 /tmp/marketplace-update.sh)",
      "printf '%s' \"$BASE64\" | base64 -d | sed -e \"s|@REPO_DIR@|${var.repo_dir}|g\" -e \"s|@BRANCH@|${var.branch}|g\" -e \"s|@DEPLOY_VERSION@|${var.deploy_version}|g\" | sudo tee /usr/local/bin/marketplace-update >/dev/null",
      "sudo chmod 755 /usr/local/bin/marketplace-update",
      "sudo /usr/local/bin/marketplace-update",
      "rm -f /tmp/marketplace-update.sh",
    ]
  }

  depends_on = [
    aws_lightsail_static_ip_attachment.app,
    aws_lightsail_instance_public_ports.app,
  ]
}
