# Terraform (OpenTofu)

Proxmox VE上にTalos Linux VMを作成し、Kubernetesクラスタのブートストラップまでを自動化する。

## Stack

| ツール | バージョン | 用途 |
|---|---|---|
| OpenTofu | >= 1.8.0 | IaC |
| bpg/proxmox | ~> 0.78 | Proxmox VMリソース管理 |
| siderolabs/talos | ~> 0.7 | Talos machine config + ブートストラップ |
| Talos Linux | v1.12.1 | Kubernetes OS |
| Kubernetes | 1.35.0 | コンテナオーケストレーション |

## File Structure

```
terraform/
├── versions.tf        # Provider要件 + state暗号化 (PBKDF2+AES-GCM)
├── providers.tf       # Proxmox / Talosプロバイダ設定
├── variables.tf       # 変数定義
├── terraform.tfvars   # 変数値 (IP, VMスペック等)
├── talos-image.tf     # Talosイメージ (factory.talos.dev + Proxmoxダウンロード)
├── vms.tf             # Proxmox VM定義 (CP + Worker統合管理)
├── talos.tf           # Talos machine config, ブートストラップ, ヘルスチェック
├── outputs.tf         # talosconfig / kubeconfig出力
└── terraform.tfstate  # 暗号化済みstate (git管理)
```

## Talos System Extensions

イメージに含まれる拡張:

| 拡張 | 用途 |
|---|---|
| `siderolabs/qemu-guest-agent` | Proxmox VM検知・メトリクス取得 |
| `siderolabs/tailscale` | 各ノードをTailscaleに接続 |
| `siderolabs/iscsi-tools` | Longhorn iSCSIボリュームアタッチ |
| `siderolabs/util-linux-tools` | Longhorn用 `nsenter` コマンド |

## Machine Config Patches

### 全ノード共通 (common_patches)

- **カーネルモジュール**: `iscsi_tcp`, `dm_thin_pool` (Longhorn用)
- **DNS**: ゲートウェイ + 1.1.1.1 + 8.8.8.8 (静的IPのため明示指定)
- **kubelet nodeIP**: `192.168.10.0/24` に制限 (Tailscale IP使用防止)
- **Tailscale**: `ExtensionServiceConfig` でauthkeyを注入（30日ごとの`time_rotating`でローテーション。詳細は下記）

### Control Plane

- `allowSchedulingOnControlPlanes: true` (ワークロード実行可能)
- `etcd.advertisedSubnets`: `192.168.10.0/24` に制限 (Tailscale IP広告防止)

### Worker

- `ZswapConfig`: メモリの20%を圧縮スワップキャッシュとして使用

## Usage

### 初期セットアップ

```bash
# 環境変数を設定
export PROXMOX_VE_ENDPOINT="https://<proxmox-ip>:8006"
export PROXMOX_VE_API_TOKEN="terraform@pve!tofu=<token>"
export TF_VAR_state_encryption_passphrase="<passphrase>"
export TF_VAR_tailscale_oauth_client_id="<oauth-client-id>"
export TF_VAR_tailscale_oauth_client_secret="<oauth-secret>"
export TF_VAR_tailscale_tailnet="<tailnet>"

# 実行
tofu init
tofu plan
tofu apply
```

### kubeconfig / talosconfig の取得

```bash
tofu output -raw kubeconfig > ~/.kube/config
tofu output -raw talosconfig > ~/.talos/config
```

### State管理

Stateは PBKDF2+AES-GCM で暗号化され、`terraform.tfstate` としてgitにコミットされる。パスフレーズなしでは復号不可。

apply成功後のstateコミット:

```bash
git add terraform.tfstate
git commit -m "Update encrypted terraform state"
git push
```

CI (`/tf-apply`) では自動的にcommit & pushされる。

## Network Architecture

```
GitHub Actions Runner
  │ (Tailscale, --accept-routes)
  │
  ├──→ Proxmox API (port 8006)     ← tofu plan/apply
  ├──→ Proxmox SSH (port 22)       ← イメージアップロード
  └──→ Talos API (port 50000)      ← machine config適用
       │
       │ (Tailscaleサブネットルーティング 192.168.10.0/24)
       │
  Proxmox Node (subnet router)
       │
       ├── cp-1   (192.168.10.110) ← K8s API port 6443
       └── worker-1 (192.168.10.120)
```

Talos VMは初回起動時にTailscaleが未設定のため、LAN IP経由でしかアクセスできない。Proxmoxノードをサブネットルーターとして設定し、Tailscale経由でLANに到達可能にする。

## Prerequisites

- Proxmox側の事前設定: [docs/proxmox-terraform-prerequisites.md](../docs/proxmox-terraform-prerequisites.md)
- Tailscaleサブネットルーティングの設定 (上記ドキュメント内 Section 5)

## Known Issues

- `SwapVolumeConfig` を `system_disk` に対して使用するとブート失敗する ([siderolabs/talos#12234](https://github.com/siderolabs/talos/issues/12234))。ディスクswapの代わりにZswapConfig(メモリ圧縮キャッシュ)を使用している。

## Tailscale auth key ローテーション

ノード用auth keyは `tailscale_tailnet_key` リソースで Terraform 管理している。`time_rotating` (30日) が期限を迎えると、次の apply でキーが置き換えられる。

### ローテーション手順 (2段階 apply が必須)

Talos provider は、plan 時に値が確定していない(unknown)入力に対して `machine_configuration_hash` を正しく計画できない問題がある。新規キー発行とノードへの適用を同じ apply で行うと `Provider produced inconsistent final plan` エラーになるため、必ず2段階で行うこと。

```bash
# 1. キー系リソースのみ先に apply (新キーを state に確定させ、plan 時の既知値にする)
tofu apply -target=time_rotating.tailscale_authkey -target=tailscale_tailnet_key.nodes

# 2. 通常 apply (plan 時にキー値が既知のため machine config の hash が一致する)
tofu apply
```

CI の `/tf-apply` (full apply のみ) ではローテーションできない。定期ローテーションを自動化する場合は、CI 側でも上記2コマンドを順に実行する workflow が必要。現時点では手動での2段階 apply が前提。

ノードへの反映は `apply` 時にmachine configが再計算され、Talos がextension serviceを再起動する。ワークロードの再起動は短期的だが、apply を実行するタイミングに注意。ローテーション後は Tailscale 管理画面で旧キーが失効済み(または手動で失効)であることを確認すること。
