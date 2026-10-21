# CI/CD Workflows

Ansible playbookとOpenTofuをGitHub Actions経由で実行するためのワークフロー。

## Workflows

### Ansible

#### ansible-check.yml (Validate)

PRで `ansible/` 配下のファイルが変更された場合に自動実行。

- `ansible-lint`、playbook の構文、対象ホストの範囲を検証
- Secret、Tailscale、SSH接続を使わずに実行

#### ansible-apply.yml (Apply)

GitHub Actions UIから、`main` のレビュー済み revision を手動実行する。
実行者は `turtton` に限定し、playbook (`site.yml` / `network-update.yml` / `vps.yml`)
と `check` / `apply` を選択する。
通常は PR の静的検証 → マージ → `check` → `apply` の順で実行する。
本番接続が必要な出力は公開 Actions ログへ掲載しない。

### OpenTofu

#### terraform-check.yml (Validate)

PRで `terraform/` 配下のファイルが変更された場合に自動実行。

- `tofu fmt` と backend を使わない `tofu validate` を実行
- Secret、Tailscale、SSH接続を使わずに実行

#### terraform-apply.yml (Apply)

GitHub Actions UIから、`main` のレビュー済み revision を `turtton` が手動実行する。
通常は PR の静的検証 → マージ → `plan` の変更件数を確認 → `apply` の順で実行する。
属性値を含む詳細出力は公開 Actions ログへ掲載しない。詳細の確認が必要な変更はローカルで plan する。
`apply` は同じ実行で作成した plan ファイルを適用し、成功後に暗号化された
state ファイルを自動的に git commit & push する。

#### terraform-rotate.yml (Tailscale auth key ローテーション)

毎週金曜 19:00 (JST) に schedule 実行（手動実行も可能）。

Talosノード用のTailscale auth key (`tailscale_tailnet_key` + `time_rotating`, 30日) の期限管理を自動化する:

1. キー系リソースのみ apply (期限超過時のみ新キー発行、それ以外はno-op)
2. `talos_machine_configuration_apply` に差分があるか検出 (`tofu plan -detailed-exitcode`)
3. 差分がある週だけフル apply で全ノードへ新キーを適用し、stateをcommit & push
4. 失敗時はGitHub Issueを自動作成し、成功時に自動close

2段階に分ける理由: Talos provider は plan 時に値が未知のキーを machine config に含めると
`inconsistent final plan` エラーになるため、キー発行とノード適用を別 apply に分ける必要がある
(詳細は terraform/README.md 参照)。

### Flux CD

#### flux-check.yml (Validate)

PRで `clusters/` 配下のファイルが変更された場合に自動実行。

- `kustomize build` でマニフェストをレンダリング
- `kubeconform` でKubernetesスキーマ検証 (`-strict`, `-ignore-missing-schemas`)
- 結果をPRにコメントとして投稿

シークレット不要。クラスタへのアクセスなしの純粋なローカルバリデーション。
FluxはGitマージ時に自動reconcileするため、applyワークフローは不要。

#### fluxer-update.yml / fluxer-update-kind.yml (更新PR)

毎日 08:17 JST と手動実行で Fluxer upstream chart / `v1` image の更新を別々の PR にする。
互換性失敗は Draft PR、自動マージなし。取得・検証 job は read-only、公開 job のみ
`GITHUB_TOKEN` の contents / pull-requests / actions write を利用する。
候補 HEAD に対する `fluxer-chart-check.yml` と `flux-check.yml` を明示 dispatch する。
詳細・手動変更保護・実動作確認は [運用手順](../../docs/fluxer-auto-update.md) を参照。

#### fluxer-chart-check.yml (互換性)

Fluxer の変更 PR と updater からの dispatch で、upstream hash、image lock、
9 release の render と既存 runtime/ownership contract を検証する。
アプリの実動作検証はマージ前に別途行う。

## Required Secrets

| Secret | 内容 | 用途 |
|---|---|---|
| `TAILSCALE_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID | Ansible / OpenTofu共通 |
| `TAILSCALE_OAUTH_SECRET` | Tailscale OAuth Client Secret | Ansible / OpenTofu共通 |
| `TAILSCALE_ACL_OAUTH_CLIENT_ID` | Tailscale ACL管理用OAuth Client ID (terraform provider用) | OpenTofu |
| `TAILSCALE_ACL_OAUTH_SECRET` | Tailscale ACL管理用OAuth Client Secret (terraform provider用) | OpenTofu |
| `TAILSCALE_TAILNET` | Tailnet名 | OpenTofu |
| `SSH_PRIVATE_KEY` | Proxmoxノード接続用SSH秘密鍵 (ed25519) | Ansible / OpenTofu共通 |
| `ANSIBLE_VAULT_PASSWORD` | ansible-vaultの復号パスワード | Ansible |
| `PROXMOX_VE_ENDPOINT` | Proxmox APIエンドポイント | OpenTofu |
| `PROXMOX_VE_API_TOKEN` | OpenTofu用APIトークン | OpenTofu |
| `TOFU_STATE_PASSPHRASE` | State暗号化パスフレーズ | OpenTofu |
| `CLOUDFLARE_API_TOKEN` | Cloudflare APIトークン (Tunnel/Access/DNS管理) | OpenTofu |

注: `tailscale_tailnet_key` リソースがキーを発行するため、ACL管理用OAuth Clientには **Auth keys (write)** スコープが必要。

## Tailscale Setup

GitHub ActionsランナーがProxmoxノードにSSH接続するため、Tailscale経由で接続する。

1. Tailscale管理画面でOAuth clientを作成
   - `tag:ci` のWrite権限を付与
2. ACLで `tag:ci` から各Proxmoxノードへのport 22アクセスを許可
3. Client IDとSecretをGitHub Secretsに登録

## Security

- SSH秘密鍵とvaultパスワードはジョブ終了時に必ず削除 (`Cleanup` ステップ)
- Tailscale接続はephemeralノードとして扱われる
- PR チェックは Secret を持たず、本番接続もしない
- 本番の plan/check/apply は `main` の固定 revision から手動実行する
- OpenTofuのstateはPBKDF2+AES-GCMで暗号化されており、パスフレーズなしでは復号不可
