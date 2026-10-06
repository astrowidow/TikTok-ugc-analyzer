# 通し試験のプロンプト（全機能・運営の Mac で。2026-10-06 版、UGC Analyzer 0.5.8 まで）

これまでに作ったもの（全部アプリ・名指しの決まり・楽曲ページ探し・取る動画の仕様・2回着席・界隈の確認と省略・検算・直し・掘り下げ・切り直し・設定・指示書・知識ベース・やめ／やり直し）を、
**2つの面から**確かめるための依頼文:

1. **中から**（A 章、重み大）: コードを領域ごとに全部読み、文書の仕様と突き合わせ、不具合の疑いを単体試験にして確かめる。網羅（試験で通る行の割合）と静的な検査も測る
2. **外から**（B〜E 章）: 友達と同じ使い方で1曲通し、時間と質を前の数字と比べる

変わった数字は文書に直す。下の「---」から下を、このリポジトリで開いた新しい Claude Code の会話にそのまま貼る（曲を変えるときは〔 〕の中を変える）。

- 結果の記録: `docs/FULL_TEST_LOG.md`（走るたびに節を足す）
- 道具: `analysis/fulltest/`（`ugc.py` 道具の呼び出し口・`AI_ROLE.md` AI 役の指示のひな形・`fill_role.py` ひな形に道具の説明を差し込む・`turn.sh` 会話の1往復を記録・`watch_acq.py` 取得の見張り）
- かかるもの: 約5〜6時間（うち取得 2〜3時間。A 章は取得の間に並べて進める）。子の AI で約400〜600万トークン（A 章の読む係6人 約200〜300万・第1版 約100万・掘り下げ 約35万・切り直し 約50万・採点 約10万×5。推測）

---

UGC Analyzer の通し試験をしてください。手順と合格の目安は `docs/FULL_TEST_PROMPT.md`（この文書）の第1〜6章、前の数字は第3章です。
取得（C1・C2）がいちばん長いので先に始め、取得の間に A 章（コードと内部の試験）と B・D 章を進める。

あなたは「試験の係」です。友達の役（利用者の言葉を言う）と運営の役（記録・見張り・採点・文書の直し）をし、**Claude デスクトップの中の Claude の役（AI 役）は、サブエージェントにやらせます**。
AI 役には `analysis/fulltest/AI_ROLE.md` に道具の説明を差し込んだもの（`AI_ROLE_full.md`）だけを渡し、リポジトリを読ませません（本物の Claude も道具しか持っていない）。

曲は〔シルエット／KANA-BOON〕。記事を正解にした採点ができ、前の版が多いため。新しい曲で試すときは第4章の採点のうち対の比較と記事基準を省き、絶対の目安（検算・言葉の検査・利用者の手間）だけで見る。

## 0. 守ること

- 返事・記録・コミットメッセージは日本語（途中の一言も）
- 始める前に `ListAgents` でほかのセッションを見て、**取得用の Chrome（9250・9251）と TikTok のアカウントを使う予定が無いか**、作業フォルダの未コミットの変更が誰のものか、アプリの入れ替えの予定が無いかを `SendMessage` で確かめる。入れ替え（`collector/install.sh`）は取得中は止まる作りなので、相手の入れ替えが近いなら、それを待ってから取得を始める
- 本物の置き場（`~/Library/Application Support/UGC Analyzer/`）で作るのは、この試験の分析1つだけ。**設定・指示書・やめ／やり直し・界隈の確認を省く試しは、写しの置き場**（`output/fulltest_<日付>_sb/home`、`HOME_DIR` に場所を書く）でやる。本物の置き場の設定・指示書・ほかの分析は書き換えない
- python は PID で止める（名前で止めない）。秘密をリポジトリに書かない。友達向けの文面では「UGC Analyzer」とだけ書く
- 利用者の流れ（何回聞かれるか・どこで止まるか）を変える直しは、作らずに案として報告する。文書の数字の直しは進めてよい
- コミットしたら push まで（確かめは要らない）。ほかのセッションの未コミットの変更は自分のコミットに混ぜない（`git add` はファイルを名指し）

## 1. 準備

