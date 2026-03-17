# Nextcloud移行手順: LXC (AIO) → Kubernetes (Helm)

LXC上のNextcloud AIO (Docker Compose) から、Kubernetes (Flux CD管理) への移行手順をまとめる。

---

## 前提条件

| 項目 | 移行元 (AIO) | 移行先 (K8s) |
|---|---|---|
| Nextcloud | 32.0.4 | 33.0.0 (Helm chart 9.0.3) |
| PostgreSQL | 17.7 | 17.9 (CNPG, imageName指定) |
| DB名 | `nextcloud_database` | `nextcloud` |
| DBユーザー | `oc_nextcloud` | `nextcloud` |
| Redis/Valkey | Redis (AIO) | Valkey 8.0.2 (認証あり) |
| データパス | `/mnt/ncdata` | `/var/www/html/data` |
| データ量 | 約183GB | PVC 250Gi |
| アクセス | Tailscale (*.ts.net) | Tailscale Ingress |

### 必要な環境

- LXCへのSSHアクセス: `ssh root@192.168.11.12`
- `kubectl` でK8sクラスタにアクセス可能
- SOPS + Age鍵が設定済み
- worker-3がOpenTofuでデプロイ済み（ストレージ容量確保のため）

---

## 1. 事前準備

### 1.1 worker-3のデプロイ

Nextcloudの250Gi PVCを収容するため、worker-3をクラスタに追加する。

```bash
cd terraform/
tofu plan   # worker-3の追加を確認
tofu apply
```

Longhornダッシュボードで新ノードが認識されていることを確認:

```bash
kubectl -n longhorn-system get nodes.longhorn.io
```

### 1.2 SOPSシークレットの暗号化

`clusters/main/apps/nextcloud/nextcloud-secrets.sops.yaml` のパスワードを実際の値に変更してから暗号化する:

```bash
# パスワードを編集 (CHANGE_ME_BEFORE_ENCRYPTING を実際の値に)
vi clusters/main/apps/nextcloud/nextcloud-secrets.sops.yaml

# SOPS暗号化
sops --encrypt --in-place clusters/main/apps/nextcloud/nextcloud-secrets.sops.yaml
```

Valkeyシークレットも同様に暗号化する:

```bash
vi clusters/main/apps/nextcloud/valkey-secrets.sops.yaml
sops --encrypt --in-place clusters/main/apps/nextcloud/valkey-secrets.sops.yaml
```

### 1.3 Tailscale ACLタグの追加

