# 0.8.1 の作業記録（状態のファイル）

計画: docs/PLAN_0_8_1.md。起こされたら、まずここを読む。

## 分担

- 私（主）: flow_w1・proto_runner・mcp_proto・user_settings・指示書・文書・試験・リリース
- 手伝い（取得側）: acquire/deepen.py（videos・add・all・per_video）・acquire/pipeline.py（user_videos・番号を変えない作り直し）・
  analysis/prep_sample.py（--keep-seqs）・collector の知らせと一覧の文

## 進み具合

- [x] 1 設定の字数
- [x] 2 一人称
- [x] 8 消す・容量
- [x] 3 章立て（restructure）
- [x] 共通: 変更の回（change_prep・change_plan・change_review）
- [x] 4 外す・戻す
- [x] 6 動画ごと・全体の取り足し（AI 側）
- [x] 5 足す（AI 側）
- [ ] 取得側（手伝い）
- [x] 7 比べる
- [x] 道具の口・案内（19）
- [ ] 試験（全体）
- [ ] 写しの置き場で会話を流す
- [ ] 本物の取得（きゃわで足す・取り足し）
- [ ] 文書（始め方のページ・紙・LINE・SYSTEM_MAP・FULL_TEST_PROMPT）
- [ ] リリース 0.8.1

## 記録

- 2026-10-12 ユーザー「全て盛り込んでください」。計画を書いた。スリープ防止（caffeinate）と目覚まし（1〜4時間後）を仕掛けた
- 主の分: 1・2・8・3・変更の回・4・5/6 の AI 側・7・道具の口（19）を書いた。tests/test_flex_081.py 28本が通る。
  全体の試験は、手伝いの取得側の作業中（pipeline.source_of が未定義）で deepen まわりが落ちている。手伝いの報告を待つ
- 注意: 作業ツリーは手伝いと共有。git stash などツリー全体に効く操作はしない（一度やってしまい、すぐ戻した）
