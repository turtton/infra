# Lepinoid 建築サーバー k8s 移行 & mainworker-1 再構成 Runbook

Proxmox main ノードの LXC `testserver`（CT 102）で稼働している Lepinoid 建築サーバー（PaperMC 1.21.1）を Kubernetes（Talos クラスタ）へ移行する。あわせて mainworker-1 を増設 1TB SSD へ移設・増強し、旧 SSD（toshibassd）の空きを cp-1 に還元、atm10 もローカル IO 化する。

> 本書は Oracle レビュー（2026-09-06、3 ラウンド）を経て **APPROVED** 済み。
>
> **実行ステータス（2026-09-06 07:40 JST 更新）**: Phase 0〜5 は全て完了（PR #131〜#138、Lepinoid/infra PR #2, #3）。build.lepinoid.net は k8s 上の build-server（mainworker-1 ローカル IO、Paper 1.21.1、CoreProtect→mysql:8.4 `coreprotect` DB）へ切替済みで外部経路の疎通検証済み。残タスクは testserver（CT 102）の廃止のみ — 数日様子見後に vzdump 外部保存 → `pct destroy 102` を実施する。実行中に判明した実機挙動の差異（bpg の datastore 移動は in-place、podman exec は不安定でクライアントコンテナ経由に変更、apt repo 401/署名問題の解消）は本書の手順を修正せず実行記録として残す。

## 背景・現状整理

### testserver（CT 102, 192.168.10.151）の実態（2026-09-06 調査）

| 項目 | 内容 |
|---|---|
| スペック | 8 vCPU / 17GB RAM / 89GB ディスク（34GB 使用） |
| MC サーバー | PaperMC 1.21.1、port 49966、Xmx12G/Xms8G、`~/lepinoid/main1.21.1` の `start.sh` を手動起動（自動再起動ループ）。現在停止中 |
| ワールド | Multiverse で 11 ワールド（1234, terrestrial, lv1, unchch, world 等）。実データ計約 2.8GB + `.git` 4.6GB |
| バックアップ | 自作プラグイン `LepinoidTools` が AutoCommit を実行し `github.com/Lepinoid/Worlds` へ push。認証は LXC 内 `gh` CLI の credential helper |
| CoreProtect DB | podman の `mysql:8.1-oracle` コンテナ（datadir: `~/lepinoid/coreprotect-sql/data`、16GB。うち binlog 約 1.2GB）。**DB 名はシステムDB `mysql` を流用**、認証は root / 平文パスワードが config に記載 |
| プラグイン | 17 個。有料: Arceon / HeadDatabase / MetaBrushes。自作: LepinoidTools。無料: Multiverse-Core, FAWE, FAVS, CoreProtect, BuildersUtilities, goBrush, goPaint, LunaChat, spark |
| 秘情報 | LepinoidTools config.yml に Discord トークン平文、CoreProtect config に MySQL パスワード平文 |
| 公開経路 | VPS の HAProxy が `build.lepinoid.net` → `testserver:49966` へ Minecraft handshake 振り分け |
| その他（スコープ外） | time-supporter-bot, health-check-bot, ~/yu（mc1.20.1/mc1.21.11）, ~/modpack/CreateAstral は今回移行しない |

### クラスタ側の現状

- `cp-1`（main, VM 1000, 4CPU/24GB, 32GB @ toshibassd）
- `mainworker-1`（main, VM 1016, 4CPU/20GB, 120GB @ toshibassd、Longhorn 除外ノード）
- 1TB SSD（CT1000MX500SSD1, /dev/sda, 931.5GB）は datastore `crucialssd`（LVM-Thin, thin pool 884.7GB, `nodes main` 限定）として作成済み（2026-09-06）。Terraform への登録は未実施
- Longhorn レプリカ配置は worker-1/2 + toliworker-1/2/3 の 5 ノード限定（`data-worker-consolidation-runbook.md`）
- atm10 の PVC は `data-locality: best-effort` だが mainworker-1 は Longhorn 除外のため、mainworker-1 上で動くと全 IO がネットワーク越しになる

## 確定方針

