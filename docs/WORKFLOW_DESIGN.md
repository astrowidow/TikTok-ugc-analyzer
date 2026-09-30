# ワークフローの洗い出しと、小機能への切り分け方針（2026-09-14）

位置づけ: `docs/ANALYSIS_PIPELINE.md` 第9章の叩き台を置き換える設計書。**方針は第6章のとおり決定済み（2026-09-14 ユーザー回答）。実装は第5章の順で。**

**何を作るか（ユーザー、2026-09-14）**: 今回の成果物は**下回り（下準備）**。いま見えている具体的なワークフローは「TikTok の特定楽曲バズ分析」（W1）だけで、
今後思いついたワークフローを**あとから低コストで追加・削除・編集（どこに AI を入れるかを含む）**できることが要件。
取得は媒体ごとに実装し、それを包む関数はできるだけ共通にする。第1章のユースケース列挙は**「どれを作るか」の選択肢ではなく、下回りが破綻しないかを試すためのインプット**。

**前提（ユーザー、2026-09-14）**: これは著者の過去記事を再現する試みではなく、**全く新しい試み**。W1 に著者の note 記事（層1・層2）が使えたのは**たまたま**であって、
ワークフローの根拠ではない。したがって本書は **対象 × 問い × 媒体 × 頻度** から洗い出し、既存資産（記事・蒸留した知識ベース・指示書）は「たまたま使える部品」として最後に対応づける。

根拠にしたもの: 第8章の E2E 結果（`output/trial_silhouette/e2e/E2E_EVAL.md` / `COMPARISON.md`）、引き継ぎ書（第0・4・7-C・7-D・8章）、
現行コード（`scraper.py` / `store.py` / `analysis/*.py` / `analysis/prompts/*.md`）、TikTok・YouTube で実際に取れるデータの形。

---

## 1. ありうるワークフロー（下回りを試すためのインプット。作る対象は W1 のみ）

この章の用途は「第2章の小機能と第3章の下回りで、将来のワークフローが**新しい関数をほとんど足さずに**組めるか」を紙の上で確かめること。
確かめた結果は第2章末の行列にある。**今回実装するのは W1 だけ**で、他は着手しない。

### 1.1 洗い出しの軸

| 軸 | 値 |
|---|---|
| **対象**（語りの主語） | 音源（楽曲）／動画1本／アカウント／ハッシュタグ・チャレンジ・フォーマット（音源をまたぐ振付など）／界隈・ジャンル（複数音源の集合）／複数対象 |
| **問い** | どう広がったか（経路の記述）／なぜ伸びたか（因果）／なぜ止まったか（伸び悩みの診断。同じ道具で符号が逆）／今どうなっているか（追跡）／次に何が起きうるか（類推・提言。人の領域が大きい） |
| **媒体** | TikTok／YouTube（長尺・Shorts）／その他（Instagram Reels 等。取得手段が未定なので本書では設計対象外、ただし構造は同じ） |
| **頻度・粒度** | 単発／定期（差分）／束ね（多数の対象を薄く） |

### 1.2 対象はデータの形で3つに畳める（設計上いちばん重要な整理）

対象が何であれ、**手に入るデータの形**は3つしかない。この形がパイプラインの骨格を決める。

| 形 | 母集団 | 時系列の元 | 骨格 |
|---|---|---|---|
| **集合型** | 多数の投稿（音源・タグ・フォーマット・界隈で束ねる） | 投稿日時 | 誰が・いつ・どう使ったか → 分類 → 段階 → 代表 → コメントで動機 → 経路と因果 |
| **単体型** | 1本の投稿 | **コメントの投稿時刻だけ**（TikTok も YouTube API も再生数の履歴は返さない） | 誰が見て・どう反応したか → 流入元（言語・提案語・時刻） → 投稿者の平常との差 → 同時期の文脈（同音源の前後・同タグ） |
| **系列型** | 1アカウントの投稿列 | 投稿日時 ＋ 各投稿の反応 | どの投稿が外れ値か → 外れ値に単体型を適用 → 前後で何が変わったか |
| 横断 | 完了した分析の成果物 | — | 比較／定点（差分）／束ね（ダイジェスト）／類推（データの無い対象に過去事例を当てる） |

現行の W1 は「集合型 × 経路と因果 × TikTok × 単発」。**媒体は取得（A 層）にしか効かず、形は整形（B 層）と LLM（C 層）に効く。**
YouTube でも3つの形は全部ありうる（Shorts の楽曲 UGC＝集合型、MV 1本＝単体型、チャンネル＝系列型）。違うのは取得の可否だけ（1.3）。

### 1.3 洗い出し

| ID | ワークフロー | 形 | 媒体 | 入口 | 主な問い | 取得の可否（現時点） | たまたま使える既存資産 |
|---|---|---|---|---|---|---|---|
| **W1** | 楽曲 UGC の時系列 **＝現行** | 集合 | TikTok | 音源 URL／曲名 | 経路・因果 | 実証済み（880本・27本のコメント） | 全工程の指示書、層1（著者の記事の蒸留）、E2E の成果物 |
| **W2** | 特定動画がバズった理由 | 単体 | TikTok | 動画 URL | 因果（誰が見た・なぜ反応した） | A3 ○ ／ A4 ○（1本を厚く）／ A5（本人の直近投稿）**未検証** | C4（コメント分析）の一部 |
| **W3** | 特定アカウントがバズった理由 | 系列 | TikTok | ユーザー名 | 転機（どの投稿で何が変わった） | A5 **未検証**。フォロワー推移は取れない（取得時点のみ） | なし（W2 を束ねる） |
| **W4** | ハッシュタグ・チャレンジ・フォーマット起点 | 集合 | TikTok | タグ URL | 経路・因果 | A2 がそのまま動く（`タグ検索_オリジナル曲.csv` が実例） | W1 と同じ |
| ~~W5~~ | 楽曲 UGC の時系列（Shorts） | 集合 | YouTube | 曲名／音源 | 経路・因果 | **一旦なし**（ユーザー判断）。Data API に「使用楽曲」の切り口が無い。Web の Shorts 音源ページは API 外・未検証 | なし |
| **W6** | 特定動画（MV・長尺・Shorts） | 単体 | YouTube | 動画 ID | 因果 | **API で完結**（videos / channels / commentThreads、ログイン不要） | なし |
| **W7** | チャンネルの成長 | 系列 | YouTube | チャンネル ID | 転機 | **API で完結**（playlistItems で全投稿、登録者数は概数の現在値のみ） | なし |
| **W8** | 伸び悩みの診断 | 集合 | TikTok／YouTube | 音源 URL | なぜ止まったか（W1 の問い違い） | W1 と同じ | W1 と同じ。B1 に停滞検出、C3/C6 に問いの変種 |
| **W9** | 定点観測 | 横断 | 全部 | 既存の分析 | 追跡（前回との差分） | 再取得を新しい run として記録 | `store.py` の `run_id` 設計そのもの |
| **W10** | 対象間の比較 | 横断 | 全部 | 完了した分析2つ以上 | 比較 | 取得なし | 共通スキーマが揃えば成立、揃わなければ不成立 |
| **W11** | 束ね（多数の対象のダイジェスト） | 横断 | 全部 | 対象リスト | 概況 | 各対象を縮小版で（サンプル小・コメント少or無） | 小機能に予算パラメータがあれば新規関数なし |
| **W12** | 類推（データの無い対象。リリース前・企画段階） | 横断 | — | 対象の記述 | 次に何が起きうるか | 取得なし。**完了した分析の蓄積**から近い事例を引く | W10 の派生。人の判断が主 |
| M1 | 知識パックの更新 | 運用 | — | 完了した分析／外部文書 | — | — | D1（蒸留）の一般化 |
| M2 | 答え合わせ | 運用 | — | 完了した分析＋人の参照分析 | — | — | E1／E2 |

