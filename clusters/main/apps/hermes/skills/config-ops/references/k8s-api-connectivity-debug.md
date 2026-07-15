# K8s API 接続不可デバッグ手順

## 経緯

ATM10 Podのワールド再生成をリモートユーザー（ちゃんせい）から依頼された際、k8s API Serverに接続できない問題が発生。ユーザーから「君はhermes-homeのPod内で動いている」と指摘され、調査の結果、Cilium + KubePrism環境でのAPI Serverエンドポイント未登録（またはControl Plane障害）が原因と判明。

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

### Step 3: ネットワーク切り分け (General)

```bash
# CoreDNS到達性 (Pod内ネットワークの基本動作確認)
timeout 3 bash -c 'echo > /dev/tcp/10.96.0.10/53 && echo "CoreDNS OK"'
# → OKならPodネットワークは動作している

# 他のClusterIP Serviceの到達性確認（重要: Cilium LB全体の健全性確認）
timeout 3 bash -c 'echo > /dev/tcp/$HERMES_HOME_SERVICE_HOST/9119 && echo "hermes-svc OK"'
# → OKならCiliumのService LBは正常動作している。kubernetes Serviceのみの問題と特定できる

# Kubernetes Service到達性
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
timeout 5 curl -sk --connect-timeout 5 https://10.96.0.1:443/api \
  --header "Authorization: Bearer $TOKEN"
# → timeout: Service IP経由のルーティングが機能していない

# API Server直接アクセス（Tokenのissフィールドから実IPとポートを確認）
# cat /var/run/secrets/kubernetes.io/serviceaccount/token | cut -d. -f2 | base64 -d | python3 -m json.tool
# → iss: "https://192.168.10.110:6443"
timeout 5 curl -sk --connect-timeout 5 https://192.168.10.110:6443/api \
  --header "Authorization: Bearer $TOKEN"
# → timeout: 直接アクセスも不可

# 他ノードへの疎通確認
timeout 3 ping -c 1 -W 2 192.168.10.110
timeout 3 ping -c 1 -W 2 192.168.10.111
timeout 3 ping -c 1 -W 2 192.168.10.102  # 自ノードのIP
# → 全て不通 = CNIオーバーレイ内に隔離されている

# CNIの種類を確認
ls /sys/class/net/
# → tunl0 が存在 = Calico または Cilium (TalosではCiliumでもtunl0が存在する)
```

## Step 3b: Cilium構成の確認（infraリポジトリから）

Talos + Cilium + kubeProxyReplacement構成の場合、k8s APIへの接続経路が特殊：

```bash
# Cilium HelmReleaseからk8sService設定を確認
grep -A5 k8sService clusters/main/infrastructure/controllers/cilium/helmrelease.yaml

# 出力例:
#     k8sServiceHost: localhost     # ← KubePrism経由
#     k8sServicePort: 7445          # ← localhost:7445
#     kubeProxyReplacement: true    # ← eBPFでService IP処理
#     socketLB:
#       hostNamespaceOnly: true     # ← Pod namespaceではsocket-level LB無効
```

### 重要な診断ポイント

- **KubePrism**（Talosの機能）: 各ノードの `localhost:7445` でAPI Serverをプロキシ。Cilium agent自身もこれを経由してAPIに接続
- **kubeProxyReplacement: true**: kube-proxy非稼働。Cilium eBPFがService IPを処理
- **socketLB.hostNamespaceOnly: true**: socket-level LBはホストnamespaceのみ。PodはTC eBPFで処理

### 原因特定: 切り分けマトリクス

| 状態 | CoreDNS (53) | hermes svc (9119) | k8s API (443) |
|---|---|---|---|
| 正常 | ✅ | ✅ | ✅ |
| API Serverのみ不調 | ✅ | ✅ | ❌ ← 今回 |
| Cilium LB全滅 | ✅ | ❌ | ❌ |

→ **他のClusterIP Serviceが動いているのにk8s APIだけ不通 = Cilium eBPF Service MapにAPI Serverエンドポイントが未登録**

### 考えられる原因（Cilium + KubePrism特有）

1. **kube-apiserver ダウン**: Talosコントロールプレーン障害
2. **KubePrism プロキシ不調**: `localhost:7445` が応答していない
3. **Cilium eBPF map 不整合**: API Server再起動後にeBPFプログラムがエンドポイントを再同期できていない
4. **NetworkPolicy**: `allow-hermes-home-egress` はport 443全宛先許可なので原因ではない可能性が高い

### 対応策

Pod内からは直接修正不可能。クラスタ管理者（turtton）に以下を依頼：

```bash
# Talos APIでControl Planeの健康状態確認
talosctl -n <control-plane-ip> health

# Cilium agent再起動（eBPF map再同期）
kubectl -n kube-system rollout restart ds/cilium

# KubePrismの状態確認（Talos node上で）
talosctl -n <node-ip> services kube-prism
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

| チェック項目 | 正常時 | 今回の結果（Cilium + KubePrism環境） |
|---|---|---|
| hostname | Pod名 | hermes-home-0 ✅ |
| Service Account | token+ca.crt存在 | 存在 ✅ |
| CoreDNS (10.96.0.10:53) | 接続可 | 接続可 ✅ |
| hermes-home svc (10.109.61.199:9119) | 接続可 | 接続可 ✅ ← **重要: Cilium LBは正常** |
| k8s Service (10.96.0.1:443) | 接続可 | timeout ❌ |
| API Server直接 (192.168.10.110:6443) | 接続可 | timeout ❌ |
| KubePrism (localhost:7445) | hostNetwork限定 | Podからは到達不能 |
| 他ノードping | 疎通可 | 不通 ❌ |
| tunl0 I/F | (Calico/Cilium IPIP) | 存在 = Cilium or Calico |

## 想定される原因（Cilium + kubeProxyReplacement環境）

1. **kube-apiserver ダウン**: Talosコントロールプレーン障害でAPI Serverが応答なし
2. **KubePrism プロキシ不調**: `localhost:7445` のプロキシが機能していない
3. **Cilium eBPF map 不整合**: kube-apiserver再起動後、CiliumのService Mapに正しいエンドポイントが反映されていない
4. **NetworkPolicy**: `allow-hermes-home-egress` はport 443全宛先許可。通常は原因にならない

## ユーザーへの報告テンプレート

> Pod内からk8s API Serverに接続できない状況です。
> - 環境: Cilium + KubePrism + kubeProxyReplacement (Talos)
> - CoreDNS (10.96.0.10:53) は到達可能 ✅
> - hermes-home service (10.109.61.199:9119) は到達可能 ✅
> - k8s API (10.96.0.1:443) はtimeout ❌
> - 直接アクセス (192.168.10.110:6443) もtimeout ❌
> - KubePrism (localhost:7445) はhostNetwork限定でPodから到達不能
> - NetworkPolicy (allow-hermes-home-egress) はport 443全宛先許可 → 原因ではない
> - 他ノードへのpingも不通（CNIオーバーレイ内に隔離）
> - CNI: Cilium (tunl0存在, kubeProxyReplacement=true)
>
> Cilium agent再起動（eBPF Service Map再同期）または Talos control planeの健康確認をお願いします。

## 参考: 関連リソース

- config-opsスキル: `本文書の親スキル`
- infraリポジトリ CLAUDE.md: `clusters/main/` 配下のマニフェスト構成
- RBAC設定: `clusters/main/apps/atm10/hermes-home-rbac.yaml`