| 論点 | 決定 |
|---|---|
| mainworker-1 移設 | VM 再作成（datastore 変更）。**config apply の再実行は plan でゲート確認し、出なければ `-replace` 明示** |
| cp-1 の旧 SSD 利用 | ディスク拡張のみ（in-place resize）。Longhorn 参加は将来 CP 冗長化時に再検討 |
| 建築サーバーデータ | Longhorn レプリカ 1 + `strict-local` + required nodeAffinity で mainworker-1 ピン留め。完全ローカル IO。**レプリカ1は単一障害点のため、Longhorn recurring backup への登録を必須とする** |
| mainworker-1 の Longhorn 参加 | 「ゲームサーバー専用ノード」として再定義し Longhorn 参加。node/disk tag `gameserver` + StorageClass の node/diskSelector で制御 |
| MySQL | CNPG（PostgreSQL）は CoreProtect 非対応のため不可。lepinoid ns 内に `mysql:8.4`（LTS）独自 Deployment + PVC。**datadir 物理コピーは行わず、CoreProtect テーブル（`co_*`）のみ論理 dump/restore し、新規 `coreprotect` DB + 専用ユーザーを作成**（DB 名整理を同時に解決） |
| プラグイン管理 | 初回は現行 `plugins/` を丸ごとコピーして起動確認。その後、無料プラグインを itzg `PLUGINS`（バージョン固定・immutable URL）へ **1 個ずつ** 移行（`PLUGINS` は一覧外エントリを削除する動作があるため一括移行しない）。有料は PVC 手動維持。自作 LepinoidTools は CI で OCI イメージ化 → initContainer 配置（後続タスク）。Multiverse-Core と LepinoidTools のバージョンは同一 PR でペア更新 |
| マニフェスト配置 | `Lepinoid/infra` リポジトリ（既存テナント構成に乗せる） |
| atm10 | mainworker-1 再作成中は git で replicas 0 にして停止。再作成後に `longhorn-gameserver` へ PVC 移行してローカル IO 化 |
| 移行スコープ | 建築サーバー（PaperMC + CoreProtect MySQL）+ atm10 ローカル IO 化 |

## Phase 0: 前提作業

1. ~~1TB SSD に datastore 作成~~ **完了（2026-09-06）**: `/dev/sda`（CT1000MX500SSD1）に VG `crucialssd` + thin pool `data`（884.7GB）、`pvesm add lvmthin` + `--nodes main` 限定済み
2. **容量・故障ドメインの preflight**:
   - main ホストの物理 RAM / CPU overcommit を確認（cp-1 4C/24G + mainworker-1 新スペック + 他 VM の合計が物理リソースに収まるか）
   - `pvesm status` と `lvs -a crucialssd` で thin pool の data/meta 使用率を記録
   - Longhorn はデフォルトで disk の 20% を予約（StorageOverProvisioningPercentage も考慮）するため、500GB root disk で build-server(50GB) + MySQL(30GB) + atm10 + スナップショットに十分な実容量があるか計算
3. testserver 側の棚卸しは本書の現状整理で完了済み
4. GitHub PAT 発行: LepinoidTools の AutoCommit push 用に `Lepinoid/Worlds` への write 権限を持つ PAT（または GitHub App）を用意。k8s Secret 化する
5. **MySQL 移行の事前確認**（testserver 上で実施）:
   - `util.checkForServerUpgrade()`（MySQL Shell）で 8.4 互換性を確認 ※論理 dump 経路でも非推奨構文・削除機能の検出に有効
   - CoreProtect テーブル一覧を確定: `SELECT table_name FROM information_schema.tables WHERE table_schema='mysql' AND table_name LIKE 'co\_%';`
   - `SHOW TRIGGERS`、`information_schema.REFERENTIAL_CONSTRAINTS` で trigger/FK の有無を確認
   - **全対象テーブルのエンジン確認**: `SELECT table_name, engine FROM information_schema.tables WHERE table_schema='mysql' AND table_name LIKE 'co\_%';` で全て InnoDB であることを確認（`--single-transaction` の整合性スナップショットはトランザクショナルテーブルのみ有効。非 InnoDB があれば明示的な read lock / メンテナンス手順に切り替える）
   - GTID / replication を使用していないことを確認

## Phase 1: mainworker-1 の新 SSD 移設・増強

testserver の 8CPU/17GB を吸収する想定スペック（最終値は Phase 0 の preflight 結果で調整）:

```hcl
# terraform/terraform.tfvars
mainworker-1 = {
  host_node    = "main"
  vm_id        = 1016
  ip           = "192.168.10.126"
  cpu          = 8
  ram          = 32768            # 32GB（物理 62GB - cp-1 24GB - ホスト分の実質上限）
  disk_size    = 800             # crucialssd 884.7GB の thin pool 上に thin 確保
  datastore_id = "crucialssd"    # Phase 0 で作成済み（LVM-Thin, 884.7GB）
}
```

