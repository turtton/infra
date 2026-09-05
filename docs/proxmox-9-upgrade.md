# Proxmox VE 8 → 9 アップグレード計画

main (192.168.10.100) / data (192.168.10.40) の Proxmox VE を 8 から 9 へ in-place アップグレードする手順。

- 公式手順（確認済み）: https://pve.proxmox.com/wiki/Upgrade_from_8_to_9
- 方針: **手動SSHで公式手順を回す**（一度きりの作業のためAnsible化はしない）
- VMダウンタイム: **許容**（Talosクラスタ等は停止してから作業）
- toliunit (192.168.10.101) は今回の対象外。混在バージョンクラスタはアップグレード期間中サポートされる
- 作業用SSH鍵: `.opencode/id_ed25519`（`ssh -F /dev/null -i .opencode/id_ed25519 root@<node>`）
  - ※ この環境ではnix store内のlibvirt ssh_configが壊れているため `-F /dev/null` が必要

## 0. 現環境チェック結果（2026-09-05 実測）

### クラスタ全体

- クラスタ `HomeServers`: main / data / toliunit の **3ノード構成・quorate**。1台ずつ作業すればquorumは維持される
- 両ノードとも **pve-manager 8.4.21** — アップグレード前提条件（8.4.1+）を満たす
- CPU: main = AMD Ryzen 5 3600 / data = Intel i3-3240（2012年製）。**異種CPUのためライブマイグレーションは非推奨**（今回はVM停止前提なので影響なし）。data側は公式の「旧ハードウェア + 6.14カーネル」注意事項に該当するため、起動不可時に備え物理アクセスを確保しておく
- Ceph: パッケージ（quincy 17.2.8）は入っているが **クラスタ未構成・ストレージ未使用**。Ceph Squidへの事前アップグレードは不要。dist-upgradeでcephリポ周りのエラーが出たら該当 `.list` を無効化する
- `pve8to9 --full` 共通FAIL: **カスタムロール `TerraformRole` が廃止予定の `VM.Monitor` 権限を使用**。アップグレード後に `/etc/pve/user.cfg` で `VM.Monitor` → `Sys.Audit`（HMP monitor用途）+ 必要なら `VM.GuestAgent.Audit` に置き換える。このロールは手動作成（terraform/ 管理外）でOpenTofuプロバイダが使用している

### main 固有

| 項目 | 状態 | 対処 |
|---|---|---|
| **root ディスク (btrfs /dev/sdb3)** | ~~111G中108.7G使用・空き1.5G（99%）~~ → **fstrimで空き52Gに解消済み（09-05）** | 原因は CT 102 の rootfs rawイメージの肥大化（実34G/物理72G）。詳細は§1.1 |
| `/etc/sysctl.conf` | `net.ipv4.ip_forward=1` あり | PVE 9では `/etc/sysctl.conf` が読まれない。`/etc/sysctl.d/90-ipforward.conf` へ移行（§1.2） |
| サードパーティリポ | `cloudflared.list`（suite: any） | 現状動作。dist-upgradeで問題が出たら一時無効化 |
| microcode | `amd64-microcode` 未導入（WARN） | 任意。non-free-firmware有効化後 `apt install amd64-microcode` |
| LVM autoactivation | storage `toshibassd` に有効なLVあり（ローカルのみ） | 任意。移行スクリプト `/usr/share/pve-manager/migrations/pve-lvm-disable-autoactivation` |
| ゲスト | VM 1000 `cp-1`, VM 1016 `mainworker-1`, CT 102 `testserver` | §2.1で停止 |

### data 固有

| 項目 | 状態 | 対処 |
|---|---|---|
| root ディスク (LVM) | 94G中12G使用・空き78G | 問題なし |
| **systemd-boot メタパッケージ** | **installed（pve8to9 FAIL）** | 実ブートローダーはGRUB（efibootmgr上 `proxmox` エントリ、`grub-efi-amd64` 導入済み）なので **`apt remove systemd-boot` してよい**（§1.2） |
| EFI + LVM root | GRUB LVMバグの対象構成だが `grub-efi-amd64` 導入済み | 対策済み。再起動後に起動しない場合は公式「Recover From Grub Failure」参照 |
| `/etc/sysctl.conf` | 空 | 対処不要 |
| microcode | `intel-microcode` 未導入（WARN） | 任意 |
| LVM autoactivation | storage `data-pve` に有効なLVあり（ローカルのみ） | 任意。mainと同じ移行スクリプト |
| ゲスト | VM 1010 `worker-1`, VM 1011 `worker-2` | §2.1で停止 |

