"""自動の更新（0.9.0。collector_app/updater.py・app.py の新しい版の処理・docs/AUTO_UPDATE.md）の試験。
TikTok にも Claude にも、本物の GitHub・置き場・/Applications にも触らない（一時フォルダ・手元の偽の GitHub で）。

  collector/.venv/bin/python -m unittest tests.test_auto_update

- 入れる候補の選び方（pick）: 友達は「最新」だけ・運営の Mac は試し版も・下書きや .dmg の無いものは見ない・「今回の版」の1行
- 入れ替えるかの決め方（decide）: 作業中は待つ・AI が閉じていれば黙って・開いていれば1日1回窓で・入れられなかった版は自分からは入れない
- 落として確かめる（stage）: 大きさ・sha256・中のアプリの識別子と版・自己点検。だめなら何も残さない。ダウンロードの印が付かない
- 入れ替えの係（swap.sh）: アプリの終わりを待つ・前の版を1つだけ控える・失敗したら前の版を戻して開く
- 新しい版で開いたとき（update_notice・version_notice）・見張り役の判断（Controller._update_tick・update_answer・_check_update）
"""
import datetime
import http.server
import importlib
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector"))
for _name in ("collector-mcp", "collector", "t"):   # 試験の画面に記録を出さない
    logging.getLogger(_name).addHandler(logging.NullHandler())

APP_ID = "jp.ugc-analyzer.collector"
LOG = logging.getLogger("t")


def make_app(folder: Path, version: str, ident: str = APP_ID, selftest_rc: int = 0, name: str = "UGC Analyzer") -> Path:
    """Info.plist と、--selftest で selftest_rc を返す実行ファイルだけの .app"""
    app = folder / f"{name}.app"
    exe = app / "Contents" / "MacOS" / "UGC Analyzer"
    exe.parent.mkdir(parents=True)
    exe.write_text(f'#!/bin/sh\n[ "$1" = "--selftest" ] && exit {selftest_rc}\nexit 0\n# {version}\n', encoding="utf-8")
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": ident, "CFBundleShortVersionString": version, "CFBundleName": "UGC Analyzer"}, f)
    return app


def make_dmg(tmp: Path, version: str, **kw) -> Path:
    src = tmp / f"src-{version}-{len(list(tmp.glob('src-*')))}"
    src.mkdir()
    make_app(src, version, **kw)
    dmg = tmp / f"{src.name}.dmg"
    subprocess.run(["hdiutil", "create", "-volname", "UGC Analyzer", "-srcfolder", str(src), "-ov", "-format", "UDZO",
                    str(dmg)], check=True, capture_output=True)
    return dmg


def release(version: str, url: str, size: int = 0, sha: str | None = None, pre: bool = False, note: str | None = None,
            asset: str = "UGC-Analyzer.dmg", draft: bool = False) -> dict:
    body = "直したこと" + (f"\n\n今回の版: {note}" if note else "")
    return {"tag_name": f"v{version}", "prerelease": pre, "draft": draft, "body": body,
            "assets": [{"name": asset, "size": size, "browser_download_url": url,
                        "digest": f"sha256:{sha}" if sha else None}]}


