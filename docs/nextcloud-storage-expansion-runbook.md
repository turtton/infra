# Nextcloud ストレージ拡張・週次バックアップ

## 構成

- ファイル PVC `nextcloud/nextcloud-nextcloud` の Longhorn CSI volumeHandle は `nextcloud-restore-20260801`。PV 名とは異なるため、操作時は PVC → PV → `spec.csi.volumeHandle` を確認する。
- ファイル容量は 250Gi から 300Gi に拡張し、Longhorn レプリカを 2 から 1 にする。唯一のレプリカは SSD を使う `mainworker-1` に置き、node/disk に `nextcloud` タグ、volume に同じ node/disk selector を設定する。既存の `gameserver` タグは残す。HDD は使わない。
- DB PVC `nextcloud/nextcloud-db-1` は別ボリューム。DB 側のレプリカ設定は変更しない。
- ファイル側は単一レプリカなので、`mainworker-1` またはその SSD を失うと停止する。ローカル Snapshot は同じ障害に耐えない。R2 に保存された週次のファイル・DB バックアップ組が復旧点であり、最大で約 7 日のデータ損失を許容する。

## 移行時の順序

1. Nextcloud を maintenance mode にし、ファイルと DB の Longhorn Snapshot を取得する。maintenance mode を解除してから両 Snapshot の R2 Backup CR が `Completed`、`progress=100`、URL ありとなるまで待つ。
2. DB Backup を一時 PVC に復元し、`pg_controldata` とチェックポイント WAL が読めることを確認する。一時 PVC は削除する。
3. `mainworker-1` の Longhorn node/disk に `nextcloud` タグを追加する。`gameserver` タグは保持する。`allow-empty-node-selector-volume` と `allow-empty-disk-selector-volume` は変更しない。
4. ファイル volume の node/disk selector を `nextcloud`、`numberOfReplicas` を 1 にし、旧ノードに残ったレプリカを `evictionRequested` で移す。移動先に healthy なレプリカが完成するまで旧レプリカを手動削除しない。唯一のレプリカが `mainworker-1` 上で `running`、volume が `healthy` であることを確認する。
5. PVC を 300Gi に拡張し、PVC capacity、Longhorn volume size、Nextcloud コンテナ内 `df -h` を確認する。GitOps の HelmRelease も 300Gi / 1 レプリカに合わせる。
6. 週次ジョブを一度手動実行し、maintenance mode が解除され、ファイル・DB の R2 バックアップ組が両方完了することを確認する。その後、両 volume に `recurring-job-group.longhorn.io/nextcloud-external=enabled` を付け、`recurring-job-group.longhorn.io/default` を外す。Longhorn は定期ジョブラベルがゼロの volume に `default` を自動付与するため、空の専用グループを残して従来の月次ジョブを重複実行させない。

切り戻しが必要なら、拡張前は selector を元に戻してレプリカ数を 2 に戻す。PVC を 300Gi に拡張した後は縮小できない。単一レプリカを失った場合は、最後に完了した同じ組の R2 バックアップからファイル・DB を復元する。

## 週次ジョブ

`clusters/main/apps/nextcloud/weekly-backup.yaml` の `nextcloud-weekly-backup` は毎週日曜 04:00 JST に実行する。Nextcloud の書き込みを止め、DB に `CHECKPOINT` を実行して両 Snapshot を取得する。Nextcloud 再開後に R2 へアップロードし、完了した組を 4 世代保持する。`nextcloud-backup-recovery` は 5 分間隔で中断したジョブを検知し、アプリのレプリカ数と maintenance mode を戻す。

```bash
kubectl -n nextcloud get cronjob nextcloud-weekly-backup nextcloud-backup-recovery
kubectl -n nextcloud get jobs --sort-by=.metadata.creationTimestamp
kubectl -n nextcloud get deploy nextcloud
kubectl -n nextcloud exec deploy/nextcloud -c nextcloud -- \
  runuser -u www-data -- php /var/www/html/occ status --output=json
kubectl -n longhorn-system get backups.longhorn.io -l backup.nextcloud.turtton.net/policy=nextcloud-weekly
```

バックアップ組を確認する際は、同じ `backup.nextcloud.turtton.net/pair` 値の `files` と `database` が共に `Completed`、`progress=100`、R2 URL ありであることを見る。障害復旧ではファイルと DB を同じ組から復元し、DB のクラッシュリカバリーを完了させてから Nextcloud を起動する。週次ジョブが失敗した場合は、maintenance mode が解除されていることと、上記の 2 つの CronJob / Job ログを確認する。

## 2026-09-29 実施記録

- 移行前の保護用 Snapshot: `ncsafe-20260929130243-files`、`ncsafe-20260929130243-db`。対応する R2 Backup は `ncsafe-20260929130243-files-backup`、`ncsafe-20260929130243-db-backup` で、両方 `Completed` / 100%。DB は R2 から一時 PVC に復元し、制御ファイルと WAL を読めた。
- 週次 Job `nextcloud-weekly-backup-manual-20260929-b` が成功。組 `20260929134311-d7f8336b` のファイル・DB Backup は両方 `Completed` / 100%。keeper 削除、Longhorn/CSI detach、Nextcloud の全コンテナ Ready への復帰を確認した。
- 同じ週次組の DB Backup を `mainworker-1` の SSD 上に一時復元し、PostgreSQL 制御ファイルとチェックポイント WAL を読めた。一時 Pod/PVC/StorageClass は削除済み。
- `mainworker-1` の Longhorn node/disk タグは `gameserver,nextcloud`。ファイル volume の希望レプリカ数は 1、node/disk selector は `nextcloud`。SSD 上の新レプリカ `nextcloud-restore-20260801-r-bea7385f` が 100% / RW / healthy になった後、旧 `toliworker-1` レプリカを削除した。
- ファイル PVC の request/capacity は 300Gi / 300Gi、Longhorn volume size は 322122547200 bytes、レプリカ 1 / healthy。Deployment を Recreate で再起動してファイルシステム拡張を完了した。コンテナ内 `df -h /var/www/html/data` は 295G 総量・236G 使用・59G 空き。Nextcloud は maintenance=false、Deployment Ready/Available 1。
- ファイルと DB の両 volume は `recurring-job-group.longhorn.io/nextcloud-external=enabled`、`default` ラベルなし。Nextcloud 週次 CronJob は日曜 04:00 JST、`suspend=false`。旧月次ジョブはこの 2 volume に適用されない。

## SSD 障害時の復元

`mainworker-1` またはその SSD が失われた場合は、まず Nextcloud の書き込みを止める。十分な空き容量がある別の SSD ノードと Longhorn disk に `nextcloud` タグを付ける。HDD へは配置しない。

Longhorn の R2 Backup から、同一 `pair` 値で両方 `Completed` / 100% のファイルと DB を選ぶ。`fromBackup` を指定した一時 StorageClass/PVC をそれぞれ作り、ファイルと DB を新規 volume として復元する。StorageClass には `numberOfReplicas: "1"` と `nodeSelector: nextcloud`、`diskSelector: nextcloud` を指定する。DB は PostgreSQL のクラッシュリカバリーを経て起動する。復元したデータで Nextcloud を起動し、`occ status`、ファイル一覧、DB の接続を確認してから利用を再開する。

復元前に既存の PVC/PV を削除しない。復旧対象の二つの R2 URL、復元先 SSD の容量、Nextcloud/CNPG の PVC 名と CSI volumeHandle を記録し、切替手順を確定させてから変更する。