読み方:

- **単体型の時系列はコメント投稿時刻だけ。** 「いつ・どこから視聴者が来たか」は `comment.created_at` × `language` × 提案語 × 反応型で読む。
  E2E で27本に対して手で行った読み（COMPARISON #16 #25 #35）を関数（B6）にする。これが W2・W6 の本体で、W3・W7 も外れ値の投稿に同じものを当てる
- **YouTube は「単体・系列は API で完結、集合は取得できない」。** 取得の差であって、形の差ではない。集合型（W5）は一旦なし（第6章 Q4）
- **W8 は W1 と同じ道具で問いが逆**（離陸したのに止まった週、界隈が増えなかった、など）。B1 に停滞の検出、C 層に問いの変種を足すだけ
- **W9・W10・W11・W12 は新しい関数がほとんど要らず、共通スキーマと予算パラメータと蓄積があれば構造上落ちてくる**
- **知識パック（層1）は「たまたまあった」もの。** 新しい形・媒体では無い状態から始まる。だから **C 層は知識パック無しで動くこと** が要件で、
  無い場合は「観測／推測／言えないこと」の汎用の章立てで書き、**完了した分析の成果物（軸・段階・統合・レポート）を次のパックに蓄積する**（M1）。
  著者の記事の蒸留は「TikTok 集合型パックの初期値」に過ぎない

### 1.4 形ごとに変わるもの・変わらないもの（切り方の根拠）

| 変わるもの | 集合型 | 単体型 | 系列型 | 横断 |
|---|---|---|---|---|
| 入口と母集団 | 音源／タグ／検索 → 投稿一覧 | 動画 → 本人の直近＋同音源・同タグの前後 | アカウント → 投稿一覧 | 完了した分析 |
| 分類の対象 | 投稿（誰が・何を・なぜ） | コメント（反応型・流入元） | 投稿（外れ値か、フォーマット） | — |
| 集計の軸 | 週 × 分類 × 段階 | コメント時刻 × 言語 × 反応型 | 投稿時刻 × 反応（平常値との乖離） | 指標の対比／差分 |
| 代表選定と予算 | セル配分、時間予算から本数 | 1本を厚く（cap 大・返信全部） | 外れ値 N 本＋前後 | — |
| 執筆の骨格 | 経路 → 段階ごとの動機 → 因果 | 反応の推移 → 流入元 → 因果 | 転機 → 前後の差 → 因果 | 対比表 → 差の説明 |
| 評価 | 人の参照分析があれば E1、無ければ E2 | E2 | E2 | E2 |

| 媒体で変わるもの | TikTok | YouTube |
|---|---|---|
| 取得の経路 | 未ログイン headless（一覧・属性）＋ ログイン実画面（コメント、Windows 機、セッション束縛） | Data API（ログイン不要、割当 10,000 unit/日） |
| 無い属性 | 再生数の履歴、投稿時点のフォロワー数 | 投稿地域、提案語、保存・リポスト、コメント言語 |
| 母集団の作り方 | 音源ページ・タグページのグリッド（推薦順で入れ替わる） | チャンネル・検索・人手リスト（音源ページは API 外） |

**変わらないもの**: 投稿・投稿者・コメントの実体、「入力束を並べる → 誰かが作る → 検証して取り込む」という LLM 工程の型、数値・引用の検算（E2）、
再開可能な実行、時間予算の消費、人の確認点。**変わるものは全部「パラメータ」か「差し替え可能な部品」にできる。**

---

## 2. 小機能カタログ v2

凡例: **[P]** 媒体依存 / **[D]** 決定論（依存なし） / **[L]** LLM（プロンプト＋スキーマ） / **[H]** 人 / **[E]** 評価

### A層: 取得 [P] — インターフェース `Source`（媒体ごとに実装）

| # | 関数 | 入力 → 出力 | TikTok 未ログイン（`scraper.py`+`enrich.py`） | TikTok ログイン実画面（`spatest.py`） | YouTube Data API v3 |
|---|---|---|---|---|---|
| A1 | `resolve` | 曲名・アーティスト → Source | Web 検索で music URL（既存 `find_tiktok_music_url`） | — | `search.list`（100 unit/回） |
| A2 | `list_posts` | Source → Post[]（ID と最小属性） | グリッド走査（既存）。**推薦順で時間とともに入れ替わる** | 同一セッションでの再走査（`--collect-scrolls`） | `search.list` / `playlistItems.list`（1 unit） |
| A3 | `get_post` | post_id → Post 全属性 ＋ Creator ＋ サムネ画像 | `__UNIVERSAL_DATA_FOR_REHYDRATION__`（既存 `extract()`、約40項目、3.2秒/本） | — | `videos.list`＋`channels.list`（1 unit ずつ） |
| A4 | `get_comments` | post_id, cap, reply_policy → Comment[] | **不可** | UI 操作＋fetch 横取り（既存。1本≈3分） | `commentThreads.list`＋`comments.list`（1 unit/100件） |
| A5 | `list_posts_by_creator` | creator_id → Post[] | **未検証**（プロフィールページのグリッド走査。音源ページと同じ作りのはず） | 未検証（SPA 遷移） | `playlistItems.list`（uploads） |
| A6 | `search_posts` | タグ／クエリ → Post[] | タグページのグリッド走査（A2 と同じ、実例あり） | — | `search.list` |

