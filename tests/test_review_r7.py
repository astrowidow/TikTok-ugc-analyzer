"""通し試験の読み合わせ・領域 R7（0.6.0 Claude の Code タブで、頼んでから完成まで一気に回す）。

待つ命令（collector_app/waiter.py）・作業フォルダ（collector_app/code_link.py）・Code タブだけの一文（mcp_proto.py の _waiting_text）
について、読み合わせで見つけた疑いを確かめる。どれも「正しい動き」を期待する形で書いてあり、不具合があれば失敗する。
不具合の疑いの試験は名前に「_bug」を付けた（失敗するのが今の姿）。それ以外は大事な分岐が今のとおり動くことの確かめ。

  cd <リポジトリ> && collector/.venv/bin/python -m unittest tests.test_review_r7 -v

本物の置き場（~/Library/Application Support/UGC Analyzer）・本物の ~/UGC Analyzer・Claude と Codex の設定・ネットワーク・
TikTok・Chrome には触らない（一時フォルダに差し替える）。caffeinate も起こさない（subprocess.Popen を差し替える）。
"""
import asyncio
import contextlib
import datetime
import io
import json
import time
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT))
from collector_app import claude_link, code_link, config, waiter  # noqa: E402

AID = "a20990101-0000-r7aa"
# CLAUDE.md の「終わった知らせが来たら、出力の1行を見る」に挙がっている言葉
CLAUDE_MD_WORDS = ("終わりました", "ログイン待ち", "止まりました", "やめてあります", "まだ終わっていません")
STOP_WORDS = ("止まりました", "やめてあります", "ログイン待ち")


def _write_atomic(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + ".tmp-test")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 待つ命令（waiter）
# ---------------------------------------------------------------------------
class _WaiterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE)
        config.ANALYSES_DIR, config.PID_FILE = base / "analyses", base / "app.pid"
        config.LOCK_DIR, config.STATE_FILE = base / "locks", base / "state.json"
        config.LOCK_DIR.mkdir(parents=True)
        config.ANALYSES_DIR.mkdir(parents=True)
        config.PID_FILE.write_text(str(os.getpid()))   # アプリが動いている扱い
        config.STATE_FILE.write_text(json.dumps({"logged_in_at": "2026-10-06 08:00:00"}), encoding="utf-8")   # ログイン済み
        # caffeinate だけを本当に起こさない（ほかの Popen、アプリが動いているかを見る ps などは本物のまま）
        real_popen = subprocess.Popen

        def popen(cmd, *a, **k):
            if cmd and str(cmd[0]).endswith("caffeinate"):
                return mock.MagicMock()
            return real_popen(cmd, *a, **k)
        self._popen = mock.patch.object(waiter.subprocess, "Popen", side_effect=popen)
        self.popen = self._popen.start()

    def tearDown(self):
        self._popen.stop()
        config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE = self.saved
        self.tmp.cleanup()

    def make(self, aid=AID, title="テスト曲", acq="running", deepen=None, **extra) -> Path:
        d = config.ANALYSES_DIR / aid
        d.mkdir(parents=True, exist_ok=True)
        m = {"analysis_id": aid, "title": title, "owner": "local",
             "acquisition": {"status": acq} if isinstance(acq, str) else acq}
        if deepen:
            m["deepen"] = deepen
        m.update(extra)
        _write_atomic(d / "analysis.json", m)
        return d

    def run_wait(self, ref=AID, limit_h=0.0003, poll_s=0.01, awake=False):
        return waiter.wait(ref, limit_h=limit_h, poll_s=poll_s, awake=awake)


