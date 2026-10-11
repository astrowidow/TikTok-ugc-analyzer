# 0.8.0 の作業記録（状態のファイル。起こされたら最初にこれを読む）

計画は `docs/PLAN_0_8.md`。ユーザーの方針は計画の第1章。

## 分担（同じファイルを同時に触らない）

| 担当 | ファイル | 中身 |
|---|---|---|
| 本体（私） | proto_runner.py・mcp_proto.py・acquire/*・tests/test_flex_080.py | 取得のやり直し（restart_analysis を広げる・start_analysis の fresh・同じ曲の判定にアーティスト）・info・status・read・cancel の不具合・頼める数・ページ6つ・全体の指示・道具の説明・codex_link の道具一覧はアプリ側の担当に頼む |
| アプリ側（手伝い1） | collector/collector_app/*・tests/test_app_080.py | 版のずれの見張り（LocalHooks.version_check）・アプリの状態（LocalHooks.app_info）・初めて開いたときの窓と開き直すボタン・通知のログ・2つ目の .app・知識ベースの同梱物・ディスクの空き |
| AI の段（手伝い2） | flow_w1.py・service_prompts/w1/*・新しい redo_flow.py・tests/test_redo_080.py | redo の中身（前の版を残す・段ごとの消す表・仕事の積み直し・見込み・戻す）・レポートのフォルダの前の版・直しの複数章・段階の数の検査 |

## 進み具合

- [x] 計画（f9d53cb）
- [x] 本体: 目録に版・page_choice・info・status・read
- [x] 本体: 取得のやり直し
- [x] アプリ側（手伝い1。filecmp を spec に足した・SystemExit を捕まえる・やめた通知の文は私が直した）
- [ ] AI の段（手伝い2）
- [ ] 本体: redo・revise・deepen・recut の道具をつなぐ・完成前の頼みを積む・25分の枠を外す
- [ ] 全体の指示・道具の説明・ChatGPT のスキル・Code タブ
- [ ] 試験（単体・入れ替えの場面）
- [ ] build・運営の Mac に入れる・友達と同じ状態の通し（AI 役）・本物の TikTok で取り直し2本
- [ ] 文書（始め方のページの更新の節・紙・LINE・FULL_TEST_PROMPT の U1〜U5・点検表・SYSTEM_MAP）
- [ ] リリース 0.8.0・ページの公開し直し・LINE の下書きをユーザーへ

## メモ
- 本体（10/12 午後）: 作った版・取った版の記録（launch.new_analysis の app_version、pipeline の段ごとの app_version）、同じ曲の判定にアーティスト（launch._same_song）・脇へ置いた分析（superseded_by・archived_at）を引き当てから外す、
  restart_analysis を全状態に（新しい分析として取り直し・前は残す・属性/コメント/サムネを使い回す・add/remove/fan_sounds/read_more/refresh/resume）、start_analysis の fresh と「違う頼みは取り直す」、
  ページ6つで切らない、頼める数で断らない、空き容量、raw/page_choice.json、cancel で取り足しもやめる、status に日付・版・使ったページ、info（analysis_info.py）・archive_analysis、
  read の名前の一覧・glossary の不具合・chapters/appendix/memo/verify、途中の直し・掘り下げ・切り直しを積む（state/pending.json → 完了の知らせのときに始める）、掘り下げを時間で切らない（deepen.EST budget_min 0）、字数 8000
- 試験: 意図して変えた振る舞いの試験を直した（test_review_r3・test_recut・test_deepen・test_review_r5）。道具の数の試験は redo を足してから
- 文書（10/12 夕）: 始め方のページに「新しい版にするとき」の節と「途中からやり直したい・集め直したい」のカード（0.8.0 以降の版で）、紙・LINE 第4章、FULL_TEST_PROMPT に U（版の入れ替え）と 2-2 点検表、SYSTEM_MAP の道具16、DEEPEN_COMMUNITY の Q2
- 試験: tests/test_flex_080.py（25本。友達の行き止まりの再現を含む）通過。全体は手伝い2の flow_w1.py の作業中で落ちている（redo_block 未定義など）。手伝い2の報告を待って redo を道具につなぐ
- 10/12 03:46 運営の Mac を 0.8.0 に（友達と同じく .dmg を Finder と同じ形で置き換え）。U1: 0.7.5 の道具は置き換えたあともエラーを出さずに 0.7.5 のまま動き続けた（友達の Mac と同じ）。
  新しいアプリを開くと「版が変わりました…前の版の道具の処理: mcp 2つ」→ 窓 → 開き直す（8秒後に押された）→ Claude の道具は 0.8.0 に。
  0.8.0 の写しを置き換えると、道具が気づいて「新しくなりました。開き直して」だけ返す（status・info・next_task とも）
- 次: 本物の置き場にきゃわの古い形の写し（a20261006-0407-60f4 から、集め終わり・書く前）→ AI 役に友達の発言 → 「この音源も入れて」で本物の取得 → レポートで自己紹介の波
