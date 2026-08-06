# ulw-loop

ULW-loop（Explore → Plan → Plan-Review → Execute → Verify → Review → Fix）構造化ワークフローの Hermes Agent プラグイン（`kind: standalone`）。

## 機能

- 耐久性のある状態管理（`goals.json` + `ledger.jsonl`、`$HERMES_HOME/ulw-loop/<session_id>/`）
- トークン方式のフェーズ遷移（`<promise>DONE</promise>` プロトコル）
- **Plan review ゲート**: プランが承認されるまで execute に進まない
- Quality Gate チェック（criteria coverage + adversarial coverage）
- ブロッカー自動分類と3回連続時の人間へのエスカレーション
- Steering（実行中のゴール追加/分割/文言修正）
- 自動再開（2-strike no-progress cap）
- Kanban 連携によるマルチエージェントオーケストレーション

## インストール

```bash
hermes plugins install turtton/hermes-plugins/ulw-loop --enable
```

## 使い方

- スラッシュコマンド: `/ulw-loop <goal>` / `/ulw-steer <action> <session-id> <params>`
- エージェントCLI: `hermes ulw-loop <goal> --context <summary> --platform discord --chat-id <id> --thread-id <id>`

## マルチプロファイル運用（任意）

プランナー分離（ゴール分解を専用プロファイルに担当させる）をする場合:

- kanban デコンポーザは各プロファイル `profile.yaml` の `description` を見てロースターを作る。**全プロファイルに description を付けること**（未記述だと `undescribed` 扱いで振り分けが偏る）
- 構成例は hermes-plugin-development スキルの `references/ulw-loop-profile-roles.md` を参照