class FakeGitHub:
    """手元の偽の GitHub（/releases/latest・/releases?per_page=20・/dl/<名前>）"""

    def __init__(self):
        self.releases = []
        self.files = {}
        self.fail = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if outer.fail:
                    self.send_error(500)
                    return
                path = self.path.split("?")[0]
                if path == "/releases/latest":
                    rel = [r for r in outer.releases if not r["prerelease"] and not r["draft"]]
                    return self._json(rel[0]) if rel else self.send_error(404)
                if path == "/releases":
                    return self._json(outer.releases)
                if path.startswith("/dl/") and path[4:] in outer.files:
                    data = outer.files[path[4:]].read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, version: str, dmg: Path, **kw) -> dict:
        import hashlib
        name = f"{version}-{dmg.name}"
        self.files[name] = dmg
        kw.setdefault("size", dmg.stat().st_size)
        kw.setdefault("sha", hashlib.sha256(dmg.read_bytes()).hexdigest())
        rel = release(version, f"{self.base}/dl/{name}", **kw)
        self.releases.insert(0, rel)
        return rel

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HomeCase(unittest.TestCase):
    """UGC_COLLECTOR_HOME を一時フォルダにして config を読み直す"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ugc-upd-"))
        self.env = mock.patch.dict(os.environ, {"UGC_COLLECTOR_HOME": str(self.tmp / "home")})
        self.env.start()
        from collector_app import config
        self.config = importlib.reload(config)
        self.config.setup_env()
        from collector_app import updater
        self.u = updater

    def tearDown(self):
        self.env.stop()
        importlib.reload(self.config)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPick(unittest.TestCase):
    def setUp(self):
        from collector_app import updater
        self.u = updater

    def test_friends_only_latest(self):
        rels = [release("9.0.2", "u2", pre=True), release("9.0.1", "u1", size=5, sha="a" * 64, note="直しました"),
                release("9.0.0", "u0")]
        got = self.u.pick(rels, include_pre=False)
        self.assertEqual((got["version"], got["url"], got["size"], got["sha256"], got["note"], got["prerelease"]),
                         ("9.0.1", "u1", 5, "a" * 64, "直しました", False))
        self.assertEqual(self.u.pick(rels, include_pre=True)["version"], "9.0.2")   # 運営の Mac は試し版も

    def test_skips_broken(self):
        rels = [release("9.9.9", "x", draft=True), release("9.9.8", "x", asset="other.zip"), release("bad", "x"),
                release("9.0.10", "ok"), release("9.0.9", "old")]
        got = self.u.pick(rels, include_pre=True)
        self.assertEqual(got["version"], "9.0.10")   # 版は数で比べる（9.0.10 > 9.0.9）
        self.assertIsNone(got["sha256"])
        self.assertIsNone(got["note"])
        self.assertIsNone(self.u.pick([], include_pre=False))

    def test_note(self):
        self.assertEqual(self.u.note_of("見出し\n\n今回の版：コメントを直しました。 \n\n友達向けのダウンロード: x"),
                         "コメントを直しました。")
        self.assertIsNone(self.u.note_of("今回は無し"))


class TestDecide(unittest.TestCase):
    def setUp(self):
        from collector_app import updater
        self.u = updater
        self.s = {"version": "9.0.1", "app": "/x"}

    def test_matrix(self):
        d = self.u.decide
        today = self.u.today()
        self.assertIsNone(d(None, None, [], {}))
        self.assertEqual(d(self.s, "取得", [], {}), "wait")
        self.assertEqual(d(self.s, "AI の作業", ["Claude"], {}, manual=True), "wait")
        self.assertEqual(d(self.s, None, [], {}), "silent")
        self.assertEqual(d(self.s, None, ["Claude"], {}), "ask")
        self.assertIsNone(d(self.s, None, ["Claude"], {"asked_on": today}))   # 自分から聞くのは1日1回
        self.assertEqual(d(self.s, None, ["Claude"], {"asked_on": "2000-01-01"}), "ask")
        self.assertEqual(d(self.s, None, ["Claude"], {"asked_on": today}, manual=True), "ask")   # メニューからはいつでも
        failed = {"failed": {"version": "9.0.1"}}
        self.assertIsNone(d(self.s, None, [], failed))   # 入れられなかった版は、自分からは入れない
        self.assertEqual(d(self.s, None, [], failed, manual=True), "ask")
        self.assertEqual(d(self.s, None, [], {"failed": {"version": "9.0.0"}}), "silent")


class TestStage(HomeCase):
    @classmethod
    def setUpClass(cls):
        cls.dmgs = Path(tempfile.mkdtemp(prefix="ugc-upd-dmg-"))
        cls.good = make_dmg(cls.dmgs, "9.0.1")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dmgs, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.gh = FakeGitHub()
        self.p_api = mock.patch.dict(os.environ, {"UGC_UPDATE_API": self.gh.base + "/releases"})
        self.p_api.start()

    def tearDown(self):
        self.p_api.stop()
        self.gh.close()
        super().tearDown()

    def leftovers(self):
        d = self.u.updates_dir()
        return sorted(str(p.relative_to(d)) for p in d.rglob("*")) if d.exists() else []

    def test_latest_release_channels(self):
        self.gh.add("9.0.1", self.good, note="直しました")
        self.gh.add("9.0.2", self.good, pre=True)
        self.assertEqual(self.u.latest_release()["version"], "9.0.1")   # 友達: 最新だけ
        self.config.OPERATOR_FLAG.write_text("1")
        self.assertEqual(self.u.latest_release()["version"], "9.0.2")   # 運営の Mac: 試し版も
        self.gh.fail = True
        with self.assertRaises(Exception):
            self.u.latest_release()

    def test_stage_good(self):
        rel = self.u.pick([self.gh.add("9.0.1", self.good, note="直しました")], False)
        s = self.u.stage(rel, LOG)
        app = Path(s["app"])
        self.assertEqual((s["version"], s["note"]), ("9.0.1", "直しました"))
        self.assertEqual(self.u.bundle_info(app)["CFBundleShortVersionString"], "9.0.1")
        self.assertTrue(os.access(app / "Contents" / "MacOS" / "UGC Analyzer", os.X_OK))
        dmg = self.u.updates_dir() / "UGC-Analyzer-9.0.1.dmg"
        self.assertTrue(dmg.exists())
        for p in (dmg, app, app / "Contents" / "MacOS" / "UGC Analyzer"):   # ダウンロードの印が付かない（「このまま開く」が要らない）
            r = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(p)], capture_output=True)
            self.assertNotEqual(r.returncode, 0, p)
        self.u.save(staged=s)
        with mock.patch.object(self.u, "VERSION", "9.0.0"):
            self.assertEqual(self.u.staged_from_state()["version"], "9.0.1")
        with mock.patch.object(self.u, "VERSION", "9.0.1"):
            self.assertIsNone(self.u.staged_from_state())   # 同じ版はもう入っている
        shutil.rmtree(app)
        with mock.patch.object(self.u, "VERSION", "9.0.0"):
            self.assertIsNone(self.u.staged_from_state())   # .app が消えた

    def test_stage_again_uses_download_and_tidies(self):
        rel = self.u.pick([self.gh.add("9.0.1", self.good)], False)
        self.u.stage(rel, LOG)
        rel2 = self.u.pick([self.gh.add("9.0.2", make_dmg(self.dmgs, "9.0.2"))], False)
        self.u.stage(rel2, LOG)
        names = self.leftovers()
        self.assertIn("UGC-Analyzer-9.0.2.dmg", names)
        self.assertNotIn("UGC-Analyzer-9.0.1.dmg", names)   # 前に用意した版は片付ける
        self.assertFalse((self.u.updates_dir() / "staged" / "9.0.1").exists())

    def test_bad_downloads_leave_nothing(self):
        cases = [("sha256", dict(sha="0" * 64)), ("大きさ", dict(size=12345))]
        for why, kw in cases:
            with self.subTest(why):
                rel = self.u.pick([self.gh.add("9.0.1", self.good, **kw)], False)
                with self.assertRaises(self.u.UpdateError) as cm:
                    self.u.stage(rel, LOG)
                self.assertIn(why, str(cm.exception))
                self.assertEqual(self.leftovers(), [])

    def test_bad_inside(self):
        cases = [("版", make_dmg(self.dmgs, "9.0.0")),                       # 中のアプリの版が違う
                 ("識別子", make_dmg(self.dmgs, "9.0.1", ident="com.example.other")),
                 ("自己点検", make_dmg(self.dmgs, "9.0.1", selftest_rc=3)),
                 ("ありません", make_dmg(self.dmgs, "9.0.1", name="Other"))]
        for why, dmg in cases:
            with self.subTest(why):
                rel = self.u.pick([self.gh.add("9.0.1", dmg)], False)
                with self.assertRaises(self.u.UpdateError) as cm:
                    self.u.stage(rel, LOG)
                self.assertIn(why, str(cm.exception))
                self.assertFalse((self.u.updates_dir() / "staged" / "9.0.1").exists())
                # 落とした .dmg は残る（中身の印は合っている。次に確かめるときに落とし直さない）
                self.u.tidy()


class TestSwap(HomeCase):
    """入れ替えの係（swap.sh）を、一時フォルダの .app で回す。開く命令は、開いた場所を書くだけの偽物"""

    def setUp(self):
        super().setUp()
        self.apps = self.tmp / "Applications"
        self.apps.mkdir()
        self.app = make_app(self.apps, "9.0.0")
        self.new = make_app(self.tmp / "staged", "9.0.1")
        self.bk = self.tmp / "home" / "updates" / "backup" / "UGC Analyzer.app"
        self.opened = self.tmp / "opened.txt"
        self.opener = self.tmp / "opener.sh"
        self.opener.write_text(f'#!/bin/sh\necho "$1" >> "{self.opened}"\n', encoding="utf-8")
        self.opener.chmod(0o755)
        self.script = self.tmp / "swap.sh"
        self.script.write_text(self.u.SWAP_SH, encoding="utf-8")
        self.log = self.tmp / "update.log"

    def swap(self, pid: int | None = None, new: Path | None = None) -> int:
        if pid is None:
            p = subprocess.Popen(["true"])
            p.wait()
            pid = p.pid
        return subprocess.run(["/bin/bash", str(self.script), str(pid), str(self.app), str(new or self.new), str(self.bk),
                               str(self.log), str(self.opener)], timeout=60).returncode

    def version(self, app: Path) -> str:
        return self.u.bundle_info(app)["CFBundleShortVersionString"]

    def test_swap(self):
        self.assertEqual(self.swap(), 0)
        self.assertEqual(self.version(self.app), "9.0.1")
        self.assertEqual(self.version(self.bk), "9.0.0")
        self.assertFalse(self.new.exists())
        self.assertEqual(self.opened.read_text().split("\n")[0], str(self.app))
        self.assertIn("入れ替えた。開く", self.log.read_text())
        # もう1回: 控えは1つだけ（前の控えは消える）
        self.new = make_app(self.tmp / "staged2", "9.0.2")
        self.assertEqual(self.swap(), 0)
        self.assertEqual((self.version(self.app), self.version(self.bk)), ("9.0.2", "9.0.1"))
        self.assertEqual([p.name for p in self.bk.parent.iterdir()], ["UGC Analyzer.app"])

    def test_waits_for_app(self):
        # アプリの代わり。試験の子にしない（子だと終わっても片付けるまで残り、係が待ち続ける。本物のアプリは Mac が片付ける）
        pid = int(subprocess.run(["/bin/bash", "-c", "sleep 1.5 >/dev/null 2>&1 & echo $!"], capture_output=True,
                                 text=True).stdout)
        t0 = time.time()
        self.assertEqual(self.swap(pid=pid), 0)
        self.assertGreater(time.time() - t0, 1.0)   # アプリが終わるまで待った
        self.assertEqual(self.version(self.app), "9.0.1")

    def test_new_missing(self):
        self.assertEqual(self.swap(new=self.tmp / "nothing.app"), 1)
        self.assertEqual(self.version(self.app), "9.0.0")
        self.assertEqual(self.opened.read_text().strip(), str(self.app))   # 前の版を開く

    def test_cannot_move_app_keeps_old(self):
        make_app(self.tmp / "oldbk", "8.0.0")
        self.bk.parent.mkdir(parents=True)
        shutil.move(str(self.tmp / "oldbk" / "UGC Analyzer.app"), str(self.bk))
        self.apps.chmod(0o555)   # アプリケーションフォルダに書けない（Mac の「アプリの管理」で断られたときの代わり）
        try:
            self.assertEqual(self.swap(), 1)
        finally:
            self.apps.chmod(0o755)
        self.assertEqual(self.version(self.app), "9.0.0")
        self.assertEqual(self.version(self.bk), "8.0.0")   # 前の控えも元に戻す
        self.assertTrue(self.new.exists())
        self.assertEqual(self.opened.read_text().strip(), str(self.app))
        self.assertIn("今の .app を動かせない", self.log.read_text())


class TestApply(HomeCase):
    def test_apply(self):
        app = make_app(self.tmp / "Applications", "9.0.0")
        new = make_app(self.tmp / "staged", "9.0.1")
        calls = []
        with mock.patch.object(self.u, "current_bundle", return_value=app), \
                mock.patch.object(self.u.subprocess, "Popen", lambda args, **kw: calls.append((args, kw))):
            self.u.apply({"version": "9.0.1", "app": str(new), "note": "直しました"}, "now", ["Claude"], LOG)
        args, kw = calls[0]
        self.assertEqual(args[0], "/bin/bash")
        self.assertEqual(args[2:7], [str(os.getpid()), str(app), str(new),
                                     str(self.u.updates_dir() / "backup" / "UGC Analyzer.app"), str(self.u.update_log())])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        p = self.config.load_state()["update_pending"]
        self.assertEqual((p["from"], p["to"], p["mode"], p["restart"], p["note"]),
                         (self.u.VERSION, "9.0.1", "now", ["Claude"], "直しました"))
        self.assertEqual(Path(args[1]).read_text(encoding="utf-8"), self.u.SWAP_SH)

    def test_outside_applications(self):
        new = make_app(self.tmp / "staged", "9.0.1")
        with mock.patch.object(self.u, "current_bundle", return_value=None):
            with self.assertRaises(self.u.UpdateError):
                self.u.apply({"version": "9.0.1", "app": str(new)}, "silent", [], LOG)
        self.assertNotIn("update_pending", self.config.load_state())

    def test_ai_active(self):
        log = self.config.LOG_DIR / "mcp.log"
        stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ",123"
        now = time.time()
        with mock.patch.object(self.u.system, "tool_processes", return_value=[]):
            self.assertFalse(self.u.ai_active())   # 道具を使ったことが無い
            log.write_text(f"{stamp(now - 60)} INFO    [1] === AI の道具を開きます（UGC Analyzer 0.9.0）\n", encoding="utf-8")
            self.assertFalse(self.u.ai_active())   # Claude を開いただけ（道具の処理を開いた行）は作業に数えない
            log.write_text(f"{stamp(now - 120)} INFO    [1] 道具の中身 status: 0.2秒\n"
                           f"{stamp(now - 60)} INFO    [1] === AI の道具を閉じました\n", encoding="utf-8")
            self.assertTrue(self.u.ai_active())
            self.assertAlmostEqual(self.u.last_tool_call(), now - 120, delta=2)
            log.write_text(f"{stamp(now - self.u.AI_QUIET - 60)} INFO    [1] 道具の中身 status: 0.2秒\n", encoding="utf-8")
            self.assertFalse(self.u.ai_active())
            log.write_text("", encoding="utf-8")   # 記録が入れ替わった直後: 1つ前の記録から
            (self.config.LOG_DIR / "mcp.log.1").write_text(f"{stamp(now - 30)} INFO    [1] 道具の中身 next_task: 1.0秒\n",
                                                            encoding="utf-8")
            self.assertTrue(self.u.ai_active())
        with mock.patch.object(self.u.system, "tool_processes", return_value=[{"kind": "wait"}]):
            (self.config.LOG_DIR / "mcp.log.1").unlink()
            self.assertTrue(self.u.ai_active())   # Code タブで待っている


class AppCase(HomeCase):
    def setUp(self):
        super().setUp()
        from collector_app import app
        self.app = app
        app.startup_alerts.clear()
        app.startup_restarts.clear()
        self.sent = []
        self.p_send = mock.patch.object(app.notify, "send", lambda t, b: self.sent.append((t, b)))
        self.p_send.start()

    def tearDown(self):
        self.p_send.stop()
        self.app.startup_alerts.clear()
        self.app.startup_restarts.clear()
        super().tearDown()


class TestNotice(AppCase):
    def links(self, claude=True):
        class Fake:
            def status(self):
                return "connected"

            def running(self):
                return claude
        return (("claude", "Claude", Fake(), "x", "running"),)

    def test_updated(self):
        self.u.save(staged={"version": self.app.VERSION}, asked_on="2000-01-01")
        st = {"update_pending": {"from": "0.0.1", "to": self.app.VERSION, "mode": "now", "restart": ["Claude"],
                                 "note": "コメントを直しました"}, "app_version": "0.0.1", "first_run_done": True}
        p = self.app.update_notice(st, LOG)
        self.assertEqual(p["mode"], "now")
        self.assertEqual(self.sent, [(f"UGC Analyzer を {self.app.VERSION} にしました",
                                      "今回の版: コメントを直しました。分析とレポートは、そのまま残っています。")])
        s = self.config.load_state()
        self.assertIsNone(s["update_pending"])
        self.assertIsNone(s["update"]["staged"])
        # 「今すぐ」: 前の版の道具が動いている AI を、聞かずに開き直す。通知は重ねない
        with mock.patch.object(self.app, "AI_LINKS", self.links()), \
                mock.patch.object(self.app.system, "old_tool_processes", return_value=[{"pid": 1, "kind": "mcp", "version": "0.0.1"}]):
            self.app.version_notice(st, LOG, p)
        self.assertEqual(self.app.startup_restarts, ["Claude"])
        self.assertEqual(self.app.startup_alerts, [])
        self.assertEqual(len(self.sent), 1)

    def test_updated_now_without_old_tools(self):
        # 窓で「今すぐ」を押したが、Claude の中の前の版の道具はもう止まっていた: 伝えたとおり Claude を開き直す
        st = {"update_pending": {"from": "0.0.1", "to": self.app.VERSION, "mode": "now", "restart": ["Claude"]},
              "app_version": "0.0.1", "first_run_done": True}
        p = self.app.update_notice(st, LOG)
        with mock.patch.object(self.app, "AI_LINKS", self.links()), \
                mock.patch.object(self.app.system, "old_tool_processes", return_value=[]):
            self.app.version_notice(st, LOG, p)
        self.assertEqual(self.app.startup_restarts, ["Claude"])
        self.assertEqual(self.app.startup_alerts, [])
        self.app.startup_restarts.clear()
        with mock.patch.object(self.app, "AI_LINKS", self.links(claude=False)), \
                mock.patch.object(self.app.system, "old_tool_processes", return_value=[]):
            self.app.version_notice(st, LOG, p)   # その間に Claude を閉じていた: 開かない
        self.assertEqual(self.app.startup_restarts, [])

    def test_updated_silently_but_claude_opened(self):
        st = {"update_pending": {"from": "0.0.1", "to": self.app.VERSION, "mode": "silent", "restart": []},
              "app_version": "0.0.1", "first_run_done": True}
        p = self.app.update_notice(st, LOG)
        self.assertEqual(self.sent[0][1], "分析とレポートは、そのまま残っています。")
        with mock.patch.object(self.app, "AI_LINKS", self.links()), \
                mock.patch.object(self.app.system, "old_tool_processes", return_value=[{"pid": 1, "kind": "mcp", "version": "0.0.1"}]):
            self.app.version_notice(st, LOG, p)
        self.assertEqual(self.app.startup_restarts, [])
        self.assertEqual(self.app.startup_alerts[0]["ok"], "開き直す")   # 入れ替えの間に開いた Claude: 今までどおり窓で

    def test_failed(self):
        self.config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.u.update_log().write_text("swap: 今の .app を動かせない（入れ替えない）\n", encoding="utf-8")
        st = {"update_pending": {"from": self.app.VERSION, "to": "99.0.0", "mode": "silent", "restart": []}}
        self.assertIsNone(self.app.update_notice(st, LOG))
        a = self.app.startup_alerts[0]
        self.assertEqual((a["title"], a["ok"], a["cancel"], a["open"]),
                         ("自動で新しくできませんでした", "手順を開く", "閉じる", self.u.GUIDE_URL))
        self.assertIn("手で入れ替えてください", a["message"])
        f = self.config.load_state()["update"]["failed"]
        self.assertEqual(f["version"], "99.0.0")
        self.assertIn("動かせない", f["why"])
        self.assertEqual(self.sent, [])

    def test_nothing_pending(self):
        self.assertIsNone(self.app.update_notice({}, LOG))
        self.assertEqual((self.sent, self.app.startup_alerts), ([], []))


class TestController(AppCase):
    def setUp(self):
        super().setUp()
        self.ctl = self.app.Controller(self.config.code_dir(), LOG)
        self.staged = {"version": "99.0.0", "app": str(make_app(self.tmp / "staged", "99.0.0")), "note": "直しました"}
        self.ctl.update_staged = self.staged

    def tick(self, why=None, ai=()):
        with mock.patch.object(self.ctl, "update_wait_reason", return_value=why), \
                mock.patch.object(self.ctl, "ai_open", return_value=list(ai)):
            self.ctl._update_tick()

    def test_silent(self):
        self.tick()
        self.assertEqual(self.ctl.update_go, {"mode": "silent", "restart": []})
        self.assertEqual(self.ctl.update_requests, [])

    def test_wait(self):
        self.tick(why="取得", ai=["Claude"])
        self.assertEqual((self.ctl.update_go, self.ctl.update_requests, self.ctl.update_busy), (None, [], "取得"))
        self.ctl.update_manual = True   # メニューから押した: 窓で知らせる
        self.tick(why="取得")
        self.assertEqual(self.ctl.update_requests, [{"kind": "wait", "version": "99.0.0", "why": "取得"}])

    def test_ask_once_a_day(self):
        self.tick(ai=["Claude"])
        self.assertEqual(self.ctl.update_requests,
                         [{"kind": "ask", "version": "99.0.0", "note": "直しました", "ai": ["Claude"]}])
        self.assertEqual(self.config.load_state()["update"]["asked_on"], self.u.today())
        self.tick(ai=["Claude"])
        self.assertEqual(len(self.ctl.update_requests), 1)   # 窓に答えるまで重ねない
        self.assertEqual(self.ctl.update_answer(False), ("later", None))
        self.ctl.update_requests.clear()
        self.tick(ai=["Claude"])
        self.assertEqual(self.ctl.update_requests, [])        # 今日はもう聞かない
        self.tick()                                           # Claude を閉じた
        self.assertEqual(self.ctl.update_go["mode"], "silent")

    def test_answer_now(self):
        with mock.patch.object(self.ctl, "update_wait_reason", return_value="取得"):
            self.assertEqual(self.ctl.update_answer(True), ("wait", "取得"))   # 窓を出している間に取得が始まった
        with mock.patch.object(self.ctl, "update_wait_reason", return_value=None), \
                mock.patch.object(self.ctl, "ai_open", return_value=["Claude"]):
            self.assertEqual(self.ctl.update_answer(True), ("go", {"mode": "now", "restart": ["Claude"]}))

    def test_same_version_installed_by_hand(self):
        self.ctl.update_staged = dict(self.staged, version=self.app.VERSION)
        self.tick()
        self.assertIsNone(self.ctl.update_staged)
        self.assertIsNone(self.ctl.update_go)

    def test_check_update(self):
        gh = FakeGitHub()
        dmgs = self.tmp / "dmgs"
        dmgs.mkdir()
        try:
            with mock.patch.dict(os.environ, {"UGC_UPDATE_API": gh.base + "/releases"}):
                self.ctl.update_staged = None
                self.ctl.update_manual = True
                self.ctl._check_update()   # リリースが無い
                self.assertEqual(self.ctl.update_requests.pop(), {"kind": "latest"})
                gh.add("99.0.0", make_dmg(dmgs, "99.0.0"), note="直しました")
                self.ctl.update_manual = True
                self.ctl._check_update()
                s = self.ctl.update_staged
                self.assertEqual((s["version"], s["note"]), ("99.0.0", "直しました"))
                self.assertEqual(self.config.load_state()["update"]["staged"]["version"], "99.0.0")
                self.assertTrue(self.ctl.update_manual)   # 用意できた: 次の見張りで窓（_update_tick）
                self.assertEqual(self.ctl.update_requests, [])
                gh.fail = True
                self.ctl.update_staged = None
                self.ctl._check_update()
                self.assertEqual(self.ctl.update_requests.pop(), {"kind": "error"})
                self.assertIn("HTTPError", self.config.load_state()["update"]["check_error"])
        finally:
            gh.close()


if __name__ == "__main__":
    unittest.main()
