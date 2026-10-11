"""0.8.0 のアプリ側の直し（docs/PLAN_0_8.md 3-4・第6章）の単体テスト。TikTok にも Claude にも、本物の置き場にも触らない（一時フォルダで）。

  python -m unittest tests.test_app_080

- 道具の処理の版のずれ（mcp_local.LocalHooks.version_check）: 実行ファイルの印が変わった・無くなった・新しい版のアプリが開かれた
- 道具が途中で読む部品の先読み（mcp_local.preload）: 道具の道筋の関数の中の import が全部先に読まれる
- info の about=app の材料（LocalHooks.app_info）・空き容量・最近の問題・2つ目の .app
- 前の版の道具の処理の見分け（system.tool_processes）と、版を上げたときの窓（app.version_notice）・.app が2つのときの窓
- 取得・取り足しをやめる印（app.Controller._handle_cancels）: 取り足しにも効く・済んだ取得は書き換えない
- 通知の記録の文（notify）・直した指示書（prompt_store.edited_prompts）・知識ベースの同梱物（config.sync_shipped_kb）
- 道具の一覧と、ChatGPT のスキル・Code タブの CLAUDE.md の決まり（mcp_proto.ASK_RULE と同じ文）
"""
import ast
import asyncio
import datetime
import importlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector"))
for _name in ("collector-mcp", "collector", "t"):   # 試験の画面に記録を出さない
    logging.getLogger(_name).addHandler(logging.NullHandler())


def _fake_app(folder: Path, name: str, version: str) -> Path:
    """Info.plist と実行ファイルだけの .app（PyInstaller と同じ XML の Info.plist）"""
    app = folder / f"{name}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "UGC Analyzer").write_bytes(b"exe-" + version.encode())
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
        "\t<key>CFBundleName</key>\n\t<string>UGC Analyzer</string>\n"
        f"\t<key>CFBundleShortVersionString</key>\n\t<string>{version}</string>\n</dict>\n</plist>\n", encoding="utf-8")
    return app


