# （退避・未合意）データベースの表の案（2026-09-30）

> **この文書は未合意の下書き。** 何を保存するか・どんな制約があるかを先に合わせるため、`docs/DB_DESIGN.md`（第1歩）を先に進めている。
> ここは第3歩（表の形）の材料として残してあるだけ。

# データベースの構想（レビュー用、2026-09-30）

このファイルの目的: ワークフローを追加しやすい下回りを作るにあたり、**データをどう持つか**を実装前にレビューしてもらうこと。
上位の設計は `docs/WORKFLOW_DESIGN.md`（第3.2節がこの文書の要約）。実装はまだしていない。

**見てほしいところは第7章の R1〜R6**。そこだけ読んでも判断できるように書いた。

---

## 1. 一言でいうと

データを「**もの**」「**いつ見たか**」「**どう解釈したか**」の3つの箱に分けて、SQLite のファイル1本（`output/ugc.db`）に入れる。

```
┌───────────────────────── ① もの（実体）──────────────────────────┐
│  集合 ──(多対多)── 投稿 ──(1対多)── コメント（返信は親を指す）     │
│                     │                                            │
│                  アカウント                                       │
└──────────────────────────────────────────────────────────────────┘
          ▲ 数値は「いつ見たか」ごとに記録
┌───────────────────────── ② いつ見たか（取得）─────────────────────┐
│  取得1回 = run。再生数・いいね・フォロワー数などの「変わる値」は     │
│  run ごとのスナップショットとして残す                               │
└──────────────────────────────────────────────────────────────────┘
          ▲ ID で参照するだけ（コピーしない）
┌───────────────────────── ③ どう解釈したか（分析）──────────────────┐
│  分析1回 = analysis（例: 「シルエットの楽曲バズ分析」）             │
│  軸 → ラベル → 段階 → 代表動画 → AI の出力 → レポート              │
└──────────────────────────────────────────────────────────────────┘
```

- **①は1つのものにつき1行。** 同じ動画は、どの分析から見ても同じ1行
- **②は取得のたびに増える。** 同じ動画を2回取れば、スナップショットが2行になる。動画の行は1行のまま
- **③は分析ごとに増える。** 同じ動画に、分析Aでは「学生」、分析Bでは「外れ値」というラベルが付いてよい。動画の行には書き込まない

---

## 2. なぜこの分け方か（今の困りごと）

### 2-1. 同じものが何度も保存される

いまの `store.py` は「取得1回ごとに、動画もコメントも丸ごと1行ずつ」保存する。手元の `output/ugc.db` の実物:

| 取得 | 動画の行 | コメントの行 |
|---|---|---|
| プロポーズ_pace100 | 940 | 11,135 |
| プロポーズ_replyfix2 | 925 | 189 |
| **合計** | **1,865**（実際の動画は約940本） | |

動画の本文・投稿日・投稿者は2回取っても変わらないのに、2行ある。ワークフローが増えると、この重複がもっと起きる。
例えば「シルエットの楽曲分析」と「@magichour0120 のアカウント分析」を両方やると、@magichour0120 の動画5本は両方の母集団に入る。
分析ごとに保存すると、この5本が別々に2か所へ保存される。しかも取得した日が違うので再生数が食い違う。

### 2-2. 分析の結果がファイルに散らばっている

シルエットの E2E では、分析の結果が形式の違うファイルに分かれていた。
軸は `taxonomy.json`、ラベルは `labels.tsv`、代表は `reps.tsv`、AI のコメント分析は `video_analysis.jsonl`。
どの版の軸で付けたラベルか、どの取得のデータを見たかは、ファイル名と記憶でしか辿れない。
ワークフローを並べ替えて使い回すなら、各 Step の入出力は決まった場所に、決まった形で置く必要がある。

### 2-3. 変わる値と変わらない値が混ざっている

再生数・いいね・フォロワー数・提案語は、取るたびに変わる。投稿日・本文・尺・投稿者 ID は、基本的に変わらない。
いまは両方が同じ行にあるので、「9月11日時点の再生数」と「動画の本文」の区別が付かない。
「投稿時点のフォロワー数は取れない」という制約（E2E で判明）がある以上、**取得した日時と値を必ず組にして残す**必要がある。

---

## 3. ①もの（実体）の表

媒体（TikTok / YouTube）で共通する列を正式な列にし、**媒体固有の列は `extra`（JSON）に入れる**。
YouTube に無い値（投稿地域・提案語など）は空（NULL）。

### collections — 集合（投稿の束ね方）

