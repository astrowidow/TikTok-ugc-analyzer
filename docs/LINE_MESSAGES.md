# 友達に LINE で送る文面の下書き（運営向け）

使い方のページ「UGC Analyzer の始め方」（https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC 、元は `docs/pages/guide.html`）と組で使う。友達はページを見ながらひとりで入れる（通話で付き添う前提にしない。2026-10-06 ユーザー）。〔 〕の中は送る前に埋める。

---

## 1. 事前に聞く（U1。配る前）

> 〔名前〕さん、前に話した TikTok の曲の分析ツール、そろそろ使ってもらえそうです。準備のために4つだけ教えてください🙏
>
> 1. 画面左上のりんごマーク →「この Mac について」を開いて、出てきた画面のスクリーンショット（または写真）を送ってください
>    （2020年以降の M1・M2・M3・M4 か、macOS 13 以降かを確かめます）
> 2. その Mac は会社の管理しているもの（会社から支給・会社のソフトが入っている）ですか？
> 3. Google Chrome は入っていますか？
> 4. AI は Claude と ChatGPT のどちらを使っていますか？ Claude なら有料プラン（Pro 以上）か、ChatGPT ならプランは何か（無料・Plus など）も教えてください。
>    Mac のアプリ（デスクトップ版）は使っていますか？

答えの扱い: 1 で「チップ」が Intel、または macOS が 12 以前（Monterey など）、2 が会社の管理なら、その人は UGC Analyzer を使えないことがある（アプリは Apple シリコン・macOS 13 以降で作っている。`docs/ALL_IN_APP_PLAN.md` 第6章）。運営と相談。
4 が ChatGPT なら、ChatGPT の Mac アプリ（Work の画面を使う）に macOS 14 以降が要る。

## 2. 入れてもらうとき（使い方のページと .dmg のリンク）

> UGC Analyzer（分析のアプリ）の準備ができました！
> 使い方はこのページにまとめています。上から順に進めれば、20分ほどでひとりで入れられます（つまずきやすいところは画面の見本つき）:
> https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC
>
> アプリはこちらからダウンロードしてください:
> https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg
>
> TikTok は、ふだんのアカウントとは別の、分析専用のサブアカウントでログインします。持っていなければ、ページの手順4で作れます。
> わからないところがあれば、いつでも LINE ください。

## 3. 入れ終わったと連絡が来たら（使い方の念押し）

> 設定おつかれさまでした！使い方はこの2つだけです。
>
> ①「UGC Analyzer で〇〇（曲名）／△△（アーティスト名）を分析して」と AI に言う（ChatGPT の人は、アプリの Work で）
> 　※ モデルは、Claude なら Opus 5.5（推論の強さ Medium 以上）、ChatGPT なら GPT-6.1 Sol（Medium 以上）がおすすめ
> 　※ Claude の人は、送る前に入力欄の「＋」→「ウェブ検索」をオンに（「続けて」を別の会話で送るときも）
> 　※「UGC Analyzer で」を付けたときだけ分析が始まります。付けなければ、AI はふだんどおりに答えます
> 　→ 2〜3時間くらい、Mac を開いたまま・電源につないでおいてください（画面は暗くなってOK、ふたは閉じない）
> ② 終わったら「〇〇の分析を続けて」と言う
> 　→ 界隈の案に「OK」か直しを言えば、40分〜1時間で完成します（そのあいだ Claude（ChatGPT）も開いたまま）
>
> 困ったら、画面右上の割れた音符のアイコンの1行目を見て、わからなければ LINE ください。
> 使い方のページ: https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC

## 4. アプリを新しくしたとき

> UGC Analyzer を新しくしました（〔直したこと〕）。
> 割れた音符 →「終了」→ 最初と同じリンクから新しいものをダウンロードして、前と同じように「アプリケーション」へドラッグして入れ直してください（「置き換える」を選んでOK。データとログインはそのまま残ります）:
> https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg
> 最初の1回だけ、また「このまま開く」が出たら同じように開いてください。
