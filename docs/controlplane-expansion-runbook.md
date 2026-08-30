# control plane 3台化 runbook (worker → controlplane 変換)

`cp-1` のみの単一 control plane 構成を、cp-1 + data worker + toliworker の3台構成に拡張し、etcd のクオラム冗長化(2/3)を得る手順書。
対象ノードは cp-1 と同じく `allowSchedulingOnControlPlanes: true` のワークロード兼任とする。

[data worker 統合 runbook](data-worker-consolidation-runbook.md) 完了後の**別フェーズ**として実行する。
（統合と本 runbook を同一メンテナンス期間に行う場合は、Flux reconciler 停止コミットは両方が終わるまで維持し、最後に一度だけ revert する）

## 変更前後の構成

| ノード | ホスト | 変更後の役割 |
|---|---|---|
| cp-1 | main | controlplane（据え置き、引き続きワークロード兼任） |
| worker-2 | data | worker → **controlplane**（兼 Longhorn ストレージ） |
| toliworker-2 | toliunit | worker → **controlplane**（兼 Longhorn ストレージ） |

- etcd メンバーは 1 → 3。3台でクオラム2、1台の CP 喪失に耐えられる
- 3ホスト（main / data / toliunit）に CP が1台ずつ散り、障害ドメインが分離される
- 採用しない候補と理由:
  - **worker-1**: 統合 runbook の Phase 4-2 で nextcloud レプリカの再構築先になる。CP 変換の再起動と時期をずらすため採用しない
  - **toliworker-1**: 統合 runbook 時点で nextcloud 唯一のレプリカが載っている。変換のリスクを避ける
  - **toliworker-3**: toliworker-2 で問題なければ採用不要（余裕が必要なら代替候補）

## 背景知識（なぜこの手順か）

1. **Talos の worker→controlplane 変換は machine config の適用だけで済む**。CP 用 machine config を適用すると、ノードは再起動後に既存クラスタの etcd に自動 join する（追加の join 操作は不要。`talosctl bootstrap` は新規クラスタ作成専用であり、本 runbook では絶対に実行しない）
2. **PVC が壊れない理由**: 変換は config 適用 + 再起動のみ。ノード名・machine ID・ディスク構成は一切変わらず、Longhorn の Node CR / ディスク / レプリカはそのまま残る。Kubernetes の Node オブジェクト名も不変のため、PVC の紐付けは維持される
3. **talos.tf の罠（Phase 1 で対処）**: `data.talos_machine_configuration.worker` には toliworker 用の extra SSD マウント patch（`/var/lib/longhorn`）があるが、controlplane 側には存在しない。そのまま `control_planes` マップに移すと toliworker 系 CP の machine config に sdb マウントが含まれず、再起動後に Longhorn ディスクが消失する。**Phase 1 で controlplane 側にも同一の条件 patch を追加する**
4. **最小差分で cp-1 と既存 worker に触れない設計**: extra_disks patch は `length(each.value.extra_disks) > 0` の場合のみ生成される条件 patch のため、cp-1（extra_disks なし）には影響しない。Phase 1 適用後の `tofu plan` で差分が出なければ正しい。**ZswapConfig は worker 専用 patch のまま残す**。common 化すると cp-1 の machine config hash が変わり apply 時に cp-1 も再起動してしまう（単一 CP 構成での cp-1 再起動はクラスタ API の一時停止を意味する）ため、あえて移動しない。CP 変換後のノードは zswap なしで動かし、必要なら後日対応する
5. **2台を同時に `control_planes` へ移してはいけない（最重要）**: `talos_machine_configuration_apply.controlplane["worker-2"]` と `["toliworker-2"]` は互いに依存しないため、同じ apply で**並行適用**される。1台ずつの段階的な 1→2→3 遷移にならず、かつ2台目は cordon/drain されないまま再起動する。**tfvars の移動は1台ずつ行い、各変換の検証を完了してから次へ進む**
6. **`talos_cluster_health` の評価タイミング**: この data source は `talos_machine_bootstrap` 経由で全 CP apply に依存するため、変更がある場合は plan 時ではなく **apply フェーズで評価**される。config 適用直後のノード再起動と競合して失敗し得るため、**各変換は `-target` apply で health check を経由せず適用し、最後に apply（no target）で回収する**構成にする
7. **`talos_machine_configuration_apply` の destroy は実機に影響しないが順序は保証されない**: worker 側リソースの destroy は state 除去のみ（ノードの設定を戻さない、ディスクは消さない）。ただし destroy と controlplane 側 create の実行順序に保証はなく、実機への影響を与えるのは create 側の config 適用である
8. **「必ず1回の再起動」とは断定できない**: apply リソースは `apply_mode` 未指定（default `auto`）。worker→controlplane では machine type 変更に加え etcd/control-plane サービスの有効化など大きな差分が伴うため再起動は実質確実だが、auto の挙動は差分次第。再起動が発生したかどうかは Talos API の一時断絡と etcd join の確認でもって検証する
9. **`talos_machine_bootstrap` の罠**: `talos_machine_bootstrap.this` の node は `values(var.control_planes)[0].ip` で決まる。Terraform の map はキー名順にソートされ、現行・変更後とも `cp-1` が先頭であり問題ない。将来 CP を増やす際は、キー名が `cp-1` より辞書順で前になる名前（例: `backup-cp`）を付けないこと
10. **公式の CP 降格手順は `talosctl reset`（ディスク消去付き）**: CP→worker の公式手順は reset ベースでディスクが消去されてしまう。本 runbook の対象ノードは Longhorn データを保持したまま worker に戻したいため、ロールバックは `talosctl etcd leave` による正常離脱 + worker config 適用という非公式経路を使う（「ロールバック」節参照。必ず etcd snapshot の復元可能性を確保してから行う）