class HomeCase(unittest.TestCase):
    """UGC_COLLECTOR_HOME を一時フォルダにして config を読み直す"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ugc-080-"))
        self.env = mock.patch.dict(os.environ, {"UGC_COLLECTOR_HOME": str(self.tmp / "home")})
        self.env.start()
        from collector_app import config
        self.config = importlib.reload(config)
        self.config.setup_env()

    def tearDown(self):
        self.env.stop()
        importlib.reload(self.config)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestVersionCheck(HomeCase):
    def setUp(self):
        super().setUp()
        from collector_app import mcp_local, system
        self.ml, self.system = mcp_local, system
        self.app = _fake_app(self.tmp / "Applications", "UGC Analyzer", "0.8.0")
        self.exe = self.app / "Contents" / "MacOS" / "UGC Analyzer"

    def hooks(self):
        h = self.ml.LocalHooks(exe=self.exe, ai="Claude")
        h.version = "0.8.0"
        return h

    def test_not_frozen_is_none(self):
        self.assertIsNone(self.ml.LocalHooks().version_check())   # 試験・開発（凍結されていない）は見ない

    def test_same_file(self):
        h = self.hooks()
        self.assertIsNone(h.version_check())
        self.assertIsNone(h.version_check())

    def test_replaced(self):
        """.app を置き換えた（実行ファイルが別のファイルになった）→ 新しい版を Info.plist から読んで、開き直しを頼む。一度ずれたらずっと"""
        h = self.hooks()
        shutil.rmtree(self.app)
        _fake_app(self.tmp / "Applications", "UGC Analyzer", "0.8.1")
        t = h.version_check()
        self.assertIn("UGC Analyzer が新しくなりました（0.8.0→0.8.1）", t)
        self.assertIn("Claudeを一度終了（⌘Q）して開き直してください。会話と分析は消えません。", t)
        self.assertNotIn("ChatGPT", t)
        self.assertEqual(h.version_check(), t)

    def test_missing_then_back(self):
        """入れ替えの途中（実行ファイルが無い）は毎回見る。同じファイルが戻れば、ずれていない"""
        h = self.hooks()
        moved = self.tmp / "moved.app"
        self.app.rename(moved)
        self.assertIn("見つかりません", h.version_check())
        moved.rename(self.app)
        self.assertIsNone(h.version_check())

    def test_newer_app_opened(self):
        """実行ファイルはそのままでも、新しい版のアプリ（「UGC Analyzer 2.app」など）が開かれて state.json の版が上がっていれば、ずれ"""
        h = self.hooks()
        self.config.save_state(app_version="0.7.5")   # 古い版が開かれても、ずれではない
        self.assertIsNone(h.version_check())
        self.config.save_state(app_version="0.8.10")
        t = h.version_check()
        self.assertIn("（0.8.0→0.8.10）", t)
        self.assertIn("古い「UGC Analyzer」（0.8.0）をゴミ箱に入れてから", t)   # 開き直しても古い方が起こされるときの出口

    def test_bundle_version(self):
        self.assertEqual(self.system.bundle_version(self.app), "0.8.0")
        self.assertIsNone(self.system.bundle_version(self.tmp / "none.app"))
        self.assertEqual(self.system.bundle_of(self.exe), self.app)
        self.assertIsNone(self.system.bundle_of(self.tmp / "python"))
        self.assertGreater(self.system.version_key("0.8.10"), self.system.version_key("0.8.9"))

    def test_call_returns_text_without_work(self):
        """mcp_proto._call は、版がずれていれば道具の仕事をせずに、その文を返す（エラーとして）"""
        from mcp import Client
        h = self.hooks()
        server = self.ml.build(h)
        shutil.rmtree(self.app)
        _fake_app(self.tmp / "Applications", "UGC Analyzer", "0.8.1")
        import proto_runner

        async def go():
            async with Client(server) as c:
                return await c.call_tool("status", {})
        with mock.patch.object(proto_runner, "status", side_effect=AssertionError("仕事をしてはいけない")):
            r = asyncio.run(go())
        text = "\n".join(getattr(x, "text", "") for x in r.content)
        self.assertTrue(r.is_error)
        self.assertIn("0.8.0→0.8.1", text)

    def test_server_version(self):
        """MCP の serverInfo の版は、この処理の版（前は "app-1"）"""
        from collector_app import VERSION
        server = self.ml.build(self.ml.LocalHooks())
        self.assertEqual(server._lowlevel_server.version, VERSION)   # noqa: SLF001


class TestPreload(HomeCase):
    """.app を置き換えたあと、前の版の道具の処理は、まだ読んでいない部品を読めない（PyInstaller）。
    道具の道筋の関数の中の import が、先読み（mcp_local.preload）で全部読まれているか"""
    FILES = ["proto_runner.py", "flow_w1.py", "kb_update.py", "store.py", "prompt_store.py", "user_settings.py",
             "analysis_info.py", "mcp_proto.py", "scraper.py", "tiktok_lock.py", "analysis/pool.py", "analysis/enrich.py",
             "analysis/attr_cluster.py", *[str(p.relative_to(ROOT)) for p in sorted((ROOT / "acquire").glob("*.py"))],
             "collector/collector_app/mcp_local.py", "collector/collector_app/worker_entry.py",
             "collector/collector_app/jobs.py", "collector/collector_app/config.py", "collector/collector_app/system.py"]
    # 先読みしないもの（理由）
    SKIP = {
        "build_llm_input", "prep_comments", "prep_sample",   # 読むと動き出すスクリプト。道具は別のプロセスで走らせる
        "AppKit", "Foundation", "objc", "UserNotifications",  # メニューのアプリだけ（スリープの知らせ・通知・窓）
        "mcp.server.transport_security",                      # Windows 機のサービス（mcp_proto.attach）だけ
    }

    @staticmethod
    def lazy_imports(path: Path) -> set:
        """関数の中の import（モジュール名。from X import y で y が部品なら X.y）"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = set()

        def visit(node, in_func):
            for child in ast.iter_child_nodes(node):
                f = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if in_func and isinstance(child, ast.Import):
                    out.update(a.name for a in child.names)
                elif in_func and isinstance(child, ast.ImportFrom):
                    base = child.module or ""
                    if child.level:
                        base = "collector_app" + ("." + base if base else "")
                    for a in child.names:
                        sub = f"{base}.{a.name}"
                        try:
                            is_mod = importlib.util.find_spec(sub) is not None
                        except (ImportError, ValueError):
                            is_mod = False
                        out.add(sub if is_mod else base)
                visit(child, f)
        visit(tree, False)
        return out

    def test_every_lazy_import_is_preloaded(self):
        # 新しい Python で、道具の処理の起動と同じ順に読む（この試験のプロセスは、ほかの試験が読んだ部品で埋まっているため）
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from collector_app import config; config.setup_env(); "
                "import log_setup; log_setup._configured = True; "   # scraper が画面（標準出力）に記録を出す設定をしない（main() と同じ）
                "from collector_app import mcp_local; res = mcp_local.preload(); "
                "print(json.dumps({'failed': res['failed'], 'modules': sorted(sys.modules)}))")
        r = subprocess.run([sys.executable, "-c", code, str(ROOT / "collector")], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        res = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(res["failed"], [], "先読みの一覧に、読めない名前がある")
        loaded = set(res["modules"])
        missing = {}
        for rel in self.FILES:
            for name in self.lazy_imports(ROOT / rel):
                if name in self.SKIP or name.split(".")[0] in self.SKIP:
                    continue
                if name not in loaded:
                    missing.setdefault(name, []).append(rel)
        self.assertEqual(missing, {}, "関数の中で読む部品が先読みされていない（mcp_local.PRELOAD に足す）")


    def test_preload_names_in_last_build(self):
        """（作ったものがあるときだけ）先読みの名前が、最後に作った .app に入っている（無い名前は先読みでも読めない）"""
        import zipfile
        b = ROOT / "collector" / "build" / "UGCCollector"
        if not (b / "PYZ-00.toc").exists():
            self.skipTest("collector/build が無い")
        pyz = {x[0] for x in ast.literal_eval((b / "PYZ-00.toc").read_text(encoding="utf-8"))[1]}
        base = {n[:-4].replace("/", ".").removesuffix(".__init__")
                for n in zipfile.ZipFile(b / "base_library.zip").namelist() if n.endswith(".pyc")}
        ext = {name.split("lib-dynload/")[-1].split(".cpython")[0].replace("/", ".")
               for name, _src, kind in ast.literal_eval((b / "COLLECT-00.toc").read_text(encoding="utf-8"))[0]
               if kind == "EXTENSION"}
        have = pyz | base | ext | set(sys.builtin_module_names)
        code = {Path(f).stem if f.endswith(".py") else f for f in self.config.CODE_FILES}   # code/ に .py のまま入れる部品
        from collector_app import mcp_local
        missing = [n for n in mcp_local.PRELOAD
                   if n.split(".")[0] not in code and n not in have and n.split(".")[0] not in {"pool", "attr_cluster", "enrich"}]
        self.assertEqual(missing, [])


class TestAppInfo(HomeCase):
    def setUp(self):
        super().setUp()
        from collector_app import mcp_local
        self.ml = mcp_local

    def write_log(self, name: str, lines: list):
        self.config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        (self.config.LOG_DIR / f"{name}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_recent_problems(self):
        now = time.time()
        ts = lambda sec: datetime.datetime.fromtimestamp(now - sec).strftime("%Y-%m-%d %H:%M:%S") + ",123"  # noqa: E731
        self.write_log("collector", [
            f"{ts(3600 * 30)} WARNING 昨日より前の行",
            f"{ts(600)} INFO    通知の許可: False Error Domain=UNErrorDomain Code=1 \"Notifications are not allowed for this application\"",
            f"{ts(500)} WARNING 取得が止まりました: a1 収集用の Chrome（ポート 9250）に接続できません",
            f"{ts(400)} WARNING 取得が止まりました: a2 収集用の Chrome（ポート 9250）に接続できません",
            f"{ts(300)} WARNING 通知センターは使えない（想定どおり）",
        ])
        self.write_log("mcp", [
            f"{ts(200)} ERROR   [123] 道具の実行で例外: next_task",
            "Traceback (most recent call last):",
            "  File \"x.py\", line 1, in <module>",
            "NameError: name 'x' is not defined",
            f"{ts(100)} INFO    [123] 道具の中身 status: 0.1秒",
        ])
        rows = self.ml.recent_problems(now=now)
        self.assertEqual([r["text"] for r in rows],
                         ["道具の実行で例外: next_task", "取得が止まりました: a2 収集用の Chrome（ポート 9250）に接続できません"])
        self.assertEqual(rows[0]["where"], "AI の道具")
        self.assertEqual(rows[1]["times"], 2)   # 数だけ違う同じ文はまとめる
        self.assertEqual(rows[0]["level"], "ERROR")

    def test_app_info(self):
        apps = self.tmp / "Applications"
        _fake_app(apps, "UGC Analyzer", "0.8.0")
        _fake_app(apps, "UGC Analyzer 2", "0.7.5")
        self.config.save_state(logged_in_at="2026-10-12 10:00:00")
        with mock.patch.dict(os.environ, {"UGC_COLLECTOR_APP_DIRS": str(apps)}), \
                mock.patch.object(self.ml.system, "app_running", side_effect=RuntimeError("壊れた")):
            info = self.ml.LocalHooks().app_info()
        for k in ("app_version_disk", "tool_version", "mismatch", "menu_app_running", "logged_in_at", "login_wanted",
                  "chrome_port_open", "recent_problems", "notifications", "duplicate_apps", "disk_free_gb",
                  "edited_prompts", "knowledge"):
            self.assertIn(k, info)
        self.assertIsNone(info["menu_app_running"])   # 調べられなければ None（落ちない）
        self.assertFalse(info["mismatch"])
        self.assertEqual(info["logged_in_at"], "2026-10-12 10:00:00")
        self.assertIn(info["chrome_port_open"], (True, False))
        self.assertEqual([a["version"] for a in info["duplicate_apps"]], ["0.8.0", "0.7.5"])   # 新しい版から
        self.assertGreater(info["disk_free_gb"], 0)
        self.assertGreater(self.ml.LocalHooks().disk_free_bytes(), 0)
        self.assertIn("スクリプトエディタ", info["notifications"])
        self.assertIn("「通知を許可」をオン", info["notifications"])
        self.assertIn("想定どおり", info["notifications"])
        self.assertEqual(info["edited_prompts"], [])

    def test_one_app_no_duplicates(self):
        apps = self.tmp / "Applications"
        _fake_app(apps, "UGC Analyzer", "0.8.0")
        (apps / "UGC Analyzer の説明.app").mkdir()   # 実行ファイルの無い、名前が似ているだけのもの
        with mock.patch.dict(os.environ, {"UGC_COLLECTOR_APP_DIRS": str(apps)}):
            self.assertEqual(self.ml._duplicate_apps(), [])   # noqa: SLF001


class TestToolProcesses(HomeCase):
    def test_old_and_new(self):
        from collector_app import VERSION, system
        apps = self.tmp / "Applications"
        exe = f"{apps}/UGC Analyzer.app/Contents/MacOS/UGC Analyzer"
        exe2 = f"{apps}/UGC Analyzer 2.app/Contents/MacOS/UGC Analyzer"
        ps = "\n".join([f"  101 {exe} --mcp", f"  102 {exe} --mcp", f"  103 {exe} --wait a1 2", f"  104 {exe}",
                        f"  105 {exe} --mcpx", f"  106 {exe2} --wait a1", f"  107 {exe} --mcp",
                        "  108 /usr/bin/python3 -m collector_app --mcp"]) + "\n"
        d = system.tools_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "101.json").write_text(json.dumps({"version": VERSION, "exe": exe}), encoding="utf-8")       # 新しい版
        (d / "103.json").write_text(json.dumps({"version": "0.7.9", "exe": exe}), encoding="utf-8")       # 前の版
        (d / "107.json").write_text(json.dumps({"version": VERSION, "exe": "/other"}), encoding="utf-8")  # 番号の使い回し
        with mock.patch.dict(os.environ, {"UGC_COLLECTOR_APP_DIRS": str(apps)}), \
                mock.patch.object(system.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ps, "")):
            procs = system.tool_processes()
            old = system.old_tool_processes()
        self.assertEqual([(p["pid"], p["kind"]) for p in procs], [(101, "mcp"), (102, "mcp"), (103, "wait"), (106, "wait"),
                                                                 (107, "mcp")])
        self.assertEqual([p["pid"] for p in old], [102, 103, 106, 107])

    def test_register(self):
        from collector_app import VERSION, system
        system.register_tool("mcp")   # 凍結されていない（試験）ときは置かない
        self.assertFalse((system.tools_dir() / f"{os.getpid()}.json").exists())
        dead = system.tools_dir() / "99999999.json"
        dead.parent.mkdir(parents=True, exist_ok=True)
        dead.write_text("{}", encoding="utf-8")
        with mock.patch.object(self.config, "FROZEN", True):
            system.register_tool("mcp")
        mark = json.loads((system.tools_dir() / f"{os.getpid()}.json").read_text(encoding="utf-8"))
        self.assertEqual((mark["kind"], mark["version"]), ("mcp", VERSION))
        self.assertFalse(dead.exists())   # 落ちた処理が残した印は片付ける