- TikTok は **2つのアダプタ**（未ログイン／ログイン実画面）。同じ媒体でも A2 と A4 が別プロセス・別機械（Windows 機）で走る。
- **セッション束縛**: TikTok の A4 は「一覧を取った同じグリッド」でしか到達できない（7-D）。A2 と A4 の間に人の工程（軸の確認・代表の確認）が挟まるので、
  実行時は「A4 の直前に同一セッションで A2 を再走査し、見つかった分だけ取り、見つからなかった代表は同セルから代替する（B10）」ループになる。
- YouTube の制約は**割当**（既定 10,000 unit/日。`search.list` が 100、他は 1）。母集団を検索で作ると 100 回/日が上限（50件/回 → 5,000件/日）。
  コメントは安い（1万件で約100 unit）。

### B層: 整形 [D] — 共通スキーマの上で動く純関数

| # | 関数 | 入力 → 出力 | 使う形 | 現状の実体 |
|---|---|---|---|---|
| B1 | `timeseries` | Post[] → 週次本数・再生、離陸週、**段階境界の候補**（構成が入れ替わる週・ピーク週・**停滞の始まり**） | 集合・横断 | `prep_sample.py`（週次まで） |
| B2 | `sample` | Post[]＋予算 → 層化サンプル（軸提案用／ラベル用） | 集合・系列 | `prep_sample.py` |
| B3 | `sheets` | サムネ → 一覧画像（20枚/枚、seq 付き） | 集合・系列 | `build_llm_input.py` |
| B4 | `allocate_cells` | Label[]＋段階＋予算（時間・本数）→ Selection（セル・必須枠・平方根配分） | 集合 | `select_reps.py`（暫定） |
| B5 | `comments_md` | Comment[]＋cap＋返信方針 → 動画ごと Markdown | 全部 | `prep_comments.py` |
| B6 | `comment_timeline` | Comment[] → 時刻ビン × 言語 × 反応型の推移、**タイムスタンプ言及の抽出**（「1:23」型） | **単体・系列** | E2E で手作業（新規） |
| B7 | `outliers` | Post[]（1アカウント）→ 平常値との乖離、外れ値の前後 | 系列 | 新規 |
| B8 | `diff_runs` | run A, run B → 新規・削除・数値差・新しい分類値 | 横断（W9） | 新規（store の run_id で可能） |
| B9 | `taboo` | 対象の名前（曲・アーティスト・アカウント・動画題名）→ 禁則語 → 知識パックの除外リスト | 全部（評価時） | `INDEX.json` の `taboo_hits`（手作業） |
| B10 | `substitute` | 未到達の Selection → 同セルの代替候補 | 集合 | 新規（7-D の結論） |
| B11 | `context_window` | 動画1本 → 同音源・同タグの前後 N 日の投稿、本人の直近 M 本 | 単体 | 新規（A2 部分実行＋A5 の組み合わせ） |

### C層: LLM [L] — 「入力束 → 生成 → 検証・取り込み」の型（3.4）

| # | タスク | 入力 → 出力（スキーマ） | 変種の軸 | 現状 |
|---|---|---|---|---|
| C1 | `propose_axes` | サンプル（＋知識パックがあれば）→ Taxonomy | **対象が投稿かコメントか** × 媒体。集合型は「誰が・何を・なぜ」、単体型は「反応型・流入元」 | `classify_clean.md` 前半（投稿のみ。反応型は `analyze_comments_clean.md` に固定語彙で埋め込み → 提案制に変える） |
| C2 | `label` | Post or Comment ＋ Taxonomy → Label（conf・reason） | 同上。Batch・構造化出力 | 同 後半 |
| C3 | `draft_pathway` | Label＋週次＋段階候補 → 段階の命名と経路の下書き | 形（集合＝段階／系列＝転機）× 問い（伸びた／止まった） | 同（pathway.md） |
| C4 | `analyze_comments` | 動画ごとの Markdown → VideoAnalysis（反応型・引用 cid・言語比・返信の読み） | standard／**deep**（単体型: 時系列と流入元まで） | `analyze_comments_clean.md` |
| C5 | `synthesize` | VideoAnalysis[] → 分類値ごと／アカウントごと／対象ごと の統合 | 形 | 同（community_synthesis.md） |
| C6 | `write_report` | B 層の表＋C3＋C5＋人の考察（＋知識パック）→ REPORT | **形 × 媒体 × 問い**。知識パックがあれば章立て・文体を継承、無ければ汎用（観測／推測／言えないこと＋付録） | `write_report_clean.md`（TikTok 集合型・著者の型に固定） |
| C7 | `finish` | REPORT → 媒体向け原稿 | 出力先（note／他） | `finish_note.md` |
| D1 | `distill` | 完了した分析の成果物 or 外部文書 → 知識パックのカード | 入力の種類 | `distill.md` / `distill_merge.md`（記事専用 → 一般化） |
| D2 | `select_precedents` | カード＋対象の特徴 → 近い事例 N 本 | **まず規則で**（形・媒体・分類値・日付）。LLM は任意 | C6 の中で LLM が選んでいる |

### E層: 評価 [E]

| # | 関数 | 内容 | 適用 |
|---|---|---|---|
| E1 | `rubric` / `compare` [L] | 8観点の採点、**人が書いた参照分析**との要素比較（著者の記事に限らない。事後に人が書いた短い所見でもよい） | 参照があるときだけ |
| E2 | `verify` [D] | 引用 cid の実在・いいね数一致、seq の実在、本数・再生の再計算、事前知識申告の有無 | **全ワークフロー必須** |
| E3 | `provenance` [D] | 各 LLM タスクが見た入力（ファイル・レコード・カード）とプロンプトのハッシュ、モデル名を記録 | 全部 |

### 人の工程 [H] — LLM タスクと同じ契約で扱う

| # | 工程 | 入力 → 出力 |
|---|---|---|
| H1 | 軸の確認・修正 | Taxonomy（提案）→ Taxonomy（確定、版を上げる） |
| H2 | 代表の確認 | Selection（案）＋予想所要 → Selection（確定） |
| H3 | 人の考察の投入 | 空欄の章 → 自由記述 |
| H4 | ログインの画面操作 | 認証コード等（引き継ぎ書 ToDo-3） |

「提案を置く → 人が直す → 検証して取り込む」は C 層の「入力束 → 生成 → 検証・取り込み」と同じ形なので、**runner から見れば LLM タスクも人のタスクも
「外部の生産者を待つステップ」**として一種類に扱える。E2E はまさにこの形（ファイルを置いてサブエージェントに渡した）で回した。

