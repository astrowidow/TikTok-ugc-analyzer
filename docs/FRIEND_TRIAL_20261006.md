# 友達の運用評価（2026-10-06）のつまずきと直し（0.6.6）

ユーザーが友達に UGC Analyzer（0.6.4）と始め方のページを渡して運用評価した。出てきたつまずきと不具合、原因、決めたこと。
手順書は `docs/pages/guide.html`（始め方のページ）・`docs/FRIEND_GUIDE.md`・`docs/LINE_MESSAGES.md`、アプリは 0.6.6。試験は `tests/test_friend_trial.py`。

| 件 | 原因 | 直し |
|---|---|---|
| ① TikTok の登録で認証コード切れ | TikTok 側（メールのコードの期限・遅れ）。Google で作れば通る | 手順4に「Google で続ける」の Tips（ふだんの TikTok とつながっていない Google アカウントで） |
| ② 3の5の直後に「ローカルネットワーク上のデバイスを見つけることを許可しますか?」 | UGC Analyzer が Chrome を子として起こすので、Mac は Chrome の通信を UGC Analyzer のものとみなす | 手順3に窓の見本（「許可」）。窓に理由の文（Info.plist の NSLocalNetworkUsageDescription） |
| ③ 手順5の「開き直す」を押しても何も起きない | Claude は作業中なら終了の前に確認の窓を出す。その窓が後ろに隠れ、アプリは30秒で待つのをやめていた | 前に出してから終了を頼み、最長10分見張って自動で開き直す。手順5に Claude の窓の見本 |
| ④ 手順6のウェブ検索のオプションが無い | 新しい Claude の画面には切り替え自体が無い（公式ヘルプ） | 手順6はモデルだけに。完了の知らせ・困ったときも「あれば」の言い方に |
| ⑤ メニューバーにアイコンが出ない | アイコンが多すぎて入りきらず、見切れていた | 手順書に「見当たらないとき」の節（設定で減らす・間隔を詰めるコマンド・整理アプリ Thaw） |
| ⑥ 開始の返事が「途中で途切れた」 | Claude のチャットは道具1回 約60秒まで。start_analysis は運営の Mac でも 28〜42秒 | status に取っている楽曲ページ、指示に「途切れたら呼び直さず status」、道具の時間を記録 |

## ② ローカルネットワークの窓

- 窓の文言（macOS の NetworkExtension の文言表）: 「“UGC Analyzer”がローカルネットワーク上のデバイスを見つけることを許可しますか?」、ボタンは「許可しない」「許可」
- 運営の Mac のローカルネットワークの一覧（`/Library/Preferences/com.apple.networkextension.plist`）にも UGC Analyzer（旧名 UGC Collector、jp.ugc-analyzer.collector）が「許可」で載っていた。運営も最初に答えていた
- 試し（2026-10-06）: Chrome を子として起こすだけの小さなアプリを2つ作った（A: 今のアプリと同じ起動オプション、B: `--disable-features=MediaRouter` を足す）。**どちらも一覧に載った**。キャスト先探しを切っても窓は消えない。Chrome を子として起こす限り出る確認とみなし、消すのはやめた（Chrome を Mac の「開く」で起こすと Chrome 名義になるはずだが、取得の土台を変えるので見送り）
- 試しのアプリ「UGC試験A」「UGC試験B」は運営の Mac のローカルネットワークの一覧に残っている（macOS に消す手段が無い。アプリは一時フォルダにあり、動かない）
- 「許可しない」を押しても、Chrome とのやりとりは 127.0.0.1 だけ（許可の要らない範囲）なので取得は動く見込み。確かめてはいない。手順書は「許可」

## ③ Claude の「作業中です」の窓（Claude.app 2.19675.1 の中身で確かめた）

- Code タブや Cowork のセッションが動いていると、終了の前に出る。題「Claudeはまだ作業中です」、本文「Claudeは1件のセッションで作業中です。今終了すると、その作業が中断されます。」、
  ボタン「このまま終了」「Claudeの作業完了を待つ」（既定）「キャンセル」。Claude の窓が見えていればその窓にくっついて出る
- 前の作り（〜0.6.5）: 終了を頼んで30秒待ち、閉じなければ「閉じられませんでした」の通知（署名なしなので「スクリプトエディタ」名義。出ないことも）で終わり。あとで Claude が閉じても開き直さない
- 0.6.6（`collector_app/system.py` の relaunch_app、`app.py` の _restart_ai）: Claude（ChatGPT）を前に出してから終了を頼む → 8秒で閉じなければ「〇〇が終了してよいか聞いています」の通知 → 最長10分見張り、閉じたら開き直す → 閉じなければ窓（rumps.alert）で「⌘Q で終了してから開いて」。開き直しの最中にもう一度押しても2つめは走らせない

