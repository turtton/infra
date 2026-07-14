# 内部で展開されており必要に応じてアクセスできるサービス

## Prometheus

```sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
```

## Longhorn

```sh
kubectl port-forward -n longhorn-system svc/longhorn-frontend 8080:80
```

## Attic

クラスタ内の Attic サーバー Pod に `kubectl exec` して `atticadm make-token` を実行し、JWT トークンを発行する。

### 前提条件

- `kubectl` がインストールされ、対象クラスタに接続していること
- `scripts/attic-make-token.sh` をリポジトリルートから実行する

### 最小権限のトークン

特定のキャッシュに対してプル・プッシュ権限のみを付与する例:

```sh
scripts/attic-make-token.sh \
  --sub "deploy-user" \
  --validity "90d" \
  --pull "my-cache" \
  --push "my-cache"
```

出力されるのはトークンのみ。標準出力をクリップボードや Secret 管理ツールに渡す。

### 管理者権限に近いトークン

すべてのキャッシュに対して現在の 7 種類の権限を付与するショートカット:

```sh
scripts/attic-make-token.sh \
  --sub "admin-user" \
  --validity "30d" \
  --admin
```

`--admin` は `--pull '*'`、`--push '*'`、`--delete '*'`、`--create-cache '*'`、`--configure-cache '*'`、`--configure-cache-retention '*'`、`--destroy-cache '*'` を展開する。これは将来追加される権限を含まないため、ルートトークンと同等とは限らない。

### 注意事項

- ワイルドカードパターンはシェルによって展開されないよう必ずクォートする
- 発行したトークンは JWT なので個別に失効できない。署名鍵をローテーションすると全ての HS256 トークンが無効になるため、有効期限は短めに設定し、必要に応じてローテーションする
- トークンは標準出力にのみ出力され、スクリプトは値をファイルやログに残さない