class TestWaiterBugs(_WaiterBase):
    """不具合の疑い（今は失敗する）"""

    def test_failed_not_retriable_error_stops_bug(self):
        """「見つかりませんでした」で止まった取得は、アプリが取り直さない（jobs.retriable が False）。
        待つ命令は「止まった」と返すべきなのに、取り直し中とみなして上限（12時間）まで待ち、3 を返して Claude にまた待たせる"""
        self.make(acq={"status": "failed", "error": "楽曲ページの動画が1本も見つかりませんでした",
                       "failed_at": _iso(datetime.datetime.now())})
        code, line = self.run_wait()
        self.assertEqual(code, 1, line)
        self.assertTrue(any(w in line for w in STOP_WORDS), line)

    def test_failed_retries_used_up_stops_bug(self):
        """自動の取り直しを3回使い切った取得（collector.retries=3）も、アプリはもう取り直さない（メニューの再開待ち）"""
        self.make(acq={"status": "failed", "error": "TimeoutException: x", "failed_at": _iso(datetime.datetime.now())},
                  collector={"retries": 3})
        code, line = self.run_wait()
        self.assertEqual(code, 1, line)

    def test_failed_long_ago_without_requeue_stops_bug(self):
        """止まったまま2時間たっている取得（アプリを開き直すと、前から止まっていた分は自動で取り直さない:
        Controller.start が known を今の状態で埋めるので _check_transitions が動かず、pending_retry も消える）"""
        self.make(acq={"status": "failed", "error": "TimeoutException: x",
                       "failed_at": _iso(datetime.datetime.now() - datetime.timedelta(hours=2))})
        # 0.6.1〜 アプリは起動の30秒後に取り直すので、開き直した直後なら待つのが正しい。ここは「アプリは3時間前から動いていて、
        # 2時間前の失敗が戻っていない」場面（app.pid は起動のときに書くので、その時刻を起動の時刻とみなす）
        old = time.time() - 3 * 3600
        os.utime(config.PID_FILE, (old, old))
        code, line = self.run_wait()
        self.assertEqual(code, 1, line)

    def test_login_wanted_by_app_state_bug(self):
        """アプリは state.json に logged_in_at が無いあいだ（初回・ログイン切れで止まったあと）ログインを待ち、取得を進めない
        （app.py _tick、mcp_local.LocalHooks.app_state の login_wanted も同じ判定）。待つ命令は need_login.json しか見ないので、
        2（ログイン待ち）を返さずに上限まで待つ"""
        config.STATE_FILE.write_text("{}", encoding="utf-8")
        self.make(acq="queued")
        code, line = self.run_wait()
        self.assertEqual(code, 2, line)

    def test_app_still_starting_is_not_stopped_bug(self):
        """start_analysis・deepen はアプリが動いていなければ起こす（ensure_app）。起動の数秒のあいだに Claude が ./ugc-wait を
        走らせると、最初の1回の見回りで「動いていません」（1）を返して止まる。少し様子を見るべき"""
        self.make(acq="queued")
        seq = iter([False] + [True] * 100000)
        with mock.patch.object(waiter, "_app_running", side_effect=lambda: next(seq)):
            code, line = self.run_wait()
        self.assertNotEqual(code, 1, line)

    def test_holds_mac_awake_while_waiting_bug(self):
        """取得が終わるとアプリは5秒ほどで caffeinate を離す。待つ命令が caffeinate を掛けるのは、次の見回り（最大30秒後）で
        「終わった」と気づいてから。その間に Mac が眠ると（利用者が長く触っていない夜など）、AI が書き始めるのは朝 Mac を起こしてから。
        待っている間から眠りを止めておくべき（-w で自分の PID に結ぶなど）"""
        self.make(acq="running")
        code, _ = self.run_wait(awake=True)
        self.assertEqual(code, 3)
        cmds = [c.args[0] for c in self.popen.call_args_list if c.args]
        self.assertTrue(any(cmd and "caffeinate" in str(cmd[0]) for cmd in cmds),
                        "待っている間、Mac の眠りを止めるものが無い")

    def test_unreadable_meta_is_not_done_bug(self):
        """analysis.json がその瞬間に読めない（書きかけ・壊れ）と _meta が {} を返し、state() が「done」にする。
        取得中なのに「終わりました」（0）で Claude を起こしてしまう"""
        d = self.make(acq="running")
        (d / "analysis.json").write_text('{"analysis_id": "x", "acquisition": {"sta', encoding="utf-8")
        code, line = self.run_wait()
        self.assertNotEqual(code, 0, line)

    def test_find_by_title_ignores_case_bug(self):
        """道具（proto_runner.resolve）は曲名を大文字小文字を区別せずに当てるが、待つ命令の find は区別する"""
        self.make(title="Bling-Bang-Bang-Born", acq="running")
        self.assertEqual(waiter.find("bling-bang"), config.ANALYSES_DIR / AID)

    def test_main_with_two_words_does_not_crash_bug(self):
        """曲名を引用符なしで渡す（./ugc-wait Night Dancer）と、2つ目を上限の時間として float() にかけて ValueError で落ちる"""
        self.make(title="Night Dancer", acq="cancelled")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = waiter.main(["Night", "Dancer"])
            except ValueError as e:
                self.fail(f"ValueError で落ちた: {e}")
        self.assertIsInstance(code, int)
        self.assertTrue(out.getvalue().strip())


