---
name: config-ops
description: turtton/infra GitOpsリポジトリのKubernetesマニフェストやHermes Agentの設定（config.yaml, plugins, skills, ツール類）を変更する際のワークフロー。infraリポジトリ、Flux、k8sマニフェスト、Hermesの設定変更に関する作業を始める前に必ずロードすること。
version: 1.5.0
---

# config-ops

あなたはインフラ設定管理エージェントです。

## 役割

KubernetesマニフェストやHermes Agentの設定変更を安全にテストし、GitOpsリポジトリにPRを作成して永続化する。

## ⚠️ 最重要ルール: 決してPRを自分でマージしない

PRを作成したら**ユーザー（turtton）に報告してマージを待つこと**。
自分で `gh pr merge` を実行してはならない。
また `gh pr review --approve` も実行しないこと（approveはユーザー判断）。

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

# リポジトリをワークスペース内にクローン（既に /opt/data/infra にあればそれを使う）
git clone https://github.com/turtton/infra.git ./infra-pr
cd ./infra-pr

# ブランチ作成
git checkout -b hermes/config-update-$(date +%Y%m%d-%H%M%S)

# 対象ファイルを編集
# - home インスタンス: clusters/main/apps/hermes/home-*.yaml
# - lepi インスタンス: clusters/main/apps/hermes/lepi-*.yaml
# - Hermes config: clusters/main/apps/hermes/home/config.yaml, lepi/config.yaml
# - Hermes plugins: clusters/main/apps/hermes/plugins/*/
# - Hermes skills: clusters/main/apps/hermes/skills/*/SKILL.md
# - ConfigMap source: clusters/main/apps/hermes/kustomization.yaml

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

### 3. CI確認 → レビュー＆修正ループ

PR作成後、以下のループを実行する：

```bash
# CIの完了を確認（失敗ステータスがなければ完了）
gh pr checks <PR番号> --watch
```

**CI失敗時のルール:**
- CI（dry-run/plan）が**失敗**した場合、エラーログを確認して**自分で修正**する
- 修正後、再度 `git push` してCIを再実行
- CIが通るまで修正を繰り返す
- どうしても直せない場合は、ユーザーに状況を報告する

**subagentレビューループ:**
- delegate_taskでsubagentにPRの内容をレビューさせる
- subagentからOK（マージ可能）の判定が出るまで、修正→再レビューを繰り返す
- レビューのコンテキストにはPR番号、変更内容、修正意図を必ず含める

### 4. ユーザーに報告

すべてのチェック（CI通過、subagentレビューOK）が完了したら、ユーザーに報告する。
**自分でマージしないこと。**

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

## 重要: 実行環境の認識

このHermes Agentは**クラスタ内Pod**（`hermes-home-0`）で動作している。外部サーバーではない。
k8s APIにアクセスする前に、以下のチェックを最初に行うこと：

```bash
# 1. ホスト名でPod内か確認
hostname                    # → hermes-home-0 ならPod内

# 2. Service Accountトークンの有無
ls /var/run/secrets/kubernetes.io/serviceaccount/
# → token, ca.crt, namespace があればPod内

# 3. kubeconfigの存在確認
ls ~/.kube/config          # → なければ in-cluster セットアップが必要

# 4. K8s API Serverへの接続確認
kubectl get pods -n atm10  # → タイムアウトする場合は connectivity 参照
```

### In-Cluster kubectl セットアップ

`~/.kube/config` が存在しない場合、Service Accountトークンを使って設定する：

```bash
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
CA_CRT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
APISERVER="https://10.96.0.1:443"

kubectl config set-cluster in-cluster \
  --server="$APISERVER" \
  --certificate-authority="$CA_CRT" \
  --embed-certs=true
kubectl config set-credentials hermes-sa --token="$TOKEN"
kubectl config set-context hermes \
  --cluster=in-cluster --user=hermes-sa --namespace=atm10
kubectl config use-context hermes
```

トークンの詳細（API Server実アドレス等）は以下で確認：
```bash
cat /var/run/secrets/kubernetes.io/serviceaccount/token \
  | cut -d. -f2 | base64 -d | python3 -m json.tool
```
→ `iss` フィールドがAPI Serverの実IPとポート（例: `https://192.168.10.110:6443`）

### 代替: OpenTofu経由のkubeconfig取得（クラスタ外/ブートストラップ時）

```bash
cd terraform/
tofu output -raw kubeconfig > ~/.kube/config
```

## 監視・調査: Discord GitHub通知の参照

`#infra` DiscordチャンネルにはGitHub Botからのembed通知が飛ぶが、`discord`ツールの`fetch_messages`はembed内容を返さない（`content: ""`）。詳細な手順は `references/discord-github-notification-investigation.md` を参照。

簡易フロー:
1. `fetch_messages`で該当メッセージのタイムスタンプを取得
2. GitHub Events API（`api.github.com/repos/turtton/infra/events`）で同時刻のイベントを特定
3. PR/Issue/Commitの詳細をGET

## CIトラブル: yq v3/v4互換性問題

`tests/scripts/atm10-observability-test.sh` は yq v3の `-s`（slurp）構文で書かれているが、
CIは yq v4.44.5 をインストールしており、v4の `-s` は `--split-exp`（ファイル分割）である。

