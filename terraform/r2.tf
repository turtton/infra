################################################################################
# Cloudflare R2 buckets + bucket-scoped API tokens for Forgejo + Longhorn backup.
#
# Outputs (sensitive) are consumed via `tofu output -raw <name>` and written
# into SOPS-encrypted Kubernetes Secrets:
#   - forgejo-backup  : consumed by CNPG barmanObjectStore (Forgejo DB WAL/base)
#   - longhorn-backup : consumed by Longhorn BackupTarget (Forgejo PVC snapshots)
#
# Each token is scoped to a single bucket via the `Workers R2 Storage Bucket
# Item Read/Write` permission groups + a resource selector pinning the bucket.
# IDs of these permission groups are stable Cloudflare-wide values.
################################################################################

locals {
  r2_permission_group_read  = "6a018a9f2fc74eb6b293b0c548f38b39" # Workers R2 Storage Bucket Item Read
  r2_permission_group_write = "2efd5506f9c8494dacb1fa10a3e7d5b6" # Workers R2 Storage Bucket Item Write

  r2_buckets = {
    forgejo  = "forgejo-backup"
    longhorn = "longhorn-backup"
  }

  # APAC = Tokyo / Osaka 近接ジュリスディクション。
  # 単一バケットあたり「最初の 10GB は無料、以降 $0.015/GB-month、egress 無料」。
  r2_location = "apac"
}

resource "cloudflare_r2_bucket" "this" {
  for_each = local.r2_buckets

  account_id    = var.cloudflare_account_id
  name          = each.value
  location      = local.r2_location
  storage_class = "Standard"
}

resource "cloudflare_account_token" "forgejo_r2" {
  account_id = var.cloudflare_account_id
  name       = "forgejo-r2-backup"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        { id = local.r2_permission_group_read },
        { id = local.r2_permission_group_write },
      ]
      resources = jsonencode({
        "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.this["forgejo"].name}" = "*"
      })
    }
  ]
}

resource "cloudflare_account_token" "longhorn_r2" {
  account_id = var.cloudflare_account_id
  name       = "longhorn-r2-backup"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        { id = local.r2_permission_group_read },
        { id = local.r2_permission_group_write },
      ]
      resources = jsonencode({
        "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.this["longhorn"].name}" = "*"
      })
    }
  ]
}
