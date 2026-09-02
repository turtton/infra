# toliworker メンテナンス停止・復旧ランブック v2

対象ノード: `toliworker-1` / `toliworker-2` / `toliworker-3`
残存ノード: `cp-1`, `mainworker-1`, `worker-1`, `worker-2`
作成日: 2026-09-02（実測値に基づく。v1 は Oracle レビューで「不安全」と判定され全面改訂）

> [!WARNING]
> 実施前に Phase 0 の現状再確認を必ず行うこと（レプリカ配置・PRMARY は日々変わり得る）。

---

## 0. 確定済みの実態（2026-09-02 時点の実測）

### 0.1 物理配置と Longhorn スケジューリング状態

**toliworker-1/2/3 は同一 Proxmox ホスト `toliunit` (192.168.10.101) 上の VM。** 3台の停止 = toliunit ホスト丸ごとの停止である。

| ノード | 物理ホスト | allowScheduling | 稼働レプリカ数 |
|--------|-----------|----------------|----------------|
| toliworker-1 | toliunit | true | 2 |
| toliworker-2 | toliunit | true | **27** |
| toliworker-3 | toliunit | true | **17** |
| worker-1 | data | true | 2 |
| worker-2 | data | false | 2（既存のみ） |
| cp-1 | main | false | 5（既存のみ） |
| mainworker-1 | main | false | 14（既存のみ） |

**事実**:
- toliworker 停止後の新規レプリカ作成先は **worker-1 のみ**。`replica-soft-anti-affinity=false`（=ノード単位 hard anti-affinity）より replica-count=2 のボリュームは**再構築不可能**（2分散先が存在しない）。
- zone ラベル（`topology.kubernetes.io/zone`）は全ノード未設定。`replica-zone-soft-anti-affinity=true` は実質機能していない。

### 0.2 両レプリカが toliunit ホスト上にのみ存在するボリューム（**停止でオンラインレプリカ 0 個**）

| PVC | 所有者 | 影響 |
|-----|--------|------|
| pvc-9073293b | forgejo/forgejo-db-1 | **forgejo 停止**（v1 では継続稼働想定だったが誤り） |
| pvc-172f0096 | forgejo/forgejo-valkey-data | forgejo キャッシュ |
| pvc-5bb2f9db | attic/attic-data | **attic 停止**（同上） |
| pvc-6457648b | monitoring/prometheus-db | **Prometheus 履歴・監視データ収集が停止** |
| pvc-14b3e698 | monitoring/alertmanager-db | Alertmanager 停止 |
| pvc-eb7f9b06 | lepinoid/lepinoid-db-data | lepinoid DB |
| pvc-de7479a2 | lepinoid/livesync-bridge-data | |
| pvc-9a785b8f | obsidian-livesync/livesync-bridge-data | |
| pvc-26d01919 | hermes/data-hermes-home-0 | hermes（停止対象） |
| pvc-03b34eba | fluxer/nats-data | fluxer（停止対象） |
| pvc-6ee54772 | fluxer/meilisearch-data | fluxer（停止対象） |
| pvc-e41f6f4d | fluxer/fluxer-db-1 | fluxer DB（停止対象） |
| pvc-2ac592a4 | iceshrimp/iceshrimp-db-2 | iceshrimp DB replica（停止対象） |

これらは**現在 already 単一物理ホスト（toliunit）に全コピーが集中している**。つまり toliunit が今日落ちても同じ事態になる。本メンテはそのリスクを可視化したとも言える。

### 0.3 その他の重要ボリューム

| ボリューム | 状態 | 備考 |
|-----------|------|------|
| iceshrimp-db-0815 | **replicas=1（toliworker-3 のみ）** | 本日再作成の iceshrimp DB ボリューム。**1レプリカのみで toliunit 上**。停止中にディスク障害が起きれば完全喪失。最優先でレプリカ増設 or 確実なバックアップが必要 |
| pvc-3e2ea4e1 / pvc-4a65234c | replicas=1（worker-2） | storageclass-single 系。既知の単一レプリカ |
| pvc-75746f61 | cp-1+mainworker-1 | 同一物理ホスト `main` 上に2レプリカ集中の例 |
| atm10-data | replicas=2（mainworker-1+toliworker-2） | 稼働維持対象。drain 後は degraded（稼働レプリカ mainworker-1 のみ）。**想定内**。VM 復帰で自動復帰 |

### 0.4 CNPG 実態（再確認済み・operator 1.30.0）

