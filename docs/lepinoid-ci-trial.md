# Lepinoid GitHub Actions runner 試験

Lepinoid の通常 CI を対象に、ホームラボ上の ephemeral runner の互換性、必要なサイズ、4ジョブ並列時の影響を確認する。
試験用マニフェストは `clusters/main/apps/ci-runners-trial/` に独立して配置し、通常の apps Kustomization には追加しない。
本番 CI の runner は試験結果を確認してから切り替える。

## 試験構成

| プロファイル | ジョブ全体の CPU | ジョブ全体のメモリ | 最大数 |
|---|---:|---:|---:|
| small | 2 | 4Gi | 2 |
| medium | 4 | 8Gi | 2 |

合計は最大4ジョブ、12 CPU、24Gi。Docker daemon が必要なジョブでは runner と DinD sidecar の合計をこの枠内に収める。
2 vCPU の比較試験では medium のメモリを8Giに保ち、CPUだけ2に下げる。比較用 runner も medium の2枠に数える。

配置先は `toliworker-1/2/3` に限定する。現在は同一の物理ホスト `toliunit`（Xeon Gold 6140×2、36物理コア・72スレッド）上で動作するため、別ノードへの分散で物理障害に対する冗長性は増えない。

各 runner は専用の使い捨て40Gi PVCを持ち、workspace、Gradle/tool cache、Nix store、Dockerのデータを保存する。
再生成できる試験データのため `longhorn-single` を使用する。OS領域の空きとPVCの空きは別に監視する。

## 認証と分離

ローカルの `gh` で対象リポジトリに対する `generate-jitconfig` を呼び、1ジョブだけ受け付ける設定を Kubernetes Secret へ渡す。
GitHub の長期トークンをクラスタへコピーしない。Secret、PVC、runner は試験 Job のライフサイクルに合わせて破棄する。
再試行には新しいJIT設定を発行する。

runner には Kubernetes の操作権限を与えず、ServiceAccount token もマウントしない。
NetworkPolicy でクラスタ・LAN・Tailscaleへの通信を遮断し、DNSとインターネットへの通信を許可する。
Docker が必要な場合だけ Pod 内の専用 daemon を使い、ホストの Docker socket・hostPath・host network は使わない。
DinD は privileged container を必要とするため、runnerは信頼済みの同一リポジトリのPRだけを対象とし、試験起動ラベルで明示的に実行する。

## 検証手順

ローカルでは `uv`、`gh`、`kubectl` を使う。`gh` は対象リポジトリのrunner管理権限、`kubectl` は試験namespaceのJob/Secret/PVC管理権限が必要。
以下はinfraのworktreeから実行する。

```sh
kubectl apply -k clusters/main/apps/ci-runners-trial
uv run python scripts/ci-runners-trial.py create \
  --size medium --cpu-override 2 --label lepi-ci-baseline --name runtime-baseline
uv run python scripts/ci-runners-trial.py create --size medium --name runtime-medium
uv run python scripts/ci-runners-trial.py status
```

対象のLepinoid PRに `ci-self-hosted-trial` ラベルを付けて比較を開始する。
Jobの完了とGitHub側の判定を確認した後、次の4枠を起動する。

```sh
uv run python scripts/ci-runners-trial.py cleanup
uv run python scripts/ci-runners-trial.py create --size small --name assets-a
uv run python scripts/ci-runners-trial.py create --size small --name assets-b
uv run python scripts/ci-runners-trial.py create --size medium --name runtime
uv run python scripts/ci-runners-trial.py create \
  --size medium --docker --label lepi-ci-docker --name docker
```

`create` の出力にあるGitHub runner IDを記録する。Jobは終了300秒後にTTLで削除されるため、失敗・未接続runnerのGitHub登録はTTL後に残っていないかIDで確認する。
`cleanup` は完了Jobだけを回収する。実行中の試験Jobを止める場合は `cleanup --job <slot> --cancel` と対象を指定する。

1. standalone Kustomization と生成 Job を server-side dry-run で確認し、試験namespaceを適用する。
2. 2 CPU/8Gi、4 CPU/8Giのrunnerをそれぞれ1台起動する。同じソースrevision、空のcache、Gradle workers=2で Runtime CI の全チェックと配布物生成を比較する。
3. 比較が成功した後、small 2台とmedium 2台を起動する。assets/world-importを2ジョブ、runtimeを1ジョブ、Docker/Nixを1ジョブ同時に実行する。runtimeはworkers=4も確認する。
4. GitHub Actionsの結果、各コンテナのCPU・メモリ、PVC・OSディスク使用量、既存ノードとサービスの状態を記録する。
5. 終了後に試験 Job、Secret、PVC、GitHub runner登録を回収し、既存サービスの状態を確認する。

試験workflowではGHCRへの公開と本番infra更新を実行しない。通常のGitHub-hosted CIは維持する。
通常のruntimeチェックはPostgreSQL/NATS統合テストと実クライアントハーネスを含まないため、これらの成功とはみなさない。

## 採用基準

- 対象チェックがすべて成功し、OOM・eviction・DiskPressureが発生しない。
- 4並列時も各ノードの `MemAvailable` に概ね4Gi以上を残す。
- CPU throttling、OSディスクの消費、既存サービスのエラー・応答時間に継続的な悪化がない。
- 4 CPUで実行時間が改善する場合だけmediumを4 CPUで維持する。差が小さければ2 CPU/8Giへ縮小する。

常設化では専用GitHub AppとARCのscale setを使い、待機数0・small最大2・medium最大2を起点とする。
試験用のローカルJIT発行スクリプトは無人の常設autoscalerではない。

## 試験結果

実行後に対象revision、Actions URL、測定値、判定を記録する。
