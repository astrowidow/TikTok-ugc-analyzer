"""Mac との付き合い: ログイン時の自動起動・スリープと復帰の知らせ・二重起動の防止・置き場所の確かめ。"""
import fcntl
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import config

AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{config.APP_ID}.plist"


# --- ログイン時に起動（LaunchAgent。署名なしのアプリでも使える） ---
def app_executable():
    return Path(sys.executable) if config.FROZEN else None


def login_item_enabled() -> bool:
    return AGENT_PLIST.exists()


def set_login_item(enabled: bool) -> None:
    exe = app_executable()
    if not enabled or exe is None:
        AGENT_PLIST.unlink(missing_ok=True)
        return
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": config.APP_ID, "ProgramArguments": [str(exe)], "RunAtLoad": True,
            "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}
    AGENT_PLIST.write_bytes(plistlib.dumps(data))


def refresh_login_item() -> None:
    """アプリを動かした場所が変わっていたら書き直す（アプリケーションフォルダへ移したときなど）"""
    if login_item_enabled() and app_executable() and not is_translocated():
        set_login_item(True)


def is_translocated() -> bool:
    """ダウンロードしたまま開くと、Mac が読み取り専用の仮の場所で動かす（App Translocation）"""
    return "/AppTranslocation/" in sys.executable


def in_applications() -> bool:
    return sys.executable.startswith("/Applications/") or sys.executable.startswith(str(Path.home() / "Applications"))


# --- 二重起動の防止・メニューバーのアプリが動いているか ---
# app.pid は「終了」メニュー以外（Mac の再起動・入れ替え・落ちた）では消えない。残った番号は別のプロセスに使い回されるので、
# 番号が生きているかだけでは見ない（2026-10-06 通し試験 R6-2）。
#   1. アプリは起動から終わるまで app.pid に flock を掛けて持つ（落ちても Mac の再起動でも、錠は OS が外す）
#   2. 錠が持たれていなければ、番号のプロセスが UGC Analyzer のメニューバーのアプリそのものかを確かめる
#      （錠を持たない前の版のアプリが、.app を置き換えたあとも動いていることがあるため）
_lock_fd = None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)   # Mac では生存確認だけ（Windows と違い、プロセスを止めない）
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid():
    try:
        return int(config.PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _exe_of(pid: int):
    """そのプロセスの実行ファイルの場所（libproc の proc_pidpath。読めなければ None）"""
    try:
        import ctypes
        buf = ctypes.create_string_buffer(4096)
        n = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath(int(pid), buf, 4096)
        return buf.value.decode("utf-8", "replace") if n > 0 else None
    except Exception:
        return None


def is_menu_app(pid) -> bool:
    """その PID が UGC Analyzer のメニューバーのアプリか。このプロセスと同じ実行ファイルで、
    AI の道具（--mcp）・待つ命令（--wait）・取得の係（-m …）・部品のスクリプト（….py）ではないもの"""
    if not pid or not _alive(pid):
        return False
    try:
        cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except Exception:
        return False
    if config.FROZEN:
        # 起動したときの名前（ps の command）で見る。実行ファイルの場所は、.app を移すと移った先に変わる
        # （入れ替えで控えに移した前の版のプロセスは、場所が output/app-backup/… になるが、名前は /Applications/… のまま）。
        # メニューバーのアプリは引数なしで動く（ログイン時の起動・open で開く）
        exe = str(sys.executable)
        if not cmd.startswith(exe):
            return False
        return not [a for a in cmd[len(exe):].split() if not a.startswith("-psn_")]   # -psn_ は古い macOS が付ける印
    exe = _exe_of(pid)   # ソースから: 同じ python か（venv の python は、本物の python に置き換わって動く）
    if not exe or exe != _exe_of(os.getpid()):
        return False
    rest = cmd[len(exe):].split() if cmd.startswith(exe) else cmd.split()[1:]
    if rest[:1] and rest[0] in ("--mcp", "--wait", "--selftest", "--connect-claude") or \
            any("worker_entry" in a for a in rest):
        return False
    return True   # python -m collector_app。試験でも、ここまで来れば同じ python のアプリとみなす


def _lock_held() -> bool:
    """app.pid の錠を、動いているアプリが持っているか（一瞬だけ共有の錠を試す）"""
    try:
        fd = os.open(str(config.PID_FILE), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def app_running() -> bool:
    """メニューバーのアプリが動いているか。AI の道具（mcp_local.app_running）・待つ命令（waiter）からも、これを使う"""
    pid = _read_pid()
    if not pid:
        return False
    if _lock_held():
        return True
    return is_menu_app(pid)


def claim_single_instance() -> bool:
    """このアプリを1つだけにする。取れたら app.pid に自分の番号を書き、終わるまで錠を持つ"""
    global _lock_fd
    if _lock_fd is not None:
        return True
    config.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        fd = os.open(str(config.PID_FILE), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)   # 動いているアプリが持っている（か、道具が「動いているか」を一瞬だけ見ている）
            time.sleep(0.2)
            continue
        try:
            same = os.fstat(fd).st_ino == os.stat(config.PID_FILE).st_ino
        except FileNotFoundError:
            same = False
        if not same:   # 錠を取るあいだに、前のアプリが終わって app.pid を消した。作り直して取り直す
            os.close(fd)
            continue
        other = _read_pid()
        if other and other != os.getpid() and is_menu_app(other):   # 錠を持たない前の版のアプリが動いている
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        _lock_fd = fd
        return True
    return False


def release_single_instance() -> None:
    global _lock_fd
    try:
        if _read_pid() == os.getpid():
            config.PID_FILE.unlink()
    except Exception:
        pass
    if _lock_fd is not None:
        try:
            os.close(_lock_fd)   # 錠も外れる
        except OSError:
            pass
        _lock_fd = None


# --- スリープと復帰 ---
_observer = None


def watch_sleep(on_sleep, on_wake) -> None:
    """Mac が眠る直前・起きた直後に呼ぶ（ふたを閉じた・スリープを選んだとき）"""
    global _observer
    from AppKit import NSWorkspace, NSWorkspaceDidWakeNotification, NSWorkspaceWillSleepNotification
    from Foundation import NSObject

    class _Obs(NSObject):
        def willSleep_(self, _n):
            on_sleep()

        def didWake_(self, _n):
            on_wake()

    _observer = _Obs.alloc().init()
    nc = NSWorkspace.sharedWorkspace().notificationCenter()
    nc.addObserver_selector_name_object_(_observer, "willSleep:", NSWorkspaceWillSleepNotification, None)
    nc.addObserver_selector_name_object_(_observer, "didWake:", NSWorkspaceDidWakeNotification, None)
