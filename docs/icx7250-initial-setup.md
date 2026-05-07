# Ruckus ICX7250 初期設定手順

ファームウェア更新済み（SPR08095t）の ICX7250 に対して、Ansible管理に必要な初期設定を行う手順をまとめる。

---

## 1. 概要

ICX7250 をホームラボのL3スイッチとして使用するにあたり、以下の初期設定をシリアルコンソールから行う。

- 管理用VLAN・IPアドレスの設定
- SSH有効化
- 管理ユーザーの作成
- enable パスワードの設定

初期設定完了後は、Ansible（`community.network.icx`）で構成管理する。

---

## 2. 前提条件

- **ファームウェア:** SPR08095t（L3イメージ）に更新済みであること（[更新手順](icx7250-firmware-upgrade.md)参照）
- **シリアル接続:** コンソールケーブルでスイッチに接続できること
  - ボーレート: 9600, 8N1
- **物理接続:** スイッチの任意のポート（1/1/1-46推奨）にPCを接続しておく

---

## 3. シリアルコンソールへの接続

```bash
# Linux の場合（screen）
screen /dev/ttyUSB0 9600

# または minicom
minicom -D /dev/ttyUSB0 -b 9600

# Nix環境
nix shell nixpkgs#screen -c screen /dev/ttyUSB0 9600
```

電源投入後、ブートが完了すると `ICX7250-48 Router>` プロンプトが表示される。

---

## 4. 設定手順

### 4.1. 特権モードに入る

```
ICX7250-48 Router> enable
ICX7250-48 Router#
```

初回はパスワードなしで入れる。

### 4.2. コンフィグモードに入る

```
ICX7250-48 Router# configure terminal
ICX7250-48 Router(config)#
```

### 4.3. ホスト名の設定

```
hostname icx7250
```

### 4.4. enable パスワードの設定

```
enable super-user-password <ENABLE_PASSWORD>
```

> **注意:** `<ENABLE_PASSWORD>` は SOPS 暗号化ファイル（`vault.sops.yml`）に保存する値と同じものを設定すること。

### 4.5. VLAN の作成

Ansible で最終構成を適用する前の暫定設定として、管理用の VLAN 10 を作成し、IPアドレスを付与する。

```
vlan 10 name LAN by port
  router-interface ve 10
  untagged ethernet 1/1/1 to 1/1/46

interface ve 10
  ip address 192.168.10.1/24
```

### 4.6. デフォルトルートの設定（任意）

上位ルーターへの疎通が必要な場合。この時点ではまだVLAN 2（アップリンク）は未設定のため、作業用PCとの疎通確保が目的。

```
ip route 0.0.0.0/0 192.168.10.254
```

> 本番では VLAN 2 経由の `ip route 0.0.0.0/0 192.168.0.1` に変更される。

### 4.7. SSH の有効化

```
crypto key generate rsa modulus 2048
ip ssh idle-time 10
```

### 4.8. 管理ユーザーの作成

```
username ansible privilege 0 password <SSH_PASSWORD>
aaa authentication login default local
```

> **注意:**
> - `<SSH_PASSWORD>` は SOPS 暗号化ファイル（`vault.sops.yml`）に保存する値と同じものを設定すること
> - `privilege 0` は通常ユーザー権限。Ansible は `enable` コマンドで特権モードに昇格する
> - enable認証は `enable super-user-password` によるパスワード認証を使用する（`aaa authentication enable default local` は設定しない）。`community.network.icx` のターミナルプラグインが `User Name:` プロンプトに対応していないため

### 4.9. 設定の保存

```
write memory
```

### 4.10. コンフィグモードの終了

```
end
```

---

## 5. 接続確認

### 5.1. スイッチ側の確認

```bash
# IPアドレスの確認
show ip interface

# SSHの状態確認
show ip ssh

# VLANの確認
show vlan brief
```

### 5.2. PC側からの疎通確認

PC側のIPを `192.168.10.0/24` の範囲（例: `192.168.10.200`）に設定してから確認する。

```bash
# Pingで疎通確認
ping 192.168.10.1

# SSH接続テスト（レガシーアルゴリズムが必要）
ssh -o KexAlgorithms=+diffie-hellman-group1-sha1 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa ansible@192.168.10.1
```

> **注意:** ICX7250のSSH実装は古いアルゴリズム（diffie-hellman-group1-sha1, ssh-rsa）のみ対応している。Ansible側では `group_vars/switches/connection.yml` の `ansible_ssh_extra_args` で自動設定済み。

### 5.3. Ansibleからの接続テスト

```bash
cd ansible/

# 接続テスト（ad-hocコマンド）
ansible icx7250 -m community.network.icx_command -a "commands='show version'"
```

---

## 6. 次のステップ

初期設定が完了したら、Ansible Playbook で本番構成を適用する。

```bash
# Dry-run（差分確認）
ansible-playbook playbooks/switch.yml --check --diff

# 本番適用
ansible-playbook playbooks/switch.yml
```

Ansible が適用する構成:
- VLAN 2（アップリンク: 192.168.0.2/24）+ VLAN 10（LAN: 192.168.10.1/24）
- ポート1/1/1-46: VLAN 10 untagged（LAN）
- ポート1/1/47-48: VLAN 2 untagged（アップリンク）
- DHCPサーバー（192.168.10.150-250）
- inter-VLAN ルーティング + デフォルトルート（192.168.0.1）
- NTP/DNS/ロギング

### 上流ルーターの設定（必須）

ICX7250 の Ansible 適用後、**上流ルーター（192.168.0.1）に戻り経路を設定する必要がある**。これがないと VLAN 10 のクライアントからインターネットへの通信は届くが、戻りパケットがルーターで迷子になり通信が成立しない。

上流ルーターに以下の静的ルートを追加する:

```
宛先: 192.168.10.0/24
ゲートウェイ: 192.168.0.2（ICX7250 の VLAN 2 アドレス）
```

設定方法はルーターの機種に依存する。Web管理画面の「静的ルート」や「ルーティング設定」から追加する。

### 適用後の確認コマンド

```
show vlan
show running-config
show ip interface brief
show ip dhcp-server
show ip route
```

---

## 7. トラブルシューティング

| 問題 | 確認事項 | 対処 |
|---|---|---|
| SSH接続できない | `show ip ssh` で SSH が有効か確認 | `crypto key generate rsa modulus 2048` を再実行 |
| Pingが通らない | `show ip interface brief` でIPが付与されているか | VLANとポートの紐付けを再確認 |
| enable に入れない | パスワードが正しいか | `enable super-user-password` で再設定 |
| Ansibleから繋がらない | `ansible.cfg` のホスト情報確認 | `host_key_checking = false` が設定されているか確認 |

---

## 参考

- [ファームウェア更新手順](icx7250-firmware-upgrade.md)
