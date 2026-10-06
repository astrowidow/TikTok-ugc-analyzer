"""Claude の Code タブで一気に回す（docs/CODE_TAB_ONE_SITTING.md）: 待つ命令（waiter）と作業フォルダ（code_link）"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
from collector_app import code_link, config, waiter  # noqa: E402


class TestWaiter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE)
        config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR = base / "analyses", base / "app.pid", base / "locks"
        # 本物の state.json を読まない（この Mac がログイン済みかどうかで結果が変わらないように。2026-10-06 通し試験）
        config.STATE_FILE = base / "state.json"
        config.STATE_FILE.write_text(json.dumps({"logged_in_at": "2026-10-06 00:00:00"}), encoding="utf-8")
        config.LOCK_DIR.mkdir(parents=True)
        config.PID_FILE.write_text(str(os.getpid()))   # アプリが動いている扱い
        self.d = config.ANALYSES_DIR / "a20990101-0000-wait"
        self.d.mkdir(parents=True)

    def tearDown(self):
        config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE = self.saved
        self.tmp.cleanup()

    def meta(self, acq="done", deepen=None):
        m = {"analysis_id": self.d.name, "title": "テスト曲", "acquisition": {"status": acq}}
        if deepen:
            m["deepen"] = deepen
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def run_wait(self, ref=None):
        return waiter.wait(ref or self.d.name, limit_h=0.0003, poll_s=0.01, awake=False)

    def test_done_and_busy(self):
        self.meta("done")
        code, line = self.run_wait()
        self.assertEqual(code, 0)
        self.assertIn("終わりました", line)
        self.assertIn("next_task", line)
        self.meta("running")
        self.assertEqual(self.run_wait()[0], 3)               # 上限まで待って「まだ終わっていません」
        self.meta("blocked")
        self.assertEqual(self.run_wait()[0], 3)               # 止まっても、アプリが取り直すあいだは待つ

    def test_deepen_and_recut(self):
        self.meta("done", {"status": "running", "kind": "recut"})
        code, line = self.run_wait()
        self.assertEqual(code, 3)
        self.assertIn("切り直した界隈の取り足し", line)
        self.meta("done", {"status": "done"})
        code, line = self.run_wait()
        self.assertEqual(code, 0)
        self.assertIn("掘り下げの取り足し", line)

    def test_stops(self):
        self.meta("cancelled")
        self.assertEqual(self.run_wait()[0], 1)
        self.meta("running")
        (config.LOCK_DIR / "need_login.json").write_text("{}")
        code, line = self.run_wait()
        self.assertEqual(code, 2)
        self.assertIn("サブアカウント", line)
        (config.LOCK_DIR / "need_login.json").unlink()
        config.PID_FILE.write_text("999999")                  # アプリが動いていない
        code, line = self.run_wait()
        self.assertEqual(code, 1)
        self.assertIn("動いていません", line)

    def test_find_by_title(self):
        self.meta("running")
        self.assertEqual(waiter.find("テスト"), self.d)
        self.assertIsNone(waiter.find("無い曲"))
        self.assertEqual(waiter.wait("無い曲", awake=False)[0], 1)


class TestCodeFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = code_link.FOLDER
        code_link.FOLDER = Path(self.tmp.name) / "UGC Analyzer"

    def tearDown(self):
        code_link.FOLDER = self.saved
        self.tmp.cleanup()

    def test_ensure(self):
        self.assertEqual(code_link.status(), "outdated")
        changed = code_link.ensure()
        self.assertEqual(sorted(changed), [".claude/settings.local.json", ".mcp.json", "CLAUDE.md", "ugc-wait"])
        self.assertEqual(code_link.status(), "connected")
        self.assertEqual(code_link.ensure(), [])                # 二度目は何も書き換えない
        f = code_link.FOLDER
        mcp = json.loads((f / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["ugc-analyzer"]
        self.assertIn("--mcp", mcp["args"])
        script = (f / "ugc-wait").read_text(encoding="utf-8")
        self.assertIn("--wait", script)
        self.assertNotIn("--mcp", script)
        self.assertTrue(os.access(f / "ugc-wait", os.X_OK))
        self.assertIn("run_in_background", (f / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_keeps_users_permissions(self):
        p = code_link.FOLDER / ".claude" / "settings.local.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "model": "x"}), encoding="utf-8")
        code_link.ensure()
        s = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("Bash(ls:*)", s["permissions"]["allow"])
        self.assertIn("mcp__ugc-analyzer", s["permissions"]["allow"])
        self.assertEqual(s["model"], "x")
        self.assertEqual(s["enabledMcpjsonServers"], ["ugc-analyzer"])


if __name__ == "__main__":
    unittest.main()