## 1. 事前準備

### 1.0. 共通

- [ ] **バックアップ確認**: 各VM/CTのvzdumpバックアップが最新であること。k8sデータはLonghorn → R2（`longhorn-backup`バケット）のバックアップが機能していることを確認
- [ ] 各ノードへの物理アクセス or IPMI/iKVM経路を確認（ネットワークが起きない・起動不可の場合に備える）
- [ ] SSHのみの場合は **必ず tmux/screen 内で作業**。Web UIの仮想コンソール経由では実行しない（途中で切断される）
- [ ] NIC名を控える: main `enp34s0` / data `enp4s0`。6.14カーネルでNIC名が変わるとブリッジ設定（`/etc/network/interfaces`）が壊れる

### 1.1. main: ディスク逼迫の解消（✅ 2026-09-05 解消済み）

~~空き1.5Gでは dist-upgrade 不可能~~ → **fstrim で解消済み（空き52G確保）**。

- 原因: CT 102 の rootfs が btrfs 上の **raw イメージ（loop0マウント）** で、CT内部で削除したブロックがホスト側に返却されず、実使用34Gに対しイメージが72Gまで物理肥大化していた
- 対処: `pct exec 102 -- fstrim -av` で 54.7GiB をトリム → root 空き 1.5G → **52G**。データは無事（fstrimはゲストが未使用とマークしたブロックのみ破棄）
- 再発防止: **不要と判断（09-05）**。CT 102 廃止時にイメージごと削除され全解放されるため。それまでに再肥大化した場合は `pct exec 102 -- fstrim -av` を手動実行すればよい
- **1TB SSD増設は引き続き実施予定だが、容量ブロッカーではなくなった**（root 置換・CT移転ともに不要。増設後の用途は別途検討）
- **増設であり移転ではない: 旧SSDは取り外さず残置**（ユーザー確定）
- **root の btrfs replace は行わない**（ユーザー確定 2026-09-05）
- **SSD物理装着にはホストの電源OFFが必要** → 装着タイミングで main 上の全ゲスト（cp-1, mainworker-1, CT 102）が停止する。**SSD到着後、アップグレードと同日にまとめて実施する（ユーザー確定 09-05）** → §2.1 のクラスタ停止手順を冒頭に繰り上げ、装着→起動→アップグレードを一連で行う
- CT 102 はデータ移管後に廃止予定。廃止作業は本アップグレード完了後に別途実施

参考: 現行レイアウト（/dev/sdb, 111.8G、変更しない）= sdb1 BIOS boot(1007K) / sdb2 EFI(512M, UUID 1862-C942) / sdb3 btrfs root(111.3G)。他ディスク: sda = TOSHIBA 256G SSD（toshibassd LVM: cp-1の32G・mainworker-1の120Gがここ）

- 不採用となった案（記録）: ① root を btrfs replace で新SSDへ置換（ブートローダー作業が要り高リスク）② CT 102 rootfs のみ新SSDへ `pct move-volume`（fstrimで解消したため不要に）③ 新SSDへPVE 9クリーンインストール+リストア（工数大）

### 1.2. 各ノードの事前修正

```bash
# main のみ: sysctl設定の移行
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/90-ipforward.conf
sed -i 's/^net.ipv4.ip_forward=1/# moved to sysctl.d/' /etc/sysctl.conf
sysctl --system   # 反映確認

# data のみ: systemd-boot メタパッケージ削除（GRUB環境のため安全）
apt remove systemd-boot   # systemd-boot-efi / systemd-boot-tools は残ってよい

# 両ノード: アップグレード中のauditログ抑制（任意）
systemctl disable --now systemd-journald-audit.socket

# 両ノード: チェック再実行でFAILが消えることを確認
pve8to9 --full
```

## 2. アップグレード（1ノードずつ）

推奨順序: **data → 動作確認 → main**（mainは §1.1 のSSD移転完了が前提。3ノードクラスタなので1台停止中もquorum維持。SSD到着待ちの間にdataを先に終わらせる進め方も可能）

### 2.1. k8sクラスタ・ゲストの停止

#### CP停止時のworker挙動（前提知識）

cp-1 は **単一コントロールプレーン** 構成。CP停止中もworkerのkubeletと既存Podは動き続けるが、API/etcdが死ぬためスケジューリング・Pod再起動・Flux・Longhorn manager 等の制御系は全停止する（「凍結」ではなく「無人運転」）。クラッシュしたPodはCP復帰まで戻らない。短時間なら放置も可能だが、計画メンテでは以下の全体停止を推奨。

