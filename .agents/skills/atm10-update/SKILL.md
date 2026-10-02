---
name: atm10-update
description: 'ATM10（all-the-mods-10）Minecraftサーバーのmodpack更新手順。定期バックアップの確認・停止後スナップショット・復帰と起動確認・手動MOD確認までを行う。'
---

## ATM10 modpack 更新手順

このリポジトリの `clusters/main/apps/atm10/` で管理される ATM10 Minecraft サーバー（Kubernetes + itzg/minecraft-server AUTO_CURSEFORGE + Longhorn PV）のmodpack更新を安全に行うための手順。

前提：Deploymentは `TYPE: AUTO_CURSEFORGE` + `CF_SLUG: all-the-mods-10` でバージョン未固定運用（`CF_FILE_ID` や `CF_FILENAME_MATCHER` は未設定。再起動時にCurseForge上で選択対象となる新しいmodpackファイルがあれば自動更新される）。世界データは `atm10-data-v2`、手動MODは `atm10-downloads-v2` PVC（Longhorn、`longhorn-gameserver`）を使用する。対象PVCは固定名を前提にせず、Deploymentのvolume設定から取得する。イメージは `itzg/minecraft-server:latest` + `imagePullPolicy: Always` なので、rolloutでmodpackとコンテナイメージが同時に更新される点に注意。

---

以下のコマンド例はbash/zshで実行し、各段階の結果を確認してから次へ進む。停止・snapshot・復帰の例をまとめて無確認で実行しない。

## 1. 更新前チェック

### 1. 手動DL必要MODの有無を外部確認

CurseForgeの公開ファイルと新バージョンのchangelogを確認し、更新先バージョン・ファイルID・NeoForgeの期待値を記録する。追加MODに配布ブロックがないか調べる。公開検索で判断できない場合はCurseForge APIの `allowModDistribution` も確認し、更新後の不足MOD確認を省略しない。APIキーをログや出力に表示しない。更新前の正常なmodpack名・ファイルIDもログとCurseForgeの公開ファイルで確認して記録する（ログにファイルIDがなければ現在のバージョンに対応する公開ファイルで補完する）。このIDはロールバック時の固定に使う。

```text
- 公開ファイル: https://www.curseforge.com/minecraft/modpacks/all-the-mods-10/files
- GitHub changelog: https://github.com/AllTheMods/ATM-10/blob/main/CHANGELOG.md
- 該当バージョン差分: changelogs/CHANGELOG-ATM10-X.Y-X.Y+1.md
```

### 2. クラスタ現状確認と変数準備

```bash
kubectl get pods -n atm10 -o wide
kubectl get pvc -n atm10

# 以降の手順で使う変数（現行値は atm10-data-v2 / atm10-downloads-v2）
DATA_PVC=$(kubectl get deployment atm10 -n atm10 \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}')
DOWNLOADS_PVC=$(kubectl get deployment atm10 -n atm10 \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="downloads")].persistentVolumeClaim.claimName}')
test -n "$DATA_PVC" && test -n "$DOWNLOADS_PVC"
DATA_VOLUME=$(kubectl get pvc "$DATA_PVC" -n atm10 -o jsonpath='{.spec.volumeName}')
test -n "$DATA_VOLUME"
echo "DATA_VOLUME=$DATA_VOLUME"

# ボリュームの健全性
kubectl get volume.longhorn.io "$DATA_VOLUME" -n longhorn-system -o jsonpath='{.status.robustness}{"\n"}'  # healthy であること

OLDPOD=$(kubectl get pod -n atm10 -l app=atm10 -o jsonpath='{.items[0].metadata.name}')
test -n "$OLDPOD"
# 停止前に接続中プレイヤーを確認
kubectl exec -n atm10 "$OLDPOD" -c minecraft -- rcon-cli list

# 現行のコンテナイメージを記録（障害時の切り分け用）
kubectl get pod -n atm10 -l app=atm10 \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="minecraft")].imageID}{"\n"}'

# 更新先バージョン（後続手順で使用）
TARGET_VERSION="8.2"   # 例。毎回、公開ファイルで確認した実際の更新先に合わせる
```