## 前提条件（作業前に必ず確認。すべて満たさなければ開始しない）

- [ ] data worker 統合 runbook が完了し、**全ボリュームが healthy（degraded なし）**であること。特に nextcloud が **2個以上の healthy 実レプリカ**を持つこと
  （統合 runbook 時点の nextcloud は toliworker-1 上の単レプリカで degraded のため、その状態では絶対に開始しない）
- [ ] 変換対象ノード上に**単レプリカのボリュームがない**こと（Phase 0-2 でボリューム単位の実レプリカ配置を確認する。spec の numberOfReplicas ではなく**実レプリカの nodeID と状態**で判定する）
- [ ] etcd snapshot を取得済み（Phase 0-1）
- [ ] 作業中は Flux の app 再デプロイ・terraform の他変更を行わない（統合 runbook と同一メンテナンス期間なら reconciler 停止コミットが既に効いている）
- [ ] data ホストのメモリに余裕があること（統合後の worker-1/2 = 6GB×2 = 12GB / ホスト 15Gi）。worker-2 は 6GB のまま CP 化する（「注意事項」を読むこと）

---

## Phase 0: 準備

### 0-1. etcd snapshot と現状記録

```bash
# etcd snapshot（cp-1 で取得。単一ノード指定、引数は出力ファイル名）
talosctl -n 192.168.10.110 etcd snapshot etcd-pre-cp-expansion.backup
# 期待出力: etcd snapshot saved to "etcd-pre-cp-expansion.backup" (...) / snapshot info: ...

# 現状の etcd メンバー（1台であること）と健全性
talosctl -n 192.168.10.110 etcd members
talosctl -n 192.168.10.110 etcd status
talosctl -n 192.168.10.110 etcd alarm list   # アラームなしであること

# ノード役割と Longhorn scheduling 状態の記録（Phase 5 の比較基準）
kubectl get nodes -L kubernetes.io/role
kubectl -n longhorn-system get nodes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,SCHED:.spec.allowScheduling'
```

snapshot は取得だけでなく復元可能性まで確保する。Talos v1.12 の etcd disaster recovery 手順は公式ドキュメント
`https://docs.siderolabs.com/talos/v1.12/build-and-extend-talos/cluster-operations-and-maintenance/disaster-recovery`
を参照。snapshot ファイルはリポジトリ外の安全な場所に保管する。

