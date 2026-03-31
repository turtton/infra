# yukulab テナントセットアップガイド

このガイドは、yukulab テナント（YukkuriLaboratory/infra）でワークロードをデプロイするための手順を説明する。

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

## 2. リポジトリ構成例

```
YukkuriLaboratory/infra/
├── .sops.yaml
├── kustomization.yaml          # 全リソースを参照
├── openclaw/
│   ├── secrets.sops.yaml       # OpenClaw用Secret（API鍵等）
│   ├── instance.yaml           # OpenClawInstance CR
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
  # OpenClaw
  - openclaw/secrets.sops.yaml
  - openclaw/instance.yaml
  # - openclaw/ingress.yaml        # Tailscale Ingress（オプション・管理者と要調整）
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

## 3. 共通注意事項

- **`metadata.namespace` は省略すること** — Flux の `targetNamespace: yukulab` により自動設定される
- **全コンテナに `resources.requests` / `resources.limits` を設定すること** — ResourceQuotaにより未設定のPodは作成が拒否される。特に `limits.cpu` は ResourceQuota の制限対象であるため必ず設定すること
- **`nodeSelector` は省略すること** — Pod のリソース消費が ResourceQuota で制限される

## 4. OpenClaw

**注意**: 本プロジェクトで使用するオペレーター名は OpenClaw である。以前 zeroclaw と呼ばれていたものと同一であるが、設定ファイルやリソース名は OpenClaw に統一すること。

### OpenClawInstance CR

yukulab テナントの deployer SA には `openclaw.rocks` API グループへの CRUD 権限が付与されている（aggregate-to-admin ClusterRole経由）。テナントリポジトリから直接 OpenClawInstance CR を作成できる。

**secrets.sops.yaml** (暗号化前):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: openclaw-yukulab-secrets
type: Opaque
stringData:
  # 共有シークレット（クラスタ側のopenclaw-shared-secretsに相当）
  # テナントnamespaceでは共有Secretにアクセスできないため、自前で用意する
  COPILOT_GITHUB_TOKEN: "<GitHub Copilotトークン>"  # github-copilotプロバイダ用（必須）
  VOYAGE_API_KEY: "<Voyage APIキー>"                  # memorySearch用
  BRAVE_API_KEY: "<Brave Search APIキー>"              # Web検索用
  # インスタンス固有
  DISCORD_BOT_TOKEN: "<Discordボットトークン>"
  GITHUB_TOKEN: "<GitHubトークン>"
```

暗号化: `sops --encrypt --in-place openclaw/secrets.sops.yaml`

**instance.yaml** — 以下はlepiインスタンスを参考にした設定例。必要に応じてカスタマイズすること:

```yaml
apiVersion: openclaw.rocks/v1alpha1
kind: OpenClawInstance
metadata:
  name: yukulab
spec:
  envFrom:
    - secretRef:
        name: openclaw-yukulab-secrets

  env:
    - name: GITHUB_REPO_URL
      value: "https://github.com/YukkuriLaboratory/<リポジトリ>.git"
    - name: GITHUB_BASE_BRANCH
      value: "main"

  storage:
    persistence:
      enabled: true
      size: 5Gi
      storageClass: longhorn

  config:
    mergeMode: overwrite
    raw:
      auth:
        profiles:
          github-copilot:github:
            provider: github-copilot
            mode: token
      models:
        providers:
          voyage:
            baseUrl: "https://api.voyageai.com/v1"
            models:
              - id: voyage-4
                name: voyage-4
      agents:
        defaults:
          model:
            primary: github-copilot/claude-sonnet-4.6
          memorySearch:
            provider: voyage
            remote:
              batch:
                enabled: false
            model: voyage-4
          compaction:
            mode: safeguard
          maxConcurrent: 4
          subagents:
            maxConcurrent: 8
        list:
          - id: main
            default: true
      tools:
        profile: full
        web:
          search:
            enabled: true
          fetch:
            enabled: true
        exec:
          security: full
          ask: "off"
      approvals:
        exec:
          enabled: false
      channels:
        discord:
          enabled: true
          groupPolicy: allowlist
          dmPolicy: allowlist
          allowFrom:
            - "<DiscordユーザーID>"
          guilds:
            "<DiscordサーバーID>":
              channels:
                "<DiscordチャンネルID>":
                  allow: true
          execApprovals:
            enabled: false
      gateway:
        controlUi:
          allowedOrigins:
            - "https://openclaw-yukulab.taile2777.ts.net"
        auth:
          mode: token
          allowTailscale: true
      plugins:
        entries:
          discord:
            enabled: true

  security:
    networkPolicy:
      allowedIngressNamespaces:
        - tailscale
```

