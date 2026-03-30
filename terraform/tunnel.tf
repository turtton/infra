locals {
  tunnel_id   = "8dcd868c-295b-4cd0-96d7-37d7928e903d"
  tunnel_name = "home-infra"
  domain      = "turtton.net"

  tunnel_ingress = {
    grafana  = "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local:80"
    longhorn = "http://longhorn-frontend.longhorn-system.svc.cluster.local:80"
    livesync = "http://couchdb.obsidian-livesync.svc.cluster.local:5984"
    kameuo   = "http://iceshrimp-web.iceshrimp.svc.cluster.local:3000"
  }

  access_protected_hostnames = toset(["grafana", "longhorn"])
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "homelab" {
  account_id = var.cloudflare_account_id
  name       = local.tunnel_name
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "homelab" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.homelab.id
  config = {
    ingress = concat(
      [for name, service in local.tunnel_ingress : {
        hostname = "${name}.${local.domain}"
        service  = service
        origin_request = contains(local.access_protected_hostnames, name) ? {
          access = {
            aud_tag   = [cloudflare_zero_trust_access_application.protected.aud]
            team_name = "turtton-net"
            required  = true
          }
        } : null
      }],
      [{ service = "http_status:404" }]
    )
  }
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = local.tunnel_ingress

  zone_id = var.cloudflare_zone_id
  name    = "${each.key}.${local.domain}"
  type    = "CNAME"
  content = "${local.tunnel_id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "homelab" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.homelab.id
}