PodがReady、`$DATA_PVC` と `$DOWNLOADS_PVC` の両PVCがBound、data volumeが `healthy` なこと。停止前に接続中プレイヤーも確認する。

---

## 2. バックアップ

Longhorn backup はbackup targetへの外部保存（ボリューム消失時の復旧用）、Longhorn snapshot は同一ボリューム内の短期ロールバックポイント。両方を確認する。

### 1. 定期バックアップの確認（必須）

現行PVCは `gameserver` グループを指定し、`recurring-job-gameserver.yaml` の週次外部バックアップ（金曜03:00 UTC＝12:00 JST、4世代）と日次ローカルスナップショット（04:00 UTC＝13:00 JST、7世代）が対象。volumeの `recurring-job-group.longhorn.io/gameserver: enabled` と実クラスタのRecurringJobも確認する。`default` グループの月2回バックアップも併用される場合があるが、鮮度判定は週次運用を基準に**8日（7日間隔＋1日猶予）**とする（GNU date 前提）。

```bash
LAST=$(kubectl get volume.longhorn.io "$DATA_VOLUME" -n longhorn-system \
  -o jsonpath='{.status.lastBackupAt}')

if [ -z "$LAST" ]; then
  echo "ERROR: lastBackupAt is empty. バックアップ設定を確認するまで更新を中止。" >&2
  exit 1
fi

MAX_BACKUP_AGE_DAYS=8
AGE=$(( $(date +%s) - $(date -d "$LAST" +%s) ))
if [ "$AGE" -lt 0 ] || [ "$AGE" -gt $((MAX_BACKUP_AGE_DAYS*24*60*60)) ]; then
  echo "ERROR: Last backup timestamp is invalid or older than ${MAX_BACKUP_AGE_DAYS} days: $LAST" >&2
  exit 1
fi
echo "Backup OK: $LAST"
```

backup target上の実バックアップも確認する。volumeの `status.lastBackup` に対応する `Backup` CRが `status.state: Completed` で、作成時刻も上記の鮮度条件を満たすこと。日次snapshotを外部backupの代わりにしない。

```bash
# ラベルキーはLonghornバージョンで異なる（1.12系は backup-volume）。両方試す
kubectl get backup.longhorn.io -n longhorn-system \
  -l "backup-volume=$DATA_VOLUME" \
  --sort-by=.status.backupCreatedAt \
  -o custom-columns=NAME:.metadata.name,STATE:.status.state,CREATED:.status.backupCreatedAt \
  | tail -3
# ヒットしない場合: -l "longhorn.io/backup-volume=$DATA_VOLUME"
# 最新Backupの STATE が Completed であることを確認する
LAST_BACKUP=$(kubectl get volume.longhorn.io "$DATA_VOLUME" -n longhorn-system \
  -o jsonpath='{.status.lastBackup}')
test -n "$LAST_BACKUP"
kubectl get backup.longhorn.io "$LAST_BACKUP" -n longhorn-system \
  -o jsonpath='{.status.state}{"\n"}{.status.backupCreatedAt}{"\n"}'
```

### 2. 正常停止 → 更新直前スナップショット（推奨）

**警告**: 稼働中スナップショットはクラッシュ整合性のみ。復旧を目的とするなら正常停止後に取る。

Flux CLIがない場合は、以下の同等操作を使う。以降の `flux suspend` / `flux resume` もそれぞれ置き換えられる。

```bash
# suspend
kubectl patch kustomization apps -n flux-system --type=merge -p '{"spec":{"suspend":true}}'
# resume
kubectl patch kustomization apps -n flux-system --type=merge -p '{"spec":{"suspend":false}}'
# 状態確認（flux get の代替）
kubectl get kustomization apps -n flux-system
```

ATM10 DeploymentはFlux管理。`kubectl scale` や `kubectl set env` はreconcileで元に戻るが、`flux suspend` 中は干渉しない。停止時間を最小にするため、失敗・中断時もATM10を止めたままにする明確な理由がなければ必ず `flux resume` で戻すこと。

