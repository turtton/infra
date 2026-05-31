# GitHubリポジトリのForgejo移行手順

## 前提条件

- Forgejoにログイン済み: `https://forgejo.turtton.net`
- Woodpecker CIが稼働中: `https://woodpecker.turtton.net`
- SSH接続: `forgejo-ssh.taile2777.ts.net` (Tailscale経由)

## 1. リポジトリのミラーリング/移行

### 方法A: Forgejo UIからインポート (推奨)

1. Forgejo右上「+」→「リポジトリの移行」
2. 以下を入力:
   - クローンURL: `https://github.com/<owner>/<repo>.git`
   - リポジトリ名: 任意
   - 「ミラー」にチェック → 定期的にGitHubから同期 (読み取り専用ミラー)
   - または「ミラー」なし → 完全移行 (以降GitHubと独立)
3. 移行オプション:
   - Issues, Labels, Milestones, Pull Requests, Releases, Wiki を必要に応じて選択

### 方法B: CLIから移行

```bash
# ベアクローン
git clone --bare https://github.com/<owner>/<repo>.git
cd <repo>.git

# Forgejoにpush (事前にForgejo上で空リポジトリを作成)
git push --mirror ssh://git@forgejo-ssh.taile2777.ts.net:22/<user>/<repo>.git

# クリーンアップ
cd .. && rm -rf <repo>.git
```

### 方法C: Forgejo API

```bash
# アクセストークンをForgejoの設定 > アプリケーションで作成
FORGEJO_TOKEN="<your-token>"

curl -X POST "https://forgejo.turtton.net/api/v1/repos/migrate" \
  -H "Authorization: token ${FORGEJO_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "clone_addr": "https://github.com/<owner>/<repo>.git",
    "repo_name": "<repo>",
    "repo_owner": "<forgejo-user-or-org>",
    "service": "github",
    "mirror": false,
    "issues": true,
    "labels": true,
    "milestones": true,
    "pull_requests": true,
    "releases": true
  }'
```

> **注意**: プライベートリポジトリの場合は `auth_token` フィールドにGitHub Personal Access Tokenを追加

## 2. ローカルリポジトリのremote変更

```bash
cd <local-repo>

# originをForgejoに変更
git remote set-url origin ssh://git@forgejo-ssh.taile2777.ts.net:22/<user>/<repo>.git

# GitHubをupstreamとして残す場合
git remote add github https://github.com/<owner>/<repo>.git

# 確認
git remote -v
```

## 3. Woodpecker CIの有効化

1. `https://woodpecker.turtton.net` にログイン (Forgejo OAuth)
2. 左メニュー「Repositories」→ 対象リポジトリを有効化 (トグルON)
3. リポジトリに `.woodpecker.yaml` を追加:

```yaml
steps:
  - name: build
    image: golang:1.22  # 言語に合わせて変更
    commands:
      - go build ./...
      - go test ./...
```

4. pushするとCIが自動実行される

## 4. Woodpecker CI設定例

### Go プロジェクト

```yaml
steps:
  - name: test
    image: golang:1.22
    commands:
      - go test -v ./...

  - name: lint
    image: golangci/golangci-lint:latest
    commands:
      - golangci-lint run
```

### Node.js プロジェクト

```yaml
steps:
  - name: install
    image: node:20
    commands:
      - npm ci

  - name: test
    image: node:20
    commands:
      - npm test

  - name: build
    image: node:20
    commands:
      - npm run build
```

### Nix プロジェクト

```yaml
steps:
  - name: check
    image: nixos/nix:latest
    commands:
      - nix flake check
```

## 5. 移行後の確認

- [ ] Forgejoでリポジトリが閲覧できる
- [ ] SSH push/pullが動作する
- [ ] Woodpecker CIが `.woodpecker.yaml` で動作する
- [ ] (ミラーの場合) GitHub→Forgejo同期が動作する

## 6. GitHubリポジトリのアーカイブ (任意)

完全移行後、GitHub側を読み取り専用にする:

```bash
gh repo archive <owner>/<repo>
```

## 注意事項

- SSH接続はTailscale経由のみ (`forgejo-ssh.taile2777.ts.net`)
- HTTPS Webアクセスは Cloudflare Tunnel経由 (`forgejo.turtton.net`)
- GitHub Actionsのワークフローは `.woodpecker.yaml` に書き換えが必要 (互換性なし)
- Woodpecker CI のシークレットはWeb UIの Settings > Secrets で管理
