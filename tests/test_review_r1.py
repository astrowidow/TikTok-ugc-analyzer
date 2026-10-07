"""通し試験（2026-10-06）の読み合わせ R1: 取得の段と見込みの単体試験（TikTok・Chrome・本物の置き場に触らない）。

  collector/.venv/bin/python -m unittest tests.test_review_r1 -v

対象: acquire/pipeline.py・acquire/launch.py・acquire/worker.py・acquire/notify.py・
      collector/collector_app/jobs.py・collector/collector_app/worker_entry.py（と、それを呼ぶ app.py の見張り）

試験は「正しい動き」を期待して書いてある。不具合の疑いを確かめる試験は、名前の頭に bug_ を付けた（失敗＝不具合）。
それ以外は、試験の無かった大事な分岐を押さえるための試験（通るのが正しい）。
"""
import atexit
import contextlib
import datetime
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# 本物の置き場（~/Library/Application Support/UGC Analyzer）や output/ に触らないよう、読み込む前に置き場を一時フォルダへ向ける
_TMP_ROOT = tempfile.mkdtemp(prefix="review_r1_")
atexit.register(shutil.rmtree, _TMP_ROOT, True)
os.environ["UGC_COLLECTOR_HOME"] = os.path.join(_TMP_ROOT, "home")
os.environ["UGC_ANALYSES_DIR"] = os.path.join(_TMP_ROOT, "analyses")
os.environ["UGC_LOCK_DIR"] = os.path.join(_TMP_ROOT, "locks")
os.environ["UGC_MAIL_CONFIG"] = os.path.join(_TMP_ROOT, "no-mail.json")
os.environ["UGC_MCP_USERS"] = os.path.join(_TMP_ROOT, "no-users.json")
os.environ.pop("UGC_COLLECTOR_TEST_NO_CHROME", None)
for _p in (ROOT / "collector", ROOT / "analysis", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

U1 = "https://www.tiktok.com/music/silhouette-7000000000000000101"
U2 = "https://www.tiktok.com/music/silhouette-sped-up-7000000000000000102"
DEAD_PID = 999999   # macOS の PID の上限（99998）より大きい＝必ず死んでいる


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


class _Killed(BaseException):
    """係が止められた（スリープ・アプリの終了）ことの代わり。工程の except Exception には捕まらない"""


class R1Base(unittest.TestCase):
    """分析の置き場・ロックの置き場をこの試験だけの一時フォルダにする"""

    def setUp(self):
        from acquire import launch, pipeline, worker
        import tiktok_lock
        self.pl, self.wk, self.la, self.tl = pipeline, worker, launch, tiktok_lock
        self.tmp = Path(tempfile.mkdtemp(prefix="t_", dir=_TMP_ROOT))
        self.adir = self.tmp / "analyses"
        self.ldir = self.tmp / "locks"
        self.adir.mkdir()
        self.ldir.mkdir()
        for target, attr, val in ((pipeline, "ANALYSES_DIR", self.adir), (tiktok_lock, "LOCK_DIR", self.ldir),
                                  (tiktok_lock, "LOCK_FILE", self.ldir / "tiktok.lock"),
                                  (worker, "GUARD", self.ldir / "acq_worker.json")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # pipeline.Run.log は標準出力にも出す。試験の出力を汚さない
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def make(self, aid: str, acq: dict | None = None, **extra) -> Path:
        d = self.adir / aid
        for sub in ("raw", "fetch_log", "derived", "derived/llm_input", "outputs", "eval", "state"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        meta = {"analysis_id": aid, "title": extra.pop("title", "シルエット"), "owner": extra.pop("owner", "local"),
                "song": {"artist": "KANA-BOON", "title": "シルエット"}, "music_url": U1, "music_urls": [U1],
                "created_at": "2026-10-06T08:49:00+09:00"}
        meta.update(extra)
        if acq is not None:
            meta["acquisition"] = acq
        (d / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return d

    def meta(self, aid: str) -> dict:
        return json.loads((self.adir / aid / "analysis.json").read_text(encoding="utf-8"))


class AppBase(R1Base):
    """collector_app（jobs・config・app）も一時フォルダに向ける"""

    def setUp(self):
        super().setUp()
        from collector_app import config, jobs
        self.cfg, self.jobs = config, jobs
        self.home = self.tmp / "home"
        self.home.mkdir()
        for attr, val in (("ANALYSES_DIR", self.adir), ("LOCK_DIR", self.ldir), ("STATE_FILE", self.home / "state.json"),
                          ("LOG_DIR", self.home / "logs"), ("DATA_DIR", self.home)):
            p = mock.patch.object(config, attr, val)
            p.start()
            self.addCleanup(p.stop)
        (self.home / "logs").mkdir()


# ---------------------------------------------------------------------------
# 1. 分析の置き場に分析以外のファイルがあるとき（Finder の .DS_Store など）
# ---------------------------------------------------------------------------
class TestStrayFileInAnalysesDir(AppBase):
    """メニュー「そのほか → データのフォルダを開く」は analyses/ を Finder で開く。Finder はそこに .DS_Store を作る
    （運営の Mac でも UGC Analyzer/ と レポート/ にはもうできている）。分析フォルダ以外があっても、待ち行列・見込み・メニューは動くべき"""

    def setUp(self):
        super().setUp()
        self.aid = "a20261006-0900-aaaa"
        self.make(self.aid, {"status": "queued", "queued_at": "2026-10-06T09:00:00+09:00", "steps": {}})
        (self.adir / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    def test_bug_queue_survives_ds_store(self):
        self.assertEqual(self.wk.queue(), [self.aid])

    def test_bug_launch_survives_ds_store(self):
        self.assertEqual(self.la.find_active("local", "シルエット"), self.aid)
        p = self.la.progress(self.aid)
        self.assertEqual(p["status"], "queued")

    def test_bug_menu_list_survives_ds_store(self):
        self.assertEqual([m["analysis_id"] for m in self.jobs.analyses()], [self.aid])


# ---------------------------------------------------------------------------
# 2. 残り時間の見込み（launch._remaining_seconds・progress）
# ---------------------------------------------------------------------------
class TestRemainingSeconds(R1Base):
    def test_bug_two_music_pages_double_list_and_enrich(self):
        """楽曲ページが2つなら、一覧（台帳）はページの数だけかかる。詳しく読む本数は 2026-10-07 から音源の数で割る
        （1音源 500本・2音源 250本ずつで、合計は同じ。pipeline.read_quota）ので、属性は増えない。
        全部読む設定（read_select 0）なら、前と同じく2ページ分の属性を見込む（10/6 の実走: シルエット2ページで 1,812本）"""
        la = self.la
        one = {"analysis_id": "x1", "music_url": U1, "music_urls": [U1], "acquisition": {"status": "queued", "steps": {}}}
        two = {"analysis_id": "x2", "music_url": U1, "music_urls": [U1, U2], "acquisition": {"status": "queued", "steps": {}}}
        diff = la._remaining_seconds(two) - la._remaining_seconds(one)
        page = la.LIST_SECONDS_BASE + la.LIST_SECONDS_PER_SCROLL * 60
        self.assertAlmostEqual(diff, page, delta=2, msg=f"2ページなら一覧1ページ分だけ増える（差 {diff}秒）")
        many = {**two, "music_urls": [U1] + [f"{U2}{i}" for i in range(20)]}
        self.assertGreater(la._remaining_seconds(many) - la._remaining_seconds(one), 500 * la.ENRICH_SECONDS_PER_VIDEO,
                           "21音源なら1音源50本で合計1,000本ぶんの属性")
        for m in (one, two):
            m["acquisition_settings"] = {"read_select": 0}
        diff = la._remaining_seconds(two) - la._remaining_seconds(one)
        self.assertGreaterEqual(diff, 0.9 * la.TYPICAL_VIDEOS * la.ENRICH_SECONDS_PER_VIDEO,
                                f"全部読む設定で2ページでも1ページと同じ見込み（差 {diff}秒）")

    def test_bug_resumed_enrich_uses_progress_not_first_start(self):
        """属性の段の途中で Mac が眠り（係は止められ、段の started_at は最初のまま残る）、6時間後に続きから始めた。
        enriched.jsonl は 400/1812本。残りは約1,400本ぶん（約75分）あるのに、最初に始めた時刻から測るので1分と見込む"""
        la = self.la
        aid = "a20261006-0849-833c"
        d = self.make(aid, None, music_urls=[U1, U2])
        with open(d / "raw" / "enriched.jsonl", "w", encoding="utf-8") as f:
            for i in range(400):
                f.write(json.dumps({"video_id": str(i)}) + "\n")
        six_h_ago = _iso(datetime.datetime.now() - datetime.timedelta(hours=6))
        m = {"analysis_id": aid, "music_urls": [U1, U2], "acquisition": {"status": "running", "step": "enrich", "steps": {
            "resolve": {"status": "done"}, "list": {"status": "done", "detail": {"links": 1812}},
            "enrich": {"status": "running", "started_at": six_h_ago}}}}
        got = la._remaining_seconds(m)
        comments = la.TYPICAL_COMMENT_VIDEOS * self.pl.DEFAULTS["min_per_page_video"] * 60 + 600
        rest_enrich = (1812 - 400) * la.ENRICH_SECONDS_PER_VIDEO
        self.assertGreaterEqual(got, comments + 0.8 * rest_enrich, f"見込み {got}秒（属性の残りを {got - comments:.0f}秒と見ている）")

    def test_comments_running_uses_done_count(self):
        """（網羅）コメントの段が走っているときは、取れた本数から残りを出す（最初の走行・要約あり）"""
        la, pl = self.la, self.pl
        aid = "a20261006-0849-cmts"
        d = self.make(aid, None)
        rows = [{"video_id": str(i), "status": "ok"} for i in range(39)]
        (d / "fetch_log" / "comments_summary.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
        m = {"analysis_id": aid, "acquisition": {"status": "running", "step": "comments", "steps": {
            s: {"status": "done"} for s in ("resolve", "list", "enrich", "derive")} | {
            "pool": {"status": "done", "detail": {"n_pool": 79}}, "comments": {"status": "running"}}}}
        per = pl.DEFAULTS["min_per_page_video"]
        self.assertAlmostEqual(la._remaining_seconds(m), (79 - 39) * per * 60 + 300 + 30 + 5, delta=1)

    def test_bug_deepen_ahead_counts_in_wait(self):
        """掘り下げの取り足し（約25分、待ち行列の先頭）が前にいるとき、新しい分析の待ち時間に入れるべき。
        今は「前に1件」と数えるのに、待ちは0秒（取得が done の分析は見込み0）"""
        la = self.la
        self.make("a20261006-0407-60f4", {"status": "done", "steps": {s: {"status": "done"} for s in self.pl.STEPS}},
                  deepen={"status": "queued", "queued_at": "2026-10-06T10:00:00+09:00", "round": 1, "est_min": 25,
                          "community": "c1", "targets": [{"video_id": "1"}]})
        self.make("a20261006-1001-bbbb", {"status": "queued", "queued_at": "2026-10-06T10:01:00+09:00", "steps": {}})
        p = la.progress("a20261006-1001-bbbb")
        self.assertEqual(p["ahead"], 1)
        self.assertGreaterEqual(p["wait_seconds"], 20 * 60, f"待ち {p['wait_seconds']}秒")


# ---------------------------------------------------------------------------
# 3. コメントの段の進み具合（○/○本）
# ---------------------------------------------------------------------------
class TestCommentsProgress(AppBase):
    """spatest の要約（comments_summary*.json）はその走行の分だけで、走行のたびに上書きされる。
    続きから取り直す（スリープ・ブロック後・止まった取得の再開）と、要約は新しい走行の分だけになる"""

    def _resumed(self, aid="a20261006-0849-resm"):
        steps = {s: {"status": "done"} for s in ("resolve", "list", "enrich", "derive")}
        steps.update({"pool": {"status": "done", "detail": {"n_pool": 79}}, "comments": {"status": "running"}})
        d = self.make(aid, {"status": "running", "step": "comments", "steps": steps})
        with open(d / "raw" / "comments.jsonl", "w", encoding="utf-8") as f:   # 前の走行までに30本＋今の走行で1本
            for i in range(31):
                f.write(json.dumps({"video_id": str(i), "status": "ok", "comments": []}) + "\n")
        (d / "fetch_log" / "comments_summary.json").write_text(
            json.dumps({"rows": [{"video_id": "30", "status": "ok"}]}), encoding="utf-8")
        return aid, d

    def test_bug_comments_ok_counts_earlier_runs(self):
        aid, d = self._resumed()
        self.assertGreaterEqual(self.pl.comments_ok(d), 31)

    def test_bug_menu_line_keeps_count_after_resume(self):
        """友達の画面（メニューの1行目）: 「コメントを取る 31/79本」のはずが「1/79本」に戻る"""
        aid, d = self._resumed()
        self.assertIn("31/79本", self.jobs.describe(self.meta(aid)))

    def test_bug_multi_page_resume_keeps_page1(self):
        """楽曲ページ2つ: 2ページ目の途中から続きを取ると、取り終えた1ページ目も走らせ直し（取るものが無い）、
        1ページ目の要約が空（rows []）で上書きされる"""
        aid = "a20261006-0849-mult"
        d = self.make(aid, None, music_urls=[U1, U2])
        with open(d / "raw" / "comments.jsonl", "w", encoding="utf-8") as f:
            for i in range(41):
                f.write(json.dumps({"video_id": str(i), "status": "ok", "comments": []}) + "\n")
        (d / "fetch_log" / "comments_summary.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
        (d / "fetch_log" / "comments_summary_p2.json").write_text(
            json.dumps({"rows": [{"video_id": "40", "status": "ok"}]}), encoding="utf-8")
        self.assertGreaterEqual(self.pl.comments_ok(d), 41)


# ---------------------------------------------------------------------------
# 4. 段の実行・再開・失敗（pipeline.Run.run）
# ---------------------------------------------------------------------------
class TestRunSteps(R1Base):
    def _patch_steps(self, called, fail=None):
        for step in self.pl.STEPS:
            def fn(run, step=step):
                called.append(step)
                if fail and fail[0] == step:
                    raise fail[1]
                return {"step": step}
            p = mock.patch.object(self.pl.Run, "step_" + step, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_resume_skips_done_steps(self):
        """（網羅）済んだ工程は飛ばし、残りを順に。最後に status=done・step=None"""
        aid = "a20261006-0849-run1"
        self.make(aid, {"status": "running", "pid": DEAD_PID, "steps": {
            "resolve": {"status": "done"}, "list": {"status": "done", "detail": {"links": 1812}}}})
        called = []
        self._patch_steps(called)
        self.assertEqual(self.pl.Run(aid).run(), "done")
        self.assertEqual(called, self.pl.STEPS[2:])
        acq = self.meta(aid)["acquisition"]
        self.assertEqual((acq["status"], acq["step"]), ("done", None))
        self.assertTrue(all(acq["steps"][s]["status"] == "done" for s in self.pl.STEPS))
        self.assertEqual(acq["steps"]["list"]["detail"], {"links": 1812})   # 済んだ工程の中身は残る

    def test_step_error_marks_failed_notifies_and_unlocks(self):
        """（網羅）StepError: failed と理由・知らせ（メールの係）・ロックを返す。止まった工程は running のまま＝次はそこから"""
        import acquire.notify as notify
        aid = "a20261006-0849-run2"
        self.make(aid, {"status": "queued", "steps": {}})
        called, sent = [], []
        self._patch_steps(called)
        StepError = self.pl.StepError

        def enrich_fails(run):   # ロックを取ったまま止まる
            run.lock("属性")
            raise StepError("理由")
        with mock.patch.object(notify, "send", lambda run, failed=None: sent.append(failed)), \
                mock.patch.object(self.pl.Run, "step_enrich", enrich_fails):
            self.assertEqual(self.pl.Run(aid).run(), "failed")
        acq = self.meta(aid)["acquisition"]
        self.assertEqual((acq["status"], acq["error"]), ("failed", "理由"))
        self.assertEqual(acq["steps"]["enrich"]["status"], "running")
        self.assertEqual(sent, ["理由"])
        self.assertFalse((self.ldir / "tiktok.lock").exists())

    def test_exception_marks_failed_without_mail(self):
        """（網羅）想定外の例外: failed と型名つきの理由。メールの知らせは送らない（アプリの通知は app.py が出す）"""
        import acquire.notify as notify
        aid = "a20261006-0849-run3"
        self.make(aid, {"status": "queued", "steps": {}})
        called, sent = [], []
        self._patch_steps(called, fail=("derive", RuntimeError("boom")))
        with mock.patch.object(notify, "send", lambda run, failed=None: sent.append(failed)):
            self.assertEqual(self.pl.Run(aid).run(), "failed")
        acq = self.meta(aid)["acquisition"]
        self.assertEqual((acq["status"], acq["error"]), ("failed", "RuntimeError: boom"))
        self.assertEqual(sent, [])


# ---------------------------------------------------------------------------
# 5. 待ち行列・係（acquire/worker.py）
# ---------------------------------------------------------------------------
class TestQueue(R1Base):
    def test_order_and_states(self):
        """（網羅）掘り下げの取り足しが先、あとは受け付け順。止まった running（PID が死んでいる）も入れる。
        走っている・やめた・止まった・済んだものは入れない"""
        self.make("a-queued", {"status": "queued", "queued_at": "2026-10-06T10:00:00+09:00"})
        self.make("a-dead", {"status": "running", "pid": DEAD_PID, "queued_at": "2026-10-06T09:00:00+09:00"})
        self.make("a-alive", {"status": "running", "pid": os.getpid(), "queued_at": "2026-10-06T08:00:00+09:00"})
        self.make("a-failed", {"status": "failed", "queued_at": "2026-10-06T07:00:00+09:00"})
        self.make("a-cancel", {"status": "cancelled", "queued_at": "2026-10-06T06:00:00+09:00"})
        self.make("a-deepen", {"status": "done", "queued_at": "2026-10-06T05:00:00+09:00"},
                  deepen={"status": "queued", "queued_at": "2026-10-06T11:00:00+09:00"})
        self.assertEqual(self.wk.queue(), ["a-deepen", "a-dead", "a-queued"])

    def test_run_one_dispatch(self):
        """（網羅）取得が残っていれば取得、取得が済んで取り足しが待っていれば取り足し"""
        from acquire import deepen
        self.make("a-acq", {"status": "queued"})
        self.make("a-dp", {"status": "done"}, deepen={"status": "queued"})
        with mock.patch.object(self.pl.Run, "run", lambda r: "acq:" + r.id), \
                mock.patch.object(deepen.Run, "run", lambda r: "deepen:" + r.id):
            self.assertEqual(self.wk.run_one("a-acq"), "acq:a-acq")
            self.assertEqual(self.wk.run_one("a-dp"), "deepen:a-dp")

    def test_bug_dead_worker_pid_reused(self):
        """係（GUARD）がいないのに、止まった分析の PID がたまたま生きている別のプロセスに再利用されていると、
        running のまま待ち行列に入らず、続きが取られない（メニューは「取得中」のまま）。起きにくいが起きると直らない"""
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.make("a-reused", {"status": "running", "pid": other.pid, "queued_at": "2026-10-06T09:00:00+09:00"})
        self.assertIsNone(self.wk.worker_pid())
        self.assertEqual(self.wk.queue(), ["a-reused"])


# ---------------------------------------------------------------------------
# 6. 止まった取得の取り直し（jobs.retriable・requeue）
# ---------------------------------------------------------------------------
class TestRetry(AppBase):
    def test_retriable_rules(self):
        """（網羅）取り直しても同じもの（楽曲ページが見つからない等）と回数切れは取り直さない"""
        r = self.jobs.retriable
        self.assertFalse(r({"acquisition": {"error": "TikTok の楽曲ページが見つかりませんでした。楽曲ページの URL を教えてください"}}))
        self.assertFalse(r({"acquisition": {"error": "楽曲ページから動画が1本も見つかりませんでした（URL が違うか、TikTok 側の表示制限）"}}))
        self.assertTrue(r({"acquisition": {"error": "コメントが1本も取れませんでした（ログインの状態か、TikTok 側の制限の可能性）"}}))
        self.assertFalse(r({"acquisition": {"error": "WebDriverException: x"}, "collector": {"retries": 3}}))

    def test_bug_before_release_not_retried(self):
        """「全部曲の公開より前の日付でした（楽曲ページが違う可能性）」は取り直しても同じ。
        今は取り直す扱いで、5分おきに一覧（約10分）を3回やり直してから止まる"""
        self.assertFalse(self.jobs.retriable({"acquisition": {
            "error": "楽曲ページの動画が、全部曲の公開より前の日付でした（楽曲ページが違う可能性）"}}))

    def test_requeue_counts(self):
        """（網羅）自動の取り直しは回数を数え、メニューからは数え直す。済んだ工程は残す"""
        aid = "a20261006-0849-rq"
        self.make(aid, {"status": "failed", "error": "x", "steps": {"list": {"status": "done"}}})
        self.jobs.requeue(aid)
        m = self.meta(aid)
        self.assertEqual((m["acquisition"]["status"], m["collector"]["retries"]), ("queued", 1))
        self.assertEqual(m["acquisition"]["steps"]["list"]["status"], "done")
        self.jobs.requeue(aid, manual=True)
        self.assertEqual(self.meta(aid)["collector"]["retries"], 0)


# ---------------------------------------------------------------------------
# 7. コメントの段の止まり方（ブロック・連続失敗・時間の上限）
# ---------------------------------------------------------------------------
class _FakeCollector:
    """spatest.SpaCollector の代わり（TikTok にも Chrome にも触らない）。behaviors を順に実行する"""
    calls = []
    behaviors = []

    def __init__(self, a):
        self.a, self.d, self.rows, self.yielded, self.between_videos = a, None, [], 0.0, None
        _FakeCollector.calls.append(a)

    def log(self, m):
        pass

    def run(self):
        _FakeCollector.behaviors.pop(0)(self)


def _blocked(c):
    from acquire import spatest
    raise spatest.Blocked("開始前")


def _ok(c):
    with open(c.a.out, "a", encoding="utf-8") as f:
        f.write(json.dumps({"video_id": "101", "status": "ok", "comments": [{"aweme_id": "101"}]}) + "\n")
    c.rows.append({"video_id": "101", "status": "ok"})


def _two_errors(c):
    c.rows += [{"video_id": "101", "status": "error"}, {"video_id": "102", "status": "error"}]


class TestStepComments(R1Base):
    def setUp(self):
        super().setUp()
        from acquire import spatest
        self.aid = "a20261006-0849-cm"
        d = self.make(self.aid, {"status": "running", "step": "comments", "steps": {"comments": {"status": "running"}}})
        (d / "derived" / "pool.tsv").write_text("video_id\tweek\tpriority\treasons\tcap\n101\tw1\t1\tkey\t20\n",
                                                 encoding="utf-8")
        (d / "derived" / "llm_input" / "records.jsonl").write_text("", encoding="utf-8")
        _FakeCollector.calls, _FakeCollector.behaviors = [], []
        self.RealCollector = spatest.SpaCollector
        for target, attr, val in ((spatest, "SpaCollector", _FakeCollector),
                                  (self.pl, "ensure_chrome", lambda port, log: None)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def test_blocked_waits_and_falls_back(self):
        """（網羅）ブロック → 90分空けて、1.8回/分・60秒に2回に落として続きを取る"""
        d = self.pl.DEFAULTS
        _FakeCollector.behaviors = [_blocked, _ok]
        sleeps = []
        with mock.patch.object(self.pl.time, "sleep", lambda s: sleeps.append(s)):
            res = self.pl.Run(self.aid).step_comments()
        self.assertEqual(sleeps, [d["blocked_wait_min"] * 60])
        self.assertEqual([a.calls_per_min for a in _FakeCollector.calls], [d["calls_per_min"], d["fallback_calls_per_min"]])
        self.assertEqual(_FakeCollector.calls[1].max_calls_per_min, d["fallback_max_calls_per_min"])
        self.assertEqual((res["videos_ok"], res["blocked"]), (1, False))
        self.assertFalse((self.ldir / "tiktok.lock").exists())

    def test_two_errors_retry_after_two_minutes(self):
        """（網羅）連続失敗で止まったら2分空けて続きから"""
        _FakeCollector.behaviors = [_two_errors, _ok]
        sleeps = []
        with mock.patch.object(self.pl.time, "sleep", lambda s: sleeps.append(s)):
            res = self.pl.Run(self.aid).step_comments()
        self.assertEqual(sleeps, [120])
        self.assertEqual(res["videos_ok"], 1)

    def test_deadline_used_up_does_not_open(self):
        """（網羅）時間の上限（20時間）を使い切っていたら、取りに行かずに確かめだけ（1本も無ければ止める）"""
        self.pl.Run(self.aid)._mark("comments", {"active_hours": 20.0})
        with self.assertRaises(self.pl.StepError):
            self.pl.Run(self.aid).step_comments()
        self.assertEqual(_FakeCollector.calls, [])

    def test_bug_resume_does_not_scroll_finished_page(self):
        """楽曲ページ2つの分析を、2ページ目の途中から続きで取る。1ページ目は取り終えているのに、step_comments は1ページ目も
        spatest に渡し、spatest は「探す動画が0本」のときグリッドを上限（150回×2秒＝約5分）までスクロールし続ける
        （want が空だと「全部見つかった」で止まらず、必要本数 10**9 を待つ）。ついでに1ページ目の要約が空で上書きされる。
        （直し FX1: 取り終えたページは pipeline 側で spatest に渡さない。spatest 自身の空振りは R2 の test_no_grid_scroll_when_nothing_left）"""
        d = self.adir / self.aid
        m = self.meta(self.aid)
        m["music_urls"] = [U1, U2]
        (d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        with open(d / "raw" / "grid_links.jsonl", "w", encoding="utf-8") as f:
            for vid, src in (("101", 1), ("201", 2), ("202", 2)):
                f.write(json.dumps({"url": f"https://www.tiktok.com/@x/video/{vid}", "video_id": vid, "source": src}) + "\n")
        (d / "derived" / "pool.tsv").write_text("video_id\tweek\tpriority\treasons\tcap\n101\tw1\t1\tkey\t20\n"
                                                 "201\tw1\t1\tkey\t20\n202\tw1\t1\tkey\t20\n", encoding="utf-8")
        with open(d / "raw" / "comments.jsonl", "w", encoding="utf-8") as f:   # 1ページ目は取り終え、2ページ目は途中まで
            for vid in ("101", "201"):
                f.write(json.dumps({"video_id": vid, "status": "ok", "comments": [{"aweme_id": vid}]}) + "\n")

        def ok202(c):
            with open(c.a.out, "a", encoding="utf-8") as f:
                f.write(json.dumps({"video_id": "202", "status": "ok", "comments": [{"aweme_id": "202"}]}) + "\n")
            c.rows.append({"video_id": "202", "status": "ok"})
        _FakeCollector.behaviors = [ok202, ok202]   # 2つ目は、1ページ目も渡してしまうとき（直す前）の分
        with mock.patch.object(self.pl.time, "sleep", lambda s: None):
            res = self.pl.Run(self.aid).step_comments()
        self.assertEqual([a.music_url for a in _FakeCollector.calls], [U2],
                         "取り終えた1ページ目も spatest に渡した（探す動画が0本でもグリッドを約5分スクロールする）")
        self.assertEqual(res["videos_ok"], 3)

    def test_bug_block_backoff_survives_restart(self):
        """ブロックのあと90分空けている途中で係が止められる（ふたを閉じた・アプリを閉じた）と、続きから始めたとき
        空けも速さの切り下げも消えていて、すぐ元の速さ（3回/分）で取りに行く"""
        d = self.pl.DEFAULTS
        _FakeCollector.behaviors = [_blocked]

        def killed(s):
            raise _Killed()
        with mock.patch.object(self.pl.time, "sleep", killed):
            with self.assertRaises(_Killed):
                self.pl.Run(self.aid).step_comments()
        # 起きて続きから（新しい係）
        _FakeCollector.behaviors = [_ok]
        with mock.patch.object(self.pl.time, "sleep", lambda s: None):
            self.pl.Run(self.aid).step_comments()
        self.assertLessEqual(_FakeCollector.calls[-1].calls_per_min, d["fallback_calls_per_min"],
                             "ブロックの直後なのに元の速さで取りに行った（空けも無し）")


# ---------------------------------------------------------------------------
# 8. アプリの入口（worker_entry）: ログイン待ち・ちょいとり
# ---------------------------------------------------------------------------
class _FakeChrome:
    seq = []   # logged_in() が返す値（尽きたら最後の値）

    def __init__(self, *a, **k):
        pass

    def has_tab(self):
        return self.listening()

    def listening(self):
        return True

    def ensure(self, *a, **k):
        pass

    def logged_in(self):
        return _FakeChrome.seq.pop(0) if len(_FakeChrome.seq) > 1 else _FakeChrome.seq[0]


class TestWorkerEntry(AppBase):
    def _main(self):
        """worker_entry.main() を、係を回さずに通す（差し込みだけ行わせる）。差し込みは試験のあと元に戻す"""
        from collector_app import worker_entry
        pl = self.pl
        for target, attr in ((pl, "ensure_chrome"), (pl.Run, "step_list"), (pl.Run, "step_pool")):
            p = mock.patch.object(target, attr, getattr(target, attr))
            p.start()
            self.addCleanup(p.stop)
        for target, attr, val in ((self.wk, "main", lambda: 0), (self.cfg, "setup_env", lambda: None)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(worker_entry.main(), 0)
        return worker_entry

    def test_login_wait_then_continue(self):
        """（網羅）ログインが切れていたら印を置いて待ち、ログインされたら印を消して続ける"""
        we = self._main()
        _FakeChrome.seq = [False, False, True]
        logs = []
        with mock.patch.object(we.chrome_mod, "Chrome", _FakeChrome), mock.patch.object(we.time, "sleep", lambda s: None):
            self.pl.ensure_chrome(9250, logs.append)
        self.assertFalse(we.need_login_flag().exists())
        self.assertTrue(any("ログインを確かめました" in x for x in logs))

    def test_login_wait_timeout(self):
        """（網羅）ログインされないまま上限の時間がたったら止める（印は消す）"""
        we = self._main()
        _FakeChrome.seq = [False]
        with mock.patch.object(we.chrome_mod, "Chrome", _FakeChrome), mock.patch.object(we, "LOGIN_WAIT_HOURS", 0), \
                mock.patch.object(we.time, "sleep", lambda s: None):
            with self.assertRaises(self.pl.StepError):
                self.pl.ensure_chrome(9250, lambda m: None)
        self.assertFalse(we.need_login_flag().exists())

    def test_trial_cuts_per_page(self):
        """（網羅）ちょいとり: 一覧はページごとに先頭から同じ本数、プールはコメントを取る動画（cap>0）だけからページを順に"""
        self._main()
        aid = "a20261006-0849-tri"
        d = self.make(aid, {"status": "running", "steps": {}}, music_urls=[U1, U2],
                      acquisition_settings={"trial_links": 20, "trial_pool": 2, "trial_cap": 30})
        with open(d / "raw" / "grid_links.jsonl", "w", encoding="utf-8") as f:   # 一覧は済んでいる（取り直さない）
            for src in (1, 2):
                for i in range(30):
                    vid = f"{src}{i:03d}"
                    f.write(json.dumps({"url": f"https://www.tiktok.com/@x/video/{vid}", "video_id": vid,
                                        "type": "Video", "source": src}) + "\n")
        r = self.pl.Run(aid)
        res = r.step_list()
        self.assertEqual((res["links"], res["trial_links_from"]), (20, 60))
        srcs = [json.loads(x)["source"] for x in (d / "raw" / "grid_links.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((srcs.count(1), srcs.count(2)), (10, 10))
        (d / "derived" / "pool.tsv").write_text(
            "video_id\tweek\tpriority\treasons\tcap\n1000\tw1\t2\tweek\t0\n1001\tw1\t1\tkey\t20\n"
            "1002\tw1\t1\tkey\t20\n2000\tw1\t1\tkey\t20\n", encoding="utf-8")
        res = r.step_pool()
        self.assertEqual(res["n_pool"], 2)
        rows = (d / "derived" / "pool.tsv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertEqual(sorted(x.split("\t")[0] for x in rows), ["1001", "2000"])
        self.assertTrue(all(x.split("\t")[4] == "30" for x in rows))

    def test_bug_login_flag_removed_when_worker_terminated(self):
        """ログイン待ちの係が止められる（スリープ・アプリの終了・「取得をやめて」）と、SIGTERM では finally が走らず、
        need_login.json が残る（消すのはこの finally だけ）"""
        home = self.tmp / "home-sub"
        env = {**os.environ, "UGC_COLLECTOR_HOME": str(home), "UGC_LOCK_DIR": str(home / "locks"),
               "UGC_ANALYSES_DIR": str(home / "analyses"), "PYTHONUTF8": "1"}
        code = (
            "import sys\n"
            f"sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / 'collector')!r}]\n"
            "from collector_app import worker_entry, chrome as chrome_mod\n"
            "class C:\n"
            "    def __init__(self, *a, **k): pass\n"
            "    def has_tab(self): return True\n"
            "    def listening(self): return True\n"
            "    def ensure(self, **k): pass\n"
            "    def logged_in(self): return False\n"
            "chrome_mod.Chrome = C\n"
            "from acquire import pipeline, worker\n"
            "worker.main = lambda: (pipeline.ensure_chrome(9250, print), 0)[1]\n"
            "sys.exit(worker_entry.main())\n")
        proc = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        flag = home / "locks" / "need_login.json"
        t0 = time.time()
        while not flag.exists() and time.time() - t0 < 20 and proc.poll() is None:
            time.sleep(0.1)
        self.assertTrue(flag.exists(), "ログイン待ちに入らなかった（試験の前提）")
        proc.send_signal(signal.SIGTERM)   # jobs.Worker.stop と同じ止め方
        proc.wait(10)
        self.assertFalse(flag.exists(), "止めた係のログイン待ちの印が残った")


# ---------------------------------------------------------------------------
# 9. メニューバーのアプリの見張り（app.Controller）から見た、係・ログイン待ち・止まった取得・やめた取得
# ---------------------------------------------------------------------------
class _FakeAppChrome:
    def __init__(self, logged_in=True):
        self._in = logged_in

    def has_tab(self):
        return self.listening()

    def listening(self):
        return True

    def logged_in(self):
        return self._in

    def window_state(self):
        return "minimized"

    def ensure(self, *a, **k):
        pass

    def navigate(self, *a):
        pass

    def show(self):
        pass

    def minimize(self):
        pass

    def quit(self):
        pass


class TestAppWatch(AppBase):
    def setUp(self):
        super().setUp()
        from collector_app import app
        self.app = app
        self.notes = []
        for target, attr, val in ((app, "TEST_NO_CHROME", False), (app.chrome, "find_binary", lambda: "/x/Chrome"),
                                  (app, "ai_where", lambda: "Claude"), (app, "ai_installed", lambda: True),
                                  (app.notify, "send", lambda title, body="": self.notes.append(title))):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.ctl = app.Controller(self.cfg.code_dir(), logging.getLogger("r1"))
        self.ctl.chrome = _FakeAppChrome()
        self.starts = []
        self.ctl.worker.start = lambda: self.starts.append(1)
        self.cfg.save_state(logged_in_at="2026-10-06 09:00:00")

    def _known(self):
        for m in self.jobs.analyses():
            self.ctl.known[m["analysis_id"]] = (m.get("acquisition") or {}).get("status")

    def test_bug_stale_login_flag_loops(self):
        """ログイン待ちの係が止められて need_login.json が残った（上の試験）。係はいない・分析は running（PID は死んでいる）。
        ログイン済みなのに、見張りが5秒ごとに「ログイン待ち」に戻って「準備OK」を出し続け、係を起こさない"""
        self.ctl.worker.running = lambda: False
        self.make("a20261006-0849-lgn", {"status": "running", "pid": DEAD_PID, "step": "comments",
                                          "queued_at": "2026-10-06T08:49:00+09:00", "steps": {}})
        (self.ldir / "need_login.json").write_text('{"since": "2026-10-06T10:00:00+09:00"}', encoding="utf-8")
        self._known()
        for _ in range(4):
            self.ctl._tick()
        self.assertLessEqual(self.notes.count("準備OK"), 1, f"通知: {self.notes}")
        self.assertTrue(self.starts, "係を起こさなかった（取得が続きから始まらない）")

    def test_bug_failed_shows_on_first_line(self):
        """FRIEND_GUIDE「まず割れた音符のアイコンの1行目を見る …『止まっています』と出ていたら → 続きから再開」。
        取り直さない止まり方（回数切れ・ページ違い）のあと、1行目は「準備OK・待機中」で、止まっていることが出ない"""
        self.ctl.worker.running = lambda: False
        self.make("a20261006-0849-fl", {"status": "failed", "error": "楽曲ページから動画が1本も見つかりませんでした（URL が違うか、"
                                        "TikTok 側の表示制限）", "steps": {}})
        self._known()
        self.ctl._tick()
        self.assertIn("止まっています", self.ctl.status_text)

    def test_bug_cancel_does_not_kill_other_analysis(self):
        """分析 B が止まり（failed・PID は今の係）、同じ係が次の分析 C を取っている。B を「やめて（やり直して）」と頼むと、
        PID だけで照らすので、C を取っている係を止めてしまう（C は続きから取り直しになり、段の途中までが無駄になる）"""
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)   # 係の代わり
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.ctl.worker.proc = proc
        self.make("a-b", {"status": "cancelled", "pid": proc.pid, "cancel_reason": "利用者が別の楽曲ページでやり直した"})
        self.make("a-c", {"status": "running", "pid": proc.pid, "step": "enrich"})
        (self.ldir / "cancel-a-b").write_text("1", encoding="utf-8")
        self.ctl._handle_cancels()
        alive = proc.poll() is None
        self.assertTrue(alive, "C を取っている係が止められた")

    def test_cancel_running_stops_worker(self):
        """（網羅）取っている最中の分析をやめたら係を止め、状態を「やめた」に書き直す"""
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.ctl.worker.proc = proc
        self.make("a-run", {"status": "done", "pid": proc.pid})   # 係が最後に書いた状態で上書きされていた
        (self.ldir / "cancel-a-run").write_text("1", encoding="utf-8")
        self.ctl._handle_cancels()
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.meta("a-run")["acquisition"]["status"], "cancelled")


# ---------------------------------------------------------------------------
# 10. Mac を眠らせない（jobs.Worker の caffeinate）
# ---------------------------------------------------------------------------
class TestWorkerAwake(AppBase):
    def _patch_popen(self):
        real = subprocess.Popen
        self.cafs = []

        class Caf:
            def __init__(s, args):
                s.args, s.done = args, False

            def poll(s):
                return 0 if s.done else None

            def terminate(s):
                s.done = True

        def fake(args, *a, **k):
            if args and args[0] == "/usr/bin/caffeinate":
                c = Caf(list(args))
                self.cafs.append(c)
                return c
            return real(args, *a, **k)
        p = mock.patch.object(self.jobs.subprocess, "Popen", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return real

    def test_caffeinate_follows_worker(self):
        """（網羅）係が生きている間だけ -w で眠らせない。画面の設定を変えたら掛け直し、係を止めたら戻す"""
        real = self._patch_popen()
        proc = real(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        w = self.jobs.Worker(ROOT, logging.getLogger("r1"))
        w.proc = proc
        w._caffeinate(proc.pid)
        self.assertEqual(self.cafs[0].args, ["/usr/bin/caffeinate", "-i", "-w", str(proc.pid)])
        w.restart_display_setting(True)
        self.assertTrue(self.cafs[0].done)
        self.assertEqual(self.cafs[1].args[1], "-di")
        w.stop("試験")
        self.assertIsNotNone(proc.poll())
        self.assertTrue(self.cafs[1].done)

    def test_adopts_orphan_worker(self):
        """（網羅）前のアプリが起こした係（GUARD に PID）が生きていれば引き取り、眠らせない。止めればプロセスグループごと止まる"""
        real = self._patch_popen()
        proc = real(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.wk.GUARD.write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        w = self.jobs.Worker(ROOT, logging.getLogger("r1"))
        self.assertTrue(w.running())
        self.assertEqual(w.adopted, proc.pid)
        self.assertEqual(self.cafs[0].args[-1], str(proc.pid))
        w.stop("試験")
        proc.wait(5)
        self.assertTrue(self.cafs[0].done)


if __name__ == "__main__":
    unittest.main()
