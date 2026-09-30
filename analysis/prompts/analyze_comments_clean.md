# 課題: 代表動画のコメント分析（段階5。答えを見ない E2E 試験の前半）

対象楽曲は **KANA-BOON「シルエット」**（NARUTO 疾風伝 OP。2025年7〜12月に TikTok で再バズ）。
先行工程が 880本を分類し、界隈ごとの代表動画のコメントと返信を取得した。あなたは**コメントを読んで、動画ごと・界隈ごとに
「なぜこの界隈がこの曲を取り上げたか」「視聴者はどう反応したか」を根拠付きで書く**。レポート本体は別担当が書く。

## 絶対に守ること
1. この楽曲のバズについて Web 検索・取得をしない。外部資料は読まない（この工程は**データだけ**で行う）
2. `output/trial_silhouette/_contaminated/` と `docs/` は読まない
3. 最初に `e2e/report/prior_knowledge_analyst.md` に事前知識を申告する（知らなければそう書く）
4. 引用する cid は入力ファイルにあるものだけ。数値（いいね数等）は入力の値をそのまま

## 入力（`/Users/belle/workspace/TikTok-ugc-analyzer/output/trial_silhouette/`）
- `clean/taxonomy.json` … 分類軸（界隈・フォーマット・採用文脈の定義）
- `clean/labels.tsv` … 200本のラベル
- `clean/pathway.md` … 拡散経路の下書き（段階の区切りと代表動画。**コメントで裏付ける／反証する対象**）
- `reps.tsv` … 代表動画の一覧（界隈・段階）
- `e2e/comments/INDEX.md` … 取得できた動画の索引（**取得できたのは代表65本のうち一部**。どの界隈が何本かは索引で確認）
- `e2e/comments/<seq>_<video_id>.md` … 動画ごとのコメント（いいね順、cid 付き）と返信（親の下）。★=投稿者がいいね、📌=固定
- `output/notes_corpus/distilled/GLOSSARY.md` の **F 章（採用文脈・動機）と H 章（指標の読み方）** … 著者が反応をどう言語化するかの語彙。他の章は読まなくてよい

## 出力（`/Users/belle/workspace/TikTok-ugc-analyzer/output/trial_silhouette/e2e/report/`）

### 1. `prior_knowledge_analyst.md`（最初に）

### 2. `video_analysis.jsonl` — 動画ごと（1行1動画。取得できた全動画）
```json
{"seq": 57, "video_id": "...", "community": "...", "phase": "...", "n_comments": 120, "n_replies": 8,
 "why_this_song": "この界隈がこの曲を取り上げた理由の読み（1〜3文。動画の属性＋コメントから）",
 "reaction_types": [{"type": "本家探し|懐かしさ|難易度|比較|キャラ愛|ツッコミ|投稿者ファン|界隈外からの流入|その他", "share": "多|中|少", "evidence_cids": ["..."]}],
 "quotes": [{"cid": "...", "text": "...", "likes": 2435, "why": "何を示す引用か"}],
 "creator_engagement": "投稿者の返信・いいね・固定の有無と内容（無ければ null）",
 "reply_threads": "返信のやり取りから読めること（質問→回答、本家への誘導など。無ければ null）",
 "languages": {"ja": 0.8, "en": 0.1, "...": 0.1},
 "top20_vs_rest": "上位20件だけで同じ結論になったか（yes/no と一言）",
 "notable": "その動画特有の発見（無ければ null）"}
```
- `quotes` は動画あたり 2〜5件。`top20_vs_rest` は後でコメント数の最適化に使うので必ず書く

### 3. `community_synthesis.md` — 界隈ごとの統合
界隈ごとに: 取得できた代表動画（seq・再生・段階）／この界隈が曲を採用した文脈（動画属性＋コメント）／視聴者の反応の共通点と差／
段階による変化／最も説明力のある引用（cid 付き、界隈あたり3〜6件）／**先行工程のラベル（採用文脈）とコメントから読める実態のズレ**／
**pathway.md の記述をコメントが支持するか反証するか**。取得できなかった界隈は「根拠なし」と明記。

### 4. `reply_value.md` — 返信の価値の実測
取得した返信のうち、分析に実際に使った（引用した・結論を変えた）ものを列挙し、
「返信が無かったら何が言えなくなったか」「返信数が多いコメント／投稿者が反応したコメント／質問形のコメント のどれに価値が集中したか」を書く。
これはコメント・返信の取得量を決めるための材料になる。

### 5. `notes_analyst.md` — 迷った点・データに足りなかったもの

## 進め方
1 → GLOSSARY の F・H 章 → clean/ の3ファイル → INDEX.md → 全動画のコメントファイル（**1本読むごとに video_analysis.jsonl に追記**）→ 3 → 4 → 5。
完了したら、分析した動画数・引用した cid の数・reaction_types の集計を報告する。聞き返さず自分で決めて進める。