手順（PR / CI ベース）:

1. **atm10 を一時停止**: `clusters/main/apps/atm10/deployment.yaml` の `replicas` を 0 にして commit & push（Flux 管理のため手動 `kubectl scale` は reconcile で戻される）。**実施済み（2026-09-07, main@fedadbc）**
2. **Longhorn 側の事前ゲート**: `mainworker-1` ノード上の replica / backing image が **ゼロ**であることを確認。**実施済み**: `lepinoid-data`（1Gi）と `atm10-data`（10Gi）のレプリカが存在したため、`nodes.longhorn.io/mainworker-1` に `allowScheduling=false, evictionRequested=true` を設定して eviction 完了（0 台確認）
3. `kubectl drain mainworker-1 --ignore-daemonsets --delete-emptydir-data` **実施済み**
4. PR を作成し CI の plan コメントで確認 — **続行条件: 差分が `talos_node["mainworker-1"]`（VM）と `talos_machine_configuration_apply.worker["mainworker-1"]`（`replace_triggered_by` により自動で replace）のみであること。`talos_machine_secrets.this` と `talos_machine_bootstrap.this` が置換対象に含まれていたら絶対に進まない**
5. **旧 VM 停止 + Node CR 削除（apply 前に手動実施）**: CI の apply は destroy/create を一続きで行うため、事前に停止点を作る:
   1. Proxmox 上で `qm shutdown 1016` → 停止を確認
   2. `kubectl delete node mainworker-1`（cordon 状態の残留防止。kubelet が停止済みなので再作成されない）
   3. `kubectl delete nodes.longhorn.io mainworker-1`（旧 disk UUID の残存防止）
6. **`/tf-apply` を PR にコメント**（turtton のみ実行可）。apply 末尾の `data.talos_cluster_health.this`（全 worker 検査）が新 VM の起動と競合して失敗した場合は、VM 作成と config apply は完了しているため、ノード復帰後に再度 `/tf-apply` で残差分を回収する
7. 再 join 後の確認:
   - `kubectl get nodes` で Ready
   - `kubectl get node mainworker-1 -o jsonpath='{.spec.unschedulable}'` が空/false であること（残っていれば `kubectl uncordon`）
   - `talosctl -n 192.168.10.126 health`
   - Cilium、Longhorn manager Pod が当該ノードで Running

## Phase 2: mainworker-1 の Longhorn 参加（ゲームサーバー専用）+ atm10 ローカル IO 化

1. Longhorn UI で `mainworker-1` ノードの Scheduling を有効化
2. ノードに tag `gameserver`、デフォルトディスク（`/var/lib/longhorn`、crucialssd 上の root disk）に disk tag `gameserver` を設定
3. **設定値の記録**: node/disk tag、disk path、disk UUID、`allowScheduling` を YAML として保存（将来の再作成時に tag 再付与が必要になるため、運用記録に残す）
4. **空 selector ボリュームの流入対策（必須実施）**: Longhorn デフォルトでは `Allow Empty Node Selector Volume=true` / `Allow Empty Disk Selector Volume=true` のため、selector を持たない既存ボリュームも `gameserver` タグ付きノードへ配置され得る。既存ノードのタグ状況を監査し、他ノードにタグがなければ（＝影響なし）、両設定を `false` に変更する。監査の結果タグ付きノードが他にあり変更できない場合は、本 Runbook の実施を止めて方針を再検討すること
5. ゲームサーバー専用 StorageClass を作成（`clusters/main/infrastructure/controllers/longhorn/` に追加。既存 `storageclass-single.yaml` のパラメータに揃える）:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: longhorn-gameserver
provisioner: driver.longhorn.io
allowVolumeExpansion: true
parameters:
  numberOfReplicas: "1"
  dataLocality: "strict-local"
  nodeSelector: "gameserver"
  diskSelector: "gameserver"
  staleReplicaTimeout: "30"
  fsType: "ext4"
  dataEngine: "v1"
  backupTargetName: "default"
  fromBackup: ""