| 列 | 例 | メモ |
|---|---|---|
| collection_id | `tiktok:music:7089…` | 主キー。媒体:種類:キー |
| platform | tiktok | |
| kind | music / tag / search / creator_uploads | アカウントの投稿一覧も「集合」の一種として扱う |
| key | 音源 ID、タグ名、検索語、アカウント ID | |
| url, title | 音源ページの URL、「シルエット - KANA-BOON」 | |

### collection_members — 集合と投稿のつながり

| 列 | 例 | メモ |
|---|---|---|
| collection_id, post_id, run_id | | この3つで1行。**どの取得で、どの集合に、どの投稿が見えたか** |
| rank | 57 | グリッドに出た順番。既存 CSV の `Index` 列はこれ。**推薦順なので取得ごとに変わる**（引き継ぎ書 7-D） |

1つの動画が複数の集合に入ってよい（多対多）。シルエットの集合にも、@magichour0120 の投稿一覧にも入る。

### posts — 投稿（動画1本 = 1行）

| 列 | 例 | メモ |
|---|---|---|
| platform, post_id | tiktok, `7531…`（19桁、**文字列**） | 主キー |
| url | | |
| creator_id | `6800…` | アカウントの ID（handle ではない。下の creators 参照） |
| created_at | 2025-07-10 12:03:00（UTC） | |
| text | 説明文 | |
| media_type | video / photo | 写真投稿は属性が取れない（ToDo-1） |
| duration, width, height | 15, 1080, 1920 | |
| music_id | | 使用音源 |
| hashtags, mentions | JSON 配列 | |
| region, language | MM, en | TikTok の `locationCreated` / `textLanguage` |
| platform_labels | `["Finger Dance & Basic Dance"]` | TikTok のカテゴリラベル |
| cover_path | `media/tiktok/covers/7531….jpg` | 画像は DB に入れない（第6章） |
| extra | `{"channel_tags":…, "effect_stickers":…}` | 媒体固有 |
| first_seen_at | 2025-12-25 | 最初に見つけた日 |

### post_snapshots — 投稿の「変わる値」を取得ごとに

| 列 | 例 | メモ |
|---|---|---|
| platform, post_id, run_id | | 主キー |
| fetched_at | 2026-09-11 17:02 | |
| fetch_status | ok / deleted / private / no_data | **削除済みもこの行で記録する**（9か月で20%が消えた） |
| plays, likes, comments_n, shares, saves, reposts | 2,800,000 … | |
| suggested_words | JSON 配列 | 提案語は検索から来るので変わる |
| is_ad | 0/1 | 広告フラグは時期で付いたり外れたりする |

### creators / creator_snapshots — アカウント

| 表 | 列 | メモ |
|---|---|---|
| creators | platform, creator_id（主キー）, account_created, extra | **handle（@名）は主キーにしない。** 改名される（E2E で @jacksonnsmith → @jacksonisbouncin、store の検算でも8件） |
| creator_snapshots | platform, creator_id, run_id（主キー）, handle, name, bio, verified, followers, following, post_count, heart_count | 「取得時点のフォロワー数」を必ず日時つきで残す |

### comments / comment_snapshots — コメント（返信も同じ表）

| 表 | 列 | メモ |
|---|---|---|
| comments | platform, comment_id（主キー）, post_id, parent_comment_id, reply_to_comment_id, level（1=本体/2=返信）, text, created_at, user_id, user_handle, is_author, language, image_url, source | **いまの store.py の列をそのまま使う**。返信は別表にせず親を指す（第8章の決定どおり） |
| comment_snapshots | platform, comment_id, run_id（主キー）, like_count, reply_count, is_pinned, is_author_liked, is_top_list | いいね数は変わるので取得ごと |

---

## 4. ②いつ見たか（取得）の表

### runs — 取得1回

| 列 | 例 | メモ |
|---|---|---|
| run_id | `20260912_win_comments_silhouette` | 主キー。**いまの store.py の runs を引き継ぐ** |
| kind | list / enrich / comments | 一覧取得・属性取得・コメント取得 |
| platform | tiktok | |
| machine | mac / win | |
| session_id | | 一覧とコメントを同じブラウザセッションで取ったかを後から確かめるため（7-D の到達性の問題） |
| params | `{"cap":120,"replies":true}` | |
| started_at, finished_at, status | | |
| stats | `{"混入":0,"cid重複":0,"20件頭打ち":0}` | TikTok は失敗しても何も言わないので、自己申告の検算結果を必ず残す（引き継ぎ書 第0章） |

