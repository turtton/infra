# Lepinoid のテナント内 ARC と容量試験

ARC controller・listener・runner・使い捨てPVCはすべて `lepinoid` namespace に置き、`Lepinoid/infra` で管理する。
このリポジトリでは共通CRD、テナントの権限、クォータ、適用順序を管理する。

## 管理分担と起動順序

| 管理元 | 対象 |
|---|---|
| turtton/infra | 公式ARC CRD、`lepinoid-helm` SA、namespace内のRBAC、ResourceQuota |
| Lepinoid/infra | ARC controller、2 scale sets、GitHub App Secret、Cilium policy、runner/PVC template |
| Lepinoid/lepinoid | CIのrunner選択、daemonless画像検査・転送、試験workflow |

`infra-controllers` が公式OCI chart 0.15.1のCRDディレクトリを同期する。
chartはdigestで固定し、CRDは `prune: false`・`deletionPolicy: Orphan` とする。
CRD用sourceの `operation: extract` と、テナントのHelm chart用sourceの `operation: copy` は用途が異なる。

テナントは `lepinoid-tenant-setup → lepinoid-arc-rbac → lepinoid-workloads` の順に適用し、
workloadsはCRD同期にも依存する。HelmReleaseは `serviceAccountName: lepinoid-helm` を明示する。
controllerは `watchSingleNamespace: lepinoid`、両chartのCRD管理は `Skip` に設定し、
ClusterRoleや別namespaceの操作権限をテナントへ渡さない。

## runner とクォータ

| scale set | CPU request / limit | memory request / limit | 最大数 | 待機数 |
|---|---:|---:|---:|---:|
| lepi-ci-small | 2 / 2 | 4Gi / 4Gi | 2 | 0 |
| lepi-ci-medium | 4 / 4 | 8Gi / 8Gi | 2 | 0 |

runner 4枠で12 CPU / 24Gi。controllerとlistener 2台は別途request合計200m CPU / 512Mi、
limit合計900m CPU / 1Giを見込む。各runnerは40Giの `longhorn-single` generic ephemeral PVCを使う。
workspace、Gradle/JDK cache、Nix store、一時ファイルをPVCへ置き、Pod削除時に回収する。

既存テナントの利用可能な枠を維持し、CIと常駐分を追加してクォータを拡張する。

| リソース | 導入前 | 導入後 |
|---|---:|---:|
| requests.cpu | 12 | 25 |
| requests.memory | 24Gi | 50Gi |
| limits.cpu | 20 | 34 |
| limits.memory | 40Gi | 66Gi |
| PVC数 | 20 | 24 |

クォータはnamespace全体の割当上限であり、この量の実メモリを事前消費しない。
配置判定は実在Podのrequests、実行時制限はlimitsで行う。
requests / limitsが必須でも両者を同じ値にする義務はない。今回は容量と性能を確認する基準として等しく設定する。

runnerの配置先は `toliworker-1/2/3`。同じ物理ホスト `toliunit` の36物理コア・72スレッドを共有し、
各VMは20Giである。OSディスクの空き約12〜15Giと、別Longhornディスクの空きは区別する。

## 認証・通信・画像処理

GitHub App `lepinoid-vuddy` を利用し、組織runner group `lepinoid-ci` は `Lepinoid/lepinoid` だけに公開する。
Appの秘密鍵は `Lepinoid/infra` のSOPS Secretで管理し、runnerへは渡さない。
runnerのSA token mountを無効化し、Ciliumの明示denyで同namespaceアプリ・API・ホスト・LANへの通信を遮断する。
controller / listenerにはKubernetes API、DNS、公開IPv4への通信を許可する。

画像はNixのDocker archiveをSkopeoで検査・転送し、privilegedなDocker daemonは使わない。
Skopeoは大きな一時ファイルに `TMPDIR` だけではなく、`--tmpdir "$RUNNER_TEMP/skopeo"` を明示する。

## 2026-11-04 の容量試験

同一ソース、空のGradle cache、memory 8Gi、Gradle workers=2でruntimeの全チェックと配布物生成を比較した。

| CPU | Gradle実行時間 | XMLスイート | failures / errors |
|---|---:|---:|---:|
| 2 vCPU | 33分52秒 | 524 | 0 / 0 |
| 4 vCPU | 28分55秒 | 524 | 0 / 0 |

4 vCPUは約15%短縮した。成功したテストレポートではTCP/UI関連の待機を含むテストが長く、
CPU増加だけで全体が比例して短縮する処理ではない。テストの分離やfork設定は変更していない。
比較時の最大working setは約5.8Giで、runtimeには8Gi枠を使う。

small 2枠のassets converter、resource pack、world-import compileはそれぞれ約6分50秒・6分45秒で成功した。
小さい方の最大working setは約3.2〜3.5Giだった。

Skopeoの独立PoCでは、非root・capabilityなし・read-only rootfs・Dockerなしの1 CPU / 1Giコンテナで、
runtimeとvelocityのarchiveをinspectし、全レイヤーをlocal directoryへcopyできた。
両configが一致し、合計43秒、一時領域最大約2.15Giだった。GHCRへの本番pushはこのPoCの対象外。

実行記録: https://github.com/Lepinoid/lepinoid/actions/runs/41774649754
small 2台・medium 2台の実ジョブ4並列は全ジョブ成功し、157秒の同時稼働を確認した。
ARC自動起動の別試験では115秒の4並列、通信分離、Pod/PVCの自動回収がすべて成功した。
終了後はrunner 0台、既存8 PVCに戻った。各試験の実行時間内ではNode Pressure・eviction・既存Podの再起動増加は検出されなかった。
ARC試験終了の約21分後（06:25:40 UTC）にlivesync-bridgeが同期処理の未実装メソッド例外で再起動した。
終了理由はError / exit code 1でOOMKilledではなく、CI負荷との関連を示す証拠は確認できなかった。
常駐controllerとlistener 2台の実使用memoryは合計約36Miだった。

ARC試験: https://github.com/Lepinoid/lepinoid/actions/runs/41782904976
詳細な運用手順と測定結果は `Lepinoid/infra` の `arc/README.md` に記載する。
