"""
取得の係。待ち行列（各分析の analysis.json の acquisition）を先頭から1つずつ片付け、空になったら終わる。

Web サービスとは別のプロセスで動く（Windows ではタスク tiktok-acq から起動。acquire/run-acq.bat）。
Web サービスを再起動しても取得は止まらない。係は同時に1つだけ（output/locks/acq_worker.json に PID）。
途中で止まった分析（status=running なのに PID が死んでいる）は、次に係が起きたとき続きから再開する。

  python -m acquire.worker
"""
import datetime
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import tiktok_lock  # noqa: E402
from acquire import deepen, pipeline  # noqa: E402

GUARD = tiktok_lock.LOCK_DIR / "acq_worker.json"
LOG_FILE = BASE_DIR / "logs" / "acq-worker.log"


def worker_pid():
    try:
        info = json.loads(GUARD.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    pid = info.get("pid")
    return pid if tiktok_lock.pid_alive(pid) else None


def claim() -> bool:
    other = worker_pid()
    if other and other != os.getpid():
        return False
    GUARD.parent.mkdir(parents=True, exist_ok=True)
    GUARD.write_text(json.dumps({"pid": os.getpid(), "since": datetime.datetime.now().isoformat(timespec="seconds")}),
                     encoding="utf-8")
    return True


def queue() -> list:
    """待っている分析（受け付け順）。途中で止まったものも含める。
    完成後の界隈の掘り下げの取り足し（acquire/deepen.py、20分前後）は、受け付け順より先に並べる（半日の取得の後ろで待たせない）"""
    items = []
    if not pipeline.ANALYSES_DIR.exists():
        return items
    for d in pipeline.ANALYSES_DIR.iterdir():
        if not d.is_dir():   # .DS_Store など
            continue
        m = pipeline.read_json(d / "analysis.json")
        acq = (m or {}).get("acquisition") or {}
        st = acq.get("status")
        if st == "queued" or (st == "running" and not tiktok_lock.pid_alive(acq.get("pid") or -1)):
            items.append((1, acq.get("queued_at") or "", d.name))
        elif m and deepen.queued(m):
            items.append((0, (m.get("deepen") or {}).get("queued_at") or "", d.name))
    return [name for _, _, name in sorted(items)]


def run_one(aid: str) -> str:
    """待ち行列の1つを片付ける: 取得が残っていれば取得、そうでなければ界隈の掘り下げの取り足し"""
    m = pipeline.read_json(pipeline.ANALYSES_DIR / aid / "analysis.json", {}) or {}
    if (m.get("acquisition") or {}).get("status") not in ("done", "cancelled", "failed") or not deepen.queued(m):
        return pipeline.Run(aid).run()
    return deepen.Run(aid).run()


def main() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)-7s acq-worker %(message)s")
    log = logging.getLogger("acq-worker")
    if not claim():
        log.info("別の係（PID %s）が動いているので終わります", worker_pid())
        return 0
    log.info("係を始めます（PID %s）", os.getpid())
    try:
        while True:
            q = queue()
            if not q:
                log.info("待ち行列が空になったので終わります")
                return 0
            aid = q[0]
            log.info("取得を始めます: %s（待ち %d件）", aid, len(q) - 1)
            try:
                res = run_one(aid)
            except Exception:
                log.error("取得が例外で止まりました: %s\n%s", aid, traceback.format_exc())
                res = "failed"
                try:
                    m = pipeline.read_json(pipeline.ANALYSES_DIR / aid / "analysis.json", {})
                    m.setdefault("acquisition", {}).update({"status": "failed", "error": "係の例外（ログを見てください）"})
                    pipeline.write_json(pipeline.ANALYSES_DIR / aid / "analysis.json", m)
                except Exception:
                    pass
            log.info("取得が終わりました: %s → %s", aid, res)
            time.sleep(5)
    finally:
        try:
            if json.loads(GUARD.read_text(encoding="utf-8")).get("pid") == os.getpid():
                GUARD.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
