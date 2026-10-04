# ChatGPT 展開の引き継ぎ書（2026-10-04）

この文書は、次のセッションで「ChatGPT を使う友達にも、Claude と同じ使い勝手で UGC Analyzer を使ってもらう」作業を始めるためのもの。
前のセッション（2026-10-03〜04）の到達点、ChatGPT について調べて分かったこと、最初にやることをまとめる。

> **追記（2026-10-05）**: 第4章の 1〜5 は済んだ（実機確認 → 0.3.0「ChatGPT につなぐ」→ 0.4.0）。経緯と結果は `docs/STAGE4_LOG.md` の
> 「ChatGPT の実機確認と『ChatGPT につなぐ』」「名指しのときだけ始める・呼び方を『UGC Analyzer』に統一」の節。要点:
> ChatGPT は Mac アプリの **Work** の画面で使う／Codex は道具の案内を会話に入れないので **スキル**（`~/.agents/skills/ugc-analyzer/`）が要る／
> 「ChatGPT につなぐ」が設定・11の道具の許可・スキルを書く／新しい分析は「**UGC Analyzer で**〇〇／△△を分析して」と名指ししたときだけ／
> アプリ名とデータの置き場は「UGC Analyzer」に変わった（この文書の中の「UGC Collector」は前の名前）。

---

## 0. 進め方とユーザーの決まり（必ず守る）

- **返事は全部日本語**（途中の一言・作業の合間の報告・最後のまとめも）。何度も英語に崩れて指摘された。全体の設定に見張りのフック（`~/.claude/hooks/ja_guard.py`）が入っていて、英語の返事は差し戻される
- 利用者（友達）の会話の流れを変える変更（確認で止める関所など）は、作る前に案を出して聞く。迷ったら「止めて確かめる」より「何で進めるかを伝えて進める」
- ユーザーが「〜できないかな？」「〜は出ないんだっけ？」と聞いたときは、まず事実と案を答える。実装は返事を待つ。「Go」が出たら止まらずに進める
- 運営（ユーザー）の手間を増やす案は先に疑う。友達のログインを運営が代行する・ID やパスワードを預かる作りは論外
- 秘密（接続の URL・鍵）をリポジトリ・文書・作業記録に書かない（リポジトリは公開）
- python は PID で止める。取得の走行中は、取得用の Chrome と TikTok アカウントに触らない
- Bot 判定の回避（偽装・署名の再実装・住宅プロキシ・レートの探索）はしない
- コミットと push は区切りごとにしてよい（確認は要らない）
- 作業記録は `docs/STAGE4_LOG.md` に書き足す（日付つきの節で）
- アプリを入れ替えるときは、取得が走っていないことを確かめてから。メニューバーのアプリを PID で止め、`/Applications/UGC Collector.app` を入れ替えて開き直す（手順は第2章）

## 1. いまの形（全部アプリ）

友達の Mac に入れた「UGC Collector」（メニューバーの割れた音符のアイコン）が、取得も AI の相手も全部する。Windows 機・ngrok は友達の経路に無い。

```
友達の Mac
├─ UGC Collector（メニューバーのアプリ）… 取得（TikTok の楽曲ページ→一覧→属性→コメント）、Mac の通知、スリープ止め
│    └─ 取得用の Chrome（専用のプロファイル・ポート。友達が分析用の TikTok アカウントでログイン）
├─ 「UGC Collector --mcp」… AI に出す道具（MCP、標準入出力）。仕事の列・指示書・検査・データ・知識ベースは全部この中
└─ Claude デスクトップ … 設定ファイル（claude_desktop_config.json）の mcpServers に「ugc-analyzer」として登録され、上の道具を起こして使う
```

友達がすること: 「〇〇／△△を分析して」→（約13時間、Mac が取得）→「〇〇の分析を続けて」→ 界隈の案に OK → 完成（1.5〜3時間）。

### 主なファイル（リポジトリ `/Users/belle/workspace/TikTok-ugc-analyzer/`）

