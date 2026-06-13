# yukulab テナントセットアップガイド

このガイドは、yukulab テナント（YukkuriLaboratory/infra）でワークロードをデプロイするための手順を説明する。

デプロイは2つのフェーズに分けて行う:
- **Phase 1**: AIエージェント（Hermes Agent）を導入し、Discord経由でのインフラ管理を可能にする
- **Phase 2**: プロジェクト管理ツール（Plane）と関連コンポーネント（Garage S3、Cloudflare Tunnel、Grafana）を追加する

## 1. 前提条件

### SOPS + Age 鍵ペアの生成

```bash
# Age鍵ペア生成
age-keygen -o age.key
# 公開鍵が標準出力に表示される（例: age1xxxxxxxxxx...）
```

生成された秘密鍵ファイル（`age.key`）をクラスタ管理者（turtton）に安全な手段で渡す。

管理者が以下を実行してクラスタに登録する:

```bash
kubectl create secret generic sops-age-yukulab \
  --namespace=flux-system \
  --from-file=age.agekey=yukulab-age.key
```

### リポジトリの `.sops.yaml` 設定

`YukkuriLaboratory/infra` リポジトリルートに `.sops.yaml` を作成する。

```yaml
creation_rules:
  - path_regex: \.sops\.(yaml|yml)$
    encrypted_regex: ^(data|stringData)$
    age: "age1xxxxxxxxxx..."  # 生成した公開鍵
```

## 2. 全体リポジトリ構成

```
YukkuriLaboratory/infra/
├── .sops.yaml
├── kustomization.yaml          # 全リソースを参照
├── hermes/
│   ├── secrets.sops.yaml       # Hermes用Secret（API Key, Discord Token, GitHub Token）
│   ├── configmap.yaml          # Hermes設定（config.yaml）
│   ├── config-ops-skill.yaml   # config-opsスキル定義
│   ├── service.yaml            # Hermes Service（StatefulSet必須）
│   ├── network-policy.yaml     # Hermes用外向き通信許可（オプション）
│   ├── statefulset.yaml        # Hermes StatefulSet
│   ├── serviceaccount.yaml     # ServiceAccount
│   ├── rbac.yaml               # RBAC（Pod自己参照権限）
│   └── ingress.yaml            # Tailscale Ingress（オプション）
├── plane/
│   ├── credentials.sops.yaml   # Plane用Secret
│   ├── helm-repository.yaml    # HelmRepository (plane-charts)
│   ├── helm-release.yaml       # HelmRelease (plane-ce)
│   ├── garage-pvc.yaml         # Garageデータ用PVC
│   ├── garage-config.yaml      # Garage設定（garage.toml）
│   └── garage.yaml             # Garage Deployment + Service
├── cloudflared/
│   ├── tunnel-token.sops.yaml  # トンネルトークンSecret
│   ├── network-policy.yaml     # cloudflared用egress許可
│   └── deployment.yaml         # cloudflared Deployment
└── grafana/
    ├── admin-credentials.sops.yaml
    ├── datasource.yaml
    ├── pvc.yaml
    ├── deployment.yaml
    └── service.yaml
```

ルートの `kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  # Hermes Agent
  - hermes/secrets.sops.yaml
  - hermes/configmap.yaml
  - hermes/config-ops-skill.yaml
  - hermes/service.yaml
  - hermes/network-policy.yaml
  - hermes/statefulset.yaml
  - hermes/serviceaccount.yaml
  - hermes/rbac.yaml
  # - hermes/ingress.yaml          # Tailscale Ingress（オプション・管理者と要調整）
  # Plane
  - plane/credentials.sops.yaml
  - plane/helm-repository.yaml
  - plane/helm-release.yaml
  - plane/garage-pvc.yaml
  - plane/garage-config.yaml
  - plane/garage.yaml
  # Cloudflared
  - cloudflared/tunnel-token.sops.yaml
  - cloudflared/network-policy.yaml
  - cloudflared/deployment.yaml
  # Grafana
  - grafana/admin-credentials.sops.yaml
  - grafana/datasource.yaml
  - grafana/pvc.yaml
  - grafana/deployment.yaml
  - grafana/service.yaml
```

---
# Phase 1: Hermes Agent 導入

Phase 1ではAIエージェント（Hermes Agent）のみをデプロイする。Discord連携によるチャットベースのインフラ管理が可能になる。

## 3. Phase 1 注意事項

- **`metadata.namespace` は省略すること** — Flux の `targetNamespace: yukulab` により自動設定される
- **全コンテナに `resources.requests` / `resources.limits` を設定すること** — ResourceQuotaにより未設定のPodは作成が拒否される。特に `limits.cpu` は ResourceQuota の制限対象であるため必ず設定すること
- **`nodeSelector` は省略すること** — Pod のリソース消費が ResourceQuota で制限される

