# skill-gitops

`skill_manage` によるスキル変更を検知し、30秒デバウンスでまとめて GitOps リポジトリへ自動 PR するプラグイン（`kind: standalone`）。
curator のバックグラウンド統合（1回の統合で50〜100+回の `skill_manage` が走る）も1つのPRに束ねる。

## インストール

```bash
hermes plugins install turtton/hermes-plugins/skill-gitops --enable
```

## 設定（環境変数、すべて任意）

| 変数 | デフォルト | 説明 |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes ホーム（スキル置き場）。Pod 等では `/opt/data` 等に設定 |
| `SKILL_GITOPS_INFRA_REPO` | `/opt/data/infra` | 同期先 GitOps リポジトリのローカルパス |
| `SKILL_GITOPS_WORKTREE` | `/opt/data/infra-sync` | 同期用の常設 git worktree のパス |
| `SKILL_GITOPS_GH_REPO` | `turtton/infra` | PR を開く GitHub リポジトリ |

## 動作

1. `post_tool_call` フックで `skill_manage`（patch / edit / write_file）を検知
2. 30秒デバウンスでバッチ化（curator の一括編集が1PRにまとまる）
3. infra リポジトリ内の固定ブランチ `hermes/skill-update` に force-push → PR 作成/更新（常に1本のPR）
4. `O_EXCL` ロックファイルで同時実行を防止。競合時はスキルを再キュー

## 前提

- `gh` CLI が認証済みであること
- 同期先リポジトリのローカルクローンと、同期用 worktree（`SKILL_GITOPS_WORKTREE`）を作成する権限があること
