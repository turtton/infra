# K8s API 接続不可デバッグ手順

## 経緯

ATM10 Podのワールド再生成をリモートユーザー（ちゃんせい）から依頼された際、k8s API Serverに接続できない問題が発生。ユーザーから「君はhermes-homeのPod内で動いている」と指摘され、調査の結果、Calicoオーバーレイネットワークの隔離が原因と判明。

## 切り分け手順

### Step 1: ランタイム環境を確認

```bash
hostname
# → hermes-home-0 (Pod内で動作中)

ls /var/run/secrets/kubernetes.io/serviceaccount/
# → token, ca.crt, namespace (Service Account存在確認)

cat /var/run/secrets/kubernetes.io/serviceaccount/namespace
# → hermes (現在のNamespace)
```

### Step 2: kubeconfig と接続確認

```bash
ls ~/.kube/config       # 存在確認
# なければ in-cluster セットアップ

# Service AccountトークンからAPI Server実アドレスを確認
cat /var/run/secrets/kubernetes.io/serviceaccount/token | cut -d. -f2 | base64 -d | python3 -m json.tool
# → iss: "https://192.168.10.110:6443" がAPI Serverの実IP

# 接続確認
kubectl get pods -n atm10
# → タイムアウトする場合: Step 3へ
```

### Step 3: ネットワーク切り分け

```bash
# CoreDNS到達性 (Pod内ネットワークの基本動作確認)
timeout 3 bash -c 'echo > /dev/tcp/10.96.0.10/53 && echo "CoreDNS OK"'
# → OKならPodネットワークは動作している

# Kubernetes Service到達性
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
timeout 5 curl -sk --connect-timeout 5 https://10.96.0.1:443/api \
  --header "Authorization: Bearer $TOKEN"
# → timeout: Service IP経由のルーティングが機能していない

# API Server直接アクセス
timeout 5 curl -sk --connect-timeout 5 https://192.168.10.110:6443/api \
  --header "Authorization: Bearer $TOKEN"
# → timeout: 直接アクセスも不可

# 他ノードへの疎通確認
timeout 3 ping -c 1 -W 2 192.168.10.110
timeout 3 ping -c 1 -W 2 192.168.10.111
timeout 3 ping -c 1 -W 2 192.168.10.102  # 自ノードのIP
# → 全て不通 = Calicoオーバーレイ内に隔離されている

# CNIの種類を確認
ls /sys/class/net/
# → tunl0 が存在 = Calico (IPIPトンネルモード)
```

### Step 4: 環境情報の収集

```bash
# DNS設定
cat /etc/resolv.conf

# 自PodのIP
hostname -I

# ルーティングテーブル
cat /proc/net/route

# FIBトライ (詳細ルート)
cat /proc/net/fib_trie

# カーネルパラメータ
cat /proc/net/fib_trie | grep -E "(LOCAL|BROADCAST)"
```

## 診断まとめ

| チェック項目 | 正常時 | 今回の結果 |
|---|---|---|
| hostname | Pod名 | hermes-home-0 ✅ |
| Service Account | token+ca.crt存在 | 存在 ✅ |
| CoreDNS (10.96.0.10:53) | 接続可 | 接続可 ✅ |
| k8s Service (10.96.0.1:443) | 接続可 | timeout ❌ |
| API Server直接 (192.168.10.110:6443) | 接続可 | timeout ❌ |
| 他ノードping | 疎通可 | 不通 ❌ |
| tunl0 I/F | (Calico環境) | 存在 = Calico IPIP |

## 想定される原因

1. **NetworkPolicy**: Calicoのデフォルトdenyポリシーがegressを制限している
2. **kube-apiserver**: ダウンしている可能性（TalosクラスタでControl Plane Node障害）
3. **kube-proxy replacement**: Calicoのkube-proxy代替機能が正常動作していない

## ユーザーへの報告テンプレート

> Pod内からk8s API Serverに接続できない状況です。
> - CoreDNS (10.96.0.10:53) は到達可能
> - API Server (10.96.0.1:443 および 192.168.10.110:6443) はtimeout
> - 他ノードへのpingも不通
> - CNIはCalico (IPIPトンネル)
> - iptables/nftablesルールの確認不可（コンテナ内にバイナリなし）
>
> CalicoのNetworkPolicyかkube-apiserverの状態を確認してください。

## 参考: 関連リソース

- config-opsスキル: `本文書の親スキル`
- infraリポジトリ CLAUDE.md: `clusters/main/` 配下のマニフェスト構成
- RBAC設定: `clusters/main/apps/atm10/hermes-home-rbac.yaml`