### ワークフロー × 小機能

● そのまま ／ ◐ 変種（プロンプトかパラメータ）／ ○ 新規実装 ／ — 不要

| | A1 | A2 | A3 | A4 | A5 | A6 | B1 | B2 | B3 | B4 | B5 | B6 | B7 | B8 | B10 | B11 | C1 | C2 | C3 | C4 | C5 | C6 | C7 | E1 | E2 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| W1 楽曲 TikTok | ● | ● | ● | ● | — | — | ● | ● | ● | ● | ● | — | — | — | ○ | — | ● | ● | ● | ● | ● | ● | ● | ● | ● |
| W2 動画 TikTok | — | ◐部分 | ● | ◐厚く | ○ | — | — | — | — | — | ● | ○ | ○ | — | — | ○ | ◐コメント軸 | ◐ | — | ◐deep | — | ◐単体 | ● | — | ● |
| W3 アカウント TikTok | — | — | ● | ● | ○ | — | ◐ | ◐ | ● | ◐ | ● | ○ | ○ | — | ○ | ○ | ◐ | ◐ | ◐転機 | ● | ◐ | ◐系列 | ● | — | ● |
| W4 タグ TikTok | — | ● | ● | ● | — | ●同じ | ● | ● | ● | ● | ● | — | — | — | ○ | — | ● | ● | ● | ● | ● | ● | ● | — | ● |
| W5 楽曲 YouTube | ○API | ○ | ○ | ○ | — | ○ | ● | ● | ● | ● | ● | — | — | — | — | — | ◐yt | ◐ | ● | ● | ● | ◐yt | ● | — | ● |
| W6 動画 YouTube | — | — | ○ | ○ | ○ | ○ | — | — | — | — | ● | ○ | ○ | — | — | ○ | ◐ | ◐ | — | ◐deep | — | ◐単体 | ● | — | ● |
| W7 チャンネル YouTube | — | — | ○ | ○ | ○ | — | ◐ | ◐ | ● | ◐ | ● | ○ | ○ | — | — | ○ | ◐ | ◐ | ◐転機 | ● | ◐ | ◐系列 | ● | — | ● |
| W8 伸び悩み | ● | ● | ● | ● | — | — | ◐停滞 | ● | ● | ● | ● | — | — | — | ○ | — | ● | ● | ◐問い | ● | ● | ◐問い | ● | — | ● |
| W9 定点 | — | ● | ● | — | — | — | ● | — | — | — | — | — | — | ○ | — | — | — | ◐既存軸 | — | — | — | ◐差分 | — | — | ● |
| W11 束ね | ● | ● | ● | ◐少 | — | — | ● | ◐小 | ● | ◐小 | ● | — | — | — | ○ | — | ◐ | ◐ | ◐ | ● | ◐ | ◐要約 | ● | — | ● |

新規実装が集中するのは **A5・B6・B7・B11（単体型・系列型の共通部）** と **YouTube アダプタ**。C 層の新規は無く、全部変種（形 × 媒体 × 問い）。
**C1 は「投稿の軸」だけでなく「コメントの反応型の軸」も提案する形に一般化する**（今は反応型が指示書に固定語彙で埋め込まれており、これは著者の語彙に依存している）。

---

## 3. 5論点の方針

### 3.1 層の切り方 — 4層で切る。ただし「共通スキーマ＋保存」と「実行」は層ではなく横串

```
ugc/                      新パッケージ。既存 scraper.py / main.py / store.py は当面そのまま動かし、段階的に中身を移す（第5章）
  schema.py               共通スキーマ（Post / Creator / Comment / Source / Run / Taxonomy / Label / Selection / Analysis / Artifact）と DDL
  store.py                既存 store.py を移動・拡張（runs / videos / comments を維持し、表を足す）。export_zip は Excel 向け納品として残す
  sources/                A層 [P]
    base.py               Source インターフェース（A1〜A6）＋ Session
    tiktok_web.py         未ログイン・ヘッドレス＝ scraper.py の収集部 ＋ enrich.py の extract()
    tiktok_session.py     ログイン・実画面＝ spatest.py（Windows 機。Session を持つ）
    youtube_api.py        Data API v3
  transform/              B層 [D]  timeseries / sample / sheets / cells / comments_md / comment_timeline / outliers / diff / taboo / substitute / context_window
  llm/                    C層 [L]
    task.py               LLMTask = prompt ＋ build_input ＋ output_schema ＋ validate ＋ ingest
    providers/            anthropic_api / openai_api / files（MCP・Claude Code 用）/ replay（記録済み出力。回帰試験用）
    tasks/                propose_axes / label / draft_pathway / analyze_comments / synthesize / write_report / finish / distill / select_precedents / rubric
    prompts/<task>/       base.md ＋ 変種（<形>_<媒体>[_<問い>].md）＋ eval_mode.md（試験条件だけ）
  knowledge/              知識パック（媒体×形ごと、任意）。tiktok_collective/ に現行の distilled/ を移す。完了した分析からカードを追記
  eval/                   E層  verify（E2）/ provenance（E3）/ rubric・compare（E1）
  workflows/              song_tiktok / video_tiktok / account_tiktok / tag_tiktok / song_youtube / video_youtube / channel_youtube / snapshot / digest（Step の並び）
  runner.py               依存解決・再開・予算・人の確認点で停止・進捗（既存 jobs の phase 表示を流用）
```

- 4層に分ける理由は 1.4 の表のとおり: **媒体で変わるものは A に閉じ、形で変わるものは B の関数選択と C のプロンプト変種に閉じ、E は共通**。
- **横串1: schema/store。** 「B 層は依存なし」が成り立つのは全アダプタが同じ Post/Comment を返すときだけ。スキーマは層ではなく前提。
- **横串2: runner。** セッション束縛（A2→A4）、12時間予算、IP 直列の待ち行列、人の確認点での停止、到達不能の代替ループは、
  どの層にも属さない実行時の関心。ここを層に押し込むと A 層が肥大する（今の `scraper.py` がそう: 収集と進捗管理と検索が1ファイル）。
- **知識パックは層ではなく「任意の入力」。** 無くても C 層は動く。これが「著者の記事に依存しない」を構造で保証する箇所。
- 棄却した案: 「取得も含めて全部プラグイン」（過剰。媒体は当面2つ）／「層を作らず analysis/ にスクリプトを足し続ける」
  （形が変わった瞬間に prep_sample 系が全部コピーになる）。

