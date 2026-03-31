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
