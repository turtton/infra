# GitOps Infrastructure Architecture Design

## Overview

Proxmox VE基盤上にKubernetesクラスタを構築し、Flux CDによるGitOpsでマルチテナント運用を行う構成。個人の趣味プロジェクトおよび複数チームプロジェクト（A, B）のワークロードを単一クラスタ上で分離管理する。外部メンバーのサーバーリソースをプロジェクト専用ノードとして受け入れる仕組みも備える。

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        Git Repositories                      │
│                                                              │
│  infra-repo (管理者専用)     personal-repo   project-a-repo  project-b-repo │
│  ├── ansible/                │               │               │
│  ├── terraform/              │               │               │
│  └── clusters/main/         │               │               │
│      ├── flux-system/       │               │               │
│      ├── infrastructure/    │               │               │
│      └── tenants/           │               │               │
└──────────┬──────────────────┴───────┬───────┴───────┬───────┘
           │                          │               │
           ▼                          ▼               ▼
┌──────────────────────────────────────────────────────────────┐
│                     Flux CD (GitOps Engine)                   │
│  Source Controller / Kustomize Controller / Helm Controller   │
│  Notification Controller → Slack / Webhook                   │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│                   Kubernetes Cluster                          │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ ns: personal │  │ ns: project-a │  │ ns: project-b │       │
│  │             │  │              │  │              │       │
│  │ ResourceQuota│  │ ResourceQuota │  │ ResourceQuota │       │
│  │ NetworkPolicy│  │ NetworkPolicy │  │ NetworkPolicy │       │
│  │ RBAC        │  │ RBAC         │  │ RBAC         │       │
│  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                │                  │               │
│  ┌──────▼──────┐  ┌──────▼───────┐  ┌──────▼───────┐       │
│  │ 共有ノード   │  │ 専用ノード    │  │ 共有ノード    │       │
│  │ (Proxmox)   │  │ (メンバー提供) │  │ (Proxmox)   │       │
│  └─────────────┘  └──────────────┘  └──────────────┘       │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│                    Proxmox VE Cluster                         │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │ pve-node1 │  │ pve-node2 │  │ pve-node3 │  ...           │
│  │ K8s VMs  │  │ K8s VMs  │  │ LXC/VM   │                  │
│  └──────────┘  └──────────┘  └──────────┘                  │
│                                                              │
│  管理: Ansible (ホスト構成) + Terraform (VM/LXC定義)          │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                       Monitoring                             │
│                                                              │
│  Proxmox ─→ prometheus-pve-exporter ─→ Prometheus ─→ Grafana │
│  K8s     ─→ kube-state-metrics      ─↗                      │
│  Flux    ─→ /metrics                 ─↗                      │
└──────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| レイヤー | ツール | 役割 |
|---|---|---|
| Proxmoxホスト構成管理 | Ansible | ノードIP、ネットワーク、パッケージ、WireGuard設定 |
| VM/LXC定義 | Terraform + Proxmox Provider | Proxmox上のリソースを宣言的に管理 |
| Kubernetes構築 | Talos Linux (推奨) or K3s | immutable & API駆動でGitOps親和性が高い |
| GitOps | Flux CD | Git→クラスタ自動同期、マルチテナントネイティブ対応 |
| モニタリング | Prometheus + Grafana | Proxmox/K8s/Fluxの統合監視 |
| VPN | Tailscale / Headscale or WireGuard | 外部メンバーノード接続用メッシュVPN |

---

## Repository Structure

### infra-repo（管理者専用）

クラスタ全体のインフラ定義を管理する。このリポジトリへの書き込み権限は管理者のみ。

```
infra-repo/
├── ansible/
│   ├── inventory/
│   │   └── hosts.yml                 # Proxmoxノード一覧・IP定義
│   ├── roles/
│   │   ├── proxmox-base/             # 共通パッケージ、NTP、DNS等
│   │   ├── proxmox-network/          # /etc/network/interfaces 管理
│   │   ├── wireguard/                # VPN設定（外部ノード接続用）
│   │   └── monitoring-agent/         # prometheus-pve-exporter
│   └── playbooks/
│       ├── site.yml                  # フル適用
│       └── network-update.yml        # IP変更時の一括更新
│
├── terraform/
│   ├── versions.tf                   # Provider設定 + state暗号化
│   ├── providers.tf                  # Proxmox/Talosプロバイダ
│   ├── variables.tf                  # 変数定義
│   ├── terraform.tfvars              # 変数値
│   ├── talos-image.tf                # Talosイメージ管理
│   ├── vms.tf                        # K8s用VM定義
│   ├── talos.tf                      # Talos設定・ブートストラップ
│   └── outputs.tf                    # 出力定義
│
├── talos/
│   ├── controlplane.yaml             # Talos control plane設定
│   ├── worker.yaml                   # Talos worker設定
│   └── patches/
│       └── cni.yaml                  # CNI設定パッチ
│
└── clusters/
    └── main/
        ├── flux-system/              # Flux自体のブートストラップ
        │   ├── gotk-components.yaml
        │   ├── gotk-sync.yaml
        │   └── kustomization.yaml
        │
        ├── infrastructure/           # 共有インフラコンポーネント
        │   ├── sources/              # HelmRepository, GitRepository定義
        │   ├── ingress-nginx/
        │   ├── cert-manager/
        │   ├── monitoring/
        │   │   ├── prometheus/
        │   │   ├── grafana/
        │   │   └── flux-dashboards/  # Flux用Grafanaダッシュボード
        │   └── kustomization.yaml
        │
        ├── tenants/
        │   ├── base/                 # テナント共通テンプレート
        │   │   ├── namespace.yaml
        │   │   ├── rbac.yaml
        │   │   ├── resource-quota.yaml
        │   │   └── network-policy.yaml
        │   ├── personal.yaml         # 個人テナント定義
        │   ├── project-a.yaml        # プロジェクトAテナント定義
        │   └── project-b.yaml        # プロジェクトBテナント定義
        │
        └── nodes/
            ├── shared.yaml           # 共有ノードのラベル定義
            ├── project-a.yaml        # project-a専用ノードのTaint/Label
            └── project-b.yaml
```