reclaimPolicy: Retain
volumeBindingMode: Immediate
```

   - PVC 作成は Pod の required affinity と node/disk tag の設定完了を確認した後に行う（Immediate binding のため順序必須）
6. `data-worker-consolidation-runbook.md` の方針記述を更新（mainworker-1 は `gameserver` タグ付きボリュームのみ配置、に改訂）

### atm10 のローカル IO 化（Phase 2 内で実施）

mainworker-1 再作成直後・atm10 停止中のタイミングで、atm10 の PVC も `longhorn-gameserver` へ移行する:

1. **旧 PV の保護**: 現 PVC（`storageClassName: longhorn`）の reclaim policy は Delete の可能性があるため、旧 PV 名・volumeHandle・reclaim policy を記録し、必要なら `kubectl patch pv <name> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'` で保護。オンデマンド backup も取得
2. 新 PVC `atm10-data-v2`（`longhorn-gameserver`, 10Gi）と `atm10-downloads-v2`（現行 500Mi 以上）を `clusters/main/apps/atm10/` に追加して push
3. 一時コピー Pod で新旧両 PVC を同時マウント（RWO のため単一 Pod、nodeSelector: mainworker-1）。**busybox には rsync がないため rsync 同梱イメージ（digest 固定）を使用**:

   ```bash
   rsync -aHAX --numeric-ids --info=progress2 /old/ /new/
   ```

   コピー後に `du` 比較・ファイル数比較（必要なら checksum 検証）
4. `deployment.yaml` を更新して push:
   - PVC 名を v2 に差し替え
   - nodeAffinity の preferred を **required**（`requiredDuringSchedulingIgnoredDuringExecution` で mainworker-1 固定）に変更（strict-local の要件）
   - `replicas: 1` に戻す
5. 起動確認（ログイン・ワールド読み込み・Longhorn UI で Volume の Node が mainworker-1 であること）
6. **ロールバック期限（数日）経過後**に旧 PVC をマニフェストから削除して push し、Longhorn 側の残存ボリュームも削除（PV が Retain 化されていることを確認してから）

### バックアップ設定

- build-server / MySQL / atm10 の各新ボリュームに Longhorn recurring job（`monthly-backup` 等）が割り当たるよう、`recurringJobSelector` または volume label/group を明示
- MySQL は月 2 回では RPO が不足するため、日次バックアップを具体化して実装する:
  - Longhorn `RecurringJob`: task=backup、cron `0 3 * * *`、retain 7、対象は volume label `backup-group: mysql-daily` 等で selector 指定
  - **Longhorn の live-volume backup は基本的にクラッシュ整合のため**、InnoDB recovery で復旧できることを restore 試験で確認し、あわせて日次の論理 dump（mysqldump を CronJob で実行し Longhorn 外 or 別 PVC へ保存）も併用する
  - 日次バックアップの作成・割当・**初回 backup 成功**を Phase 5 廃止ゲートの続行条件とする

## Phase 3: cp-1 ディスク拡張

1. `terraform.tfvars` の `cp-1.disk_size` を 32 → 128 に変更（toshibassd 256GB、mainworker-1 の 120GB 退避後の空きを充当）
2. `tofu plan` で in-place 拡張（replace なし）になることを確認して `tofu apply`
3. block device / partition / filesystem それぞれのサイズを確認（`talosctl -n 192.168.10.110 mounts`、fdisk 等）。**Talos の EPHEMERAL filesystem grow は再起動が必要な場合があり、その場合は単一 CP の API 停止を伴うため mainworker 再作成とは別の管理ウィンドウで実施する**。cp-1 の再起動と mainworker-1 再作成を同時に行わないこと

## Phase 4: 建築サーバーの k8s マニフェスト作成（Lepinoid/infra）+ データ移行

`Lepinoid/infra` リポジトリに `build-server/` を追加（`turtton/infra` の `clusters/main/apps/atm10/` を雛形にする）:

```
build-server/
├── kustomization.yaml
├── deployment.yaml        # itzg/minecraft-server（digest/tag 固定）, TYPE=PAPER, VERSION=1.21.1
├── service.yaml           # Tailscale LoadBalancer, port 25565
├── pvc.yaml               # 50GB, longhorn-gameserver, strict-local
├── mysql-deployment.yaml  # mysql:8.4
├── mysql-service.yaml     # ClusterIP
├── mysql-pvc.yaml         # 30GB, longhorn-gameserver
└── secrets.sops.yaml      # gh PAT, MySQL パスワード, Discord トークン
```

### マニフェスト要点

