"""領域 R6（メニューバーのアプリの殻・AI へのつなぎ・作り方と入れ替え・知識ベース）の見直しの試験（2026-10-06、友達に配る前の通し試験）。

  collector/.venv/bin/python -m unittest tests.test_review_r6 -v

- どの試験も「正しい動き」を期待する形で書いてある。失敗する試験は、不具合の疑いがあるところ（試験の名前と説明に起き方を書いた）
- 本物の置き場（~/Library/Application Support/UGC Analyzer）・Claude と Codex の設定・ログイン時の起動の設定・TikTok・Chrome・ネットには触らない。
  置き場と設定はすべて一時フォルダ、Chrome との話（DevTools の口）は差し替え、通知は記録するだけ
"""
import ast
import datetime
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import types
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TMP = None
_SAVED_ENV = {}
_SAVED_ATTRS = []
NOTES = []   # 送ろうとした通知 (題, 本文)
_ENV_KEYS = ("UGC_COLLECTOR_HOME", "UGC_CLAUDE_CONFIG", "UGC_CODEX_HOME", "UGC_CODEX_SKILLS", "UGC_CODE_FOLDER",
             "UGC_COLLECTOR_PORT", "UGC_COLLECTOR_TEST_NO_CHROME", "UGC_ANALYSES_DIR", "UGC_LOCK_DIR", "UGC_PROMPTS_DIR",
             "UGC_KB_DIR", "UGC_USERS_DIR", "UGC_REPORTS_DIR", "UGC_LOCAL_APP", "PYTHONUTF8", "UGC_COLLECTOR_NO_APP_LAUNCH")


def _set_attr(obj, name, value):
    _SAVED_ATTRS.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="ugc-review-r6-")
    for k in _ENV_KEYS:
        _SAVED_ENV[k] = os.environ.get(k)
    os.environ.pop("UGC_COLLECTOR_TEST_NO_CHROME", None)
    os.environ.update({
        "UGC_COLLECTOR_HOME": f"{_TMP}/home",
        "UGC_CLAUDE_CONFIG": f"{_TMP}/claude/claude_desktop_config.json",
        "UGC_CODEX_HOME": f"{_TMP}/codex",
        "UGC_CODEX_SKILLS": f"{_TMP}/skills",
        "UGC_CODE_FOLDER": f"{_TMP}/codefolder",
        "UGC_COLLECTOR_PORT": "59998",          # 本物の取得用の Chrome（9250・9251）とは別
        "UGC_COLLECTOR_NO_APP_LAUNCH": "1",
    })
    from collector_app import config
    importlib.reload(config)
    config.setup_env()
    from collector_app import claude_link, code_link, codex_link, system
    for m in (claude_link, codex_link, code_link, system):
        importlib.reload(m)
    _set_attr(system, "AGENT_PLIST", Path(_TMP) / "LaunchAgents" / "agent.plist")   # 本物のログイン時の起動には触らない
    import tiktok_lock
    from acquire import pipeline, worker
    _set_attr(pipeline, "ANALYSES_DIR", config.ANALYSES_DIR)
    _set_attr(tiktok_lock, "LOCK_DIR", config.LOCK_DIR)
    _set_attr(tiktok_lock, "LOCK_FILE", config.LOCK_DIR / "tiktok.lock")
    _set_attr(worker, "GUARD", config.LOCK_DIR / "acq_worker.json")
    from collector_app import app, chrome, notify
    _set_attr(notify, "send", lambda title, body: NOTES.append((title, body)))
    _set_attr(notify, "setup", lambda log: None)
    _set_attr(chrome, "find_binary", lambda: Path("/fake/Google Chrome"))
    _set_attr(chrome, "activate_pid", lambda pid: None)
    _set_attr(app, "TEST_NO_CHROME", False)


def tearDownModule():
    for obj, name, value in reversed(_SAVED_ATTRS):
        setattr(obj, name, value)
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    from collector_app import claude_link, code_link, codex_link, config, system
    for m in (config, claude_link, codex_link, code_link, system):
        importlib.reload(m)
    shutil.rmtree(_TMP, ignore_errors=True)


def _cfg():
    from collector_app import config
    return config