1. 読む: `docs/SYSTEM_MAP.md`・`docs/STAGE4_LOG.md` の最後の3節・`docs/COMMENT_TARGETS.md`・`docs/DEEPEN_COMMUNITY.md` の 3-1・`docs/RECUT_COMMUNITY.md` の 1〜3章・`docs/FULL_TEST_LOG.md`（前の走行）
2. 走行のフォルダ `output/fulltest_<日付>/`（`work/` も）を作り、その中で
   `../../collector/.venv/bin/python ../../analysis/fulltest/ugc.py tools > tools.txt`、`python3 ../../analysis/fulltest/fill_role.py .`
3. 写しの置き場: 本物の置き場から `analyses`・`analyses-archive-*`・`chrome-profile`・`logs`・`レポート*`・`code`・`locks`・`app.pid` を除いて `output/fulltest_<日付>_sb/home/` に写し、
   `home/analyses/` を空で作り、`output/fulltest_<日付>_sb/HOME_DIR` にその場所を書く。tools.txt を写して fill_role.py を走らせる
4. 利用者の発言は、AI 役に渡す前に `ugc.py say user <ファイル>`、AI 役の最後の返事は `ugc.py say ai <ファイル>` で記録に残す（時刻が測れるように）

AI 役の起こし方: `Agent`（general-purpose・裏で）に「最初に <走行のフォルダ>/AI_ROLE_full.md を Read で全部読み、役と決まりに従ってください。\n\n利用者の発言:\n<言葉>」。
同じ会話の続き（「OK」など）は `SendMessage` でその AI 役に送る。友達が新しい会話を開く場面（取得のあとの「続けて」など）は、新しい AI 役を起こす。

## 2. 試験の項目

### A. コードと内部の試験（中から。重み大。取得の間に並べて進める）

作業用の道具は、アプリの作り直しに使う環境（collector/.venv）を汚さないよう、作業用の置き場に入れる:
`collector/.venv/bin/python -m pip install -q --target <作業用の置き場>/pytools coverage pyflakes`（以下 `PYTHONPATH=<作業用の置き場>/pytools` を付けて使う）