class TestWaiterBranches(_WaiterBase):
    """大事な分岐が今のとおり動くか（通る）"""

    def _later(self, sec, fn):
        t = threading.Timer(sec, fn)
        t.start()
        self.addCleanup(t.cancel)

    def test_failed_just_now_retriable_keeps_waiting(self):
        """Chrome が閉じられた等で止まった直後（アプリが30秒〜5分で取り直す）は待ち続ける"""
        self.make(acq={"status": "failed", "error": "Chrome に接続できません", "failed_at": _iso(datetime.datetime.now())})
        code, line = self.run_wait()
        self.assertEqual(code, 3, line)

    def test_done_while_waiting(self):
        self.make(acq="running")
        self._later(0.1, lambda: self.make(acq="done"))
        code, line = self.run_wait(limit_h=0.002)
        self.assertEqual(code, 0, line)
        self.assertIn("終わりました", line)
        self.assertIn(AID, line)

    def test_cancelled_while_waiting(self):
        self.make(acq="running")
        self._later(0.1, lambda: self.make(acq={"status": "cancelled", "cancel_reason": "利用者がやめた"}))
        code, line = self.run_wait(limit_h=0.002)
        self.assertEqual(code, 1, line)
        self.assertIn("やめてあります", line)

    def test_app_quit_while_waiting(self):
        """メニューの「終了」は PID ファイルを消す（release_single_instance）"""
        self.make(acq="running")
        self._later(0.1, lambda: config.PID_FILE.unlink())
        code, line = self.run_wait(limit_h=0.002)
        self.assertEqual(code, 1, line)
        self.assertIn("止まりました", line)

    def test_app_crashed_pid_dead(self):
        self.make(acq="running")
        config.PID_FILE.write_text("999999")
        code, line = self.run_wait()
        self.assertEqual(code, 1, line)

    def test_deepen_queued_then_running_then_done(self):
        self.make(acq="done", deepen={"round": 1, "status": "queued", "community": "dance"})
        self._later(0.05, lambda: self.make(acq="done", deepen={"round": 1, "status": "running", "community": "dance"}))
        self._later(0.15, lambda: self.make(acq="done", deepen={"round": 1, "status": "done", "community": "dance",
                                                               "result": {"error": "x", "blocked": True}}))
        code, line = self.run_wait(limit_h=0.002)
        self.assertEqual(code, 0, line)
        self.assertIn("掘り下げの取り足し", line)

    def test_recut_fetch_failed_still_continues(self):
        """切り直しの取り足しが failed でも、手元のコメントで書き直す（アプリの通知と同じ）ので 0"""
        self.make(acq="done", deepen={"round": 2, "kind": "recut", "status": "failed"})
        code, line = self.run_wait()
        self.assertEqual(code, 0, line)
        self.assertIn("切り直した界隈の取り足し", line)

    def test_login_flag_during_deepen(self):
        self.make(acq="done", deepen={"round": 1, "status": "running"})
        (config.LOCK_DIR / "need_login.json").write_text("{}")
        code, line = self.run_wait()
        self.assertEqual(code, 2, line)

    def test_limit_line_tells_how_to_wait_again(self):
        self.make(acq="running")
        code, line = self.run_wait()
        self.assertEqual(code, 3)
        self.assertIn(f"./ugc-wait {AID}", line)

    def test_done_keeps_mac_awake_after(self):
        """終わったら AI が書く間 caffeinate -i -t 5400（C5）。Popen は差し替えてあるので本当には起こさない"""
        self.make(acq="done")
        code, _ = self.run_wait(awake=True)
        self.assertEqual(code, 0)
        cmd = self.popen.call_args.args[0]
        self.assertEqual(cmd[:2], ["/usr/bin/caffeinate", "-i"])
        self.assertIn("5400", cmd)

    def test_every_line_matches_claude_md(self):
        """待つ命令の1行には、作業フォルダの CLAUDE.md が見分けに使う言葉のどれかが入っている"""
        cases = [("done", None, None), ("cancelled", None, None), ("running", None, None)]
        for acq, _, _ in cases:
            self.make(acq=acq)
            self.assertTrue(any(w in self.run_wait()[1] for w in CLAUDE_MD_WORDS), acq)
        self.make(acq="running")
        (config.LOCK_DIR / "need_login.json").write_text("{}")
        self.assertTrue(any(w in self.run_wait()[1] for w in CLAUDE_MD_WORDS))
        (config.LOCK_DIR / "need_login.json").unlink()
        config.PID_FILE.unlink()
        self.assertTrue(any(w in self.run_wait()[1] for w in CLAUDE_MD_WORDS))
        for w in CLAUDE_MD_WORDS:
            self.assertIn(w, code_link.CLAUDE_MD)

    def test_find_prefers_busy_then_newest(self):
        """同じ曲名の分析が複数: 取得中のものを選ぶ。どれも取得中でなければ新しいもの"""
        old = self.make(aid="a20990101-0000-old1", title="同じ曲", acq="running")
        new = self.make(aid="a20990102-0000-new1", title="同じ曲", acq={"status": "cancelled"})
        self.assertEqual(waiter.find("同じ曲"), old)
        self.make(aid="a20990101-0000-old1", title="同じ曲", acq="done")
        self.assertEqual(waiter.find("同じ曲"), new)
        self.assertEqual(waiter.find(new.name), new)   # 分析 ID は完全一致で先に当てる