```bash
OLDPOD=$(kubectl get pod -n atm10 -l app=atm10 -o jsonpath='{.items[0].metadata.name}')

# 停止（apps Kustomizationは他アプリも含むのでsuspendは最短で）
flux suspend kustomization apps -n flux-system
# Pod削除後も正常停止を確認できるよう、停止前にログ追跡を開始する
SHUTDOWN_LOG=$(mktemp /tmp/atm10-shutdown.XXXXXX.log)
kubectl logs -f -n atm10 "$OLDPOD" -c minecraft --since=1m > "$SHUTDOWN_LOG" 2>&1 &
SHUTDOWN_LOG_PID=$!
if ! kubectl scale deployment/atm10 -n atm10 --replicas=0; then
  kill "$SHUTDOWN_LOG_PID" 2>/dev/null || true
  flux resume kustomization apps -n flux-system
  exit 1
fi
if ! kubectl wait --for=delete "pod/$OLDPOD" -n atm10 --timeout=300s; then
  kill "$SHUTDOWN_LOG_PID" 2>/dev/null || true
  cat "$SHUTDOWN_LOG"
  kubectl describe pod "$OLDPOD" -n atm10
  # 旧Podの状態を調査。正常停止・デタッチ未確認のままsnapshotや更新に進まない。
  exit 1
fi
wait "$SHUTDOWN_LOG_PID" || true
cat "$SHUTDOWN_LOG"
# Saving worlds / All dimensions are saved / mc-server-runnerのDone等で正常終了を確認。
# Pod削除だけでは正常停止の証明にならない。
# 注: terminationGracePeriodSeconds=120 超で強制終了された場合はアプリケーション整合性のある停止とは扱わない。
#     直前ログとイベントを確認し、必要なら更新を中止する。

# ボリュームのデタッチ確認（失敗時は snapshot 作成へ進まず中断）
if ! kubectl wait --for=jsonpath='{.status.state}'=detached \
  "volume.longhorn.io/$DATA_VOLUME" -n longhorn-system --timeout=300s; then
  echo "ERROR: Volume did not detach. Snapshot creation aborted." >&2
  kubectl get volumeattachment -A
  kubectl get volumeattachment.longhorn.io -n longhorn-system
  exit 1
fi

# スナップショット作成（名前に日時を含め再実行時の衝突を回避）
SNAPSHOT_NAME="atm10-pre-${TARGET_VERSION//./-}-$(date -u +%Y%m%d-%H%M%S)"
echo "Creating snapshot: $SNAPSHOT_NAME for $DATA_VOLUME"
cat <<EOF | kubectl apply -f -
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata:
  name: ${SNAPSHOT_NAME}
  namespace: longhorn-system
spec:
  volume: ${DATA_VOLUME}
  createSnapshot: true
EOF

# 作成後もCRは残る。snapshot controllerが処理のためvolumeを一時attachすることがあるが異常ではない
# 失敗時は復元点なしで更新に進まないよう中断する
if ! kubectl wait --for=jsonpath='{.status.readyToUse}'=true \
  snapshot.longhorn.io/${SNAPSHOT_NAME} -n longhorn-system --timeout=300s; then
  echo "ERROR: Snapshot was not ready. Update aborted." >&2
  kubectl describe snapshot.longhorn.io ${SNAPSHOT_NAME} -n longhorn-system
  # ATM10を停止したまま原因調査する。復旧時は flux resume を忘れないこと
  exit 1
fi
```

---

## 3. 更新実行（復帰のみ。追加の rollout restart は不要）

`CF_FORCE_SYNCHRONIZE=true` は不要。AUTO_CURSEFORGEは選択されたmodpackファイルと永続化されたインストール情報を比較し、差分があれば自動で新バージョンをDLする。`CF_FORCE_SYNCHRONIZE` は万能な強制更新フラグではなく、既存ファイルやexclude/include条件の再同期が必要と確認できた場合に限り、Git側のenvに以下形式で一時追加→PR→同期確認後に削除する。

