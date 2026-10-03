"""Mac との付き合い: ログイン時の自動起動・スリープと復帰の知らせ・二重起動の防止・置き場所の確かめ。"""
import os
import plistlib
import sys
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


# --- 二重起動の防止 ---
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)   # Mac では生存確認だけ（Windows と違い、プロセスを止めない）
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claim_single_instance() -> bool:
    try:
        other = int(config.PID_FILE.read_text().strip())
        if other != os.getpid() and _alive(other):
            return False
    except (FileNotFoundError, ValueError):
        pass
    config.PID_FILE.write_text(str(os.getpid()))
    return True


def release_single_instance() -> None:
    try:
        if int(config.PID_FILE.read_text().strip()) == os.getpid():
            config.PID_FILE.unlink()
    except Exception:
        pass


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