### 0-2. 変換対象ノード上の実レプリカ配置確認（必須ゲート）

ボリューム単位で「変換対象を落としたときに healthy レプリカが残るか」を確認する。
集計値ではなく **1ボリュームずつ**以下を検査する:

```bash
# 全レプリカの ボリューム×ノード×状態 一覧
kubectl -n longhorn-system get replicas.longhorn.io -o json | jq -r '
  .items[] | "\(.metadata.labels.longhornvolume)\t\(.spec.nodeID)\t\(.status.currentState)"' | sort

# 判定基準:
# - 各ボリュームについて、worker-2 上のレプリカを除いても
#   「running かつ healthy なレプリカ」が最低1個（重要データは2個）残ること
# - Phase 2 と Phase 4 が同一日に行われるなら、worker-2 と toliworker-2 の
#   両方を除いた配置でも同条件を満たすこと（あるボリュームの実レプリカが
#   両ノードに1個ずつ配置されていた場合、2台同時変換で全レプリカを失うため）
```

単レプリカボリュームが対象ノード上にあれば、そのボリュームのレプリカを先に他ノードへ移動（もしくは変換を延期）すること。

### 0-3. 対象アプリの書き込み停止（必要な場合のみ）

変換はノード再起動を伴い、その間 RWO ボリュームは detach/attach される。
nextcloud のように書き込み中の切り離しを嫌うアプリが変換対象ノード上で動いている場合のみ、該当 deploy を scale 0 にする。
対象ノード上が Longhorn レプリカとシステム Pod 中心なら省略してよい。

## Phase 1: talos.tf 改修（controlplane 側へ extra_disks マウント patch を追加）

`terraform/talos.tf` の `data.talos_machine_configuration.controlplane` の `config_patches` に、worker 側と同じ条件 patch を追加する。

```hcl
data "talos_machine_configuration" "controlplane" {
  for_each = var.control_planes
  # ... 既存フィールドは据え置き ...
  config_patches = concat(local.common_patches, [
    # ... 既存の cluster/network patch ...
    # extra_disks がある CP のみ：sdb を /var/lib/longhorn にマウント（worker 側と同一の条件 patch）
    length(each.value.extra_disks) > 0 ? [
      yamlencode({
        machine = {
          disks = [
            {
              device = "/dev/sdb"
              partitions = [
                {
                  mountpoint = "/var/lib/longhorn"
                }
              ]
            }
          ]
        }
      })
    ] : []
  ])
}
```

```bash
cd terraform/
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" tofu plan
# 期待: No changes（cp-1 は extra_disks を持たないため生成 config に差分が出ない）。
# 差分が出ていたら条件 patch の追加位置を疑うこと
```

差分がなければ PR を作成して main にマージする（またはローカルで次 Phase に進む）。

## Phase 2: 1台目の変換（worker-2）

**worker-2 のみ**を `control_planes` へ移動する。toliworker-2 はこの時点ではまだ `workers` に残す。

### 2-1. tfvars で worker-2 を control_planes へ移動

`terraform/terraform.tfvars`:

```hcl
control_planes = {
  cp-1 = { ... 据え置き ... }
  worker-2 = {
    host_node    = "data"
    vm_id        = 1011
    ip           = "192.168.10.121"
    cpu          = 2
    ram          = 6144 # 統合後のスペックを維持
    disk_size    = 350
    datastore_id = "data-pve"
  }
}

workers = {
  worker-1     = { ... 据え置き ... }
  worker-3     = { ... 統合 runbook 完了後は削除済み ... }
  toliworker-1 = { ... 据え置き ... }
  toliworker-2 = { ... 据え置き（この時点では workers に残す）... }
  toliworker-3 = { ... 据え置き ... }
  mainworker-1 = { ... 据え置き ... }
}
```

### 2-2. plan で差分を検証