### comment_fetches — 動画ごとのコメント取得結果

| 列 | 例 | メモ |
|---|---|---|
| run_id, post_id | | 主キー |
| status | ok / empty / blocked / unreachable | unreachable はグリッドで見つからず取れなかったもの（E2E で38本） |
| fetched, replies_fetched | 120, 8 | 実際に取れた件数 |
| total_reported, has_more, top_list_count | 2092, 1, 88 | API が申告した総数など |
| cap | 120 | |

いまの store.py の `videos` 表の後ろ半分（`comments_fetched` 〜 `top_list_count`）を切り出したもの。

---

## 5. ③どう解釈したか（分析）の表

### analyses — 分析1回

| 列 | 例 | メモ |
|---|---|---|
| analysis_id | `silhouette_w1_e2e` | 主キー |
| workflow | song_tiktok | どのワークフローで回したか |
| target | `tiktok:music:7089…` | 分析の対象（集合・投稿・アカウントのどれか） |
| title, created_at, status, notes | | |

### analysis_runs — その分析が使った取得

| 列 | メモ |
|---|---|
| analysis_id, run_id | **この分析はどの時点のデータを見たか**を固定する。あとで再取得しても、過去の分析の数値は変わらない |

### その下の表（全部 analysis_id に属する）

| 表 | 1行の中身 | シルエット E2E で対応するファイル |
|---|---|---|
| taxonomies | 軸の版（軸の定義 JSON、作ったのは AI か人か、元にした版） | `clean/taxonomy.json` |
| labels | 1投稿（またはコメント）に付けたラベル（値の JSON、確信度 H/M/L、根拠） | `clean/labels.tsv`（200行） |
| phases | 段階（名前・開始・終了・理由） | `clean/pathway.md` の区切り（4段階） |
| selections | 代表動画（セル、選んだ理由、コメント上限、返信方針、取得できたか、代替先） | `reps.tsv`（65本） |
| outputs | AI（または人）の出力1件（JSON、使ったモデル、プロンプトのハッシュ、入力のハッシュ） | `video_analysis.jsonl`（27行）、界隈の統合 |
| artifacts | 成果物ファイルへの参照（パス、ハッシュ、作った Step） | `REPORT.md`、`NOTE_DRAFT.md`、`E2E_EVAL.md` |

ラベルと出力は、分析ごとに中身の形が違う（楽曲分析の「界隈」と、アカウント分析の「外れ値」など）。
そのため**値は JSON で持ち、表の形は共通**にする。ワークフローを足しても表を増やさずに済む。

---

## 6. DB の外に置くもの

| もの | 置き場所 | 理由 |
|---|---|---|
| サムネイル画像 | `output/media/<媒体>/covers/<post_id>.jpg` | 画像は大きい。DB にはパスだけ |
| AI に渡した入力一式・指示書・AI が書いたファイル | `output/analyses/<analysis_id>/` | 人がエディタで読む。DB の artifacts に参照とハッシュを残す |
| レポート（Markdown） | 同上 | 同上 |
| 納品用の CSV / ZIP | 今までどおり `store.py export` で書き出す | **Excel 運用（既存13列を先頭に）は維持**。DB から組み立てる |

---

## 7. レビューしてほしい論点

### R1. ファイルは1本にするか

- **案（推奨）**: `output/ugc.db` の1本に全部入れる。ものは1か所にだけあり、各分析はそれを ID で指す
- 別案: 分析ごと（または集合ごと）にファイルを分ける。分析単位で持ち運びやすく、消しやすい
- 推奨の理由: 同じ動画・アカウントが複数の分析に出てくる（第2-1節）。分けると重複と食い違いが起きる。
  持ち運び用には、今の ZIP 書き出しを分析単位で出せば足りる

### R2. 変わる値を別の表（スナップショット）にするか

- **案（推奨）**: 変わらない値は posts / creators / comments に1行。変わる値は *_snapshots に取得ごと1行
- 別案（今の store.py）: 取得ごとに全部を1行にする。構造は単純だが、第2-1節の重複が起きる
- 推奨の理由: 「投稿時点のフォロワー数は取れない」「9か月で20%が削除」「グリッドの並びは取得ごとに変わる」といった制約は、
  全部「いつ見た値か」を区別したいという話。表を分けておけば、定点観測（前回との差分）も表の比較だけで出せる
- 代わりに払うもの: 「最新の値」を引くには2つの表を結合する必要がある。これは「最新だけを見せるビュー」を1つ用意して吸収する