### テナントリポジトリ（各チーム管理）

各プロジェクトチームが自身のリポジトリでワークロードを管理する。

```
project-a-repo/
├── base/
│   ├── deployment.yaml
│   ├── service.yaml
│   └── kustomization.yaml
└── overlays/
    └── production/
        ├── kustomization.yaml       # Tolerations, NodeAffinity パッチ含む
        └── patches/
            └── scheduling.yaml      # 専用ノードへの配置設定
```

---

## Multi-Tenancy Configuration

### テナント定義（Flux Kustomization）

```yaml
# clusters/main/tenants/project-a.yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: project-a
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/org/project-a-repo
  ref:
    branch: main
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: project-a
  namespace: flux-system
spec:
  interval: 5m
  sourceRef:
    kind: GitRepository
    name: project-a
  path: ./overlays/production
  prune: true
  targetNamespace: project-a
  serviceAccountName: project-a-deployer
```

### Namespace & RBAC

```yaml
# clusters/main/tenants/base/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <TENANT>
  labels:
    tenant: <TENANT>
---
# clusters/main/tenants/base/rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: <TENANT>-deployer
  namespace: <TENANT>
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: <TENANT>-admin
  namespace: <TENANT>
subjects:
  - kind: ServiceAccount
    name: <TENANT>-deployer
    namespace: <TENANT>
roleRef:
  kind: ClusterRole
  name: admin
  apiGroup: rbac.authorization.k8s.io
```

### ResourceQuota

```yaml
# clusters/main/tenants/base/resource-quota.yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: <TENANT>-quota
  namespace: <TENANT>
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
    persistentvolumeclaims: "10"
```

### NetworkPolicy（デフォルトDeny + テナント内通信許可）

```yaml
# clusters/main/tenants/base/network-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: <TENANT>
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: <TENANT>
spec:
  podSelector: {}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: <TENANT>
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - to: []
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - to:
        - podSelector: {}
```

---

## Dedicated Node Management

### 専用ノードの設定（プロジェクト専用リソース）

メンバー提供サーバーをプロジェクト専用ノードとして割り当てる。

```bash
# ノード追加後に実行
kubectl label nodes <node-name> tenant=project-a
kubectl taint nodes <node-name> tenant=project-a:NoSchedule
```

### ワークロード側のスケジューリング設定

```yaml
# project-a-repo/overlays/production/patches/scheduling.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: any
spec:
  template:
    spec:
      tolerations:
        - key: tenant
          value: project-a
          effect: NoSchedule
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: tenant
                    operator: In
                    values: ["project-a"]
```

### ノード配置戦略

| ワークロード種別 | 配置先 | 理由 |
|---|---|---|
| Flux, Monitoring, Ingress | Proxmox安定ノード（共有） | 高可用性が必要 |
| DB, StatefulSet | Proxmox安定ノード（共有 or 専用） | データ永続性 |
| ステートレスアプリ | メンバー提供ノード（専用） | 停止耐性あり |
| 個人プロジェクト | Proxmox安定ノード（共有） | 常時稼働 |

---

## External Member Node Onboarding

### 前提条件

- メンバーのサーバーにTailscale/HeadscaleまたはWireGuardが導入済み
- K8sクラスタのCNI通信がVPN越しで動作すること（MTU調整が必要な場合あり）

### オンボーディング手順

```
1. メンバーサーバーにVPNエージェントをインストール
   → Ansibleの wireguard ロールで自動化

2. K8s workerノードとしてjoin
   → Talosの場合: talosctl apply-config でmachine config適用
   → K3sの場合:   K3S_URL=... K3S_TOKEN=... k3s agent

3. ノードにラベル・Taint付与
   kubectl label nodes <node> tenant=<project>
   kubectl taint nodes <node> tenant=<project>:NoSchedule

4. infra-repoにノード定義を追加してcommit
   → Fluxがreconcileし、ワークロードが自動配置される
```

### オフボーディング手順

