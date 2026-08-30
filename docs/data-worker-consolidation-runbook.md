# data ホスト worker 統合 runbook

`data` Proxmox ホスト上の worker-1/2/3（各1 CPU / 4GB）を、worker-1/2 の2ノード（各2 CPU / 6GB）に統合する手順書。
旧 PR #70 の計画を、ブロッカー（nextcloud 250Gi レプリカの退避先不足）を回避する手順に修正したもの。
（Oracle レビュー反映済み）

## 変更前後の構成

| ノード | CPU | RAM | ディスク | 変更 |
|---|---|---|---|---|
| worker-1 | 1 → **2** | 4GB → **6GB** | 100GB → **450GB** | リサイズ（再起動1回） |
| worker-2 | 1 → **2** | 4GB → **6GB** | 350GB（据え置き） | リサイズ（再起動1回） |
| worker-3 | 1 | 4GB | 350GB | **削除** |

- data-pve（LVM-Thin、総容量 852GB）の thin 割当は 800GB → 800GB で増減なし
- Longhorn レプリカ配置先は最終的に **worker-1/2 + toliworker-1/2/3 の5ノード**に限定する（cp-1 / mainworker-1 は除外。mainworker-1 は atm10 等のゲームサーバー優先のため）

## 背景知識（なぜこの順序か）

1. **nextcloud ボリューム（250Gi）のレプリカは直接 evict できない**。toliworker 各ノードは 25% ルール（`storageMinimalAvailablePercentage` デフォルト25%、420GB ディスクなら最低 105Gi 空き維持）により 250Gi の新規レプリカを受け入れられない（PR #70 で実証済み）
2. **LVM-Thin でも実データの一時重複は避ける**。evict は「新コピー完成後に旧コピー削除」のため、250Gi の移動中は実使用量が最大 +250Gi 増える。実空き 200GB を超過し thin pool 溢れのリスクがある
3. よって **「worker-3 側レプリカを先に手動削除（一時1レプリカ運転）→ worker-3 削除 → worker-1 を 450GB に拡張 → レプリカ数を2に戻すと空の worker-1 に再構築」** の順で、重複期間ゼロ・退避先不足なしで移行する
4. レプリカを手動削除しても `numberOfReplicas=2` のままだと **Longhorn が即座に再構築を試みる**。そのため削除前に**全ノードの scheduling を一時凍結**し、削除直後に `numberOfReplicas=1` へ下げる
5. worker-1/2 のディスク拡張・CPU/RAM 変更は in-place で可能（Talos v1.12 は EPHEMERAL を再起動時に自動 grow（デフォルト `grow: true`、本リポジトリで上書きなし）、Longhorn はノードのディスク容量を定期的に自動検知。同一VM・同一ファイルシステムの容量拡張では FS UUID は変わらないため、通常は `diskUUID` 問題（DiskFilesystemChanged）は発生しない）

## 前提条件（作業前に必ず確認）

- [ ] data-pve が LVM-Thin で、実使用量に 200GB 以上の空きがある（`pvesm status` で確認済み: 使用率72%）
- [ ] 全ボリュームが `healthy` である（`kubectl -n longhorn-system get volumes.longhorn.io`）
- [ ] Longhorn の R2 バックアップが直近で成功している（RecurringJob `backup`）
- [ ] 作業中は Flux の app 再デプロイ・terraform の他変更を行わない
- [ ] data ホストの `MemAvailable` / swap / OOM ログを記録（6GB×2 化後の比較用）

---

## Phase 0: 現状記録と nextcloud の保護

### 0-1. 現状記録

```bash
# ノード別の scheduling 状態と容量を記録（後で元に戻す際の基準）
kubectl -n longhorn-system get nodes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,SCHED:.spec.allowScheduling,\
MAX:.status.diskStatus[*].storageMaximum,AVAIL:.status.diskStatus[*].storageAvailable

# ボリューム一覧と状態
kubectl -n longhorn-system get volumes.longhorn.io

# nextcloud ボリュームのレプリカ配置を特定（どちらが worker-3 上か控える）
kubectl -n longhorn-system get replicas.longhorn.io \
  -l longhornvolume=nextcloud-nextcloud -o wide

# Node Drain Policy の確認（未上書きならデフォルト block-if-contains-last-replica のはず）
kubectl -n longhorn-system get settings.longhorn.io node-drain-policy \
  -o jsonpath='{.value}{"\n"}'
```

