resource "talos_machine_secrets" "this" {
  talos_version = var.talos_version
}

locals {
  # 共通config patches
  common_patches = [
    # Longhorn用カーネルモジュール自動ロード
    yamlencode({
      machine = {
        kernel = {
          modules = [
            { name = "iscsi_tcp" },
            { name = "dm_thin_pool" },
          ]
        }
      }
    }),
    # DNSネームサーバー設定（静的IP使用のため明示的に指定）
    # kubeletノードIPをLANに制限（Tailscale IP使用を防止）
    yamlencode({
      machine = {
        network = {
          nameservers = [var.gateway, "1.1.1.1", "8.8.8.8"]
        }
        kubelet = {
          nodeIP = {
            validSubnets = ["192.168.11.0/24"]
          }
        }
      }
    }),
    # Tailscale ExtensionServiceConfig（configuration依存を満たすために必須）
    <<-EOT
    apiVersion: v1alpha1
    kind: ExtensionServiceConfig
    name: tailscale
    environment:
      - TS_AUTHKEY=${var.tailscale_authkey}
    EOT
    ,
  ]
}

# Control Plane machine config
data "talos_machine_configuration" "controlplane" {
  for_each = var.control_planes

  cluster_name     = var.cluster_name
  cluster_endpoint = "https://${var.cluster_endpoint}:6443"
  machine_type     = "controlplane"
  machine_secrets  = talos_machine_secrets.this.machine_secrets
  talos_version    = var.talos_version
  kubernetes_version = var.kubernetes_version

  config_patches = concat(local.common_patches, [
    # CPでワークロード実行を許可 + etcdをLANサブネットに制限
    # デフォルトCNI(Flannel)無効化 → Cilium(Flux CDでデプロイ)に移行
    # kube-proxy無効化 → CiliumのeBPFで代替
    yamlencode({
      cluster = {
        allowSchedulingOnControlPlanes = true
        etcd = {
          advertisedSubnets = ["192.168.11.0/24"]
        }
        network = {
          cni = {
            name = "none"
          }
        }
        proxy = {
          disabled = true
        }
      }
    }),
  ])
}

# Worker machine config
data "talos_machine_configuration" "worker" {
  for_each = var.workers

  cluster_name     = var.cluster_name
  cluster_endpoint = "https://${var.cluster_endpoint}:6443"
  machine_type     = "worker"
  machine_secrets  = talos_machine_secrets.this.machine_secrets
  talos_version    = var.talos_version
  kubernetes_version = var.kubernetes_version

  config_patches = concat(local.common_patches, [
    # zswap（メモリの20%を圧縮キャッシュとして使用）
    <<-EOT
    apiVersion: v1alpha1
    kind: ZswapConfig
    maxPoolPercent: 20
    EOT
    ,
  ])
}

# VMへconfig適用
resource "talos_machine_configuration_apply" "controlplane" {
  for_each = var.control_planes

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.controlplane[each.key].machine_configuration
  node                        = each.value.ip

  depends_on = [proxmox_virtual_environment_vm.talos_node]
}

resource "talos_machine_configuration_apply" "worker" {
  for_each = var.workers

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.worker[each.key].machine_configuration
  node                        = each.value.ip

  depends_on = [proxmox_virtual_environment_vm.talos_node]
}

# 最初のCPでブートストラップ
resource "talos_machine_bootstrap" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = values(var.control_planes)[0].ip

  depends_on = [talos_machine_configuration_apply.controlplane]
}

# クラスタヘルスチェック
# CNI=noneのため、Cilium(Flux CD)デプロイ前はNodeがNotReadyになる
# skip_kubernetes_checksでノード到達性とetcdのみ検証し、K8sレベルのReadyチェックをスキップ
data "talos_cluster_health" "this" {
  client_configuration   = talos_machine_secrets.this.client_configuration
  control_plane_nodes    = [for cp in var.control_planes : cp.ip]
  worker_nodes           = [for w in var.workers : w.ip]
  endpoints              = [for cp in var.control_planes : cp.ip]
  skip_kubernetes_checks = true

  depends_on = [talos_machine_bootstrap.this]
}

# kubeconfig取得
resource "talos_cluster_kubeconfig" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = values(var.control_planes)[0].ip

  depends_on = [data.talos_cluster_health.this]
}

# talosconfig取得
data "talos_client_configuration" "this" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.this.client_configuration
  nodes                = [for node in local.all_nodes : node.ip]
  endpoints            = [for cp in var.control_planes : cp.ip]
}