### 3.2 共通スキーマ — 「実体の階層」と「分析の単位」を分け、実体は1か所に蓄積する。`store.py` は残して拡張する

> **2026-09-30**: データの持ち方は `docs/DB_DESIGN.md` で段階的に合意中。**分析と分析は独立（同じ動画でも分析ごとに取得、共有しない）と決まったので、本節の「実体は DB 1本に蓄積・分析は参照」は覆った。** `docs/DB_TABLES_DRAFT.md` も同じ理由で未合意。

**2つの言い分け（ユーザー回答 Q2 から）**

- **実体の階層**: 集合（音源・タグ・検索 → 投稿の集まり）／投稿（動画1本）／アカウント／コメント。**階層ごとに別の表**、粒度を混ぜない（`store.py` の原則）。
  **同じ投稿が複数の集合に属し、同じ投稿がアカウントの系列にも出てくる**ので、実体は分析の単位に関係なく **DB 1本に蓄積**し、取得ごとの履歴（run）を持つ
- **分析の単位（analysis）**: 「問い × 対象 × ワークフロー」1回分（例: シルエットの W1 を1回）。軸・ラベル・段階・代表・LLM 出力・レポートは
  この単位に属し、実体を ID で参照する。**分析ごとに DB を分けない**（実体が重複するため。初稿の「プロジェクトごとに1 DB」は取り下げ）
- 既存コードの `project_name`（CSV の13列目、`scraper.py` のジョブ名）は「集合の名前」に対応する

**実体の表（媒体共通の核 ＋ `extra` JSON で媒体固有を吸収）**

| 表 | 主キー | 核（全媒体共通） | `extra`（媒体固有、JSON） | 備考 |
|---|---|---|---|---|
| `collections` | (platform, collection_id) | kind(music/tag/query/creator_posts), key, url, title | — | 母集団の定義。W10 はこれを並べる |
| `collection_posts` | (run_id, collection_id, post_id) | grid_seq, seen_at | — | 集合と投稿の多対多。グリッドの並び（推薦順）も残す |
| `runs` | run_id | collection_id or post_id[], kind(list/enrich/comments/snapshot), started/finished, counts, status, **session_id** | stats_json | **既存 store.py の `runs` を継承**。W9 の差分単位 |
| `posts` | (platform, post_id, run_id) | url, created_at(UTC), creator_id, text, media_type(video/photo), duration, plays/likes/comments_n/shares/saves/reposts, language, region, is_ad, music_id, cover_path, hashtags[], mentions[], platform_labels[], suggested_words[], sticker_texts[], fetched_at, fetch_status | TikTok: diversificationLabels, channelTags, effect_stickers, duet/stitch… ／ YouTube: categoryId, tags, definition, liveBroadcast… | **既存 `videos` を継承**（13列は残す。`enrich.py` の約40項目を足す）。run_id で履歴 |
| `creators` | (platform, creator_id, fetched_at) | handle, name, bio, verified, follower_count, following, post_count, heart_count, account_created | tt_seller, private… ／ YouTube: subscriber(概数), country | **取得時刻つきスナップショット**。「投稿時点フォロワー数」は取れないが「取得時点」は必ず残す |
| `comments` | (run_id, comment_id) | 既存22列 ＋ platform | share_info 等は捨てる（現行どおり） | **既存 store.py の `comments` をそのまま**。YouTube の topLevel/replies は level 1/2 に写す |

**分析の表（analysis_id に属し、実体を参照する）**

| 表 | 主キー | 内容 | 備考 |
|---|---|---|---|
| `analyses` | analysis_id | workflow, shape, platform, question, target(collection_id / post_id / creator_id), created_at, status | 分析の単位そのもの |
| `taxonomies` | (analysis_id, version) | unit(post/comment), axes json, source(llm/human), parent_version, prompt_hash | H1 で版が上がる。他の分析の版を「雛形」として読める |
| `labels` | (analysis_id, taxonomy_version, unit_id) | 軸ごとの値, conf(H/M/L), reason, provider, model | C2 の出力。unit_id は post_id か comment_id |
| `phases` | (analysis_id, version, phase_id) | name, start, end, rationale, kind(段階/転機/停滞) | B1 の候補 → C3 の命名 → H が確認 |
| `selections` | (analysis_id, version, post_id) | cell, rule(必須/配分/人が追加/外れ値), status(planned/fetched/unreachable/substituted), substituted_by, est_seconds, reply_policy | B4/B7 → H2 → A4 → B10 |
| `outputs` | (analysis_id, task, unit_key) | json（VideoAnalysis 等）, provider, model, prompt_hash, created_at | C4/C5 の構造化出力。unit_key は post_id／分類値／creator_id |
| `artifacts` | (analysis_id, kind, version) | path, sha256, stage, inputs(provenance) | REPORT/NOTE/EVAL 等のファイルは DB に入れず参照だけ |

**TikTok → 核、YouTube → 核 の対応（抜粋）**

| 核 | TikTok（itemStruct） | YouTube（videos.list） |
|---|---|---|
| post_id | `id`（19桁、**文字列**） | `id`（11文字） |
| created_at | `createTime`（UNIX 秒 → UTC） | `snippet.publishedAt`（RFC3339） |
| plays / likes / comments_n | `statsV2.playCount / diggCount / commentCount` | `statistics.viewCount / likeCount / commentCount` |
| saves / reposts / shares | `collectCount / repostCount / shareCount` | **無し（null）** |
| region | `locationCreated` | **無し**（チャンネルの `country` を creators 側に） |
| language | `textLanguage` | `snippet.defaultAudioLanguage` or 判定 |
| platform_labels | `diversificationLabels` | `snippet.categoryId`＋`topicDetails` |
| suggested_words | `suggestedWords` / `videoSuggestWordsList` | **無し** |
| cover_path | `video.cover`（24時間で失効 → 保存） | `snippet.thumbnails.maxres`（失効しない。URL のままでよい） |
| comment.language | `comment_language` | **無し**（判定するか null） |
| comment.is_author | `label_list type=1` | `authorChannelId == video.channelId` |

「無い列は null」で通す。B 層の関数は null を「不明」として扱う（現行の `unknown` と同じ扱い）。

**`store.py` との関係（第8章で実装済みのものを壊さない）**