```bash
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" tofu plan
# 期待される差分:
#   ~ proxmox_virtual_environment_vm.talos_node["worker-2"]: tags のみ（worker→controlplane。in-place）
#   - talos_machine_configuration_apply.worker["worker-2"]: destroy（実機への影響なし）
#   + talos_machine_configuration_apply.controlplane["worker-2"]: create
#   cp-1 / toliworker-2 / 他ノードの差分は出ないこと
# plan は通るはず（talos_cluster_health は apply フェーズまで評価が遅延される）。
# plan で即エラーになった場合は手順のどこかが間違っている。中断して原因を調べること
```

**VM が destroy/recreate 対象になっていたら即中断**（vm_id / キー名の誤りを疑う）。

### 2-3. cordon + drain

```bash
kubectl cordon worker-2
kubectl drain worker-2 --ignore-daemonsets --delete-emptydir-data --force --grace-period=-1 --timeout=300s
# block-if-contains-last-replica で drain がブロックされた場合、対象ノードに
# そのボリュームの最終レプリカが残っている。0-2 の確認漏れなので復旧してから続行する

# drain 後の退避完了を確認（通常 Pod が残っていないこと）
kubectl get pods -A -o wide --field-selector spec.nodeName=worker-2
```

### 2-4. config 適用（-target で health check を経由しない）

```bash
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" \
  tofu apply -target=talos_machine_configuration_apply
# talos_machine_configuration_apply リソース群のみを適用し、
# talos_cluster_health / kubeconfig の評価をこの時点で通さない。
# （no-target apply だと config 適用直後の再起動タイミングで health check が落ちることがある）
```

### 2-5. 変換完了の検証（すべて満たしてから次へ）

```bash
# Talos API / Node の復帰を待つ
kubectl wait --for=condition=Ready node/worker-2 --timeout=600s

# CP static pods（kube-apiserver / etcd / scheduler / controller-manager）が起動していること
kubectl -n kube-system get pods -o wide --field-selector spec.nodeName=worker-2 | grep -E "apiserver|etcd|scheduler|controller"

# etcd メンバーが2台になったこと（この時点では2台構成。速やかに Phase 3 へ）
talosctl -n 192.168.10.110 etcd members

# 2台構成の安定を確認（この確認を飛ばさないこと）
talosctl -n 192.168.10.110,192.168.10.121 etcd status   # 両ノードの RAFT 状態が正常
talosctl -n 192.168.10.110 etcd alarm list              # アラームなし
talosctl -n 192.168.10.121 logs etcd 2>&1 | tail -20    # leader election 連発・継続的 timeout がないこと
kubectl uncordon worker-2
```

2台構成（クオラム2/2）はどちらか1台の喪失で etcd が停止する状態であり、**次の変換までの滞留は最小限にする**。

## Phase 3: 1台目変換後の Longhorn 健全性確認（ゲート）

worker-2 上のレプリカは再起動中 degraded になっていたはずなので、全ボリュームが healthy に戻るのを確認する。

```bash
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,ROBUSTNESS:.status.robustness,STATE:.status.state'
# 全ボリューム healthy（degraded / unknown なし）であること
```

**全ボリュームが healthy に戻るまでは Phase 4 に進まない。**
degraded が残る場合はレプリカ再構築が進行中か失敗している。重要ボリュームが関わる場合は復旧を優先する。
2台構成を長期間維持するリスクと天秤にかけ、Phase 4 を別日に延期するか、このまま即座に Phase 4 を完了させるかを判断する（可能なら一気通貫で終わらせるのが望ましい）。

## Phase 4: 2台目の変換（toliworker-2）

**Phase 2 と Phase 3 の検証がすべて完了したら、速やかに続行する。**

### 4-1. tfvars で toliworker-2 を control_planes へ移動

```hcl
control_planes = {
  cp-1 = { ... 据え置き ... }
  worker-2 = { ... Phase 2 で移動済み ... }
  toliworker-2 = {
    host_node    = "toliunit"
    vm_id        = 1014
    ip           = "192.168.10.124"
    cpu          = 22
    ram          = 20480
    disk_size    = 60
    datastore_id = "ssd0"
    extra_disks = [
      { datastore_id = "ssd2", size = 420 },
    ]
  }
}

# workers から toliworker-2 のブロックは削除
```

### 4-2. plan → cordon/drain → -target apply

