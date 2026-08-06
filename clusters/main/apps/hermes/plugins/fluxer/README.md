# fluxer

Fluxer インスタントメッセージングプラットフォームの Hermes Agent アダプタ（`kind: platform`）。
セルフホスト/クラウド両対応。`/.well-known/fluxer` からの自動エンドポイント検出で、インスタンスURLを指定するだけで動作する。

## インストール

```bash
hermes plugins install turtton/hermes-plugins/fluxer --enable
```

`requires_env` に宣言された必須環境変数は、インストール時に未設定分だけ対話で聞かれる。
あとから設定する場合は `$HERMES_HOME/.env` に追記する。

## 環境変数

必須:

| 変数 | 説明 |
|---|---|
| `FLUXER_TOKEN` | Fluxer OAuth2 アプリケーションのボットトークン（https://fluxer.app/developers/applications） |
| `FLUXER_CHANNEL` | 参加するチャンネル（例: `#general`） |
| `FLUXER_INSTANCE_URL` | インスタンスのベースURL（例: `https://fluxer.example.com`）。エンドポイントは `/.well-known/fluxer` から自動検出 |

任意:

| 変数 | 説明 |
|---|---|
| `FLUXER_HOME_CHANNEL` | cron 配信のデフォルトチャンネル |
| `FLUXER_ALLOWED_USERS` | ボットと会話を許可するユーザーID（カンマ区切り） |
| `FLUXER_ALLOW_ALL_USERS` | `true` で全ユーザーを許可 |
| `FLUXER_ALLOW_ADMIN_FROM` | スラッシュコマンド全権限を持つ管理者ユーザーID（カンマ区切り） |
| `FLUXER_USER_ALLOWED_COMMANDS` | 一般ユーザーが実行できるスラッシュコマンド（カンマ区切り） |
| `FLUXER_HISTORY_BACKFILL` | 起動時バックフィルの有効/無効（デフォルト: `true`） |
| `FLUXER_HISTORY_BACKFILL_LIMIT` | バックフィルで遡る最大メッセージ数（デフォルト: 50） |

## 有効化

プラグイン自体の有効化に加え、gateway 設定でチャンネルの有効化が必要:

```yaml
# config.yaml
plugins:
  enabled:
    - fluxer
gateway:
  platforms:
    fluxer:
      enabled: true
```
