# Ansible

Proxmox VEノードとVPS (os3-387) の構成管理。

## Roles

### Proxmox向け

| ロール | 概要 |
|---|---|
| `proxmox_base` | パッケージ管理、timezone (Asia/Tokyo)、DNS (Cloudflare)、NTP (chrony)、SSH hardening |
| `proxmox_network` | `/etc/network/interfaces` のテンプレート管理 |
| `tailscale` | Tailscale VPNのインストール・接続 |
| `monitoring_agent` | prometheus-pve-exporter (uv、API Token認証、systemd) |

### VPS (os3-387) 向け

| ロール | 概要 |
|---|---|
| `bootstrap_vps` | 初期ブートストラップ (python3導入、ansibleユーザー作成) |
| `linux_base` | ベースパッケージ、hostname、timezone、unattended-upgrades |
| `ssh_hardening` | sshd設定のテンプレート管理 (ListenAddress制限、socket activation無効化) |
| `docker_host` | Docker CE + Compose pluginのインストール |
| `nftables_vps` | nftablesファイアウォール (`/etc/nftables.conf` テンプレート管理) |
| `haproxy_minecraft` | Minecraftプロキシ用HAProxy (Docker Compose + systemd、SNI的なドメイン振り分け) |

## Playbooks

| Playbook | 用途 |
|---|---|
| `site.yml` | Proxmoxノードに全ロールを順番に適用 |
| `network-update.yml` | ネットワーク設定のみ更新 (`serial: 1` で1台ずつ) |
| `bootstrap-vps.yml` | VPSの初期ブートストラップ (`--limit os3-387-26840` 必須) |
| `vps.yml` | VPS (os3-387) の構成適用 |

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
ansible-playbook playbooks/site.yml --tags proxmox_base
```

### VPS (os3-387)

対象ホスト: `os3-387-26840` (さくらVPS、Tailscale名 `sakura-mcproxy-1`)。MinecraftプロキシとしてHAProxyを稼働させている。

```bash
# 初期セットアップ (debianユーザーで実行、--limit 必須)
ansible-playbook playbooks/bootstrap-vps.yml --limit os3-387-26840

# 通常の構成適用
ansible-playbook playbooks/vps.yml --check --diff
ansible-playbook playbooks/vps.yml

# haproxy_minecraftのみ適用
ansible-playbook playbooks/vps.yml --tags haproxy_minecraft
```

初回セットアップの詳細は [../docs/vps-bootstrap.md](../docs/vps-bootstrap.md) を参照。

## Inventory

```
inventory/
├── hosts.yml                        # ノード定義 (proxmox / switches / vpsグループ)
├── host_vars/
│   ├── main/network.yml             # main固有のネットワーク設定
│   ├── data/network.yml             # data固有のネットワーク設定
│   └── toliunit/network.yml         # toliunit固有のネットワーク設定
└── group_vars/
    └── proxmox/vault.sops.yml        # SOPS暗号化された機密変数
```

`hosts.yml` のグループ:

| グループ | ホスト | 用途 |
|---|---|---|
| `proxmox` | main, data, toliunit | Proxmox VEノード |
| `switches` | icx7250 | スイッチ (手動実行のみ) |
| `vps` | os3-387-26840 (`sakura-mcproxy-1`) | さくらVPS (Minecraftプロキシ) |

### Secret管理

機密変数 (`monitoring_agent_pve_exporter_api_token_value`, `tailscale_auth_key`) は SOPS + Age で暗号化して管理する。

```bash
# 編集
sops inventory/group_vars/proxmox/vault.sops.yml

# 内容確認
sops --decrypt inventory/group_vars/proxmox/vault.sops.yml
```

## Role Details

### proxmox_base

全ノード共通の基本設定。

- apt update + 共通パッケージ (curl, wget, htop, vim, tmux, jq 等)
- timezone: `Asia/Tokyo`
- DNS: Cloudflare (`1.1.1.1`, `1.0.0.1`) → `/etc/resolv.conf`
- NTP: chrony (`ntp.nict.jp`, `ntp.jst.mfeed.ad.jp`)
- SSH: 鍵認証のみ、パスワード認証無効 → `/etc/ssh/sshd_config.d/hardening.conf`

### proxmox_network

ノードごとのネットワーク設定。`host_vars` で物理NIC名・IPアドレスを定義。

- `/etc/network/interfaces` をテンプレートで管理
- 変更時は自動バックアップ
- `network-update.yml` で `serial: 1` のローリング適用

### tailscale

Tailscale SaaS接続。

- 公式aptリポジトリからインストール
- pre-auth keyで自動接続 (`no_log` で出力抑制)
- 既にRunning状態ならスキップ (冪等性)

### monitoring_agent

prometheus-pve-exporterをuvでインストール。

- 専用システムユーザー (`pve-exporter`) で実行
- API Token認証 (設定ファイルは `0600`)
- systemd hardening (NoNewPrivileges, ProtectSystem=strict)
- port: `9221`

### bootstrap_vps

VPSの初期ブートストラップ。`bootstrap-vps.yml` からdebianユーザーで実行する。

- 安全のため `--limit os3-387-26840` 必須 (未指定時はfail)
- python3 / sudo / acl のインストール
- ansibleユーザー・グループの作成

### linux_base

VPS向けの基本設定。proxmox_baseのLinux汎用版。

- ベースパッケージ、hostname、timezone
- unattended-upgrades (自動セキュリティ更新)

### ssh_hardening

VPS向けsshd設定。

- `sshd_config` をテンプレート管理 (`sshd -t` でバリデーション)
- ListenAddressを指定インターフェースに制限
- ssh.socket activationを無効化しssh.serviceで管理

### docker_host

Docker CEのインストール。

- 公式aptリポジトリからdocker-ce + compose pluginを導入

### nftables_vps

VPS向けファイアウォール。

- `/etc/nftables.conf` をテンプレート管理
- 変更時は `reload nftables`

### haproxy_minecraft

Minecraft Java Editionのドメイン振り分けプロキシ (os3-387で稼働)。

- HAProxyをDocker Composeで起動し、systemd (`haproxy-minecraft.service`) で管理
- LuaスクリプトでMinecraft handshakeをパースし、接続先ホスト名でバックエンドを振り分け
- 未設定ドメイン・IP直打ちは `reject_unknown_host` で拒否
- 設定変更時は自動でrestart

サーバー追加は `roles/haproxy_minecraft/defaults/main.yml` の `haproxy_minecraft_servers` に追記する:

```yaml
haproxy_minecraft_servers:
  - domain: example.lepinoid.net   # クライアントが接続に使うドメイン (完全一致)
    backend: some-tailscale-host:25565  # VPSから名前解決可能なバックエンド
```

- `domain` は完全一致 (`-m str`) で判定される
- `backend` のホスト名はHAProxy起動時に名前解決される (`init-addr libc,last`) ため、VPSのDNS/Tailscaleから解決できる必要がある
- 反映は `ansible-playbook playbooks/vps.yml --tags haproxy_minecraft`