Phase 2-2 〜 2-4 と同一の手順を toliworker-2 で実施する（差分の期待値も同様。worker-2 / cp-1 関連の差分は出ないこと）。

```bash
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" tofu plan   # 差分検証
kubectl cordon toliworker-2
kubectl drain toliworker-2 --ignore-daemonsets --delete-emptydir-data --force --grace-period=-1 --timeout=300s
kubectl get pods -A -o wide --field-selector spec.nodeName=toliworker-2   # 退避完了確認
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" \
  tofu apply -target=talos_machine_configuration_apply
```

### 4-3. 変換完了の検証

```bash
kubectl wait --for=condition=Ready node/toliworker-2 --timeout=600s
kubectl -n kube-system get pods -o wide --field-selector spec.nodeName=toliworker-2 | grep -E "apiserver|etcd|scheduler|controller"

# etcd メンバーが3台になったこと（クオラム2成立）
talosctl -n 192.168.10.110 etcd members
talosctl -n 192.168.10.110,192.168.10.121,192.168.10.124 etcd status
talosctl -n 192.168.10.110 etcd alarm list

kubectl uncordon toliworker-2
```

### 4-4. Longhorn 復旧確認と残差分の回収

```bash
# Phase 3 と同じく全ボリューム healthy に戻ること
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,ROBUSTNESS:.status.robustness'

# -target apply を使った分、VM tags / talos_cluster_health / kubeconfig 周りの
# 差分が残っているはずなので、最後に no-target apply で回収する
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" tofu plan
# VM tags（2台分）と data source 回収のみであることを確認してから
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" tofu apply
# この apply で talos_cluster_health が評価される。3台構成が安定していれば通る
```

## Phase 5: 全体確認

```bash
kubectl get nodes   # worker-2 / toliworker-2 が control-plane ロールで Ready
talosctl -n 192.168.10.110 etcd members                   # 3メンバー
talosctl -n 192.168.10.110,192.168.10.121,192.168.10.124 etcd status
talosctl -n 192.168.10.110 etcd alarm list                # アラームなし

# etcd ログに leader election の連発・継続的な timeout がないか目視
talosctl -n 192.168.10.110 logs etcd 2>&1 | tail -30
talosctl -n 192.168.10.121 logs etcd 2>&1 | tail -30
talosctl -n 192.168.10.124 logs etcd 2>&1 | tail -30

# Longhorn: 全ボリューム healthy、全ノードのディスク認識
kubectl -n longhorn-system get volumes.longhorn.io
kubectl -n longhorn-system get nodes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,SCHED:.spec.allowScheduling'
# scheduling 状態は Phase 0-1 で記録した値と一致していること
# （cp-1/mainworker-1 の Longhorn 配置制限は統合 runbook の方針に従う。
#   本 runbook は Longhorn scheduling を変更しない）
```

- worker-2 / toliworker-2 上の Longhorn ディスクが認識され、レプリカが復帰していること（ディスク消失があれば Phase 1 の patch 適用漏れを疑う）
- kubeconfig / talosconfig の API エンドポイントは cp-1 固定（`cluster_endpoint = 192.168.10.110`）のまま動作していること

## 後続タスク（別 PR で検討）

- 変換後 CP の RAM 監視。worker-2 は 6GB で etcd+apiserver（1.5〜2.5GB）+ Longhorn + ワークロードを運用する。`kubectl top node` / Proxmox のメモリ使用率で1週間様子を見て、逼迫するなら data ホストの物理メモリ増設か配置見直し
- CP への ZswapConfig 追加（common 化する場合は cp-1 も再起動する計画を立てる）
- kube-apiserver エンドポイントの冗長化（cp-1 固定のまま運用してよいが、3台化の旨味を活かすなら将来的に複数エンドポイント化や VIP 化）
- `data.talos_cluster_health` が apply フェーズでしか評価されない構造の見直し（apply 中の再起動と競合し得るため）

## ロールバック

