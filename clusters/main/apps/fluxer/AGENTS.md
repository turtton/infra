# Fluxer Deployment

Fluxer はオープンソースの Discord-like チャット/VoIP アプリ。このディレクトリには K8s マニフェストと HelmRelease が含まれる。

## Architecture

```
Cloudflare Tunnel → Caddy:80 → app-proxy:8080 (SPA)
                              → api:8080 (REST)
                              → gateway:8080 (WebSocket)
                              → media-proxy:8080
                              → admin:8080
                              → static-proxy:8080 (user-uploaded content)
```

内部通信:
- gateway → caddy:8088/api → api:8080/internal/rpc (HTTP, `x-fluxer-rpc-auth` header で認証)
  - Caddy が `X-Forwarded-For` を付与。API の `RequireClientIpMiddleware` を通過するため必須
- 全サービス ↔ NATS (メッセージバス)
- api ↔ CNPG (PostgreSQL)
- api ↔ meilisearch:7700 (全文検索)
- api ↔ seaweedfs:8333 (S3互換ストレージ)
- api ↔ valkey:6379 (KVキャッシュ)
- snowflakes/messages/users/unfurl → NATS (svc.xxx subject)

## Source Charts

- Upstream: `https://github.com/fluxerapp/fluxer` (branch: main)
- GitRepository `fluxer-upstream` が `deploy/helm/` のみを fetch
- 各 HelmRelease は `chart: ./deploy/helm/<service>` を参照
- Image registry: `ghcr.io/fluxerapp`

## Key Configuration Gotchas

### API (helmrelease-api.yaml)
- **wrapper script 必須**: アプリ内 `Config.nats.coreUrl` が env var を無視するため、
  `command` で Typescript ラッパー (`fluxer-api-wrapper.ts`) を差し込み、import前に
  `Config.nats.coreUrl` を上書きしている。変更する場合はこのラッパーも修正すること。
- `FLUXER_POSTGRES_SSLMODE=require` (CNPG が TLS を要求するため)
- `FLUXER_INTERNAL_API_ENDPOINT` / `FLUXER_INTERNAL_GATEWAY_ENDPOINT` などが必要

### Gateway (helmrelease-gateway.yaml)
- **FLUXER_GATEWAY_PORT**: デフォルト 8771 だが chart は 8080 を期待 → 明示設定必須
- **FLUXER_INTERNAL_API_ENDPOINT**: 未設定だと `http://127.0.0.1:8080` を使う (動かない)
- **FLUXER_GATEWAY_RPC_AUTH_TOKEN**: API との HTTP RPC 認証用
- **NetworkPolicy**: upstream chart が ingress-nginx のみ許可する NetworkPolicy を生成。
  postRenderer で全許可パッチを当てている
- **nodeSelector**: upstream の `values.yaml` に特定ホスト名の nodeSelector あり。
  postRenderer で削除
- **imagePullSecrets**: chart が自動生成。postRenderer で削除
- ログは全て `/dev/null` に捨てられる (docker_entrypoint.sh の仕様)

### App-Proxy (helmrelease-app-proxy.yaml)
- **self-hosted image 必須**: `fluxer-app-proxy` (通常) は HTML に `https://fluxerstatic.com`
  の assets URL をハードコード。`fluxer-app-proxy-self-hosted` は相対パス (`/assets/*`) を生成。
  CSP 違反を防ぐため self-hosted イメージを使うこと
- `DISCOVERY_UPSTREAM_URL=http://caddy:80/.well-known/fluxer`
  (API の discovery endpoint は proxy header 必須のため caddy 経由)

### Shards (messages, users, unfurl, snowflakes)
- shard はデフォルトで Cassandra を想定しているが、Postgres もサポートされている
- `svc.extraEnv` で Postgres 接続設定 (`FLUXER_POSTGRES_*`) を注入することで CNPG をバックエンドとして利用可能
- **設定済み**: `helmrelease-messages.yaml` / `helmrelease-users.yaml` に extraEnv で CNPG 接続情報を追加
- unfurl-shard / snowflakes-shard は Cassandra 不要のためそのまま動作

