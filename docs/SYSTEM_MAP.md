# 全体の地図 — 全部アプリに入れた形（2026-10-03）

友達（と運営）の Mac に入れるアプリ1つが、取得も AI の相手も全部持つ。計画と経緯は `docs/ALL_IN_APP_PLAN.md`、作業記録は `docs/STAGE4_LOG.md`。

> 同じ日の前の版（取得だけ Mac、AI の相手は Windows 機のサービス、ngrok 経由）は、ユーザーの決定（全部アプリ）で置き換えた。
> 前の版の中身は git の履歴（コミット 7ef5115）にある。

---

## 1. 場所は2つだけ

| 場所 | 誰のもの | 動くもの |
|---|---|---|
| **[Mac]** その人の Mac | 各自 | **UGC Analyzer**（メニューバーのアプリ）。①取得（TikTok・取得用の Chrome）②AI の相手（仕事の列・指示書・検査・データ・知識ベース）③レポートの置き場 |
| **[AI]** その人の Claude か ChatGPT | 各自（Anthropic・OpenAI） | 会話。Mac のアプリの道具を呼ぶ（Claude デスクトップアプリ、または ChatGPT の Mac アプリの Work の画面。2026-10-04〜） |

外とのやりとりは3つだけ（どれも Mac から出ていく。Mac に入ってくる口は無い）:

| 相手 | 何を | いつ |
|---|---|---|
| TikTok | 一覧・属性・コメント（その人の捨て垢・その人の回線） | 取得の2〜3時間 |
| note.com | 著者の記事の一覧と本文（知識ベースの更新） | 週1回と「UGC Analyzer の知識ベースを更新して」のとき。数リクエスト |
| Anthropic・OpenAI | AI の会話（Claude デスクトップアプリ・ChatGPT の Mac アプリが話す） | 着席のとき |

**運営の Windows 機と ngrok は、友達の経路に出てこない。** Windows 機は今のまま置いてある（段1〜3 の本番。触っていない）。

## 2. Mac の中

```
Claude デスクトップアプリ ──（標準入出力）──> UGC Analyzer --mcp    … AI の道具（Claude が起こす。2つ同時に起きることがある）
ChatGPT の Mac アプリ（Work）──（標準入出力）──┘                       （ChatGPT は ~/.codex/config.toml を読んで起こす）
                                                   │ 分析を作る・仕事を渡す・結果を検査して保存
                                                   ▼
                          ~/Library/Application Support/UGC Analyzer/
                            analyses/   分析ごとのフォルダ（原本・計算物・成果物・仕事の状態）
                            prompts/    指示書（編集できる）
                            knowledge/  知識ベース（著者の記事と蒸留物。新しくなる）
                            users/      会話で変えた設定
                            レポート/    できあがったレポート（曲ごと）
                                                   ▲
UGC Analyzer（メニューバー）── 5秒おきに見る ──────┘  待っている分析を拾って取得する（係は子プロセス）。
                                                     取得用の Chrome（ポート 9250）はコメントの段の頭で最小化で開く
```

| 部品 | ファイル | 役 |
|---|---|---|
| メニューバー | `collector/collector_app/app.py` | 取得の見張り・ログイン・通知・メニュー（指示書の編集・知識ベース・Claude につなぐ） |
| Claude の道具 | `collector/collector_app/mcp_local.py` | `--mcp` の入口。中身は本線の `mcp_proto.py`（道具9つ）・`proto_runner.py`・`flow_w1.py` |
| Claude につなぐ | `collector/collector_app/claude_link.py` | Claude の設定ファイルに道具を1行足す（控えを取る） |
| ChatGPT につなぐ | `collector/collector_app/codex_link.py` | Codex の設定ファイル（`~/.codex/config.toml`）に道具の節と道具ごとの許可を足し（控えを取る）、スキル（`~/.agents/skills/ugc-analyzer/`）を置く。ChatGPT は道具の案内を会話に入れないので、スキルで気づかせる |
| 取得 | 本線の `acquire/`・`analysis/` | 一覧 → 属性 → プール → コメント（Windows 機と同じ部品）。どの動画のコメントを何件取るかは `docs/COMMENT_TARGETS.md` |
| 指示書 | 本線の `prompt_store.py`・`service_prompts/w1/` | 初期の指示書と、利用者の指示書。「出力の形」の節はいつも初期のもの |
| 知識ベース | 本線の `kb_update.py` | 新着を見る・取り込みと整理の仕事 |

## 3. 1曲の流れ

| # | 利用者 | Claude | Mac のアプリ |
|---|---|---|---|
| 1 | 「UGC Analyzer で〇〇を分析して」（名指しが無ければ AI は道具を使わない） | 曲名・アーティスト名で `start_analysis`（ウェブ検索で楽曲ページや動画の URL が見つかっていれば足す。利用者に確かめない） | TikTok の discover のページ（曲名で人気の動画が並ぶ。ログインなし）の動画の音源を読んで楽曲ページを見つけ、楽曲ページを開いて UGC 数を比べ、一番使われている公式のページと、その2割以上の同じ曲の公式のページ（sped up 版など）を合わせて取ると決め、どのページで進めるか・外したページを返す。分析を作る（待ち行列へ）。メニューバーが拾う |
| 2 | Mac を開いたまま（2〜3時間） | 何もしない（待たない・見に来ない） | 取得。終わると Mac の通知 |
| 3 | 「〇〇の分析を続けて」 | `next_task` → 指示書どおりに → `submit` を繰り返す | 仕事を渡し、検査し、保存する。サービスの工程（代表の選定・組み立て・検算・Excel 用 ZIP）も Mac で |
| 4 | 界隈の案に答える | — | — |
| 5 | Mac と Claude を開いたまま（界隈の確認のあと30〜45分） | 続きを片付ける。知識ベースの取り込みがあれば、ここではさむ | 完成したらレポートを「レポート」フォルダへ |

## 4. 言葉の決まり

| 言葉 | 指すもの |
|---|---|
| UGC Analyzer | 友達の Mac に入れるアプリ（メニューバー）と、それが AI に出す道具。利用者に見せる呼び方はこれだけ（2026-10-05 に UGC Collector・取得アプリ・分析アプリから統一。中の部品の名前 collector_app・UGC_COLLECTOR_HOME などはそのまま） |
| 道具 | アプリが AI に出す12（start_analysis・status・next_task・submit・read・revise・deepen・settings・prompts・update_knowledge・cancel_analysis・restart_analysis）。deepen は完成後の界隈の掘り下げ（2026-10-06〜、`docs/DEEPEN_COMMUNITY.md`） |
| 指示書 | AI に渡す作業の指示（11本）。利用者が直せる |
| 知識ベース | 著者（山本慶太朗）の note 記事と、その蒸留物（用語集・文体ガイド・カード） |
| 取得用の Chrome | アプリが開く、専用プロファイルの Chrome（捨て垢のログインはここだけ） |
