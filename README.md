# infra

Proxmox VE基盤上にKubernetesクラスタを構築し、Flux CDによるGitOpsでマルチテナント運用を行うためのインフラリポジトリ。

## Architecture

```
Git (このリポジトリ)
 ├── ansible/     → Proxmoxノードの構成管理
 ├── terraform/   → VM/LXC定義 (予定)
 ├── talos/       → Kubernetesクラスタ設定 (予定)
 └── clusters/    → Flux CD マニフェスト (予定)
```

全体のアーキテクチャ設計は [gitops-architecture.md](gitops-architecture.md) を参照。

## Current Status

| コンポーネント | 状態 |
|---|---|
| Ansible (Proxmox構成管理) | **実装済み** |
| CI/CD (GitHub Actions) | **実装済み** |
| Terraform | 未着手 |
| Kubernetes (Talos) | 未着手 |
| Flux CD | 未着手 |

## Managed Nodes

| ホスト名 | ローカルIP | 役割 |
|---|---|---|
| main | 192.168.11.100 | Proxmox VEノード |
| data | 192.168.11.40 | Proxmox VEノード |

## Getting Started

### Prerequisites

Proxmoxノードの事前設定は [docs/proxmox-prerequisites.md](docs/proxmox-prerequisites.md) を参照。

### Ansible Dry-Run

```bash
cd ansible/
ansible-playbook playbooks/site.yml --check --diff --ask-vault-pass
```

### CI/CD

PRで `ansible/` 配下を変更すると自動dry-runが実行される。`/apply` コメントで本番適用。

詳細は [.github/workflows/README.md](.github/workflows/README.md) を参照。

## Directory Structure

```
.
├── .github/workflows/     # CI/CDパイプライン
├── ansible/               # Proxmox構成管理
│   ├── inventory/         # ノード定義・変数
│   ├── playbooks/         # 実行エントリポイント
│   └── roles/             # Ansibleロール
├── docs/                  # 運用ドキュメント
└── gitops-architecture.md # 全体アーキテクチャ設計
```