### Caddy (caddy-configmap.yaml, raw k8s resource)
- HelmRelease ではなく raw Deployment + ConfigMap
- ConfigMap は `subPath: Caddyfile` でマウント → 更新時は Pod 再起動必須
- `.well-known/fluxer` → api:8080 (proxy header 付与のため caddy 経由)
- `/sw.js` → app-proxy (Cache-Control no-cache, CDN-Cache-Control no-cache)
- `/web/*` → `https://fluxerstatic.com` (apple-touch-icon 等の CDNアセット)
- sw.js は圧縮対象外 (`@notSW not path /sw.js`)
- CSP は app-proxy が生成。caddy では上書きしない

## Secrets

`*.sops.yaml` は sops + age で暗号化。復号には `SOPS_AGE_KEY_FILE` または `SOPS_AGE_KEY` が必要。
復号コマンド: `sops --decrypt <file>`

Key Ring:
- `fluxer-runtime-keys` → 各種 secret key (admin, vapid, rpc token, etc.)
- `gateway-erlang-cookie` → Erlang distribution cookie
- `meilisearch-key` → `MEILI_MASTER_KEY`
- `s3-credentials` → `FLUXER_S3_ACCESS_KEY_ID`, `FLUXER_S3_SECRET_ACCESS_KEY`
- `upload-relay` → `relay_secret_base64`
- CNPG が自動生成: `fluxer-db-app` (username, password)

## Reloader

`infrastructure/controllers/reloader/` の HelmRelease で稼働。Secret 変更を検知して
関連 Pod を自動再起動する。

## Troubleshooting

- ログイン後 connecting から進まない → gateway の `FLUXER_INTERNAL_API_ENDPOINT` / `FLUXER_GATEWAY_RPC_AUTH_TOKEN` を確認
- API が NATS に接続できない → wrapper script の `Config.nats.coreUrl` 設定を確認
- Gateway が WebSocket を受け付けない → NetworkPolicy がブロックしていないか確認
- SPA が真っ白 → app-proxy の image が `fluxer-app-proxy-self-hosted` か確認。CSP 違反がないかブラウザコンソール確認
- HelmRelease が stalled → 管理対象リソースが Failed 状態の場合がある。該当リソースを削除して再 reconcile

## LiveKit (Voice/Calls)

LiveKit サーバーが `clusters/main/apps/fluxer/livekit-helmrelease.yaml` でデプロイされている。
UDP は無効化され、ICE-TCP (7881) + WebSocket/HTTP (7880) のみで動作する。

### Architecture

```
Browser ─┬─ wss://chat.turtton.net/livekit ──→ Cloudflare Tunnel ──→ Caddy ──→ livekit:7880 (signaling)
          └─ tcp://livekit-media.<tailnet>.ts.net:7881 ──→ Tailscale Funnel ──→ livekit:7881 (ICE-TCP media)
```

- シグナリング: Cloudflare Tunnel → Caddy (/livekit/*) → livekit:7880
- メディア (ICE-TCP): Tailscale Funnel → livekit-tailscale-forwarder → livekit:7881

### Configuration

- `helmrelease-api.yaml`: `FLUXER_LIVEKIT_ENABLED=true`, URL/Key/Secret/Webhook 設定済み
- `helmrelease-gateway.yaml`: `roles.calls.enabled: true`
- `livekit-helmrelease.yaml`: UDP無効 (`port_range_start: null`, `port_range_end: null`)、
  ICE-TCP on 7881、Redis は Valkey を利用
- `livekit-keys.sops.yaml`: SOPS 暗号化済み。API key/secret を保持

### Tailscale Funnel

- `livekit-tailscale-forwarder.yaml`: Tailscale sidecar が `livekit:7881` への TCP 転送 + Funnel 公開
- 事前に `tailscale-livekit-auth` Secret (namespace: fluxer) に Tailscale auth key が必要
- Tailscale ACL で使用するタグに `funnel` 属性が付与されていることを確認

### 注意点

- Cloudflare Tunnel は UDP を通さないため、メディアは ICE-TCP (TCP) のみ
- Tailscale Funnel TCP は raw TCP フォワーディングを行う
- ブラウザの WebRTC スタックが ICE-TCP にフォールバックできる必要がある
- `livekit-keys.sops.yaml` の API_SECRET は運用前に適切な値に変更すること