class TestStartupNotices(HomeCase):
    """版を上げたあと初めて開いたとき、前の版の道具が動いていれば窓でも知らせる。.app が2つあれば知らせる"""

    def setUp(self):
        super().setUp()
        from collector_app import app
        self.app = app
        app.startup_alerts.clear()
        self.sent = []
        self.p_send = mock.patch.object(app.notify, "send", lambda t, b: self.sent.append(t))
        self.p_send.start()

    def tearDown(self):
        self.p_send.stop()
        self.app.startup_alerts.clear()
        super().tearDown()

    def links(self, claude_running=True, chatgpt_running=False):
        class Fake:
            def __init__(self, run):
                self.run = run

            def status(self):
                return "connected"

            def running(self):
                return self.run
        return (("claude", "Claude", Fake(claude_running), "x", "running"),
                ("chatgpt", "ChatGPT", Fake(chatgpt_running), "x", "running"))

    def notice(self, st, old, **kw):
        with mock.patch.object(self.app, "AI_LINKS", self.links(**kw)), \
                mock.patch.object(self.app.system, "old_tool_processes", return_value=old):
            self.app.version_notice(st, logging.getLogger("t"))

    def test_old_tools_running(self):
        self.notice({"app_version": "0.0.1", "first_run_done": True}, [{"pid": 1, "kind": "mcp", "version": None}])
        self.assertEqual(len(self.sent), 1)
        a = self.app.startup_alerts[0]
        self.assertEqual(a["title"], f"UGC Analyzer を {self.app.VERSION} にしました")
        self.assertIn("Claude を開き直すと新しい版が使えます（話している途中の会話は消えません）", a["message"])
        self.assertEqual((a["ok"], a["cancel"], a["restart"]), ("開き直す", "あとで", ["Claude"]))
        self.assertEqual(self.config.load_state()["app_version"], self.app.VERSION)

    def test_no_old_tools(self):
        self.notice({"app_version": "0.0.1", "first_run_done": True}, [])
        self.assertEqual(self.app.startup_alerts, [])   # 今までどおり通知だけ
        self.assertEqual(len(self.sent), 1)

    def test_no_ai_running(self):
        self.notice({"app_version": "0.0.1", "first_run_done": True}, [{"pid": 1, "kind": "wait", "version": "0.7.5"}],
                    claude_running=False)
        a = self.app.startup_alerts[0]
        self.assertEqual((a["ok"], a["cancel"], a["restart"]), ("OK", None, []))
        self.assertIn("⌘Q", a["message"])

    def test_same_version_or_first_install(self):
        self.notice({"app_version": self.app.VERSION, "first_run_done": True}, [{"pid": 1, "kind": "mcp", "version": None}])
        self.notice({}, [{"pid": 1, "kind": "mcp", "version": None}])
        self.assertEqual(self.app.startup_alerts, [])
        self.assertEqual(self.sent, [])

    def test_duplicates(self):
        apps = self.tmp / "Applications"
        _fake_app(apps, "UGC Analyzer", "0.7.5")
        with mock.patch.dict(os.environ, {"UGC_COLLECTOR_APP_DIRS": str(apps)}):
            self.app.duplicate_notice(logging.getLogger("t"))
            self.assertEqual(self.app.startup_alerts, [])   # 1つなら何もしない
            _fake_app(apps, "UGC Analyzer 2", "0.8.0")
            self.app.duplicate_notice(logging.getLogger("t"))
        a = self.app.startup_alerts[0]
        self.assertEqual(a["title"], "UGC Analyzer が2つ入っています")
        self.assertIn("・UGC Analyzer 2（0.8.0） ← 新しい版", a["message"])
        self.assertIn("ほか（「UGC Analyzer」）をゴミ箱に入れてください", a["message"])
        self.assertTrue((apps / "UGC Analyzer.app").exists())   # 自動では消さない


