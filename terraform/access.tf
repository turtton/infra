resource "cloudflare_zero_trust_access_application" "protected" {
  account_id       = var.cloudflare_account_id
  name             = "home-monitor"
  type             = "self_hosted"
  session_duration = "24h"

  destinations = [for name in local.access_protected_hostnames : {
    type = "public"
    uri  = "${name}.${local.domain}"
  }]

  policies = [
    {
      id         = var.cloudflare_access_policy_id
      precedence = 1
    }
  ]
}
