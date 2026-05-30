# ネットワークアーキテクチャ

## トポロジー

```
インターネット
    │
ISPルーター (192.168.1.1) ─── static route非対応
    │
Buffalo WSR-1800AX4S (WAN: 1.x / LAN: 192.168.11.1)
    │  - NATルーター (11.0/24 → インターネット)
    │  - DHCPサーバー (11.0/24, GW通知=11.2)
    │  - static route: 10.0/24 → 11.2 (NAT戻りトラフィック・Buffalo自身の10.0/24アクセスに必要)
    │
[11.0/24 L2セグメント] ─── Buffalo LAN + ICX port 47-48
    │
ICX7250 (VE 2: 192.168.11.2, VE 10: 192.168.10.1)
    │  - L3ルーター (VLAN間ルーティング)
    │  - デフォルトルート: 0.0.0.0/0 → 11.1 (Buffalo経由でインターネット)
    │  - DHCPサーバー (10.0/24: .150-.250)
    │
[10.0/24 L2セグメント] ─── ICX port 1-46 (VLAN 10)
    ├── Proxmox main (192.168.10.100, GW: 10.1)
    ├── Proxmox data (192.168.10.40, GW: 10.1)
    ├── Proxmox toliunit (192.168.10.101, GW: 10.1)
    └── Talos VMs (192.168.10.110-125)
```

## ルーティング設計

| 送信元 | 宛先 | パス |
|--------|------|------|
| 11.0/24クライアント → 10.0/24 | ICX (GW 11.2) → 直接ルーティング |
| 11.0/24クライアント → インターネット | ICX (GW 11.2) → Buffalo (11.1) → NAT → ISPルーター |
| 10.0/24 → 11.0/24 | ICX → 直接ルーティング (VE 2経由) |
| 10.0/24 → インターネット | ICX → Buffalo (11.1) → NAT → ISPルーター |

**重要**: 11.0/24クライアントのデフォルトGWは **ICX (11.2)** であること。BuffaloのDHCPで通知している。

## 非対称ルーティング問題 (解決済み)

### 症状

11.0/24のクライアントから10.0/24へのTCP接続が確立できない（SYNは通るがACKが到達しない）。ICMP (ping) とUDP (Tailscale WireGuard) は影響なし。

### 原因

デフォルトGWがBuffalo (11.1) の場合、非対称ルーティングが発生:

```
[アウトバウンド] PC → Buffalo(SPI conntrack作成) → ICX → Proxmox
[インバウンド]  Proxmox → ICX → PC(直接L2、Buffaloスキップ)
```

BuffaloのSPI (Stateful Packet Inspection) はSYNとACKを観測するが、SYN-ACKはICX→PCへ直帰するため観測できない。結果としてACKを「状態不整合」としてDROPしていた。

### 修正

BuffaloのDHCP「デフォルトゲートウェイの通知」を `192.168.11.2` (ICX) に変更。これにより全トラフィックがICX経由となり対称ルーティングが実現。

- 10.0/24向け: PC ↔ ICX ↔ Proxmox (Buffalo関与なし)
- インターネット向け: PC → ICX → Buffalo → NAT → インターネット (BuffaloがNAT処理するため双方向を観測、SPI正常動作)

## デバイス情報

| デバイス | 機種 | 役割 |
|----------|------|------|
| ISPルーター | 不明 | インターネット接続 (static route非対応) |
| Buffalo | WSR-1800AX4S | NAT + DHCP (11.0/24) + Wi-Fi |
| ICX7250 | Ruckus ICX7250-48-HPOE | L3スイッチ (VLAN間ルーティング + DHCP 10.0/24) |
| Proxmox main | - | 仮想化ホスト (CP node: cp-1) |
| Proxmox data | - | 仮想化ホスト (Worker nodes: worker-1〜3) |
| Proxmox toliunit | - | 仮想化ホスト (Worker nodes: toliworker-1〜3) |
