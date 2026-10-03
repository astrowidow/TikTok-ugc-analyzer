"""取得専用の Chrome（利用者の Mac の Google Chrome を、専用のプロファイルとポートで起動する）。

- 利用者がふだん使う Chrome とは別のプロファイルなので、本垢のログインや閲覧履歴とは混ざらない
- 捨て垢のログインは利用者が自分でする（このウィンドウの TikTok のログイン画面で）。ID・パスはどこにも送らない
- 取得の間は最小化して Dock にしまう。描画は取得の部品（spatest）が「フォーカスの模擬」で回し続ける
  （2026-10-01 の試験: 最小化でも前面と同じフレーム数・タイマー・スクロールの検知。docs/STAGE4_LOG.md）
- ヘッドレスにはしない・自動化の印を偽装しない（COMMENT_ACQUISITION_HANDOVER 第4章・7-B）

Chrome との話は DevTools の口（127.0.0.1 のポート）だけ。TikTok に何かを送るのは取得の部品で、ここではしない。
"""
import json
import socket
import subprocess
import time
from pathlib import Path

import requests
import websocket

CHROME_CANDIDATES = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
]
LOGIN_URL = "https://www.tiktok.com/login"
HOME_URL = "https://www.tiktok.com/"

RENDER_JS = """new Promise(res => { let f = 0; const t0 = performance.now();
  const tick = () => { f++; if (performance.now() - t0 < 800) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  setTimeout(() => res(JSON.stringify({vis: document.visibilityState, frames: f, url: location.href})), 1500); })"""


class ChromeError(Exception):
    pass


def find_binary():
    for p in CHROME_CANDIDATES:
        if p.exists():
            return p
    return None


class Chrome:
    def __init__(self, port: int, profile: Path, log):
        self.port = port
        self.profile = profile
        self.log = log
        self.proc = None

    # --- 起動・停止 ---
    def listening(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def ensure(self, url: str = HOME_URL, minimized: bool = True, timeout: float = 30) -> None:
        """待ち受けていなければ起動する。minimized なら起動後すぐ Dock にしまう"""
        if self.listening():
            return
        exe = find_binary()
        if not exe:
            raise ChromeError("Google Chrome が見つかりません。先に Google Chrome を入れてください")
        self.profile.mkdir(parents=True, exist_ok=True)
        self.log.info("取得用の Chrome を起動します（ポート %s）", self.port)
        self.proc = subprocess.Popen(
            [str(exe), f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}",
             "--no-first-run", "--no-default-browser-check", "--mute-audio", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.listening() and self._page_target():
                break
            time.sleep(0.5)
        else:
            raise ChromeError("取得用の Chrome が起動しませんでした")
        if minimized:
            time.sleep(1.0)
            self.minimize()

    def quit(self) -> None:
        if not self.listening():
            return
        try:
            self._browser_cmd("Browser.close")
        except Exception:
            pass
        t0 = time.time()
        while self.listening() and time.time() - t0 < 10:
            time.sleep(0.3)
        if self.listening() and self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def pid(self):
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        try:  # 前のアプリが起動した Chrome（アプリを再起動したとき）
            out = subprocess.run(["pgrep", "-f", f"remote-debugging-port={self.port}"], capture_output=True,
                                 text=True).stdout.split()
            for p in out:
                cmd = subprocess.run(["ps", "-o", "command=", "-p", p], capture_output=True, text=True).stdout
                if "--type=" not in cmd and "Google Chrome" in cmd:
                    return int(p)
        except Exception:
            pass
        return None

    # --- DevTools の口 ---
    def _json(self, path: str):
        return requests.get(f"http://127.0.0.1:{self.port}{path}", timeout=5).json()

    def _page_target(self):
        try:
            pages = [t for t in self._json("/json/list") if t.get("type") == "page"]
        except Exception:
            return None
        return pages[0] if pages else None

    def _send(self, ws_url: str, method: str, params: dict | None = None, timeout: float = 10):
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise ChromeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result") or {}
        finally:
            ws.close()

    def _browser_cmd(self, method: str, params: dict | None = None, timeout: float = 10):
        return self._send(self._json("/json/version")["webSocketDebuggerUrl"], method, params, timeout)

    def _page_cmd(self, method: str, params: dict | None = None, timeout: float = 10):
        t = self._page_target()
        if not t:
            raise ChromeError("Chrome のタブが見つかりません")
        return self._send(t["webSocketDebuggerUrl"], method, params, timeout)

    # --- ウィンドウ ---
    def _window_id(self) -> int:
        t = self._page_target()
        if not t:
            raise ChromeError("Chrome のタブが見つかりません")
        return self._browser_cmd("Browser.getWindowForTarget", {"targetId": t["id"]})["windowId"]

    def window_state(self) -> str:
        try:
            wid = self._window_id()
            return self._browser_cmd("Browser.getWindowBounds", {"windowId": wid})["bounds"].get("windowState", "?")
        except Exception:
            return "?"

    def minimize(self) -> None:
        wid = self._window_id()
        self._browser_cmd("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})

    def show(self) -> None:
        wid = self._window_id()
        self._browser_cmd("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        try:
            self._page_cmd("Page.bringToFront")
        except Exception:
            pass
        activate_pid(self.pid())

    def navigate(self, url: str) -> None:
        self._page_cmd("Page.navigate", {"url": url})

    # --- ログインと描画 ---
    def logged_in(self) -> bool:
        """TikTok のセッションの Cookie があるか（中身は読まない・どこにも送らない）"""
        cookies = self._browser_cmd("Storage.getCookies").get("cookies") or []
        return any(c.get("name") == "sessionid" and "tiktok.com" in (c.get("domain") or "") and c.get("value")
                   for c in cookies)

    def render_check(self) -> dict:
        """取得中のタブで描画が回っているか（1.5秒だけ JS を走らせる。TikTok には何も送らない）"""
        r = self._page_cmd("Runtime.evaluate", {"expression": RENDER_JS, "awaitPromise": True, "returnByValue": True},
                           timeout=15)
        return json.loads(((r.get("result") or {}).get("value")) or "{}")


def activate_pid(pid) -> None:
    """そのプロセスのウィンドウを前に出す（アクセシビリティの許可は要らない）"""
    if not pid:
        return
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass
