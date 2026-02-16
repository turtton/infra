# Proxmox VE Pre-Configuration Guide

Ansible playbookを実行する前に、各Proxmoxノードで手動で行う必要がある事前設定をまとめる。

---

## 1. SSH公開鍵の配置

Ansibleはroot SSHでノードに接続する。CI/CDランナー（GitHub Actions）の鍵を各ノードに配置する。

### 手順

各Proxmoxノードで以下を実行:

```bash
# CI/CD用の公開鍵を追記
echo $(cat ~/.ssh/infra-ci) >> /etc/pve/priv/authorized_keys
```

### 確認

外部から鍵認証でSSH接続できることを確認:

```bash
ssh -i <秘密鍵> root@192.168.11.100
ssh -i <秘密鍵> root@192.168.11.40
```

---

## 2. Python3の確認

Proxmox VE (Debian) にはPython3がプリインストールされているが、念のため確認する。

```bash
python3 --version
```

もし存在しなければ:

```bash
apt update && apt install -y python3
```

---

## 3. prometheus-pve-exporter用APIトークンの作成

`monitoring-agent`ロールはProxmox APIにトークン認証で接続する。WebUIまたはCLIでユーザーとトークンを作成する。

### 3.1 ユーザー作成

Proxmox WebUI: Datacenter → Permissions → Users → Add

| 項目 | 値 |
|---|---|
| User name | `monitoring` |
| Realm | `pve` (Proxmox VE authentication) |
| Enabled | Yes |

CLI:

```bash
pveum user add monitoring@pve
```

### 3.2 権限の付与

exporter がメトリクスを取得するために読み取り権限が必要。

Proxmox WebUI: Datacenter → Permissions → Add → User Permission

| 項目 | 値 |
|---|---|
| Path | `/` |
| User | `monitoring@pve` |
| Role | `PVEAuditor` |

CLI:

```bash
pveum acl modify / --users monitoring@pve --roles PVEAuditor
```

### 3.3 APIトークンの作成

Proxmox WebUI: Datacenter → Permissions → API Tokens → Add

| 項目 | 値 |
|---|---|
| User | `monitoring@pve` |
| Token ID | `exporter` |
| Privilege Separation | **チェックを外す** |

CLI:

```bash
pveum user token add monitoring@pve exporter --privsep 0
```

**出力されるトークン値を控えておくこと。** 再表示はできない。

### 3.4 Ansible Vaultへの格納

控えたトークン値を暗号化して保存する:

```bash
cd ansible/
mkdir -p inventory/group_vars/proxmox

# vaultファイルを作成 (暗号化パスワードを求められる)
ansible-vault create inventory/group_vars/proxmox/vault.yml
```

以下の内容を記述:

```yaml
pve_exporter_api_token_value: "<控えたトークン値>"
```

---

## 4. ノード間の疎通確認

各ノードがお互いに通信できることを確認する。

```bash
# mainノードから
ping -c 3 192.168.11.40

# dataノードから
ping -c 3 192.168.11.100
```

---

## チェックリスト

| 項目 | main | data |
|---|---|---|
| SSH鍵認証でroot接続可能 | [ ] | [ ] |
| Python3がインストール済み | [ ] | [ ] |
| `monitoring@pve`ユーザー作成済み | [ ] | - |
| APIトークン (`exporter`) 作成済み | [ ] | - |
| `PVEAuditor`権限付与済み | [ ] | - |
| トークン値をansible-vaultに格納済み | [ ] | - |
| ノード間疎通確認 | [ ] | [ ] |

> **Note:** APIトークン関連の設定はProxmoxクラスタ内で共有されるため、1台で実施すれば全ノードに反映される。
