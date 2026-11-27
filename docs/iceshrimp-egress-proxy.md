# Iceshrimp 外部通信の VPS プロキシ化 (基盤編)

kameuo の Iceshrimp Web Pod が行う外部 HTTP(S) 通信を、さくら VPS (`os3-387-26840` / Tailscale 名 `sakura-mcproxy-1`) の Squid 経由にする。
外部から見える送信元 IPv4 は VPS の公開 IPv4 `133.167.115.94` になる。

このドキュメントは 3 本の PR で段階的に反映する計画の運用手順書である。

| PR | 内容 | 状態 |
|---|---|---|
| PR1 基盤 | Tailscale grant/tests、Squid ロール、専用 playbook、CI の許可リスト、SOPS host vars、本書 | マージ・適用済み (#209、#210) |
| PR2 経路変更 | Iceshrimp の ConfigMap (`proxy`・許可 `/32`)、Reloader annotation。Node の環境変数は **マージ前ゲート不合格のため含めない** (「PR2」を参照) | この PR |
| PR3 egress 制限 | NetworkPolicy `iceshrimp-web-egress` | 未着手 (段階 4 の必須ゲート合格後) |

PR2・PR3 の手順は、それぞれの PR で本書へ追記する。**各段階の必須ゲートを満たすまで次へ進まない。**

## 範囲

- 対象は Iceshrimp Web Pod の外部 HTTP/HTTPS 通信だけ。ingress、SMTP、DB/Valkey Pod、他アプリ、新しい常設サービスは変更しない。
- R2 もプロキシ経由とする (既存の `objectStorageUseProxy=true` を維持する)。
- `nftables_vps` は変更しない。公開側 TCP/3128 は開けない。
- 障害時は従来の直通構成への切り戻しを優先する。その間は自宅 IP が一時的に再露出するため、報告に含める。

## 構成 (PR1 で入るもの)

```text
cp-1 (Iceshrimp Pod) -- Tailscale TCP/3128 --> VPS 100.89.111.33:3128 (Squid) -- 133.167.115.94 --> 外部
```

- Tailscale ACL: `tag:infra-talos-cluster` から `tag:mcproxy` への `tcp:3128` だけを許可する grant を 1 件追加する (`terraform/tailscale.tf`)。
  この許可はノード単位で、cp-1 上のどの Pod かは識別しない。`tag:client` は既存の全許可により 3128 へ届くため、Squid の送信元 ACL で拒否する。
- Squid ロール: `ansible/roles/squid_forward_proxy/`。Debian の apt パッケージと、既存の `squid.service` への drop-in で構成する。
  `ansible/playbooks/vps-squid.yml` は Squid ロールだけを実行し、nftables・Tailscale・SSH・他アプリのロールは再適用しない。`vps.yml` にも同じロールをタグ付きで入れてある。
- 許可送信元: cp-1 の Tailscale IP の `/32` だけ。SOPS で暗号化した `ansible/inventory/host_vars/os3-387-26840/squid.sops.yml` に置く。
  Tailnet 全体や Pod CIDR 全体へは広げない。空の場合、`/32` 以外が含まれる場合、`100.64.0.0/10` 外の場合は適用が失敗する。
- 平文で書いてよい Tailscale IP は VPS の `100.89.111.33` だけ。他ノードの Tailscale IP、トークン、平文の state/plan、R2 の資格情報や署名付き URL は、コミットにも公開ログにも出さない。

### Squid の主な設定

| 項目 | 設定 |
|---|---|
| 待ち受け | `100.89.111.33:3128` のみ。`0.0.0.0` へのフォールバックはしない |
| 送信元 | `tcp_outgoing_address 133.167.115.94` |
| 宛先 | ポートは 80/443、CONNECT は 443 のみ。名前解決後の IP で RFC1918、ループバック、リンクローカル、`100.64.0.0/10`、予約帯、IPv6 の内部帯、VPS 自身を拒否。最後は `http_access deny all` |
| ヘッダー | `forwarded_for delete`、`via off`、`X-Forwarded-For`/`Forwarded`/`X-Real-IP` 等を除去。TLS bump はしない (CONNECT 内の HTTPS ヘッダーは変更できない) |
| キャッシュ | 無効 (`cache deny all`、`cache_mem 0 MB`、`cache_dir` なし)、`workers 1`、`memory_pools off` |
| FD | `max_filedescriptors 4096` と `LimitNOFILE=4096` |
| メモリ | systemd の `MemoryHigh=128M`、`MemoryMax=160M` (`cache_mem` はプロセス全体の上限ではないため cgroup で制限する) |
| 復旧 | `Wants`/`After`(tailscaled)、`ExecStartPre` で指定 IP を最大 60 秒待つ (Debian の unit は `Type=notify`、`--foreground` で起動し、パッケージ自身の `ExecStartPre` は残す)、`Restart=on-failure`、`RestartSec=10s`、`StartLimitIntervalSec=0`。`Requires=tailscaled.service` は付けない |
| ログ | `access.log` は時刻・結果・転送量・メソッド・宛先 host:port だけ (path と query は記録しない)。`logfile_rotate 0` (回転は Debian の logrotate に任せる) |

`4096` は観測した外部 TCP 接続 (最大 127) に対する FD の余裕であり、接続容量の保証ではない。

### 制約と注意

- **80/443 以外で動作するリモートには接続できなくなる。** 非標準ポートの ActivityPub サーバーや R2 以外のオブジェクトストレージは対象外。
- IPv6 のみの宛先は非対応 (VPS にグローバル IPv6 がない)。dual-stack の宛先は IPv4 で接続する。将来 VPS にグローバル IPv6 を足す場合は設計を見直す。
- DNS はプロキシ非対応の経路で Pod 内から CoreDNS 経由で引かれる。上流リゾルバが自宅 IP から再帰問い合わせする構成だと、相手の権威 DNS に自宅 IP が見える。
  受け入れ条件の範囲外だが、PR3 の前に上流リゾルバの構成を確認して本書へ追記する。
- Tailscale の IP は変更されうる。変更時の手順は「運用」を参照。

## 段階 0: 事前記録

PR1 作成時点 (読み取りのみ) の記録。SOPS で許可 `/32` の暗号化と復号 (値は非表示) を確認済み。

| 項目 | 結果 |
|---|---|
| Ansible ping | 成功 (`ansible vps -m ping`) |
| OS / カーネル | Debian 12 / 6.1.0-56 |
| VPS CPU | 1 vCPU、負荷 (loadavg) は 0.05 前後 |
| VPS メモリ | 合計 約 458 MiB、`MemAvailable` 約 269 MiB、swap なし |
| グローバル IPv6 | なし |
| 既存サービス | tailscaled / ssh / nftables / docker / nginx が active (Minecraft の HAProxy は Docker 側)。待ち受けは SSH、25565、tailscaled のみ |
| Squid | 未導入。候補 `5.7-2+deb12u6`。`--no-install-recommends` の導入シミュレーションは新規 7 パッケージ (squid, squid-common, squid-langpack, libecap3, libltdl7, libtdb1, libdbi-perl) |
| AppArmor | カーネル側は有効。`aa-status` 未導入、`/etc/apparmor.d/` に squid のプロファイルなし。導入後に再確認する |
| cp-1 ↔ VPS の Tailscale 経路 | VPS からの `tailscale ping` で最初の数回は DERP 経由、その後 direct (約 8 ms) に切り替わった |
| Web Pod の外部 TCP 接続候補 | 5 秒間隔 5 回で 123、127、126、126、127。最大 127 (短時間のサンプル。長期ピークの保証ではない) |
| Pod eth0 の転送量 | 2 分間で RX 約 29.99 MiB、TX 約 2.87 MiB (内部通信を含む Pod 全体の値) |

静的検証 (PR1): `ansible-lint` (CI と同じ 26.6.0 でも合格)、`vps.yml`/`vps-squid.yml` の `--syntax-check`、`tofu fmt -check`/`tofu validate`、手元から VPS への `--check`。
Squid の設定は、使い捨ての Debian 12 コンテナ (Squid 5.7) で `squid -k parse` と、ACL・ヘッダー除去・ログ形式の試験を行った (VPS 上の動作確認は段階 2・3 で行う)。

段階 0 のうち、Iceshrimp 側の基準状態 (image digest、Node バージョン、Pod 配置・再起動数、検索・メディア・R2・配送キューの基準) は PR2 の前に記録する。
アプリのログインが必要な確認 (検索、フォロー、アップロードと削除、管理画面) の実施者と手段は、段階 4 の前に決める。

## 段階 1: Tailscale ACL (PR1 マージ後)

CI で ACL だけを plan → apply する。

```sh
gh workflow run terraform-apply.yml --ref main -f mode=plan -f scope=tailscale-acl
# plan の変更件数を確認してから
gh workflow run terraform-apply.yml --ref main -f mode=apply -f scope=tailscale-acl
```

ゲート:

- Tailscale の tests が成功する。
- 変更は `tailscale_acl.this` の update のみ。既存 grant (特に LiveKit 向け UDP/30082) は変わらない。
- 暗号化 state の自動 commit を確認する。以降の Ansible は、この新しい main の SHA を基準にそろえる。
- 適用後に `scope=full` の plan で残差分を確認する。**full apply はしない。**

想定外の差分があれば中止する。

## 段階 2: Squid の導入と単体確認

### 2-1. 未導入状態の check

Squid 未導入のうちに、次の 2 つを CI で実行する。いずれも `mode=check` で、`vps.yml` は apply しない。

```sh
gh workflow run ansible-apply.yml --ref main -f mode=check -f playbook=vps.yml
gh workflow run ansible-apply.yml --ref main -f mode=check -f playbook=vps-squid.yml
```

`vps.yml` の check で失敗した場合は、失敗タスク名 (CI ログに出る) で、新ロール起因か既存ロール起因かを切り分ける。既存ロールの修正は本件の範囲外。
check mode では parse・listener・実効値の検証は行わない (Squid が未導入のため)。これらは通常適用時に必ず実施される。

### 2-2. 適用

```sh
gh workflow run ansible-apply.yml --ref main -f mode=check -f playbook=vps-squid.yml   # 2-1 の再実行でよい
# 同じ main SHA の check 成功を確認してから
gh workflow run ansible-apply.yml --ref main -f mode=apply -f playbook=vps-squid.yml
```

check と apply は別の run になる。**両 run の `headSha` (`gh run view <id> --json headSha`) が同じであることを運用記録に残す。**
間に main が進んだら、新しい revision で check をやり直す。ワークフローの apply 中の main 一致検査は、先行 check との一致確認の代わりにならない。

適用では次を行い、いずれかが失敗するとタスクが失敗する。

1. 変数・Tailscale・メモリ (`MemAvailable` 192 MiB 以上) の検証
2. `policy_rc_d: 101` で自動起動を防いだ `squid` の導入
3. 待機スクリプトと drop-in の配置
4. `/etc/squid/squid.conf.candidate` を `squid -k parse` で検証してから `squid.conf` を置き換える (失敗時は稼働中の設定を置き換えない)
5. daemon-reload、enable/start
6. listener が `100.89.111.33:3128` だけであること、`LimitNOFILE`/`MemoryHigh`/`MemoryMax`/`Restart`/`RestartUSec` の実効値、`Requires` に tailscaled が含まれないことの確認

### 2-3. 証跡の取り方

CI の詳細ログは公開されない (成功/失敗と失敗タスク名だけ)。次を VPS 上で確認して記録する (ログイン後に実行する。IP や設定内容は貼らない)。

```sh
systemctl cat squid
systemctl is-active squid
systemctl show squid -p After -p Wants -p Requires -p Restart -p RestartUSec -p NRestarts -p ActiveEnterTimestamp
systemctl show squid -p LimitNOFILE -p LimitNOFILESoft -p MemoryCurrent -p MemoryHigh -p MemoryMax
squid -k parse
ss -lntp | grep 3128
journalctl -u squid --since "10 minutes ago"
cat /proc/$(systemctl show squid -p MainPID --value)/limits | grep 'open files'
ls /etc/apparmor.d 2>/dev/null | grep -i squid   # プロファイルが追加されていないか
```

冪等性は、**再適用の前後**で `NRestarts` と `ActiveEnterTimestamp`、`/etc/squid/squid.conf` と drop-in の更新時刻 (`stat -c %y`) が変わらないことで確認する。
`ansible-playbook --check --diff` を手元から実行して差分が空であることを確認するのは、診断として許容する (読み取りのみ。CI の check/apply の代わりにはしない)。

### 2-4. ゲート

- `100.89.111.33:3128` のみで listen。公開 IP の 3128 へ接続できない。
- FD・メモリ制限が実効値に反映されている。
- 自動復旧: アプリを切り替える前に、次を行う。

  ```sh
  systemctl kill -s KILL squid
  ```

  180 秒以内を目安に、手動の start / reset-failed なしで指定 IP への listen とプロキシ通信が復帰する。
  **tailscaled の再起動試験と VPS の再起動試験は行わない** (SSH・Minecraft・LiveKit を巻き込むため)。
- 起動時の再試行経路 (Tailscale のアドレス確定前に `ExecStartPre` が失敗して再試行) は、サービスに触れずに待機スクリプトを単体で確認する。

  ```sh
  time /usr/local/sbin/squid-wait-for-tailscale 192.0.2.99 3   # 3 秒後に非ゼロ (1) で終了する
  /usr/local/sbin/squid-wait-for-tailscale 100.89.111.33 3     # 直ちに 0 で終了する
  ```
- Tailscale と既存サービス (SSH、Minecraft、NGINX 経由の LiveKit) を再起動していない。
- 再適用が冪等。
- 資源停止条件 (後述) に該当しない。

### 資源の停止条件

- 導入直前の `MemAvailable` が 192 MiB 未満: 原因と既存負荷を確認するまで導入を保留する (ロールが失敗する)。
- 稼働中の `MemAvailable` が 96 MiB 未満、または Squid の使用量が 128 MiB を超える状態が 5 分程度続く: 次の段階へ進まない。
- OOM、`MemoryMax` 到達に伴う kill、再起動ループ、既存サービスへの影響: 即時中止して切り戻す。
- FD 使用率 75% 以上の継続、FD 警告、新規接続の失敗: 容量評価をやり直す。
- 1 vCPU の負荷が継続的に高く、キュー遅延・再送・既存サービスの劣化を伴う: 中止する。

メモリ不足を FD 上限や `MemoryMax` の無条件の引き上げで解消しない。アプリ切替後に停止条件が発生した場合は、**先にアプリを直通へ戻し、その後で必要なら Squid を停止する。**
VPS の増強、swap の追加、新しい監視サービスの導入は本計画に含めない。

## 段階 3: 経路・拒否・MSS・転送の試験 (PR2 の前)

cp-1 上の一時テスト Pod (`app=iceshrimp-web` を付けない。試験後に削除する) から確認する。

```sh
curl --fail --max-time 30 --proxy http://100.89.111.33:3128 https://<利用可能な IP 確認先>
```

確認項目:

- 外部から見える送信元が `133.167.115.94` である。
- Squid が観測する送信元が、SOPS に入れた cp-1 の `/32` と一致する。不一致なら経路を調査する。**許可範囲を広げて回避しない。**
- cp-1 ↔ VPS が direct 接続か。VPS 上で `tailscale ping <cp-1 の Tailscale 名>` を使い、DERP 経由のままなら配送遅延の原因になるため記録する (cp-1 の IP は記録に残さない)。
- TCP 到達可能な許可外のノード/クライアントは Squid で 403 になる。CI など Tailscale ACL で拒否されるものは、Squid の 403 と区別して記録する。
- RFC1918、ループバック、リンクローカル、Tailnet、VPS 自身、CONNECT:80、CONNECT:8443、SMTP (25) が拒否される。private IP へ解決される名前と、そこへのリダイレクトも拒否される。
- HTTP の送信元識別ヘッダー (`X-Forwarded-For` 等) が外部へ残らない (HTTP echo で確認)。
- MSS: VPS の `tailscale0` は MTU 1280 なので、クライアント向け SYN-ACK の MSS は IPv4 で 1240 以下を期待する。`ss -ti` の `advmss` 等で方向を区別して確認する。
  必要なら SYN/SYN-ACK だけの短時間 capture を使う (payload や認証情報は収集しない)。想定外の MSS や再送・途中停止があれば原因を調べる。クラスタ全体の MTU/MSS は場当たり的に変更しない。
- 下り: `kameuofile.turtton.net` の既存の大きいオブジェクトをプロキシ経由で取得し、期待サイズまたは既知ハッシュと照合する。適切なものがなければ未実施と記録し、段階 4 で補完する。
- 上り: 第三者への大容量 POST はしない。必要なら自分の R2 バケットの既知エンドポイントへ、非機密の 1〜16 MiB を最大 3 回程度 PUT する (HTTP/1.1、`Expect:` を空にする)。
  `size_upload`、HTTP status、curl 終了状態、所要時間を記録し、Squid の `access.log` の上り転送量 (`TCP_TUNNEL` 行の 5 列目 = 上り、6 列目 = 下り) と照合する。
  **合格は `size_upload` がファイルサイズと一致し、Squid の転送量と整合すること。** 認証エラーの応答だけでは成功としない。本体送信が途中で打ち切られた場合は「判定不能」とし、経路の合格にも不合格にもしない。
  判定不能でも MSS と他の経路確認を満たせば段階 4 へ進めるが、**段階 5 へは段階 4 の大容量 R2 試験の成功が必須。**

## PR2: Iceshrimp の経路変更

### マージ前ゲート (Node 単体試験) の結果

cp-1 上の一時 Pod で、本番と同じ image (`iceshrimp.dev/iceshrimp/iceshrimp@sha256:f3b3d81b…`、Node v24.9.0、got 15.0.5、node-fetch 3.3.2、hpagent 0.1.2、`@iceshrimp/summaly` 2.7.3) を使い、`sleep` に command を上書きして試験した。
DB/Valkey の資格情報、Secret、本番設定は渡していない (設定ファイルはダミーの `ICESHRIMP_CONFIG`)。ServiceAccount token は mount せず、`app=iceshrimp-web` は付けず、試験後に削除した。
Iceshrimp 本体は起動していない。試験は `NODE_USE_ENV_PROXY=1` と `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` を付けた新規 Node プロセスで行った (起動後の `process.env` 変更は使っていない)。
経路は、返ってきた外部 IP と、VPS の Squid `access.log` (送信元は伏せて参照) で判定した。

| 項目 | 結果 |
|---|---|
| 直通の対照 (環境変数なし) | http/https の 4 クライアントすべてが自宅 IP (値は伏せる) |
| `http.get`/`https.get`、node-fetch、got | http/https とも `133.167.115.94`。環境変数だけでプロキシ経由になる |
| グローバル `fetch` | https は `133.167.115.94`。**http:// は `CONNECT host:80` を送り、Squid が 403** (undici は http:// も CONNECT でトンネルする) |
| Iceshrimp の独自 agent (`config.proxy` 相当、`getResponse`・`downloadUrl`・S3 `NodeHttpHandler`) | https は `133.167.115.94` で、トンネルは 1 本 (二重 CONNECT なし。`CONNECT 100.89.111.33:3128` の行もない)。`allowedPrivateNetworks` に `100.89.111.33/32` があれば `Blocked address` は出ない。無いと `Blocked address: 100.89.111.33` になる。**http:// は hpagent が `CONNECT host:80` を送り、Squid が 403** (環境変数の有無によらない) |
| 独自 agent の bypass 用 `CheckedHttpAgent`/`CheckedHttpsAgent` | `NODE_USE_ENV_PROXY` を無視して直通 (自宅 IP)。`proxyBypassHosts` と `objectStorageUseProxy=false` のときだけ使われ、どちらも本構成では使わない |
| プロキシ到達不能時 | `config.proxy` が不達でも、環境変数が不達でも、全クライアント (独自 agent・S3 を含む) が `ECONNREFUSED` で失敗し、直通へフォールバックしない |
| `NO_PROXY` | `127.0.0.1`、`localhost`、Pod 名は直接接続 (Squid にログなし)。`NO_PROXY` に無い Pod IP はプロキシへ送られ 403 |
| `@iceshrimp/summaly` | **不合格。** `summaly.default(url, {followRedirects: false, …})` は https の GET 自体は CONNECT 経由で成功するが、`Private IP rejected 100.89.111.33` で失敗する (下記) |

#### summaly が失敗する理由

summaly は内部の got (agent 指定なし) で取得し、応答の `response.ip` を `private-ip` で検査する。環境変数プロキシでは `response.ip` がプロキシの `100.89.111.33` になり、`100.64.0.0/10` は private 扱いのため拒否される。
Iceshrimp の `allowedPrivateNetworks` は Iceshrimp 自身の agent の検査にしか効かず、summaly には効かない。
回避は summaly の環境変数 `SUMMALY_ALLOW_PRIVATE_IP=true` (「テスト用」と書かれた検査の無効化) しかない。これを設定すると、https の GET、アイコン取得、`downloadUrl` による OG 画像 (41 KB) 取得がすべてプロキシ経由で成功した。
検査を弱める設定なので、**ユーザーの判断なしには採用しない** (計画 §6.3)。Squid は名前解決後の宛先 IP を拒否するが、summaly 側の検査は弱まる。

なお、本番の `url-preview.ts` は `followRedirects: false` で呼ぶため、summaly の redirect 追跡 (グローバル `fetch` の HEAD) は使われない。http:// の URL は直通でも 301 を追わずに失敗する (環境変数の有無によらない)。

### この PR の内容と、環境変数を含めない理由

常に入れるもの:

- `configmap.yaml` の `default.yml` に `proxy: http://100.89.111.33:3128`。
- `allowedPrivateNetworks` に `100.89.111.33/32` **だけ**を追加 (`100.64.0.0/10` 全体は許可しない。既存の許可は削除しない)。`proxyBypassHosts` に外部宛先は追加しない。`objectStorageUseProxy` と DB は変更しない。
- `deployment.yaml` に Reloader の `configmap.reloader.stakater.com/reload: "iceshrimp-config"`。

**Web コンテナの環境変数 (`NODE_USE_ENV_PROXY`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`) は、ゲート不合格のため追加していない。** このため `config.proxy` を使わない次の経路は、この PR のマージ後も **直通のまま**である。

| 直通のまま残る経路 | 理由・影響 |
|---|---|
| URL プレビュー (`@iceshrimp/summaly`) | got に agent 指定なし。プレビュー先に自宅 IP が見える。環境変数を入れるとプレビュー自体が失敗する |
| 未キャッシュのリモートユーザーの followers/following 件数 (`person.ts` の node-fetch、agent なし) | プロキシ経由にならない。環境変数でなら経由する (試験済み) |
| 管理画面の latest-version / release 確認 (グローバル `fetch`) | 環境変数でなら https は経由する (試験済み) |
| Web Push (`web-push` のデフォルト agent) | 未試験。仕組みは `https.get` と同じなので環境変数で経由すると見られる |
| SMTP (`nodemailer`) | HTTP プロキシの対象外。本計画の範囲外 |

これらは **PR3 (NetworkPolicy) で遮断すると失敗する**。外部への直通許可は足さない (C-11)。ユーザー判断が要る選択肢:

| 選択肢 | 内容 | 影響 |
|---|---|---|
| A. 環境変数を入れ、summaly の検査を無効化 | 環境変数 + `SUMMALY_ALLOW_PRIVATE_IP=true`。全 HTTP(S) 経路がプロキシ経由になる | summaly の private IP 検査が無効になる (SSRF 防御は Squid の宛先 ACL に一本化)。PR3 後も全機能が動く見込み |
| B. 環境変数を入れず、URL プレビューを含む上記を直通のまま残す | この PR のまま | 自宅 IP が URL プレビュー先などに見える。PR3 の egress 制限を入れると該当機能が壊れる |
| C. 環境変数を入れ、URL プレビューは諦める | summaly を使わない (外部の `summalyProxy` サービスは新規常設サービスになり本計画の範囲外) | URL プレビューが失敗する。その他は経由する |

どれを採るかが決まるまで PR3 へは進まない。

### 追加の注意: http:// 宛先の扱い

Iceshrimp の独自 agent (hpagent 0.1.2) は、http:// の宛先にも `CONNECT host:80` を使う。Squid は CONNECT を 443 だけ許可する (C-05) ため、**`config.proxy` を設定すると、Iceshrimp の独自 agent では http:// (80 番) のリモートサーバーに接続できなくなる**。
これは環境変数の有無に関係せず、この PR の `proxy` 設定だけで起こる。443 の https:// が主流であり、Squid の CONNECT:80 を許すと内部・外部の拒否設計に例外が増えるため、今回は許可しない。
80 番だけで動くリモートや、配送の失敗が増えた場合は、`access.log` の `TCP_DENIED/403 CONNECT …:80` を確認する。

### 反映手順 (マージ後)

Flux が `apps` を反映すると、ConfigMap の変更を Reloader が検知して Pod を再作成する。

```sh
flux reconcile kustomization apps -n flux-system --with-source
kubectl -n iceshrimp rollout status deployment/iceshrimp-web --timeout=300s
```

Pod が再作成されたか (作成時刻が新しいか) を確認する。されていなければ、ConfigMap の取り込み順序を確認してから明示的に再作成する。

```sh
kubectl -n iceshrimp rollout restart deployment/iceshrimp-web
```

Pod 内の設定は、DB パスワードを出力しないよう `proxy` と許可リストの行だけを確認する。

```sh
kubectl -n iceshrimp exec deploy/iceshrimp-web -- grep -E "^proxy:|100\\.89\\.111\\.33" /iceshrimp/.config/default.yml
```

1 replica・Recreate のため、再作成中は短時間停止する (許容)。反映後の機能確認 (検索、フォロー、連合、リモートメディア、通常・大容量の R2 アップロード、Hubble での直通観測) は段階 4 の項目で行う。

### PR2 の切り戻し

`rollout undo` だけでは ConfigMap が戻らない。**PR2 を revert して、ConfigMap と Deployment の両方を戻す。** Pod の再作成後、直通構成の正常性を確認する。
**直通へ戻る前に Squid を止めない。**

## 運用

### 秘密情報

- CI の公開ログへ、他ノードの Tailscale IP、許可送信元、生成した `squid.conf`、Ansible の diff を出さない。ロールの該当タスクは `no_log`/`diff: false` を指定している。
- ホスト変数の編集は `sops ansible/inventory/host_vars/os3-387-26840/squid.sops.yml` で行う。値を端末やログへ貼らない。

### 手元適用 (例外)

CI の実行者制限で CI を実行できない場合だけ、手元適用を代替とする。構文エラー・check 失敗・apply 失敗を迂回する目的では使わない。

- レビュー済み main の同じ固定 revision から実行する。
- 対象ホストとタグを限定する。
- CI との Ansible・collections のバージョン差を解消するか、差を明示する。
- SHA、check の結果、実行者、CI を使えない理由、規約からの逸脱を記録する。ローカルの結果を「CI 成功」とは記録しない。

```sh
cd ansible
ansible-playbook playbooks/vps-squid.yml --limit os3-387-26840 --tags squid_forward_proxy --check --diff
ansible-playbook playbooks/vps-squid.yml --limit os3-387-26840 --tags squid_forward_proxy
```

### IP 変更

**cp-1 の再登録・配置変更で Tailscale IP が変わった場合**

1. 現在の送信元を非公開で確認する。
2. SOPS の `squid.sops.yml` の許可 `/32` を更新する (新しい値を追加し、新経路の成功後に旧値を除く)。
3. `vps-squid.yml` を check → apply する。
4. 許可元は cp-1 のみとし、Tailnet 全体に広げない。

**VPS の Tailscale IP が変わった場合**

更新箇所は次のとおり。新しい IP を平文で記載してよいかも確認し、値を一括置換して無検証で反映しない。

1. Ansible の `squid_forward_proxy_expected_tailscale_ipv4` (期待する待ち受け IP)。
2. Iceshrimp の ConfigMap の `proxy` と `allowedPrivateNetworks` (PR2)。
3. Deployment の `HTTP_PROXY`/`HTTPS_PROXY` (PR2)。
4. NetworkPolicy の `/32` (PR3)。
5. 本書と関連する試験手順。

実行中に IP が変わると、ロールは `tailscale ip -4` と期待値の不一致で失敗し、Squid を別アドレスで起動しない。

### Ansible 検証の落とし穴

- **`ss` などの実行結果を使う assert は、通常適用時にしか動かない。** check mode ではコマンドと assert を省くため、check の成功は、その検証が通ったことの証拠にならない。
  実際に、listener 検証の `regex_replace` で `'\1'` を書いていたため Jinja2 が制御文字 `0x01` と解釈し、初回の適用が失敗した (#210 で `'\\1'` に修正。check では検出できなかった)。
- ロールの Jinja2 式を変えたときは、check の成功だけで済ませず、期待どおりの入力で通ること、不正な入力 (例: `0.0.0.0` での listen) で失敗することを、ansible-core で式だけ評価して確認する。
- 通常適用が失敗したら、Squid 自体の状態 (`systemctl is-active squid`、`ss -lntp`) を先に確認し、切り戻しが必要な障害か、検証タスクだけの失敗かを切り分ける。

### ログ

- `/var/log/squid/access.log` と `cache.log` は `proxy:proxy`、ローテートは Debian の `logrotate` (日次、2 世代) に任せる。Squid 側は `logfile_rotate 0` で、回転せず再オープンだけを行う。
- `cache.log` は FD 警告や起動異常の観測に使う。試験用の query を使って、異常系でも秘密が記録されないことを確認する (手元の試験では `access.log`/`cache.log` に query 文字列は残らなかった)。

### 症状と確認

| 症状 | 確認 |
|---|---|
| Squid が起動しない | `journalctl -u squid`。`squid-wait-for-tailscale` が失敗していれば tailscale0 の IP を確認する。10 秒間隔で再試行される |
| 403 が返る | `access.log` の結果 (`TCP_DENIED/403`)。許可送信元・宛先 ACL・ポート (80/443 のみ) のどれかを確認する |
| 接続が失敗する | FD 警告 (`cache.log`)、`MemoryCurrent`、OOM、`NRestarts` |

## 切り戻し (PR1 の範囲)

- アプリは変更しない (PR1 の段階ではアプリは未接続)。
- Squid だけを止める場合は `squid_forward_proxy_enabled: false` にして適用する (導入済みなら停止・無効化される)。ロールを playbook から消すだけでは停止しない。
- grant だけを戻す場合は、追加した grant と tests だけを revert し、ACL 限定で apply する。Minecraft・LiveKit の既存設定は戻さない。
- PR2 以降の切り戻しは、各 PR で追記する。**直通へ戻る前に Squid を止めない。**