class TestCancels(HomeCase):
    """やめる印（cancel-<分析 ID>）: 取得に加えて、掘り下げ・切り直しの取り足しにも効く（2026-10-12）"""

    def setUp(self):
        super().setUp()
        from collector_app import app
        self.app = app
        self.ctl = app.Controller(self.config.code_dir(), logging.getLogger("t"))
        self.proc = subprocess.Popen(["sleep", "60"], start_new_session=True)   # 取得の係の代わり
        self.ctl.worker.proc = self.proc
        self.p_send = mock.patch.object(app.notify, "send", lambda *a, **k: None)
        self.p_send.start()
        from acquire import pipeline   # 一覧（jobs.analyses）は、pipeline を最初に読んだときの置き場を見るので、この試験の置き場に
        self.p_dir = mock.patch.object(pipeline, "ANALYSES_DIR", self.config.ANALYSES_DIR)
        self.p_dir.start()

    def tearDown(self):
        self.p_send.stop()
        self.p_dir.stop()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        super().tearDown()

    def put(self, aid: str, m: dict, flag: bool = True):
        d = self.config.ANALYSES_DIR / aid
        d.mkdir(parents=True, exist_ok=True)
        (d / "analysis.json").write_text(json.dumps({"analysis_id": aid, "title": "試験", **m}), encoding="utf-8")
        if flag:
            (self.config.LOCK_DIR / f"cancel-{aid}").write_text("1", encoding="utf-8")

    def get(self, aid: str) -> dict:
        return json.loads((self.config.ANALYSES_DIR / aid / "analysis.json").read_text(encoding="utf-8"))

    def test_deepen_running(self):
        """取り足しを取っている係を止め、取得（done）はそのまま、取り足しは「やめた」"""
        self.put("a1", {"acquisition": {"status": "done", "pid": self.proc.pid},
                        "deepen": {"round": 2, "status": "cancelled", "cancelled_at": "2026-10-12T10:00:00+09:00",
                                   "cancel_reason": "利用者がやめた", "pid": self.proc.pid}})
        self.ctl._handle_cancels()
        self.assertIsNotNone(self.proc.wait(10))   # 係が止まった
        m = self.get("a1")
        self.assertEqual(m["acquisition"]["status"], "done")
        self.assertEqual(m["deepen"]["status"], "cancelled")
        self.assertEqual(m["deepen"]["cancelled_at"], "2026-10-12T10:00:00+09:00")
        self.assertEqual(self.ctl.known_deepen[("a1", 2)], "cancelled")
        self.assertFalse((self.config.LOCK_DIR / "cancel-a1").exists())

    def test_deepen_overwritten_by_worker(self):
        """係が最後に deepen を書き戻して cancelled を上書きしていたら、cancelled に戻す（係はもうほかを取っている）"""
        self.put("a1", {"acquisition": {"status": "done", "pid": 1},
                        "deepen": {"round": 1, "status": "done", "cancelled_at": "2026-10-12T10:00:00+09:00",
                                   "cancel_reason": "利用者がやめた", "pid": 1, "result": {"videos_new": 3}}})
        self.ctl._handle_cancels()
        self.assertIsNone(self.proc.poll())   # 係は止めない（この分析を取っていない）
        m = self.get("a1")
        self.assertEqual((m["acquisition"]["status"], m["deepen"]["status"]), ("done", "cancelled"))
        self.assertEqual(m["deepen"]["result"], {"videos_new": 3})   # 係が書いたほかの項目は残す

    def test_deepen_running_by_worker_wrote_running(self):
        """道具が cancelled を書いた直後に係が running を書いた（cancelled_at は残る）→ 係を止めて cancelled に"""
        self.put("a1", {"acquisition": {"status": "done", "pid": 1},
                        "deepen": {"round": 3, "status": "running", "cancelled_at": "2026-10-12T10:00:00+09:00",
                                   "pid": self.proc.pid}})
        self.ctl._handle_cancels()
        self.assertIsNotNone(self.proc.wait(10))
        self.assertEqual(self.get("a1")["deepen"]["status"], "cancelled")

    def test_done_acquisition_untouched(self):
        """取得が済んだ分析に残った印（取り足しも無い）: 取得の状態は書き換えず、係も止めない"""
        self.put("a1", {"acquisition": {"status": "done", "pid": self.proc.pid}})
        self.ctl._handle_cancels()
        self.assertIsNone(self.proc.poll())
        self.assertEqual(self.get("a1")["acquisition"]["status"], "done")
        self.assertFalse((self.config.LOCK_DIR / "cancel-a1").exists())

    def test_acquisition_cancel_overwritten_by_done(self):
        """道具がやめると書いた直後に、係が取り終えて done で上書きした（cancelled_at は残る）→ 係を止めて「やめた」に戻す"""
        self.put("a1", {"acquisition": {"status": "done", "pid": self.proc.pid, "cancelled_at": "2026-10-12T10:00:00+09:00",
                                        "cancel_reason": "利用者がやめた", "finished_at": "2026-10-12T10:00:01+09:00"}})
        self.ctl._handle_cancels()
        self.assertIsNotNone(self.proc.wait(10))
        self.assertEqual(self.get("a1")["acquisition"]["status"], "cancelled")
        # 前にやめて、そのあと取り直して取り終えた分析（cancelled_at が古い）は、done のまま
        self.put("a2", {"acquisition": {"status": "done", "pid": 1, "cancelled_at": "2026-10-11T10:00:00+09:00",
                                        "requeued_at": "2026-10-11T11:00:00+09:00"}})
        self.ctl._handle_cancels()
        self.assertEqual(self.get("a2")["acquisition"]["status"], "done")

    def test_worker_busy_with_other(self):
        """取り足しの PID が係でも、係がもうほかの分析を取っていれば止めない"""
        self.put("a1", {"acquisition": {"status": "done", "pid": 1},
                        "deepen": {"round": 1, "status": "cancelled", "cancelled_at": "x", "pid": self.proc.pid}})
        self.put("a2", {"acquisition": {"status": "running", "pid": self.proc.pid}}, flag=False)
        self.ctl._handle_cancels()
        self.assertIsNone(self.proc.poll())
        self.assertEqual(self.get("a1")["deepen"]["status"], "cancelled")
        self.assertEqual(self.get("a2")["acquisition"]["status"], "running")

    def test_acquisition_running(self):
        """取得中の取得をやめる（今までどおり）"""
        self.put("a1", {"acquisition": {"status": "running", "pid": self.proc.pid}})
        self.ctl._handle_cancels()
        self.assertIsNotNone(self.proc.wait(10))
        self.assertEqual(self.get("a1")["acquisition"]["status"], "cancelled")


