# DNS records for the Sakura VPS.
# Minecraft domains must NOT be proxied by Cloudflare (TCP 25565).

resource "cloudflare_dns_record" "vps_atm10" {
  zone_id = var.cloudflare_zone_id
  name    = "atm10.turtton.net"
  type    = "A"
  content = "133.167.115.94"
  proxied = false
  ttl     = 1
}
