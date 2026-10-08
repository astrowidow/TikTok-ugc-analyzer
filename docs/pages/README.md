# 友達に見せるページ（claude.ai で公開）

友達（利用者）にそのまま見せるページの置き場と、元のファイル。どれも claude.ai の「アーティファクト」として公開していて、運営の claude.ai のサイドバーにピン留めしてある。
一覧は claude.ai の https://claude.ai/code/artifacts （Claude Code のターミナルなら `/artifacts`）でも見られる。

| ページ | URL | 元のファイル | 中身 |
|---|---|---|---|
| UGC Analyzer の始め方 | https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC | `docs/pages/guide.html` | 入れ方（「開いていません」の越え方の画面の見本つき）・頼み方（実際の会話の吹き出し）・直し方・困ったとき。2026-10-06 |
| きゃわぽっぴんどぅー分析の実例 | https://claude.ai/artifact/PhDfG1qMWM2z9bMHHN2iu6 | `output/case_kyawa_20261008_074/` の `build_page.py` で作る（output/ は Git に入れていない。前の版の元は `output/case_kyawa_20261006_064/`・`output/case_kyawa_20261006/`） | 実際の会話と、できたレポート3本（第1版・掘り下げ・切り直し）。2026-10-08 に 0.7.5 で0から通して作り直した（取得も同じ日）。載せるレポートは利用者のフォルダの「レポート.md」 |

- UGC Analyzer のダウンロード: https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg （始め方のページの手順2のボタン・紙・LINE の文面はこのリンク）。GitHub の公開リポジトリのリリースなので、友達は登録なしで落とせる。リンクはいつも最新のリリースを指すので、版を上げても直さなくてよい。載せるのは `collector/release.sh`（`collector/build.sh` のあと、コミットを push してから）。.dmg には著者の記事（知識ベース）が入っていて、リンクを知っていれば誰でも取れる（2026-10-06、それを伝えたうえでユーザーが本体のリポジトリを選んだ）
- 公開のしかた: できたページは本人（運営）しか開けない。友達に見せるときは、ページ右上の「共有」から「リンクを知っている人」に開く。始め方のページは実例のページへリンクしているので、両方を開く
- 直すとき: 元のファイルを直して、同じファイルで公開し直す（URL は変わらない）。別の会話からは URL を渡して公開し直す
- 友達向けの紙 `docs/FRIEND_GUIDE.md` は、このページの元になった1枚もの。数字や手順を変えたら、両方を直す
- 前に作った「UGC Analyzer かんたんガイド」（claude.ai の Doc、https://claude.ai/artifact/ExZT6tYLWyP8XdLDY2i2Sw ）は、始め方のページに置き換えた古い版