| クラスタ | instances | PRIMARY | 配置 |
|---------|-----------|---------|------|
| attic/attic-db | 1 | attic-db-1 | worker-2（Pod）/ データは toliunit |
| fluxer/fluxer-db | 1 | fluxer-db-1 | toliworker-3 |
| forgejo/forgejo-db | 1 | forgejo-db-1 | cp-1（Pod）/ データは toliunit |
| iceshrimp/iceshrimp-db | **2** | **iceshrimp-db-2** | 本日再作成済み（v1の「1インスタンス」前提は誤りだった） |
| nextcloud/nextcloud-db | 1 | nextcloud-db-1 | cp-1 |
| woodpecker/woodpecker-db | 1 | woodpecker-db-1 | toliworker-1 |

### 0.5 ユーザー懸念への回答（Longhorn 自動復旧とデータ破損）

- **Longhorn は全レプリカ消失時にデータを「破壊」する動作はしない。** ボリュームは Faulted/detached となり、データは停止 VM のディスク上に残る。VM 復帰でレプリカが戻り自動で healthy に復帰する。
- 実害は2点:
  1. **forgejo / attic / monitoring / lepinoid が「継続稼働」できない**（Pod は cp-1 等に居てもデータが toliunit 上のため）
  2. 停止中、0.2 のデータの**オンライン生存コピーがゼロ**になる。toliunit 側で障害が重なれば実損失。
- `node-drain-policy=block-if-contains-last-replica` は `instance-manager` Pod の eviction をブロックする仕組みのため、`--disable-eviction` で drain すると**この保護ごと無効化される**（v1 の不備）。

### 0.6 計画の二者択一

| | Option A: 全体 graceful 停止 | Option B: 保持サービス退避 |
|---|---|---|
| 停止中に動くもの | k8s基盤/Flux/Longhorn/Cilium のみ | ＋ forgejo, attic, monitoring |
| 事前作業 | なし（ゲートのみ） | toliunit 上レプリカを worker-1/2 へ手動退避（再構築待ちあり、数GB〜数十GB） |
| リスク | 停止中の障害で 0.2 のデータ損失可能性は残る | 同左＋退避作業ミスのリスク |
| 推奨 | **短期停止ならこちら** | 長期停止 or forgejo/監視を止めたくない場合 |

---

## Phase 0: 実施前ゲート（共通・必須。全項目 OK でない場合は中止）

```bash
# G1. iceshrimp-db-0815 の単一レプリカ問題を解消（最優先）
kubectl -n longhorn-system get volume iceshrimp-db-0815 -o jsonpath='{.spec.numberOfReplicas}'
# 対処例A: 一時的に replicas=2 にして worker-1 へ再構築 → healthy 化
# 対処例B: CNPG オンデマンドバックアップ + pg_dump を取得
# → どちらか完了まで次へ進まない

# G2. 本日時点の配置を再確認（0.2/0.4 の表と食い違う場合は計画を修正してから実施）
kubectl -n longhorn-system get replicas.longhorn.io -o json | \
  jq -r '.items[] | select(.spec.failedAt=="") | "\(.spec.volumeName) \(.spec.nodeID)"' | sort
kubectl get clusters.postgresql.cnpg.io -A

# G3. DB の復元可能性確認
#   - ObjectStorage バックアップ設定済みクラスタ（forgejo/woodpecker）: 直近 Completed backup の存在
kubectl get backups.postgresql.cnpg.io -A
#   - 未設定クラスタ（fluxer/iceshrimp/nextcloud）: hibernate 前に pg_dump または Longhorn スナップショット取得を検討

# G4. drain 対象上に unmanaged Pod（コントローラ非管理）がいないこと
kubectl get pods -A -o wide --field-selector spec.nodeName=toliworker-1
kubectl get pods -A -o wide --field-selector spec.nodeName=toliworker-2
kubectl get pods -A -o wide --field-selector spec.nodeName=toliworker-3
```

---

## Option A: 全体 graceful 停止（推奨）

### A-1. Flux 停止

```bash
flux suspend kustomization apps -n flux-system
flux suspend kustomization monitoring-config -n flux-system
flux suspend kustomization lepinoid-workloads -n flux-system
flux suspend kustomization yukulab-workloads -n flux-system
flux suspend kustomization tenants -n flux-system
flux suspend kustomization lepinoid-tenant-setup -n flux-system
flux suspend kustomization yukulab-tenant-setup -n flux-system

# HelmRelease も個別に suspend（Kustomization の suspend では HelmRelease の reconcile は止まらない）
flux get helmreleases -A   # 対象を確認後、個別に:
# flux suspend helmrelease -n <namespace> <name>
```

### A-2. 全 CNPG クラスタ hibernate（nextcloud-db・forgejo-db・attic-db も含む）

