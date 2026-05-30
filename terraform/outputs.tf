output "talosconfig" {
  description = "Talos client configuration"
  value       = data.talos_client_configuration.this.talos_config
  sensitive   = true
}

output "kubeconfig" {
  description = "Kubernetes kubeconfig"
  value       = talos_cluster_kubeconfig.this.kubeconfig_raw
  sensitive   = true
}

# Tunnel再作成時はtokenが変わるため、K8s側のSOPS Secretも更新が必要:
#   tofu output -raw tunnel_token | \
#     kubectl create secret generic cloudflared-tunnel-token \
#       -n cloudflared --from-file=token=/dev/stdin --dry-run=client -o yaml | \
#     sops --encrypt --input-type yaml --output-type yaml /dev/stdin \
#       > ../clusters/main/infrastructure/controllers/cloudflared/tunnel-credentials.sops.yaml
output "tunnel_token" {
  description = "Cloudflare Tunnel token for cloudflared deployment"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.homelab.token
  sensitive   = true
}

# R2 credentials for SOPS-encrypted Kubernetes Secrets.
#   tofu output -raw forgejo_r2_access_key_id
#   tofu output -raw forgejo_r2_secret_access_key   # = sha256(token.value)
#   tofu output -raw longhorn_r2_access_key_id
#   tofu output -raw longhorn_r2_secret_access_key
#   tofu output -raw r2_endpoint_url
#   tofu output -raw r2_account_id
output "r2_account_id" {
  description = "Cloudflare account ID (R2 S3 API path component)"
  value       = var.cloudflare_account_id
}

output "r2_endpoint_url" {
  description = "R2 S3-compatible endpoint URL (account-scoped, path-style)"
  value       = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
}

output "forgejo_r2_access_key_id" {
  description = "R2 access key ID for Forgejo CNPG barmanObjectStore (token ID)"
  value       = cloudflare_api_token.forgejo_r2.id
  sensitive   = true
}

output "forgejo_r2_secret_access_key" {
  description = "R2 secret access key for Forgejo CNPG barmanObjectStore (sha256 of token value)"
  value       = sha256(cloudflare_api_token.forgejo_r2.value)
  sensitive   = true
}

output "longhorn_r2_access_key_id" {
  description = "R2 access key ID for Longhorn BackupTarget (token ID)"
  value       = cloudflare_api_token.longhorn_r2.id
  sensitive   = true
}

output "longhorn_r2_secret_access_key" {
  description = "R2 secret access key for Longhorn BackupTarget (sha256 of token value)"
  value       = sha256(cloudflare_api_token.longhorn_r2.value)
  sensitive   = true
}