# ---------------------------------------------------------------------------
# Code タブだけの一文（mcp_proto._waiting_text）
# ---------------------------------------------------------------------------
class _ToolBase(unittest.TestCase):
    def setUp(self):
        import mcp_proto
        import proto_runner
        self.mp, self.pr = mcp_proto, proto_runner
        self._log = logging.getLogger("mcp")
        self._lvl = self._log.level
        self._log.setLevel(logging.WARNING)
        self._saved = {k: getattr(proto_runner, k) for k in
                       ("next_task", "start_analysis", "deepen", "recut", "restart_analysis", "ANALYSES_DIR", "LOCAL")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.pr, k, v)
        self._log.setLevel(self._lvl)

    def call(self, tool: str, args: dict, client: str | None) -> str:
        from mcp import Client
        from mcp.types import Implementation
        srv = self.mp._build_server(user_of=lambda ctx: "local", local=True)   # noqa: SLF001

        async def go():
            info = Implementation(name=client, version="1") if client is not None else None
            async with Client(srv, client_info=info) as c:
                r = await c.call_tool(tool, args)
                return "\n".join(getattr(x, "text", "") for x in r.content)

        return asyncio.run(go())


WAIT_LINE = "その間 AI は待てないため、Mac に通知が出たら「テスト曲の分析を続けて」と頼んでください。そこからレポートを書きます。"