```bash
for pair in "fluxer fluxer-db" "iceshrimp iceshrimp-db" "woodpecker woodpecker-db" \
            "nextcloud nextcloud-db" "forgejo forgejo-db" "attic attic-db"; do
  set -- $pair
  kubectl annotate cluster -n "$1" "$2" cnpg.io/hibernation=on --overwrite
done

# 完了確認: 全クラスタの instance Pod が 0 になるまで待つ
kubectl get clusters.postgresql.cnpg.io -A     # INSTANCES が 0/<n> へ
kubectl get pods -A | grep -E -- "-db-[0-9]+"  # DB pod が消えるまで待機
```

### A-3. アプリ全停止（forgejo/attic も含む）

```bash
kubectl scale deploy,statefulset --all -n fluxer --replicas=0
kubectl scale deploy,statefulset --all -n woodpecker --replicas=0
kubectl scale deploy,statefulset --all -n hermes --replicas=0
kubectl scale deploy,statefulset --all -n yukulab --replicas=0
kubectl scale deploy,statefulset --all -n lepinoid --replicas=0
kubectl scale deploy,statefulset --all -n nextcloud --replicas=0
kubectl scale deploy,statefulset --all -n iceshrimp --replicas=0
kubectl scale deploy,statefulset --all -n forgejo --replicas=0
kubectl scale deploy,statefulset --all -n attic --replicas=0
# monitoring: Prometheus/Alertmanager のデータは toliunit 上のため実質止まる。
#   残して Faulted ボリュームの CrashLoop を許容するか、scale 0 にするかは実施時判断。
# tailscale ns の ts-* は operator 管理（namespace scale の対象外）。
#   停止対象サービスの proxy は接続先が消えるだけなので放置可。必要なら該当 Service/Ingress を一時無効化。
```

### A-4. drain → VM 停止（**1台ずつ・毎回確認**）

```bash
kubectl cordon toliworker-1 toliworker-2 toliworker-3

# 1台ずつ実行。--disable-eviction は使わないこと（PDB と Longhorn drain 保護を迂回するため禁止）
kubectl drain toliworker-1 --ignore-daemonsets --delete-emptydir-data --timeout=30m
kubectl get pods -A -o wide | grep toliworker-1   # DaemonSet 以外が残っていないこと
kubectl -n longhorn-system get volumes.longhorn.io | grep -i faulted  # 想定外 faulted がないこと
# OK なら toliworker-2 → toliworker-3 と同様に続行

# drain が PDB でブロックされた場合: A-2/A-3 の停止漏れ（残 DB pod / 未 suspend の HelmRelease）を直すこと。
# 絶対に --force / --disable-eviction で突破しない。

# Proxmox 側（1台ずつ。`qm shutdown <vmid1> <vmid2>` は無効な構文なので禁止）
# 対応表（terraform/terraform.tfvars より）:
#   toliworker-1 → VM ID 1013
#   toliworker-2 → VM ID 1014
#   toliworker-3 → VM ID 1015
# 方法A: toliunit (192.168.10.101) に SSH して実行:
#   qm shutdown 1013
#   qm shutdown 1014
#   qm shutdown 1015
#   qm wait 1013; qm wait 1014; qm wait 1015
# 方法B（2026-09-02 実施・ローカルに SSH 鍵がない環境向け）: Proxmox API 経由。
#   PROXMOX_VE_ENDPOINT は main ノード固定だがクラスタ API のため toliunit の VM も操作可。
#   Talos は qemu-guest-agent 搭載のため ACPI graceful shutdown が効く（約50秒で停止）:
#   curl -sk -X POST -H "Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN" \
#     "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/toliunit/qemu/1013/status/shutdown"
#   # status が stopped になるまで polling（下記を VMID ごとに）
#   curl -sk -H "Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN" \
#     "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/toliunit/qemu/1013/status/current"
```

#### A-4a. instance-manager PDB が drain をブロックした場合の対処（2026-09-02 実測）

**発生条件**: 全ボリューム detached（アプリ停止済み）でも、`node-drain-policy=block-if-contains-last-replica` が「stopped レプリカが他ノードに代替を持たない」ケースで PDB をブロックし続ける。

**実例（toliworker-2）**:
- toliunit-only ボリューム（pvc-5bb2f9db 等13本）のレプリカが toliworker-2/3 のみに存在
- cordon 時に instance-manager PDB 削除を試みるが、`block-if-contains-last-replica` が stopped レプリカにも発動
- drain が15分タイムアウトで失敗（イベント: `Failed to evict pod ... pdb blocks eviction`）

