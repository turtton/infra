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
ssh -i <秘密鍵> root@192.168.10.100
ssh -i <秘密鍵> root@192.168.10.40
ssh -i <秘密鍵> root@192.168.10.101
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

### 3.2 ユーザーへの権限付与

このユーザー自身にも `PVEAuditor` を付与する。privilege separation 有効時、token の有効権限は「user 権限 ∩ token 権限」の積集合となるため、user 側 ACL を消すと token も読めなくなる。

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
| Token ID | `monitoring` |
| Privilege Separation | **チェックを入れる** (推奨) |

CLI:

```bash
pveum user token add monitoring@pve monitoring --privsep 1
```

**出力されるトークン値を控えておくこと。** 再表示はできない。

> **Note:** トークン ID は `ansible/roles/monitoring-agent/defaults/main.yml` の
> `pve_exporter_api_user: "monitoring@pve!monitoring"` と一致している必要がある。
> 異なる ID を使う場合は defaults もしくは group_vars で `pve_exporter_api_user` を
> `<user>@<realm>!<tokenid>` 形式で上書きすること。

### 3.4 トークンへのACL付与（privsep=1 で必須）

`--privsep 1` を付けると、token は user の権限を継承せず**独立した ACL** を持つ。
user 側に `PVEAuditor` を付けただけだと、token は何も読めず exporter は HTTP 200 + 空 body
（PVE API 側で 401）を返す silent failure になる。token 自体にも ACL を必ず付与する:

```bash
pveum acl modify / --tokens 'monitoring@pve!monitoring' --roles PVEAuditor
```

確認:

```bash
pveum acl list
# 期待: 以下の2行が存在すること
#   /  PVEAuditor  user   monitoring@pve             1
#   /  PVEAuditor  token  monitoring@pve!monitoring  1
```

### 3.5 Ansible Vaultへの格納

控えたトークン値を暗号化して保存する:

```bash
cd ansible/
mkdir -p inventory/group_vars/proxmox

# vault.sops.yml ファイルを作成
cat > inventory/group_vars/proxmox/vault.sops.yml <<EOF
pve_exporter_api_token_value: "<控えたトークン値>"
EOF

# SOPSで暗号化
sops --encrypt --in-place inventory/group_vars/proxmox/vault.sops.yml
```

---

## 4. ノード間の疎通確認

各ノードがお互いに通信できることを確認する。

```bash
# mainノードから
ping -c 3 192.168.10.40
ping -c 3 192.168.10.101

# dataノードから
ping -c 3 192.168.10.100
ping -c 3 192.168.10.101
```

---

## 5. トークンローテーション手順

トークンの有効期限切れや漏洩対応で再発行する場合は、次の順序で実施する。**privsep=1 のため
ACL 再付与を飛ばすと exporter は HTTP 200 + 空 body を返す silent failure に陥る**ので、
順序を厳守すること。

1. **PVE 側でトークンを再発行**

   ```bash
   pveum user token remove monitoring@pve monitoring
   pveum user token add monitoring@pve monitoring --privsep 1
   # → 表示されたトークン値を控える
   ```

2. **token ACL を再付与し、`pveum acl list` で存在確認**

   PVE のバージョンや WebUI / CLI の経路によって token 削除時に token-side ACL が
   残るかが変わり得るため、**必ず再付与してから一覧で存在確認**する:

   ```bash
   pveum acl modify / --tokens 'monitoring@pve!monitoring' --roles PVEAuditor
   pveum acl list   # token 行が存在することを確認
   ```

3. **SOPS Vault を更新**

   ```bash
   cd ansible/
   sops inventory/group_vars/proxmox/vault.sops.yml
   # pve_exporter_api_token_value を新しいトークン値に書き換えて保存
   ```

4. **Ansible 適用**（PR から `/ansible-apply` でも可、ローカルでも可）

   ```bash
   ansible-playbook playbooks/site.yml
   ```

5. **メトリクス再取得確認**

   ```bash
   # Prometheus pod から
   kubectl -n monitoring exec sts/prometheus-kube-prometheus-stack-prometheus -c prometheus -- \
     wget -qO- http://192.168.10.100:9221/metrics | grep -c '^pve_node_up'
   # 期待: 1以上
   ```

   Grafana の `proxmox-ve-pve-exporter` ダッシュボードに直近 5 分のデータが
   再表示されることも確認する。

---

## 6. pve-exporter トラブルシュート

### 症状: Grafana で No data、Prometheus targets は up=1

bigtcze/pve-exporter は upstream PVE API が 401 を返しても自身は HTTP 200 + 空 body を返す
silent failure 設計のため、`up{}` だけでは検知できない。次のいずれかが原因のことが多い。

1. **トークン ID 不一致**
   - `ansible/roles/monitoring-agent/defaults/main.yml` の `pve_exporter_api_user` と
     PVE 側の実在トークンが一致していない。
   - 確認: `pveum user token list monitoring@pve`
2. **token 側 ACL 欠落（privsep=1）**
   - user に PVEAuditor が付いていても、token 自身に ACL が無いと読めない。
   - 確認: `pveum acl list | grep 'token .*monitoring'`
   - 修正: §3.4 のコマンドを再実行
3. **user 側 ACL 欠落**
   - privsep=1 では有効権限 = user 側 ACL ∩ token 側 ACL。
     user 側を消すと token 側があっても読めず、同じ silent failure になる。
   - 確認: `pveum acl list | grep 'user .*monitoring@pve'`
   - 修正: §3.2 のコマンドを再実行
4. **SOPS のトークン値が古い**
   - PVE 側で token を再発行したが Ansible 側の `pve_exporter_api_token_value` が
     未更新。§5 のローテーション手順 3〜5 を実施。

### 切り分けに使える確認

```bash
# PVE ノード上で exporter のエラーログを確認
journalctl -u pve-exporter -n 50 --no-pager | grep -i 'error\|401'

# exporter 単体で /metrics が空かを直接確認
curl -s http://localhost:9221/metrics | head -5

# token の実効権限を直接確認 (user∩token の結果が見える)
pveum user token permissions monitoring@pve monitoring
```

`pve_node_up` が出ていれば exporter は健全。空応答なら PVE API への認証が壊れている。

---

## チェックリスト

| 項目 | main | data | toliunit |
|---|---|---|---|
| SSH鍵認証でroot接続可能 | [ ] | [ ] | [ ] |
| Python3がインストール済み | [ ] | [ ] | [ ] |
| `monitoring@pve`ユーザー作成済み | [ ] | - | - |
| user 側 `PVEAuditor` 付与済み | [ ] | - | - |
| APIトークン (`monitoring`, privsep=1) 作成済み | [ ] | - | - |
| token 側 `PVEAuditor` 付与済み | [ ] | - | - |
| トークン値をSOPSで暗号化済み | [ ] | - | - |
| ノード間疎通確認 | [ ] | [ ] | [ ] |

> **Note:** APIトークン関連の設定はProxmoxクラスタ内で共有されるため、1台で実施すれば全ノードに反映される。
