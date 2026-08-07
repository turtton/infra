#!/usr/bin/env python3
"""GitHub App installation tokenを発行してgh CLIを認証する。

hermesユーザーとして実行される。用途は2つ:
- コンテナ起動時の初期認証（StatefulSetのcommandから直接呼ばれる）
- hermes cronのno_agentジョブによる定期更新（installation tokenは1時間で失効するため）

cron経由では成功時にstdoutを出さない（サイレントtick）こと。
失敗時は非0で終了し、cronスケジューラがエラーアラートをDiscordに配送する。
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SECRETS_DIR = Path("/mnt/secrets")
GH_BIN = "/opt/data/bin/gh"
GH_CONFIG_DIR = "/run/gh-config"

# cronスクリプトはプロセス環境の認証情報を継承しない可能性があるため、
# 必要なものだけ明示的に渡す。
GH_ENV = {
    "PATH": "/opt/data/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/opt/data",
    "GH_CONFIG_DIR": GH_CONFIG_DIR,
}


def mint_installation_token() -> str:
    app_id = (SECRETS_DIR / "GITHUB_APP_ID").read_text(encoding="utf-8").strip()
    installation_id = (
        (SECRETS_DIR / "GITHUB_APP_INSTALLATION_ID").read_text(encoding="utf-8").strip()
    )
    private_key = (SECRETS_DIR / "GITHUB_APP_PRIVATE_KEY").read_text(encoding="utf-8")

    # PyJWT[crypto] は hermes-agent のコア依存としてイメージに同梱済み
    import jwt

    now = int(time.time())
    encoded_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": app_id},
        private_key,
        algorithm="RS256",
    )

    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {encoded_jwt}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)["token"]


def login_gh(token: str) -> None:
    result = subprocess.run(
        [GH_BIN, "auth", "login", "--with-token"],
        input=token,
        text=True,
        capture_output=True,
        env=GH_ENV,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh auth login failed: {result.stderr.strip()}")


def main() -> int:
    token = mint_installation_token()
    login_gh(token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # 非0終了 → cronスケジューラがエラーアラートを配送する
        print(f"github app token refresh failed: {error}", file=sys.stderr)
        sys.exit(1)