#### 停止手順（worker → CP の順）

```bash
# 0. etcdスナップショット取得（単一CPのため必須。ローカルに保存）
talosctl -n 192.168.10.110 etcd snapshot etcd-backup-$(date +%Y%m%d).db

# 1. Longhornボリュームの健全性確認（全ボリューム healthy であること）
kubectl -n longhorn-system get volumes

# 2. worker を順に shutdown（toliworker系 → mainworker-1 → worker-1,2）
talosctl -n 192.168.10.123 shutdown   # toliworker-1
talosctl -n 192.168.10.124 shutdown   # toliworker-2
talosctl -n 192.168.10.125 shutdown   # toliworker-3
talosctl -n 192.168.10.126 shutdown   # mainworker-1
talosctl -n 192.168.10.120 shutdown   # worker-1
talosctl -n 192.168.10.121 shutdown   # worker-2

# 3. 最後にコントロールプレーン
talosctl -n 192.168.10.110 shutdown   # cp-1

# 4. 残りのゲスト（CT 102 testserver）
ssh root@192.168.10.100 pct shutdown 102
```

#### 起動手順（CP → worker の順・逆順厳守）

```bash
# PVEホスト側でVM起動（onboot設定の確認も兼ねる）
qm start 1000   # cp-1 を最初に
# API/etcdの復帰を確認してからworkerを起動
talosctl -n 192.168.10.110 health   # または kubectl get nodes でcp-1 Ready確認
qm start 1016 1010 1011             # mainworker-1, worker-1, worker-2
# toliunit側の toliworker-1〜3 も起動
```

> Flux CD管理のワークロードはTalos VM起動後に自動復帰する。Longhornボリュームの復旧状態は §3 で確認。

### 2.2. リポジトリ切り替え（Debian trixie + PVE 9）

```bash
sed -i 's/bookworm/trixie/g' /etc/apt/sources.list

# no-subscriptionリポジトリをdeb822形式で追加
cat > /etc/apt/sources.list.d/proxmox.sources << EOF
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# 旧PVE8リポジトリ行を削除 or コメントアウト後、確認
apt update && apt policy
```

### 2.3. dist-upgrade

```bash
apt dist-upgrade
```

設定ファイル差分を聞かれた場合の目安:

| ファイル | 推奨 |
|---|---|
| `/etc/issue` | No（現状維持） |
| `/etc/lvm/lvm.conf` | 自分で変更していなければ Yes |
| `/etc/ssh/sshd_config` | 自分で変更していなければメンテナ版（要差分確認） |
| `/etc/default/grub` | カーネル引数を変更していなければ No。**変更している場合は差分を精査** |
| `/etc/chrony/chrony.conf` | 自分で変更していなければ Yes |

### 2.4. 再起動と確認

```bash
pve8to9   # 再チェック
reboot
```

起動後:

```bash
pveversion          # 9.x であること
ip -br link         # NIC名が変わっていないか（変わっていたら /etc/network/interfaces を修正）
systemctl --failed
pvecm status        # クラスタ復帰確認
```

## 3. 事後作業

1. [ ] 両ノード完了後、ゲスト（Talos VM・CT 102）を起動
2. [ ] **`TerraformRole` の権限修正**: `/etc/pve/user.cfg` で `VM.Monitor` を削除し `Sys.Audit`（+ 必要なら `VM.GuestAgent.Audit`）を付与。修正後に `tofu plan` が通ることを確認
3. [ ] Talosクラスタ健全性確認: `talosctl health`、`kubectl get nodes`
4. [ ] Longhornボリュームの復旧確認（R2バックアップからのリストアが必要なボリュームがないか）
5. [ ] Flux CDの同期確認: `flux check`、`flux get kustomizations`
6. [ ] Web UIはキャッシュクリア（Ctrl+Shift+R）
7. [ ] （任意）`apt modernize-sources` でリポジトリ設定をdeb822形式に統一
8. [ ] （任意）LVM autoactivation移行スクリプト `/usr/share/pve-manager/migrations/pve-lvm-disable-autoactivation` の実行
9. [ ] ネットワーク設定に変更が必要だった場合は `ansible/roles/proxmox_network` 側の変数も同期させる

## トラブル時

