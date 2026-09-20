---
name: plugin-ops
description: "hermes-pluginsの運用: プラグイン修正→直接push→--force再installで更新・動作確認。"
version: 1.0.0
author: turtton
tags: [hermes, plugins, distribution, auto-update]
---

# plugin-ops

`turtton/hermes-plugins`（publicモノレポ）の運用スキル。プラグインの配布・更新・動作確認を担当する。**infra（turtton/infra）の変更は対象外** — そちらは config-ops スキル（PR必須）を参照。

## このスキル自体の管理

- ソース: infra repo `clusters/main/apps/hermes/skills/plugin-ops/SKILL.md`（config-ops / github-ops と同様の仕組み）
- Podへの配布: kustomize ConfigMap（`hermes-plugin-ops-skill`）→ init-skills が `/opt/data/skills/plugin-ops/` にシード
- 変更は **skill-gitops が自動で infra へPR**（`hermes/skill-update` ブランチ）→ CI通過後ユーザーがマージ → 次回Pod再起動で反映
- ローカル作業コピーは `$HERMES_HOME/skills/plugin-ops/` の**フラット配置**（カテゴリサブディレクトリに入れると skill-gitops の同期対象外になる）

## 対象

- リポジトリ: https://github.com/turtton/hermes-plugins (public, main直pushが運用ルール)
- プラグイン: `fluxer/`（platformアダプタ）/ `ulw-loop/`（standalone）/ `skill-gitops/`（standalone）
- 各ディレクトリが1プラグイン。`plugin.yaml` + `__init__.py` 必須、ファイルはフラット構造を維持

## 運用フロー（PR不要・直接push）

1. プラグインを修正（リポジトリの作業コピーで。Podの `$HERMES_HOME/plugins/` は自動更新で上書きされるので直接触らない）
2. ローカル検証: `python3 -m py_compile` + plugin.yaml のYAMLパース
3. **mainへ直接push**（PR/レビュー不要 — config-opsと違い承認ゲートなし）
4. 反映はPod側の自動更新cron（毎日04:00）が行う。すぐ反映したい場合は手動で:
   ```bash
   bash /opt/data/scripts/plugins-update.sh   # 全プラグイン --force 再install
   # または個別に:
   hermes plugins install turtton/hermes-plugins/<name> --force --no-enable
   ```
5. 動作確認: `hermes plugins list` で enabled 状態とバージョン、必要なら `$HERMES_HOME/logs/agent.log` を確認

## 重要: 更新手段は `--force` 再install のみ

- モノレポの**サブディレクトリinstallは `.git` が付かない** → `hermes plugins update` は「not installed from git」エラーで使えない
- したがって自動更新も手動更新も `hermes plugins install turtton/hermes-plugins/<name> --force --no-enable`
- `--no-enable` で `plugins.enabled`（config.yaml側で管理）を変更しない
- 未インストール時も `--force` で入る（PVC消失からの復旧を兼ねる）

## 自動更新cron（watchdog方式）

- スクリプト: `/opt/data/scripts/plugins-update.sh`（cron: `plugins-auto-update`、毎日04:00、no_agent=true）
- 仕組み: `git ls-remote` でhermes-plugins main HEAD取得 → 前回記録SHAと一致なら**無言終了**（空stdout=通知なし）、変化していれば3プラグインを `--force` 再installして結果を通知
- 状態ファイル: `$HERMES_HOME/.cache/plugins-update-sha`
- プラグイン追加時はスクリプトの `PLUGINS` 変数に追記する

## Pitfalls

- **root所有ディレクトリ**: 旧infra initコンテナ（root実行）が作った `/opt/data/plugins/*` がroot所有だと、`--force` のrmtreeが `PermissionError` で失敗する。infra側の `init-fix-plugin-owner` が恒久修正済み（`chown -R 10000:10000` — **busyboxは`hermes`ユーザー名を解決できないため数値UID/GID指定必須**）。手元で直す場合は `mv` で退避→再install（退避dirは後で削除）
- **requires_env**: 環境変数が未設定だとinstall時にプロンプトが出る。非対話環境ではEOFErrorで安全にスキップされる（あとで .env に設定）
- **platformプラグイン（fluxer）**: 再install後、gateway restartまで旧コードがメモリ上で動き続ける。挙動変更の反映には `hermes gateway restart`（またはPod再起動）が必要な場合あり
- **hermes-pluginsの変更は直接push**: config-ops（PR必須・CI必須・subagentレビュー）とはポリシーが異なる。混同しないこと