### R3. コメントも R2 と同じ扱いにするか

- **案（推奨）**: コメントも本文は1行、いいね数は取得ごと（comment_snapshots）
- 別案: コメントは今の store.py のまま、取得ごとに丸ごと1行
- 迷う理由: コメントを同じ動画について取り直すことは少ない。一方で、表ごとに規則が違うと AI が関数を書くときに間違えやすい。
  そこで規則を揃える方を推す。件数は最大でも数十万行で、SQLite で問題にならない

### R4. アカウントの主キーを handle（@名）にしないこと

- **案**: TikTok の内部 ID（uid）を主キーにし、handle はスナップショット側に置く
- 理由: 改名が実際に起きている（E2E で3件、store の検算で8件）。handle を主キーにすると、改名した瞬間に別人扱いになる

### R5. 分析の中身（ラベル・AI 出力）を JSON で持つこと

- **案**: 表の形は共通にして、値だけ JSON で持つ
- 別案: ワークフローごとに専用の列を持つ表を作る（例: `labels_song` に community / format / motive 列）
- 推奨の理由: ワークフローを足すたびに表を作るのは「関数を並べるだけで追加」という要件に反する。
  代わりに、JSON の形は各 Step が宣言したスキーマで検査する（`WORKFLOW_DESIGN.md` 3.4）

### R6. 今の store.py を作り替えること

- 今の store.py は主キーが「取得 × 動画」「取得 × コメント」。**R2 を採るとここが変わる**
- 守るもの: 19桁 ID は文字列、粒度を混ぜない、返信は親を指す、CSV のエスケープ、**納品 ZIP の中身（既存13列を先頭に）**
- 変わるもの: 表の主キー。単体テスト `tests/test_store.py` 14件のうち、表の形に依存するものは書き直す。
  納品 ZIP の中身が一字一句変わらないことを、新しいテストとして足す
- 既存の `output/ugc.db`（プロポーズ2回分）は新しい形に移し替える

---

## 8. シルエットを入れるとこうなる（試算）

| 表 | 行数 | 元のファイル |
|---|---|---|
| collections | 1 | 音源ページ |
| collection_members | 880 | `シルエット.csv`（2025-12 取得） |
| posts | 880 | 同上 ＋ `enriched.jsonl` |
| post_snapshots | 880 ＋ 880 | CSV の数値（2025-12）と `enriched.jsonl`（2026-09-11）の2時点。後者のうち 245本は削除済みなどで `fetch_status` ≠ ok |
| creators / creator_snapshots | 約550 / 約550 | `enriched.jsonl` の投稿者 |
| runs | 3 | 一覧・属性・コメント（Windows 機、2026-09-12） |
| comment_fetches | 65 | 代表65本（取得できたのは27本、38本は unreachable） |
| comments / comment_snapshots | 約4,050 / 約4,050 | `e2e.jsonl`（本体3,549 ＋ 返信496 ＋ 同梱返信） |
| analyses | 1 | `silhouette_w1_e2e` |
| taxonomies / labels / phases / selections | 1 / 200 / 4 / 65 | `clean/` と `reps.tsv` |
| outputs / artifacts | 約30 / 約10 | `e2e/report/` |

この取り込みは、実装の最初の手順（`WORKFLOW_DESIGN.md` 第5章の順1）の完了判定にもなる。
取り込んだあと、今のスクリプトが作ったファイル（`weekly.tsv` / `reps.tsv` / `comments/*.md`）を DB から作り直して一致すれば、器は正しい。

### 別のワークフローが来たとき

後で「@magichour0120 のアカウント分析」をやる場合:

1. 集合を1行足す（`tiktok:creator_uploads:<uid>`）。アカウントの投稿一覧を取り、collection_members に入れる
2. そのうち5本は、**posts に既にある**。新しく入るのはスナップショットだけ
3. その5本のコメントも既にある（シルエットの E2E で取得済み）。取り直すかどうかは Step 側で決める
4. analyses に1行足し、その下にラベル・出力をぶら下げる。シルエットの分析には一切触らない

---

## 9. この文書でやらないこと

- 表を作る SQL そのもの（レビュー後に書く）
- YouTube の取得（今回は TikTok だけ。表は YouTube も入る形にしてあるが、実装しない）
- 利用者（招待制の自己登録）ごとのデータの分け方。Web サービスに載せる段階で決める。
  そのときは analyses に「誰の分析か」の列を足す想定で、①②は利用者をまたいで共有してよいかが論点になる
