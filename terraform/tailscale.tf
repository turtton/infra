locals {
  proxmox_tailscale_devices = ["main", "data", "toliunit"]
}

data "tailscale_device" "proxmox" {
  for_each = toset(local.proxmox_tailscale_devices)
  hostname = each.value
  wait_for = "60s"
}

resource "tailscale_device_tags" "proxmox" {
  for_each   = data.tailscale_device.proxmox
  device_id  = each.value.node_id
  tags       = ["tag:proxmox-cluster"]
  depends_on = [tailscale_acl.this]
}

resource "tailscale_acl" "this" {
  acl = jsonencode({
    tagOwners = {
      "tag:client"              = ["autogroup:admin"]
      "tag:mcserver"            = ["autogroup:admin"]
      "tag:mcproxy"             = ["autogroup:admin"]
      "tag:tmp"                 = ["autogroup:admin"]
      "tag:privatecloud"        = ["autogroup:admin"]
      "tag:nix"                 = ["autogroup:admin"]
      "tag:attic"               = ["tag:privatecloud", "tag:k8s-operator"]
      "tag:proxmox-cluster"     = ["autogroup:admin"]
      "tag:ci"                  = ["autogroup:admin"]
      "tag:infra-talos-cluster" = ["autogroup:admin"]
      "tag:k8s-operator"        = []
      "tag:k8s"                 = ["tag:k8s-operator"]
      "tag:nextcloud"           = ["tag:k8s-operator"]
      "tag:hermes"              = ["tag:k8s-operator"]
      "tag:forgejo"             = ["tag:k8s-operator"]
    }

    acls = []

    ssh = [
      {
        action = "check"
        src    = ["autogroup:member"]
        dst    = ["autogroup:self"]
        users  = ["autogroup:nonroot", "root"]
      },
    ]

    tests = [
      {
        src    = "tag:client"
        accept = ["tag:mcserver:22", "tag:mcproxy:22", "tag:tmp:22", "tag:privatecloud:22"]
      },
      {
        src    = "tag:mcproxy"
        accept = ["tag:mcserver:22", "tag:mcserver:25777", "tag:mcserver:24454"]
        deny   = ["tag:client:22"]
      },
      {
        src  = "tag:mcserver"
        deny = ["tag:mcproxy:22", "tag:client:22"]
      },
      {
        src  = "tag:privatecloud"
        deny = ["tag:mcserver:22", "tag:mcproxy:22", "tag:client:22"]
      },
      {
        src  = "tag:nextcloud"
        deny = ["tag:mcserver:22", "tag:mcproxy:22", "tag:client:22"]
      },
      {
        src  = "tag:forgejo"
        deny = ["tag:mcserver:22", "tag:mcproxy:22", "tag:client:22"]
      },
      {
        src    = "tag:nix"
        accept = ["tag:attic:8080"]
        deny   = ["tag:client:22", "tag:privatecloud:22", "tag:mcserver:22"]
      },
      {
        src   = "tag:proxmox-cluster"
        deny  = ["tag:client:22"]
        proto = "tcp"
      },
      {
        src    = "tag:ci"
        accept = ["tag:proxmox-cluster:22"]
        deny   = ["tag:client:22"]
      },
      {
        src    = "tag:ci"
        accept = ["tag:proxmox-cluster:22", "tag:infra-talos-cluster:22"]
      },
      {
        src    = "tag:ci"
        accept = ["192.168.11.100:5000"]
      },
      {
        src    = "tag:ci"
        accept = ["192.168.10.110:50000", "192.168.10.120:50000", "192.168.10.121:50000", "192.168.10.122:50000", "192.168.10.123:50000", "192.168.10.124:50000", "192.168.10.125:50000"]
      },
      {
        src    = "tag:ci"
        accept = ["192.168.10.100:22", "192.168.10.40:22", "192.168.10.101:22"]
      },
    ]

    nodeAttrs = [
      {
        target = ["autogroup:member", "tag:attic"]
        attr   = ["funnel"]
      },
    ]

    autoApprovers = {
      routes = {
        "192.168.10.0/24" = ["tag:proxmox-cluster"]
        "192.168.11.0/24" = ["tag:proxmox-cluster"]
      }
    }

    grants = [
      {
        src = ["tag:mcproxy"]
        dst = ["tag:mcserver"]
        ip  = ["*"]
      },
      {
        src = ["autogroup:member", "autogroup:shared"]
        dst = ["tag:mcserver"]
        ip  = ["*"]
      },
      {
        src = ["tag:client"]
        dst = ["*"]
        ip  = ["*"]
      },
      {
        src = ["autogroup:admin"]
        dst = ["tag:privatecloud", "tag:nextcloud"]
        ip  = ["443"]
      },
      {
        src = ["autogroup:admin"]
        dst = ["tag:forgejo"]
        ip  = ["22", "443"]
      },
      {
        src = ["tag:nix"]
        dst = ["tag:attic"]
        ip  = ["tcp:*"]
      },
      {
        src = ["tag:ci"]
        dst = ["tag:proxmox-cluster", "tag:infra-talos-cluster", "192.168.11.0/24", "192.168.10.0/24"]
        ip  = ["*"]
      },
      {
        src = ["tag:proxmox-cluster"]
        dst = ["tag:infra-talos-cluster"]
        ip  = ["*"]
      },
    ]
  })
}