class TestNotifyLog(unittest.TestCase):
    def test_answer_text(self):
        from collector_app import notify

        class Err:
            def domain(self):
                return "UNErrorDomain"

            def code(self):
                return 1

            def __str__(self):
                return "Notifications are not allowed for this application"
        t = notify.answer_text(False, Err())
        self.assertIn("通知センターは使えない（署名していないアプリのため。想定どおり", t)
        self.assertIn("スクリプトエディタの名前で出す", t)
        self.assertIn("UNErrorDomain 1", t)
        self.assertNotIn("not allowed", t)   # 読み違えのもとになった英語の文は書かない
        self.assertIn("許可されました", notify.answer_text(True, None))

    def test_send_logs_each(self):
        from collector_app import notify
        log = logging.getLogger("t-notify")
        with mock.patch.object(notify, "_log", log), mock.patch.object(notify, "_center", None), \
                mock.patch.object(notify.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                self.assertLogs(log, level="INFO") as cm:
            notify.send("「試験」の取得が終わりました", "本文")
        self.assertIn("通知（osascript・スクリプトエディタの名前）: 「試験」の取得が終わりました", cm.output[0])


class TestEditedPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"UGC_PROMPTS_DIR": self.tmp})
        self.env.start()
        import prompt_store
        self.ps = importlib.reload(prompt_store)
        self.ps.seed()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_edited_and_outdated(self):
        self.assertEqual(self.ps.edited_prompts(), [])
        p = Path(self.tmp) / "w1" / "axes.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n- 追記: 推し活の界隈を分けて\n", encoding="utf-8")
        rows = self.ps.edited_prompts()
        self.assertEqual([(r["name"], r["default_changed"], r["ok"]) for r in rows], [("axes.md", False, True)])
        self.assertTrue(rows[0]["edited_at"])
        self.assertEqual(self.ps.outdated_user_prompts(), [])
        # アプリの版で初期の指示書が変わった（直した時点の初期の印と、今の初期の本文が違う）
        man_p = Path(self.tmp) / "w1" / self.ps.MANIFEST
        man = json.loads(man_p.read_text(encoding="utf-8"))
        man["axes.md"] = "0" * 64
        man_p.write_text(json.dumps(man), encoding="utf-8")
        self.assertEqual([(r["name"], r["default_changed"]) for r in self.ps.outdated_user_prompts()], [("axes.md", True)])
        self.assertEqual(self.ps.seed()["kept_edited"], ["axes.md"])   # 版を上げても直したものは残し、印も書き換えない
        self.assertTrue(self.ps.outdated_user_prompts()[0]["default_changed"])
        man_p.unlink()   # 印が無い → 分からない
        self.assertEqual([(r["name"], r["default_changed"]) for r in self.ps.outdated_user_prompts()], [("axes.md", None)])


