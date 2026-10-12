# 0.8.1 の計画 — まだ断っていた頼みを全部受ける（2026-10-12）

## 0. きっかけ

0.8.0 のあと、道具とコードを見直して「友達が言いそうなのに断る頼み」を8つ見つけた。ユーザー「全て盛り込んでください」（2026-10-12）。
方針は 0.8.0 と同じ（docs/PLAN_0_8.md 第1章）: 原理上できることは断らない。時間・利用枠がかかるなら見込みを一度伝えて進める。前の版は消さずに残す。

| # | 頼み（例） | 0.8.0 | 0.8.1 |
|---|---|---|---|
| 1 | 設定に長めの指示を入れたい | 400字で断る（計画の 2000字をやり忘れた） | 2000字 |
| 2 | 一人称を「私」にしたい | 設定で足せるが、仕上げの指示書の例文「〜だと僕は考えています」とぶつかる | 例文から「僕」を外し、一人称は設定に従う（無ければ文体ガイドのまま） |
| 3 | 「音楽の章はいらない」「ファン心理の章を足して」「章の順番を変えて」 | 章立てが固定 | 道具 restructure（章を足す・外す・並べ替える） |
| 4 | 「この動画は関係ないから外して」「外した動画を戻して」 | 口が無い | 道具 videos の exclude・include |
| 5 | 「この動画も入れて」 | 口が無い（回り道だけ） | 道具 videos の add（属性・コメントを取り、ラベルを付け、関わる章を書き直す） |
| 6 | 「この動画のコメントを全部読んで」「全体でコメントをもっと」 | 掘り下げは界隈ごとだけ | deepen に videos・community="all"・per_video（件数か "all"） |
| 7 | 「前回からどれだけ伸びた？」「〇〇と△△を比べて」 | 比べたレポートは作れない | 道具 compare（2つ以上の分析を比べたレポート） |
| 8 | 「古い分析を消して容量を空けて」 | しまうだけ | archive_analysis の delete（ゴミ箱へ。戻せる）・info の storage（分析ごとの容量） |

## 1. 共通の仕組み: 「変更の回」

4・5・6 は、掘り下げ（deepen）の仕組みを広げて作る。analysis.json の `deepen` に回を積み、取得の係が取り足し、AI が関わるところだけ直す。

- 回の種類（`deepen.kind`）: `deepen`（界隈。今まで）・`recut`（今まで）・`videos`（動画ごとの取り足し・全体の取り足し）・`add`（動画を足す）・
  `exclude`・`include`（取得なし。すぐ済む）
- AI の仕事: `deepen`（界隈1つ）は今までどおり dcomments → outline_revise → review → … 。ほかは
  [label（ラベルの無い動画だけ）] → change_prep［service］→ dcomments×（関わる界隈）→ outline_revise（汎用の指示書 change_plan.md）→
  review（汎用の指示書 change_review.md）→ assemble → verify → export → done
- change_prep: 回の動画のラベルから関わる界隈を決める。外した動画は、コメントを読んでいた界隈だけ読み直す。どこにも出てこなければ（章・構成案・
  コメント分析・段階）、組み立てと検算だけ（AI の作業なし）
- 書いている途中（取得中・AI の途中）は、今までどおり積んで、終わったら始める（state/pending.json）。ただし外す・戻すの印そのものは、すぐ付ける
  （このあとの仕事から効く）

## 2. 道具ごと

### 2-1. 設定の字数（1）・一人称（2）

- user_settings.MAX_CHARS 400 → 2000。道具 settings の説明も
- finish.md: 「〜だと僕は考えています」→「〜だと考えています」。一人称は利用者の設定（文体）に指定があればそれ、無ければ文体ガイドの型
- write.md: 一人称の決まりを1行（設定 > 文体ガイド）

### 2-2. 章立てを変える（3。道具 restructure）

- 引数: remove（章の id）・add（[{title, about, after}]）・order（章の id の並び）・instruction
- 置き場: outputs/chapter_plan.json（[{id}…]。`path` は拡大経路の章（段階ごと）のまとまり。足した章は id `c1`… と title・about）。
  版を残す（keep_version）ので「前の版に戻して」で章立ても戻る
