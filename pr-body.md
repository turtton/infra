## 変更内容

ULW-loopプラグイン + マルチエージェントKanbanワークフロー設定を追加。

### 追加: ULW-loop Plugin
- plugins/ulw-loop/plugin.yaml
- plugins/ulw-loop/__init__.py
- plugins/ulw-loop/ulw_loop.py

Discord の `/ulw-loop <目標>` スラッシュコマンドからKanbanワークフローを起動。

### 追加: プロファイル設定（init-containerで自動シード）

| プロファイル | model | reasoning_effort | toolsets | 役割 |
|------------|-------|-----------------|----------|------|
| orchestrator | kimi-k2.7-code | (なし) | kanban, memory, gateway | タスク分解/集約/全体レビュー |
| executor | glm-5.2 | max | terminal, file, code_execution, kanban, github, skills | 実装 |
| reviewer | glm-5.2 | max | terminal, file, kanban, github, skills, memory | コードレビュー |

### 変更: メイン設定
- home/config.yaml - plugins.enabled に ulw-loop 追加、kanban.orchestrator_profile 設定
- kustomization.yaml - hermes-ulw-loop-plugin + hermes-profiles ConfigMap追加
- home-statefulset.yaml - init-container + volume追加（プラグインシード、プロファイルシード）

### 動作フロー
1. Discord: /ulw-loop <目標>
2. Kanban triageにタスク作成
3. orchestratorが自動分解（auto_decompose）
4. executorが実装（glm-5.2, resonating max）
5. reviewerがレビュー（glm-5.2, resonating max）
6. 全完了 → orchestrator再起床 → 全体確認
7. Discordに完了通知

### CI
dry-run pass確認済み
