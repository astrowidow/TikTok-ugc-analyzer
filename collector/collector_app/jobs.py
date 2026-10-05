"""仕事（分析1つぶんの取得）の作り方と、取得の係の起動・停止・再開。

分析を作るのは2か所。どちらもこの Mac の分析フォルダ（config.ANALYSES_DIR）に作り、係が順に拾う:
  - Claude の道具「分析を始める」（mcp_local.py → proto_runner.start_analysis）。ふだんはこれ
  - LocalJobs: メニューの運営向けの「曲を取得する…」「ちょいとり…」

取得の係は本線の acquire.worker をそのまま子プロセスで動かす（待ち行列を順に片付け、空になったら終わる。
途中で止まったもの＝status が running なのに PID が死んでいるもの、は次に起こしたとき続きから）。
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config


def _python_cmd() -> list:
    """取得の部品を動かす「python」。固めたアプリでは自分自身（__main__ が -m・.py を受ける）"""
    return [sys.executable]


class LocalJobs:
    """メニューの運営向けの項目から、この Mac の中で分析を作る"""
    name = "local"

    def create(self, song: str, artist: str = "", music_url: str = "", trial: bool = False) -> str:
        from acquire import launch
        settings = {"chrome_port": str(config.CHROME_PORT)}
        if trial:   # ちょいとり（約5分）。中身は config.TRIAL_SETTINGS
            settings.update(config.TRIAL_SETTINGS)
        if music_url and not song:   # URL だけで頼まれたら、URL の曲名の部分を題名にする（通知が URL だらけにならないよう）
            slug = music_url.rstrip("/").rsplit("/", 1)[-1]
            song = re.sub(r"-\d+$", "", slug).replace("-", " ").strip() or music_url
        return launch.new_analysis(config.OWNER, song, artist, music_url, settings)


class Worker:
    """取得の係（子プロセス）。スリープの前に止め、起きたら続きから起こす"""

    def __init__(self, code_dir: Path, log):
        self.code_dir = code_dir
        self.log = log
        self.proc = None
        self.adopted = None
        self.caffeinate = None
        self.keep_display_on = False

    def _orphan_pid(self):
        """前のアプリ（強制終了された等）が起こした係がまだ動いていれば、その PID"""
        try:
            from acquire import worker
            pid = worker.worker_pid()
        except Exception:
            return None
        if pid and (not self.proc or pid != self.proc.pid):
            return pid
        return None

    def running(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        pid = self._orphan_pid()
        if pid:   # 引き取る（起こし直すと「別の係が動いている」で即終了を繰り返すため）
            self.adopted = pid
            if not self.caffeinate or self.caffeinate.poll() is not None:
                self._caffeinate(pid)
            return True
        self.adopted = None
        return False

    def _caffeinate(self, pid: int) -> None:
        # 係が生きている間はスリープさせない（-i: アイドルで寝ない。-d: 画面も消さない＝設定で選ぶ）
        flags = "-di" if self.keep_display_on else "-i"
        self.caffeinate = subprocess.Popen(["/usr/bin/caffeinate", flags, "-w", str(pid)],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)

    def start(self) -> None:
        if self.running():
            return
        env = {**os.environ, "PYTHONUTF8": "1"}
        if not config.FROZEN:   # ソースから: collector_app を読めるように
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(config.REPO_ROOT / "collector"),
                                                              env.get("PYTHONPATH", "")]))
        out = open(config.LOG_DIR / "worker.out", "a", encoding="utf-8")
        self.proc = subprocess.Popen([*_python_cmd(), "-m", "collector_app.worker_entry"], cwd=str(self.code_dir), env=env,
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.log.info("取得の係を起こしました（PID %s）", self.proc.pid)
        self._caffeinate(self.proc.pid)

    def pid(self):
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        return getattr(self, "adopted", None)

    def stop(self, reason: str = "") -> None:
        """係と、係が起こした chromedriver・ヘッドレスの Chrome をまとめて止める（取得用の Chrome 本体は別）。
        係は自分のセッションの先頭（start_new_session）なので、プロセスグループごと止められる。
        止めた分析は status=running・PID が死んだ状態で残るので、次に起こすと続きから"""
        if not self.running():
            return
        pid = self.pid()
        self.log.info("取得の係を止めます（%s、PID %s）", reason, pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        if not wait_until(lambda: not _alive(pid), 15):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self.proc:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.adopted = None
        if self.caffeinate and self.caffeinate.poll() is None:
            self.caffeinate.terminate()

    def restart_display_setting(self, keep_on: bool) -> None:
        self.keep_display_on = keep_on
        if self.running():   # caffeinate だけ掛け直す（係は止めない）
            if self.caffeinate and self.caffeinate.poll() is None:
                self.caffeinate.terminate()
            self._caffeinate(self.pid())


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)   # Mac では生存確認だけ（Windows と違い、止めない）
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # 終わったがまだ回収されていない子（ゾンビ）は死んだものとみなす
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 状態の読み取り（analysis.json と取得の記録から）
# ---------------------------------------------------------------------------
def _hm(sec) -> str:
    sec = int(sec or 0)
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}時間{m}分" if h else f"{max(m, 1)}分"


def analyses() -> list:
    from acquire import pipeline
    out = []
    if not pipeline.ANALYSES_DIR.exists():
        return out
    for d in pipeline.ANALYSES_DIR.iterdir():
        m = pipeline.read_json(d / "analysis.json")
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("created_at") or "")
    return out


def queued_ids() -> list:
    from acquire import worker
    return worker.queue()


def describe(m: dict) -> str:
    """メニューの状態の1行"""
    from acquire import launch, pipeline
    acq = m.get("acquisition") or {}
    title = m.get("title") or m.get("analysis_id")
    st = acq.get("status")
    if st == "done":
        c = ((acq.get("steps") or {}).get("comments") or {}).get("detail") or {}
        return f"取得済み: {title}（コメント {c.get('videos_ok', '?')}本・{c.get('comments', '?')}件）"
    if st == "failed":
        return f"止まっています: {title} — {acq.get('error')}"
    if st == "queued":
        return f"順番待ち: {title}"
    step = acq.get("step")
    label = pipeline.STEP_LABELS.get(step, "準備") if step else "準備"
    extra = ""
    if step == "comments":
        d = config.ANALYSES_DIR / m["analysis_id"]
        ok = pipeline.comments_ok(d)   # 楽曲ページごとの要約を合わせる（要約がまだ無ければ原本の行数）
        n_pool = (((acq.get("steps") or {}).get("pool") or {}).get("detail") or {}).get("n_pool")
        extra = f" {ok}/{n_pool or '?'}本"
    try:
        left = launch._remaining_seconds(m)   # noqa: SLF001 本線と同じ見込みの出し方
        extra += f"・残り約{_hm(left)}"
    except Exception:
        pass
    return f"取得中: {title} — {label}{extra}"


def deepening(m: dict) -> bool:
    """完成後の界隈の掘り下げの取り足しが、順番待ち・取得中か（acquire/deepen.py）"""
    return ((m.get("deepen") or {}).get("status")) in ("queued", "running")


def describe_deepen(m: dict) -> str:
    dp = m.get("deepen") or {}
    title = m.get("title") or m.get("analysis_id")
    recut = dp.get("kind") == "recut"   # 界隈の切り直しの取り足し（acquire/recut.py）
    if dp.get("status") == "queued":
        return f"順番待ち: {title}（{'界隈の切り直し' if recut else '界隈の掘り下げ'}）"
    d = config.ANALYSES_DIR / m["analysis_id"] / "raw" / "deepen" / f"r{dp.get('round')}.jsonl"
    done = sum(1 for _ in open(d, encoding="utf-8")) if d.exists() else 0
    return f"取り足し中: {title}（{'切り直した界隈' if recut else '界隈 ' + str(dp.get('community'))}）{done}/{len(dp.get('targets') or [])}本"


MAX_RETRIES = 3


def retriable(m: dict) -> bool:
    """自動で取り直してよいか（見つからない・URL が違う、のように取り直しても同じものと、回数切れは除く）"""
    err = (m.get("acquisition") or {}).get("error") or ""
    if any(k in err for k in ("見つかりませんでした", "URL を教えて", "1本も見つかりませんでした")):
        return False
    return int((m.get("collector") or {}).get("retries") or 0) < MAX_RETRIES


def requeue(aid: str, manual: bool = False) -> None:
    """止まった分析を待ち行列に戻す（済んだ工程は飛ばし、続きから）。manual はメニューから＝回数を数え直す"""
    from acquire import pipeline
    p = config.ANALYSES_DIR / aid / "analysis.json"
    cur = pipeline.read_json(p, {})
    c = cur.setdefault("collector", {})
    c["retries"] = 0 if manual else int(c.get("retries") or 0) + 1
    cur["acquisition"].update({"status": "queued", "requeued_at": pipeline.now()})
    pipeline.write_json(p, cur)


def failed_ids() -> list:
    return [m["analysis_id"] for m in analyses() if (m.get("acquisition") or {}).get("status") == "failed"]


def wait_until(pred, timeout: float, step: float = 0.5) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False
