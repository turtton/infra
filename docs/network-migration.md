# ネットワーク移行手順: ICX7250への移行

Proxmox VEノードを旧ネットワーク (192.168.11.X) からICX7250管理下のネットワーク (192.168.10.0/24, VLAN 10) に移行する手順。

## 前提条件

| 項目 | 状態 |
|------|------|
| ICX7250 | 稼働中。VLAN 10 (192.168.10.1/24) でLAN提供中 |
| Proxmox main | 192.168.11.X (旧ネットワーク) |
| Proxmox data | 192.168.11.X (旧ネットワーク) |
| Talos VM | 192.168.10.110-122 で設定済み (稼働中) |
| 作業用PC (PC2) | ICX7250に接続してAnsible実行に使用 |
| ローカルコンソール | 各Proxmoxノードにモニタ+キーボード接続可能 (ネットワーク切替失敗時の復旧用) |
| 電源操作 | 各ノードの電源ボタンにアクセス可能 |
| SOPS復号鍵 | PC2に `SOPS_AGE_KEY_FILE` 環境変数 (または `age.key`) が設定済み |

## ネットワーク設計

```
ISP Router (192.168.0.1)
    │
    ├── port 47-48 (VLAN 2: UPLINK)
    │
ICX7250 (192.168.10.1)
    │
    ├── port 1-46 (VLAN 10: LAN, 192.168.10.0/24)
    │       ├── Proxmox main (192.168.10.100) - vmbr0
    │       │       ├── cp-1 VM (192.168.10.110)
    │       │       └── ...
    │       ├── Proxmox data (192.168.10.40) - vmbr0
    │       │       └── worker-1 VM (192.168.10.120)
    │       └── PC2 (DHCP: 192.168.10.150-250)
    │
DHCP: 192.168.10.150-250 (除外: .1-.149, .251-.254)
```

## 移行手順

### Phase 1: 事前準備

1. **PC2をICX7250に接続**

   VLAN 10のポート (port 1-46) にLANケーブルを接続し、DHCPでIPを取得。

   ```bash
   # 接続確認
   ping 192.168.10.1
   ```

2. **ICX7250のポート確認**

   PC2からスイッチに接続して使用ポートを確認:

   ```bash
   # ICX7250はレガシーSSHアルゴリズムのみ対応。接続できない場合は以下のオプションを追加:
   # ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa -o MACs=+hmac-sha1 <user>@192.168.10.1
   ssh <switch_user>@192.168.10.1
   show vlan
   show interfaces brief
   ```

   Proxmox ノードを接続するポートが **VLAN 10 に untagged (access) で所属** していることを確認する。
   タグ付き (tagged/trunk) になっていると、Proxmox 側で VLAN タグ設定なしの場合通信できない。

3. **作業環境の準備**

   PC2にこのリポジトリをクローンし、Ansible実行環境を整える:

   ```bash
   git clone <repo>
   cd infra/ansible
   # nix develop or direnv allow で環境構築
   ```

### Phase 2: Proxmox data ノード移行

影響が小さいワーカー側から開始する。

1. **旧ネットワーク経由でSSH**

   ```bash
   ssh root@<data の旧IP: 192.168.11.X>
   ```

2. **現在の設定をバックアップ**

   ```bash
   cp /etc/network/interfaces /root/interfaces.pre-migration
   ```

3. **/etc/network/interfaces を変更**

   ```bash
   cat > /etc/network/interfaces << 'EOF'
   # Managed by Ansible
   auto lo
   iface lo inet loopback

   auto enp4s0
   iface enp4s0 inet manual

   auto vmbr0
   iface vmbr0 inet static
       address 192.168.10.40/24
       gateway 192.168.10.1
       bridge-ports enp4s0
       bridge-stp off
       bridge-fd 0
   EOF
   ```

4. **ノードをシャットダウン**

   ```bash
   shutdown -h now
   ```

5. **物理作業**

   LANケーブルを旧スイッチから抜き、ICX7250のVLAN 10ポート (port 1-46) に接続する。

6. **スイッチ側でリンク確認**

   ICX7250に接続して、想定ポートにリンクアップとMAC学習が出ていることを確認:

   ```
   show interfaces brief
   show mac-address
   ```

7. **ノードを起動**

   電源ボタンで起動。

8. **PC2から疎通確認 (段階的に)**

   ```bash
   # 1. ゲートウェイ到達 (L2/L3)
   ping 192.168.10.40

   # 2. 外部IP到達 (ルーティング)
   ping 1.1.1.1

   # 3. DNS解決 + HTTPS (名前解決)
   curl -s https://ifconfig.me

   # 4. SSH接続
   ssh root@192.168.10.40
   ```

9. **Ansible 到達確認**

   ```bash
   # PC2から (infra/ansible ディレクトリで)
   ansible data -m ping
   ```

### Phase 3: Proxmox main ノード移行

> **⚠️ 影響**: `main` ノードには control plane (cp-1) が稼働している。このフェーズ中は
> Kubernetes API (`kubectl`)、Flux CD、Grafana が一時停止する。これは正常動作であり障害ではない。

1. **旧ネットワーク経由でSSH**

   ```bash
   ssh root@<main の旧IP: 192.168.11.X>
   ```

2. **現在の設定をバックアップ**

   ```bash
   cp /etc/network/interfaces /root/interfaces.pre-migration
   ```