```
1. kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
2. kubectl delete node <node>
3. infra-repoからノード定義を削除してcommit
4. VPN設定からメンバーを除外
```

### セキュリティ考慮事項

- **NodeRestriction admission controller** を有効化（kubeletが自ノード以外のリソースを操作できないようにする）
- Taintにより**他テナントのPodが絶対にスケジュールされない**ことを保証
- 機密性の高いSecret（DBクレデンシャル等）はProxmox側の安定ノードで動かすワークロードに限定
- メンバーのマシンではrootアクセスがあるため、そのノード上のPodのSecretは読み取り可能であることを認識しておく

---

## Monitoring Stack

### Prometheus データソース

| エクスポーター | 対象 | メトリクス例 |
|---|---|---|
| prometheus-pve-exporter | Proxmoxノード | CPU, メモリ, ストレージ, VM状態 |
| kube-state-metrics | K8sリソース | Pod状態, Deployment replica数 |
| Flux controllers /metrics | Flux reconciliation | 同期成功/失敗率, 所要時間 |
| node-exporter | 全ノード | ハードウェアレベルメトリクス |

### Grafana ダッシュボード構成

- **Proxmox Overview**: ノードの健全性、リソース使用率
- **Kubernetes Cluster**: namespace別のリソース消費、Pod状態
- **Flux CD**: Reconciliation状態、Git sync頻度、エラー一覧
- **Tenant Usage**: テナント別のResourceQuota消費率

### アラート設定（Flux Notification Controller）

```yaml
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Provider
metadata:
  name: slack
  namespace: flux-system
spec:
  type: slack
  channel: "#gitops-alerts"
  secretRef:
    name: slack-webhook-url
---
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Alert
metadata:
  name: flux-alert
  namespace: flux-system
spec:
  providerRef:
    name: slack
  eventSeverity: error
  eventSources:
    - kind: Kustomization
      name: "*"
    - kind: HelmRelease
      name: "*"
```

---

## Bootstrap Procedure

### Phase 1: Proxmoxノード構成（Ansible）

```bash
cd infra-repo/ansible
ansible-playbook -i inventory/hosts.yml playbooks/site.yml
```

- ネットワーク設定、DNS、NTP
- WireGuard / Tailscale設定
- prometheus-pve-exporterインストール

### Phase 2: K8s VM作成（Terraform）

```bash
cd infra-repo/terraform
terraform init
terraform plan
terraform apply
```

- Proxmox上にTalos Linux VMを作成（control plane × 3, worker × N）

### Phase 3: K8sクラスタブートストラップ（Talos）

```bash
talosctl gen config my-cluster https://<control-plane-vip>:6443
talosctl apply-config --insecure --nodes <cp1>,<cp2>,<cp3> --file controlplane.yaml
talosctl apply-config --insecure --nodes <w1>,<w2>,...      --file worker.yaml
talosctl bootstrap --nodes <cp1>
talosctl kubeconfig
```

### Phase 4: Flux CDブートストラップ

```bash
flux bootstrap github \
  --owner=<github-org> \
  --repository=infra-repo \
  --path=clusters/main \
  --personal
```

以降のすべての変更はGit経由で行う。

### Phase 5: テナント追加

1. `clusters/main/tenants/` にテナント定義を追加
2. テナント用GitRepositoryを作成
3. commit & push → Fluxが自動reconcile

---

## Day-2 Operations Runbook

### Proxmox ノードIPの変更

```
1. ansible/inventory/hosts.yml のIP更新
2. ansible/roles/proxmox-network/ の設定更新
3. commit & push
4. ansible-playbook playbooks/network-update.yml
```

これにより全ノードのネットワーク設定が一括で更新される。

### 新規テナント追加

```
1. clusters/main/tenants/<new-tenant>.yaml を作成
2. テナント用GitリポジトリのGitRepository/Kustomization定義
3. ResourceQuota, NetworkPolicy, RBAC をテナントに合わせて調整
4. commit & push → Flux自動同期
```

### 外部ノード追加

```
1. メンバーサーバーにVPN + K8s workerセットアップ
2. kubectl label / taint でテナント割り当て
3. clusters/main/nodes/<project>.yaml にノード定義追加
4. commit & push
```

### 障害対応

| 状況 | 対応 |
|---|---|
| Flux reconciliation失敗 | Grafanaダッシュボードで確認 → Gitリポ修正 → push |
| 外部ノード離脱 | PodDisruptionBudgetにより自動フェイルオーバー → drain & delete |
| Proxmoxノード障害 | Proxmox HA有効なら自動マイグレーション → Ansibleで再構築 |
| テナントのリソース枯渇 | ResourceQuota調整 → commit & push |

---

## Notes

- Proxmox WebUIは維持する。K8s以外のVM/LXC管理や緊急時の操作に使用
- Fluxが合わない場合はArgoCDへの切り替えも可能（テナント分離はAppProjectで対応）
- CNIの選択（Cilium推奨）はNetworkPolicyの要件に影響するため、初期構築時に決定する
- 外部ノードのCNI通信はVPN越しのオーバーヘッドがあるため、帯域・レイテンシ要件を事前に確認する
