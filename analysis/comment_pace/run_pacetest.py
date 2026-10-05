"""間隔を詰めた長時間の走行（2026-10-05、ユーザー「いまあなたが長時間実験走らせてくれない？根拠を持って数字は決めて」）。

設定: 平均2.0回/分・60秒に3回まで（本番は1.8回/分・60秒に2回）。根拠は docs/COMMENT_SPEED.md 第5章「間隔を詰める」:
  瞬間3回/分は実績（5回/分を約4時間・11回/分を短時間、どちらもブロックなし）より十分低い。
  平均（模擬で2.23回/分）は長時間の実績1.95回/分と短時間の実績2.58回/分の間。実績に無いのは「2.2回/分前後を6時間」だけで、それをこの走行で観測する。
閾値を探る試験ではない（一段だけ・固定の設定・踏んだら止めて上げない。docs/COMMENT_ACQUISITION_HANDOVER.md 第4章）。

- 取るもの: シルエット（a20260930-2342-0035）のプール197本を、本番と同じ引数（acquire/pipeline.py の step_comments、A+B+C 入り、取る量も今のまま）で取り直す
- アプリと同じロックを取り、Chrome はアプリのプロファイルでポート 9251 に最小化で起こす（アプリは 9250 しか見ない）。終わったら自分で閉じる
- 空応答・4xx/5xx・チャレンジ画面を1回でも踏んだら spatest がその場で止める（リトライしない）。止まったらその日は再開しない
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

REPO = Path("/Users/belle/workspace/TikTok-ugc-analyzer")
APP = Path.home() / "Library/Application Support/UGC Analyzer"
SRC = APP / "analyses/a20260930-2342-0035"
# 引数なしは 10/5 の長時間の走行（2.0回/分・60秒に3回・プール197本）。短時間の試験は --cpm 2.5 --pool <一部> --out <別の場所>
ap = argparse.ArgumentParser()
ap.add_argument("--cpm", default="2.0")
ap.add_argument("--max-cpm", default="3.0")
ap.add_argument("--pool", default=str(SRC / "derived/pool.tsv"))
ap.add_argument("--out", default=str(REPO / "output/pacetest-20261005"))
ap.add_argument("--hours", default="8")
cli = ap.parse_args()
OUT = Path(cli.out)
CPM, MAX_CPM = cli.cpm, cli.max_cpm

os.environ["UGC_LOCK_DIR"] = str(APP / "locks")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "collector"))

import tiktok_lock  # noqa: E402
from acquire import spatest  # noqa: E402
from collector_app import chrome as chrome_mod  # noqa: E402

OUT.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(), logging.FileHandler(OUT / "run.log", encoding="utf-8")])
log = logging.getLogger("pacetest")
OWNER = f"pacetest:{OUT.name}"

if not tiktok_lock.try_acquire(OWNER):
    log.info("ロックが取れない（アプリが取得中）: %s", tiktok_lock.holder())
    sys.exit(2)
c = None
ch = None
try:
    ch = chrome_mod.Chrome(9251, APP / "chrome-profile", log)
    ch.ensure(minimized=True)
    if not ch.logged_in():
        log.info("TikTok のログインが切れているので走らせない")
        sys.exit(3)
    log.info("Chrome 準備よし（ログイン済み・%s）。設定 平均%s回/分・60秒に%s回まで", ch.window_state(), CPM, MAX_CPM)
    music_url = json.loads((SRC / "analysis.json").read_text(encoding="utf-8"))["music_url"]
    args = ["--port", "9251", "--music-url", music_url,
            "--pool", cli.pool,
            "--candidates", str(SRC / "derived/llm_input/records.jsonl"),
            "--subs-out", str(OUT / "subs.tsv"),
            "--collect-scrolls", "150",
            "--reply-policy", "targets", "--reply-top", "1", "--reply-questions", "1", "--reply-author", "1",
            "--cap", "40", "--min-comments", "20",
            "--calls-per-min", CPM, "--max-calls-per-min", MAX_CPM,
            "--interval", "15", "--jitter", "0.5", "--deadline-hours", cli.hours,
            "--out", str(OUT / "comments.jsonl"), "--log", str(OUT / "comments.log"),
            "--summary", str(OUT / "comments_summary.json"),
            "--stop-on-no-more", "--prescroll", "--only-open-video", "--remount-on-stall"]
    a = spatest.build_parser().parse_args(args)
    c = spatest.SpaCollector(a)
    t0 = time.time()
    try:
        c.run()
    except spatest.Blocked as e:
        c.log(f"!! 開始前にブロック検知: {e}")
    log.info("終わり: %.1f分", (time.time() - t0) / 60)
finally:
    try:
        if c and c.d:
            c.d.service.stop()
    except Exception:
        pass
    try:
        if ch:
            ch.quit()
    except Exception:
        pass
    tiktok_lock.release(OWNER)
    log.info("ロックを返した")
