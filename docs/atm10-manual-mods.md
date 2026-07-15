# ATM10 手動MOD導入手順書

ATM10 modpackには、作者が自動ダウンロードを拒否しているMODが含まれている場合があります。
`itzg/minecraft-server` の `AUTO_CURSEFORGE` 機能はこれらのMODを自動で取得できないため、CurseForgeのWebサイトから手動でダウンロードし、Kubernetesクラスタの `/downloads` PVCに配置する必要があります。

## 背景

- 自動ダウンロード不可のMODは、サーバー起動時に以下のようなエラーで停止します。

  ```text
  [mc-image-helper] WARN : The authors of the mod '...' have disallowed project distribution.
  [init] [ERROR] Failed to auto-install CurseForge modpack
  ```

- これらのMODは `/downloads/mods/` に配置すると、起動時に `mc-image-helper` が `/data/mods/` にコピーします。
- `/downloads` は `atm10-downloads` という専用PVC（500Mi）にマウントされています。

## 現在確認されている手動MOD

| MOD名 | ファイル名 | ダウンロードURL |
|---|---|---|
| unofficial cc:tweaked v 1.120.1 cf | `cc-tweaked-1.21.1-forge-1.120.0.jar` | `https://www.curseforge.com/minecraft/mc-mods/unofficial-cc-tweaked-v-1-120-1-cf/download/8273779` |

## 前提条件

- クラスターへの `kubectl` アクセスが可能であること
- Flux CDによって `atm10` Deploymentが管理されていること

## 手順

### 1. MODファイルを手動でダウンロード

1. エラーログまたは `MODS_NEED_DOWNLOAD.txt` に記載されたダウンロードURLをブラウザで開く
2. CurseForgeのダウンロードページから `.jar` ファイルをローカルマシンに保存
3. ファイル名を確認（例: `cc-tweaked-1.21.1-forge-1.120.0.jar`）

> **注意**: CurseForgeのダウンロードURLはCloudflare保護されているため、`curl` などのCLIツールからの直接ダウンロードは通常できません。ブラウザでダウンロードしてください。

### 2. Deploymentを一時停止

Flux CDが自動的に `replicas=1` に戻すため、`kubectl scale` だけでは不十分です。
一時的にGitで `replicas=0` に設定してpushします。

```bash
cd /home/turtton/.ghr/github.com/turtton/infra

# deployment.yaml の replicas: 1 を replicas: 0 に変更
sed -i 's/replicas: 1/replicas: 0/' clusters/main/apps/atm10/deployment.yaml

git add clusters/main/apps/atm10/deployment.yaml
git commit -m 'tmp(atm10): scale to 0 for manual mod update'
git push origin main

flux reconcile source git flux-system -n flux-system
flux reconcile kustomization apps -n flux-system
```

Podが削除され、PVCがデタッチされるまで待ちます。

```bash
kubectl wait --for=delete pod -l app=atm10 -n atm10 --timeout=120s
```

### 3. uploader Podを作成

`atm10-data` と `atm10-downloads` の両方のPVCをマウントした一時Podを起動します。

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: atm10-mod-uploader
  namespace: atm10
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: uploader
      image: busybox:latest
      command: ["sleep", "3600"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: data
          mountPath: /data
        - name: downloads
          mountPath: /downloads
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: atm10-data
    - name: downloads
      persistentVolumeClaim:
        claimName: atm10-downloads
EOF

kubectl wait --for=condition=Ready pod/atm10-mod-uploader -n atm10 --timeout=120s
```

> PVCが他のPodにアタッチされている場合、uploader Podが `ContainerCreating` のままになることがあります。
> `kubectl describe pod -n atm10 atm10-mod-uploader` で `Multi-Attach error` が発生している場合は、Deployment Podが完全に削除されるまで待ってください。

### 4. MODファイルをアップロード

ローカルでダウンロードした `.jar` ファイルを `/downloads/mods/` にコピーします。

```bash
kubectl cp ./cc-tweaked-1.21.1-forge-1.120.0.jar \
  atm10/atm10-mod-uploader:/downloads/mods/cc-tweaked-1.21.1-forge-1.120.0.jar

kubectl exec -n atm10 atm10-mod-uploader -- ls -la /downloads/mods/
```

### 5. uploader Podを削除

```bash
kubectl delete pod -n atm10 atm10-mod-uploader
```

### 6. Deploymentを復帰

Gitで `replicas=1` に戻してpushします。

```bash
sed -i 's/replicas: 0/replicas: 1/' clusters/main/apps/atm10/deployment.yaml

git add clusters/main/apps/atm10/deployment.yaml
git commit -m 'feat(atm10): restore replicas to 1 after manual mod update'
git push origin main

flux reconcile source git flux-system -n flux-system
flux reconcile kustomization apps -n flux-system
```

### 7. 起動確認

```bash
kubectl get pods -n atm10 -w
```

`READY 1/1` になり、`Dedicated server took ... seconds to load` がログに出力されたら成功です。

## MODパック更新時の注意

MODパックを更新する際、`mc-image-helper` は `/downloads` 配下のファイルを `/data` に再配置し、関連する設定ファイルも再生成することがあります。この再配置の影響で、以下の設定がデフォルト値に戻ることがあります。

- **Simple Backups の無効化設定**
  - ファイル: `/data/config/simplebackups-common.toml`
  - 確認項目: `enabled = false`

Longhorn による PVC スナップショットバックアップを運用している場合、MOD 側のバックアップが有効になっていると二重でバックアップが実行されてしまいます。MODパック更新後は必ず以下を確認してください。

```bash
kubectl exec -n atm10 deployment/atm10 -- grep '^enabled' /data/config/simplebackups-common.toml
```

出力が `enabled = false` ではなく `enabled = true` に戻っていた場合は、再度無効化して pod を再起動してください。

## トラブルシューティング

### `Multi-Attach error` でuploader Podが起動しない

`atm10-data` PVCがまだDeployment Podにアタッチされている状態です。Deployment Podが完全に削除され、Longhornがボリュームをデタッチするまで待ちます。急ぐ場合は、該当の `VolumeAttachment` を強制削除できます。

```bash
kubectl get volumeattachment | grep pvc-6b5c29c6-187d-4f8d-9c7d-32029918e5e9
kubectl delete volumeattachment <VOLUME_ATTACHMENT_NAME> --force
```

### MODを `/data/mods` に直接置いた場合

`/data/mods` は `mc-image-helper` が管理対象とするディレクトリです。手動MODをここに置くと、modpack更新時に削除される可能性があります。必ず `/downloads/mods/` を使用してください。

## 自動化の可能性（参考）

CurseForge APIでは `allowModDistribution: false` のMODは `downloadUrl` を取得できませんが、edge.forgecdn.netのURLは以下のパターンで推測できます。

```text
https://edge.forgecdn.net/files/{fileIdの上位4桁}/{fileIdの下位4桁}/{filename}
```

例:

```text
fileId: 8273779
filename: cc-tweaked-1.21.1-forge-1.120.0.jar
URL: https://edge.forgecdn.net/files/8273/779/cc-tweaked-1.21.1-forge-1.120.0.jar
```

このパターンは非公式であり、CurseForge側の変更で突然使えなくなる可能性があるため、運用の自動化には注意が必要です。