```yaml
- name: CF_FORCE_SYNCHRONIZE
  value: "true"
```

### 復帰と起動追跡

```bash
flux resume kustomization apps -n flux-system
# FluxがGit上の replicas: 1 を再適用してPodが作成される（手動scaleや rollout restart は不要）

# Pod作成を待ってから取得（300sタイムアウト、Flux障害・スケジュール失敗の無限待ちを防ぐ）
NEWPOD=""
for _ in $(seq 1 150); do
  NEWPOD=$(kubectl get pod -n atm10 -l app=atm10 \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null)
  [ -n "$NEWPOD" ] && break
  sleep 2
done
if [ -z "$NEWPOD" ]; then
  echo "ERROR: ATM10 Pod was not created within 300 seconds" >&2
  flux get kustomization apps -n flux-system
  kubectl get deployment,replicaset,pod -n atm10
  exit 1
fi
echo "POD=$NEWPOD"

# Ready待ちを先に（タイムアウトは成功ではない。手動MOD不足等の診断へ進む）
kubectl wait --for=condition=Ready "pod/$NEWPOD" -n atm10 --timeout=1200s || true

# ログ確認（リアルタイム追跡したい場合は別ターミナルで kubectl logs -f を使う）
kubectl logs -n atm10 "$NEWPOD" --tail=200
```

---

## 4. 更新後の確認

### 1. バージョン確定（必ず期待値と照合）

```bash
kubectl logs -n atm10 "$NEWPOD" | grep -E "Processing modpack|Requested CurseForge modpack|neoForgeVersion" | head -5
```

- 新規更新時の `Processing modpack 'All the Mods 10-X.Y' ... @ projectID:fileID`、または既存インストール時の `Requested CurseForge modpack ... is already installed` から、modpack名が `All the Mods 10-<TARGET_VERSION>` と完全一致することを確認する。ファイルIDが出力される場合は事前確認した値とも照合する（`is already installed` の文字だけでは成功判定しない）
- NeoForgeバージョンがchangelogの期待値と一致

### 2. 手動DL必須MODの有無

```bash
kubectl logs -n atm10 "$NEWPOD" --all-containers=true | \
  grep -iE 'disallowed project distribution|mods need download|failed to auto-install'
kubectl exec -n atm10 "$NEWPOD" -- \
  sh -c 'find /data -maxdepth 3 -name "MODS_NEED_DOWNLOAD.txt" -type f -print -exec cat {} \;' 2>/dev/null
```

PodがReady・起動完了ログあり・上記エラーなしなら手動対応なし。CrashLoop中でexecが失敗する場合は前回ログを確認：

```bash
kubectl logs -n atm10 "$NEWPOD" -c minecraft --previous --tail=500
kubectl describe pod -n atm10 "$NEWPOD"
```

### 3. 不足MODがあった場合の対処

配布制限MODは `/downloads/mods/` に配置する。通常は `/data/mods/` に直接配置しない（modpack更新時に削除される）。

- PodがRunning維持の場合のみ `kubectl cp <jar> atm10/$NEWPOD:/downloads/mods/` で簡易コピー可
- CrashLoopBackOffで不安定な場合は `docs/atm10-manual-mods.md` の「Deployment一時停止→uploader Pod→復帰」手順を使用。手順書には旧PVC名 `atm10-data` / `atm10-downloads` が残っているため、uploaderのclaimNameは `$DATA_PVC` / `$DOWNLOADS_PVC` に置き換える。`longhorn-gameserver` はstrict-localなので、uploaderもDeploymentと同じノード（現行 `mainworker-1`）へ配置する。停止・復帰にはこのスキルのFlux suspend/resumeを利用できる
- 例外：Prometheus Exporter等のmanifest外追加MODは `docs/atm10-manual-mods.md` に従い、必要時のみ `/data/mods/` にもコピー

### 4. 起動完了の確認（3条件）

