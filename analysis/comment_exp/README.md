# コメント取得戦略の実験の道具

「最小のコメントで、今の分析と同じ点数のレポート」を確かめるための道具（`docs/COMMENT_STRATEGY_HANDOVER.md` 第5章）。
TikTok にも本物の置き場にも書き込まない。本物の置き場（`~/Library/Application Support/UGC Analyzer/`）は読むだけで、
写しは `output/comment_exp/home/`（git の対象外）に作る。

## 流れ

```
setup_home.py                    実験の置き場を作る（知識ベース・指示書を写して固定。一度だけ）
fork.py <元> <新> [--cut …] --pin-refs
                                 plan の直後まで戻した写し。コメントを削り、derived/comments を作り直し、plan を走らせる（AI は使わない）
AI 役（AI_ROLE.md を渡したサブエージェント）が ai.py next / read / submit で ccomments から done まで回す
grading/blind.py <曲>            条件を伏せた採点の束（採点者2人分）。対応表は output/comment_exp/keys/
採点者（grading/GRADER.md を渡したサブエージェント）が束ごとに grade.json を書く
grading/aggregate.py <曲>        点数・元の版どうしのぶれ・同点の判定
grading/metrics.py <曲>          機械の指標（主張の数・引用 cid・検算・AI の作業量）
cuts.py                          削り方ごとに残るコメントの量（AI を使わない）
```

削り方（`cuts.py`）: `no_replies`（返信を開かない）・`artist40`（本人だけの理由で120件の動画を40件に）・`std20`（標準40件を20件に）・`half_weekly`（週ごとに配った本数を半分に）。
取得の時間の模擬は `analysis/comment_pace/strategies.py`（同じ選び方を使う）。

版の一覧は `grading/versions.json`（曲ごとに、元の版 `base`・削った版・確かめ用 `check`）。写しを作ったら足す。

## 確かめたこと（2026-10-05、AI は使っていない）

- 元の量の写しは、本番と同じ仕事の列になる（シルエット57個・きゃわ51個。plan の結果も一致）。作り直した derived/comments も本番と同じ
  （シルエットは Windows 機で作ったので改行が CRLF。中身は同じ）
- 写しの task_id は全部新しい ID。差し戻しも本番と同じ検査で返る
- 知識ベースの仕事は割り込まない（`proto_runner.LOCAL` が None のため）