- **Deployment**: required nodeAffinity で mainworker-1 固定（strict-local の要件）、`strategy: Recreate`、MEMORY=12G
- **プローブ**: `startupProbe`（大規模ワールド + 多数プラグインの起動待ち、atm10 同様に最大 10 分程度）、liveness/readiness は TCP 25565、`terminationGracePeriodSeconds` を十分に確保し SIGTERM で Minecraft の `stop` が完了することを確認。MySQL にも startup/readiness probe と shutdown grace を設定
- **イメージ固定**: `itzg/minecraft-server:latest` は使わず検証済み tag/digest に固定。使用イメージで `git --version` を確認し AutoCommit に使えることを検証
- **プラグイン**: 初回は現行 `plugins/` を丸ごとコピーして起動確認（`PLUGINS` 移行は後日 1 個ずつ）
- **AutoCommit 用 git 認証**: initContainer の `git config --global` は main container に引き継がれない（rootfs/$HOME 非共有）。PAT を Secret から読む `GIT_ASKPASS` スクリプト、または main container から読める専用 memory volume に `.gitconfig`/credentials を生成する構成にする。**PAT をワールド PVC や `.git/config` の remote URL に保存しない**
- **CoreProtect**: `mysql-host` を Service 名（`coreprotect-mysql`）に変更。接続ユーザーを root から専用ユーザーに変更
- **プラグイン設定内の平文 Secret の置換**: `plugins/` を丸ごとコピーすると CoreProtect config.yml の root パスワードと LepinoidTools config.yml の Discord トークンも PVC に残る。以下を手順化する:
  1. initContainer または手動のデバッグ Pod で、コピー後の `config.yml` を Secret 参照の値に書き換える（envsubst 等で `secrets.sops.yaml` 由来の値を注入）
  2. CoreProtect: DB host/名/ユーザー/パスワードを新値に変更
  3. LepinoidTools: Discord トークンを新値に変更
  4. PVC 上の旧平文値が残っていないことを grep で確認
- **MySQL**: `mysql:8.4` を**新規空 datadir**で初期化。`MYSQL_DATABASE=coreprotect`、`MYSQL_USER`/`MYSQL_PASSWORD`（専用ユーザー）、`MYSQL_ROOT_PASSWORD` を Secret から設定。旧 datadir はコピーしない
- **SOPS**: Lepinoid テナントは `sops-age-lepinoid` で復号済みのため、同じ Age 鍵で暗号化

### データ移行手順（write fence あり・二段階コピー）

**転送経路**: デバッグ Pod（rsync + openssh-client 同梱の digest 固定イメージ、build-server PVC マウント、nodeSelector: mainworker-1）から **pull 方向**で rsync する。移行用 SSH 秘密鍵は Secret マウントし、testserver 側の authorized_keys に一時登録:

```bash
# デバッグ Pod 内で実行（再実行可能・差分転送）
rsync -aHAX --numeric-ids --info=progress2 \
  -e "ssh -i /ssh-key/id_ed25519 -p 5141 -o StrictHostKeyChecking=accept-new" \
  root@192.168.10.151:/root/lepinoid/main1.21.1/ /data/
```

**事前コピー（旧サーバー稼働中でも可）**:

1. 上記 rsync で `~/lepinoid/main1.21.1/` → build-server PVC の `/data`（`.git` ごと）

**最終コピー（書き込み停止後・本番切替時）**:

2. 旧 Minecraft を停止（`stop` コマンドで正常終了）。**以後、旧サーバーを起動しない（write fence）**
3. MySQL への**アプリ接続がないこと**を確認（`SHOW PROCESSLIST` で CoreProtect/Minecraft 由来の接続がなく、管理用の確認セッション自身のみであること。接続元 host・command・state で識別）
4. 必要な場合のみ、稼働中に SQL の `PURGE BINARY LOGS` でサイズ削減（**binlog ファイルの手動削除は禁止**）
5. **MySQL 8.1 を稼働させたまま** CoreProtect の論理 dump（`--single-transaction` のため停止不要）:

   ```bash
   mysqldump --single-transaction --quick --triggers --hex-blob --set-gtid-purged=OFF \
     mysql co_table1 co_table2 ... > coreprotect.sql
   ```

   - テーブル一覧は Phase 0-5 で確定した `co_*` を明示指定（`--databases` は付けない。`CREATE DATABASE`/`USE` を含まない dump になる）
   - 終了コード・ファイルサイズを確認。テスト dump で `mysql` スキーマを再選択する記述がないことを確認
