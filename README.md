# infra

Proxmox VE基盤上にKubernetesクラスタを構築し、Flux CDによるGitOpsでマルチテナント運用を行うためのインフラリポジトリ。

## Architecture

```
Git (このリポジトリ)
 ├── ansible/     → Proxmoxノードの構成管理
 ├── terraform/   → Talos Linux VM作成 + クラスタブートストラップ
 ├── talos/       → Kubernetesクラスタ設定 (予定)
 └── clusters/    → Flux CD マニフェスト (GitOps)
```

全体のアーキテクチャ設計は [gitops-architecture.md](gitops-architecture.md) を参照。

## Current Status

| コンポーネント | 状態 |
|---|---|
| Ansible (Proxmox構成管理) | **実装済み** |
| CI/CD (GitHub Actions) | **実装済み** |
| OpenTofu (Talos VM + ブートストラップ) | **実装済み** |
| Kubernetes (Talos) | **稼働中** |
| Flux CD | **Bootstrap済み** |

## Cluster

| ノード名 | IP | 役割 | ホストノード | CPU | RAM |
|---|---|---|---|---|---|
| cp-1 | 192.168.11.110 | Control Plane (schedulable) | main | 4 | 24GB |
| worker-1 | 192.168.11.120 | Worker (Longhornストレージ) | data | 1 | 4GB |

## Proxmox Nodes

| ホスト名 | ローカルIP | 役割 |
|---|---|---|
| main | 192.168.11.100 | Proxmox VEノード |
| data | 192.168.11.40 | Proxmox VEノード |

## Getting Started

### Prerequisites

- Proxmoxノードの事前設定: [docs/proxmox-prerequisites.md](docs/proxmox-prerequisites.md)
- OpenTofu用Proxmox設定: [docs/proxmox-terraform-prerequisites.md](docs/proxmox-terraform-prerequisites.md)

### Ansible

```bash
cd ansible/
ansible-playbook playbooks/site.yml --check --diff --ask-vault-pass
```

### OpenTofu

```bash
cd terraform/
tofu init
tofu plan
tofu apply
```

詳細は [terraform/README.md](terraform/README.md) を参照。

### kubeconfig / talosconfig の取得

```bash
cd terraform/
tofu output -raw kubeconfig > ~/.kube/config
tofu output -raw talosconfig > ~/.talos/config
```

### Flux CD

クラスタへのkubeconfig接続が設定済みであること。

#### CNI（Cilium）の手動インストール

Talosの構成で `cni.name = "none"`（Flannel無効化）としているため、クラスタ初期状態ではノードが `NotReady` になる。
Flux CDのPod自体がネットワークを必要とするため、bootstrap前にCiliumを手動でインストールする必要がある。

```bash
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium --version 1.19.1 --namespace kube-system \
  --set ipam.mode=kubernetes \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=localhost \
  --set k8sServicePort=7445 \
  --set cgroup.autoMount.enabled=false \
  --set cgroup.hostRoot=/sys/fs/cgroup \
  --set 'securityContext.capabilities.ciliumAgent={CHOWN,KILL,NET_ADMIN,NET_RAW,IPC_LOCK,SYS_ADMIN,SYS_RESOURCE,DAC_OVERRIDE,FOWNER,SETGID,SETUID}' \
  --set 'securityContext.capabilities.cleanCiliumState={NET_ADMIN,SYS_ADMIN,SYS_RESOURCE}' \
  --set operator.replicas=1 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true

# ノードがReadyになるまで待機
kubectl wait --for=condition=Ready nodes --all --timeout=300s
```

bootstrap後はFluxのHelmReleaseがCiliumの管理を引き継ぐ。

#### SOPS Age鍵のSecret作成

Flux CDが暗号化されたSecretを復号するために、Age秘密鍵のSecretを作成する。

```bash
# SOPS_AGE_KEY_CMD が設定済みの場合
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-literal=age.agekey="$(eval $SOPS_AGE_KEY_CMD)"

# age.keyファイルがある場合
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey=age.key
```

#### Bootstrap

```bash
# 前提条件チェック
flux check --pre

# Bootstrap（GitHub PATが必要）
GITHUB_TOKEN=$(gh auth token) \
flux bootstrap github \
  --owner=turtton \
  --repository=infra \
  --path=clusters/main \
  --personal \
  --branch=main

# 状態確認
flux check
flux get sources git
flux get kustomizations
```

### CI/CD

- PRで `ansible/` 配下を変更すると自動dry-runが実行される。`/ansible-apply` コメントで本番適用。
- PRで `terraform/` 配下を変更すると自動planが実行される。`/tf-apply` コメントで本番適用。

詳細は [.github/workflows/README.md](.github/workflows/README.md) を参照。

## Directory Structure

```
.
├── .github/workflows/     # CI/CDパイプライン
├── ansible/               # Proxmox構成管理
│   ├── inventory/         # ノード定義・変数
│   ├── playbooks/         # 実行エントリポイント
│   └── roles/             # Ansibleロール
├── terraform/             # OpenTofu (Talos VM + クラスタ)
├── clusters/              # Flux CD マニフェスト (GitOps)
│   └── main/              # メインクラスタ定義
│       └── flux-system/   # Flux コンポーネント (bootstrap生成)
├── docs/                  # 運用ドキュメント
└── gitops-architecture.md # 全体アーキテクチャ設計
```