## 4. Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) はNous Research製の軽量AIエージェント基盤。Discord連携、スキルシステム、各種ツール（ファイル操作、Web検索、コード実行等）を備える。

公式イメージ `docker.io/nousresearch/hermes-agent:latest` を使用する。Helmチャートは使用せず、StatefulSet + ConfigMapで直接デプロイする。

### 4.1 Secrets

API Key・トークンはSecret + 環境変数で管理する。HermesはOpenAI互換APIを使用する。

**secrets.sops.yaml** (暗号化前):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: hermes-yukulab-secrets
type: Opaque
stringData:
  OPENAI_API_KEY: "<API Key>"                     # OpenAI互換API Key（例: Crof AI）
  DISCORD_BOT_TOKEN: "<Discordボットトークン>"     # Discord接続用
  GITHUB_TOKEN: "<GitHubトークン>"                 # GitHub API操作用
```

暗号化: `sops --encrypt --in-place hermes/secrets.sops.yaml`

### 4.2 ConfigMap（config.yaml）

Hermesの動作設定は `config.yaml` で管理する。`${OPENAI_API_KEY}` はinit containerでSecretから注入される。

**configmap.yaml**:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: hermes-yukulab-config
data:
  config.yaml: |
    model:
      provider: custom
      default: deepseek-v4-pro-lightning
      base_url: https://crof.ai/v1
      api_key: ${OPENAI_API_KEY}
    personality:
      name: Main Assistant
      restrict_to_workspace: false
    tools:
      append_file: true
      edit_file: true
      read_file: true
      write_file: true
      list_dir: true
      exec: true
      web: true
      web_fetch: true
      send_file: true
      spawn: true
      subagent: true
      message: true
      cron: true
      skills: true
      find_skills: true
      install_skill: true
    skills:
      external_dirs:
        - /hermes/skills
    approvals:
      mode: auto
    logging:
      level: info
```

> **備考**: Hermesは複数エージェントの並列実行やバインディング（チャンネル→エージェントルーティング）をサポートしている。必要に応じて `personality` や `tools` の設定をカスタマイズすること。

### 4.3 Skills（スキル定義）

ConfigMapでスキルを定義し、StatefulSetのinit containerでPVCに展開する。

**config-ops-skill.yaml** — インフラ設定管理スキル:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: hermes-config-ops-skill
data:
  SKILL.md: |
    ---
    name: config-ops
    description: Kubernetesマニフェストの設定変更とGitOps PR作成
    version: 1.0.0
    ---

    # config-ops

    あなたはインフラ設定管理エージェントです。

    ## 役割

    Kubernetesマニフェストの設定変更を安全にテストし、GitOpsリポジトリにPRを作成する。

    ## ワークフロー

    1. Pod内でマニフェスト変更をテスト
    2. 成功したらPR作成（gh CLI使用）
```

下記のStatefulSet例では `config-ops` スキルのみをprojected volumeに含めている。

### 4.4 ServiceAccount + RBAC

Hermesが自身のPod情報を参照できるよう、最小権限のRBACを設定する。

**serviceaccount.yaml**:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hermes-yukulab
```