6. **旧 DB の検証ベースライン記録（停止前に必須）**: `co_*` のテーブル一覧・各テーブルの row count・trigger 定義・FK メタデータ（必要に応じて checksum）をファイルに保存する。**停止後では旧 DB と比較できないため必ずこの順序で実施**
7. dump 検証・ベースライン記録の完了後、旧 MySQL を正常停止（datadir はコピーしないため `innodb_fast_shutdown=0` は不要）
8. ワールドデータの最終差分 rsync（手順 2 以降の変更分のみ転送される）
9. 新 MySQL 8.4 を起動 → 初期化完了と readiness を確認 → restore（認証は Secret 由来の環境変数で渡し、コマンドラインにパスワードを出さない）:

   ```bash
   kubectl exec -i -n lepinoid deploy/coreprotect-mysql -- \
     sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" coreprotect' \
     < coreprotect.sql
   ```

   - `MYSQL_USER` / `MYSQL_PASSWORD` の環境変数名は実際の Deployment で Secret keyRef として露出しているものに合わせること
10. **restore 検証**: 保存済みベースラインと新 DB を比較（テーブル一覧・row count・trigger・FK の一致）し、代表的な CoreProtect lookup が通ることを確認
11. プラグイン設定の Secret 置換（前節の手順）を実施
12. デバッグ Pod 削除 → build-server 起動
13. 動作確認: ワールド読み込み、CoreProtect lookup/rollback、AutoCommit push、再起動試験

## Phase 5: 切替・廃止

1. **Tailscale LB の疎通を先行ゲート化**: Tailscale Operator が指定 tag を付与できること、VPS から新 hostname の名前解決・TCP 接続ができること、ACL が許可することを確認（HAProxy は起動時に backend 名を解決するため、名前解決前に適用すると backend が down になる）
2. HAProxy 切替（**プレイヤー退出後のメンテナンス時間に実施**、restart で既存セッション切断のため）:
   - `ansible/roles/haproxy_minecraft/defaults/main.yml` の `build.lepinoid.net` backend を `testserver:49966` → `<新 Tailscale hostname>:25565` に変更
   - `ansible-playbook playbooks/vps.yml --check --diff --tags haproxy_minecraft` で確認後、同じ `--tags` で apply
3. クライアントから `build.lepinoid.net` 接続確認
4. **廃止ゲート**: 新環境のバックアップ取得成功（MySQL 日次 job の初回 backup 完了を含む）・restore テスト・AutoCommit push・CoreProtect lookup/rollback・再起動試験をすべて通過した後、testserver LXC を停止。**外部保存物は Proxmox 内部 snapshot ではなく `vzdump` の LXC backup** を取得し、リポジトリ外ストレージへコピー（ファイル名・checksum・復元確認・保持期限を記録）してから `pct destroy 102`
5. `docs/proxmox-9-upgrade.md` 等の CT 102 関連記述を更新

## ロールバック

- **切替前**: データの削除・上書きは一切行わないため、サービスを再起動すればいつでも旧環境に復帰可能（write fence のための Minecraft/MySQL 停止は行うが、データは無変更）
- **切替直後（read-only smoke test 中）**: HAProxy backend を `testserver:49966` に戻し、testserver で `start.sh` を起動すれば即時復帰可能
- **プレイヤー書き込み開始後**: 切替後のワールド変更・Git commit・CoreProtect 履歴は旧環境に存在しない。ロールバックする場合は以下のいずれか:
  - 新側を停止し、ワールドは reverse rsync、DB は新 `coreprotect` DB の dump を**旧側の `mysql` DB に restore**（旧 CoreProtect config は `mysql` DB を指したままのため）。8.4 → 8.1 の論理互換性は事前にリハーサルしておく
  - または明示的に変更損失を受容する
  - 判断基準を事前に関係者と共有しておく
- **新旧の Minecraft/MySQL を同時に writable で起動しない**

## 後続タスク（本 Runbook のスコープ外）

- **LepinoidTools 自動デプロイ基盤**: LepinoidTools リポジトリの CI で jar を OCI イメージ化（`ghcr.io/lepinoid/lepinoid-tools:x.y.z`）し、initContainer で PVC の plugins/ へ配置する仕組み。Multiverse との互換バージョンを jar メタデータに持たせ起動時チェックする案も検討
- **無料プラグインの `PLUGINS` 宣言移行**: 起動確認後、immutable URL/checksum 付きで 1 個ずつ管理対象へ移す
- **time-supporter-bot / health-check-bot の k8s 化**: 要否を別途判断
- **CP 冗長化時の Longhorn 参加再検討**: `controlplane-expansion-runbook.md` 実行時に判断