class TestCodeTabSentence(_ToolBase):
    def test_restart_analysis_in_code_tab_bug(self):
        """「〇〇の取得をやめて、このページでやり直して」「それも入れて」（restart_analysis に URL）は、新しい分析の取得を始め、
        start_analysis と同じ「その間 AI は待てない…続けてと頼んでください」の返事を返す。ところが restart_analysis だけ
        _waiting_text を通していないので、Code タブでも ./ugc-wait の一文が付かず、利用者に「続けて」を頼んで止まる"""
        self.pr.restart_analysis = lambda *a: {
            "text": "「テスト曲」の前の取得（https://www.tiktok.com/music/x-1）をやめました。\n「テスト曲」の取得を受け付けました"
                    f"（分析 ID: a20990101-0000-new1）。\n「{WAIT_LINE}」", "analysis_id": "a20990101-0000-new1",
            "cancelled": AID}
        t = self.call("restart_analysis", {"analysis_id": "テスト曲",
                                           "music_url": "https://www.tiktok.com/music/x-2"}, "claude-code")
        self.assertIn("./ugc-wait a20990101-0000-new1", t)

    def test_only_code_tab_gets_sentence(self):
        self.pr.next_task = lambda *a: {"text": "# 待ち: テスト曲\n\n- kind: `wait`\n\n取得中", "kind": "wait",
                                        "analysis_id": AID, "task_id": None}
        self.assertIn(f"./ugc-wait {AID}", self.call("next_task", {"analysis_id": "テスト曲"}, "claude-code"))
        self.assertIn(f"./ugc-wait {AID}", self.call("next_task", {"analysis_id": "テスト曲"}, "Claude Code"))
        for name in ("claude-ai", "codex-mcp-client", "ChatGPT", ""):
            self.assertNotIn("./ugc-wait", self.call("next_task", {"analysis_id": "テスト曲"}, name), name)

    def test_no_sentence_unless_waiting(self):
        for kind in ("ai", "ask_user", "done"):
            self.pr.next_task = lambda *a, k=kind: {"text": f"# 仕事\n- kind: `{k}`", "kind": k, "analysis_id": AID,
                                                    "task_id": f"{AID}/01"}
            self.assertNotIn("./ugc-wait", self.call("next_task", {}, "claude-code"), kind)
        # 知識ベースの仕事（analysis_id なし）
        self.pr.next_task = lambda *a: {"text": "kb", "kind": "ai", "analysis_id": None, "task_id": "kb/1"}
        self.assertNotIn("./ugc-wait", self.call("next_task", {}, "claude-code"))

    def test_start_and_deepen(self):
        self.pr.start_analysis = lambda *a: {"text": f"受け付けました（分析 ID: {AID}）。\n「{WAIT_LINE}」", "analysis_id": AID}
        self.assertIn(f"./ugc-wait {AID}", self.call("start_analysis", {"song": "テスト曲"}, "claude-code"))
        self.assertNotIn("./ugc-wait", self.call("start_analysis", {"song": "テスト曲"}, "claude-ai"))
        # 楽曲ページ探しの案内（分析はまだ無い）には付けない
        self.pr.start_analysis = lambda *a: {"text": "楽曲ページを探して呼び直す。" + WAIT_LINE, "need_search": True}
        self.assertNotIn("./ugc-wait", self.call("start_analysis", {"song": "テスト曲"}, "claude-code"))
        # 掘り下げ: 取り足しがあるときだけ
        self.pr.deepen = lambda *a: {"text": f"掘り下げます。\n「この界隈のコメントを取り足すのに約25分かかります。{WAIT_LINE}」",
                                     "analysis_id": AID}
        self.assertIn(f"./ugc-wait {AID}", self.call("deepen", {"community": "dance", "instruction": "x"}, "claude-code"))
        self.pr.deepen = lambda *a: {"text": "取り足せる動画が無いので、手元のコメントで直します。続けて next_task を呼んで",
                                     "analysis_id": AID}
        self.assertNotIn("./ugc-wait", self.call("deepen", {"community": "dance", "instruction": "x"}, "claude-code"))

    def test_recut_accept_has_no_sentence(self):
        """切り直しの受け付けは待ちではない（待ちは next_task の kind=wait で来る）"""
        self.pr.recut = lambda *a: {"text": "切り直します。続けて next_task を呼び…", "analysis_id": AID}
        self.assertNotIn("./ugc-wait", self.call("recut", {"instruction": "2つに分けて"}, "claude-code"))