Tailscale管理コンソール (https://login.tailscale.com/admin/acls) で `tag:nextcloud` を許可する。

### 1.4 Tailscaleホスト名の衝突回避

現在のLXC上のNextcloudが `nextcloud` というTailscaleホスト名を使用している場合、K8s移行前にLXC側を停止するか名前を変更する必要がある。

```bash
# LXCのTailscaleで現在のホスト名を確認
ssh root@192.168.11.12
tailscale status
```

---

## 2. LXCからデータをエクスポート

### 2.1 Nextcloudをメンテナンスモードに

```bash
ssh root@192.168.11.12

# メンテナンスモード有効化
docker exec -u www-data nextcloud-aio-nextcloud php occ maintenance:mode --on
```

### 2.2 PostgreSQLダンプ

DB名・ユーザー名をK8s側に合わせて変換したダンプを取得する。

```bash
# LXC上で実行
# ダンプ取得 (カスタムフォーマット)
docker exec nextcloud-aio-database pg_dump \
  -U oc_nextcloud \
  -d nextcloud_database \
  -Fc \
  -f /tmp/nextcloud_dump.custom

# ダンプをLXCのローカルにコピー
docker cp nextcloud-aio-database:/tmp/nextcloud_dump.custom /tmp/nextcloud_dump.custom
```

### 2.3 ユーザーデータの確認

```bash
# データサイズ確認
du -sh /var/lib/docker/volumes/nextcloud_aio_nextcloud_data/_data/
```

---

## 3. K8sにNextcloudをデプロイ (空の状態)

### 3.1 マニフェストをGitにプッシュ

```bash
git add clusters/main/apps/nextcloud/ terraform/terraform.tfvars
git commit -m "feat: add nextcloud deployment manifests"
git push
```

### 3.2 Flux同期を確認

```bash
flux reconcile kustomization apps
flux get kustomizations

# Nextcloud namespace のリソース確認
kubectl -n nextcloud get all,pvc,ingress
```

### 3.3 初回起動を待つ

HelmReleaseの初回インストールには最大15分かかる。

```bash
# HelmRelease状態
kubectl -n nextcloud get helmrelease nextcloud -w

# Pod状態
kubectl -n nextcloud get pods -w

# CNPG Cluster状態
kubectl -n nextcloud get cluster nextcloud-db
```

### 3.4 初回起動の確認後、Nextcloudを停止

データインポート前にNextcloudのPodを停止する。HelmReleaseを一時的にサスペンドする:

```bash
flux suspend helmrelease nextcloud -n nextcloud
kubectl -n nextcloud scale deployment nextcloud --replicas=0

# CronJobも停止（DB復元中にバックグラウンドジョブが走るのを防止）
kubectl -n nextcloud get cronjob
kubectl -n nextcloud patch cronjob nextcloud-cron -p '{"spec":{"suspend":true}}' 2>/dev/null || true
```

---

## 4. データベースの移行

### 4.1 ダンプファイルをCNPG Podに転送

```bash
# LXCからローカルにコピー
scp -i ./ssh_key root@192.168.11.12:/tmp/nextcloud_dump.custom /tmp/nextcloud_dump.custom

# CNPG Pod名を取得
CNPG_POD=$(kubectl -n nextcloud get pods -l cnpg.io/cluster=nextcloud-db -l role=primary -o jsonpath='{.items[0].metadata.name}')

# PodにダンプをコピーCLI
kubectl cp /tmp/nextcloud_dump.custom nextcloud/${CNPG_POD}:/tmp/nextcloud_dump.custom
```

### 4.2 既存DBを削除してリストア

CNPG自動生成のDB (`nextcloud`) を一度削除し、ダンプからリストアする。ダンプ内のロール名 (`oc_nextcloud`) をK8s側のロール名 (`nextcloud`) に読み替える。

```bash
kubectl -n nextcloud exec -it ${CNPG_POD} -- bash

# DB内で実行
# 既存DBを削除して再作成
psql -U postgres -c "DROP DATABASE IF EXISTS nextcloud;"
psql -U postgres -c "CREATE DATABASE nextcloud OWNER nextcloud;"

# リストア (ロール名を変換)
pg_restore \
  -U postgres \
  -d nextcloud \
  --no-owner \
  --role=nextcloud \
  /tmp/nextcloud_dump.custom

# oc_nextcloud のオブジェクトを nextcloud に変更
psql -U postgres -d nextcloud -c "REASSIGN OWNED BY oc_nextcloud TO nextcloud;" 2>/dev/null || true

# 確認
psql -U nextcloud -d nextcloud -c "\dt" | head -20
psql -U nextcloud -d nextcloud -c "SELECT COUNT(*) FROM oc_users;"

# ダンプファイル削除
rm /tmp/nextcloud_dump.custom

exit
```

---

## 5. ユーザーデータの移行

### 5.1 NextcloudのPVC名を確認

```bash
PVC_NAME=$(kubectl -n nextcloud get pvc -l app.kubernetes.io/name=nextcloud -o jsonpath='{.items[0].metadata.name}')
echo "PVC: ${PVC_NAME}"
```

### 5.2 データ転送用の一時Podを作成

大量データ(183GB)の転送にはrsyncを使用する。一時Podを経由して転送する。

```bash
# Step 5.1で取得したPVC名を使用
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: nextcloud-data-migration
  namespace: nextcloud
spec:
  nodeSelector:
    kubernetes.io/hostname: cp-1
  containers:
  - name: rsync
    image: alpine:3.21
    command: ["sleep", "infinity"]
    volumeMounts:
    - name: nextcloud-data
      mountPath: /data
  volumes:
  - name: nextcloud-data
    persistentVolumeClaim:
      claimName: ${PVC_NAME}
  restartPolicy: Never
EOF

kubectl -n nextcloud wait --for=condition=Ready pod/nextcloud-data-migration --timeout=120s
```

### 5.3 rsyncでデータ転送

```bash
# 一時Podにrsyncをインストール
kubectl -n nextcloud exec nextcloud-data-migration -- apk add --no-cache rsync openssh-client

# LXCのデータパス
LXC_DATA="/var/lib/docker/volumes/nextcloud_aio_nextcloud_data/_data"

# 方法A: kubectl exec + tar (ネットワーク経由でストリーム)
ssh -i ./ssh_key root@192.168.11.12 \
  "tar -C ${LXC_DATA} -cf - ." | \
  kubectl -n nextcloud exec -i nextcloud-data-migration -- tar -C /data/data -xf -

# 方法B: 中間ノード経由 (cp-1にSSHできる場合)
# cp-1上でLXCからrsyncし、Longhornボリュームに直接書き込む
```

> **注意:** 183GBの転送には相当時間がかかる。tmux等でセッション切れに備えること。

### 5.4 データの所有権を修正

Nextcloud Helm chartのコンテナはUID `33` (www-data) で動作する。

```bash
kubectl -n nextcloud exec nextcloud-data-migration -- chown -R 33:33 /data/data
```

### 5.5 転送確認

```bash
# サイズ確認
kubectl -n nextcloud exec nextcloud-data-migration -- du -sh /data/data

# ディレクトリ構造確認
kubectl -n nextcloud exec nextcloud-data-migration -- ls -la /data/data/

# 一時Pod削除
kubectl -n nextcloud delete pod nextcloud-data-migration
```

---

## 6. Nextcloudの起動とDB設定更新

### 6.1 旧環境からconfig.php秘密鍵を取得

Nextcloudの `config.php` にはインスタンス固有の秘密鍵（`instanceid`, `passwordsalt`, `secret`）が含まれている。これらを移行しないと既存ユーザーのパスワードやセッションが無効になる。

```bash
# LXC上で秘密鍵を取得
ssh -i ./ssh_key root@192.168.11.12

docker exec nextcloud-aio-nextcloud grep -E "instanceid|passwordsalt|secret" /var/www/html/config/config.php
```

出力例:
```
  'instanceid' => 'ocXXXXXXXXXX',
  'passwordsalt' => 'XXXXXXXXXXXXXXXXXXXXXXXXXX',
  'secret' => 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX',
```

これらの値をメモしておく。

### 6.2 Nextcloudを再起動

```bash
flux resume helmrelease nextcloud -n nextcloud
kubectl -n nextcloud rollout status deployment nextcloud --timeout=600s
```

### 6.3 config.php秘密鍵の移行

Step 6.1でメモした秘密鍵をK8s上のNextcloudに設定する。

```bash
NC_POD=$(kubectl -n nextcloud get pods -l app.kubernetes.io/name=nextcloud -o jsonpath='{.items[0].metadata.name}')

# 秘密鍵を設定（Step 6.1でメモした値に置き換えること）
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set instanceid --value="ocXXXXXXXXXX"
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set passwordsalt --value="XXXXXXXXXXXXXXXXXXXXXXXXXX"
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set secret --value="XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

### 6.4 DB設定の更新

Nextcloudの `config.php` 内のDB接続情報を更新する必要がある。

```bash
# 現在のconfig確認
kubectl -n nextcloud exec ${NC_POD} -c nextcloud -- cat /var/www/html/config/config.php

# DB設定を更新 (occ コマンド)
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set dbhost --value="nextcloud-db-rw.nextcloud.svc.cluster.local"
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set dbname --value="nextcloud"
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set dbuser --value="nextcloud"

# DBパスワードはCNPGシークレットから取得して設定
DB_PASS=$(kubectl -n nextcloud get secret nextcloud-db-app -o jsonpath='{.data.password}' | base64 -d)
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set dbpassword --value="${DB_PASS}"
```

### 6.5 trusted_domains の設定

```bash
# Tailscaleのホスト名を trusted_domains に追加
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set trusted_domains 0 --value="nextcloud"
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set trusted_domains 1 --value="nextcloud.taile2777.ts.net"
```

### 6.6 datadirectory の確認

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:get datadirectory
# 期待値: /var/www/html/data
```

もしAIOの旧パス (`/mnt/ncdata`) が設定されていた場合:

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ config:system:set datadirectory --value="/var/www/html/data"
```

---

## 7. Nextcloudのアップグレードとファイルスキャン

### 7.1 データベースのアップグレード

32→33のバージョンアップに伴うDBマイグレーションを実行する。

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ upgrade
```

### 7.2 ファイルスキャン

移行したファイルをNextcloudに認識させる。

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ files:scan --all
```

> **注意:** 183GBのデータでは相当時間かかる。

### 7.3 メンテナンスモード解除

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ maintenance:mode --off
```

---

## 8. 動作確認

### 8.1 アクセス確認

Tailscaleネットワーク内のデバイスからブラウザでアクセス:

```
https://nextcloud.taile2777.ts.net
```

### 8.2 確認項目

| 項目 | 確認方法 |
|---|---|
| ログイン | 既存ユーザーでログイン可能 |
| ファイル一覧 | 移行したファイルが表示される |
| ファイルアップロード | 新規ファイルのアップロードが成功 |
| ファイルダウンロード | 既存ファイルのダウンロードが成功 |
| 共有リンク | 既存の共有リンクが機能する |
| 管理画面 | 設定 → 概要でエラーがないか確認 |
| Cron | バックグラウンドジョブが実行されている |

### 8.3 管理画面の警告確認

```bash
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ status
kubectl -n nextcloud exec -u 33 ${NC_POD} -c nextcloud -- php occ check
```

---

## 9. クリーンアップ

移行完了を確認したら、旧LXC環境を停止する。

### 9.1 LXCのNextcloud AIOを停止

```bash
ssh root@192.168.11.12
docker compose down  # AIOの停止
```

### 9.2 LXCのTailscaleを停止

Tailscaleホスト名の衝突を回避するため（まだ停止していない場合）:

```bash
ssh root@192.168.11.12
tailscale down
```

### 9.3 ローカルのダンプファイルを削除

```bash
rm /tmp/nextcloud_dump.custom
```

---

## トラブルシューティング

### Pod が起動しない

```bash
kubectl -n nextcloud describe pod -l app.kubernetes.io/name=nextcloud
kubectl -n nextcloud logs -l app.kubernetes.io/name=nextcloud -c nextcloud --tail=100
kubectl -n nextcloud logs -l app.kubernetes.io/name=nextcloud -c nginx --tail=100
```

### HelmRelease のエラー

```bash
kubectl -n nextcloud get helmrelease nextcloud -o yaml | grep -A 10 status
flux logs --kind=HelmRelease --name=nextcloud -n nextcloud
```

### CNPG データベースの確認

```bash
kubectl -n nextcloud get cluster nextcloud-db
kubectl -n nextcloud logs -l cnpg.io/cluster=nextcloud-db --tail=50
```

### Tailscale Ingress の問題

```bash
# Ingress状態
kubectl -n nextcloud get ingress
kubectl -n nextcloud describe ingress nextcloud

# tailscale-operator のログ
kubectl -n tailscale logs -l app.kubernetes.io/name=operator --tail=50
```

### ファイル権限の問題

```bash
# Pod内でファイル権限を確認
kubectl -n nextcloud exec ${NC_POD} -c nextcloud -- ls -la /var/www/html/data/
kubectl -n nextcloud exec ${NC_POD} -c nextcloud -- id
# www-data (uid=33) であること
```

---

## チェックリスト

| ステップ | 完了 |
|---|---|
| worker-3 デプロイ完了 | [ ] |
| SOPSシークレット暗号化済み (nextcloud-secrets + valkey-secrets) | [ ] |
| Tailscale ACLに `tag:nextcloud` 追加 | [ ] |
| LXCメンテナンスモード有効化 | [ ] |
| PostgreSQLダンプ取得 | [ ] |
| K8sにNextcloudマニフェストデプロイ | [ ] |
| 初回起動確認 | [ ] |
| DBリストア完了 | [ ] |
| ユーザーデータ転送完了 (183GB) | [ ] |
| 所有権修正 (www-data) | [ ] |
| config.php秘密鍵移行 (instanceid/passwordsalt/secret) | [ ] |
| DB設定更新 (occ) | [ ] |
| trusted_domains 設定 | [ ] |
| `occ upgrade` 実行 | [ ] |
| `occ files:scan --all` 実行 | [ ] |
| メンテナンスモード解除 | [ ] |
| ブラウザアクセス確認 | [ ] |
| ファイル操作確認 | [ ] |
| 旧LXC環境停止 | [ ] |