**対処手順**:
```bash
# 1. 全ボリュームが detached であることを確認（稼働 I/O ゼロ）
kubectl -n longhorn-system get volumes.longhorn.io
# state=detached が全てであること

# 2. node-drain-policy を一時変更
kubectl -n longhorn-system patch setting node-drain-policy --type=merge -p '{"value":"allow-if-replica-is-stopped"}'

# 3. drain 再実行
kubectl drain toliworker-2 --ignore-daemonsets --delete-emptydir-data --timeout=20m
# instance-manager 含め正常に evict される

# 4. drain 完了後、即座に policy を元に戻す
kubectl -n longhorn-system patch setting node-drain-policy --type=merge -p '{"value":"block-if-contains-last-replica"}'
kubectl -n longhorn-system get setting node-drain-policy -o jsonpath='{.value}'  # block-if-contains-last-replica を確認
```

**注意**:
- この一時変更は「全レプリカ stopped・detached」かつ「停止中 I/O ゼロ」の場合のみ安全
- drain 中は Longhorn の最終レプリカ保護が無効化されるため、**必ず drain 完了後に復元すること**
- toliworker-1/2/3 の drain 全てで同じ手順を繰り返す（1台ずつ、policy 変更→drain→復元）

### A-5. 復旧

```bash
# 1. VM 起動（Proxmox）→ Node Ready 待ち
#   SSH: toliunit で qm start 1013 / 1014 / 1015
#   API（2026-09-02 停止時と同じ経路。1台ずつ起動→Ready 確認→次へ）:
#   curl -sk -X POST -H "Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN" \
#     "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/toliunit/qemu/1013/status/start"
#   # status=running を確認してから Node Ready を待ち、次の VM へ
kubectl get nodes -w

# 2. 1台ずつ uncordon + Longhorn node Ready 確認
kubectl uncordon toliworker-1
kubectl -n longhorn-system get nodes.longhorn.io toliworker-1 \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'   # True を確認
# toliworker-2, -3 も同様に1台ずつ

# 3. ボリューム復帰確認（faulted/degraded が消えるまで待つ。detached hibernated DB は unknown になり得るが正常）
kubectl -n longhorn-system get volumes.longhorn.io

# 4. DB 復帰（hibernation 解除）
for pair in "fluxer fluxer-db" "iceshrimp iceshrimp-db" "woodpecker woodpecker-db" \
            "nextcloud nextcloud-db" "forgejo forgejo-db" "attic attic-db"; do
  set -- $pair
  kubectl annotate cluster -n "$1" "$2" cnpg.io/hibernation-
done
kubectl get clusters.postgresql.cnpg.io -A   # 全 Cluster in healthy state まで待つ

# 5. Flux 復帰（suspend 解除 → HelmRelease も個別に resume）
flux resume kustomization apps -n flux-system --wait
flux resume kustomization monitoring-config -n flux-system --wait
flux resume kustomization lepinoid-workloads -n flux-system --wait
flux resume kustomization yukulab-workloads -n flux-system --wait
flux resume kustomization tenants -n flux-system --wait
flux resume kustomization lepinoid-tenant-setup -n flux-system --wait
flux resume kustomization yukulab-tenant-setup -n flux-system --wait
# flux resume helmrelease -n <namespace> <name> --wait

# 6. 最終確認
kubectl get pods -A | grep -vE "Running|Completed"                        # 空
kubectl -n longhorn-system get volumes.longhorn.io | grep -iE "faulted|degraded"  # 空
```

---

## Option B: 保持サービスのレプリカ事前退避（forgejo/attic/monitoring を動かし続ける）

```bash
# B-0. worker-2 の Longhorn スケジューリングを有効化（2分散先を確保。ディスク残量を確認のこと）
kubectl -n longhorn-system edit node.longhorn.io worker-2   # spec.allowScheduling: true

# B-1. 保持対象ボリュームごとに次を実施（forgejo-db-1 / forgejo-valkey-data / attic-data /
#      prometheus-db / alertmanager-db）:
#   1) replicas 2→3 にして worker-1 or worker-2 に増殖
#   2) robustness=healthy を待つ（データ量に応じて数十分かかり得る）
#   3) toliunit 側のレプリカを削除
#   4) replicas 3→2 に戻す
kubectl -n longhorn-system edit volume <vol>                       # spec.numberOfReplicas: 3
kubectl -n longhorn-system get volume <vol> -w                     # healthy 待ち
kubectl -n longhorn-system get replicas.longhorn.io | grep <vol>   # toliunit 側レプリカ名特定
kubectl -n longhorn-system delete replica <toliunit側レプリカ名>
kubectl -n longhorn-system edit volume <vol>                       # spec.numberOfReplicas: 2
```