class TestCodeTabSentenceRealRunner(_ToolBase):
    """本物の proto_runner（一時フォルダの分析）で"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.pr.ANALYSES_DIR = Path(self.tmp.name) / "analyses"
        self.pr.LOCAL = None

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def test_cancelled_analysis_does_not_promise_auto_continue_bug(self):
        """やめた分析に「〇〇の分析を続けて」→ next_task は kind=wait（「この取得はやめました」）を返し、Code タブの一文が付く。
        AI は利用者に「集め終わったら、このまま自動で続けます」と伝えて ./ugc-wait を走らせ、すぐ「やめてあります」で起こされる。
        進まない待ち（やめた・止まった）には一文を付けないか、別の言い方にするべき"""
        d = self.pr.ANALYSES_DIR / AID
        (d / "state").mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({
            "analysis_id": AID, "title": "テスト曲", "owner": "local", "created_at": "2099-01-01T00:00:00+09:00",
            "acquisition": {"status": "cancelled", "cancel_reason": "利用者がやめた"}}, ensure_ascii=False), encoding="utf-8")
        t = self.call("next_task", {"analysis_id": "テスト曲"}, "claude-code")
        self.assertIn("やめました", t)
        self.assertNotIn("自動で続けます", t)


# ---------------------------------------------------------------------------
# 作業フォルダ（code_link）
# ---------------------------------------------------------------------------
class TestCodeFolderR7(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = code_link.FOLDER
        code_link.FOLDER = Path(self.tmp.name) / "UGC Analyzer"

    def tearDown(self):
        code_link.FOLDER = self.saved
        self.tmp.cleanup()

    def test_odd_settings_file_does_not_crash_app_start_bug(self):
        """app.run は code_link.ensure() の OSError しか受け止めない。.claude/settings.local.json が JSON として読めても
        辞書でない（[] など）と AttributeError が抜け、アプリの起動そのものが毎回止まる（起きにくいが、起きると重い）"""
        p = code_link.FOLDER / ".claude" / "settings.local.json"
        p.parent.mkdir(parents=True)
        p.write_text("[]", encoding="utf-8")
        try:
            code_link.ensure()
        except OSError:
            pass
        except Exception as e:   # noqa: BLE001
            self.fail(f"OSError 以外が抜けた（アプリの起動が止まる）: {type(e).__name__}: {e}")

    def test_keeps_other_files_and_rewrites_ours(self):
        f = code_link.FOLDER
        f.mkdir(parents=True)
        (f / "メモ.txt").write_text("利用者のメモ", encoding="utf-8")
        (f / "CLAUDE.md").write_text("古い手順", encoding="utf-8")
        changed = code_link.ensure()
        self.assertIn("CLAUDE.md", changed)
        self.assertEqual((f / "メモ.txt").read_text(encoding="utf-8"), "利用者のメモ")
        self.assertEqual((f / "CLAUDE.md").read_text(encoding="utf-8"), code_link.CLAUDE_MD)
        self.assertFalse(list(f.glob("*.tmp")), "書きかけのファイルが残っている")

    def test_folder_path_taken_by_a_file_raises_oserror(self):
        """~/UGC Analyzer がファイルだったとき、アプリが受け止められる OSError で止まる"""
        code_link.FOLDER.parent.mkdir(parents=True, exist_ok=True)
        code_link.FOLDER.write_text("x")
        with self.assertRaises(OSError):
            code_link.ensure()

    def test_wait_script_quotes_app_path(self):
        frozen = {"command": "/Applications/UGC Analyzer.app/Contents/MacOS/UGC Analyzer", "args": ["--mcp"]}
        with mock.patch.object(claude_link, "entry", return_value=frozen):
            script = code_link._wait_script()   # noqa: SLF001
        line = [ln for ln in script.splitlines() if ln.startswith("exec ")][0]
        self.assertEqual(shlex.split(line), ["exec", frozen["command"], "--wait", "$@"])

    def test_wait_script_runs_end_to_end(self):
        """作業フォルダの ugc-wait を本当に走らせる（ソースの python で。空白の入った場所・空白の入った曲名）"""
        code_link.ensure()
        home = Path(self.tmp.name) / "data"
        d = home / "analyses" / AID
        d.mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({"analysis_id": AID, "title": "Night Dancer",
                                                     "acquisition": {"status": "cancelled"}}), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name, "UGC_COLLECTOR_HOME": str(home)}
        r = subprocess.run(["./ugc-wait", "Night Dancer"], cwd=str(code_link.FOLDER), env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("やめてあります", r.stdout)
        self.assertIn(AID, r.stdout)


# ---------------------------------------------------------------------------
# 担当外（R7 の待つ命令の「ログイン待ち」の判定に関わるので、念のため）
# ---------------------------------------------------------------------------
class _FakeChrome:
    def __init__(self, *a, **k):
        pass

    def listening(self):
        return True

    def logged_in(self):
        return True

    def ensure(self, *a, **k):
        pass

    def navigate(self, *a, **k):
        pass

    def show(self):
        pass

    def minimize(self):
        pass

    def quit(self):
        pass


class TestStaleLoginFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE)
        config.ANALYSES_DIR, config.PID_FILE = base / "analyses", base / "app.pid"
        config.LOCK_DIR, config.STATE_FILE = base / "locks", base / "state.json"
        for p in (config.ANALYSES_DIR, config.LOCK_DIR):
            p.mkdir(parents=True)

    def tearDown(self):
        config.ANALYSES_DIR, config.PID_FILE, config.LOCK_DIR, config.STATE_FILE = self.saved
        self.tmp.cleanup()

    def test_stale_need_login_flag_does_not_loop_bug(self):
        """取得の係はログイン待ちの間 need_login.json を置き、finally で消す。ところがアプリは係を SIGTERM で止める
        （Mac のスリープ・アプリの終了・取得をやめる）ので、ログイン待ちの最中に止まると finally が走らず、印が残る。
        アプリの見張り（_tick）は「印があってログインを待っていない」たびにログインの流れに入り直し、ログイン済みなら
        「準備OK」を出して戻る → 5秒ごとに繰り返し、取得の係を起こさない（待つ命令はその間ずっと 2 を返す）"""
        from acquire import pipeline
        from collector_app import app
        (config.LOCK_DIR / "need_login.json").write_text("{}")
        config.STATE_FILE.write_text(json.dumps({"logged_in_at": "2026-10-06 08:00:00"}), encoding="utf-8")
        sent = []
        with mock.patch.object(app.chrome, "Chrome", _FakeChrome), \
                mock.patch.object(app.chrome, "find_binary", return_value="/x/Google Chrome"), \
                mock.patch.object(app.notify, "send", side_effect=lambda *a, **k: sent.append(a)), \
                mock.patch.object(app, "ai_where", return_value="Claude"), \
                mock.patch.object(pipeline, "ANALYSES_DIR", config.ANALYSES_DIR):
            ctl = app.Controller(Path(self.tmp.name), logging.getLogger("r7-test"))
            for _ in range(3):
                ctl._tick()   # noqa: SLF001
        ready = [a for a in sent if a and a[0] == "準備OK"]
        self.assertLessEqual(len(ready), 1, f"「準備OK」が {len(ready)} 回出た（見張りのたびに繰り返す）")


if __name__ == "__main__":
    unittest.main()
