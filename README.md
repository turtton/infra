# infra

Proxmox VE基盤上にKubernetesクラスタを構築し、Flux CDによるGitOpsでマルチテナント運用を行うためのインフラリポジトリ。

## Architecture

```
Git (このリポジトリ)
 ├── ansible/     → Proxmoxノードの構成管理
 ├── terraform/   → Talos Linux VM作成 + クラスタブートストラップ
 ├── talos/       → Kubernetesクラスタ設定 (予定)
 └── clusters/    → Flux CD マニフェスト (予定)
```

全体のアーキテクチャ設計は [gitops-architecture.md](gitops-architecture.md) を参照。

## Current Status

| コンポーネント | 状態 |
|---|---|
| Ansible (Proxmox構成管理) | **実装済み** |
| CI/CD (GitHub Actions) | **実装済み** |
| OpenTofu (Talos VM + ブートストラップ) | **実装済み** |
| Kubernetes (Talos) | **稼働中** |
| Flux CD | 未着手 |

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
├── docs/                  # 運用ドキュメント
└── gitops-architecture.md # 全体アーキテクチャ設計
```