## ④ ウェブ検索

公式ヘルプ https://support.claude.com/en/articles/10684626-enable-and-use-web-search 「If you have the new Claude experience, there's no web search toggle. Claude searches the web when it helps.」
「+」→「Web search」が残るのは前の画面のアカウントと Team・Enterprise。くわしくは `docs/WEB_RESEARCH.md`。おすすめのモデル（手順6）はそのまま。

## ⑤ メニューバーのアイコン

- アプリは動いていた（AI が取得の状態を読めた。取得はメニューバーのアプリが始める）。見えないだけ
- **原因（ユーザーが友達の Mac で確かめた）: アイコンの数が多すぎて、入りきらない分が見切れていた。**「メニューバーに追加することを許可」は原因ではなかった
- macOS は入りきらないアイコンを何の印もなく隠す。新しく入れたアプリのアイコンは左端（隠れやすい側）に足されるので、最後に入れた UGC Analyzer が真っ先に隠れる。MacBook では致命的になりやすい（ユーザー）。運営の Mac（Mac Studio・大きなモニター）では起きない
- 決めたこと（ユーザー）: **Dock にアイコンを出す案はやめる。設定を直して正しい状態にするのを目指す**。友達が自分で直せるように、整理アプリとアイコンの間隔を詰めるコマンドを紹介する
- 手順書（始め方のページの「困ったとき」の節「割れた音符のアイコンが見当たらないとき」、紙も同じ）: まず「メニューバーに追加することを許可」で UGC Analyzer がオンか → 上から簡単な順に3つ
  1. 使っていないアイコンを消す: システム設定 →「メニューバー」で、「メニューバーに追加することを許可」の使っていないアプリをオフ、「メニューバーコントロール」の項目を「メニューバーに非表示」、「時計のオプション…」の「日付を表示」をオフ（日本語の表記は macOS の文言表で確かめた）
  2. 間隔を詰める: `defaults -currentHost write -globalDomain NSStatusItemSpacing -int 8; defaults -currentHost write -globalDomain NSStatusItemSelectionPadding -int 8` → 再起動（ログアウト・ログインでも効く）。戻すのは `defaults -currentHost delete -globalDomain NSStatusItemSpacing; defaults -currentHost delete -globalDomain NSStatusItemSelectionPadding` → 再起動。値 8 は既定の約半分（9to5Mac 2026-05-22）。書き方は試しのキーで確かめた（運営の Mac の本物のキーには触れていない）
  3. 整理アプリ **Thaw**（https://github.com/thaw-app/Thaw 、Ice のフォーク。無料・GPL-3.0）。リンクは https://github.com/thaw-app/Thaw/releases/latest/download/Thaw.dmg 。2026-10-06 時点の安定版 2.0.1（2026-09-02）、macOS 26.0 以降、.dmg もアプリも Apple の公証済み（`spctl` で Notarized Developer ID を確かめた。開くときの警告は出ない）。最初にアクセシビリティの許可を求める（画面収録は任意）。間隔を詰める機能もある
- Ice（jordanbaird/Ice）は安定版が 0.11.12（2024-10-29）で止まっていて、macOS 26 で隠したアイコンが戻らないなどの報告があるので案内しない

## ⑥ 開始の返事が途切れた

- Claude のチャット・Cowork は道具1回の待ちが約60秒（`docs/CODE_TAB_ONE_SITTING.md`）。start_analysis は返事の前に Mac が TikTok で楽曲ページを探す（運営の Mac で 27.9〜42秒。`docs/FULL_TEST_LOG.md` C1・`docs/STAGE4_LOG.md`）。友達の Mac で超え、Claude には時間切れ、Mac は裏で探し終えて取得を受け付けた
- chromedriver の初回ダウンロードは測ると約2秒で、主な原因ではない
- 2回目の頼みは「もう受け付けています」の返事になり、二重の取得はできない（ただし受け付け済みに気づく前に TikTok をもう一度探す）
- 0.6.6: status の返事に取っている楽曲ページ（題・作者・UGC 数・URL。`raw/music_pages.json` から）。AI への指示（`mcp_proto.START_CUT_RULE`。全体の指示と start_analysis の説明）に「途切れたら呼び直さず status を見て、受け付けていればそのページを伝える」。道具ごとにかかった秒数を `logs/mcp.log` に「道具の中身 〇〇: 〇秒」で残す
- **やらないと決めたこと（ユーザー）**: start_analysis に持ち時間をつけて60秒前に返す案はいらない