**rbac.yaml**:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: hermes-yukulab-self-access
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
    resourceNames: ["hermes-yukulab-0"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: hermes-yukulab-self-access
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: hermes-yukulab-self-access
subjects:
  - kind: ServiceAccount
    name: hermes-yukulab
```

### 4.5 StatefulSet

**statefulset.yaml**:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: hermes-yukulab
  labels:
    app: hermes-yukulab
spec:
  serviceName: hermes-yukulab
  replicas: 1
  selector:
    matchLabels:
      app: hermes-yukulab
  template:
    metadata:
      labels:
        app: hermes-yukulab
    spec:
      serviceAccountName: hermes-yukulab
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: init-config
          image: busybox:latest
          command:
            - sh
            - -c
            - |
              sed 's|\${OPENAI_API_KEY}|'"$OPENAI_API_KEY"'|g' /template/config.yaml > /opt/data/config.yaml && \
              chmod 644 /opt/data/config.yaml
          env:
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: hermes-yukulab-secrets
                  key: OPENAI_API_KEY
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 100m
              memory: 128Mi
          volumeMounts:
            - name: config-template
              mountPath: /template
            - name: data
              mountPath: /opt/data
        - name: init-skills
          image: busybox:latest
          command:
            - sh
            - -c
            - |
              mkdir -p /opt/data/skills && \
              cp -r /skill-seed/* /opt/data/skills/ && \
              chmod -R 755 /opt/data/skills
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 100m
              memory: 128Mi
          volumeMounts:
            - name: skill-seed
              mountPath: /skill-seed
            - name: data
              mountPath: /opt/data
        - name: install-tools
          image: alpine:latest
          command:
            - sh
            - -c
            - |
              mkdir -p /opt/data/bin && \
              apk add --no-cache curl >/dev/null 2>&1 && \
              curl -sLO "https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl" && \
              chmod +x kubectl && \
              mv kubectl /opt/data/bin/kubectl
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 256Mi
          volumeMounts:
            - name: data
              mountPath: /opt/data
      containers:
        - name: hermes
          image: docker.io/nousresearch/hermes-agent:latest
          imagePullPolicy: Always
          tty: true
          stdin: true
          args:
            - gateway
            - run
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities:
              drop:
                - "ALL"
              add:
                - SETUID
                - SETGID
                - CHOWN
                - DAC_OVERRIDE
                - FOWNER
                - SETPCAP
          env:
            - name: HERMES_HOME
              value: /opt/data
            - name: PATH
              value: /opt/data/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: hermes-yukulab-secrets
                  key: OPENAI_API_KEY
            - name: OPENAI_BASE_URL
              value: "https://crof.ai/v1"
            - name: DISCORD_BOT_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hermes-yukulab-secrets
                  key: DISCORD_BOT_TOKEN
            - name: DISCORD_ALLOWED_USERS
              value: "<DiscordユーザーID（カンマ区切り）>"
            - name: DISCORD_REQUIRE_MENTION
              value: "true"
            - name: GITHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hermes-yukulab-secrets
                  key: GITHUB_TOKEN
            - name: GH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hermes-yukulab-secrets
                  key: GITHUB_TOKEN
          ports:
            - name: http
              containerPort: 8080
              protocol: TCP
          volumeMounts:
            - name: data
              mountPath: /opt/data
            - name: run
              mountPath: /run
          resources:
            requests:
              cpu: 50m
              memory: 512Mi
            limits:
              cpu: 500m
              memory: 1Gi
          livenessProbe:
            exec:
              command:
                - sh
                - -c
                - "ps aux | grep -q '[h]ermes'"
            initialDelaySeconds: 60
            periodSeconds: 30
          readinessProbe:
            exec:
              command:
                - sh
                - -c
                - "ps aux | grep -q '[h]ermes'"
            initialDelaySeconds: 30
            periodSeconds: 10
      volumes:
        - name: config-template
          configMap:
            name: hermes-yukulab-config
        - name: skill-seed
          projected:
            sources:
              - configMap:
                  name: hermes-config-ops-skill
                  items:
                    - key: SKILL.md
                      path: config-ops/SKILL.md
        - name: run
          emptyDir:
            medium: Memory
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: longhorn
        resources:
          requests:
            storage: 5Gi
```

> **注意**: `DISCORD_BOT_TOKEN` は環境変数で直接注入される。`DISCORD_ALLOWED_USERS` に許可するDiscordユーザーIDをカンマ区切りで指定する。`DISCORD_REQUIRE_MENTION: "true"` の場合、メンション付きメッセージにのみ応答する。

> **注意**: Hermes StatefulSetはPVC（`volumeClaimTemplates`）を使用し、データとスキルを永続化する。Pod再起動後もスキルの変更や会話履歴が維持される。

> **注意**: `install-tools` init container は `kubectl` のみをインストールする。config-ops スキルで `gh` CLI を使用する場合は、`install-tools` に以下の行を追加して `gh` をインストールすること:
> ```bash
> # install-tools の curl 行の後に追加
> curl -sLO "https://github.com/cli/cli/releases/download/v2.67.0/gh_2.67.0_linux_amd64.tar.gz" && \
>   tar xzf gh_2.67.0_linux_amd64.tar.gz && \
>   mv gh_2.67.0_linux_amd64/bin/gh /opt/data/bin/gh && \
>   rm -rf gh_2.67.0_linux_amd64 gh_2.67.0_linux_amd64.tar.gz
> ```
> または、環境変数 `GITHUB_TOKEN` / `GH_TOKEN` を使用して GitHub REST API を直接呼び出すようにスキルを修正することもできる。

### 4.6 NetworkPolicy

HermesはDiscord、GitHub、Crof AI（OpenAI互換API）へ外部通信を行う。テナントnamespaceのdefault-deny NetworkPolicy下では、以下のEgressルールが必要。

**network-policy.yaml**:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-hermes-egress
spec:
  podSelector:
    matchLabels:
      app: hermes-yukulab
  policyTypes:
    - Egress
  egress:
    # 外部HTTPS通信（Discord, GitHub, Crof AI API等）
    - ports:
        - protocol: TCP
          port: 443
    # DNS解決
    - ports:
        - protocol: TCP
          port: 53
        - protocol: UDP
          port: 53
    # namespace内のその他サービスへのアクセス
    - to:
        - podSelector: {}
      ports:
        - protocol: TCP
```

### 4.7 Service + Ingress (Tailscale経由でWeb UIにアクセスする場合)

HermesのHTTPポート（8080）にアクセスするには、Serviceリソースを作成する。

**service.yaml**:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: hermes-yukulab
spec:
  selector:
    app: hermes-yukulab
  ports:
    - port: 8080
      targetPort: http
      protocol: TCP
```

Tailscale Ingress 経由で公開する場合は以下を参照。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: hermes-yukulab
  annotations:
    tailscale.com/funnel: "false"
    tailscale.com/tags: "tag:hermes"
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: hermes-yukulab
      port:
        number: 8080
  tls:
    - hosts:
        - hermes-yukulab
```

注意: Tailscale Ingress の作成にはクラスタスコープの権限が必要な場合がある。テナント deployer SA では作成できない可能性があるため、管理者側での対応が必要になることがある。上記のServiceが事前に作成されていることを確認すること。

## 5. Phase 1 リポジトリ構成（最小）

Phase 1では `hermes/` ディレクトリのみを作成する。

```
YukkuriLaboratory/infra/
├── .sops.yaml
├── kustomization.yaml
└── hermes/
    ├── secrets.sops.yaml
    ├── configmap.yaml
    ├── config-ops-skill.yaml
├── service.yaml
    ├── network-policy.yaml
    ├── statefulset.yaml
    ├── serviceaccount.yaml
    ├── rbac.yaml
    └── ingress.yaml          # オプション
```

ルートの `kustomization.yaml` は Hermes 関連のみを参照:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  # Hermes Agent
  - hermes/secrets.sops.yaml
  - hermes/configmap.yaml
  - hermes/config-ops-skill.yaml
  - hermes/service.yaml
  - hermes/network-policy.yaml
  - hermes/statefulset.yaml
  - hermes/serviceaccount.yaml
  - hermes/rbac.yaml
  # - hermes/ingress.yaml    # オプション
```

## 6. Phase 1 用制約事項

Phase 1（Hermesのみ）の推奨最小ResourceQuota:

| 項目 | 推奨最小値 |
|------|-----------|
| requests.cpu | 500m |
| requests.memory | 2Gi |
| limits.cpu | 2 |
| limits.memory | 4Gi |
| PVC数 | 2 |

- 実際の値は管理者と調整の上設定すること
- 上記は Hermes Agent 1インスタンス + オーバーヘッド分を含む

## 7. Phase 1 デプロイ手順

1. `YukkuriLaboratory/infra` リポジトリをpublic化する（またはFlux GitRepositoryの認証を設定する）
2. Age鍵ペアを生成し、秘密鍵を管理者に渡す（Section 1参照）
3. `.sops.yaml` を設定し、Secret を暗号化する
4. `hermes/` ディレクトリと上記のマニフェストを作成し、commit & push する
5. 管理者がクラスタにAge秘密鍵Secretを登録する:
   ```bash
   kubectl create secret generic sops-age-yukulab \
     --namespace=flux-system \
     --from-file=age.agekey=yukulab-age.key
   ```
6. 管理者が `yukulab-workloads` Kustomization の `suspend: false` に変更してcommit
7. Flux が自動的にreconcileし、Hermes Agent がデプロイされる

## 8. Phase 1 検証

```bash
# Flux Kustomization の状態確認
flux get kustomizations yukulab-tenant-setup
flux get kustomizations yukulab-workloads

# GitRepository の fetch 状態確認
flux get sources git yukulab-infra

# テナントリソースの確認
kubectl get namespace yukulab
kubectl get serviceaccount -n flux-system yukulab-deployer
kubectl get rolebinding -n yukulab yukulab-admin
kubectl get resourcequota -n yukulab

# Hermes Agent StatefulSet の確認
kubectl get statefulset -n yukulab hermes-yukulab
kubectl get pods -n yukulab -l app=hermes-yukulab
kubectl logs -n yukulab -l app=hermes-yukulab --tail=20
```

DiscordでHermes Agentがオンラインになっていることを確認する。

---

# Phase 2: Plane 導入

Phase 2ではプロジェクト管理ツール Plane と、その関連コンポーネント（Garage S3、Cloudflare Tunnel、Grafana）を追加デプロイする。Phase 1でHermes Agentが稼働している状態を前提とする。

## 9. Phase 2 注意事項

- **`metadata.namespace` は省略すること** — Phase 1と同様
- **全コンテナに `resources.requests` / `resources.limits` を設定すること** — Phase 1と同様
- **External PostgreSQL / Redis が必要** — Plane はデータベースに PostgreSQL、キャッシュに Redis を必要とする。これらは chart 外部で別途デプロイすること（StatefulSet またはマネージドサービス）。バックアップ/リストア方針も合わせて検討すること
- **Plane は Garage のアクセスキーが正しく設定されるまでファイルアップロードが動作しない** — アプリケーション自体の起動は S3 認証なしでも進むが、添付ファイル操作時にエラーとなる
- **NetworkPolicy は Cilium により有効** — cloudflared の外部通信は明示的に許可する必要がある

## 10. Phase 2 リポジトリ追加構成

Phase 1の構成に加えて、以下のディレクトリ・ファイルを追加する。

```
YukkuriLaboratory/infra/
├── ...                   # Phase 1 のファイル
├── plane/
│   ├── credentials.sops.yaml
│   ├── helm-repository.yaml
│   ├── helm-release.yaml
│   ├── garage-pvc.yaml
│   ├── garage-config.yaml
│   └── garage.yaml
├── cloudflared/
│   ├── tunnel-token.sops.yaml
│   ├── network-policy.yaml
│   └── deployment.yaml
└── grafana/              # オプション
    ├── admin-credentials.sops.yaml
    ├── datasource.yaml
    ├── pvc.yaml
    ├── deployment.yaml
    └── service.yaml
```

`kustomization.yaml` に Phase 2 のリソースを追加:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  # Phase 1: Hermes Agent
  - hermes/...
  # Phase 2: Plane
  - plane/credentials.sops.yaml
  - plane/helm-repository.yaml
  - plane/helm-release.yaml
  - plane/garage-pvc.yaml
  - plane/garage-config.yaml
  - plane/garage.yaml
  # Cloudflared
  - cloudflared/tunnel-token.sops.yaml
  - cloudflared/network-policy.yaml
  - cloudflared/deployment.yaml
  # Grafana（オプション）
  - grafana/...
```

## 11. Plane

[Plane](https://plane.so) はオープンソースのプロジェクト管理ツール。公式の Helm チャート (`plane-ce`) を使用して、Flux CD の `HelmRelease` でデプロイする。

### 11.1 HelmRepository

まず、Plane の Helm チャートリポジトリを登録する。

**helm-repository.yaml**:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: plane-charts
spec:
  interval: 1h
  url: https://helm.plane.so
```

### 11.2 credentials.sops.yaml

データベースやストレージの接続情報を設定する。RabbitMQ は chart 組み込みを使用するため、ここには含めない。

**credentials.sops.yaml** (暗号化前):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: plane-credentials
type: Opaque
stringData:
  SECRET_KEY: "<ランダムなシークレットキー>"
  LIVE_SERVER_SECRET_KEY: "<ランダムなシークレットキー>"
  DATABASE_URL: "postgresql://plane:<パスワード>@plane-pgdb:5432/plane"
  REDIS_URL: "redis://plane-redis:6379/0"
  AWS_ACCESS_KEY_ID: "<Garageアクセスキー>"
  AWS_SECRET_ACCESS_KEY: "<Garageシークレットキー>"
```

### 11.3 HelmRelease

`HelmRelease` を使用して Plane の各コンポーネントをデプロイする。chart のデフォルト PostgreSQL・Redis・MinIO は無効化し、外部の Garage と Plane 用 PostgreSQL/Redis を使用する。RabbitMQ は chart 組み込みを使用する。`ingress.appHost` はchart内部で `WEB_URL` や `CORS_ALLOWED_ORIGINS` の生成に使われるため、Ingressを無効化する場合でも設定が必要。

**helm-release.yaml**:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: plane
spec:
  interval: 30m
  releaseName: plane  # Service名が plane-web, plane-api 等になる
  chart:
    spec:
      chart: plane-ce
      version: "1.5.0"  # バージョンを固定
      sourceRef:
        kind: HelmRepository
        name: plane-charts
  valuesFrom:
    - kind: Secret
      name: plane-credentials
      valuesKey: SECRET_KEY
      targetPath: env.secret_key
    - kind: Secret
      name: plane-credentials
      valuesKey: LIVE_SERVER_SECRET_KEY
      targetPath: env.live_server_secret_key
    - kind: Secret
      name: plane-credentials
      valuesKey: DATABASE_URL
      targetPath: env.pgdb_remote_url
    - kind: Secret
      name: plane-credentials
      valuesKey: REDIS_URL
      targetPath: env.remote_redis_url
    - kind: Secret
      name: plane-credentials
      valuesKey: AWS_ACCESS_KEY_ID
      targetPath: env.aws_access_key
    - kind: Secret
      name: plane-credentials
      valuesKey: AWS_SECRET_ACCESS_KEY
      targetPath: env.aws_secret_access_key
  values:
    postgres:
      local_setup: false
    redis:
      local_setup: false
    minio:
      local_setup: false
    ingress:
      enabled: false
      appHost: "plane.yukulab.example.com"
    env:
      aws_region: "garage"
      aws_s3_endpoint_url: "http://plane-garage:3900"
      docstore_bucket: "plane-uploads"
    rabbitmq:
      local_setup: true
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
    api:
      replicas: 1
      cpuRequest: 50m
      memoryRequest: 128Mi
      cpuLimit: 300m
      memoryLimit: 512Mi
    web:
      replicas: 1
      cpuRequest: 50m
      memoryRequest: 128Mi
      cpuLimit: 200m
      memoryLimit: 256Mi
    space:
      replicas: 1
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
    admin:
      replicas: 1
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
    live:
      replicas: 1
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
    worker:
      replicas: 1
      cpuRequest: 50m
      memoryRequest: 128Mi
      cpuLimit: 300m
      memoryLimit: 512Mi
    beatworker:
      replicas: 1
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
  postRenderers:
    - kustomize:
        patches:
          - target:
              kind: Job
              name: "plane-api-migrate.*"
            patch: |
              - op: add
                path: /spec/template/spec/containers/0/resources
                value:
                  requests:
                    cpu: 50m
                    memory: 128Mi
                  limits:
                    cpu: 300m
                    memory: 512Mi
```

> **注意**: `postRenderers` は Flux HelmRelease の機能で、Helm テンプレートレンダリング後に Kustomize パッチを適用する。migrator Job の名前は `{releaseName}-api-migrate-{revision}` の形式になるため、正規表現でマッチさせている。chart が将来 migrator に `resources` フィールドを追加した場合は `postRenderers` を削除できる。

> **代替案**: `postRenderers` の代わりに、namespace に `LimitRange` を設定してデフォルトリソースを自動割り当てする方法もある。ただしすべての Pod に影響するため、意図しないリソース割り当てに注意。

> **注意 (ServiceAccount)**: plane-ce chart はデフォルトで `automountServiceAccountToken: true` のまま Pod を作成する。セキュリティを強化したい場合は `postRenderers` で各 Deployment/Job の `spec.template.spec.automountServiceAccountToken` を `false` に上書きするか、ServiceAccount に `automount: false` を設定すること。

> **注意**: chart バージョン `1.5.0` 時点の values 構造に基づく。アップグレード時は `helm show values plane-ce --version <new> --repo https://helm.plane.so` で差分を確認すること。

### 11.4 Garage

[Garage](https://garagehq.deuxfleurs.fr/) は公式 Helm チャートに含まれないため、個別のマニフェストで管理する。

**garage-pvc.yaml**:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: plane-garage-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 5Gi
```

**garage-config.yaml**:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: garage-config
data:
  garage.toml: |
    metadata_dir = "/data/meta"
    data_dir = "/data/blocks"
    db_engine = "lmdb"
    
    replication_factor = 1
    
    rpc_bind_addr = "[::]:3901"
    rpc_secret_file = "/data/rpc-secret"
    allow_world_readable_secrets = true
    
    [s3_api]
    api_bind_addr = "[::]:3900"
    s3_region = "garage"
    root_domain = ".s3.garage.localhost"
    
    [s3_web]
    bind_addr = "[::]:3902"
    root_domain = ".web.garage.localhost"
    
    [admin]
    api_bind_addr = "127.0.0.1:3903"
```

**garage.yaml**:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: plane-garage
spec:
  replicas: 1
  selector:
    matchLabels:
      app: plane-garage
  template:
    metadata:
      labels:
        app: plane-garage
    spec:
      automountServiceAccountToken: false
      initContainers:
        - name: init-rpc-secret
          image: busybox:1.36
          command: ["sh", "-c", "if [ ! -f /data/rpc-secret ]; then head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \\n' > /data/rpc-secret; fi"]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 100m
              memory: 128Mi
          volumeMounts:
            - name: data
              mountPath: /data
      containers:
        - name: garage
          image: dxflrs/garage:v1.1.0
          ports:
            - name: s3-api
              containerPort: 3900
            - name: rpc
              containerPort: 3901
            - name: web
              containerPort: 3902
          volumeMounts:
            - name: data
              mountPath: /data
            - name: config
              mountPath: /etc/garage.toml
              subPath: garage.toml
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 200m
              memory: 256Mi
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: plane-garage-data
        - name: config
          configMap:
            name: garage-config
---
apiVersion: v1
kind: Service
metadata:
  name: plane-garage
spec:
  selector:
    app: plane-garage
  ports:
    - name: s3-api
      port: 3900
      targetPort: 3900
```

> **初期セットアップ**: Garageデプロイ後、以下の手順でバケットとアクセスキーを設定する。
>
> ```bash
> # クラスタレイアウト確認
> kubectl exec -it deploy/plane-garage -- garage layout show
> # ノードにキャパシティを割り当て
> kubectl exec -it deploy/plane-garage -- garage layout assign <node-id> -z dc1 -c 5GB
> kubectl exec -it deploy/plane-garage -- garage layout apply --version 1
> # バケット作成
> kubectl exec -it deploy/plane-garage -- garage bucket create plane-uploads
> # アクセスキー作成・バケットに紐付け
> kubectl exec -it deploy/plane-garage -- garage key create plane-key
> kubectl exec -it deploy/plane-garage -- garage bucket allow --read --write --owner plane-uploads --key plane-key
> ```
>
> 生成されたアクセスキーID・シークレットを `credentials.sops.yaml` の `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` に設定する。

> **データ耐久性について**: Garage は `replication_factor=1`・単一PVC で動作しており、HA構成ではない。ノード障害やPVC損失時に Plane の添付ファイルが失われる可能性がある。業務上重要なデータは別途バックアップを取ること。

### 11.5 Plane + Garage リソース合計見積もり

| コンポーネント | requests.cpu | requests.memory | limits.cpu | limits.memory |
|---------------|-------------|-----------------|-----------|--------------|
| api           | 50m         | 128Mi           | 300m      | 512Mi        |
| web           | 50m         | 128Mi           | 200m      | 256Mi        |
| space         | 25m         | 64Mi            | 100m      | 128Mi        |
| admin         | 25m         | 64Mi            | 100m      | 128Mi        |
| live          | 25m         | 64Mi            | 100m      | 128Mi        |
| worker        | 50m         | 128Mi           | 300m      | 512Mi        |
| beatworker    | 25m         | 64Mi            | 100m      | 128Mi        |
| rabbitmq      | 25m         | 64Mi            | 100m      | 128Mi        |
| migrator (Job)| 50m         | 128Mi           | 300m      | 512Mi        |
| Garage        | 50m         | 128Mi           | 200m      | 256Mi        |
| **小計**      | **375m**    | **~960Mi**      | **1600m** | **~2.6Gi**   |

> **注意**: 上記には PostgreSQL・Redis のリソースは含まれていない。Phase 1 の Hermes Agent のリソースは Phase 1 用制約事項を参照。

## 12. Cloudflare Tunnel

Plane を Cloudflare Tunnel 経由で公開する場合、パスベースのルーティングが必要になる。

### ルーティング設定

Plane は以下のパスを適切なサービスに振り分ける必要がある（release名 = `plane` の場合）:
- `/` → `plane-web` (port 3000)
- `/api` → `plane-api` (port 8000)
- `/auth` → `plane-api` (port 8000)
- `/live/` → `plane-live` (port 3000)
- `/spaces` → `plane-space` (port 3000)
- `/god-mode` → `plane-admin` (port 3000)

Cloudflare Tunnel で公開する場合、上記のパスベースルーティングを Cloudflare Dashboard の Tunnel 設定画面（Public Hostname タブ）で定義する。
または、chart の `ingress.enabled: true` + `ingress.ingressClass` で Ingress Controller（nginx等）を使い、cloudflared はルートドメイン宛てに単一サービスへ転送する方法もある。後者の方がシンプルだが、テナント ResourceQuota の制限に注意すること。

> **注意 (GitOps drift)**: `TUNNEL_TOKEN` を使用したリモート管理モードでは、ルーティング設定は Cloudflare Dashboard 上で管理される（Git 管理外）。設定変更の追跡が必要な場合は `cloudflared` の `--config` フラグでローカル設定ファイルを使用するか、Dashboard の変更を手動でドキュメントに反映すること。

**tunnel-token.sops.yaml** (暗号化前):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflared-tunnel-token
type: Opaque
stringData:
  token: <Cloudflare Tunnelトークン>
```

**network-policy.yaml** — cloudflared用のEgress許可:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-cloudflared-egress
spec:
  podSelector:
    matchLabels:
      app: cloudflared
  policyTypes:
    - Egress
  egress:
    - ports:
        - protocol: TCP
          port: 443
        - protocol: UDP
          port: 443
        - protocol: TCP
          port: 7844
        - protocol: UDP
          port: 7844
    - ports:
        - protocol: TCP
          port: 53
        - protocol: UDP
          port: 53
    - to:
        - podSelector: {}
      ports:
        - protocol: TCP
```

**deployment.yaml** — `--protocol http2` を必ず指定すること:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cloudflared
  template:
    metadata:
      labels:
        app: cloudflared
    spec:
      automountServiceAccountToken: false
      containers:
        - name: cloudflared
          image: cloudflare/cloudflared:2025.10.0
          args:
            - tunnel
            - --no-autoupdate
            - --metrics
            - 0.0.0.0:2000
            - --protocol
            - http2
            - run
          env:
            - name: TUNNEL_TOKEN
              valueFrom:
                secretKeyRef:
                  name: cloudflared-tunnel-token
                  key: token
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 100m
              memory: 128Mi
```

## 13. Grafana（オプション）

既存のPrometheusに接続するスタンドアロンGrafanaをデプロイする場合は、[テナントセットアップガイド](./tenant-setup-guide.md) の Grafana セクションを参照。

## 14. 全体制約事項（Phase 1 + Phase 2 合計）

| 項目 | 制限値 |
|------|--------|
| requests.cpu | 4 |
| requests.memory | 8Gi |
| limits.cpu | 8 |
| limits.memory | 16Gi |
| PVC数 | 10 |

- **クラスタスコープのリソースは作成不可** — ClusterRole, CustomResourceDefinition, PersistentVolume 等
- **全Podに `resources.requests` / `resources.limits` を設定すること** — 未設定のPodはResourceQuotaにより作成が拒否される。すべてのコンテナに `limits.cpu` を明示的に設定すること
- **`metadata.namespace` は省略推奨** — Flux の `targetNamespace: yukulab` で自動設定される

## 15. Phase 2 デプロイ手順

1. Phase 1（Hermes Agent）がデプロイ済みであることを確認する
2. External PostgreSQL / Redis をデプロイする（別途用意）
3. `plane/`, `cloudflared/`, `grafana/` のマニフェストを作成する
   - **注意**: `credentials.sops.yaml` の `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` は初回デプロイ時にはまだ不明のため、プレースホルダー値を設定しておく
4. commit & push する
5. Flux が自動的にreconcileし、全コンポーネントがデプロイされる
6. **Garage の初期セットアップ**（Section 11.4「初期セットアップ」手順を実行）:
   - `kubectl exec` で Garage の layout assign/apply を実行
   - バケット作成、アクセスキー作成
7. 生成されたアクセスキーで `credentials.sops.yaml` を更新し、再暗号化して commit & push
8. Flux が再reconcileし、Plane が正しい S3 認証情報で起動する
9. Cloudflare Tunnel のルーティング設定を行う（Dashboard または Ingress）

## 16. Phase 2 検証

```bash
# 全テナントワークロードの確認
kubectl get all -n yukulab

# HelmRelease の状態確認
flux get helmreleases -n yukulab

# Plane の各コンポーネント確認
kubectl get pods -n yukulab -l app.kubernetes.io/instance=plane
kubectl get svc -n yukulab -l app.kubernetes.io/instance=plane

# migrator Job の確認（初回デプロイ・アップグレード時）
kubectl get jobs -n yukulab

# Garage の状態確認
kubectl get pods -n yukulab -l app=plane-garage
kubectl exec -it deploy/plane-garage -- garage status

# cloudflared の状態確認
kubectl get pods -n yukulab -l app=cloudflared
```

Plane の Web UI にアクセスして動作確認する。

## 17. 全体注意事項

- NetworkPolicy は Cilium により**有効**である。cloudflared等の外部通信はテナントリポジトリ側のNetworkPolicyで明示的に許可する必要がある
- `yukulab-workloads` Kustomization は初期状態で `suspend: true`。テナントリポジトリとSOPS鍵が準備できてから解除する
- テナントリポジトリがprivateの場合、GitRepositoryのfetchが認証エラーで失敗する
- Plane の Helm チャートバージョンは固定し、アップグレード前に `helm diff` 等で差分を確認すること
- Phase 1 から Phase 2 への移行は、`kustomization.yaml` にリソースを追加 → commit & push するだけでよい。Flux が自動的に差分を検出してデプロイする
- Hermes Agent の ConfigMap（`config.yaml`）や StatefulSet の環境変数は環境に合わせてカスタマイズが必要（モデル選択、Discord設定、GitHub設定等）
- Hermes StatefulSet の `volumeClaimTemplates` で作成される PVC は削除するとデータが消失する。スキルの再初期化やデータ移行が必要な場合は注意すること
- yukulab namespace は他テナントの namespace とは NetworkPolicy で隔離されるが、namespace 内の Pod 同士は相互通信が可能（intra-tenant マイクロセグメンテーションなし）