| 既存の決定 | 扱い |
|---|---|
| 粒度を混ぜない（動画1行／コメント1行） | 維持。表を足すだけ |
| 19桁 ID は文字列、CSV 書き出し時のみ `=+-@` をエスケープ | 維持（YouTube の ID も TEXT） |
| 既存13列を順序ごと先頭に（Excel 貼り付け運用） | **`export_zip` の責務として維持**。DB の `posts` は列を足してよい（書き出し時に13列を先頭に並べるのは今もそう） |
| `runs / videos / comments` の3表と `ingest()` の検算（aweme_id 混入で拒否、孤児0） | 維持。`ingest()` に `enrich.jsonl` の取り込み口を足す |
| DB は `output/ugc.db` に全部蓄積（run_id で区別） | **維持**（Q2）。実体は1本に蓄積し、分析は `analysis_id` で区別する |
| `tests/test_store.py` 14件 | そのまま通す（回帰の第一段） |

現行の `analysis/*.py` は CSV/JSONL のファイルを直接読み書きしている（`videos.jsonl` / `enriched.jsonl` / `labels.tsv` / `reps.tsv`）。
これらは**そのまま「store からの書き出し形式」として残し**、B 層は store を読む形に移す。E2E の成果物（`output/trial_silhouette/`）を
store に取り込めれば、以後の回帰試験の固定データになる（第5章）。分析の成果物ファイルは `output/analyses/<analysis_id>/` に置く。

### 3.3 ワークフロー定義 — Python で「関数を並べるだけ」。書くのは基本 AI。UI での編集は対象外

決定（Q3）: **Python の宣言的リスト**（YAML は採らない。UI 制御は「リッチすぎる」ので対象外）。
要件は「思いついたワークフローを、関数を並べるだけで追加・削除・編集できる」こと。**どこに AI を入れるか**も並べ方で決める:
同じ Step を `producer="code" | "llm" | "human"` で切り替えられる（例: 段階の命名を、規則だけで／LLM で／人が付ける）。関数側は生産者を知らない。

```python
# ugc/workflows/song_tiktok.py（骨格のイメージ。実装ではない）
WORKFLOW = Workflow("song_tiktok", shape="collective", platform="tiktok", steps=[
    Step("list",      A.list_posts,        needs=[],                 resource="tiktok_web"),
    Step("enrich",    A.get_post,          needs=["list"],           resource="tiktok_web",  batch=True),
    Step("prep",      B.timeseries_sample, needs=["enrich"]),
    Step("axes",      C.propose_axes,      needs=["prep"],           producer="llm"),
    Step("axes_ok",   H.confirm,           needs=["axes"],           producer="human"),         # ここで止まる
    Step("labels",    C.label,             needs=["axes_ok"],        producer="llm", batch=True),
    Step("phases",    C.draft_pathway,     needs=["labels"],         producer="llm"),
    Step("select",    B.allocate_cells,    needs=["phases"],         budget="time"),
    Step("select_ok", H.confirm,           needs=["select"],         producer="human"),
    Step("comments",  A.get_comments,      needs=["select_ok"],      resource="tiktok_session", budget="time",
                                            retry_with=B.substitute),                               # 到達不能→代替→再試行
    Step("analyze",   C.analyze_comments,  needs=["comments"],       producer="llm", batch=True),
    Step("synth",     C.synthesize,        needs=["analyze"],        producer="llm"),
    Step("report",    C.write_report,      needs=["synth"],          producer="llm"),
    Step("finish",    C.finish,            needs=["report"],         producer="llm"),
    Step("verify",    E.verify,            needs=["report"]),
])
# video_tiktok は list/enrich → context_window → comments(厚く) → axes(コメント) → comment_timeline → analyze(deep) → report(単体)
# account_tiktok は list_by_creator → enrich → outliers → 外れ値ごとに video_tiktok の後半 → synth(系列) → report(系列)
```

YAML でなく Python にする理由:

1. **一方向 DAG では書けない箇所がある**: セッション束縛（A2→A4 を同じ Session で）、到達不能の代替ループ（A4→B10→A4）、
   12時間予算の途中打ち切りと `--resume`、系列型の「外れ値ごとに単体型を回す」入れ子。YAML でこれを表すと独自の制御語彙が増え、結局インタプリタを書くことになる
2. 人の確認点で止まって再開する**状態機械**であり、DAG の実行ではない
3. 開発者は1人＋AI。YAML のスキーマとローダを保守するコストに見合う利用者（設定だけで新ワークフローを組む人）が居ない
4. ただし **`Step` はデータ**（関数参照・依存・資源・予算・生産者の種別）にしておく。runner が `needs` で順序を解き、
   WEBUI が並びを表示し、`--from` / `--only` / `--dry-run` が効く。YAML が欲しくなったら `Step` のリストを YAML から作るローダを足せばよい（逆は無理）
5. `scraper.py` の `PHASES` / `set_phase` / `jobs` は既にこの形の原型。進捗・待ち時間表示はそのまま流用できる
6. **AI が書く前提**なので、`Step` の引数は少なく・名前で意味が分かるものだけにし、`workflows/` に W1 の1本を手本として置く。
   新しいワークフローは「手本をコピーして Step を足す・消す・producer を変える」だけで書ける状態を保つ

冪等性と再開: **各 Step の出力は store に (analysis_id, step, version) で保存し、出力があれば飛ばす**（`--force` で作り直し）。
`enrich.py` の「取得済み video_id は飛ばす」、`spatest.py --resume` と同じ規則を全 Step に揃える。

### 3.4 LLM 呼び出しの共通化 — 「LLM 工程はファイル／レコードの契約であって関数呼び出しではない」

E2E で実際にやったこと＝「入力ファイルを揃える → 指示書を渡す → 別プロセスが出力ファイルを書く → 検算する」。
これをそのまま型にする。

```
LLMTask
  name            "analyze_comments"
  prompt          prompts/analyze_comments/base.md ＋ 変種（<形>_<媒体>[_<問い>].md）＋（評価時のみ）eval_mode.md
  build_input     store → 入力束（Markdown/JSON/画像のファイル群 ＋ レコード）。知識パックがあれば足す。何を入れたかを provenance に記録
  output_schema   構造化出力（JSON Schema）。Markdown 出力のタスク（REPORT）は「必須節」の検査
  validate        cid の実在・数値一致・seq の実在・事前知識申告の有無（E2 を呼ぶ）
  ingest          analyses / taxonomies / labels / artifacts へ取り込み。prompt_hash・model・provider を残す
```

**生産者（provider）は差し替え可能。3経路はここで吸収する**（ANALYSIS_PIPELINE 6b）:

| provider | 仕組み | 使う工程 |
|---|---|---|
| `anthropic_api` / `openai_api` | SDK。Batch（C2・C4）、構造化出力、base.md＋知識パックを固定プレフィックスにしてキャッシュ | 無人実行 |
| `files` | 入力束と `TASK.md`（レンダ済み指示書）を `output/analyses/<analysis_id>/tasks/<task>/` に置き、`OUTPUT/` が書かれるのを待つ | **MCP コネクタ**（`get_task_input` / `submit_task_output` の2ツール）と **Claude Code**（同じディレクトリを読み書きするスキル）は同じ provider |
| `replay` | 過去の `analyses` / `artifacts` から出力を返す | 回帰試験（第5章）。LLM を1回も呼ばずに全工程が通る |

プロンプトの分割（現行 `analysis/prompts/*.md` からの移行）:

- 現行の指示書は **試験条件（Web 禁止・`_contaminated/` を読むな・事前知識申告）と楽曲名と絶対パスと著者の語彙（反応型の固定リスト、著者の型・文体）が本文に埋め込まれている**。
  `base.md`（タスクの定義。安定・キャッシュ対象）／変種（形・媒体・問い）／`eval_mode.md`（試験条件）／ヘッダ（対象・パス。レンダ時に埋める）／知識パック（任意）に分ける
- **知識パックが無いときの既定の章立て**を base.md 側に持つ（E2E のレポートが自然に取った「観測／推測／言えないこと」「付録: 使ったデータ・取れなかったもの」がそれ）
- **数値は LLM に計算させない**（第6章の原則）: B 層の表を入力束に入れ、REPORT はそれを引用する。E2 が引用を検算する
- 構造化出力にするもの: C1（Taxonomy）・C2（Label）・C4（VideoAnalysis）・E1（採点）。Markdown のまま: C3・C5・C6・C7

### 3.5 「答えを見ない評価」の再現 — E2 は全ワークフロー必須、E1 は人の参照分析があるときだけ。汚染防止は機械で

| 仕組み | 内容 |
|---|---|
| **禁則語（B9）** | 分析の対象名（曲・アーティスト・アカウント・動画題名）から機械生成 → 知識パックのカードから `taboo_hits` を除外（現行の `INDEX.json` を自動化） |
| **出所の記録（E3）** | 各 LLM タスクが見た入力（ファイル・レコード ID・カード）とプロンプトのハッシュを `analyses` / `artifacts` に残す。「このレポートは X を見ていない」を後から証明できる |
| **事前知識の申告** | 全 LLM タスクの出力スキーマに `prior_knowledge` を必須項目として持つ（今は別ファイル）。E2 が空でないことを検査 |
| **E2（検算）** | cid・seq の実在、いいね数・再生数・本数の一致、存在しない動画の言及ゼロ。**参照分析が無い W2/W3/W6/W7/W9 ではこれが唯一の評価** |
| **E1（採点・比較）** | 人の参照分析があるときだけ。採点者タスクだけが参照を読む（今の作法どおり）。参照は著者の記事でなくてよい: **人が事後に書いた短い所見**（EDITOR_NOTES の逆向き）でも掛けられる |
| **回帰試験** | シルエットの E2E 成果物（880本の属性・200本のラベル・27本のコメント・分析・REPORT）を store に取り込み、`replay` provider で全工程を流し、B 層の出力（weekly / reps / comments_md）が現行ファイルと一致することを確かめる。**リファクタの安全網はこれ** |

---

## 4. 設計に効く制約（再掲。忘れると設計が崩れる）

| 制約 | 出所 | 設計への反映 |
|---|---|---|
| TikTok の失敗は無言（例外なし・HTTP 200） | 引き継ぎ書 第0章 | A 層の戻り値に**必ず**自己申告（`aweme_id` 照合・`total/has_more`・描画状態・ペース）を含め、runner が読む。「取れた件数」だけを返す関数を書かない |
| 一覧取得とコメント取得は同一セッション | 7-D | `Session` 資源と B10 代替ループ（3.3） |
| 直接 URL 読み込みは 3〜4本でブロック | 2-5b | A3 の未ログイン経路（headless、3.2秒/本）と A4 のログイン経路を混ぜない |
| 偽装・署名再実装・閾値探索・プロキシはやらない | 第4章 | A 層の TikTok アダプタは「ページに操作させて横取り」のみ。YouTube は公式 API のみ |
| リストを寝かせると 9か月で 20% 消える | 7-D | A2 と A3 を同じ run で完結。`creators` は取得時刻つき |
| 1本 ≒ API回数 ÷ 1.8回/分 ＋ 15秒、1ジョブ 12時間 | 7-C・第8章 | 予算は runner が持ち、B4 が本数に換算。W11 は予算プロファイルを小さくするだけ |
| コメントは標準40件、返信は分類値で切り替え | ABLATION | `reply_policy` は Selection の行ごとに持つ（分類値から決める規則は B4） |
| 起点動画は必ず取る／質問形は返信1件でも取る | 第8章 弱点1・2 | B4 の必須枠、`spatest.py --reply-min` の質問形対応（未着手のまま。A4 の改修項目） |
| コメント分析と執筆は別プロセス（50万トークン超を避ける） | 第8章 | C4/C5 と C6 は別タスク。runner が別実行にする |
| auto モード不可・無人実行が最優先 | 7-C・メモリ | 実装フェーズも default モード。人の確認点以外で止まらない runner |
| YouTube Data API 10,000 unit/日、search は 100 unit | 公開仕様 | W5 の母集団は検索よりチャンネル・人手リストを優先。割当は runner の予算の一種 |
| 写真投稿は属性が取れない、削除済みは属性なし | ToDo-1・7-D | `media_type=photo` と `fetch_status` を posts に持ち、B 層は除外でなく「不明」として扱う |
| 再生数の履歴・投稿時点フォロワー数は取れない（両媒体） | 1.2 | 単体型の時系列はコメント時刻だけと明記。`creators` は取得時点のスナップショット |

---

## 5. 実装の順番（既存を壊さない strangler 方式）

**今回の成果物は 1〜5（W1 を新しい下回りに載せる）。6 以降は「あとから低コストで足せる」ことの確認項目で、今回は着手しない。**