### 0-2. nextcloud の書き込み停止とオンデマンドバックアップ

RecurringJob は月2回のため、直近バックアップでは最大半月分の変更を失い得る。**一時1レプリカ化の前に必ずオンデマンドバックアップを取る**。

```bash
# nextcloud を maintenance mode にするか scale 0 にして書き込みを停止
kubectl -n nextcloud scale deploy/nextcloud --replicas=0   # 実際のリソース名は要確認

# オンデマンドバックアップ（backup CR 作成）
kubectl -n longhorn-system create -f - <<'EOF'
apiVersion: longhorn.io/v1beta2
kind: Backup
metadata:
  generateName: nextcloud-pre-consolidation-
  labels:
    backup-volume: nextcloud-nextcloud
spec:
  backupTargetName: default
  snapshotName: ""   # 空なら Longhorn が snapshot を自動作成
  labels:
    purpose: pre-consolidation
EOF

# Completed になるまで待つ
kubectl -n longhorn-system get backups.longhorn.io -w
```

> DB（PostgreSQL 等）が別ボリュームにある場合は、DB とデータボリュームの整合性が取れるよう、書き込み停止後に両方のバックアップを取ること。

## Phase 1: レプリカ配置先の制限

cp-1 と mainworker-1 を Longhorn スケジュール対象から外す（既存レプリカはそのまま。今後の新規・再構築レプリカが5ノードに限定される）。

```bash
kubectl -n longhorn-system patch nodes.longhorn.io cp-1 --type=merge \
  -p '{"spec":{"allowScheduling":false}}'
kubectl -n longhorn-system patch nodes.longhorn.io mainworker-1 --type=merge \
  -p '{"spec":{"allowScheduling":false}}'
```

> mainworker-1 上に既存レプリカがある場合、この時点では強制移動しない（ゲームサーバー停止中等の任意タイミングで `evictionRequested: true` を追加して退避させてもよい）。

## Phase 2: nextcloud の一時1レプリカ化（scheduling freeze 付き）

レプリカを削除しても `numberOfReplicas=2` のままだと即座に再構築が走るため、**全ノードの scheduling を凍結してから削除し、直後に desired count を1に下げる**。freeze 中は他ボリュームの自動修復も止まるため、最小限の時間で完了させる。

```bash
# 2-1. 全ノードで新規 replica scheduling を一時停止
for n in cp-1 mainworker-1 worker-1 worker-2 worker-3 \
         toliworker-1 toliworker-2 toliworker-3; do
  kubectl -n longhorn-system patch nodes.longhorn.io "$n" --type=merge \
    -p '{"spec":{"allowScheduling":false}}'
done

# 2-2. 削除対象のレプリカと、残る側レプリカが healthy/running であることを再確認
kubectl -n longhorn-system get replicas.longhorn.io \
  -l longhornvolume=nextcloud-nextcloud -o wide

# 2-3. worker-3 上のレプリカを削除
kubectl -n longhorn-system delete replicas.longhorn.io <nextcloud-replica-on-worker-3>

# 2-4. 即座に desired count を 1 に下げる（再構築を正式に抑止）
kubectl -n longhorn-system patch volumes.longhorn.io nextcloud-nextcloud \
  --type=merge -p '{"spec":{"numberOfReplicas":1}}'

# 2-5. worker-3 以外の scheduling を元に戻す（cp-1/mainworker-1 は false のまま）
for n in worker-1 worker-2 toliworker-1 toliworker-2 toliworker-3; do
  kubectl -n longhorn-system patch nodes.longhorn.io "$n" --type=merge \
    -p '{"spec":{"allowScheduling":true}}'
done
```

> ⚠️ Phase 4-2 でレプリカ数を2に戻して `healthy` になるまで、nextcloud の書き込み停止（scale 0）を維持すること。

## Phase 3: worker-3 の縮退（eviction → drain の順）

**Longhorn 側の eviction を先に完了させてから drain する**（逆順だと instance-manager 停止・volume detach・Node Drain Policy のブロックが退避と競合する）。

### 3-1. レプリカ退避

