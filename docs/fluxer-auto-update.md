# Fluxer 更新PRの運用

`fluxer-update.yml` は毎日 **08:17 JST**（UTC 23:17）に実行する。GitHub のスケジュール実行には遅延があり得る。手動実行は次のとおり。

```sh
gh workflow run fluxer-update.yml --ref main -f kind=all
# kind=charts / kind=images で片方だけ実行できる
```

更新があれば、`codex/fluxer-charts-updates` と `codex/fluxer-images-updates` に別々の PR を作成・更新する。同じ候補では再公開しない。自動マージは行わず、マージ後に Flux が本番へ反映する。片方の取得失敗はもう片方の更新を妨げない。

## 取得元と固定方法

- Chart: `fluxerapp/fluxer` の `main` を commit SHA に解決し、必要な5 chart と LICENSE だけを GitHub API から取得する。Git blob hash・サイズを確認し、`charts/fluxer/current/UPSTREAM.json` に commit と SHA256 を記録する。履歴全体は clone しない。chart 内容が同じなら upstream commit の進行だけでは PR を作らない。
- Image: 既存9 repository の公開 `v1` tag を Linux/amd64 manifest digest に解決する。manifest/config の digest と platform を確認し、9 HelmRelease と `images.lock.json` を更新する。同一 repository は全 workload で同じ digest に揃える。初期 lock の既存 index digest も、amd64 child と config を検証して保持する。
- PR 本文に取得元、digest、source revision、image の起動設定、互換性結果を記載する。各 component の `v1` は個別に公開されるため、同一 commit の一式である保証はない。

Renovate はこれらの chart・image 管理対象だけを除外する。LiveKit やその他のインフラ依存の Renovate 更新は継続する。

## CIとマージ前の確認

候補の prepare job は read-only token で取得・レンダリングする。publish job だけが組み立て済み patch を許可パスへ適用し、GitHub 標準の `GITHUB_TOKEN` で branch/PR を公開する。追加の PAT やクラスタ認証情報は不要。互換性チェックが失敗した候補は Draft PR にする。

公開後は候補 HEAD を指定して `fluxer-chart-check.yml` と `flux-check.yml` を dispatch する。両 workflow は実行 SHA の一致を検証する。自動 token で作られた PR の通常 CI が承認待ちになる場合に備え、再利用するのは明示 dispatch の run だけとする。CI は以下を確認する。

- upstream commit の実ファイルと vendor hash、immutable image manifest/config。
- 9 release・48 resource の inventory、namespace、selector、StatefulSet の immutable field、runtime/network spec、Helm 所有権と hook 禁止。
- image lock と render の一致、Flux revision suffix による不要な Pod 再起動の防止、Kubernetes schema。
- updater の失敗時復元、更新 branch の手動変更保護、同一候補の再公開防止。

CI は image layer を取得・実行せず、ログイン・送受信・upload・通話を確認しない。特に API は既存 TypeScript/NATS wrapper を維持するため、新しい image に wrapper が依存する実行環境・source file があるかは image config だけでは判定できない。image PR は上流変更と起動設定を確認し、必要なら検証環境で起動・接続・ログイン・チャット・upload・通話を試してからマージする。chart の runtime contract 変更が必要なら別途レビューし、失敗した候補に合わせて fixture を再生成しない。

マージ後は HelmRelease の Ready、Pod の restart・ログ、公開 endpoint と実動作を確認する。問題時は更新差分を revert して以前の chart/image pin に戻す。ただしアプリ側の DB migration や保存形式変更は Git revert だけで戻せるとは限らないため、上流の互換性・バックアップを確認する。

## 更新が止まった場合

Actions の実行結果と step summary を確認する。取得・検証失敗は次回に再試行する。prepare 中に main が進んだ場合は候補を公開せず次回へ延期する。

自動 branch への手動 commit/amend、PR 本文の記録 HEAD と branch HEAD の不一致、open PR のない同名 branch は保護のため停止する。公開直前の競合も期待 SHA を指定した force-with-lease で拒否する。手動修正は別 branch/PR に移し、必要な変更が保存されたことを確認してから元の自動 PR/branch を整理する。

push 後の PR API 失敗時は公開結果を再取得する。PR の marker が更新済みなら次回へ継続し、未更新なら自分が push した SHA に対する CAS で旧 HEAD へ戻す（その run で作った新 branch なら削除する）。結果を確認できない場合や人の更新があった場合は branch を保持して検査を促す。

同じ候補では commit・push・PR 本文更新を省略し、draft 状態の修復と失敗・未実行 CI の再 dispatch だけを行う。成功・実行中の CI は再利用する。手動で CI を再実行したい場合は、Actions UI から再実行するか対象 workflow をその branch と現在の HEAD の `expected_sha` で dispatch する。互換性失敗の原因を直した場合は次回の候補を確認する。
