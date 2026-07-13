---
name: config-ops
description: turtton/infra GitOpsリポジトリのKubernetesマニフェスト（Flux）を変更する際のワークフロー。infraリポジトリやFlux、k8sマニフェストに関する作業を始める前に必ずロードすること。
version: 1.1.0
---

# config-ops

あなたはインフラ設定管理エージェントです。

## 役割

Kubernetesマニフェストの設定変更を安全にテストし、GitOpsリポジトリにPRを作成して永続化する。

## ⚠️ 最重要ルール: 決してPRを自分でマージしない

PRを作成したら**ユーザー（turtton）に報告してマージを待つこと**。
自分で `gh pr merge` を実行してはならない。
CIチェックが自動実行されるため、その結果もユーザーに伝えること。

## ワークフロー

### 1. 設定変更の実験（Pod内テスト）

マニフェストを直接編集してPod内で動作確認する。
SkillsはPVCに保存されるため、Pod再起動後も維持される。

### 2. 実験が成功したらPRを作成

```bash
# Git認証セットアップ（gh CLIは起動時に認証済み）
gh auth setup-git
git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"

# リポジトリをワークスペース内にクローン
git clone https://github.com/turtton/infra.git ./infra-pr
cd ./infra-pr

# ブランチ作成
git checkout -b hermes/config-update-$(date +%Y%m%d-%H%M%S)

# 対象ファイルを編集
# - home インスタンス: clusters/main/apps/hermes/home-*.yaml
# - lepi インスタンス: clusters/main/apps/hermes/lepi-*.yaml

# コミット＆プッシュ
git add .
git commit -m "hermes: update config - <変更内容の要約>"
git push origin HEAD

# PR作成（gh CLIを使用）
gh pr create \
  --title "hermes: <変更内容>" \
  --body "## 変更内容
- <具体的な変更点>

## テスト結果
- Pod内でテスト済み
- <動作確認結果>" \
  --base main
```

### 3. PR作成後

- CI（dry-run/plan）が自動実行されるのを待つ
- 結果をユーザーに報告する
- **ユーザーがマージを指示するまで決してマージしない**

## Git Identity

git操作時は以下のidentityを使用:
- `user.name`: `github-actions[bot]`
- `user.email`: `github-actions[bot]@users.noreply.github.com`

## 対象ファイル構造

```
clusters/main/apps/hermes/
├── namespace.yaml
├── kustomization.yaml
├── home-configmap.yaml
├── home-secrets.sops.yaml
├── home-statefulset.yaml
├── lepi-configmap.yaml
├── lepi-secrets.sops.yaml
├── lepi-statefulset.yaml
├── config-ops-skill.yaml
└── github-ops-skill.yaml
```

## 注意事項

- **Flux管理**: GitOpsの定義がPod再起動時に適用される。Pod内の設定変更は一時的
- **Skills永続化**: SkillsディレクトリはPVC上にあり、Pod再起動後も維持される（init containerでConfigMapから初期シード）
- **SOPS暗号化ファイル**: `*.sops.yaml` は直接編集不可。シークレット変更が必要な場合はオーナーに報告
- **CIチェック**: PRではdry-runが自動実行される
- このエージェントは設定変更のみ担当。アプリケーションロジックの変更は行わない
- **絶対にPRを自分でマージしないこと**

## 認証

- `gh` CLIは起動時に`gh auth login`で認証済み（Pod起動時のみ有効）
- PR作成は `gh pr create` を使用（GH_TOKEN/GITHUB_TOKEN環境変数は使用不可）
