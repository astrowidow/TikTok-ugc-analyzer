"""待つ命令（`UGC Analyzer --wait <分析>`）。Claude の Code タブで、Claude が裏で走らせる（docs/CODE_TAB_ONE_SITTING.md）。

取得（一覧・属性・コメント）か、掘り下げ・切り直しの取り足しが終わるまで待ち、1行を出して終わる。
Code タブは、裏で走らせたコマンドが終わると Claude を起こし直すので、Claude はそこから next_task を続けられる
（チャット・Cowork にはこの仕組みが無いので、利用者が「続けて」と言う2回着席のまま）。

  終わり方: 0＝終わった（next_task から続ける）／1＝止まった・やめてある・アプリが動いていない／2＝TikTok のログイン待ち／3＝待つ上限（もう一度走らせる）
待っている間は何も書き換えない（見るだけ）。終わったら、AI が書く間（約1時間半）Mac を眠らせない。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from . import config

POLL_S = 30
LIMIT_H = 12.0
AWAKE_S = 5400          # 待ちが終わったあと、AI が書く間 Mac を眠らせない（caffeinate -i）
BUSY = ("queued", "running")


def _meta(d: Path) -> dict:
    try:
        return json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find(ref: str | None) -> Path | None:
    """分析 ID か曲名（部分一致）で探す。複数当たれば、取得中・取り足し中のもの、その中で新しいもの"""
    base = config.ANALYSES_DIR
    if ref and (base / ref / "analysis.json").exists():
        return base / ref
    cands = []
    for d in sorted(base.glob("*")) if base.exists() else []:
        m = _meta(d)
        if not m or (ref and ref not in str(m.get("title", "")) and ref not in d.name):
            continue
        busy = (m.get("acquisition") or {}).get("status") in BUSY or (m.get("deepen") or {}).get("status") in BUSY
        cands.append((busy, d.name, d))
    return max(cands)[2] if cands else None


def _app_running() -> bool:
    try:
        pid = int(config.PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _login_wanted() -> bool:
    return (config.LOCK_DIR / "need_login.json").exists()


def state(d: Path) -> tuple[str, str]:
    """(busy | done | stopped | cancelled, 何を待っているか)"""
    m = _meta(d)
    acq, dp = m.get("acquisition") or {}, m.get("deepen") or {}
    what = "切り直した界隈の取り足し" if dp.get("kind") == "recut" else "掘り下げの取り足し"
    if acq.get("status") == "cancelled":
        return "cancelled", "取得"
    if acq.get("status") in BUSY:
        return "busy", "取得"
    if acq.get("status") in ("failed", "blocked"):   # アプリが続きから取り直す。動いている限り待つ
        return "busy", "取得（止まったところから取り直し中）"
    if dp.get("status") in BUSY:
        return "busy", what
    return "done", what if dp.get("status") in ("done", "failed") else "取得"


def keep_awake(seconds: int = AWAKE_S) -> None:
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-t", str(int(seconds))], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def wait(ref: str | None, limit_h: float = LIMIT_H, poll_s: float = POLL_S, awake: bool = True) -> tuple[int, str]:
    d = find(ref)
    if d is None:
        return 1, f"「{ref or ''}」に当たる分析がありません。status で分析 ID を確かめてください"
    title = _meta(d).get("title") or d.name
    t0 = time.time()
    while True:
        st, what = state(d)
        if st == "done":
            if awake:
                keep_awake()
            return 0, f"終わりました: 「{title}」の{what}（分析 ID {d.name}）。next_task から続けてください"
        if st == "cancelled":
            return 1, f"「{title}」の取得はやめてあります（分析 ID {d.name}）。利用者に伝えて止まってください"
        if _login_wanted():
            return 2, ("ログイン待ち: UGC Analyzer が TikTok のログインを待っています。利用者に「UGC Analyzer が開いた Chrome で、"
                       "分析専用のサブアカウントでログインしてください」と伝えて止まってください")
        if not _app_running():
            return 1, ("止まりました: この Mac の UGC Analyzer が動いていません。利用者に「アプリケーションフォルダの UGC Analyzer を開いてください。"
                       "済んだところの続きから取ります」と伝えて止まってください")
        if time.time() - t0 > limit_h * 3600:
            return 3, f"まだ終わっていません（{what}。待つ上限 {limit_h:g}時間）。もう一度 ./ugc-wait {d.name} を裏で走らせてください"
        time.sleep(poll_s)


def main(args: list) -> int:
    ref = args[0] if args else None
    limit_h = float(args[1]) if len(args) > 1 else LIMIT_H
    code, line = wait(ref, limit_h)
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