| 場所 | 役目 |
|---|---|
| `collector/collector_app/app.py` | メニューバーのアプリ（メニュー・取得の係の起動・通知・スリープ止め・指示書と知識ベースの見張り）。「Claude につなぐ」のメニューもここ |
| `collector/collector_app/claude_link.py` | Claude デスクトップの設定ファイルに道具を書き足す・Claude を開き直す。**ChatGPT 用にはこれに当たるものが要る** |
| `collector/collector_app/mcp_local.py` | 道具のサーバー（`--mcp`）。Mac 側の作業（楽曲ページを探す・読む、取得をやめる印）を `LocalHooks` で差し込む |
| `mcp_proto.py` | 道具の定義（11個: start_analysis, next_task, submit, read, status, revise, settings, prompts, update_knowledge, cancel_analysis, restart_analysis）と、サーバーの instructions |
| `proto_runner.py` | 仕事の列（next_task / submit）・検査・楽曲ページの選び方（20% の規則）・取得のやめ／やり直し |
| `flow_w1.py` | 分析の流れ（W1）: 界隈の案 → 確認 → ラベル → 段階 → 界隈ごとのコメント分析 → 参考記事 → 構成案 → 執筆 → 仕上げ → 検算 → Excel 用 ZIP → 完成 |
| `service_prompts/w1/*.md` | AI への指示書（利用者が編集できる。`prompt_store.py`） |
| `kb_update.py` | 知識ベース（著者の note の新着の取り込み・用語集・界隈の名付け方の手引き） |
| `acquire/pipeline.py` | 取得の工程。複数の楽曲ページの合算・曲の公開より前の投稿の除外もここ |
| `collector/build.sh` | .app と .dmg を作る（自己点検と `tests/check_local_mcp.py --app` つき） |
| `tests/test_local_app.py`, `tests/check_local_mcp.py` | 単体試験と、道具を AI と同じ形（標準入出力）で起こす確かめ |

データの置き場（友達の Mac）: `~/Library/Application Support/UGC Collector/`（analyses/・knowledge/・prompts/・レポート/・logs/）。

### 道具は AI の種類を問わない

- サービス側（道具の説明・instructions・指示書・返事の文面）には Claude 専用の言い回しはほぼ無い（「Claude」はコメントに2つだけ）
- Claude 専用なのは、アプリのメニュー（「Claude につなぐ」と、そのあとの通知・案内の文、`app.py` に26か所）・`claude_link.py`・友達向けの文書（`docs/FRIEND_GUIDE.md`・`docs/LINE_MESSAGES.md`）
- 道具 `read` は、サムネの一覧画像を画像（MCP の Image）で返す。分析の最初（界隈の案）でこれを見る

## 2. 前のセッションの到達点（2026-10-03〜04）