def _clean_data():
    config = _cfg()
    for d in (config.ANALYSES_DIR, config.LOCK_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    config.STATE_FILE.unlink(missing_ok=True)
    config.PID_FILE.unlink(missing_ok=True)
    NOTES.clear()


def _put_analysis(aid: str, **meta) -> Path:
    config = _cfg()
    d = config.ANALYSES_DIR / aid
    d.mkdir(parents=True, exist_ok=True)
    m = {"analysis_id": aid, "title": meta.pop("title", "試験の曲"), "created_at": "2026-10-06T10:00:00", **meta}
    (d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return d


class StubChrome:
    """取得用の Chrome の代わり（DevTools の口に触らない）"""

    def __init__(self, listening=False, logged=False):
        self._listening, self.logged, self.calls = listening, logged, []

    def listening(self):
        return self._listening

    def ensure(self, **k):
        self.calls.append(("ensure", k))
        self._listening = True

    def logged_in(self):
        return self.logged

    def navigate(self, url):
        self.calls.append(("navigate", url))

    def show(self):
        self.calls.append("show")

    def minimize(self):
        self.calls.append("minimize")

    def window_state(self):
        return "minimized"

    def quit(self):
        self.calls.append("quit")


def _controller(test):
    """見張り役（裏のスレッドは回さない。係は起こさない）"""
    from collector_app import app
    ctl = app.Controller(_cfg().code_dir(), logging.getLogger("review-r6"))
    ctl.chrome = StubChrome()
    test.started = []
    ctl.worker.running = lambda: False
    ctl.worker.start = lambda: test.started.append(1)
    ctl.stopped = True   # start() が起こす見張りのスレッドはすぐ終わる
    return ctl


# ---------------------------------------------------------------------------
# 取得用の Chrome との話（DevTools の口）の差し替え
# ---------------------------------------------------------------------------
class FakeDevtools:
    """DevTools の口の代わり。pages は今開いているタブ（type=page）。窓を全部閉じた Chrome は pages が空"""

    def __init__(self, pages=(), cookies=()):
        self.pages = []
        self.cookies = list(cookies)
        self.calls = []
        self.n = 0
        for url in pages:
            self.new_page(url)

    def new_page(self, url):
        self.n += 1
        t = {"id": f"T{self.n}", "type": "page", "url": url, "webSocketDebuggerUrl": f"ws://fake/page/T{self.n}"}
        self.pages.append(t)
        return t

    @staticmethod
    def _resp(data):
        return types.SimpleNamespace(json=lambda: data, status_code=200, raise_for_status=lambda: None,
                                     text=json.dumps(data))

    def _http(self, url, **kw):
        u = urllib.parse.urlsplit(url)
        self.calls.append(("http", u.path, u.query))
        if u.path == "/json/list" or u.path == "/json":
            return self._resp([dict(p) for p in self.pages])
        if u.path == "/json/version":
            return self._resp({"webSocketDebuggerUrl": "ws://fake/browser"})
        if u.path == "/json/new":
            return self._resp(self.new_page(urllib.parse.unquote(u.query) or "about:blank"))
        raise AssertionError(f"知らない口: {url}")

    get = put = post = _http

    def create_connection(self, ws_url, **kw):
        return _FakeWS(self, ws_url)

    def handle(self, ws_url, method, params):
        self.calls.append((ws_url, method, params))
        if ws_url == "ws://fake/browser":
            if method == "Target.createTarget":
                return {"targetId": self.new_page(params.get("url") or "about:blank")["id"]}
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": p["id"], "type": "page", "url": p["url"]} for p in self.pages]}
            if method == "Browser.getWindowForTarget":
                if any(p["id"] == params.get("targetId") for p in self.pages):
                    return {"windowId": 1}
                return {"__error": "No target with given id found"}
            if method == "Browser.getWindowBounds":
                return {"bounds": {"windowState": "normal"}}
            if method == "Storage.getCookies":
                return {"cookies": list(self.cookies)}
            return {}
        page = next((p for p in self.pages if ws_url.endswith("/" + p["id"])), None)
        if page is None:
            return {"__error": "target closed"}
        if method == "Page.navigate":
            page["url"] = params["url"]
        return {}


class _FakeWS:
    def __init__(self, dt, url):
        self.dt, self.url, self.q = dt, url, []

    def send(self, s):
        msg = json.loads(s)
        r = self.dt.handle(self.url, msg["method"], msg.get("params") or {})
        if "__error" in r:
            self.q.append(json.dumps({"id": msg["id"], "error": {"message": r["__error"]}}))
        else:
            self.q.append(json.dumps({"id": msg["id"], "result": r}))

    def recv(self):
        return self.q.pop(0)

    def close(self):
        pass


class _NoLaunch:
    """chrome モジュールの subprocess の代わり（本物の Chrome を起こそうとしたら失敗にする）"""

    @staticmethod
    def Popen(*a, **k):
        raise AssertionError("試験の中で本物の Chrome を起こそうとした")

    @staticmethod
    def run(*a, **k):
        return types.SimpleNamespace(stdout="", returncode=1)

    DEVNULL = subprocess.DEVNULL


class TestChromeWindowClosed(unittest.TestCase):
    """取得用の Chrome の窓を赤い×で閉じたとき（Mac の Chrome は最後の窓を閉じても終わらず、DevTools の口は開いたまま・タブは0）"""

    def setUp(self):
        _clean_data()
        from collector_app import chrome
        self.chrome_mod = chrome
        self.ctl = _controller(self)
        self.ctl.chrome = chrome.Chrome(_cfg().CHROME_PORT, _cfg().PROFILE_DIR, logging.getLogger("review-r6"))
        self.ctl.chrome.listening = lambda: True
        self.ctl.chrome.pid = lambda: None

    def _patched(self, dt):
        return (mock.patch.object(self.chrome_mod, "requests", dt),
                mock.patch.object(self.chrome_mod, "websocket", types.SimpleNamespace(create_connection=dt.create_connection)),
                mock.patch.object(self.chrome_mod, "subprocess", _NoLaunch))

    def _run(self, dt, fn):
        a, b, c = self._patched(dt)
        with a, b, c:
            fn()

    def test_request_login_with_tab(self):
        """（通る想定）タブがあれば、メニュー「TikTok にログイン」はそのタブをログインの画面にして前に出す"""
        dt = FakeDevtools(pages=["https://www.tiktok.com/"])
        self._run(dt, self.ctl.request_login)
        self.assertEqual(dt.pages[0]["url"], self.chrome_mod.LOGIN_URL)
        self.assertTrue(self.ctl.login_wanted)

    def test_request_login_when_window_closed(self):
        """窓を閉じたあと、案内どおりメニュー「TikTok にログイン」を押す → ログインの画面が出るべき。
        今は navigate が「Chrome のタブが見つかりません」で落ち（裏のスレッドなので何も出ない）、ログインの画面が出ない"""
        dt = FakeDevtools(pages=[])
        try:
            self._run(dt, self.ctl.request_login)
        except Exception as e:
            self.fail(f"「TikTok にログイン」が例外で止まった（画面に何も出ない）: {type(e).__name__}: {e}")
        self.assertTrue(any(p["url"] == self.chrome_mod.LOGIN_URL for p in dt.pages),
                        "ログインの画面（タブ）が開かれていない")

    def test_login_flow_after_restart_when_window_closed(self):
        """窓を閉じた Chrome が残ったままアプリを開き直した（Mac の再起動を除く）→ ログインの画面を出し直すべき。
        今は毎回 navigate が落ち、メニューの1行目が「エラー: Chrome のタブが見つかりません」のまま進まない"""
        dt = FakeDevtools(pages=[], cookies=[])
        self.ctl.login_presented = False
        try:
            self._run(dt, self.ctl._login_flow)
        except Exception as e:
            self.fail(f"ログイン待ちの処理が例外で止まった: {type(e).__name__}: {e}")
        self.assertTrue(any(p["url"] == self.chrome_mod.LOGIN_URL for p in dt.pages), "ログインの画面が出ていない")
        self.assertTrue(any(t == "TikTok にログインしてください" for t, _ in NOTES))

    def test_keep_chrome_when_window_closed(self):
        """コメントの段で窓を閉じた → 取得用の Chrome を開き直すべき（_keep_chrome、2026-10-02 ユーザー）。
        今は口が開いている（listening）だけを見るので何もしない。タブの無い Chrome には係（chromedriver）がつながらず、取得が止まる"""
        dt = FakeDevtools(pages=[])
        self._run(dt, self.ctl._keep_chrome)
        self.assertTrue(dt.pages, "タブの無い Chrome のまま（係がつながれない）")


# ---------------------------------------------------------------------------
# 二重起動の防止（app.pid）
# ---------------------------------------------------------------------------
class TestSingleInstance(unittest.TestCase):
    """app.pid は「終了」メニュー以外（Mac の再起動・入れ替え・落ちた）では消えない。その番号を別のプロセスが使っていたら"""

    def setUp(self):
        _clean_data()
        self.other = subprocess.Popen(["/bin/sleep", "30"])   # UGC Analyzer ではないプロセス（番号の使い回しの代わり）
        _cfg().PID_FILE.write_text(str(self.other.pid))

    def tearDown(self):
        self.other.kill()
        self.other.wait()

    def test_claim_ignores_reused_pid(self):
        """残った app.pid の番号が別のプロセスに使い回されている → 起動してよい。
        今は「生きている」とみなして「すでに起動しています」で終わる（再起動のあと自動で立ち上がらない）"""
        from collector_app import system
        self.assertTrue(system.claim_single_instance(),
                        "UGC Analyzer ではないプロセスの番号を見て、起動をやめた")

    def test_mcp_does_not_think_app_running(self):
        """同じ理由で、AI の道具（--mcp）は「メニューバーのアプリは動いている」と思い込み、起こさない（取得が始まらない）"""
        from collector_app import mcp_local
        self.assertFalse(mcp_local.app_running(), "UGC Analyzer ではないプロセスを、動いているアプリとみなした")


# ---------------------------------------------------------------------------
# 起動（run）: 初回のログイン時の起動
# ---------------------------------------------------------------------------
class TestFirstRun(unittest.TestCase):
    def setUp(self):
        _clean_data()

    def test_first_launch_outside_applications(self):
        """1回目を「アプリケーション」の外（.dmg の窓・ダウンロードのまま＝仮の場所）で開き、2回目にアプリケーションから開いた
        → 2回目に「ログイン時に起動」が入るべき（友達向けの紙「Mac を起動すると自動で立ち上がります」）。
        今は1回目で first_run_done が立つので、2回目に入らない"""
        from collector_app import app, claude_link, codex_link, config, notify, system
        calls = []

        class _Stop(Exception):
            pass

        def stop(*a, **k):
            raise _Stop()

        loc = {"translocated": True}
        with mock.patch.object(config, "FROZEN", True), \
                mock.patch.object(config, "setup_env", lambda: ROOT), \
                mock.patch.object(config, "seed_data", lambda log=None: {}), \
                mock.patch.object(config, "logger", lambda name="collector": logging.getLogger("review-r6-run")), \
                mock.patch.object(notify, "setup", lambda log: None), \
                mock.patch.object(system, "claim_single_instance", lambda: True), \
                mock.patch.object(system, "is_translocated", lambda: loc["translocated"]), \
                mock.patch.object(system, "in_applications", lambda: not loc["translocated"]), \
                mock.patch.object(system, "set_login_item", lambda on: calls.append(on)), \
                mock.patch.object(system, "refresh_login_item", lambda: None), \
                mock.patch.object(claude_link, "status", lambda: "none"), \
                mock.patch.object(codex_link, "status", lambda: "none"), \
                mock.patch.object(claude_link, "claude_installed", lambda: False), \
                mock.patch.object(app, "Controller", stop):
            with self.assertRaises(_Stop):
                app.run()   # 1回目: 仮の場所
            self.assertEqual(calls, [])
            loc["translocated"] = False
            with self.assertRaises(_Stop):
                app.run()   # 2回目: アプリケーションから
        self.assertIn(True, calls, "アプリケーションから初めて開いたのに「ログイン時に起動」が入らなかった")


# ---------------------------------------------------------------------------
# メニューの1行目と、止まった取得
# ---------------------------------------------------------------------------
class TestMenuLine(unittest.TestCase):
    def setUp(self):
        _clean_data()
        _cfg().save_state(logged_in_at="2026-10-06 10:00:00")

    def test_failed_shows_stopped(self):
        """自動の取り直しを使い切って止まった取得がある → 1行目は「止まっています: …」であるべき
        （友達向けの紙・始め方のページ「1行目が『止まっています』なら『止まった取得を続きから再開』」）。
        今は jobs.describe を取得中のものにしか使わず「準備OK・待機中…」と出る"""
        from collector_app import app
        _put_analysis("a20261006-1000-fail", title="止まった曲",
                      acquisition={"status": "failed", "error": "TikTok 側の制限（試験）"}, collector={"retries": 3})
        ctl = _controller(self)
        ctl.start()
        with mock.patch.object(app, "ai_where", lambda: "Claude"), mock.patch.object(app, "ai_installed", lambda: True):
            ctl._tick()
        self.assertIn("止まっています", ctl.status_text, f"1行目: {ctl.status_text}")

    def test_retry_survives_restart(self):
        """自動で取り直す前（5分待ち）にアプリが開き直された（Mac の再起動・終了・入れ替え）→ 開いたら取り直しを続けるべき
        （紙「途中で閉じてしまっても大丈夫。開けば続きから進みます」）。
        今は取り直しの予定がメモリ（pending_retry）にしか無く、起動時の状態を「前に見た状態」にするので、止まったまま残る"""
        _put_analysis("a20261006-1001-retry", title="取り直す曲",
                      acquisition={"status": "failed", "error": "TikTok 側でエラー（試験）"}, collector={"retries": 0})
        ctl = _controller(self)
        ctl.start()   # 開き直したばかり
        ctl._tick()
        m = json.loads((_cfg().ANALYSES_DIR / "a20261006-1001-retry" / "analysis.json").read_text(encoding="utf-8"))
        self.assertTrue("a20261006-1001-retry" in ctl.pending_retry or m["acquisition"]["status"] == "queued" or self.started,
                        "止まった取得の取り直しが、開き直したあと予定に入っていない")

    def test_login_wait_line(self):
        """（通る想定）ログイン待ちの1行目は、紙の「TikTok のログイン待ち」で始まる"""
        _cfg().save_state(logged_in_at=None)
        ctl = _controller(self)
        ctl.chrome = StubChrome(listening=True, logged=False)
        ctl._tick()
        self.assertTrue(ctl.status_text.startswith("TikTok のログイン待ち"), ctl.status_text)
        self.assertIn(("navigate", "https://www.tiktok.com/login"), ctl.chrome.calls)

    def test_idle_text_variants(self):
        """（通る想定）待機中の1行目: AI が無い／入っているがつながっていない／つながっている"""
        from collector_app import app
        ctl = _controller(self)
        with mock.patch.object(app, "ai_where", lambda: ""), mock.patch.object(app, "ai_installed", lambda: False):
            self.assertIn("アプリを入れてください", ctl._idle_text())
        with mock.patch.object(app, "ai_where", lambda: ""), mock.patch.object(app, "ai_installed", lambda: True):
            self.assertIn("「Claude につなぐ」か「ChatGPT につなぐ」", ctl._idle_text())
        with mock.patch.object(app, "ai_where", lambda: "Claude"), mock.patch.object(app, "ai_installed", lambda: True):
            self.assertEqual(ctl._idle_text(), f"準備OK・待機中（Claude で「{app.START_PHRASE}」と言ってください）")

    def test_relogin_notifies_once(self):
        """取得の途中でログインが切れた（係が need_login.json を置いて10秒おきに見る）→ ログインし直したら「準備OK」は1回。
        今は、係が印を消すまでの最大10秒のあいだにアプリの見張り（5秒おき）が印を見て、ログインの流れをもう一度回し「準備OK」を2回出す"""
        from collector_app import app, worker_entry
        ctl = _controller(self)
        ctl.worker.running = lambda: True
        ctl.chrome = StubChrome(listening=True, logged=False)
        # 係（生きている。ここでは試験のプロセスで代わり）がログインを待っている印
        worker_entry.need_login_flag().write_text(json.dumps({"since": "2026-10-06T10:00:00", "pid": os.getpid()}),
                                                  encoding="utf-8")
        with mock.patch.object(app, "ai_where", lambda: "Claude"), mock.patch.object(app, "ai_installed", lambda: True):
            ctl._tick()                 # 印に気づいてログインの画面を出す
            ctl.chrome.logged = True    # 利用者がログインした
            ctl._tick()                 # ログインを確かめる
            ctl._tick()                 # 係はまだ印を消していない（10秒おきに見る）
        ready = [n for n in NOTES if n[0] == "準備OK"]
        self.assertEqual(len(ready), 1, f"「準備OK」の通知が {len(ready)} 回出た")


# ---------------------------------------------------------------------------
# ChatGPT（Codex）の設定
# ---------------------------------------------------------------------------
class TestCodexLinkReview(unittest.TestCase):
    def setUp(self):
        _clean_data()
        from collector_app import codex_link
        self.cl = codex_link
        shutil.rmtree(self.cl.CODEX_HOME, ignore_errors=True)
        shutil.rmtree(self.cl.SKILL_DIR.parent, ignore_errors=True)

    def _cfg(self):
        return tomllib.loads(self.cl.CONFIG.read_text(encoding="utf-8"))

    def test_disabled_survives_app_update(self):
        """利用者が ChatGPT の設定で UGC Analyzer を切った（enabled = false）あとにアプリを新しくした（スキルが変わる）
        → 切ったままにするべき（記録「ChatGPT で切られているものには触らない」、試験 test_new_file_and_states「勝手には戻さない」）。
        今は status() が切られているより先に「古い」を返し、起動時の connect() が節を書き直して enabled = false を消す"""
        cl = self.cl
        cl.connect()
        cl.CONFIG.write_text(cl.CONFIG.read_text(encoding="utf-8").replace(
            "startup_timeout_sec", "enabled = false\nstartup_timeout_sec", 1), encoding="utf-8")
        self.assertEqual(cl.status(), "disabled")
        (cl.SKILL_DIR / "SKILL.md").write_text("前の版のスキル", encoding="utf-8")   # アプリを新しくした
        if cl.status() == "outdated":   # app.run() と同じ
            cl.connect()
        self.assertIs(self._cfg()["mcp_servers"]["ugc-analyzer"].get("enabled"), False,
                      "利用者が切った UGC Analyzer が、アプリを新しくしたら勝手に入っていた")

    def test_non_utf8_config_autolink(self):
        """Codex の設定ファイルが UTF-8 で読めない → 自動でつなぐは「つなげなかった」として記録し、設定には触らない。
        今は connect() の最初の read_text が UnicodeDecodeError（LinkError でない）を投げ、autolink() ごと落ちる
        （メニューの見張りが10秒おきに同じ例外。メニュー「ChatGPT につなぐ」も何も出ずに終わる）"""
        from collector_app import app, claude_link, codex_link
        cl = self.cl
        cl.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        raw = '[desktop]\nname = "あいう"\n'.encode("cp932")
        cl.CONFIG.write_bytes(raw)
        with mock.patch.object(claude_link, "claude_installed", lambda: False), \
                mock.patch.object(codex_link, "chatgpt_installed", lambda: True), \
                mock.patch.object(codex_link, "chatgpt_running", lambda: False):
            try:
                app.autolink(logging.getLogger("review-r6"))
            except Exception as e:
                self.fail(f"自動でつなぐが例外で止まった: {type(e).__name__}: {e}")
        self.assertEqual((_cfg().load_state().get("autolinked") or {}).get("chatgpt"), "error")
        self.assertEqual(cl.CONFIG.read_bytes(), raw)

    def test_similar_named_user_section_kept(self):
        """（通る想定）利用者が自分で書いた似た名前の節・ほかの表は消さない。2回つないでも同じ"""
        cl = self.cl
        cl.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        cl.CONFIG.write_text(
            'model = "gpt-x"\n\n[mcp_servers.ugc-analyzer-mine]\ncommand = "/mine"\n\n'
            '[mcp_servers."ugc-analyzer"]\ncommand = "/old/UGC Analyzer"\nargs = ["--mcp"]\n\n'
            "[mcp_servers.'ugc-analyzer'.tools.status]\napproval_mode = \"approve\"\n\n"
            '[profiles.work]\nmodel = "y"\n', encoding="utf-8")
        cl.connect()
        first = cl.CONFIG.read_text(encoding="utf-8")
        d = self._cfg()
        self.assertEqual(d["model"], "gpt-x")
        self.assertEqual(d["mcp_servers"]["ugc-analyzer-mine"], {"command": "/mine"})
        self.assertEqual(d["profiles"]["work"], {"model": "y"})
        self.assertEqual(d["mcp_servers"]["ugc-analyzer"]["command"], cl.entry()["command"])
        self.assertEqual(cl.status(), "connected")
        cl.connect()
        self.assertEqual(cl.CONFIG.read_text(encoding="utf-8"), first)

    def test_tools_match_mcp_proto(self):
        """（通る想定）許可を書く道具の一覧・スキルに挙げる道具が、mcp_proto.py の道具と同じ（13: recut を含む）"""
        tree = ast.parse((ROOT / "mcp_proto.py").read_text(encoding="utf-8"))
        tools = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in n.decorator_list:
                    f = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(f, ast.Attribute) and f.attr == "tool":
                        tools.add(n.name)
        self.assertEqual(set(self.cl.TOOLS), tools)
        for t in tools:
            self.assertIn(t, self.cl.SKILL_MD)


# ---------------------------------------------------------------------------
# Claude デスクトップの設定
# ---------------------------------------------------------------------------
class TestClaudeLinkReview(unittest.TestCase):
    def setUp(self):
        _clean_data()
        from collector_app import claude_link
        self.cl = claude_link
        shutil.rmtree(self.cl.CONFIG.parent, ignore_errors=True)
        self.cl.CONFIG.parent.mkdir(parents=True)

    def _baks(self):
        return sorted(self.cl.CONFIG.parent.glob(self.cl.CONFIG.name + ".bak-ugc-*"))

    def test_connect_twice_keeps_others(self):
        """（通る想定）ほかの道具・設定は残す。控えは元の中身。2回目も中身は同じ"""
        cl = self.cl
        orig = {"mcpServers": {"mine": {"command": "/x", "args": ["a"]},
                               cl.SERVER_NAME: {"command": "/old/UGC Analyzer", "args": ["--mcp"]}},
                "globalShortcut": "Alt+Space", "preferences": {"menuBarEnabled": True}}
        text = json.dumps(orig, ensure_ascii=False, indent=1)
        cl.CONFIG.write_text(text, encoding="utf-8")
        self.assertEqual(cl.status(), "outdated")
        b = cl.connect()
        self.assertEqual(Path(b).read_text(encoding="utf-8"), text)
        d = json.loads(cl.CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(d["mcpServers"]["mine"], orig["mcpServers"]["mine"])
        self.assertEqual((d["globalShortcut"], d["preferences"]), ("Alt+Space", {"menuBarEnabled": True}))
        self.assertEqual(d["mcpServers"][cl.SERVER_NAME], cl.entry())
        self.assertEqual(cl.status(), "connected")
        after = cl.CONFIG.read_text(encoding="utf-8")
        cl.connect()
        self.assertEqual(cl.CONFIG.read_text(encoding="utf-8"), after)

    def test_broken_config_untouched(self):
        """（通る想定）壊れた設定ファイルには書かない・控えも作らない・状態は none"""
        cl = self.cl
        cl.CONFIG.write_text('{"mcpServers": {', encoding="utf-8")
        self.assertEqual(cl.status(), "none")
        with self.assertRaises(cl.LinkError):
            cl.connect()
        self.assertEqual(cl.CONFIG.read_text(encoding="utf-8"), '{"mcpServers": {')
        self.assertEqual(self._baks(), [])

    def test_null_servers(self):
        """（通る想定）mcpServers が null でも、ほかの設定を残して足せる"""
        cl = self.cl
        cl.CONFIG.write_text('{"mcpServers": null, "x": 1}', encoding="utf-8")
        cl.connect()
        d = json.loads(cl.CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(d["x"], 1)
        self.assertEqual(cl.status(), "connected")


# ---------------------------------------------------------------------------
# Code タブ用の作業フォルダ（起動のたびに app.run() が整える。受け止めるのは OSError だけ）
# ---------------------------------------------------------------------------
class TestCodeFolderStartup(unittest.TestCase):
    def test_odd_settings_does_not_crash_startup(self):
        """~/UGC Analyzer/.claude/settings.local.json が JSON として読めるが辞書でない（[] など）
        → 起動を止めない（OSError として扱うか、書き直す）べき。
        今は AttributeError が app.run() の except OSError をすり抜け、メニューバーのアプリが起動しない"""
        from collector_app import code_link
        shutil.rmtree(code_link.FOLDER, ignore_errors=True)
        p = code_link.FOLDER / ".claude" / "settings.local.json"
        p.parent.mkdir(parents=True)
        p.write_text("[]", encoding="utf-8")
        try:
            code_link.ensure()
        except OSError:
            pass
        except Exception as e:
            self.fail(f"起動を止める例外: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 同梱の部品の写し（code/）
# ---------------------------------------------------------------------------
class TestCodeCopy(unittest.TestCase):
    def test_copy_once_and_new_version(self):
        """（通る想定）同梱の部品は1回だけ写す。中身が変われば別の場所に写す。__pycache__ は版の印に入れない。写しかけは残さない"""
        config = _cfg()
        tmp = Path(tempfile.mkdtemp(dir=_TMP))
        src = tmp / "bundle" / "code"
        (src / "acquire" / "__pycache__").mkdir(parents=True)
        (src / "acquire" / "x.py").write_text("A = 1\n", encoding="utf-8")
        (src / "acquire" / "__pycache__" / "x.pyc").write_bytes(b"zz")
        root = tmp / "coderoot"
        with mock.patch.object(config, "FROZEN", True), mock.patch.object(config, "bundled_code_dir", lambda: src), \
                mock.patch.object(config, "CODE_ROOT", root):
            d1 = config.code_dir()
            self.assertTrue((d1 / ".complete").exists())
            self.assertEqual((d1 / "acquire" / "x.py").read_text(encoding="utf-8"), "A = 1\n")
            (d1 / "marker").write_text("1")
            self.assertEqual(config.code_dir(), d1)
            self.assertTrue((d1 / "marker").exists())
            (src / "acquire" / "__pycache__" / "x.pyc").write_bytes(b"yy")
            self.assertEqual(config.code_dir(), d1)
            (src / "acquire" / "x.py").write_text("A = 2\n", encoding="utf-8")
            d2 = config.code_dir()
            self.assertNotEqual(d1, d2)
            self.assertEqual((d2 / "acquire" / "x.py").read_text(encoding="utf-8"), "A = 2\n")
            self.assertEqual([p for p in root.iterdir() if ".tmp" in p.name], [])


# ---------------------------------------------------------------------------
# 固めたアプリ（PyInstaller）に入るもの
# ---------------------------------------------------------------------------
def _spec_text() -> str:
    return (ROOT / "collector" / "UGCCollector.spec").read_text(encoding="utf-8")


def _spec_suffixes() -> tuple:
    m = re.search(r"q\.suffix in (\([^)]*\))", _spec_text())
    return ast.literal_eval(m.group(1))


def _shipped() -> set:
    """spec が code/ に入れるファイル（リポジトリからの相対パス）"""
    from collector_app.config import CODE_FILES
    out = set()
    suf = _spec_suffixes()
    for f in CODE_FILES:
        p = ROOT / f
        if p.is_dir():
            out |= {str(q.relative_to(ROOT)) for q in p.rglob("*")
                    if q.is_file() and "__pycache__" not in q.parts and q.suffix in suf}
        else:
            out.add(f)
    return out


def _imports(path: Path):
    """(モジュール名, from で取り出す名前の一覧) を全部（関数の中の import も）"""
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name, []
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            yield n.module, [a.name for a in n.names]


class TestBundle(unittest.TestCase):
    def setUp(self):
        self.shipped = _shipped()
        self.py = [ROOT / f for f in sorted(self.shipped) if f.endswith(".py")]
        self.app_py = sorted((ROOT / "collector" / "collector_app").glob("*.py"))
        self.repo_mods = {p.stem: f"{p.name}" for p in ROOT.glob("*.py")}
        self.analysis_mods = {p.stem: f"analysis/{p.name}" for p in (ROOT / "analysis").glob("*.py")}

    def test_every_local_import_is_shipped(self):
        """（通る想定）同梱の部品と collector_app が import する本線のモジュールは、全部 code/ に入る（recut.py などの取りこぼし）"""
        missing = []
        for f in self.py + self.app_py:
            for mod, names in _imports(f):
                top = mod.split(".")[0]
                if top == "acquire":
                    cands = [f"acquire/{mod.split('.', 1)[1]}.py"] if "." in mod else \
                        [f"acquire/{n}.py" for n in names if (ROOT / "acquire" / f"{n}.py").exists()] or ["acquire/__init__.py"]
                elif top in self.repo_mods and top != "collector_app":
                    cands = [self.repo_mods[top]]
                elif top in self.analysis_mods:
                    cands = [self.analysis_mods[top]]
                else:
                    continue
                for c in cands:
                    if c not in self.shipped:
                        missing.append(f"{f.relative_to(ROOT)} → {c}")
        self.assertEqual(missing, [])

    def test_files_read_by_path_are_shipped(self):
        """（通る想定）部品が場所で呼ぶスクリプト・指示書が code/ に入る。同梱するフォルダに spec の拡張子で落ちるファイルが無い"""
        want = set()
        for f in self.py:
            t = f.read_text(encoding="utf-8")
            want |= set(re.findall(r'_script\(\s*"([^"]+\.py)"', t))
            want |= set(re.findall(r'\[\s*"(analysis/[a-z_]+\.py)"', t))
            want |= {f"analysis/{x}" for x in re.findall(r'BASE_DIR\s*/\s*"analysis"\s*/\s*"([a-z_]+\.py)"', t)}
            want |= set(re.findall(r'BASE_DIR\s*/\s*"([a-z_]+\.py)"', t))
            want |= {f"service_prompts/w1/{x}" for x in re.findall(r'\btpl\("([a-z_]+\.md)"\)', t)}
        import prompt_store
        want |= {f"service_prompts/w1/{n}" for n in prompt_store.TITLES}
        self.assertIn("acquire/recut.py", self.shipped)
        self.assertIn("service_prompts/w1/recut.md", self.shipped)
        self.assertTrue(want)
        self.assertEqual(sorted(want - self.shipped), [])
        from collector_app.config import CODE_FILES
        dropped = []
        for f in CODE_FILES:
            p = ROOT / f
            if p.is_dir():
                dropped += [str(q.relative_to(ROOT)) for q in p.rglob("*")
                            if q.is_file() and "__pycache__" not in q.parts and q.suffix not in _spec_suffixes()
                            and q.name != ".DS_Store"]
        self.assertEqual(dropped, [], "spec の拡張子の絞り込みで落ちるファイル")

    def test_third_party_named_in_spec(self):
        """（通る想定）code/ の部品が使う外のライブラリは、spec の hiddenimports か collect_submodules に名前がある
        （code/ の .py は PyInstaller に解析されないため）"""
        names = {n.value for n in ast.walk(ast.parse(_spec_text())) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        local = set(self.repo_mods) | set(self.analysis_mods) | {"acquire", "collector_app"}
        missing = []
        for f in self.py:
            for mod, _ in _imports(f):
                top = mod.split(".")[0]
                if top in sys.stdlib_module_names or top in local:
                    continue
                if not any(s == mod or mod.startswith(s + ".") or s.startswith(mod + ".") for s in names):
                    missing.append(f"{f.relative_to(ROOT)}: {mod}")
        self.assertEqual(missing, [])

    def test_last_build_has_everything(self):
        """（通る想定・作ったものがあるときだけ）最後に作った .app の PYZ に、code/ の部品が import する標準と外のモジュールが全部ある"""
        b = ROOT / "collector" / "build" / "UGCCollector"
        if not (b / "PYZ-00.toc").exists():
            self.skipTest("collector/build が無い")
        pyz = {x[0] for x in ast.literal_eval((b / "PYZ-00.toc").read_text(encoding="utf-8"))[1]}
        base = {n[:-4].replace("/", ".").removesuffix(".__init__")
                for n in zipfile.ZipFile(b / "base_library.zip").namelist() if n.endswith(".pyc")}
        ext = set()
        for name, _src, kind in ast.literal_eval((b / "COLLECT-00.toc").read_text(encoding="utf-8"))[0]:
            if kind == "EXTENSION":
                n = name.split("lib-dynload/")[-1]
                ext.add(n.split(".cpython")[0].replace("/", "."))
        have = pyz | base | ext | set(sys.builtin_module_names)
        local = set(self.repo_mods) | set(self.analysis_mods) | {"acquire", "collector_app"}
        missing = []
        for f in self.py:
            for mod, names in _imports(f):
                if mod.split(".")[0] in local:
                    continue
                if mod not in have:
                    missing.append(f"{f.relative_to(ROOT)}: {mod}")
                for n in names:   # from X import Y の Y がモジュールなら、それも要る
                    full = f"{mod}.{n}"
                    if importlib.util.find_spec(mod) and _is_module(full) and full not in have:
                        missing.append(f"{f.relative_to(ROOT)}: {full}")
        self.assertEqual(missing, [])

    @unittest.skipUnless(os.environ.get("UGC_CHECK_DIST"), "作り直した直後に UGC_CHECK_DIST=1 で走らせる（ソースを直すたびに落ちるため）")
    def test_dist_app_matches_repo(self):
        """（作ったものがあるときだけ）配る .app（collector/dist）の code/ が、今の作業ツリーと同じ。
        落ちたら「.app を作ったあとに部品が変わった」＝配る前に作り直しが要る（不具合ではなく、作り直し忘れの見張り）"""
        app_code = ROOT / "collector" / "dist" / "UGC Analyzer.app" / "Contents" / "Frameworks" / "code"
        if not app_code.exists():
            self.skipTest("collector/dist の .app が無い")
        in_app = {str(q.relative_to(app_code)) for q in app_code.rglob("*") if q.is_file() and "__pycache__" not in q.parts}
        self.assertEqual(sorted(self.shipped ^ in_app), [], "配る .app の code/ と、今の同梱の一覧が違う（作り直しが要る）")
        differ = [f for f in sorted(self.shipped) if (app_code / f).read_bytes() != (ROOT / f).read_bytes()]
        self.assertEqual(differ, [], "配る .app の code/ の中身が今の作業ツリーと違う（作り直しが要る）")


def _is_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 入れ替え（collector/install.sh）の「取得中なら止まる」
# ---------------------------------------------------------------------------
class TestInstallBusyCheck(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "collector" / "install.sh").read_text(encoding="utf-8")
        self.d = Path(tempfile.mkdtemp(dir=_TMP)) / "analyses"
        self.d.mkdir()

    def _put(self, aid, meta=None, raw=None):
        (self.d / aid).mkdir()
        (self.d / aid / "analysis.json").write_text(raw if raw is not None else json.dumps({"title": aid, **meta}),
                                                    encoding="utf-8")

    def _busy(self):
        py = self.script.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
        r = subprocess.run([sys.executable, "-", str(self.d)], input=py, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()}

    def test_busy_states(self):
        """（通る想定）本線の取得（順番待ち・取得中）・掘り下げと切り直しの取り足し（順番待ち・取得中）で止まる。済み・止まった・やめたでは止まらない"""
        self._put("q", {"acquisition": {"status": "queued"}})
        self._put("r", {"acquisition": {"status": "running", "pid": 99999}})
        self._put("cutq", {"acquisition": {"status": "done"}, "deepen": {"status": "queued", "kind": "recut", "round": 1}})
        self._put("dpr", {"acquisition": {"status": "done"}, "deepen": {"status": "running", "round": 2}})
        self._put("done", {"acquisition": {"status": "done"}, "deepen": {"status": "done", "round": 1}})
        self._put("cancel", {"acquisition": {"status": "cancelled"}})
        self._put("broken", raw="{")
        self.assertEqual(self._busy(), {"q", "r", "cutq", "dpr"})

    def test_paths_match_app(self):
        """（通る想定）install.sh の置き場・ロック・.app の名前が、アプリと spec と同じ"""
        config = _cfg()
        import tiktok_lock
        self.assertIn(f'DATA="$HOME/Library/Application Support/{config.APP_NAME}"', self.script)
        self.assertIn('"$DATA/locks/tiktok.lock"', self.script)
        self.assertEqual(tiktok_lock.LOCK_FILE.name, "tiktok.lock")
        self.assertEqual(Path(os.environ["UGC_LOCK_DIR"]), config.DATA_DIR / "locks")
        self.assertIn(f'APP="/Applications/{config.APP_NAME}.app"', self.script)
        self.assertIn(f'EXE="$APP/Contents/MacOS/{config.APP_NAME}"', self.script)
        self.assertIn(f'name="{config.APP_NAME}"', _spec_text())


# ---------------------------------------------------------------------------
# 知識ベースの週1の確かめ
# ---------------------------------------------------------------------------
class TestKbWeekly(unittest.TestCase):
    def setUp(self):
        _clean_data()
        self.kbdir = Path(tempfile.mkdtemp(dir=_TMP))
        (self.kbdir / "distilled").mkdir()
        (self.kbdir / "notes").mkdir()
        (self.kbdir / "distilled" / "GLOSSARY.md").write_text("# G\n", encoding="utf-8")
        (self.kbdir / "INDEX.json").write_text(json.dumps([{"key": "nold1", "file": "old.md"}]), encoding="utf-8")
        self.saved = os.environ.get("UGC_KB_DIR")
        os.environ["UGC_KB_DIR"] = str(self.kbdir)
        import kb_update
        self.kb = kb_update

    def tearDown(self):
        os.environ["UGC_KB_DIR"] = self.saved

    def _last(self, delta):
        t = (datetime.datetime.now().astimezone() - delta).isoformat(timespec="seconds")
        (self.kbdir / "update_state.json").write_text(json.dumps({"last_check": t}), encoding="utf-8")

    def test_due_boundary(self):
        """（通る想定）最後に見てから7日で確かめる。6日23時間ではまだ"""
        self.assertTrue(self.kb.due())   # 一度も見ていない
        self._last(datetime.timedelta(days=6, hours=23))
        self.assertFalse(self.kb.due())
        self._last(datetime.timedelta(days=7, minutes=1))
        self.assertTrue(self.kb.due())

    def test_failed_check_is_retried(self):
        """（通る想定）note につながらなかったら、最後に見た時刻を書かない（1時間後にまた確かめる）"""
        def fetch(url):
            raise ConnectionError("ネットなし")
        with self.assertRaises(ConnectionError):
            self.kb.check_new(log=lambda m: None, fetch=fetch, sleep=0)
        self.assertTrue(self.kb.due())
        self.assertNotIn("last_check", self.kb.state())

    def test_side_jobs_checks_only_when_due(self):
        """（通る想定）メニューのアプリは1時間おきに「確かめる時期か」を見て、時期のときだけ note を見る（通知は静かに）"""
        ctl = _controller(self)
        calls = []
        ctl.check_knowledge = lambda quiet=False: calls.append(quiet)
        ctl.next_prompts = time.time() + 3600
        with mock.patch.object(self.kb, "ready", lambda: True), mock.patch.object(self.kb, "due", lambda s=None: False):
            ctl.next_kb = 0
            ctl._side_jobs()
        self.assertEqual(calls, [])
        with mock.patch.object(self.kb, "ready", lambda: True), mock.patch.object(self.kb, "due", lambda s=None: True):
            ctl.next_kb = 0
            ctl._side_jobs()
            ctl._side_jobs()   # 1時間たつまでは見ない
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
