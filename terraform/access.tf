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
      id         = "cb1bf754-ee1f-44e6-96e5-d51885fe3684"
      precedence = 1
    }
  ]
}