**症状:** 全yqチェックが"cannot index array with 'kind'"で失敗、validation出力が空

**修正パターンの詳細:** `references/ci-yq-v3-v4-pitfall.md`
**修正例:** PR #95 (fix/yq-v4-compat)

## トラブルシューティング: K8s API 接続不可

Pod内にいるのに `kubectl` がタイムアウトする場合：

### 症状
- `10.96.0.1:443`（Kubernetes Service）に接続できない
- ただし `10.96.0.10:53`（CoreDNS）は到達可能
- 同一セグメントのノードIPにpingすら通らない
- `tunl0` インターフェースが存在（CiliumのIPIPトンネル — TalosではCalicoと同様のtunl0が存在する）

### 原因
- **Cilium/NetworkPolicy** でworkload PodからControl Planeへのegressが制限されている
- **kube-apiserver** がダウンしている（CoreDNSは生きているので部分停止）
- **kube-proxy replacement (Cilium eBPF)** が正常動作していない（API ServerエンドポイントがCiliumのService Mapに未登録）

### 診断手順（詳細は `references/k8s-api-connectivity-debug.md`）

#### 1. 環境確認
```bash
hostname                    # Pod名: hermes-home-0
ls /var/run/secrets/kubernetes.io/serviceaccount/  # token, ca.crt
cat /var/run/secrets/kubernetes.io/serviceaccount/token | cut -d. -f2 | base64 -d | python3 -m json.tool
```

#### 2. Cilium設定の確認（infraリポジトリ）
```bash
grep -A5 k8sService clusters/main/infrastructure/controllers/cilium/helmrelease.yaml
# → k8sServiceHost: localhost, k8sServicePort: 7445 (KubePrism)
```

#### 3. ネットワーク切り分け
```bash
# 他のClusterIP Serviceが正常か確認
timeout 3 bash -c 'echo > /dev/tcp/10.96.0.10/53 && echo "CoreDNS OK"'
timeout 3 bash -c 'echo > /dev/tcp/$HERMES_HOME_SERVICE_HOST/9119 && echo "hermes svc OK"'
# → OKならCiliumのService LBは動作しているが、kubernetes Serviceのみ問題

# KubePrismエンドポイント
timeout 5 curl -sk --connect-timeout 5 https://localhost:7445/api --header "Authorization: Bearer $TOKEN"
# → Podからは到達不能（hostNetwork限定）
```

#### 4. NetworkPolicy確認
```yaml
# clusters/main/apps/hermes/home-network-policy.yaml
# allow-hermes-home-egress は port 443全宛先へのegressを許可している
# → NetworkPolicyは原因ではない可能性が高い
```

#### 5. 切り分けマトリクス
| チェック | 正常 | 異常パターンA | 異常パターンB |
|---|---|---|---|
| CoreDNS (UDP 53) | ✅ | ✅ | ✅ |
| hermes-home svc | ✅ | ✅ | ❌ |
| k8s Service (443) | ✅ | ❌ | ❌ |
| → 推定原因 | - | **API Serverダウン or Cilium eBPF不調** | **Cilium LB全体不調** |

### 報告テンプレート（ユーザー/管理者向け）
> Pod内からk8s API Serverに接続できない状況です。
> - 環境: hermes-home-0 (Cilium + KubePrism + kubeProxyReplacement)
> - CoreDNS (10.96.0.10:53) は到達可能 ✅
> - hermes-home service (10.109.61.199:9119) は到達可能 ✅
> - k8s API (10.96.0.1:443) はtimeout ❌
> - KubePrism (localhost:7445) はPodから到達不能（hostNetwork限定）
> - NetworkPolicy (allow-hermes-home-egress) はport 443全宛先許可
> - Cilium eBPF Service MapにAPI Serverエンドポイントが未登録の可能性
> - 対応: Cilium agent再起動 または talosctl health 確認をお願いします

## Git Worktree Pattern for Multi-Repo Branch Management

When making changes across multiple external repos (not this infra repo), use the **bare clone + git worktree** pattern documented in `references/worktree-pattern.md`. This avoids cloning fresh per branch and keeps the origin repos clean. Works well for batch standardizations (e.g., adding `workflow_dispatch` + `checks` across all flake repos).

## 注意事項

- **Flux管理**: GitOpsの定義がPod再起動時に適用される。Pod内の設定変更は一時的
- **Skills永続化**: SkillsディレクトリはPVC上にあり、Pod再起動後も維持される（init containerでConfigMapから初期シード）
- **SOPS暗号化ファイル**: `*.sops.yaml` は直接編集不可。シークレット変更が必要な場合はオーナーに報告
- **CIチェック**: PRではdry-runが自動実行される。CIが失敗したら修正して再プッシュすること
- **subagentレビュー**: 必ずsubagentにレビューさせ、OKが出るまで修正を繰り返すこと
- このエージェントは設定変更のみ担当。アプリケーションロジックの変更は行わない
- **絶対にPRを自分でマージしないこと**
- **`gh pr review --approve` も実行しないこと**

## 認証

- `gh` CLIは起動時に`gh auth login`で認証済み（Pod起動時のみ有効）
- PR作成は `gh pr create` を使用（GH_TOKEN/GITHUB_TOKEN環境変数は使用不可）
