# CI/CD Workflows

Ansible playbookをGitHub Actions経由で実行するためのワークフロー。

## Workflows

### ansible-check.yml (Dry-Run)

PRで `ansible/` 配下のファイルが変更された場合に自動実行。

- `--check --diff` でdry-runを実行
- 結果をPRにコメントとして投稿
- コメント末尾に `/apply` の案内を表示

### ansible-apply.yml (Apply)

2つのトリガーを持つ:

**1. PRコメント `/apply`**

PRにコメント `/apply` を投稿すると実行される。

```
PR作成 → dry-run自動実行 → 結果確認 → `/apply` コメント → 本番適用 → 結果確認 → マージ
```

**2. workflow_dispatch (手動実行)**

GitHub Actions UIから手動で実行。playbook (`site.yml` / `network-update.yml`) を選択可能。

## Required Secrets

| Secret | 内容 |
|---|---|
| `TAILSCALE_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID |
| `TAILSCALE_OAUTH_SECRET` | Tailscale OAuth Client Secret |
| `SSH_PRIVATE_KEY` | Proxmoxノード接続用SSH秘密鍵 (ed25519) |
| `ANSIBLE_VAULT_PASSWORD` | ansible-vaultの復号パスワード |

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
