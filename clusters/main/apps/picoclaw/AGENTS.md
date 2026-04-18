# PicoClaw 構成

## 関連リポジトリ

| リポジトリ | 説明 |
|---|---|
| [turtton/picoclaw-copilot](https://github.com/turtton/picoclaw-copilot) | PicoClaw のフォーク。カスタムイメージ `ghcr.io/turtton/picoclaw-copilot` をビルド。copilot-cli サブコマンドも含む |
| [turtton/picoclaw-helm](https://github.com/turtton/picoclaw-helm) | PicoClaw 用カスタム Helm チャート。agentFiles・copilotCli・launcher・persistence 等をサポート |
| [turtton/opencode](https://github.com/turtton/opencode) | opencode のフォーク。サイドカーとして `ghcr.io/turtton/opencode` を使用。`serve` モードで HTTP API を提供 |

## インスタンス構成

### picoclaw-lepi

- **モデル**: `claude-sonnet-4.6`（`localhost:4321` copilot-cli プロキシ経由）
- **エージェント**: main, config-ops, github-ops
- **agentFiles**: config-ops → `picoclaw-config-ops-skill` ConfigMap, github-ops → `picoclaw-github-ops-skill` ConfigMap
- **copilotCli**: 有効（GitHub Copilot API プロキシ、ポート 4321）
- **opencode サイドカー**: 有効（ポート 4567）
- **launcher**: 有効（ポート 18800）
- **バインディング**: github-ops → Discord ギルド `719207321854541874`

### picoclaw-home

- **モデル**: `kimi-k2.5`（OpenRouter 経由）
- **エージェント**: main, config-ops
- **agentFiles**: config-ops → `picoclaw-config-ops-skill` ConfigMap
- **copilotCli**: なし
- **opencode サイドカー**: 有効（ポート 4567）
- **launcher**: 有効（ポート 18800）
- **バインディング**: なし

## Helm チャート機能（v0.1.12）

### agentFiles

エージェントごとのワークスペースディレクトリに `AGENT.md` 等のファイルをマウントする。チャートが自動で `config.json` の `workspace` パスを設定する。

```yaml
agentFiles:
  <agent-id>:
    configMapName: <ConfigMap名>
    items:
      - key: AGENT.md
        path: AGENT.md
```

### copilotCli

GitHub Copilot API をローカルプロキシする sidecar コンテナ。`model_list` で `api_base: "localhost:4321"` を指定して利用。

### opencode サイドカー（postRenderers）

Helm チャートの機能ではなく、Flux `postRenderers` で Deployment にパッチ適用する:

- **initContainer** `fetch-opencode-config`: dotnix リポジトリから opencode 設定をクローン → emptyDir にコピー
- **sidecar** `opencode`: `ghcr.io/turtton/opencode:latest`、`0.0.0.0:4567` で HTTP サーバー起動
- **環境変数**: `COPILOT_GITHUB_TOKEN`（Secret から取得）、`HOME=/root`
- picoclaw 側は `PICOCLAW_TOOLS_OPENCODE_TASK_ENABLED=true` + `PICOCLAW_TOOLS_OPENCODE_TASK_SERVER_URL=http://localhost:4567` で接続

### securitySecret

`.security.yml` をマウントしてモデル API キー・Discord トークン等を管理。`config.json` にマージされ値を上書き。

### persistence

Longhorn StorageClass で 5Gi PVC をマウント。エージェントのメモリ（JSONL）やワークスペースデータを永続化。

## ファイル構成

```
clusters/main/apps/picoclaw/
├── AGENTS.md                      # このファイル
├── config-ops-skill.yaml          # config-ops エージェント用 AGENT.md ConfigMap
├── github-ops-skill.yaml          # github-ops エージェント用 AGENT.md ConfigMap
├── helmrelease-home.yaml          # home インスタンス HelmRelease
├── helmrelease-lepi.yaml          # lepi インスタンス HelmRelease
├── home-secrets.sops.yaml         # home 用 Secret（SOPS 暗号化）
├── home-security.sops.yaml        # home 用 security.yml Secret
├── ingress-home.yaml              # home Tailscale Ingress
├── ingress-lepi.yaml              # lepi Tailscale Ingress
├── kustomization.yaml             # Kustomize エントリポイント
├── lepi-copilot-secrets.sops.yaml # lepi copilot-cli 用 Secret
├── lepi-secrets.sops.yaml         # lepi 用 Secret
├── lepi-security.sops.yaml        # lepi 用 security.yml Secret
└── namespace.yaml                 # picoclaw namespace
```
