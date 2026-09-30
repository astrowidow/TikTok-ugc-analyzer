# 課題: 分析レポートの執筆（段階6。答えを見ない E2E 試験の後半）

対象楽曲は **KANA-BOON「シルエット」**（NARUTO 疾風伝 OP。2025年7〜12月に TikTok で再バズ）。
先行工程が「分類軸・200本のラベル・拡散経路の下書き」と「代表動画のコメント分析・界隈統合」を作った。
あなたは著者（山本慶太朗 / Hi-Trink Inc.）の**枠組みと文体で**、人間のアナリストが書くのと同じ体裁・粒度の分析レポートを仕上げる。

## 絶対に守ること（試験の条件）
1. **この楽曲のバズについて Web 検索・取得をしない。** 外部資料は下記の「層1」「層2」だけ
2. `output/trial_silhouette/_contaminated/` と `docs/` は読まない
3. `output/notes_corpus/` の記事本文は **`ALLOWED.txt` に載っているファイルだけ**読んでよい（載っていないものは対象楽曲に触れているので禁止）
4. 最初に `e2e/report/prior_knowledge_writer.md` に事前知識を申告する（知らなければそう書く）
5. **数値は必ずデータから引く。** 再生数・本数・日付・いいね数は入力の値をそのまま。推測は「推測」と書く。存在しない動画・コメント・出来事を書かない

## 入力

### 層1: 著者の分析の言語（全部読む）
`/Users/belle/workspace/TikTok-ugc-analyzer/output/notes_corpus/distilled/` の `README.md` → `GLOSSARY.md` → `STYLE_GUIDE.md` → `cards.jsonl`

### 層2: 近い過去レポートの全文（6〜8本）
`cards.jsonl` から、本楽曲に近いもの（アニメ主題歌／リバイバル／海外発／振付フォーマット／UGC 数が大きい曲／公式が後追いだった曲 など）を
自分で 4〜6本選び、必ず次の2本を加える: `2024-08-22_n06c909376d1b.md`（はいよろこんで）、`2024-11-19_nf643834b934f.md`（一目惚れ）。
全文は `/Users/belle/workspace/TikTok-ugc-analyzer/output/notes_corpus/<file>`（`ALLOWED.txt` にあるものだけ）。
選んだ理由を `notes_writer.md` に書く。

### 対象楽曲のデータ（`/Users/belle/workspace/TikTok-ugc-analyzer/output/trial_silhouette/`）
- `clean/taxonomy.json` / `clean/labels.tsv` / `clean/pathway.md` / `clean/notes.md` … 分類と経路の下書き
- `llm_input/records.jsonl` … 全880本の属性（数値の出典）。`weekly.tsv` … 週次
- `reps.tsv` … 代表動画。`e2e/comments/INDEX.md` … コメントを取得できた動画（**代表のうち一部のみ**）
- `e2e/report/video_analysis.jsonl` / `community_synthesis.md` / `reply_value.md` … コメント分析の結果（**主な根拠**）
- `e2e/comments/<seq>_<video_id>.md` … 引用を検証したいときだけ開く（全部読む必要はない）

## 出力（`/Users/belle/workspace/TikTok-ugc-analyzer/output/trial_silhouette/e2e/report/`）

### 1. `prior_knowledge_writer.md`（最初に）

### 2. `REPORT.md` — 分析レポート本体
**STYLE_GUIDE の「TikTok 版の標準章立て」と GLOSSARY A 章の固定手順に従う**（型判定 → UGC 規模 → 拡大経路 → 音楽的特徴 → 楽曲構成 → 時代背景 → 結果 → 再現性）。
- 拡大経路は段階ごとに「何が起きたか → 代表動画（seq・投稿者・日付・再生）→ コメントが示す動機（cid 引用）→ 観測と推測の区別」
- 界隈ごとの分析はコメント引用を根拠に。取得できなかった界隈は「根拠薄」と明記
- **音楽的特徴・楽曲構成・時代背景の章は、本試験ではデータ（音源・歌詞・年代別リスナー）が無い。** 著者の型どおり見出しは立て、
  「人の考察を入れる場所」と書いて空けるか、コメント・属性から言えることだけを短く書く（例: 切り出し箇所は提案語や説明文の歌詞引用から推定できる範囲で）。**埋めない**
- 「なぜバズったか」は GLOSSARY の I 章（因果パターン）と J 章（否定された通説）に照らして書く。**J 章にある結論を書かない**
- 「バズった結果」は TikTok 内のデータで言えることのみ。TikTok 外は「本データでは扱えない」と明記
- 「再現性のある要素」は著者の型（自分でコントロール下に置ける要素だけ）で
- 付録: 使ったデータ・件数・取得できなかったもの・信頼度の注意
- 文体は著者本人（STYLE_GUIDE の後期トーン）。一般論を書かない。すべての段落にデータ上の根拠（数値・seq・cid）

### 3. `notes_writer.md` — 層2の選定理由、迷った点、先行工程への差し戻し事項、人の考察が要る箇所の一覧

## 進め方
1 → 層1（4ファイル）→ 層2（選定して全文）→ 対象楽曲のデータ → REPORT.md → notes_writer.md。
完了したら、REPORT.md の章ごとの行数、引用した cid の数、層2に選んだ記事を報告する。聞き返さず自分で決めて進める。
