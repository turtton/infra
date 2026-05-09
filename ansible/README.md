# Ansible

Proxmox VEノードの構成管理。

## Roles

| ロール | 概要 |
|---|---|
| `proxmox-base` | パッケージ管理、timezone (Asia/Tokyo)、DNS (Cloudflare)、NTP (chrony)、SSH hardening |
| `proxmox-network` | `/etc/network/interfaces` のテンプレート管理 |
| `tailscale` | Tailscale VPNのインストール・接続 |
| `monitoring-agent` | prometheus-pve-exporter (uv、API Token認証、systemd) |

## Playbooks

| Playbook | 用途 |
|---|---|
| `site.yml` | 全ロールを順番に適用 |
| `network-update.yml` | ネットワーク設定のみ更新 (`serial: 1` で1台ずつ) |

## Usage

### Full Apply

```bash
ansible-playbook playbooks/site.yml
```

### Dry-Run (変更内容を確認)

```bash
ansible-playbook playbooks/site.yml --check --diff
```

### ネットワーク設定のみ更新

```bash
ansible-playbook playbooks/network-update.yml
```

### 特定ノードのみ

```bash
ansible-playbook playbooks/site.yml --limit main
```

### 特定ロールのみ

```bash
ansible-playbook playbooks/site.yml --tags proxmox-base
```

## Inventory

```
inventory/
├── hosts.yml                        # ノード定義 (proxmoxグループ)
├── host_vars/
│   ├── main/network.yml             # main固有のネットワーク設定
│   ├── data/network.yml             # data固有のネットワーク設定
│   └── toliunit/network.yml         # toliunit固有のネットワーク設定
└── group_vars/
    └── proxmox/vault.sops.yml        # SOPS暗号化された機密変数
```

### Secret管理

機密変数 (`pve_exporter_api_token_value`, `tailscale_auth_key`) は SOPS + Age で暗号化して管理する。

```bash
# 編集
sops inventory/group_vars/proxmox/vault.sops.yml

# 内容確認
sops --decrypt inventory/group_vars/proxmox/vault.sops.yml
```

## Role Details

### proxmox-base

全ノード共通の基本設定。

- apt update + 共通パッケージ (curl, wget, htop, vim, tmux, jq 等)
- timezone: `Asia/Tokyo`
- DNS: Cloudflare (`1.1.1.1`, `1.0.0.1`) → `/etc/resolv.conf`
- NTP: chrony (`ntp.nict.jp`, `ntp.jst.mfeed.ad.jp`)
- SSH: 鍵認証のみ、パスワード認証無効 → `/etc/ssh/sshd_config.d/hardening.conf`

### proxmox-network

ノードごとのネットワーク設定。`host_vars` で物理NIC名・IPアドレスを定義。

- `/etc/network/interfaces` をテンプレートで管理
- 変更時は自動バックアップ
- `network-update.yml` で `serial: 1` のローリング適用

### tailscale

Tailscale SaaS接続。

- 公式aptリポジトリからインストール
- pre-auth keyで自動接続 (`no_log` で出力抑制)
- 既にRunning状態ならスキップ (冪等性)

### monitoring-agent

prometheus-pve-exporterをuvでインストール。

- 専用システムユーザー (`pve-exporter`) で実行
- API Token認証 (設定ファイルは `0600`)
- systemd hardening (NoNewPrivileges, ProtectSystem=strict)
- port: `9221`