- 章の並び（chapter_order）・書く順（冒頭は最後）・仕事の列（_flow_tail・やり直し・直し）を、全部この表から作る。外した章のファイルは消さない
- 足した章の執筆: 汎用の決まり（利用者の言葉・構成案の計画・材料は read で全部）。見出しは `## <題>`
- 組み立てで `## N.` の番号を振り直す（足した章にも番号）。付録も
- 流れ: 書く前（構成案の前）なら表を書くだけ（最初から効く）。書いたあとなら 版を残す → change_plan（足した章の計画・書き直す章。冒頭は必ず）
  → 書き直し → change_review → 仕上げ → 組み立て → 検算 → 完了

### 2-3. 動画を外す・戻す・足す（4・5。道具 videos）

- action: exclude（外す）・include（戻す）・add（足す）。targets は動画の URL か seq
- 外す: analysis.json の `excluded_videos`。records・videos・labels・ledger・fetched・pool・label_seqs と取り足しの候補から外す（番号は変えない）
- 足す: 取得の前（一覧の段の前）なら `user_videos` に書き、一覧の段で必ず詳しく読む。そうでなければ取得の回 `add`:
  一覧（grid_links）に足す → 属性を取る（続きから）→ 番号を変えずに作り直す（prep_sample の --keep-seqs）→ コメントを取る
  （楽曲ページの一覧から。台帳に無ければその動画の音源のページから）→ ラベル → change_prep …
- 取り直し（restart_analysis）は `user_videos`・`excluded_videos` を引き継ぐ

### 2-4. 動画ごと・全体のコメント（6。deepen を広げる）

- deepen に videos（URL か seq）・community="all"・per_video（件数か "all"＝その動画のコメント数まで）
- 取る動画: 指定の動画（コメントが無ければ新しく、あれば続き）。all は全部の界隈の候補
- AI: 界隈1つなら今までの流れ、2つ以上・ラベルの無い動画があれば 第1章の汎用の流れ

### 2-5. 比べる（7。道具 compare）

- 引数: analysis_ids（2つ以上。曲名でも）・instruction
- 同じ曲の新旧なら、サービスが差を数える（UGC 数・投稿の期間・新しく出た動画・上位の動画の再生の伸び・界隈ごとの本数）
- 仕事: compare（AI。比べたレポートを書く。各分析の章は read の analysis_id で読める）→ compare_save［service］→ done
- 置き場: レポートのフォルダの「比較/<曲>と<曲>（日付）/比較レポート.md」。新しいほうの分析の仕事の列に足す（途中なら積む）
- 比べる相手が無い（同じ曲の分析が1つ）: 「最新の数字で集め直して（restart_analysis refresh）、できたら比べる」を返す。取り直し中の分析に頼めば積む

### 2-6. 消す（8。archive_analysis の delete・info の storage）

- delete=true: 分析のフォルダをゴミ箱へ移す（Finder の「戻す」で戻せる。ゴミ箱を空にすると容量が空く）。with_report=true でレポートのフォルダも
- 取得中・取り足し中は断る（先に cancel_analysis）
- info の about=storage: 分析ごとの容量・状態・しまったか・取り直したか、合計、空き容量

## 3. 道具の案内・文書

- 道具は 16 → 19（restructure・videos・compare）。mcp_proto の説明と INSTRUCTIONS の「この頼みにはこの道具」、codex_link の TOOLS と SKILL_MD、
  code_link の CLAUDE_MD、check_local_mcp、SYSTEM_MAP
- 始め方のページ・紙: 「途中からやり直したい・集め直したい」のカードに、章立て・動画の足し外し・比べる・消すを足す（0.8.1 以降の版で）
- LINE の下書き: 第6章に 0.8.1（0.8.0 をまだ送っていなければ、まとめた文面）

## 4. 確かめ

- 単体試験: 8つそれぞれ。変更の回は、外す（引用あり・なし）・戻す・足す（取得の前・後）・動画ごとの取り足し・全体
- 写しの置き場（output/fulltest_20261012_sb）で、AI 役のエージェントが会話を流す: 「音楽の章はいらない、代わりにファン心理の章を」
  「この動画は外して」「この動画のコメントを全部読んで」「前の分析と比べて」「古い分析を消して」
- 本物の TikTok での取得（足す・取り足し）は、運営の Mac のきゃわで1回

## 5. 進め方

- 取得側（acquire/・analysis/prep_sample.py・collector の知らせ）は手伝い、仕事の列・道具・文書は私
- 状態のファイル: docs/IMPL_0_8_1_LOG.md
- 出すとき: 版 0.8.1、build.sh → push → release.sh、始め方のページを公開し直す、LINE の下書き