1. 期待modpackバージョンが選択されている
2. `kubectl get pods -n atm10` で READY `1/1`
3. 起動完了ログ：

```bash
kubectl logs -n atm10 "$NEWPOD" | grep -E 'Dedicated server took .* seconds to load|Done \(.*\)! For help'
kubectl exec -n atm10 "$NEWPOD" -c minecraft -- rcon-cli list
```

### 5. Simple Backups設定の再確認（必須）

modpack更新で初期値に戻るとLonghornと二重バックアップになる。

```bash
kubectl exec -n atm10 "$NEWPOD" -- grep -E '^[[:space:]]*enabled[[:space:]]*=' \
  /data/config/simplebackups-common.toml
# 期待値: enabled = false
```

`false` でない場合の修正：

```bash
kubectl exec -n atm10 "$NEWPOD" -- \
  sed -i -E 's/^[[:space:]]*enabled[[:space:]]*=.*/enabled = false/' \
  /data/config/simplebackups-common.toml
kubectl exec -n atm10 "$NEWPOD" -- grep -E '^[[:space:]]*enabled[[:space:]]*=' \
  /data/config/simplebackups-common.toml   # enabled = false を確認
kubectl rollout restart deployment/atm10 -n atm10   # Fluxがresume済みであること
```

修正で再起動した場合は新しいPodを取得し直し、バージョン・Ready・起動完了ログ・Simple Backups設定を再確認する。

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| バージョンが更新されない | 起動ログの選択済みmodpack名・ファイルIDと、`deployment.yaml` に `CF_FILE_ID`/`CF_FILENAME_MATCHER` が意図せず追加されていないかを確認。内部のバージョン記録ファイルは推測で削除しない。再同期が必要と判明したらgit側envに `CF_FORCE_SYNCHRONIZE` を追加してPR（完了後に削除） |
| 手動DL MODが繰り返し要求される | `MODS_NEED_DOWNLOAD.txt` のファイル名と `/downloads/mods/` の実ファイルを照合（別バージョンjar・名前変更・破損・権限を疑う） |
| PodがTerminatingのまま | `kubectl describe pod`、配置ノード、Kubernetesの `VolumeAttachment`、Longhorn volumeの state を確認し、旧ノードでプロセスとボリューム利用が止まったことを確認できるまで強制削除しない（ノード停止等で確実な場合のみ最終手段として強制削除） |
| イメージ更新で問題発生 | 手順1-2で記録した古い imageID と比較し、modpack問題かコンテナイメージ問題かを切り分ける |
| 更新後に起動しない（ロールバック） | 1) `flux suspend kustomization apps -n flux-system` → scale 0 → Pod削除・volume Detached確認 2) Longhorn UIで対象snapshotを Revert（detached状態で実行） 3) volume が `healthy` を確認 4) 復帰前にGit側の `CF_FILE_ID` を更新前の正常ファイルIDへ固定し、必要ならコンテナイメージも記録した正常digestへ固定する。PRで反映し、FluxのGitRepositoryがその変更を含むrevisionを取得済みであることを確認してから `flux resume` 5) 正常版の選択・Ready・起動ログとワールドを確認。**未固定のままresumeすると失敗した最新版を再選択するため、snapshotだけでロールバック完了とはしない。snapshotはボリューム内のため、ボリューム自体の消失時はLonghorn backupからのリストアが必要** |

## 参照ファイル

- マニフェスト: `clusters/main/apps/atm10/deployment.yaml`
- PVC: `clusters/main/apps/atm10/pvc-v2.yaml`（atm10-data-v2 / atm10-downloads-v2）
- 手動MOD運用・uploader Pod手順・Simple Backups: `docs/atm10-manual-mods.md`
- ゲームサーバー用週次バックアップ・日次スナップショット: `clusters/main/infrastructure/controllers/longhorn/recurring-job-gameserver.yaml`
- defaultグループ用月2回バックアップ: `clusters/main/infrastructure/controllers/longhorn/recurring-job.yaml`
- 外部公開: `atm10.turtton.net`（HAProxy via VPS）
