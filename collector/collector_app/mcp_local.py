"""Claude に出す道具（MCP、この Mac の中で動く）。docs/ALL_IN_APP_PLAN.md 段1。

Claude デスクトップが、設定ファイル（claude_link.py が書く）にしたがって次のように起動し、標準入出力で話す:

  /Applications/UGC Collector.app/Contents/MacOS/UGC Collector --mcp

道具の中身は本線と同じ（mcp_proto.py の道具・proto_runner.py の仕事の列・flow_w1.py の W1）。違いは3つだけ:
  - 利用者はこの Mac の1人で固定（秘密の URL は要らない）
  - 取得は Windows 機の係でなく、メニューバーの取得アプリが拾う（LocalHooks）
  - 「指示書を見る・直す」「知識ベースを新しくする」の道具を足す
"""
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import config, system, worker_entry


class FileRLock:
    """プロセスをまたぐ再入可能なロック。Claude が道具のプロセスを2つ起こしても（チャットと Cowork など）、
    仕事の状態（tasks.json）を同時に書き換えないようにする"""

    def __init__(self, path: Path):
        self.path = path
        self._t = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._t.acquire()
        if self._depth == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._t.release()
        return False


class LocalHooks:
    """proto_runner.LOCAL に差し込むもの（取得アプリの形）"""

    def acquisition_settings(self) -> dict:
        s = {"chrome_port": str(config.CHROME_PORT)}
        if config.load_state().get("ai_trial"):   # 運営の試し用（メニューの運営向け）
            s.update(config.TRIAL_SETTINGS)
        return s

    def app_state(self) -> dict:
        st = config.load_state()
        return {"running": app_running(),
                "login_wanted": worker_entry.need_login_flag().exists() or not st.get("logged_in_at")}

    def ensure_app(self) -> str:
        """メニューバーの取得アプリが動いていなければ起こす（仕事は、取得アプリが5秒おきに見て拾う）"""
        if app_running():
            return "running"
        if os.environ.get("UGC_COLLECTOR_NO_APP_LAUNCH"):   # 試験用: メニューバーのアプリを起こさない（TikTok に触らない）
            return "disabled"
        try:
            if config.FROZEN:
                bundle = Path(sys.executable).resolve().parents[2]   # …/UGC Collector.app
                subprocess.Popen(["/usr/bin/open", "-g", str(bundle)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                env = {**os.environ, "PYTHONPATH": str(config.REPO_ROOT / "collector")}
                subprocess.Popen([sys.executable, "-m", "collector_app"], cwd=str(config.REPO_ROOT), env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            return "started"
        except Exception as e:
            return f"failed: {e}"


def app_running() -> bool:
    try:
        pid = int(config.PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    return system._alive(pid)   # noqa: SLF001


def build():
    """道具をそろえたサーバー（試験でも使う）"""
    import mcp_proto
    import proto_runner
    proto_runner.LOCAL = LocalHooks()
    proto_runner._lock = FileRLock(config.LOCK_DIR / "runner.lock")   # noqa: SLF001
    return mcp_proto._build_server(user_of=lambda ctx: config.OWNER, local=True)   # noqa: SLF001


def main() -> int:
    config.setup_env()
    import logging
    from logging.handlers import RotatingFileHandler
    # 記録は全部 logs/mcp.log へ（mcp_proto・SDK の記録も。標準出力は Claude との通話に使う）。
    # ハンドラは根元にだけ付ける（名前つきのロガーにも付けると、伝わって2行ずつ出る）
    root = logging.getLogger()
    if not root.handlers:
        h = RotatingFileHandler(config.LOG_DIR / "mcp.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(process)d] %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    log = logging.getLogger("collector-mcp")
    config.seed_data(log)
    log.info("=== Claude の道具を開きます（%s %s、データ %s）", config.APP_NAME, __import__("collector_app").VERSION,
             config.DATA_DIR)
    try:
        server = build()
        server.run("stdio")
    except Exception:
        log.exception("Claude の道具が止まりました")
        raise
    finally:
        log.info("=== Claude の道具を閉じました")
    return 0