- **dist-upgradeが途中失敗**: `apt -f install` で修復後、リポジトリ設定を見直して再開
- **`proxmox-ve` を削除しようとする警告が出る**: trixie向けリポジトリ設定が不足。設定を見直す（削除を続行しない）
- **cephリポでエラー**: Ceph未使用のため `/etc/apt/sources.list.d/ceph.list` を無効化して再開
- **再起動後にネットワークが繋がらない**: NIC名変更の可能性大。物理/IPMIコンソールから `ip -br link` で新しいNIC名を確認し `/etc/network/interfaces` を修正
- **data がGRUBで起動しない（UEFI + root on LVM）**: 公式「Recover From Grub Failure」参照
- **LVM Thin Pool repair要求**: `lvconvert --repair pve/data`
- **ロールバック**: in-placeアップグレードの巻き戻しは非現実的。バックアップからの復旧が前提となるため、§1.0のバックアップ確認は必須

## 実行ログ: toliunit 追従更新 (2026-09-05) ✅

- toliunit は既に PVE 9.1.1 だった（今回の8→9対象外はそのため）。**pve系リポジトリが enterprise のみ(無効)で no-subscription が無く、PVEパッケージが更新不能だった** → `proxmox.sources` 追加 → 74パッケージ更新で 9.2.11 へ（ゲスト無停止）
- 新カーネル 7.0.14-15-pve 導入済みだが稼働カーネルは 6.17.2 のまま。**再起動は任意の保守窓口で**（onboot で toliworker 自動復帰）
- `zfs-import@ssd*.service` の failed 表示は過去の起動時レースの残滓（プールは全て ONLINE・healthy）。reset-failed 済み
- 注意: ZFS pool `ssd` が **84% 使用**（残69.5G）。`ssd2` 61%, `ssd3` 63%

## 実行ログ: 再起動後の残違警告の調査・解消 (2026-09-05 夜) ✅

- 症状: cilium(mainworker-1) が Init/CrashLoopBackOff 継続・alloy 4ノード(cp-1/worker-1/mainworker-1/toliworker-3) が readiness timeout 継続。mainworker-1 で IO PSI full avg10=80-90% が4時間以上持続し Talos OOMController が繰り返し SIGKILL
- 調査: Longhorn rebuild は完了済み・IM ログも静か。**主犯は alloy の CPU busy-loop**(内部で無言で回転、CPU時間が kubelet の3倍)。alloy が kubelet 経由のログ取得を大量に発行し、cp-1/mainworker-1 のルートディスク(main ホストの TOSHIBA SSD LVM)で 1600 reads/s の読み取り嵐を引き起こしていた
- 対処: 4台の alloy Pod を削除・再作成(positions は tmpfs のため最新位置から再開)。直後に IO PSI avg10=0、alloy 全7 Pod 2/2 Ready、cilium 安定稼働
- 教訓: **alloy は `--storage.path=/tmp/alloy`(tmpfs)で positions を保持**しており、大規模障害後のログバックログ処理で暴走し得る。readiness が長時間回復しない alloy は再起動で切る

## 実行ログ: main (2026-09-05) ✅ 完了

1. クラスタ全停止（drainあり・--forceなし）→ main シャットダウン → 1TB SSD装着 → 起動
2. 装着後のデバイス名変化に注意: **新SSD=sda / TOSHIBA 256G=sdb / root用PNY 120G=sdc**（sdb→sdcにずれたが fstab/GRUB/LVM は全てUUID/PV UUID指定のため無事起動）
3. sysctl移行（`/etc/sysctl.d/90-ipforward.conf`）→ pve8to9 FAILURES: 0
4. リポジトリ切替（ceph.list.bak化、proxmox.sources作成、cloudflaredはsuite `any`のため変更不要）
5. tmux内 dist-upgrade → EXIT_CODE=0 → 再起動 → **pve-manager 9.2.11 / kernel 7.0.14-15-pve**、NIC名不変（enp34s0）、failed units なし、quorate維持
6. cp-1/mainworker-1 は onboot 自動起動 → worker VM起動 → 全7ノード Ready

### 発生した問題と対処（大規模再構築嵐インシデント）