**前提知識**: 公式の CP 降格手順は `talosctl reset`（ディスク消去付き）であり、これを使うと Longhorn データも消える。Longhorn データを保持したまま worker に戻すには、以下の「正常離脱 + config 適用」という非公式の経路を使う。この経路は公式にテストされていないため、必ず Phase 0-1 の etcd snapshot の復元可能性を確保した上で行う。

| フェーズ | 戻し方 |
|---|---|
| Phase 1, 2-1 後（未 apply） | コミットを revert するだけ。実害なし |
| Phase 2-5 後（worker-2 が CP 化済み、2台構成） | **滞留させない**。Phase 4 を完了させるか、直ちに1台構成へ後退する（下記「2台構成からの後退」参照） |
| Phase 4 後（3台構成） | いずれか1台を workers へ戻す（下記「CP の降格」参照）。3→2→1 と順に減らす際、2台構成の期間を最短にする |
| 変換中に etcd が壊れた場合 | 公式 DR 手順（`https://docs.siderolabs.com/talos/v1.12/build-and-extend-talos/cluster-operations-and-maintenance/disaster-recovery`）に従い、Phase 0-1 の snapshot から復元。**2台・3台構成では cp-1 が生きていても単独ではクオラムを満たせない**（membership がコミットされた後は過半数の生存メンバーが必要） |

### 2台構成からの後退（worker-2 のみ CP 化した状態をやめる場合）

```bash
# 1. 対象ノードの etcd から正常離脱（graceful）
talosctl -n 192.168.10.121 etcd leave

# 2. メンバー数が1に戻ったことを確認（cp-1 側でクオラム1を取り戻す）
talosctl -n 192.168.10.110 etcd members
talosctl -n 192.168.10.110 etcd status

# 3. tfvars を revert（worker-2 を workers へ戻す）し、CP config を worker config で上書き
PROXMOX_VE_ENDPOINT="https://192.168.10.40:8006" \
  tofu apply -target=talos_machine_configuration_apply
kubectl wait --for=condition=Ready node/worker-2 --timeout=600s

# 4. ノード上の control-plane ラベルを除去（残っていれば）
kubectl label node worker-2 node-role.kubernetes.io/control-plane-
```

### CP の降格（3台構成から1台を外す場合）

上記「2台構成からの後退」と同じ手順で、外すノードで `etcd leave` → メンバー確認 → worker config 適用の順に行う。**必ず leave → メンバー数確認 → config 適用の順**とし、逆順（config 適用を先に、remove を後から）はクオラムを破壊する可能性がある。

```bash
# 死活不明ノードの除去だけは逆方向：残存クオラムから memberID を指定して除去
# （これは leave ができない場合の緊急手段。正常なノードには使わない）
talosctl -n 192.168.10.110 etcd members     # memberID を特定
talosctl -n 192.168.10.110 etcd remove-member <memberID>
kubectl delete node <node-name>
```

## 注意事項

- **Phase 2-5 と Phase 4 の間で作業を終了しない**（2台構成の滞留はクオラム喪失リスク。Phase 3 で Longhorn 復旧待ちが必要な場合は、2台構成のリスクを受け入れるか Phase 2 自体を別日に延期することを検討する）
- **Phase 1 の talos.tf 改修で ZswapConfig や既存 patch を common 化しない**。cp-1 の machine config hash が変わり、cp-1 が再起動する（単一 CP 構成での cp-1 再起動はクラスタ API の一時停止を意味する）
- worker-2 の RAM は 6GB のままにする。8GB に増やすと data ホストの割当が 14GB/15Gi に迫り、Proxmox ホスト自体のメモリ逼迫リスクが上がる。6GB でまず運用し、データで判断する
- drain 中に `block-if-contains-last-replica` で止まったら、0-2 の「単レプリカなし」確認が漏れている。強制解除せずレプリカを先に移動すること
- 変換対象ノードの再起動中、そのノード上のレプリカを持つボリュームは degraded 表示になるが、残りレプリカで IO は継続される。Phase 3 / 4-4 で全ボリューム healthy に戻ることを必ず確認する
- `talosctl bootstrap` は本 runbook のどの段階でも実行しない（既存クラスタへの新 CP join は自動。bootstrap を実行すると etcd が新規初期化され既存データを壊す）
