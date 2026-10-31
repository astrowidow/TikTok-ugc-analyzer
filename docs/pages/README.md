# 友達に見せるページ（claude.ai で公開）

友達（利用者）にそのまま見せるページの置き場と、元のファイル。どれも claude.ai の「アーティファクト」として公開していて、運営の claude.ai のサイドバーにピン留めしてある。
一覧は claude.ai の https://claude.ai/code/artifacts （Claude Code のターミナルなら `/artifacts`）でも見られる。

| ページ | URL | 元のファイル | 中身 |
|---|---|---|---|
| UGC Analyzer の始め方 | https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC | `docs/pages/guide.html` | 入れ方（「開いていません」の越え方の画面の見本つき）・頼み方（実際の会話の吹き出し）・直し方・やり直し方・新しい版にするとき・困ったとき。2026-10-06（2026-10-12 に 0.8.0 の節を足した。Version 21。同じ日に 0.8.1 のカード（章立て・使う動画・比べる・消す・一人称）を足し、入れ替えの手順に「いまの版は UGC Analyzer 0.8.1」と書いた。Version 23。版を上げたら、この「いまの版」を直す（collector/release.sh が食い違いを止める）。2026-10-31 に「新しい版にするとき」を自動のアップデートの節「アップデート」に書き直し、画面の文言の見直しに合わせて、0.9.5 を友達に出すときに公開し直した（Version 24）。0.9.6 を友達に出すときに公開し直した（Version 25。いまのバージョンは 0.9.6）） |
| UGC Analyzer 自動の更新の見本（運営向け。友達には見せない） | https://claude.ai/artifact/UCfG8e4oXC2F6GdZY2nB8N | `docs/pages/update-preview.html` | 0.9.0 の自動の更新の見え方（メニュー・窓・通知・手順書の書き直し・運営の流れ）。2026-10-31 にユーザーが確かめた（メニューの「新しい版を確かめる…」をいつも出すように直した Version 2）。仕組みは `docs/AUTO_UPDATE.md` |
| きゃわぽっぴんどぅー分析の実例 | https://claude.ai/artifact/PhDfG1qMWM2z9bMHHN2iu6 | `output/case_kyawa_20261008_074/` の `build_page.py` で作る（output/ は Git に入れていない。前の版の元は `output/case_kyawa_20261006_064/`・`output/case_kyawa_20261006/`） | 実際の会話と、できたレポート3本（第1版・掘り下げ・切り直し）。2026-10-08 に 0.7.5 で0から通して作り直した（取得も同じ日）。載せるレポートは利用者のフォルダの「レポート.md」 |

- UGC Analyzer のダウンロード: https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg （始め方のページの手順2のボタン・紙・LINE の文面はこのリンク）。GitHub の公開リポジトリのリリースなので、友達は登録なしで落とせる。リンクはいつも最新のリリースを指すので、版を上げても直さなくてよい。載せるのは `collector/release.sh`（`collector/build.sh` のあと、コミットを push してから）。0.9.0 から2段: `release.sh` は試し版（リンクは前の版のまま）、ユーザーが「友達に出して」と言ったら `release.sh --friends 〇〇 --note "…"` でリンクの先と友達のアプリの更新がその版になる（`docs/AUTO_UPDATE.md`）。.dmg には著者の記事（知識ベース）が入っていて、リンクを知っていれば誰でも取れる（2026-10-06、それを伝えたうえでユーザーが本体のリポジトリを選んだ）
- 公開のしかた: できたページは本人（運営）しか開けない。友達に見せるときは、ページ右上の「共有」から「リンクを知っている人」に開く。始め方のページは実例のページへリンクしているので、両方を開く
- 直すとき: 元のファイルを直して、同じファイルで公開し直す（URL は変わらない）。別の会話からは URL を渡して公開し直す
- 友達向けの紙 `docs/FRIEND_GUIDE.md` は、このページの元になった1枚もの。数字や手順を変えたら、両方を直す
- 前に作った「UGC Analyzer かんたんガイド」（claude.ai の Doc、https://claude.ai/artifact/ExZT6tYLWyP8XdLDY2i2Sw ）は、始め方のページに置き換えた古い版