- **症状**: 全ノード同時復帰後、Cilium が複数ノードで flap（containerd StartError `procReady not received`、init stuck、sandbox死亡）→ instance-manager が sandbox を作れず Longhorn ボリュームが faulted 多発
- **根本原因**: ① 全ノード同時起動 + Longhorn レプリカ再構築が `concurrent-replica-rebuild-per-node-limit=5`（全体最大35並列）で走り **I/O PSI が full 60-85% に張り付き**、② runc init や cilium init が I/O wait でタイムアウト、③ Talos OOMController がメモリPSIバーストで burstable cgroup（Cilium Pod）を SIGKILL、というフィードバックループ。バージョン固有バグではなく負荷誘発（100日間安定稼働していた同構成で再現）
- **対処**: rebuild並列数を **1に絞り**（緊急措置）、Ciliumを1ノードずつ回復、longhorn-manager の起動時 settings race（`guaranteed-instance-manager-cpu` の optimistic concurrency conflict で FATAL）はPodを1つずつ削除して回避、cp-1 上の単一レプリカ5本は `spec.failedAt=""` を patch して手動salvage
- **最終状態**: 全ボリューム attached（29 healthy + 7 rebuilding）、全ワークロード Running、flux check passed
- **残課題・教訓**:
  - `concurrent-replica-rebuild-per-node-limit` は現状 **1** のまま。HDD-backed ノードがあるこのクラスタでは恒久的に低め（1〜2）を推奨
  - 次回の全停止→起動は **段階起動** にする（CP → Cilium安定 → ストレージノード1台ずつ → 一般ワークロード）。VMのonboot一斉起動が今回の嵐の引き金
  - Talos 1.12.2+ に OOM controller の修正あり。librarian調査では直接の修正確認は取れなかったが、Talos 1.12.3 への更新を検討（terraform側でイメージバージョン管理）
  - CT 102 の `lxc.cgroup.devices.allow` は deprecated 警告あり（将来 hard error 化予定。廃止予定のため実害なし）
  - この環境のローカル `/tmp` は揮発する（etcdスナップショット喪失の事案あり）。**バックアップはワークスペース `.opencode/backups/` へ保存すること**（同ディレクトリは global gitignore で除外済み）

## 実行ログ: data (2026-09-05) ✅ 完了

1. etcdスナップショット取得（`/tmp/opencode/etcd-backup-20260905.db`, 164M）※/tmp揮発により喪失。代替は `.opencode/backups/etcd-backup-20260905-postupgrade.db`
2. worker-1/2 を `talosctl shutdown --force` で停止（drain省略はユーザー承認済み）
3. `systemd-boot` 削除 → pve8to9 のFAIL解消（TerraformRole のみ残存）
4. リポジトリ切替: sources.list の bookworm→trixie、PVE行を削除して deb822 `proxmox.sources` 作成、ceph.list は `.bak` に無効化、tailscale.list も trixie 化
5. tmux内で `DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y -o Dpkg::Options::=--force-conf{def,old}` → **EXIT_CODE=0**
6. 再起動 → **pve-manager 9.2.11 / kernel 7.0.14-15-pve**、NIC名不変（enp4s0）、failed units なし、chrony正常、クラスタ quorate 維持
7. VM 1010/1011 は **onboot自動起動で既に起動済み**（手動start不要だった）。worker-1/2 Ready 復帰。worker-1のcordonは再起動で解除済み

### 発生した問題と対処

- **Longhorn単一レプリカボリューム2本が faulted**（`attic/attic-db-1` 10Gi, `iceshrimp/iceshrimp-valkey-data` 1Gi — 両方 worker-2 上の単一レプリカ）。`--force` による非graceful停止が原因。Longhornのauto-salvageで自動復旧し、両Pod正常起動を確認。**教訓: `--force` は原則使わずdrain完了を待つ。使う場合は単一レプリカボリュームの有無を事前確認すること**
- `nextcloud-restore-20260801`（toliworker-3接続）がdegraded — worker-1上レプリカの再構築中。自動回復見込み
- pve8to9 残WARN: intel-microcode未導入（任意対応）、chrony（再起動で解消）
- **`TerraformRole` 修正済み**（2026-09-05）: `VM.Monitor` を削除し `VM.GuestAgent.Audit` を追加（`Sys.Audit` は既存）。クラスタ全体に伝播確認済み。**注意: main（PVE8）は `VM.GuestAgent.Audit` を未認識で警告を出す**ため、main をPVE9化するまで terraform@pve は main 上VMのゲストエージェント参照権限を持たない。tofu plan の挙動はユーザー側で確認予定
- data にはvzdumpバックアップ設定が無いことが判明 → **ユーザー判断で vzdump 導入はしない**（VMはTerraform再作成可能＆データはLonghorn R2でカバー。CT 102 `testserver` も廃止予定のため対象外）
