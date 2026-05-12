# Longhorn × toliworker SSD 運用Runbook

`toliworker-1/2/3` には Longhorn 用に 420GB SSD (Proxmox の `ssd` / `ssd2` / `ssd3` データストア) を `extra_disks` として割り当て、Talos の `machine.disks` 設定で `/dev/sdb` を `/var/lib/longhorn` にマウントしている。

このドキュメントは、ノード再作成や SSD 再初期化時に発生しうる **Longhorn `diskUUID` mismatch** の対処手順と、Terraform 適用後の検証手順をまとめる。

---

## 1. 前提構成

| 項目 | 値 |
|---|---|
| 対象ノード | `toliworker-1`, `toliworker-2`, `toliworker-3` |
| SSD 容量 | 各 420GB |
| Proxmox datastore | `ssd` / `ssd2` / `ssd3` |
| VM SCSI 番号 | `scsi1` (root は `scsi0`) |
| Talos 上のデバイス | `/dev/sdb` |
| マウントポイント | `/var/lib/longhorn` (XFS, Talos が自動フォーマット) |
| Longhorn `defaultDataPath` | `/var/lib/longhorn` |

関連ファイル:

- [terraform/talos.tf](../terraform/talos.tf) — `extra_disks` がある worker のみ条件付きで `machine.disks` patch を適用
- [terraform/vms.tf](../terraform/vms.tf) — `extra_disks` を `scsi1` 以降に attach
- [terraform/terraform.tfvars](../terraform/terraform.tfvars) — `toliworker-{1,2,3}.extra_disks`
- [clusters/main/infrastructure/controllers/longhorn/helmrelease.yaml](../clusters/main/infrastructure/controllers/longhorn/helmrelease.yaml) — `defaultDataPath: /var/lib/longhorn`

> **重要**: 現在の Talos patch は `/dev/sdb` をハードコードしている。`extra_disks` を 2 本以上に増やす場合は patch を一般化する必要がある。

---

## 2. `tofu apply` 後の検証手順

`talos.tf` や `vms.tf` の変更で toliworker の machine config が変わったら、必ず以下を確認する。

### 2.1 Talos のマウント状態

```bash
for n in 192.168.10.123 192.168.10.124 192.168.10.125; do
  echo "=== $n ==="
  talosctl -n $n get mountstatus | grep -E 'sdb|longhorn' || echo "no sdb mount"
done
```

期待される出力例:

```
sdb-1   /var/lib/longhorn  xfs
```

`sdb` のマウントが無い場合 → `/dev/sdb` 自体が存在しないか、Talos の patch が当たっていない。`talosctl get disks` でデバイスを確認し、`talos.tf` の machine config が `toliworker-*` に反映されているか確認する。

### 2.2 Kubernetes ノード状態

```bash
kubectl get nodes -o wide
```

`toliworker-1/2/3` が **Ready** になっていること。`NotReady` の場合は kubelet ログを確認:

```bash
talosctl -n <ip> logs kubelet | tail -50
```

### 2.3 Longhorn ノード/ディスク状態

```bash
kubectl get nodes.longhorn.io -n longhorn-system -o custom-columns=\
NAME:.metadata.name,\
READY:.status.conditions[?(@.type=='Ready')].status,\
SCHED:.spec.allowScheduling,\
PATH:.spec.disks.*.path,\
MAX:.status.diskStatus.*.storageMaximum
```

期待される状態:

| 項目 | 期待値 |
|---|---|
| `READY` | `True` |
| `SCHED` | `true` |
| `MAX` | 約 `450749272064` (≈ 420GiB) |
| `PATH` | `/var/lib/longhorn` |

`MAX` が 60GB 程度 (root fs 容量) になっている場合 → SSD がマウントされていないか、Longhorn が古い `diskUUID` をキャッシュしている。後者なら次節の手順で復旧する。

### 2.4 Volume の health

```bash
kubectl get volumes.longhorn.io -n longhorn-system
```

全 volume が `attached` / `healthy` であること。`degraded` がある場合は replica の再構築を待つ (`replicas.longhorn.io` で進捗確認)。

---

## 3. `diskUUID` mismatch 復旧手順

### 3.1 症状

`kubectl get nodes.longhorn.io <node> -o yaml` で以下のような condition が出る:

```yaml
conditions:
- type: Ready
  status: "False"
  reason: DiskFilesystemChanged
  message: "record diskUUID doesn't match the one on the disk"
```

`storageMaximum: 0` になり、新規 replica がスケジュールされない。

### 3.2 発生条件

- ノードの SSD を再初期化した (filesystem UUID が変わった)
- ノードを再作成し、Longhorn が前世代の `diskUUID` を覚えている
- 一度 root fs を `/var/lib/longhorn` として使っていたノードに後から SSD をマウントした (今回のケース)

### 3.3 復旧手順 (replica と storageScheduled が 0 のノードでのみ実施)

> **CRITICAL**: この手順を実行する前に、対象ノードに replica と scheduled storage が無いことを必ず確認する。
> ```bash
> # replica が残っていないか
> kubectl get replicas.longhorn.io -n longhorn-system -o json \
>   | jq -r '.items[] | select(.spec.nodeID=="<node>") | .metadata.name'
>
> # Longhorn validator は storageScheduled != 0 でも削除を弾く
> kubectl get nodes.longhorn.io <node> -n longhorn-system \
>   -o jsonpath='{.status.diskStatus.*.storageScheduled}{"\n"}'
> ```
> どちらかが 0 件 / 0 byte でない場合は実施しないこと。先に他ノードへ replica/backing image を退避させる。backing image を使っている場合は `kubectl get backingimages.longhorn.io -n longhorn-system -o yaml` で当該ノードへの配置が無いことも確認する。

