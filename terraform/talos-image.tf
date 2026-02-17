locals {
  # Talos拡張モジュール
  talos_extensions = [
    "siderolabs/qemu-guest-agent",
    "siderolabs/tailscale",
    "siderolabs/iscsi-tools",
    "siderolabs/util-linux-tools",
  ]

  # 全ノード定義を統合
  all_nodes = merge(var.control_planes, var.workers)

  # イメージをダウンロードする必要のあるProxmoxノード（重複除去）
  proxmox_nodes = distinct([for node in local.all_nodes : node.host_node])
}

# factory.talos.dev APIからschematic IDを取得
resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = local.talos_extensions
      }
    }
  })
}

# 各Proxmoxノードにイメージをダウンロード
resource "proxmox_virtual_environment_download_file" "talos_image" {
  for_each = toset(local.proxmox_nodes)

  content_type = "iso"
  datastore_id = "local"
  node_name    = each.value
  file_name    = "talos-${var.talos_version}-nocloud-amd64.img"

  url = "https://factory.talos.dev/image/${talos_image_factory_schematic.this.id}/${var.talos_version}/nocloud-amd64.raw.xz"

  decompression_algorithm = "xz"
  overwrite               = false
}