class TestShippedKnowledge(unittest.TestCase):
    """版を上げたとき、運営の同梱物（記事・一覧・用語集・文体ガイド・README・カード）を既存の Mac にも届ける。Mac で足した分は残す"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "ship"
        self.kb = self.tmp / "kb"
        self.env = mock.patch.dict(os.environ, {"UGC_KB_DIR": str(self.kb)})
        self.env.start()
        from collector_app import config
        self.config = config
        (self.src / "notes").mkdir(parents=True)
        (self.src / "distilled").mkdir()
        self.ship(1)
        # 最初の写し（0.7.5 までの seed_data と同じ: 記事・一覧・蒸留物）
        shutil.copytree(self.src / "notes", self.kb / "notes")
        shutil.copytree(self.src / "distilled", self.kb / "distilled")
        shutil.copy2(self.src / "INDEX.json", self.kb / "INDEX.json")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ship(self, v: int):
        """運営の同梱物の第 v 版（記事は v 本・カードも v 本）"""
        for i in range(1, v + 1):
            (self.src / "notes" / f"2026-01-0{i}_n{i}.md").write_text(f"# 記事{i}\n", encoding="utf-8")
        (self.src / "INDEX.json").write_text(json.dumps([{"key": f"n{i}", "file": f"2026-01-0{i}_n{i}.md"}
                                                         for i in range(1, v + 1)]), encoding="utf-8")
        d = self.src / "distilled"
        for n in ("README.md", "STYLE_GUIDE.md", "GLOSSARY.md"):
            (d / n).write_text(f"# {n} 第{v}版\n", encoding="utf-8")
        (d / "cards.jsonl").write_text("".join(json.dumps({"file": f"2026-01-0{i}_n{i}.md"}) + "\n" for i in range(1, v + 1)),
                                       encoding="utf-8")
        st = time.time() + v   # 名前・大きさ・時刻の印が版ごとに変わるように
        for p in self.src.rglob("*"):
            if p.is_file():
                os.utime(p, (st, st))

    def mac_adds(self):
        """この Mac で kb_update が足したもの: note の新着の記事・一覧・カード・取り込みの記録"""
        (self.kb / "notes" / "2026-10-01_nmac.md").write_text("# Mac で取った記事\n", encoding="utf-8")
        idx = json.loads((self.kb / "INDEX.json").read_text(encoding="utf-8"))
        idx.append({"key": "nmac", "file": "2026-10-01_nmac.md", "added_at": "2026-10-01T10:00:00+09:00"})
        (self.kb / "INDEX.json").write_text(json.dumps(idx), encoding="utf-8")
        with open(self.kb / "distilled" / "cards.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"file": "2026-10-01_nmac.md"}) + "\n")
        (self.kb / "update_state.json").write_text(json.dumps({"ingested": [{"key": "nmac", "file": "2026-10-01_nmac.md"}]}),
                                                   encoding="utf-8")

    def files_of(self, rel: str) -> list:
        return [json.loads(x)["file"] for x in (self.kb / rel).read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_first_sync_keeps_mac_adds(self):
        self.mac_adds()
        self.ship(2)
        done = self.config.sync_shipped_kb(self.src, self.kb)
        self.assertEqual(sorted(done["replaced"]), ["GLOSSARY.md", "README.md", "STYLE_GUIDE.md"])
        self.assertEqual(len(done["backup"]), 3)   # 記録の無い最初の1回は、入れ替える前の中身を残す
        self.assertEqual((self.kb / "distilled" / "GLOSSARY.md").read_text(encoding="utf-8"), "# GLOSSARY.md 第2版\n")
        self.assertTrue((self.kb / "notes" / "2026-01-02_n2.md").exists())
        self.assertTrue((self.kb / "notes" / "2026-10-01_nmac.md").exists())
        idx = json.loads((self.kb / "INDEX.json").read_text(encoding="utf-8"))
        self.assertEqual([x["key"] for x in idx], ["n1", "n2", "nmac"])   # 運営の一覧＋Mac で足した記事
        self.assertEqual(self.files_of("distilled/cards.jsonl"),
                         ["2026-01-01_n1.md", "2026-01-02_n2.md", "2026-10-01_nmac.md"])
        self.assertEqual(self.config.sync_shipped_kb(self.src, self.kb), {})   # 同じ同梱物なら何もしない

    def test_merged_glossary_is_kept(self):
        """整理の仕事で用語集を書き直した Mac は、用語集だけそのまま（ほかは届ける）"""
        g = self.kb / "distilled" / "GLOSSARY.md"
        g.write_text("# 用語集（この Mac で整理した）\n", encoding="utf-8")
        (self.kb / "distilled" / "GLOSSARY.md.bak-20261005-101010").write_text("# 前の用語集\n", encoding="utf-8")
        self.ship(2)
        done = self.config.sync_shipped_kb(self.src, self.kb)
        self.assertEqual(done["kept"], ["GLOSSARY.md"])
        self.assertEqual(g.read_text(encoding="utf-8"), "# 用語集（この Mac で整理した）\n")
        self.assertEqual((self.kb / "distilled" / "STYLE_GUIDE.md").read_text(encoding="utf-8"), "# STYLE_GUIDE.md 第2版\n")

    def test_later_sync_by_record(self):
        """2回目からは届けたときの印で見分ける: 印と同じなら入れ替え、Mac で書き換えたものは残す"""
        self.config.sync_shipped_kb(self.src, self.kb)   # 記録を作る（中身は同じ）
        (self.kb / "distilled" / "STYLE_GUIDE.md").write_text("# 利用者が手で直した\n", encoding="utf-8")
        self.ship(3)
        done = self.config.sync_shipped_kb(self.src, self.kb)
        self.assertEqual(sorted(done["replaced"]), ["GLOSSARY.md", "README.md"])
        self.assertEqual(done["kept"], ["STYLE_GUIDE.md"])
        self.assertNotIn("backup", done)   # 記録どおりのものを入れ替えるときは控えを作らない
        self.assertEqual((self.kb / "distilled" / "STYLE_GUIDE.md").read_text(encoding="utf-8"), "# 利用者が手で直した\n")
        mark = json.loads((self.kb / self.config.SHIPPED_MARK).read_text(encoding="utf-8"))
        self.assertEqual(mark["kept"], ["STYLE_GUIDE.md"])


class TestToolLists(unittest.TestCase):
    """道具の一覧（ChatGPT の道具ごとの許可・スキル）と、質問とやり直しの決まり（mcp_proto.ASK_RULE と同じ文）"""

    def test_tools_and_rules(self):
        import mcp_proto
        from collector_app import code_link, codex_link
        for t in ("info", "redo", "archive_analysis"):
            self.assertIn(t, codex_link.TOOLS)
            self.assertIn(f"`{t}`", codex_link.SKILL_MD)
        self.assertEqual(len(codex_link.TOOLS), len(set(codex_link.TOOLS)))
        listed = codex_link.SKILL_MD.split("を探す: ", 1)[1].split("\n", 1)[0].rstrip("。").split("・")
        self.assertEqual(listed, list(codex_link.TOOLS))   # スキルの道具の一覧も同じ並び
        self.assertEqual(codex_link.ASK_RULE, mcp_proto.ASK_RULE)
        self.assertIn(mcp_proto.ASK_RULE, codex_link.SKILL_MD)
        self.assertIn(mcp_proto.ASK_RULE, code_link.CLAUDE_MD)
        self.assertIn(mcp_proto.ASK_RULE, mcp_proto.INSTRUCTIONS)
        wait = code_link.CLAUDE_MD.split("## 待つところ", 1)[1]
        self.assertIn("restart_analysis（TikTok から集め直す）は、集め直す間の待ちを返す", wait)
        self.assertIn("redo（集めたデータのまま AI の作業を段からやり直す）は待ちを返さない", wait)
        self.assertIn("そのまま next_task を続ける", wait)


if __name__ == "__main__":
    unittest.main()
