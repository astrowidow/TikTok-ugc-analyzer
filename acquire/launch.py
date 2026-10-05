"""
取得の受け付け（Web サービスの接続口と運営のコマンドから使う）。

- new_analysis(): 分析フォルダを作って待ち行列に入れる（analysis.json の acquisition.status = queued）
- ensure_worker(): 取得の係が動いていなければ起こす。Windows はタスク tiktok-acq（対話セッション、多重起動しない）、
  無ければ切り離したプロセス。係は Web サービスの外で動くので、Web サービスを再起動しても取得は止まらない
- progress(): 待ち行列の中の位置と、残り時間の見込み

運営用:
  python -m acquire.launch new --song シルエット --artist KANA-BOON --music-url https://www.tiktok.com/music/... \\
      --owner operator [--pool-budget 10] [--comment-hours 1]
  python -m acquire.launch kick      # 係を起こす
  python -m acquire.launch show      # 待ち行列
"""
import argparse
import datetime
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from acquire import pipeline, worker  # noqa: E402

TASK_NAME = "tiktok-acq"
# 見込み（シルエット規模）。一覧5分・属性 1本3.3秒・派生2分・コメントはコメントを取る本数×1本の見込み（pipeline.minutes_per_comment_video）
LIST_SECONDS = 300
ENRICH_SECONDS_PER_VIDEO = 3.3
DERIVE_SECONDS = 120
TYPICAL_VIDEOS = 900
# プールが決まる前の、コメントを取る本数の見込み（"page"＝必ず入れる動画だけ。シルエット59本・きゃわ97本、2026-10-06）
TYPICAL_COMMENT_VIDEOS = 80


