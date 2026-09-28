###############################################################################
# Outputs
#
# Marked sensitive where the value is a credential, so it is not printed by
# default. Note that Terraform still stores sensitive values in state.
###############################################################################

output "instance_public_ip" {
  description = "Static IP of the instance. Use this for SSH."
  value       = aws_lightsail_static_ip.app.ip_address
}

output "ssh_command" {
  description = "Ready-to-paste SSH command (only valid if ssh_allowed_cidrs includes you)."
  value       = "ssh -i <your-key>.pem ubuntu@${aws_lightsail_static_ip.app.ip_address}"
}

output "app_url" {
  description = "Public URL, once the tunnel token is in place and the stack is running."
  value       = local.app_url
}

output "tunnel_id" {
  description = "Cloudflare Tunnel ID."
  value       = cloudflare_zero_trust_tunnel_cloudflared.app.id
}

output "admin_username" {
  description = "Admin username. The login identity is <username>@local."
  value       = var.admin_username
}

output "admin_login_email" {
  description = "Exactly what to type in the username field at /login."
  value       = local.admin_email
}

output "admin_password" {
  description = "Generated admin password. Change it in Admin -> Settings after first login."
  value       = random_password.admin.result
  sensitive   = true
}

output "generated_ssh_private_key" {
  description = <<-EOT
    Only populated when ssh_public_key was empty. Save it and then destroy the
    state, because this value lives in plaintext in Terraform state:
      tofu output -raw generated_ssh_private_key > dash.pem
  EOT
  value       = local.use_existing_key ? null : tls_private_key.generated[0].private_key_pem
  sensitive   = true
}

output "remaining_manual_steps" {
  description = "What Terraform cannot do for you."
  value       = <<-EOT

    1. Copy the tunnel token into the instance's .env:
         Zero Trust -> Networks -> Tunnels -> ${var.tunnel_name} -> install token
         Add a Public hostname: domain ${var.domain}, service HTTP, URL web:8000
         Then on the instance:
           sed -i "s|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=<token>|" ${var.repo_dir}/.env

    2. Start the stack:
         cd ${var.repo_dir}
         docker compose -f docker-compose.lightsail.yml up -d --build

    3. Register the Google OAuth redirect:
         ${local.app_url}/auth/google/callback

    4. Cloudflare zone settings (once, in the dashboard):
         SSL/TLS -> Edge Certificates -> Minimum TLS Version = 1.2
                                      -> Always Use HTTPS    = On
  EOT
}