| # | やること | 合格・記録すること |
|---|---|---|
| A1 | 単体試験: `collector/.venv/bin/python -m unittest discover -s tests -t .` | 全部 OK（0.5.8 で 113本）。本数を記録 |
| A2 | 網羅: `python -m coverage run --source=.,collector/collector_app --omit='tests/*,output/*,analysis/comment_exp/*,analysis/comment_pace/*,analysis/fulltest/*,*/.venv/*,collector/build/*' -m unittest discover -s tests -t .` → `coverage report --sort=cover` | 全体と、主なファイル（proto_runner・flow_w1・mcp_proto・acquire/*・analysis/pool・collector_app/*）の割合を記録し、前の走行と比べる。2026-10-06 の始め: 全体 46%（mcp_proto 18%・app 22%・spatest 25%・proto_runner 54%・flow_w1 62%）。A6 の試験を足したあとにもう一度測る |
| A3 | 静的な検査: `python -m pyflakes proto_runner.py flow_w1.py mcp_proto.py prompt_store.py kb_update.py user_settings.py acquire/*.py analysis/pool.py analysis/attr_cluster.py collector/collector_app/*.py scraper.py` | 未使用の import と f 文字列を除いた指摘を1件ずつコードで確かめ、誤検知か不具合かを書く |
| A4 | 入っているアプリの道具の自己点検: `collector/.venv/bin/python tests/check_local_mcp.py --app "/Applications/UGC Analyzer.app" --no-network` | 「結果: 全部 OK」 |
| A5 | 版と道具とつなぎ: 入っている版（`defaults read "/Applications/UGC Analyzer.app/Contents/Info" CFBundleShortVersionString`）と `collector/collector_app/__init__.py` の版・tools.txt の道具の数・`~/Library/Application Support/Claude/claude_desktop_config.json` の `ugc-analyzer`・`~/.codex/config.toml` の道具ごとの許可・`~/.agents/skills/ugc-analyzer/`・`collector/install.sh --dry-run`（作った .app があるとき） | 版が同じ（違えば、誰の未コミットの変更かを書く）。道具 13（start_analysis・status・next_task・submit・revise・settings・read・deepen・recut・prompts・restart_analysis・cancel_analysis・update_knowledge）。Codex の許可の名前が道具と同じ |
| A6 | **コードを読む**: 下の6領域に1人ずつ読む係（サブエージェント、general-purpose、裏で同時に）。各係は担当のファイルを全部読み、仕様の文書と突き合わせ、不具合の疑いを「正しい動きを期待する単体試験」として `tests/test_review_<領域>.py` に書いて走らせる（失敗＝不具合の確認）。ソース・既存の試験・文書は直さない。TikTok・Chrome・本物の置き場・本物の設定ファイルには触れない | 係の報告（重さ・場所・起き方・試験の名前と結果・直し方の案）を、試験の係がコードと試験で確かめ直してから記録に書く（係の報告をそのまま信じない） |
| A7 | 文書とコードの突き合わせ: 文書に書いた定数・本数・時間・道具の数（`docs/SYSTEM_MAP.md`・`docs/COMMENT_TARGETS.md`・`docs/DEEPEN_COMMUNITY.md`・`docs/RECUT_COMMUNITY.md`・`docs/FRIEND_GUIDE.md`）と、コードの定数（`acquire/pipeline.py` の DEFAULTS・`acquire/launch.py` の見込み・`acquire/deepen.py` の EST・`analysis/pool.py`・`acquire/recut.py` の RULE） | 食い違いを一覧にし、文書が古ければ文書を直す（第5章）。コードが仕様と違えば不具合として A6 と同じに扱う |

A6 の領域（係に渡す依頼文の雛形は 2026-10-06 の走行の記録 `docs/FULL_TEST_LOG.md` にある）:

| 領域 | 全部読むファイル | 仕様の文書 | 主な観点 |
|---|---|---|---|
| R1 取得の段と見込み | acquire/pipeline.py・launch.py・worker.py・notify.py・collector_app/jobs.py・worker_entry.py | SYSTEM_MAP・COMMENT_TARGETS 4章・FRIEND_GUIDE | 段の再開・失敗と知らせ・ロック・やめた分析・眠らせない処理・残り時間の見込み（楽曲ページ2つ以上） |
| R2 取る動画の決まり | analysis/pool.py・attr_cluster.py・acquire/spatest.py | COMMENT_TARGETS・COMMENT_SPEED 7章・COMMENT_ACQUISITION_HANDOVER 3・4章 | 仕様の表とコード・境目・1ページで止める判定・要求の間隔・混入の防ぎ方 |
| R3 道具の入口と分析の管理 | mcp_proto.py・collector_app/mcp_local.py・proto_runner.py | SYSTEM_MAP・DEEPEN 3-1・RECUT 2章・FRIEND_GUIDE | 楽曲ページ探しと選び方・やめ／やり直し・曲名の当て方・引数の引き継ぎ・状態の保存・道具の説明とコードの食い違い |
| R4 W1 の仕事と検査 | flow_w1.py・prompt_store.py・user_settings.py・service_prompts/w1/*.md | W1_WORKFLOW・STAGE4_LOG | 指示書の差し込みの印・利用者の編集の効き方・accept の検査の抜けと誤り・検算・ZIP・2回 submit |
| R5 掘り下げと切り直し | acquire/deepen.py・recut.py と flow_w1・proto_runner の関わる所・指示書 | DEEPEN_COMMUNITY・RECUT_COMMUNITY・COMMENT_TARGETS 6章 | 取る動画と25分の配り方・打ち切り・原本への合わせ方・history・続けて使ったときの状態 |
| R6 アプリの殻と配り方 | collector_app/app.py・config.py・chrome.py・claude_link.py・codex_link.py・system.py・notify.py・selftest.py・__main__.py・install.sh・build.sh・UGCCollector.spec・kb_update.py | SYSTEM_MAP・FRIEND_GUIDE・CHATGPT_HANDOVER | 固めたアプリへの同梱の漏れ・設定の書き換えの冪等さ・初回の自動つなぎ・install.sh の判定・知識ベースの週1 |
| R7 Code タブの一気通貫（0.6.0〜） | collector_app/code_link.py・waiter.py・__main__.py・mcp_proto.py と app.py の 0.6.0 の差分・tests/test_code_tab.py | CODE_TAB_ONE_SITTING・FRIEND_GUIDE と guide.html の Code タブの節 | 待つ命令が終わり・失敗・やめた・時間切れで正しく抜けるか・待つ分析の当て方・作業フォルダ・「Code タブのときだけ」の判定がふつうの会話や ChatGPT で誤って効かないか・同梱 |

新しい部品が入ったら、領域を足す（その版の差分 `git show <コミット>` を読む係を1人）。

A6 で書いた試験の扱い: 通る試験は網羅を上げるものとしてそのまま残す。不具合を確かめた（失敗する）試験は `@unittest.expectedFailure` を付け、記録の不具合の番号を書き添えて残す（試験の全体は緑のまま、直したら印を外す）。不具合の直しはユーザーの判断のあと。

### B. 名指しの決まり（写しの置き場。AI 役2回）

| # | 利用者の言葉 | 合格 |
|---|---|---|
| B1 | 「〔シルエット／KANA-BOON〕を分析して」 | 道具を1回も呼ばず、ふつうに答える（記録に tool の行が無い） |
| B2 | 「〔シルエット〕の分析を続けて」（写しの置き場には分析が無い） | status だけ呼び、分析が無いと分かったら道具なしで答える |

### C. 本線（本物の置き場。分析1つ）

| # | 利用者の言葉・できごと | 測ること | 合格の目安 |
|---|---|---|---|
| C1 | 「UGC Analyzer で〔シルエット／KANA-BOON〕を分析して」 | start_analysis の秒数・見つけた楽曲ページ（題・UGC 数）・合わせた／外したページ・見込みの時間と時刻 | 数十秒。どのページで進めるかを省かずに伝え、最後に come_back_line（「約〇時間かかります（〇時ごろ…）…「〇〇の分析を続けて」と頼んでください…」）を言い換えずに入れる。利用者に確かめを求めない |
| C2 | 取得（見張り: `python3 analysis/fulltest/watch_acq.py <分析フォルダ> acquisition 330` を裏で） | 段ごとの時間（一覧・属性・プール・コメント）・動画の数・コメントを取った本数と件数・混入・20件頭打ち・ブロック・届かない動画・描画の見張り（collector.log の「描画:」）・Mac を眠らせない（-i）の始まりと戻し | 混入0・20件頭打ち0・blocked false・描画 OK（★止まっているが出ても確かめ直して取れていればよい）。全体の時間は見込み（C1）の ±30% |
| C3 | 取得の途中で、新しい AI 役に「〔シルエット〕はどうなってる？」 | status の返事 | 今の段と残りの見込みを返す。取得を始め直さない |
| C4 | 取得の途中で、新しい AI 役に「〔シルエット〕の分析を続けて」 | next_task | kind=wait で止まり、待つように伝える。AI が待ち続けない |
| C5 | 取得のあと、新しい AI 役に「〔シルエット〕の分析を続けて」 | 界隈の案（ask_user）までの時間・仕事の数・差し戻し | 約5分。案は初出順・代表動画のリンクつき |
| C6 | 同じ AI 役に「OK」 | 完成（done）までの時間・仕事の数・差し戻しの数と理由・検算（verify.json の errors・warnings）・完了の知らせ | 30〜45分。検算の errors 0・warnings 0（0.5.8 で誤報0）。完了の知らせにレポート・note 用原稿・Excel 用 ZIP のリンク、「数字の一部は運営が確認中」が出ない |
| C7 | できたもの | 「レポート」フォルダ（レポート・note 用原稿・Excel 用 ZIP・素材）・REPORT.md の字数と章・曲全体の UGC 数で語っているか・読者に見せない言葉（「用語集の通り」「指示書」「seq」「cid」など）が本文に無いか | 全部ある。見せない言葉0 |
| C8 | 新しい AI 役に「〔シルエット〕のレポートの〇章に〜を足して」（読んで足りないと思った点を1つ） | revise の受付・直しの時間・変わった章 | 頼んだ章だけ変わり、前の版が history に残る |
| C9 | 新しい AI 役に「〔シルエット〕のレポートの〇〇界隈のところ、なぜバズったのかが弱い。もっとコメントを取って掘り下げて」（界隈はレポートを読んで決める） | deepen の返事（取る本数・見込み・come_back_line）・取り足しの実際の時間と本数・件数・届かない動画 | 見込みは25分以内。実際の時間は見込みの ±50%（0.5.8 で見込みを実測に直した） |
| C10 | 通知のあと、新しい AI 役に「〔シルエット〕の分析を続けて」 | 書き直しの時間・仕事の数・差し戻し・変わった章・検算 | 15分前後。検算の誤報0 |
| C11 | 新しい AI 役に「〔シルエット〕の〇〇界隈を2つに分けて」など、界隈の分け方を変える頼み（レポートを読んで決める） | recut の受付・切り直しとラベルの付け直しの時間・取り足しが要ったか（要れば come_back_line と時間）・書き直しの時間 | 足りていれば着席1回で新しい版まで、足りなければ最初に知らせて2回。取り足しの実際は見込みの ±50% |
| C12 | 取得・取り足しのあと | `collector/install.sh --dry-run` | 止める理由が無い（取得の印が残っていない） |

### D. 写しの置き場で試すもの（AI 役。TikTok には楽曲ページ探しだけ触る）

| # | 利用者の言葉 | 合格 |
|---|---|---|
| D1 | 「UGC Analyzer で〔新しい曲／アーティスト〕を分析して」→「〔新しい曲〕の取得をやめて」→「〔新しい曲〕を、このページでやり直して <楽曲ページの URL>」→「〔新しい曲〕の取得をやめて」 | 楽曲ページ探しが新しい曲でも当たる（題・作者・UGC 数）。やめる・URL でやり直す（新しい分析）・やめるが通る。写しの置き場なので取得は始まらない |
| D2 | 「UGC Analyzer の設定を見せて」→「UGC Analyzer で、今後はレポートの文体をもう少しくだけた感じにして」→「UGC Analyzer の設定を元に戻して」 | settings の get・set（style）・reset が通り、戻したあと空になる |
| D3 | 「UGC Analyzer の完了の知らせの指示書を見せて」→「その指示書を、最後に『お疲れさまでした』と添えるように変えて」→「UGC Analyzer の指示書を全部初期に戻して」。ほかに試験の係が直接、差し込みの印（`{{…}}`）を消した全文で `prompts set` | get・set・reset が通る。印を消した書き換えは断られる |
| D4 | 「UGC Analyzer の知識ベースを更新して」 | note の新着を確かめて返す（新着があれば取り込みの仕事の案内） |
| D5 | 完成済みの分析（取得まで済んだもの）を、AI の仕事が無い形で写し（`outputs/`・`state/` を除き、目録の deepen・deepen_history・recut を消す）、「〔曲〕の分析を続けて。界隈の確認はいらないので、そのまま最後まで書いて」 | next_task に skip_confirm=true を付け、界隈の案で止まらずにラベルの仕事へ進む（`outputs/confirm_answer.json` に auto=true）。確かめたら AI 役を止めてよい（完了の知らせの中身は単体試験 test_skip_confirm が見ている） |

### E. 質の採点（シルエットのとき。子の AI。条件を伏せる）

| # | 物差し | 道具 | 前の数字 |
|---|---|---|---|
| E1 | 記事を正解にした大局（流れ・理由づけ・核・示唆、各1〜5） | `analysis/comment_exp/grading/article_macro.py build <版>`（版を `grading/versions.json` に足す）→ 採点者に `ARTICLE_MACRO.md` → `agg` | 本番 13、元の量の写し 13・13、1ページの版 14（12〜14） |
| E2 | 大局の対の比較（本番・元の量の写しと） | `pair.py --macro build silhouette new:prod new:b1` → 採点者に `PAIRWISE_MACRO.md` → `agg` | 元の版どうしのぶれ ±1 |
| E3 | コメント由来の発見の対の比較 | `pair.py build silhouette new:prod new:b1` → `PAIRWISE.md` | 元の版どうしのぶれ ±1 |
| E4 | 掘り下げ・切り直しの前後 | E1 を第2版・第3版にも | 掘り下げで大局は動かない（12→12）。切り直しは核が動き得る |

採点者は1束に1人（general-purpose、`GRADER.md`・`PAIRWISE*.md`・`ARTICLE_MACRO.md` と束のファイルだけを読ませる）。
今回の分析は楽曲ページの範囲が前の版と違うことがある（C1 で確かめる）。違えば採点の解釈に書く。

### F. 利用者の手が要るもの（試験の係はやらない。一覧にして報告で渡す）

- ChatGPT の Mac アプリ（Work）で同じ一言が通るか（0.4.0 で確かめた以降、道具が増えた: deepen・recut・skip_confirm）
- Mac の通知が実際に画面に出るか（署名なしのため osascript で出している。collector.log の「通知の許可: False」は既知）
- 友達向けのページ（https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC）の時間が、第5章で直した数字と合っているか

## 3. 前の数字（比べる相手。2026-10-06 朝まで）

| 何 | 前の数字 | 出どころ |
|---|---|---|
| 取得の全体 | 2〜3時間（きゃわ 1,711本で2時間34〜35分、見込み約2時間） | `docs/FRIEND_GUIDE.md`・事例（`output/case_kyawa_20261006`） |
| 一覧と属性 | 1〜1.5時間 | `docs/COMMENT_TARGETS.md` 4章 |
| コメントの段 | シルエット79本で約45〜55分（1本0.55分＋準備10分） | 同上・`acquire/pipeline.py` の min_per_page_video |
| 界隈の案まで | 約5分 | `docs/FRIEND_GUIDE.md` |
| 「OK」から完成 | 30〜45分（本番4件で28〜42分、きゃわ 29分） | 同上 |
| 掘り下げの取り足し | 見込み: 楽曲ページ1つ1.5分＋新しく1本1.0分＋続き1本3.0分、枠25分（0.5.8）。実際: きゃわ 7分（9本）・シルエットの写し 17.5分（9本） | `acquire/deepen.py` の EST・`docs/DEEPEN_COMMUNITY.md` |
| 掘り下げの書き直し | 12分（きゃわ）、14仕事・差し戻し0（シルエットの写し） | 同上 |
| 切り直し | 切り直し5分＋取り足し1〜2分（見込み14.2分）＋書き直し23分（きゃわ） | `docs/RECUT_COMMUNITY.md` |
| 検算の誤報 | 0.5.8 で0件（前は最大15件） | `docs/STAGE4_LOG.md` 0.5.8 の節 |
| 取得の質 | 混入0・20件頭打ち0・ブロック0 | 事例の collector.log |
| 質（シルエット） | 記事基準の大局 12〜14（本番13）。対の比較のぶれ ±1 | E1・E2 |

## 4. 記録

`docs/FULL_TEST_LOG.md` に、走行ごとの節を足す: 版・曲・分析 ID・各項目の結果（合格／不合格／気づき）・A6 の不具合の一覧（番号・重さ・場所・起き方・試験の名前・直し方の案）・網羅の前後・第3章と同じ表の「今回の数字」・採点・直した文書・残件。
走行のフォルダ（`output/fulltest_<日付>/`、git の対象外）に log.jsonl・watch の出力・採点の束が残る。

## 5. 文書への反映の決まり

- 今回の数字が第3章の幅から外れたら、その数字を書いている文書を直す。場所の目安:
  取得の全体・完成までの時間 → `docs/FRIEND_GUIDE.md`・`docs/LINE_MESSAGES.md`・`docs/SYSTEM_MAP.md` 3章（友達向けのページ `docs/pages/guide.html` は、直す文を報告に書き、公開し直しは担当のセッションかユーザーに任せる）／
  コメントの段・一覧と属性 → `docs/COMMENT_TARGETS.md` 4章／掘り下げ → `docs/DEEPEN_COMMUNITY.md`／切り直し → `docs/RECUT_COMMUNITY.md`／この文書の第3章
- 幅の中なら、数字は直さず `docs/FULL_TEST_LOG.md` に「幅の中」と書く
- コードの定数（見込みの式など）がずれていたら、直す案（どの定数をいくつに・根拠）を報告に書く。直すのはユーザーの判断のあと（アプリの作り直しと入れ替えが要るため）
- 作業記録 `docs/STAGE4_LOG.md` に1節（何を試し、何が変わったか、直した文書）

## 6. 報告（ユーザーへ）

短く: 全体の合否 → **コードを読んで確かめた不具合（重い順。友達への配布に響くものを先に）** → E2E の項目ごとの不合格と気づき（直すなら案）→ 網羅の前後 → 前と変わった数字（表）→ 直した文書 → 利用者の手が要るもの（F）。