def new_analysis(owner: str, song: str, artist: str = "", music_url: str = "", settings: dict | None = None,
                 music_urls: list | None = None) -> str:
    """music_urls: 同じ曲の楽曲ページが複数のとき全部（1つ目が主。music_url はその主）"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    aid = f"a{stamp}-{secrets.token_hex(2)}"
    d = pipeline.ANALYSES_DIR / aid
    for sub in ("raw", "fetch_log", "derived", "outputs", "eval", "state"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    meta = {
        "analysis_id": aid,
        "title": song or music_url,
        "song": {"artist": artist, "title": song},
        "owner": owner,
        "created_at": pipeline.now(),
        "music_url": music_url or None,
        "music_urls": [u for u in (music_urls or [music_url]) if u],
        "download_key": secrets.token_urlsafe(24),
        "acquisition": {"status": "queued", "queued_at": pipeline.now(), "steps": {}},
        "layout": {"raw": "①原本", "fetch_log": "②取得の記録", "derived": "③計算物", "outputs": "④成果物",
                   "eval": "⑤評価", "state": "サービスの台帳（仕事の状態）"},
    }
    if settings:
        meta["acquisition_settings"] = settings
    pipeline.write_json(d / "analysis.json", meta)
    return aid


def find_active(owner: str, song: str, music_url: str = ""):
    """同じ利用者の、同じ曲の取得中・待ちの分析（AI が二重に頼んだときに2つ作らない）"""
    for d in pipeline.ANALYSES_DIR.iterdir() if pipeline.ANALYSES_DIR.exists() else []:
        m = pipeline.read_json(d / "analysis.json") or {}
        if m.get("owner") != owner:
            continue
        st = (m.get("acquisition") or {}).get("status")
        if st not in ("queued", "running"):
            continue
        if (music_url and music_url in ([m.get("music_url")] + list(m.get("music_urls") or []))) or \
                (song and (m.get("song") or {}).get("title", "").strip().lower() == song.strip().lower()):
            return d.name
    return None


def ensure_worker() -> str:
    if os.environ.get("UGC_ACQ_NO_KICK"):   # Mac での試験用: 係を起こさない（TikTok に触らない）
        return "disabled"
    if worker.worker_pid():
        return "running"
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True)
        if r.returncode == 0:
            return "started(task)"
        flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen([sys.executable, "-m", "acquire.worker"], cwd=str(BASE_DIR), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            subprocess.Popen([sys.executable, "-m", "acquire.worker"], cwd=str(BASE_DIR),
                             creationflags=0x00000008 | 0x00000200,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "started(process)"
    subprocess.Popen([sys.executable, "-m", "acquire.worker"], cwd=str(BASE_DIR), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "started(process)"


def _remaining_seconds(m: dict) -> int:
    """その分析の取得の残り時間の見込み"""
    acq = m.get("acquisition") or {}
    steps = acq.get("steps") or {}
    s = {**pipeline.DEFAULTS, **(m.get("acquisition_settings") or {})}
    n_links = ((steps.get("list") or {}).get("detail") or {}).get("links") or TYPICAL_VIDEOS
    if s.get("comment_plan", "page") == "page":
        guess = TYPICAL_COMMENT_VIDEOS
    else:
        guess = int(s.get("pool_budget") or 0) or int(float(s["comment_hours"]) * 60 / float(s["min_per_video"]))
    n_pool = ((steps.get("pool") or {}).get("detail") or {}).get("n_pool") or guess
    per = pipeline.minutes_per_comment_video(s)
    est = {"resolve": 60, "list": LIST_SECONDS, "enrich": n_links * ENRICH_SECONDS_PER_VIDEO, "derive": DERIVE_SECONDS,
           "pool": 10, "comments": n_pool * per * 60 + 600,
           "comments_md": 30, "notify": 5}
    total = 0.0
    for step in pipeline.STEPS:
        st = steps.get(step) or {}
        if st.get("status") == "done":
            continue
        e = est[step]
        if st.get("status") == "running" and step == "comments":
            e = max(600.0, e - float(st.get("active_hours") or 0) * 3600)
            # 走行中なら取れた本数から残りを見る
            done_n = pipeline.comments_ok(pipeline.ANALYSES_DIR / m["analysis_id"])
            if done_n:
                e = min(e, max(0, n_pool - done_n) * per * 60 + 300)
        elif st.get("status") == "running" and st.get("started_at"):
            try:
                el = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(st["started_at"])).total_seconds()
                e = max(60.0, e - el)
            except Exception:
                pass
        total += e
    return int(total)


def progress(analysis_id: str) -> dict:
    """待ち行列の位置と残り時間の見込み。取得が済んでいれば status=done"""
    m = pipeline.read_json(pipeline.ANALYSES_DIR / analysis_id / "analysis.json", {}) or {}
    acq = m.get("acquisition") or {}
    st = acq.get("status")
    if st in (None, "done", "failed"):
        return {"status": st or "none", "error": acq.get("error")}
    q = worker.queue()
    ahead_ids = q[:q.index(analysis_id)] if analysis_id in q else []
    # いま係が処理中の分析（PID が生きている running）は待ち行列の先頭より前にいる
    active = [d.name for d in pipeline.ANALYSES_DIR.iterdir()
              if ((pipeline.read_json(d / "analysis.json") or {}).get("acquisition") or {}).get("status") == "running"
              and d.name not in q and d.name != analysis_id]
    if analysis_id in q:
        ahead_ids = active + ahead_ids
    ahead = len(ahead_ids)
    wait = 0
    for other in ahead_ids:
        om = pipeline.read_json(pipeline.ANALYSES_DIR / other / "analysis.json", {}) or {}
        wait += _remaining_seconds(om)
    own = _remaining_seconds(m)
    step = acq.get("step")
    res = {"status": st, "ahead": ahead, "wait_seconds": wait, "own_seconds": own, "eta_seconds": wait + own,
           "step": step, "step_label": pipeline.STEP_LABELS.get(step) if step else None}
    if step == "comments":   # 「コメントを取る 35/197本」と出すため
        d = pipeline.ANALYSES_DIR / analysis_id
        res["comments_done"] = pipeline.comments_ok(d)
        res["n_pool"] = (((acq.get("steps") or {}).get("pool") or {}).get("detail") or {}).get("n_pool")
    return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    n = sub.add_parser("new")
    n.add_argument("--song", required=True)
    n.add_argument("--artist", default="")
    n.add_argument("--music-url", default="")
    n.add_argument("--owner", default="operator")
    n.add_argument("--pool-budget", type=int, default=0)
    n.add_argument("--comment-hours", type=float, default=0)
    n.add_argument("--no-kick", action="store_true")
    sub.add_parser("kick")
    sub.add_parser("show")
    a = ap.parse_args()
    if a.cmd == "new":
        settings = {}
        if a.pool_budget:
            settings["pool_budget"] = a.pool_budget
        if a.comment_hours:
            settings["comment_hours"] = a.comment_hours
        aid = new_analysis(a.owner, a.song, a.artist, a.music_url, settings or None)
        print(aid)
        if not a.no_kick:
            print(ensure_worker())
    elif a.cmd == "kick":
        print(ensure_worker())
    elif a.cmd == "show":
        print("worker pid:", worker.worker_pid())
        for aid in worker.queue():
            print(aid, json.dumps(progress(aid), ensure_ascii=False))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
