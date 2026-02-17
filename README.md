# infra

Proxmox VE基盤上にKubernetesクラスタを構築し、Flux CDによるGitOpsでマルチテナント運用を行うためのインフラリポジトリ。

## Architecture

```
Git (このリポジトリ)
 ├── ansible/     → Proxmoxノードの構成管理
 ├── terraform/   → Talos Linux VM定義 + クラスタブートストラップ
 ├── talos/       → Kubernetesクラスタ設定 (予定)
 └── clusters/    → Flux CD マニフェスト (予定)
```

全体のアーキテクチャ設計は [gitops-architecture.md](gitops-architecture.md) を参照。

## Current Status

| コンポーネント | 状態 |
|---|---|
| Ansible (Proxmox構成管理) | **実装済み** |
| CI/CD (GitHub Actions) | **実装済み** |
| Terraform (Talos VM + ブートストラップ) | **実装済み** |
| Kubernetes (Talos) | 未着手 |
| Flux CD | 未着手 |

## Managed Nodes

| ホスト名 | ローカルIP | 役割 |
|---|---|---|
| main | 192.168.11.100 | Proxmox VEノード |
| data | 192.168.11.40 | Proxmox VEノード |

## Getting Started

### Prerequisites

- Proxmoxノードの事前設定: [docs/proxmox-prerequisites.md](docs/proxmox-prerequisites.md)
- OpenTofu用Proxmox設定: [docs/proxmox-terraform-prerequisites.md](docs/proxmox-terraform-prerequisites.md)

### Ansible Dry-Run

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
│   ├── versions.tf        # Provider設定 + state暗号化
│   ├── providers.tf       # Proxmox/Talosプロバイダ
│   ├── variables.tf       # 変数定義
│   ├── terraform.tfvars   # 変数値
│   ├── talos-image.tf     # Talosイメージ管理
│   ├── vms.tf             # K8s用VM定義
│   ├── talos.tf           # Talos設定・ブートストラップ
│   └── outputs.tf         # 出力定義
├── docs/                  # 運用ドキュメント
└── gitops-architecture.md # 全体アーキテクチャ設計
```