以下では `<node>` と `<disk-id>` を置き換える。`<disk-id>` は固定値ではなく、`kubectl get nodes.longhorn.io <node> -o jsonpath='{.spec.disks}'` で得られる **実際のキー** を使う (環境により例: `default-disk-080500000000` のような UUID 風文字列になる)。

**Step 1: スケジュール停止 + eviction 要求**

```bash
kubectl patch nodes.longhorn.io <node> -n longhorn-system --type=json -p '[
  {"op":"replace","path":"/spec/disks/<disk-id>/allowScheduling","value":false},
  {"op":"replace","path":"/spec/disks/<disk-id>/evictionRequested","value":true}
]'
```

> Longhorn validator の削除条件は本質的に `allowScheduling=false` かつ `storageScheduled=0` の組合せ。`evictionRequested=true` は scheduled 状態のレプリカを他ノードに逃がす指示で、replica が既に 0 のノードでは必須ではないが、誤って残っていた場合の保険として併用する。Step 2 の `remove` 時に「最終状態として unschedulable + storageScheduled=0」になっていることが重要。

**Step 2: ディスクエントリ削除**

```bash
sleep 8
kubectl patch nodes.longhorn.io <node> -n longhorn-system --type=json -p '[
  {"op":"remove","path":"/spec/disks/<disk-id>"}
]'
```

**Step 3: ディスク再登録**

```bash
sleep 5
kubectl patch nodes.longhorn.io <node> -n longhorn-system --type=json -p '[
  {"op":"add","path":"/spec/disks/<disk-id>","value":{
    "allowScheduling":true,
    "diskDriver":"",
    "diskType":"filesystem",
    "evictionRequested":false,
    "path":"/var/lib/longhorn",
    "storageReserved":0,
    "tags":[]
  }}
]'
```

`syncing and please retry later` が返った場合は 10 秒待って再試行する。

**Step 4: 確認**

```bash
kubectl get nodes.longhorn.io <node> -n longhorn-system -o custom-columns=\
NAME:.metadata.name,\
READY:.status.conditions[?(@.type=='Ready')].status,\
PATH:.spec.disks.*.path,\
MAX:.status.diskStatus.*.storageMaximum
```

`READY=True` かつ `MAX` が 420GB 相当 (≈ `450749272064`) になればOK。

---

## 4. ノード再作成 (`tofu destroy && tofu apply`) 時の注意

1. `extra_disks` で定義した SSD は Proxmox 上で新規ディスクとして作成される (古い VM の disk が残っていれば手動削除が必要)。
2. Talos は初回起動時に `/dev/sdb` を XFS でフォーマットし `/var/lib/longhorn` にマウントする。**自動なので手動操作は不要。**
3. Longhorn は同じ node 名で再登録されるが、`diskUUID` は当然変わる → **`DiskFilesystemChanged` が再発する可能性が高い**。第 3 節の手順で復旧すること。
4. データ消失の影響範囲: `toliworker-*` 上に replica が乗っている volume のみ。`defaultReplicaCount=2` で他ノードに replica が残っていれば自動的に再同期される。

---

## 5. トラブルシュート

### 5.1 `/dev/sdb` がそもそも存在しない

```bash
talosctl -n <ip> get disks
```

`sda` のみで `sdb` が無い場合:

- Proxmox 側で `scsi1` が attach されているか (`qm config <vmid>` で確認)
- `terraform/terraform.tfvars` の対象 worker に `extra_disks` が定義されているか
- `tofu apply` 時に VM が再作成されたか (`extra_disks` の追加は VM 再作成を伴う場合がある)

### 5.2 mount は成功しているが Longhorn が rootfs 側を使っている

`nodes.longhorn.io <node>` の `spec.disks.<id>.path` が `/var/lib/longhorn` であっても、`StorageMaximum` が root fs 容量と一致する場合は Longhorn が古い disk record を使っている。第 3 節の手順で `diskUUID` をリセットする。

### 5.3 全ノードの `Schedulable` を確認

```bash
kubectl get nodes.longhorn.io -n longhorn-system -o custom-columns=NAME:.metadata.name,READY:.status.conditions[?(@.type=='Ready')].status,SCHED:.spec.allowScheduling
```

何らかの理由で `Schedulable=False` のノードがあるとレプリカ配置の余力が減るので、意図的な maintenance でない場合は調査する。

### 5.4 `kubectl patch` が webhook エラーで失敗する

`failed calling webhook "validator.longhorn.io"` / `connection refused` / `syncing and please retry later` などのエラーが出る場合、longhorn-manager または admission webhook が未Readyの可能性が高い。

```bash
kubectl get pods -n longhorn-system -l app=longhorn-manager
kubectl get pods -n longhorn-system -l app=longhorn-admission-webhook
```

Pod が Ready でない場合は rollout 完了を待ってから再試行する。`syncing` の場合は通常 10〜30 秒で解消する。
