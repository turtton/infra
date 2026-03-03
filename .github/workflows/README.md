# CI/CD Workflows

Ansible playbookとOpenTofuをGitHub Actions経由で実行するためのワークフロー。

## Workflows

### Ansible

#### ansible-check.yml (Dry-Run)

PRで `ansible/` 配下のファイルが変更された場合に自動実行。

- `--check --diff` でdry-runを実行
- 結果をPRにコメントとして投稿
- コメント末尾に `/ansible-apply` の案内を表示

#### ansible-apply.yml (Apply)

2つのトリガーを持つ:

**1. PRコメント `/ansible-apply`**

PRにコメント `/ansible-apply` を投稿すると実行される。

```
PR作成 → dry-run自動実行 → 結果確認 → `/ansible-apply` コメント → 本番適用 → 結果確認 → マージ
```

**2. workflow_dispatch (手動実行)**

GitHub Actions UIから手動で実行。playbook (`site.yml` / `network-update.yml`) を選択可能。

### OpenTofu

#### terraform-check.yml (Plan)

PRで `terraform/` 配下のファイルが変更された場合に自動実行。

- `tofu plan` を実行
- 結果をPRにコメントとして投稿
- コメント末尾に `/tf-apply` の案内を表示

#### terraform-apply.yml (Apply)

2つのトリガーを持つ:

**1. PRコメント `/tf-apply`**

PRにコメント `/tf-apply` を投稿すると実行される（`turtton`ユーザー限定）。

```
PR作成 → plan自動実行 → 結果確認 → `/tf-apply` コメント → apply実行 → 暗号化stateをcommit → 結果確認 → マージ
```

**2. workflow_dispatch (手動実行)**

GitHub Actions UIから手動で実行。mainブランチのterraform構成をapplyする。

apply成功後、暗号化されたstateファイルを自動的にgit commit & pushする。

### Flux CD

#### flux-check.yml (Validate)

PRで `clusters/` 配下のファイルが変更された場合に自動実行。

- `kustomize build` でマニフェストをレンダリング
- `kubeconform` でKubernetesスキーマ検証 (`-strict`, `-ignore-missing-schemas`)
- 結果をPRにコメントとして投稿

シークレット不要。クラスタへのアクセスなしの純粋なローカルバリデーション。
FluxはGitマージ時に自動reconcileするため、applyワークフローは不要。

## Required Secrets

| Secret | 内容 | 用途 |
|---|---|---|
| `TAILSCALE_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID | Ansible / OpenTofu共通 |
| `TAILSCALE_OAUTH_SECRET` | Tailscale OAuth Client Secret | Ansible / OpenTofu共通 |
| `SSH_PRIVATE_KEY` | Proxmoxノード接続用SSH秘密鍵 (ed25519) | Ansible / OpenTofu共通 |
| `ANSIBLE_VAULT_PASSWORD` | ansible-vaultの復号パスワード | Ansible |
| `PROXMOX_VE_ENDPOINT` | Proxmox APIエンドポイント | OpenTofu |
| `PROXMOX_VE_API_TOKEN` | OpenTofu用APIトークン | OpenTofu |
| `TOFU_STATE_PASSPHRASE` | State暗号化パスフレーズ | OpenTofu |
| `TAILSCALE_AUTHKEY` | Talosノード用reusable authkey | OpenTofu |

## Tailscale Setup

GitHub ActionsランナーがProxmoxノードにSSH接続するため、Tailscale経由で接続する。

1. Tailscale管理画面でOAuth clientを作成
   - `tag:ci` のWrite権限を付与
2. ACLで `tag:ci` から各Proxmoxノードへのport 22アクセスを許可
3. Client IDとSecretをGitHub Secretsに登録

## Security

- SSH秘密鍵とvaultパスワードはジョブ終了時に必ず削除 (`Cleanup` ステップ)
- Tailscale接続はephemeralノードとして扱われる
- PRコメントトリガーは `issue_comment` イベントで、PRに紐づくコメントのみ反応
- OpenTofuのstateはPBKDF2+AES-GCMで暗号化されており、パスフレーズなしでは復号不可
- `/tf-apply` コマンドは `turtton` ユーザーのみ実行可能