```bash
# worker-3 は scheduling 無効のまま、eviction を要求
kubectl -n longhorn-system patch nodes.longhorn.io worker-3 --type=merge \
  -p '{"spec":{"evictionRequested":true}}'
```

### 3-2. 退避完了を確認（scheduledReplica/scheduledBackingImage は名前の map として扱う）

```bash
kubectl -n longhorn-system get nodes.longhorn.io worker-3 -o json | jq '
  .status.diskStatus
  | to_entries[]
  | {
      disk: .key,
      scheduledReplicas: ((.value.scheduledReplica // {}) | keys),
      scheduledBackingImages: ((.value.scheduledBackingImage // {}) | keys),
      storageScheduled: .value.storageScheduled
    }'

# CR 実体でも worker-3 上にレプリカが残っていないことを確認
kubectl -n longhorn-system get replicas.longhorn.io -o json | \
  jq -r '.items[] | select(.spec.nodeID=="worker-3") | .metadata.name'
```

**続行条件**: 全ディスクで両 map が空（可能なら `storageScheduled == 0`）、かつ後者の出力が空。

> stuck した場合: 移動先の空き容量（25% ルール適用後）を確認。toliworker-1 の空きが最も少ない（PR #70 時点で 164Gi）。小さいレプリカの合計がこれを超える場合は worker-1（100GB、レプリカ0）への配置状況も確認する。退避が完了するまで drain に進まないこと。

### 3-3. drain とノード削除

```bash
kubectl cordon worker-3
kubectl drain worker-3 --ignore-daemonsets --delete-emptydir-data --force --grace-period=-1 --timeout=300s
```

> drain が timeout した場合、instance-manager Pod を強制削除せず、残存レプリカ・PDB・`node-drain-policy` Setting を調査すること（3-2 の確認漏れが典型例）。

```bash
kubectl delete node worker-3
kubectl -n longhorn-system delete nodes.longhorn.io worker-3
```

## Phase 4: terraform 適用（2段階）

**worker-1/2 を同じ apply で変更しない**（bpg/proxmox が CPU 更新を並列実行し、両 VM が同時再起動して data ホストのストレージが同時に落ちる可能性がある）。

### 4-1. Apply A: worker-3 削除 + worker-1 のみ変更

`terraform/terraform.tfvars`:

```hcl
worker-1 = {
  host_node    = "data"
  vm_id        = 1010
  ip           = "192.168.10.120"
  cpu          = 2
  ram          = 6144 # 6GB
  disk_size    = 450
  datastore_id = "data-pve"
}
# worker-2 はこの時点では現状維持
# worker-3 のブロックは削除
```

```bash
cd terraform/
tofu plan
# 期待: worker-3 の destroy + worker-1 の in-place update。worker-1 が destroy 対象になっていたら即中断
# （vm_id / キー名 / datastore の誤りを疑う。disk_size 増加・cpu/ram 変更は replacement 要件ではない）
tofu apply
```

worker-1 は CPU 変更により自動で1回再起動され、再起動時に Talos が EPHEMERAL を 450GB まで自動 grow する。

### 4-2. worker-1 の拡張確認と nextcloud 再構築（worker-1 限定）

```bash
# worker-1 のディスクが 450GB を認識し、Ready/Schedulable になるまで待つ（ポーリングで確認）
kubectl -n longhorn-system get nodes.longhorn.io worker-1 -o json | jq '
  .status.diskStatus | to_entries[] | {
    disk: .key,
    storageMaximum: .value.storageMaximum,
    storageAvailable: .value.storageAvailable,
    conditions: [.value.conditions[] | {type, status}]
  }'
```

続行条件: `storageMaximum` が 450GB 相当、conditions が `Ready=True` / `Schedulable=True`、`storageAvailable` が 250Gi レプリカのスケジュール要件（25% ルール）を満たす。

**再構築先はスケジューラ任せでは保証されない**ため、再構築中だけ worker-1 のみを有効化する:

