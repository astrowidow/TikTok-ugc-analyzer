"""UGC Analyzer（アプリ。中の部品の名前は collector_app のまま）— 利用者の Mac で、TikTok の取得から AI の相手（仕事の列・指示書・検査・データ・知識ベース）まで全部を回す、
メニューバー常駐のアプリ。docs/ALL_IN_APP_PLAN.md（計画）、docs/COLLECTOR_TRIAL.md（試作 0.1.0 の記録）。

取得の本体は本線の acquire/・analysis/、AI の相手は本線の proto_runner・flow_w1・mcp_proto をそのまま同梱して使う。
このパッケージが持つのは「メニュー・Chrome とログイン・係の起動と再開・スリープ・通知」（app.py ほか）と、
「AI に出す道具の入口」（mcp_local.py）、「Claude につなぐ」（claude_link.py）・「ChatGPT につなぐ」（codex_link.py）。
"""
VERSION = "0.5.2"