- 完了後は Option A の A-1 以降を実施（ただし A-2 では forgejo-db / attic-db を hibernate しない、A-3 では forgejo / attic / monitoring を scale 0 しない）
- lepinoid / obsidian-livesync は停止を受け入れる。

> [!IMPORTANT]
> Option B でも新規配置先は worker-1/worker-2 のみ（両方とも `data` ホスト）。**停止中は保持サービスも単一物理ホスト `data` に依存する**。data ホスト障害時は保持サービスも全滅する点を許容すること。

---

## 停止中の状態（Option A 想定）

| 稼働 | 停止 |
|------|------|
| k8s control-plane, Flux基盤, Longhorn基盤, Cilium, Tailscale operator, Grafana（データ表示なし） | fluxer, woodpecker, iceshrimp, hermes, nextcloud, yukulab/lepinoid 系, **forgejo, attic, Prometheus/Alertmanager（監視）** |

### 停止中の正常な表示（2026-09-02 実測。誤判定しないこと）

- ノード: toliworker-1/2/3 = `NotReady,SchedulingDisabled`
- toliworker 上に残っているように見える Pod:
  - `Pending` の cilium/cilium-envoy/alloy/node-exporter → DaemonSet が NotReady ノードへスケジュールできず Pending のまま。正常
  - `Running` 表示の engine-image/longhorn-manager/loki-canary → VM は stopped 済みだがコントローラが Phase を未更新なだけの表示残留。正常（実プロセスは存在しない）
- Longhorn volumes: `faulted 0`、`degraded 1`（atm10-data のみ・想定内）、`unknown 34`（全て detached の正常状態）
- 制御系（coredns/flux/cnpg-op/cilium-op/tailscale-op/reloader）は cp-1/worker-1 へ退避済みで Running

## 異常時対処

| 症状 | 対処 |
|------|------|
| drain が PDB で止まる | A-2/A-3 の停止漏れを確認。`--force` / `--disable-eviction` で突破しない |
| volume が faulted | `kubectl -n longhorn-system describe volume <name>` で replica の failedAt/ownerID を確認。VM 復帰で自動復帰が正常系 |
| DB 復帰しない | pod log で起動エラー確認。hibernate 解除漏れ・timeline 分岐（promote 履歴）の有無を確認 |
| Flux 復帰しない | Git SHA と `lastAppliedRevision` の一致確認 → `flux reconcile kustomization <name> --wait` |
| longhorn-manager が CrashLoopBackOff（復帰直後） | `kubectl logs <pod> -n longhorn-system --previous` の末尾に `settings.longhorn.io "default-replica-count": the object has been modified` の fatal があれば起動時 race。`kubectl delete pod <pod> -n longhorn-system` で削除すれば次回起動で回避される（今回 toliworker-1/-3 で発生、両方とも通常削除のみで復旧） |
| アプリ Pod が `ContainerCreating` のまま「FailedAttachVolume: node X is not ready」 | k8sノードは Ready でも Longhorn 側 attach 情報が stale の場合がある。対象 volume の `VolumeAttachment` を `kubectl delete volumeattachment <name>` で削除すると CSI が再 attach する（今回 woodpecker で発生、force 不要） |
| HelmRelease が `UpgradeFailed`（DaemonSet timeout）で固まったまま | `status.observedGeneration == metadata.generation` かつ `updatedNumberScheduled == desired` を確認後に `flux reconcile helmrelease <name> -n <ns> --force`。remediation retries 上限到達時は force が必須 |
| helm リリースが `failed` 履歴のみで進まない | まず DS/STS 側を収束させてから `helm history` で deployed 版へ `helm rollback` |

## 残課題（今回のスコープ外だが要検討）

1. **toliunit 単一ホストへのレプリカ集中の恒久的解消** — 0.2 のボリュームはメンテナンス以前に既に単一障害点状態。Proxmox ホスト単位の zone ラベル整備 + Longhorn zone anti-affinity の実効化が必要
2. worker-2/cp-1/mainworker-1 の Longhorn scheduling off の意図確認と容量計画
3. ~~iceshrimp-db-0815 の replicas=1 是正~~ → 解消済み（2026-09-02: ユーザー方針「CNPG 側冗長化のみ」に基づき pvc-2ac592a4 も replicas=1 へ変更。CNPG 2インスタンスが WAL 冗長を担う）
4. CNPG バックアップ未設定クラスタ（fluxer/iceshrimp/nextcloud）への ObjectStorage バックアップ整備
