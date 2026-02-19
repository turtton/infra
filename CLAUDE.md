# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Proxmox VE上にTalos Linux Kubernetesクラスタを構築・運用するホームラボインフラリポジトリ。Ansibleでベアメタル構成管理、OpenTofuでVM作成とクラスタブートストラップを行う。

## Development Environment

Nix Flakeで開発環境を提供。`direnv allow` または `nix develop` で以下のツールが利用可能になる:
- `ansible`, `ansible-lint`, `opentofu`, `talosctl`, `kubectl`

Nixフォーマッタ: `nix fmt` (`nixfmt-tree`)

## Common Commands

### Ansible (`ansible/` ディレクトリで実行)

```bash
cd ansible/
# Dry-run (差分確認)
ansible-playbook playbooks/site.yml --check --diff --ask-vault-pass
# 本番適用
ansible-playbook playbooks/site.yml --ask-vault-pass
# ネットワーク設定のみ
ansible-playbook playbooks/network-update.yml --ask-vault-pass
# Lint
ansible-lint
```

### OpenTofu (`terraform/` ディレクトリで実行)

```bash
cd terraform/
tofu init
tofu plan
tofu apply
# kubeconfig / talosconfig 取得
tofu output -raw kubeconfig > ~/.kube/config
tofu output -raw talosconfig > ~/.talos/config
```

State暗号化パスフレーズは環境変数 `TF_VAR_state_encryption_passphrase` で渡す。

## Architecture

```
ansible/     → Proxmox VEノード(main: 192.168.11.100, data: 192.168.11.40)の構成管理
terraform/   → Talos Linux VM作成 + Kubernetesクラスタブートストラップ (OpenTofu)
docs/        → Proxmoxの事前設定手順など運用ドキュメント
```

### Ansible構成

- `ansible.cfg`: inventory=`inventory/hosts.yml`、接続ユーザーは`root`、privilege escalation無効
- ホスト固有変数: `inventory/host_vars/{main,data}/network.yml`
- 暗号化変数: `inventory/group_vars/proxmox/vault.yml` (Ansible Vault, AES256)
- ロール: `proxmox-base`(パッケージ・NTP・SSH), `proxmox-network`(ブリッジ設定), `tailscale`, `monitoring-agent`(prometheus-pve-exporter)

### OpenTofu構成

- プロバイダ: `bpg/proxmox`, `siderolabs/talos`
- `terraform.tfstate` は暗号化(PBKDF2+AES-GCM)してgitにコミットされている — 削除しないこと
- `.terraform.lock.hcl` はgitignoreされている — `tofu init` で再生成
- Talos拡張: qemu-guest-agent, tailscale, iscsi-tools, util-linux-tools
- マシン設定パッチ: Longhornカーネルモジュール、DNS、kubelet nodeIP制限(LAN帯域のみ)、Tailscale

## CI/CD

PRで該当ディレクトリを変更すると自動でdry-run/planが実行され、結果がPRコメントに投稿される。

- `ansible/**` 変更 → dry-run実行 → `/ansible-apply` コメントで適用
- `terraform/**` 変更 → plan実行 → `/tf-apply` コメントで適用

Apply権限は `turtton` ユーザーのみ。CIからのTailscale接続でProxmoxにアクセスする。

## Important Notes

- `terraform.tfstate` は暗号化済みで意図的にgit管理されている。絶対にgitignoreに追加したり削除しないこと
- Ansible VaultパスワードはCI上 `/tmp/.vault-pass` に一時書き出しされ、always stepで削除される
- Proxmoxプロバイダは自己署名証明書のため `insecure = true` で接続する
- ドキュメントは日本語で記述する