3. **/etc/network/interfaces を変更**

   ```bash
   cat > /etc/network/interfaces << 'EOF'
   # Managed by Ansible
   auto lo
   iface lo inet loopback

   auto enp34s0
   iface enp34s0 inet manual

   auto vmbr0
   iface vmbr0 inet static
       address 192.168.10.100/24
       gateway 192.168.10.1
       bridge-ports enp34s0
       bridge-stp off
       bridge-fd 0
   EOF
   ```

4. **シャットダウン → ケーブル差し替え → 起動**

   ```bash
   shutdown -h now
   # → ICX7250ポートに接続 → スイッチ側で show interfaces brief でリンク確認 → 起動
   ```

5. **PC2から疎通確認 (段階的に)**

   ```bash
   # 1. ゲートウェイ到達
   ping 192.168.10.100

   # 2. 外部IP到達
   ping 1.1.1.1

   # 3. DNS + HTTPS
   curl -s https://ifconfig.me

   # 4. SSH
   ssh root@192.168.10.100
   ```

6. **Ansible 到達確認**

   ```bash
   ansible main -m ping
   ```

### Phase 4: Ansible で冪等性確認

PC2から Ansible を実行し、設定が正しいことを確認:

```bash
cd ansible/

# 1. ネットワーク設定の確認 (変更なしが期待値)
ansible-playbook playbooks/network-update.yml --check --diff

# 2. ネットワーク差分がゼロであることを確認してから site.yml を確認
#    (site.yml は serial:1 ではないため、ネットワーク差分がある状態で流すと
#     両ノード同時にネットワークリロードが走り接続断になるリスクがある)
ansible-playbook playbooks/site.yml --check --diff

# 3. 問題なければ適用 (Tailscale再接続など)
ansible-playbook playbooks/site.yml
```

> **注意**: `network-update.yml --check --diff` でネットワーク差分が出た場合は、
> 先に `network-update.yml` (serial: 1) を実行して収束させてから `site.yml` を流すこと。

### Phase 5: Talos VM の確認

Proxmox のvmbr0がICX7250 VLAN 10に接続されたことで、VMは自動的に正しいネットワークに接続される。

```bash
# VM への疎通確認
ping 192.168.10.110   # cp-1
ping 192.168.10.120   # worker-1

# Kubernetes クラスタ状態
export KUBECONFIG=~/.kube/config
kubectl get nodes
kubectl get pods -A

# Flux CD 確認
flux get sources git
flux get kustomizations
```

**VMが応答しない場合**:

1. Proxmox Web UIからVM再起動を試す
   - https://192.168.10.100:8006 (main)
   - https://192.168.10.40:8006 (data)

2. VMのネットワーク設定を確認
   ```bash
   # Proxmox CLI で VM の NIC 設定を確認
   qm config <vmid> | grep net
   # Bridge が vmbr0 であること
   ```

3. `talosctl` で状態確認
   ```bash
   talosctl -n 192.168.10.110 get addresses
   talosctl -n 192.168.10.110 get routes
   ```

4. **最終手段: クラスタ再作成 (⚠️ 全データ消失)**

   上記の手順すべてで復旧できない場合のみ。
   ```bash
   cd terraform/
   tofu destroy
   tofu apply
   ```

> **注意**: `tofu destroy` は etcd データと Longhorn ボリュームを含む全VMデータを削除する。
> Kubernetes クラスタの再構築 (Cilium手動インストール + Flux bootstrap) が必要になる。

### Phase 6: 最終検証チェックリスト

- [ ] Proxmox main (192.168.10.100) ↔ data (192.168.10.40) 通信
- [ ] 両ノード → インターネット
- [ ] Proxmox Web UI アクセス可能
- [ ] VM → ゲートウェイ (192.168.10.1)
- [ ] VM → インターネット
- [ ] `kubectl get nodes` — 全ノード Ready
- [ ] `flux get kustomizations` — 全て正常
- [ ] Tailscale 再接続確認
- [ ] `terraform/tailscale.tf` の旧ネットワーク参照 (192.168.11.X) を更新済み
- [ ] Grafana アクセス確認

## トラブルシューティング

### ノードが起動後に通信できない

1. 物理コンソール (モニタ+キーボード) で直接ログイン
2. `ip addr show` でIPが正しいか確認
3. `ip route` でデフォルトルートが 192.168.10.1 を向いているか確認
4. ICX7250側で `show mac-address` でMACアドレスが見えているか確認

### VMがゲートウェイに到達できない

1. Proxmox Web UI で VM のネットワーク設定を確認 (Bridge: vmbr0)
2. `talosctl -n 192.168.10.110 get addresses` でTalosのIP確認
3. VM再起動を試す

### Ansible が接続できない

```bash
# SSH接続テスト
ansible proxmox -m ping

# SSH鍵が正しいか確認
ssh -v root@192.168.10.100
```

## ロールバック

各ノードで旧設定に戻す場合:

```bash
# Phase 2/3 で作成したバックアップから復元
cp /root/interfaces.pre-migration /etc/network/interfaces

# ケーブルを旧スイッチに戻して再起動
shutdown -r now
```

`ansible/inventory/hosts.yml` の `ansible_host` も旧環境に合わせて戻すか削除すること。

## 移行後の後続作業

移行完了後、以下の設定を新ネットワークに合わせて更新する:

- [ ] `terraform/tailscale.tf`: CI用ACLの `192.168.11.0/24` → `192.168.10.0/24` に変更
- [ ] `terraform/tailscale.tf`: Proxmox APIアクセス元の `192.168.11.100:5000` を更新
- [ ] Tailscale subnet router のアドバタイズ対象ネットワークを確認