- 版は 0.2.0（`collector/collector_app/__init__.py`）。運営の Mac の `/Applications/UGC Collector.app` は最新（develop の 82c5db9 時点）
- 入れ替えの手順（運営の Mac）: `collector/build.sh` → 取得の仕事が無いことを確かめる（analyses/*/analysis.json の acquisition.status に queued・running が無い）→ `~/Library/Application Support/UGC Collector/app.pid` の PID を kill → `/Applications/UGC Collector.app` を消して `collector/dist/UGC Collector.app` を ditto → `open -g` で開き直す。道具の形を変えたときは、Claude も開き直す（⌘Q）
- 済んだこと（詳しくは `docs/STAGE4_LOG.md`）
  - 楽曲ページは **Mac が TikTok で探す**（`tiktok.com/discover/曲名` の人気の動画の音源を読み、楽曲ページの UGC 数を比べる。AI のウェブ検索には楽曲ページが出ないため）。start_analysis は曲名だけで30秒前後かかる
  - 同じ曲の楽曲ページを**合わせて取る**（一番使われている公式のページの 20% 以上。個人の音源は除く。UGC 数は合計と内訳）
  - **曲の公開（楽曲ページが作られた時刻。番号の上の32ビット）より前の日付の投稿を、一覧の段で除く**
  - 界隈の案は、全レポートから蒸留した **界隈の名付け方の手引き**（`kb:community`）を読む（固定の過去レポート2本はやめた）
  - レポートのフォルダに `週ごとの投稿数.csv` を置く（そのほかの途中物は置かない。全データは data.zip）
  - 取得をやめる・最初からやり直す道具（cancel_analysis / restart_analysis）
  - 2ページを合わせた本番規模の一晩の取得（きゃわぽっぴんどぅー、a20261004-0837-4e69）が問題なく終わった（混入0・頭打ち0・ブロック無し）
  - Mac の通知は、画面の共有中（AltTab などの画面取り込み）だと macOS が消音する。運営の Mac は「画面のミラーリングまたは共有中に通知を許可」をオンにして解決
- 決まったこと
  - 「続けて」のきっかけは**今のまま**（取得が終わったら利用者が「〇〇の分析を続けて」と言う）。自動化は「ChatGPT でも同じ使い勝手でなければ作る価値はない」が条件で、見送り
  - 年の離れた版（例: シルエットの 2020年と2025年のページ）も合わせてよい
- 残っていること
  - きゃわぽっぴんどぅーの「続けて」→ 界隈の案（手引きを読んだ新しい案）→ レポートの完成を、ユーザーが確かめる
  - 2026-10-06 から Claude の Cowork の新しい作業はクラウドで動く。Mac の中の道具が使えるかを一度確かめる
  - 配る準備: 友達への質問（U1。`docs/LINE_MESSAGES.md` 第1節）、.dmg の置き場（U2。おすすめは Google ドライブの共有リンク）
  - 友達のセットアップ手順の下書きは会話に出した（一人で進められる形）。FRIEND_GUIDE.md への差し替えはユーザーの OK 待ち

## 3. ChatGPT について調べて分かったこと（2026-10-04、公式文書と第三者情報。**実機では未確認**）

| 項目 | 分かったこと | 確からしさ |
|---|---|---|
| ChatGPT の Mac アプリ | 2026-07 に Codex を統合。Chat / Work / Codex の3つの画面。全プラン（Free を含む）で使えるという記述 | 中（発表の抜粋） |
| Mac の中の道具（ローカル stdio MCP） | **Codex の画面なら使える**。設定は `~/.codex/config.toml` の `[mcp_servers.<名前>]`（command / args / env）。Codex CLI と共有。Work の画面も使えるという第三者情報 | Codex 高・Work 中 |
| 普段の Chat の画面・chatgpt.com | **使えない**（ブラウザ版はローカルの設定を読まない。Mac アプリの Chat 画面でローカルの道具が出ない不具合報告もある） | 高 |
| 道具の画像（MCP の Image） | Codex は道具が返した画像をモデルに渡す。ただし結果に structuredContent が付くと画像が落ちる不具合の報告がある（`read` の返し方を確かめる） | 高・要確認 |
| 道具の時間切れ | Codex は道具1回の待ちが既定60秒（`tool_timeout_sec` で延ばせる）。start_analysis は30〜40秒かかるので、余裕を持たせる | 高 |
| 道具の許可 | 無人実行（`codex exec`・定期タスク）では道具の呼び出しが黙って拒否されることがあり、`default_tools_approval_mode = "approve"` で通るという第三者の報告 | 中 |
| 文面入りで開くリンク | `codex://new?prompt=…`。入れるだけで送信はしない。どの画面で開くかは選べない | 高 |
| 利用枠 | Plus の目安が文書にあるが、分析1本（ラベル付け・界隈ごとのコメント分析・執筆）がどのくらい使うかは測っていない | 未 |

出典などの詳しいメモは、前のセッションのメモリー `chatgpt-parity-research.md` にある。

**大事な前提**: ChatGPT の友達には、普段の Chat ではなく **Codex の画面**（または Work）で話しかけてもらうことになる見込み。ユーザーは ChatGPT の有料プランを持っていない（前のメモ）。試すにはアカウントが要る（Free で足りるかも確かめる）。

## 4. 最初にやること（提案。ユーザーと合わせてから作る）

1. **実機で確かめる（作る前に）**。ユーザーの ChatGPT のアカウントで次を確かめる（試す手順を用意して、ユーザーに頼む部分を分ける）
   - Mac の ChatGPT アプリの Codex 画面に、`UGC Collector --mcp` を道具として登録して、`status` が呼べるか
   - `start_analysis`（30〜40秒）が時間切れにならないか。`read` のサムネの画像を読めるか
   - 道具の許可の出方（毎回聞かれるか・「常に許可」に当たるものがあるか）
   - 「〇〇の分析を続けて」で next_task → submit を繰り返し、界隈の確認（ask_user）で止まって答えを待つか。長い仕事（ラベル付けの繰り返し）を最後まで回せるか
   - Free と Plus のどちらで、分析1本を回しきれるか（利用枠）
2. **アプリに「ChatGPT につなぐ」を足す**（`claude_link.py` に当たる `codex_link.py`）
   - `~/.codex/config.toml` に `[mcp_servers.ugc-analyzer]`（command は `/Applications/UGC Collector.app/Contents/MacOS/UGC Collector`、args は `["--mcp"]`）と、`tool_timeout_sec`・許可の設定を書き足す。既存の設定は壊さない（控えを取る。TOML を書き換える）
   - ChatGPT アプリを開き直す案内。メニューは「Claude につなぐ」「ChatGPT につなぐ」の両方を出すか、入っているアプリを見て出し分ける
3. **案内の文を AI の種類で出し分ける**: アプリの通知・メニューの文（「Claude で〜と言ってください」）、取得の受付の返事、FRIEND_GUIDE・LINE の文面。ChatGPT の友達には「Codex の画面で」を明記する
4. **道具の返し方を Codex に合わせる**: `read` が画像を返すときに structuredContent が付かないか（mcp 2.2.0 の FastMCP の出力を確かめる）。必要なら画像を返す道具だけ構造化の出力を切る
5. 試験（`tests/check_local_mcp.py` に Codex の設定ファイルを書く試験を足す）→ build → 入れ替え → 作業記録 → コミット・push

## 5. 決めることの候補（ユーザーに聞く）

- ChatGPT の友達に、Codex の画面を使ってもらうことでよいか（普段の Chat では使えない）
- 試すアカウント（ユーザーが ChatGPT に登録するか。Free で足りるか）
- 1台の Mac で Claude と ChatGPT の両方につなぐ形を許すか（同じ分析を両方から触れる）
