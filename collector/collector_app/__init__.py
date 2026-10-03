"""UGC Collector — 利用者の Mac で TikTok の取得を走らせる、メニューバー常駐の試作アプリ。

docs/COLLECTOR_TRIAL.md（試し方）、docs/ACQUISITION_PLACEMENT.md の案2（取得ランナー）。
取得の本体は本線の acquire/・analysis/ をそのまま使い、このパッケージは「Chrome とログイン・係の起動と再開・
スリープ・通知・メニュー」だけを持つ。仕事の受け取り先（jobs.py）を差し替えれば本線のサービスにつながる。
"""
VERSION = "0.1.0"
