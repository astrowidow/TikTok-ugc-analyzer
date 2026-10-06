"""友達の運用評価（2026-10-06）の直し・0.6.6。

- 「開き直す」を押しても何も起きない: Claude は作業中なら終了の前に確認の窓を出す。前に出してから終了を頼み、閉じるまで見張って開き直す
  （collector_app/system.py の relaunch_app、app.py の _restart_ai）
- 開始の返事が途中で途切れた: status の返事に取っている楽曲ページを出し、AI への指示に「途切れたら呼び直さず status を見る」
  （proto_runner.status・_acq_pages、mcp_proto.START_CUT_RULE）。道具ごとの時間を記録に残す
- 完了の知らせの「ウェブ検索をオンにして」: 新しい Claude の画面には切り替えが無いので、あれば、の言い方に（flow_w1.py）
- ローカルネットワークの窓に理由の文を出す（UGCCollector.spec の NSLocalNetworkUsageDescription）

  collector/.venv/bin/python -m unittest tests.test_friend_trial -v

Claude・ChatGPT・Chrome・TikTok・本物の置き場には触らない（osascript・open は差し替える）。
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UGC_ANALYSES_DIR", tempfile.mkdtemp())
from collector_app import system  # noqa: E402

AID = "a20990101-0000-frnd"
REAL_SLEEP = __import__("time").sleep


class FakeApp:
    """終了を頼まれてから、何回目の「動いているか」で閉じるか（None なら閉じない）"""

    def __init__(self, close_after=None, running=True):
        self.close_after = close_after
        self.alive = running
        self.quit_asked = False
        self.checks = 0
        self.calls = []

    def running(self):
        if self.quit_asked and self.close_after is not None:
            self.checks += 1
            if self.checks > self.close_after:
                self.alive = False
        return self.alive

    def run(self, args, **kw):
        self.calls.append(args)
        if args[0] == "/usr/bin/osascript":
            self.quit_asked = True
        return mock.Mock(returncode=0, stdout="", stderr="")


class TestRelaunch(unittest.TestCase):
    def go(self, app, wait_max=0.3, ask_after=0.1):
        asked = []
        with mock.patch.object(system.subprocess, "run", app.run), \
                mock.patch.object(system, "QUIT_ASK_AFTER", ask_after), \
                mock.patch.object(system.time, "sleep", lambda s: REAL_SLEEP(0.01)):
            ok = system.relaunch_app("Claude", app.running, lambda: asked.append(1), wait_max=wait_max)
        return ok, asked

    def test_brings_to_front_before_quit(self):
        """終了の確認の窓は Claude の窓にくっついて出るので、終了を頼む前に Claude を前に出す"""
        app = FakeApp(close_after=1)
        ok, asked = self.go(app)
        self.assertTrue(ok)
        osa = app.calls[0]
        self.assertEqual(osa[0], "/usr/bin/osascript")
        script = " ".join(osa)
        self.assertLess(script.index('tell application "Claude" to activate'), script.index('tell application "Claude" to quit'))
        self.assertEqual(app.calls[-1], ["/usr/bin/open", "-a", "Claude"])
        self.assertEqual(asked, [])   # すぐ閉じたので案内は出さない

    def test_waits_for_the_answer_and_reopens(self):
        """確認の窓に答えてもらうまで（前の作りの30秒より長く）待ち、案内を1回だけ出して、閉じたら開き直す"""
        app = FakeApp(close_after=40)   # 0.01秒 × 40回ほどで閉じる（QUIT_ASK_AFTER の 0.1秒は過ぎる）
        ok, asked = self.go(app, wait_max=5)
        self.assertTrue(ok)
        self.assertEqual(asked, [1])
        self.assertEqual(app.calls[-1], ["/usr/bin/open", "-a", "Claude"])

    def test_gives_up_without_reopening(self):
        """閉じなければ（「キャンセル」を押した）開き直さずに False"""
        app = FakeApp(close_after=None)
        ok, asked = self.go(app, wait_max=0.3)
        self.assertFalse(ok)
        self.assertEqual(asked, [1])
        self.assertNotIn(["/usr/bin/open", "-a", "Claude"], app.calls)

    def test_not_running_just_opens(self):
        app = FakeApp(running=False)
        ok, asked = self.go(app)
        self.assertTrue(ok)
        self.assertEqual(app.calls, [["/usr/bin/open", "-a", "Claude"]])

    def test_default_wait_is_long_enough(self):
        """「Claudeの作業完了を待つ」を選んでも開き直せるよう、待つ上限は数分以上"""
        self.assertGreaterEqual(system.QUIT_WAIT_MAX, 300)

    def test_links_use_relaunch(self):
        from collector_app import claude_link, codex_link
        seen = []
        with mock.patch.object(system, "relaunch_app", lambda name, running, on_asking=None: seen.append((name, running)) or True):
            claude_link.restart_claude()
            codex_link.restart_chatgpt()
        self.assertEqual([(n, r.__name__) for n, r in seen], [("Claude", "claude_running"), ("ChatGPT", "chatgpt_running")])


class TestRestartFromMenu(unittest.TestCase):
    def setUp(self):
        from collector_app import app
        self.app_mod = app
        self.obj = app.CollectorApp.__new__(app.CollectorApp)
        self.obj._restarting = set()
        self.obj._restart_lock = threading.Lock()
        self.sent, self.alerts = [], []

    def run_restart(self, restart):
        with mock.patch.object(self.app_mod.notify, "send", lambda t, b: self.sent.append((t, b))), \
                mock.patch.object(self.app_mod.AppHelper, "callAfter", lambda f, *a, **k: self.alerts.append(a)):
            self.obj._restart_ai("Claude", restart, "「このまま終了」", "あとの一言")

    def test_success_notifies(self):
        def restart(on_asking=None):
            on_asking()
            return True
        self.run_restart(restart)
        self.assertIn("「このまま終了」", self.sent[0][1])          # 確認の窓で押すボタン
        self.assertEqual(self.sent[-1][0], "Claude を開き直しました")
        self.assertEqual(self.alerts, [])

    def test_failure_shows_window_not_notification(self):
        """閉じられなかったら、見落としやすい通知でなく窓で出す（画面のスレッドで）"""
        self.run_restart(lambda on_asking=None: False)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("閉じられませんでした", self.alerts[0][0])
        self.assertIn("⌘Q", self.alerts[0][1])

    def test_no_double_restart(self):
        """開き直しの最中にもう一度押しても、2つめは走らせない"""
        self.obj._restarting.add("Claude")
        called = []
        self.run_restart(lambda on_asking=None: called.append(1) or True)
        self.assertEqual(called, [])
        self.assertEqual(self.sent, [])


class TestStatusShowsPages(unittest.TestCase):
    def setUp(self):
        import proto_runner as pr
        self.pr = pr
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, pr._acq_view, pr.LOCAL)
        pr.ANALYSES_DIR = Path(self.tmp.name)
        pr.LOCAL = None
        d = pr.ANALYSES_DIR / AID
        (d / "raw").mkdir(parents=True)
        self.u1 = "https://www.tiktok.com/music/%E3%81%A6%E3%81%99%E3%81%A8-7000000000000000001"
        self.u2 = "https://www.tiktok.com/music/%E3%81%A6%E3%81%99%E3%81%A8-7000000000000000002"
        (d / "analysis.json").write_text(json.dumps({
            "analysis_id": AID, "owner": "local", "title": "てすと", "song": {"title": "てすと", "artist": "だれか"},
            "music_url": self.u1, "music_urls": [self.u1, self.u2],
            "acquisition": {"status": "running", "step": "list"}}, ensure_ascii=False), encoding="utf-8")
        (d / "raw" / "music_pages.json").write_text(json.dumps([
            {"url": self.u1, "title": "てすと", "creator": "だれか", "video_count_text": "31.2K 動画", "video_count": 31200},
            {"url": self.u2, "title": "てすと（sped up）", "creator": "だれか", "video_count_text": "17.7K 動画", "video_count": 17700},
        ], ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.pr.ANALYSES_DIR, self.pr._acq_view, self.pr.LOCAL = self.saved
        self.tmp.cleanup()

    def test_acquiring_shows_pages(self):
        """開始の返事が途切れても、status から楽曲ページ（題・作者・UGC 数・URL）を伝えられる"""
        self.pr._acq_view = lambda a: {"state": "acquiring", "message": "あなたの Mac で取得中（動画一覧を集める）。", "eta_seconds": 3600}
        r = self.pr.status("local")
        self.assertIn("『てすと／だれか』（UGC 31.2K 動画）" + self.u1, r["text"])
        self.assertIn("『てすと（sped up）／だれか』（UGC 17.7K 動画）" + self.u2, r["text"])
        self.assertEqual([p["url"] for p in r["analyses"][0]["pages"]], [self.u1, self.u2])

    def test_page_without_record_still_shows_url(self):
        (self.pr.ANALYSES_DIR / AID / "raw" / "music_pages.json").unlink()
        self.pr._acq_view = lambda a: {"state": "acquiring", "message": "取得中。", "eta_seconds": None}
        r = self.pr.status("local")
        self.assertIn("（UGC 読めず）" + self.u1, r["text"])

    def test_cancelled_has_no_pages(self):
        self.pr._acq_view = lambda a: {"state": "cancelled", "message": "この取得はやめました。", "eta_seconds": None}
        r = self.pr.status("local")
        self.assertNotIn("取っている楽曲ページ", r["text"])


class TestStartCutRule(unittest.TestCase):
    def test_rule_in_instructions_and_start_tool(self):
        import mcp_proto
        self.assertIn(mcp_proto.START_CUT_RULE, mcp_proto.INSTRUCTIONS)
        self.assertIn("呼び直さずに status を見る", mcp_proto.START_CUT_RULE)
        srv = mcp_proto._build_server(user_of=lambda ctx: "local", local=True)
        descs = {t.name: t.description for t in asyncio.run(srv.list_tools())}
        self.assertIn(mcp_proto.START_CUT_RULE, descs["start_analysis"])

    def test_tool_time_is_logged(self):
        """道具ごとにかかった時間を記録に残す（Claude のチャットは道具1回 約60秒まで）"""
        import mcp_proto
        srv = mcp_proto._build_server(user_of=lambda ctx: "local", local=True)
        with self.assertLogs("mcp", level=logging.INFO) as cm, \
                mock.patch.object(mcp_proto.runner, "status", lambda user, ref=None: {"text": "なし", "analyses": []}):
            asyncio.run(srv.call_tool("status", {}))
        self.assertTrue(any("道具の中身 <lambda>" in line or "道具の中身 status" in line for line in cm.output), cm.output)


class TestWebSearchWording(unittest.TestCase):
    def test_no_toggle_assumed(self):
        """新しい Claude の画面にはウェブ検索の切り替えが無い（公式ヘルプ）。「オンにして」と決めつけない"""
        src = (ROOT / "flow_w1.py").read_text(encoding="utf-8")
        self.assertNotIn("ウェブ検索をオンにして", src)
        self.assertEqual(src.count("「＋」に「ウェブ検索」があれば"), 2)


class TestLocalNetworkReason(unittest.TestCase):
    def test_reason_in_info_plist(self):
        spec = (ROOT / "collector" / "UGCCollector.spec").read_text(encoding="utf-8")
        self.assertIn('"NSLocalNetworkUsageDescription"', spec)
        self.assertIn("「許可」を押してください", spec)


if __name__ == "__main__":
    unittest.main()