```bash
# worker-1 以外を一時的に scheduling 停止（cp-1/mainworker-1/worker-3 は既に false）
for n in worker-2 toliworker-1 toliworker-2 toliworker-3; do
  kubectl -n longhorn-system patch nodes.longhorn.io "$n" --type=merge \
    -p '{"spec":{"allowScheduling":false}}'
done

# 残存レプリカが worker-1 上にないこと（hard anti-affinity の確認）を再確認してから2に戻す
kubectl -n longhorn-system get replicas.longhorn.io \
  -l longhornvolume=nextcloud-nextcloud -o wide
kubectl -n longhorn-system patch volumes.longhorn.io nextcloud-nextcloud \
  --type=merge -p '{"spec":{"numberOfReplicas":2}}'

# worker-1 上の新レプリカが running、ボリュームが healthy になるまで待つ
kubectl -n longhorn-system get volumes.longhorn.io nextcloud-nextcloud -w
```

healthy を確認したら:

```bash
# scheduling を元に戻す（cp-1/mainworker-1 は false のまま維持）
for n in worker-2 toliworker-1 toliworker-2 toliworker-3; do
  kubectl -n longhorn-system patch nodes.longhorn.io "$n" --type=merge \
    -p '{"spec":{"allowScheduling":true}}'
done

# nextcloud を復帰
kubectl -n nextcloud scale deploy/nextcloud --replicas=1   # 実際のリソース名・レプリカ数は要確認
```

### 4-3. Apply B: worker-2 の変更

```hcl
worker-2 = {
  host_node    = "data"
  vm_id        = 1011
  ip           = "192.168.10.121"
  cpu          = 2
  ram          = 6144 # 6GB
  disk_size    = 350
  datastore_id = "data-pve"
}
```

```bash
tofu plan   # worker-2 の in-place update のみであることを確認
tofu apply
```

worker-2 が Ready に戻り、全ボリュームが healthy であることを確認。

## Phase 5: 全体確認

```bash
kubectl -n longhorn-system get volumes.longhorn.io   # 全て healthy
kubectl get nodes                                     # worker-1/2 が Ready、worker-3 不在
kubectl -n longhorn-system get nodes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,SCHED:.spec.allowScheduling
# → cp-1/mainworker-1: false、他: true であること
flux get kustomizations                               # GitOps 正常
```

data ホスト側でも `MemAvailable` / swap / OOM ログを Phase 0 の記録と比較する。

## 後続タスク（別 PR で検討）

- README.md のノード構成表の更新
- `guaranteedInstanceManagerCPU` の見直し（5 → 10 検討。2コアで 5% = 約100m。instance-manager の CPU 不足が実測で見られた場合のみ変更する。移行成立には不要なので本 runbook には含めない）
- mainworker-1 上の既存レプリカの退避（ゲームサーバー停止中などの任意タイミングで `evictionRequested: true`）
- cp-1/mainworker-1 の `allowScheduling: false` を Flux 管理の Node CR マニフェストとして固定化するかの検討（現状は runtime 変更のみ。Longhorn ノード CR はランタイム生成のため、GitOps 化する場合は別 Kustomization で管理）

---

## ロールバック

| フェーズ | 戻し方 |
|---|---|
| Phase 1 後 | `allowScheduling: true` に戻すだけ。実害なし |
| Phase 2 後（1レプリカ化済み） | `numberOfReplicas: 2` に patch すれば再構築される（worker-3 削除前なら元の構成で復旧可能） |
| Phase 3 後（worker-3 ノード削除済み） | tfvars に worker-3 を復活させて `tofu apply` で新規作成し、クラスタ再参加（ディスクは空の状態に戻る） |
| Phase 4 後（VM 削除・変更済み） | 同様に worker-3 を再作成可能。nextcloud データの最終手段は **R2 バックアップからの復元**（ただし DB ボリュームとの整合性が必要。Phase 0-2 のオンデマンドバックアップから復元する） |
| Phase 5 後 | 設定値を戻すのみ |

## 注意事項

- **`tofu plan` で worker-1/2 が destroy 対象になっていたら即中断**（in-place update であるべき）
- thin pool の実使用率が 80% を超えたら作業を中断し、使用量を確認する（LVM-Thin の溢れは全 VM の I/O エラーに直結）
- scheduling freeze（Phase 2, 4-2）の間は他ボリュームの自動修復も停止する。最小限の時間で完了させること
- 作業時間の目安: Phase 3 の退避待ちと Phase 4-2 の 250Gi 再構築が最長（レプリカ量・回線次第で数十分〜数時間）