**ingress.yaml** (Tailscale経由でControl UIにアクセスする場合):

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: openclaw-yukulab
  annotations:
    tailscale.com/funnel: "false"
    tailscale.com/tags: "tag:openclaw"
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: yukulab
      port:
        number: 18789
  tls:
    - hosts:
        - openclaw-yukulab
```

注意: Tailscale Ingress の作成にはクラスタスコープの権限が必要な場合がある。テナント deployer SA では作成できない可能性があるため、管理者側での対応が必要になることがある。

## 5. Plane

[Plane](https://plane.so) はオープンソースのプロジェクト管理ツール。公式の Helm チャート (`plane-ce`) を使用して、Flux CD の `HelmRelease` でデプロイする。

### HelmRepository

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

### credentials.sops.yaml

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

### HelmRelease

`HelmRelease` を使用して Plane の各コンポーネントをデプロイする。chart のデフォルト PostgreSQL・Redis・MinIO は無効化し、外部の Garage と Plane 用 PostgreSQL/Redis を使用する。RabbitMQ は chart 組み込みを使用する。`ingress.appHost` はchart内部で `WEB_URL` や `CORS_ALLOWED_ORIGINS` の生成に使われるため、Ingressを無効化する場合でも設定が必要。

**helm-release.yaml**:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: plane
spec:
  interval: 30m
  releaseName: plane  # Service名が plane-web, plane-api 等になる（Section 6のルーティングと一致させる）
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
    # 組み込みコンポーネントを無効化（RabbitMQ は chart 組み込みを使用）
    postgres:
      local_setup: false
    redis:
      local_setup: false
    minio:
      local_setup: false
    # Ingress設定（chart内部でWEB_URL, CORS等を生成する）
    ingress:
      enabled: false  # Cloudflare Tunnel経由のためchart Ingressは無効化
      appHost: "plane.yukulab.example.com"  # WEB_URLの生成元として必要
    # 外部ストレージ (Garage) 設定
    env:
      aws_region: "garage"
      aws_s3_endpoint_url: "http://plane-garage:3900"
      docstore_bucket: "plane-uploads"
    # RabbitMQ リソース設定
    rabbitmq:
      local_setup: true
      cpuRequest: 25m
      memoryRequest: 64Mi
      cpuLimit: 100m
      memoryLimit: 128Mi
    # リソース設定（テナントのResourceQuota内に収めること）
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
  # migrator Job にリソース制限を注入（chart v1.5.0 はネイティブ非対応）
  # ResourceQuota 環境では resources 未指定の Pod が admission rejection されるため必須
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

> **注意**: このガイドでは PostgreSQL と Redis を chart の外部サービスとして設定しているが、それらのデプロイ方法は記載していない。別途 StatefulSet またはマネージドサービスを用意する必要がある。ResourceQuota の見積もりにはそれらのリソースも含めること。PostgreSQL はバックアップ/リストア方針も合わせて検討すること — Plane の主要データ（プロジェクト、Issue、設定等）はすべて PostgreSQL に保存されるため、Garage（添付ファイル）よりもデータ損失の影響が大きい。

### Garage

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
> **注意**: Garage admin API へのアクセスキー情報は平文で出力されるため、セットアップ後はターミナル履歴をクリアすること。
>
> ```bash
> # クラスタレイアウト確認（出力の先頭に表示されるIDが <node-id>）
> kubectl exec -it deploy/plane-garage -- garage layout show
>
> # ノードにキャパシティを割り当て（<node-id> は上記出力から取得）
> kubectl exec -it deploy/plane-garage -- garage layout assign <node-id> -z dc1 -c 5GB
> kubectl exec -it deploy/plane-garage -- garage layout apply --version 1
>
> # バケット作成
> kubectl exec -it deploy/plane-garage -- garage bucket create plane-uploads
>
> # アクセスキー作成・バケットに紐付け
> kubectl exec -it deploy/plane-garage -- garage key create plane-key
> kubectl exec -it deploy/plane-garage -- garage bucket allow --read --write --owner plane-uploads --key plane-key
> ```
>
> 生成されたアクセスキーID・シークレットを `credentials.sops.yaml` の `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` に設定する。

> **データ耐久性について**: Garage は `replication_factor=1`・単一PVC で動作しており、HA構成ではない。ノード障害やPVC損失時に Plane の添付ファイルが失われる可能性がある。業務上重要なデータは別途バックアップを取ること。

### Plane リソース合計見積もり

HelmRelease で設定したリソース値に基づく見積もり:

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
| **Plane合計** | **375m**    | **~960Mi**      | **1600m** | **~2.6Gi**   |

> **注意**: 上記には PostgreSQL・Redis のリソースは含まれていない。外部サービスとして別途デプロイする場合はそのリソースも ResourceQuota の見積もりに含めること。migrator は Helm install/upgrade 時にのみ実行される一時的な Job だが、ResourceQuota は Job の Pod にも適用されるため見積もりに含めている。

> **注意**: OpenClaw（またはそれに代わるAIエージェント）のリソースは別途加算する必要がある。

## 6. Cloudflare Tunnel

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

**network-policy.yaml** — cloudflared用のEgress許可（Cloudflare接続 + DNS + namespace内Planeサービスへの転送）:

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
    # Cloudflare への接続（HTTPS + QUIC）
    - ports:
        - protocol: TCP
          port: 443
        - protocol: UDP
          port: 443
        - protocol: TCP
          port: 7844
        - protocol: UDP
          port: 7844
    # DNS解決
    - ports:
        - protocol: TCP
          port: 53
        - protocol: UDP
          port: 53
    # namespace内のPlaneサービスへのアクセス
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

## 7. Grafana（オプション）

既存のPrometheusに接続するスタンドアロンGrafanaをデプロイする場合は、[テナントセットアップガイド](./tenant-setup-guide.md) の Grafana セクションを参照。

## 8. 制約事項

| 項目 | 制限値 |
|------|--------|
| requests.cpu | 2 |
| requests.memory | 4Gi |
| limits.cpu | 4 |
| limits.memory | 8Gi |
| PVC数 | 10 |

- **クラスタスコープのリソースは作成不可** — ClusterRole, CustomResourceDefinition, PersistentVolume 等（OpenClawInstance CRは例外として許可済み）
- **全Podに `resources.requests` / `resources.limits` を設定すること** — 未設定のPodはResourceQuotaにより作成が拒否される。すべてのコンテナに `limits.cpu` を明示的に設定すること
- **`metadata.namespace` は省略推奨** — Flux の `targetNamespace: yukulab` で自動設定される

## 9. デプロイ手順

1. `YukkuriLaboratory/infra` リポジトリをpublic化する（またはFlux GitRepositoryの認証を設定する）
2. Age鍵ペアを生成し、秘密鍵を管理者に渡す
3. `.sops.yaml` を設定し、Secret を暗号化する
4. マニフェストを作成し、commit & push する
   - **注意**: `credentials.sops.yaml` の `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` は初回デプロイ時にはまだ不明のため、プレースホルダー値を設定しておく
5. 管理者がクラスタにAge秘密鍵Secretを登録する:
   ```bash
   kubectl create secret generic sops-age-yukulab \
     --namespace=flux-system \
     --from-file=age.agekey=yukulab-age.key
   ```
6. 管理者が `yukulab-workloads` Kustomization の `suspend: false` に変更してcommit
7. Flux が自動的にreconcileし、テナントのワークロードがデプロイされる
8. **Garage の初期セットアップ**（Section 5 の「初期セットアップ」手順を実行）:
   - `kubectl exec` で Garage の layout assign/apply を実行
   - バケット作成、アクセスキー作成
9. 生成されたアクセスキーで `credentials.sops.yaml` を更新し、再暗号化して commit & push
10. Flux が再reconcileし、Plane が正しい S3 認証情報で起動する

> **注意**: Plane は Garage のアクセスキーが正しく設定されるまで、ファイルアップロード機能が動作しない。アプリケーション自体の起動は S3 認証なしでも進むが、添付ファイル操作時にエラーとなる。

## 10. 検証

デプロイ後の状態確認コマンド:

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

# テナントワークロードの確認
kubectl get all -n yukulab

# HelmRelease の状態確認
flux get helmreleases -n yukulab

# migrator Job の確認（初回デプロイ・アップグレード時）
kubectl get jobs -n yukulab

# OpenClawInstance の確認
kubectl get openclawinstances -n yukulab
```

## 11. 注意事項

- NetworkPolicy は Cilium により**有効**である。cloudflared等の外部通信はテナントリポジトリ側のNetworkPolicyで明示的に許可する必要がある
- `yukulab-workloads` は初期状態で `suspend: true`。テナントリポジトリとSOPS鍵が準備できてから解除する
- テナントリポジトリがprivateの場合、GitRepositoryのfetchが認証エラーで失敗する
- Plane の Helm チャートバージョンは固定し、アップグレード前に `helm diff` 等で差分を確認すること
- OpenClawInstance の `config.raw` は環境に合わせてカスタマイズが必要（Discord設定、GitHub設定等）
- yukulab namespace は他テナントの namespace とは NetworkPolicy で隔離されるが、namespace 内の Pod 同士は相互通信が可能（intra-tenant マイクロセグメンテーションなし）
