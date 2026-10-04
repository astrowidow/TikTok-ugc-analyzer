"""速さの実験（A+B+C）を、アプリの Chrome とロックを借りて走らせる。

- アプリと同じロック（~/Library/Application Support/UGC Analyzer/locks/tiktok.lock）を取るので、アプリの取得とはぶつからない
- Chrome はアプリの取得用（ポート 9250・アプリのプロファイル）を最小化で起こす。ログインが切れていたら走らせない
- 要求の間隔の設定は本番と同じ（平均1.8回/分・60秒に2回まで・動画の間15秒）
"""
import logging
import sys
import time
from pathlib import Path

REPO = Path("/Users/belle/workspace/TikTok-ugc-analyzer")
APP = Path.home() / "Library/Application Support/UGC Analyzer"
OUT = REPO / "output/speedtest-20261005"
import os  # noqa: E402

os.environ["UGC_LOCK_DIR"] = str(APP / "locks")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "collector"))

import tiktok_lock  # noqa: E402
from acquire import spatest  # noqa: E402
from collector_app import chrome as chrome_mod  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("speedtest")
OWNER = "speedtest:20261005"
mode = sys.argv[1] if len(sys.argv) > 1 else "abc"

if not tiktok_lock.try_acquire(OWNER):
    log.info("ロックが取れない（アプリが取得中）: %s", tiktok_lock.holder())
    sys.exit(2)
c = None
try:
    ch = chrome_mod.Chrome(9251, APP / "chrome-profile", log)
    ch.ensure(minimized=True)
    if not ch.logged_in():
        log.info("TikTok のログインが切れているので走らせない")
        sys.exit(3)
    log.info("Chrome 準備よし（ログイン済み・%s）", ch.window_state())
    tag = {"abc": "", "fix": "_fix", "base": "_base"}[mode]
    args = ["--port", "9251", "--music-url", (OUT / "music_url.txt").read_text().strip(),
            "--pool", str(OUT / "pool.tsv"),
            "--candidates", str(APP / "analyses/a20261004-0837-4e69/derived/llm_input/records.jsonl"),
            "--subs-out", str(OUT / f"subs{tag}.tsv"),
            "--collect-scrolls", "150",
            "--reply-policy", "targets", "--reply-top", "1", "--reply-questions", "1", "--reply-author", "1",
            "--cap", "40", "--min-comments", "20",
            "--calls-per-min", "1.8", "--max-calls-per-min", "2.0",
            "--interval", "15", "--jitter", "0.5", "--deadline-hours", "1.5",
            "--out", str(OUT / f"comments{tag}.jsonl"), "--log", str(OUT / f"comments{tag}.log"),
            "--summary", str(OUT / f"comments_summary{tag}.json")]
    if mode in ("abc", "fix"):
        args += ["--stop-on-no-more", "--prescroll", "--only-open-video", "--remount-on-stall"]
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
            c.d.service.stop()   # chromedriver だけ止める（Chrome 本体はアプリと同じ扱い）
    except Exception:
        pass
    try:
        ch.quit()   # 9251 はアプリが見ていないので、自分で閉じる（アプリは使うときに 9250 で起こし直す）
    except Exception:
        pass
    tiktok_lock.release(OWNER)
    log.info("ロックを返した")
