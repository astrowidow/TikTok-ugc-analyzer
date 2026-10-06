"""待つ命令（`UGC Analyzer --wait <分析>`）。Claude の Code タブで、Claude が裏で走らせる（docs/CODE_TAB_ONE_SITTING.md）。

取得（一覧・属性・コメント）か、掘り下げ・切り直しの取り足しが終わるまで待ち、1行を出して終わる。
Code タブは、裏で走らせたコマンドが終わると Claude を起こし直すので、Claude はそこから next_task を続けられる
（チャット・Cowork にはこの仕組みが無いので、利用者が「続けて」と言う2回着席のまま）。

  終わり方: 0＝終わった（next_task から続ける）／1＝止まった（アプリがもう取り直さない・アプリが動いていない）・やめてある／
            2＝TikTok のログイン待ち／3＝待つ上限（もう一度走らせる）
待っている間は何も書き換えない（見るだけ）。待っている間と、終わったあと AI が書く間（約1時間半）は Mac を眠らせない。
"""
import datetime
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, jobs

POLL_S = 30
LIMIT_H = 12.0
AWAKE_S = 5400          # 待ちが終わったあと、AI が書く間 Mac を眠らせない（caffeinate -i）
BUSY = ("queued", "running")
STOP_POLLS = 3          # 止まった・ログイン待ちは、続けて3回（最初に見てから2回の見回り、約60秒）見たときだけ返す
                        # （アプリの起動中・眠りから覚めた直後・ログインした直後に、早まって Claude を起こさない）
STALL_S = 15 * 60       # 止まった取得を、アプリが待ち行列に戻すのを待つ長さ（アプリは30秒〜5分で戻す。ほかの取得の間は戻さない）


def _meta(d: Path) -> dict:
    try:
        m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def find(ref: str | None) -> Path | None:
    """分析 ID か曲名・アーティスト名（部分一致、大文字小文字を区別しない）で探す。道具（proto_runner.resolve）と同じ当て方。
    複数当たれば、題がぴったり合うもの、取得中・取り足し中のもの、その中で新しいもの"""
    base = config.ANALYSES_DIR
    ref = (ref or "").strip() or None
    if ref and (base / ref / "analysis.json").exists():
        return base / ref
    ref_n = (ref or "").lower()
    cands = []
    for d in sorted(base.glob("*")) if base.exists() else []:
        m = _meta(d)
        if not m:
            continue
        exact = False
        if ref_n:
            title = str(m.get("title") or "").strip().lower()
            s = m.get("song") if isinstance(m.get("song"), dict) else {}
            song_line = f"{s.get('artist', '')}「{s.get('title', '')}」".lower()
            if ref_n not in title and ref_n not in song_line and ref_n not in d.name.lower():
                continue
            exact = ref_n in (title, d.name.lower(), str(s.get("title") or "").strip().lower())
        busy = (m.get("acquisition") or {}).get("status") in BUSY or (m.get("deepen") or {}).get("status") in BUSY
        cands.append((exact, busy, d.name, d))
    return max(cands)[3] if cands else None


def _app_running() -> bool:
    """メニューバーのアプリが動いているか（AI の道具と同じ判定。残った app.pid の番号の使い回しを動いているとみなさない）"""
    from . import system
    return system.app_running()