| 順 | 作業 | 完了の判定 |
|---|---|---|
| 1 | `ugc/schema.py` ＋ `store.py` 拡張（実体の階層の表・分析の表を足す・`enrich.jsonl` の取り込み口） | `tests/test_store.py` 14件が通る。シルエット（`enriched.jsonl` 880本、`e2e.jsonl` 27本、`labels.tsv`、`reps.tsv`）が取り込める |
| 2 | `transform/` に B1〜B5 を移植（`prep_sample` / `build_llm_input` / `select_reps` / `prep_comments`） | store から生成した `weekly.tsv` / `sample_*` / `reps.tsv` / `comments/*.md` が現行ファイルと一致 |
| 3 | `llm/task.py` ＋ `files` / `replay` provider。プロンプトを base/変種/eval_mode/知識パック（任意）に分割 | eval_mode＋TikTok 集合型パックで描画したシルエット用の指示書が現行 `*_clean.md` と実質同文。`replay` で C1〜C7 が通る。**パック無しでも描画できる** |
| 4 | `runner.py` ＋ `workflows/song_tiktok.py`（手本の1本） | W1 を `replay` で端から端まで流し、REPORT の入力束が E2E と一致。人の確認点で止まって再開できる。`producer` の切り替えが効く |
| 5 | `sources/` に既存3経路を包む（`scraper.py` / `enrich.py` / `spatest.py` は中身を変えない） | 既存 Web サービス（`main.py`）と `spatest.py` の挙動不変。A4 の戻り値に自己申告が揃う |
| 6 | （確認）B6 / B7 / B11 / A5 ＋ `workflows/video_tiktok.py` `account_tiktok.py` | W2 / W3 が新規関数3〜4個と Step の並び替えで書ける |
| 7 | （確認）`youtube_api.py` ＋ `video_youtube.py` `channel_youtube.py` | 媒体アダプタ1枚で W6 / W7 が書ける |
| 8 | `anthropic_api` provider（Batch・構造化出力・キャッシュ）、MCP サーバー、Claude Code スキル | 3経路で同じタスクが同じ出力スキーマを返す。ANALYSIS_PIPELINE「実装の状態と順番」の 2a–2c と同じ項目 |
| 9 | （確認）M1: 完了した分析からカードを生成して知識パックに追記 | W1 の成果物からカードが出て、次の W1 の `select_precedents` が引ける |

1〜4 は **API キー・Windows 機・ログイン不要**（シルエットの既存データだけで完結）。5 で実機に触る。

---

## 6. 決定の記録（2026-09-14 ユーザー回答）

| Q | 私が聞いたこと | ユーザーの答え | 設計への反映 |
|---|---|---|---|
| **Q1** | 初回の設計対象をどれにするか | **質問がずれていた。** 今見えているのは TikTok の特定楽曲バズ分析（W1）だけ。列挙したユースケースは「下回りが破綻しないための入力」であって選択肢ではない。要件は、あとから思いついたワークフローを低コストで追加・削除・編集（どこに AI を入れるかを含む）できること | 第1章を「下回りを試すための入力」に、第5章を「1〜5 が成果物、6 以降は確認項目」に書き換え。`producer` を Step 単位で切り替え可能に（3.3） |
| **Q2** | 保存の単位（分析1回ごとに DB を分けるか、1本にまとめるか） | 「プロジェクト」が何を指すか不明。データは**多層**（動画・集合・アカウント）で、階層は分けなければならない | 「実体の階層」と「分析の単位」を言い分け（3.2）。**実体は階層ごとの表で DB 1本に蓄積**（同じ投稿が複数の集合・アカウントの系列に出るため）、分析は `analysis_id` で参照。「プロジェクトごとに DB」は取り下げ。`project_name` は集合の名前に対応 |
| **Q3** | ワークフロー定義の書き方 | 関数を並べるだけで簡単に書けるなら何でもよい。基本 AI が書く。UI 制御は対象外 | Python の宣言的 Step リスト。W1 を手本として1本置く（3.3） |
| **Q4** | YouTube の集合型（W5）の母集団 | 一旦空欄。YouTube は「使用楽曲」で切れないなら一旦なし | **W5 は無し**。Data API に使用楽曲の切り口は無い（Web の Shorts 音源ページは API 外・未検証）。YouTube は単体・系列（W6/W7）だけが将来候補 |
| **Q5** | 新しい形（動画・アカウント）の分析の枠組み（軸・段階・語彙）をどう用意するか | 質問の意図が不明 | 取り下げ。W1 しか見えていない今は決める必要がない。下回りとしては「LLM ステップに任意の知識入力（知識パック）を差せる」だけ確保する（3.4） |

### 私が決めて進めたこと（異論があれば戻す）

- 新パッケージ `ugc/` を既存と並置し、`scraper.py` / `main.py` / `store.py` / `spatest.py` の中身は当面変えない（strangler）
- 対象を「集合・単体・系列・横断」の4つの形に畳み、媒体は A 層だけに効かせる
- 知識パックを層ではなく任意入力にし、C 層はパック無しで動くことを要件にする。著者の記事の蒸留は TikTok 集合型パックの初期値として `knowledge/tiktok_collective/` に置く
- LLM タスクと人のタスクを runner 上で同じ「外部生産者」として扱う
- C1（軸の提案）を投稿だけでなくコメントの反応型にも適用し、指示書に固定されている著者の語彙を提案制に変える
- `select_precedents`（近い事例の選定）は LLM でなく規則で行う（形・媒体・分類値・日付）。再現性と汚染防止のため
- E2（検算）を全ワークフロー必須にし、E1 を任意にする
- シルエットの E2E 成果物を回帰試験の固定データにする

---

## 7. 第9章の叩き台からの変更点

| 叩き台 | 本書 |
|---|---|
| ワークフロー4本 | 12本＋運用2本。対象を「集合・単体・系列・横断」の形に畳み、媒体と直交させた |
| 「4 YouTube: A1〜A5 を差し替え、B〜E はそのまま」 | 単体・系列は API で完結、集合は母集団の取得が弱い（Q4）。B は B6/B7/B11 が新規、C は変種 |
| 小機能 A1〜E2 | A5/A6 の実装状況、B6〜B11 を追加、H1〜H4（人）を LLM と同じ契約に、知識パックを任意入力に |
| 「2 特定動画: A3 → A4 → C4 → C5」 | 時系列はコメント時刻のみ、と明記。B6（コメント時系列）・B11（文脈）・A5（本人の直近）が本体 |
| 論点5つ | 各論点に推奨・理由・棄却案（第3章）。決定待ちは Q1〜Q5 |

**初稿（同日）からの訂正**: 初稿は「著者の成果物の型（記事の kind × platform）」を洗い出しの根拠にしていた。これは誤りで、
本書は対象 × 問い × 媒体 × 頻度から洗い出し直し、著者の記事は「TikTok 集合型にたまたま使えた既存資産」に位置づけを変えた。
それに伴い「YouTube は単体・系列の派生」「月報」「著者の型4種」といった記事由来の結論を取り下げ、形（1.2）から引き直した。
