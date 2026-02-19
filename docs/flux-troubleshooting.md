# Flux CD トラブルシューティング

Flux CDで管理しているHelmReleaseやKustomizationがデプロイに失敗した場合の確認方法と対処法をまとめる。

---

## 1. 状態確認の基本手順

### 1.1 全体の同期状態を確認

```bash
# Kustomizationの同期状態
flux get kustomizations

# HelmReleaseの同期状態（全namespace）
flux get helmreleases -A

# GitRepositoryのソース取得状態
flux get sources git
```

依存チェーン `flux-system` → `infra-controllers` → `apps` の順で確認し、どこで止まっているか特定する。

### 1.2 Pod状態の確認

```bash
kubectl get pods -n <namespace>
```

`Init:0/1`、`CrashLoopBackOff`、`Error` などの異常状態に注目する。

### 1.3 イベントとログの確認

```bash
# namespaceのイベント（時系列順）
kubectl get events -n <namespace> --sort-by='.lastTimestamp'

# 問題のあるPodの詳細
kubectl describe pod <pod-name> -n <namespace>

# Podのログ
kubectl logs <pod-name> -n <namespace>
```

---

## 2. Pod Security Standards違反

### 症状

- DaemonSetやDeploymentの `DESIRED > 0` だが `CURRENT = 0`（Podが一つも作成されない）
- イベントに以下のようなエラーが出る:

```
violates PodSecurity "baseline:latest": hostPath volumes (...), privileged (...)
```

### 原因

namespaceのPod Security Standardsが `baseline`（デフォルト）のままで、`hostPath` ボリュームや `privileged` コンテナを使うワークロードがブロックされている。

Longhorn、Prometheus Node Exporterなど、ホストリソースにアクセスするコンポーネントで発生しやすい。

### 対処

namespace定義に `privileged` ラベルを追加する:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <namespace>
  labels:
    pod-security.kubernetes.io/enforce: privileged
```

変更をcommit & pushし、Fluxの同期を待つ（または `flux reconcile` で即時反映）。

---

## 3. HelmReleaseが `uninstalling` 状態で詰まる

### 症状

- `flux get helmreleases -A` で `Ready: False`、メッセージに `unable to determine state for release with status 'uninstalling'` と表示される
- HelmReleaseがインストール失敗 → アンインストール → アンインストールも失敗、のループに陥っている

### 確認方法

```bash
# Helmリリースの状態を直接確認（-a で全状態表示）
helm list -n <namespace> -a

# Helmリリースを管理するSecretの確認
kubectl get secrets -n <namespace> -l owner=helm
```

`STATUS` が `uninstalling` や `pending-install` で止まっている場合、手動介入が必要。

### 対処

壊れたHelmリリースのSecretを削除して、Fluxに新規インストールさせる:

```bash
# Secretの特定
kubectl get secrets -n <namespace> -l owner=helm

# 壊れたリリースのSecretを削除
kubectl delete secret <sh.helm.release.v1.NAME.vN> -n <namespace>

# HelmReleaseを再reconcile
flux reconcile helmrelease <release-name> -n <namespace>
```

---

## 4. HelmReleaseのインストールがタイムアウトする

### 症状

- `Helm install failed for release ... context deadline exceeded`

### 確認方法

```bash
# HelmReleaseの詳細を確認
kubectl describe helmrelease <name> -n <namespace>

# namespace内のPod状態を確認し、起動に失敗しているPodを特定
kubectl get pods -n <namespace>
```

### よくある原因と対処

| 原因 | 確認方法 | 対処 |
|---|---|---|
| Pod Security Standards違反 | `kubectl get events -n <ns>` でPodSecurity関連エラー | セクション2を参照 |
| イメージPull失敗 | Pod statusが `ImagePullBackOff` | レジストリへの接続、イメージ名・タグを確認 |
| リソース不足 | Pod statusが `Pending`、イベントに `Insufficient cpu/memory` | ノードのリソースを確認、values調整 |

---

## 5. Kustomizationの依存関係で後続がブロックされる

### 症状

- `flux get kustomizations` で `dependency 'flux-system/infra-controllers' is not ready` のようなメッセージが出る

### 対処

依存元（この例では `infra-controllers`）の問題を先に解決する。依存元が `Ready: True` になれば後続も自動的に処理される。

```bash
# 依存元の状態確認
flux get kustomization infra-controllers

# 依存元に含まれるHelmReleaseの状態確認
flux get helmreleases -A
```

---

## 6. 手動reconcileコマンド一覧

```bash
# GitRepositoryの即時同期
flux reconcile source git flux-system

# Kustomizationの即時同期
flux reconcile kustomization <name>

# HelmReleaseの即時同期
flux reconcile helmrelease <name> -n <namespace>
```

通常はGitRepositoryの `interval: 1m` で自動検知されるが、すぐに反映したい場合に使用する。