def _app_started_at() -> float:
    """アプリが起動した時刻（app.pid は起動のときに書く）。読めなければ 0"""
    try:
        return config.PID_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _login_flag() -> bool:
    """係がログイン待ちの印（need_login.json）を置いて、ログインを待っているか。worker_entry.login_waiting と同じ見分け
    （印を置いた係が死んでいれば古い印）だが、見るだけで印は消さない（消すのはアプリ）。
    PID の無い前の形の印は印とみなす（この命令は setup_env を通らないので、acquire.worker の係の印の場所が違い、置いた係を確かめられない）"""
    try:
        info = json.loads((config.LOCK_DIR / "need_login.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True   # 書きかけ。少し待てば読める
    pid = info.get("pid") if isinstance(info, dict) else None
    return pid is None or jobs._alive(pid)   # noqa: SLF001 ゾンビ（回収されていない子）は死んだものとみなす


def _login_wanted() -> bool:
    """アプリが TikTok のログインを待っているか（mcp_local.LocalHooks.app_state と同じ判定:
    係がログイン切れに気づいた印があるか、ログインした時刻が無い。アプリはその間ログインの画面を出して、取得を進めない）"""
    if _login_flag():
        return True
    try:
        st = config.load_state()
    except OSError:
        return False
    return not (st.get("logged_in_at") if isinstance(st, dict) else None)


def _others_busy(d: Path) -> bool:
    """ほかの分析の取得・取り足しが順番待ち・取得中か（その間、アプリは止まった取得を待ち行列に戻さない）"""
    for o in config.ANALYSES_DIR.glob("*"):
        if o.name == d.name:
            continue
        m = _meta(o)
        if (m.get("acquisition") or {}).get("status") in BUSY or (m.get("deepen") or {}).get("status") in BUSY:
            return True
    return False


def _ts(s) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def state(d: Path) -> tuple[str, str, dict]:
    """(busy | done | failed | cancelled | gone, 何を待っているか, analysis.json の中身)。
    failed は取得が止まったもの（アプリが取り直すかは wait で決める）。gone は分析のフォルダが消えたもの"""
    if not d.is_dir():
        return "gone", "取得", {}
    m = _meta(d)
    if not m:   # 書きかけ・壊れていて、その瞬間は読めない。終わったとはみなさずに次の見回りを待つ
        return "busy", "取得", m
    acq, dp = m.get("acquisition") or {}, m.get("deepen") or {}
    what = "切り直した界隈の取り足し" if dp.get("kind") == "recut" else "掘り下げの取り足し"
    if acq.get("status") == "cancelled":
        return "cancelled", "取得", m
    if acq.get("status") in BUSY:
        return "busy", "取得", m
    if acq.get("status") in ("failed", "blocked"):
        return "failed", "取得（止まったところから取り直し中）", m
    if dp.get("status") in BUSY:
        return "busy", what, m
    return "done", what if dp.get("status") in ("done", "failed") else "取得", m


def hold_awake() -> None:
    """待っている間 Mac を眠らせない（caffeinate -i -w <自分の PID>。待つ命令が終わると caffeinate も終わる）。
    取得が終わるとアプリは数秒で眠りを戻すので、次の見回りまでに眠ると、AI が書き始めるのが朝 Mac を起こしてからになる"""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def keep_awake(seconds: int = AWAKE_S) -> None:
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-t", str(int(seconds))], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _failed_line(d: Path, title: str, m: dict) -> str:
    full = str((m.get("acquisition") or {}).get("error") or "")
    if "日本の地域では使えません" in full:   # 再開しても同じ所で止まる（acquire/pipeline.py の UNAVAILABLE_STOP）
        return (f"止まりました: 「{title}」の取得は、{full}（分析 ID {d.name}）。"
                "利用者にこのことを伝え、ほかの楽曲ページを探して（start_analysis の返事の探し方のとおり）やり直してください")
    err = full[:80]
    return (f"止まりました: 「{title}」の取得が止まり、UGC Analyzer はもう自動では取り直しません（{err + '。' if err else ''}分析 ID {d.name}）。"
            "利用者に「メニューバーの UGC Analyzer のメニューで『止まった取得を続きから再開』を押して"
            "（アプリが開いていなければ、先にアプリケーションフォルダの UGC Analyzer を開く）、そのあとここで『続けて』と言ってください。"
            "済んだところの続きから取ります」と伝えて止まってください")


def wait(ref: str | None, limit_h: float = LIMIT_H, poll_s: float = POLL_S, awake: bool = True) -> tuple[int, str]:
    d = find(ref)
    if d is None:
        return 1, f"「{ref or ''}」に当たる分析がありません。status で分析 ID を確かめてください"
    title = _meta(d).get("title") or d.name
    t0 = time.time()
    held = False
    failed_seen = None   # 止まった取得を最初に見た時刻（failed_at が読めないとき、ここから数える）
    others_at = 0.0      # ほかの分析の取得・取り足しが最後に動いていた時刻
    last, streak = None, 0
    while True:
        st, what, m = state(d)
        if st == "done":
            if awake:
                keep_awake()
            return 0, f"終わりました: 「{title}」の{what}（分析 ID {d.name}）。next_task から続けてください"
        if st == "cancelled":
            return 1, f"「{title}」の取得はやめてあります（分析 ID {d.name}）。利用者に伝えて止まってください"
        if st == "gone":
            return 1, f"止まりました: 「{title}」の分析（分析 ID {d.name}）が見つからなくなりました。利用者に伝えて止まってください"
        failed_seen = (failed_seen or time.time()) if st == "failed" else None
        stop = None
        if not _app_running():
            # アプリは起動すると、取り直してよい止まった取得（jobs.retriable）を30秒〜5分後に続きから取り直す（0.6.1〜）。
            # 取り直さない失敗だけ、メニューの再開を案内する
            stop = (1, _failed_line(d, title, m) if st == "failed" and not jobs.retriable(m) else
                    ("止まりました: この Mac の UGC Analyzer が動いていません。利用者に「アプリケーションフォルダの UGC Analyzer を開いてください。"
                     "済んだところの続きから取ります」と伝えて止まってください"))
        elif _login_wanted():
            stop = (2, ("ログイン待ち: UGC Analyzer が TikTok のログインを待っています。利用者に「UGC Analyzer が開いた Chrome で、"
                        "分析専用のサブアカウントでログインしてください」と伝えて止まってください"))
        elif st == "failed":
            # アプリが取り直すのは、取り直してよい失敗（jobs.retriable）を、アプリが動いている間に見たときだけ（30秒〜5分後に待ち行列へ）。
            # 取り直さない失敗・回数切れ・アプリを開き直す前からの失敗は、メニューの再開を待つ
            if _others_busy(d):
                others_at = time.time()
            # アプリを開き直した直後は、起動から数える（起動の30秒後に取り直す。0.6.1〜）
            since = max(_ts((m.get("acquisition") or {}).get("failed_at")) or failed_seen, others_at, _app_started_at())
            if not jobs.retriable(m) or time.time() - since > STALL_S:
                stop = (1, _failed_line(d, title, m))
        if stop is None:
            last, streak = None, 0
        else:
            streak = streak + 1 if stop[0] == last else 1
            last = stop[0]
            if streak >= STOP_POLLS:
                return stop
        if time.time() - t0 > limit_h * 3600:
            return 3, f"まだ終わっていません（{what}。待つ上限 {limit_h:g}時間）。もう一度 ./ugc-wait {d.name} を裏で走らせてください"
        if awake and not held:
            hold_awake()
            held = True
        time.sleep(poll_s)


def main(args: list) -> int:
    """引数は「分析 ID か曲名」と「上限の時間」。曲名を引用符なしで渡されたら（./ugc-wait Night Dancer）、
    最後が数のときだけそれを上限の時間とし、残りをまとめて曲名にする"""
    limit_h = LIMIT_H
    if len(args) > 1:
        try:
            n = float(args[-1])
        except ValueError:
            n = None
        if n is not None and math.isfinite(n) and n > 0:   # 「Infinity」「nan」のような曲名の語は数とみなさない
            limit_h, args = n, args[:-1]
    code, line = wait(" ".join(args) or None, limit_h)
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
