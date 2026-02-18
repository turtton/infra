# Proxmox VE OpenTofu Pre-Configuration Guide

OpenTofuでProxmox上にVMを作成・管理するために必要な事前設定をまとめる。

---

## 1. ユーザー作成

OpenTofu用のユーザーを作成する。

Proxmox WebUI: Datacenter → Permissions → Users → Add

| 項目 | 値 |
|---|---|
| User name | `terraform` |
| Realm | `pve` (Proxmox VE authentication) |
| Enabled | Yes |

CLI:

```bash
pveum user add terraform@pve
```

---

## 2. カスタムロール作成

OpenTofuがVM/リソースを管理するために必要な権限を持つロールを作成する。

CLI:

```bash
pveum role add TerraformRole -privs "VM.Allocate VM.Audit VM.Clone VM.Config.CDROM VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Migrate VM.Monitor VM.PowerMgmt Datastore.AllocateSpace Datastore.AllocateTemplate Datastore.Audit SDN.Use Sys.Audit Sys.Modify"
```

### 権限一覧

| 権限 | 用途 |
|---|---|
| `VM.Allocate` | VM作成・削除 |
| `VM.Audit` | VM構成・ステータス参照 |
| `VM.Clone` | VMクローン |
| `VM.Config.CDROM` | CD/DVDドライブ設定 |
| `VM.Config.CPU` | CPU設定変更 |
| `VM.Config.Cloudinit` | Cloud-Init設定 |
| `VM.Config.Disk` | ディスク設定変更 |
| `VM.Config.HWType` | ハードウェアタイプ設定 |
| `VM.Config.Memory` | メモリ設定変更 |
| `VM.Config.Network` | ネットワーク設定変更 |
| `VM.Config.Options` | その他オプション設定 |
| `VM.Migrate` | VMマイグレーション |
| `VM.Monitor` | VMモニターアクセス |
| `VM.PowerMgmt` | VM電源管理 |
| `Datastore.AllocateSpace` | ストレージ領域確保 |
| `Datastore.AllocateTemplate` | テンプレート管理 |
| `Datastore.Audit` | ストレージ情報参照 |
| `SDN.Use` | SDNネットワーク利用 |
| `Sys.Audit` | システム情報参照 |
| `Sys.Modify` | システム設定変更（ISOダウンロード等） |

---

## 3. ロール割り当て

ユーザーにロールを割り当てる。`/`パスに付与することで全リソースへのアクセスを許可する。

Proxmox WebUI: Datacenter → Permissions → Add → User Permission

| 項目 | 値 |
|---|---|
| Path | `/` |
| User | `terraform@pve` |
| Role | `TerraformRole` |

CLI:

```bash
pveum acl modify / --users terraform@pve --roles TerraformRole
```

---

## 4. APIトークン作成

OpenTofuがAPI経由でProxmoxに接続するためのトークンを作成する。

Proxmox WebUI: Datacenter → Permissions → API Tokens → Add

| 項目 | 値 |
|---|---|
| User | `terraform@pve` |
| Token ID | `tofu` |
| Privilege Separation | **チェックを外す** |

CLI:

```bash
pveum user token add terraform@pve tofu --privsep 0
```

**出力されるトークン値を控えておくこと。** 再表示はできない。

トークンの形式: `terraform@pve!tofu=<token-value>`

---

## 5. 環境変数の設定

### ローカル実行

```bash
export PROXMOX_VE_ENDPOINT="https://<proxmox-ip>:8006"
export PROXMOX_VE_API_TOKEN="terraform@pve!tofu=<token-value>"
export TF_VAR_state_encryption_passphrase="<16文字以上のパスフレーズ>"
export TF_VAR_tailscale_authkey="tskey-auth-..."
```

### GitHub Actions

以下のSecretsを登録する:

| Secret | 値 |
|---|---|
| `PROXMOX_VE_ENDPOINT` | `https://<tailscale-ip>:8006` |
| `PROXMOX_VE_API_TOKEN` | `terraform@pve!tofu=<token-value>` |
| `TOFU_STATE_PASSPHRASE` | State暗号化パスフレーズ（32文字以上推奨） |
| `TAILSCALE_AUTHKEY` | Talosノード用reusable authkey |

---

## 6. State Commitリカバリ手順

`tofu apply`は成功したが、暗号化stateのgit commit/pushに失敗した場合のリカバリ手順。

### ローカルからリカバリ

```bash
cd terraform/

# 環境変数を設定した状態で
tofu init
tofu state pull > /dev/null  # stateが読めることを確認

# stateファイルをcommit
git add terraform/terraform.tfstate
git commit -m "Recover terraform state"
git push
```

### CIでapply成功・push失敗の場合

1. GitHub ActionsのCIログからapply結果を確認
2. ローカルで環境変数を設定し、`tofu plan`で差分がないことを確認
3. 差分がある場合は`tofu import`でリソースを再インポート
4. stateファイルをcommit & push

---

## チェックリスト

| 項目 | 完了 |
|---|---|
| `terraform@pve`ユーザー作成済み | [ ] |
| `TerraformRole`カスタムロール作成済み | [ ] |
| ロール割り当て済み（`/`パス） | [ ] |
| APIトークン(`tofu`)作成済み | [ ] |
| トークン値を控えた | [ ] |
| ローカル環境変数設定済み | [ ] |
| GitHub Secrets登録済み | [ ] |

> **Note:** ユーザー・ロール・トークンの設定はProxmoxクラスタ内で共有されるため、1台で実施すれば全ノードに反映される。
