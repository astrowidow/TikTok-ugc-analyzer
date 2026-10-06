"""領域 R3（AI に出す道具の入口と、分析の管理）の読み合わせで見つけた疑いを確かめる試験（2026-10-06、友達に配る前の通し試験）。

対象: mcp_proto.py（道具の説明・Code タブの待ちの一文）・collector/collector_app/mcp_local.py（プロセスをまたぐロック）・
proto_runner.py（start_analysis・楽曲ページの選び方・restart_analysis・cancel_analysis・status・next_task・曲名から分析を選ぶ所・
come_back_line・時刻と長さの書き方・状態の保存）。

どれも「正しい動き」を期待する形で書いてある。失敗する試験は不具合の疑い（docs/FULL_TEST_LOG.md の D1 ほか）。
TikTok・Chrome・取得の係・アプリには触らない（取得アプリの形 proto_runner.LOCAL は偽物）。置き場は一時フォルダ。

  collector/.venv/bin/python -m unittest tests.test_review_r3 -v
"""
import asyncio
import datetime
import json
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector"))

import flow_w1  # noqa: E402
import mcp_proto  # noqa: E402
import proto_runner as pr  # noqa: E402
from acquire import pipeline  # noqa: E402

U1 = "https://www.tiktok.com/music/test-7000000000000000011"
U2 = "https://www.tiktok.com/music/test2-7000000000000000012"
U3 = "https://www.tiktok.com/music/test3-7000000000000000013"
UNREADABLE = {"title": None, "creator": None, "video_count_text": None, "video_count": None}
# 楽曲ページが読めなかった（地域の制限で「この楽曲はご利用になれません」と出るなど）ことを、利用者か AI に知らせる言葉
WARN_RE = re.compile(r"注意|可能性|ご利用になれ|地域|見られな|開けな|読めませんでした")


def _page(title, creator, n):
    return {"title": title, "creator": creator, "video_count": n, "video_count_text": f"{n} 動画" if n else None}


class FakeLocal:
    """取得アプリの形（proto_runner.LOCAL）の偽物。TikTok にも Chrome にも触らない"""

    def __init__(self, pages=None, sounds=None):
        self.pages = pages or {}        # 楽曲ページの id → 読めた中身
        self.sounds = sounds or []      # find_sounds が返す、動画の音源の読み
        self.fail_many = False          # inspect_many で Chrome が起きない
        self.stopped = []

    def acquisition_settings(self):
        return {"chrome_port": "9250"}

    def app_state(self):
        return {"running": True, "login_wanted": False}

    def ensure_app(self):
        return "running"

    def request_stop(self, aid):
        self.stopped.append(aid)

    def _info(self, url):
        return dict(self.pages.get(url.rsplit("-", 1)[-1], _page("テスト", "だれか", 1000)))

    def inspect_music(self, url):
        return self._info(url)

    def inspect_many(self, urls):
        if self.fail_many:
            # 本物（mcp_local.LocalHooks.inspect_many）は scraper.create_headless_driver() が try の外。Chrome が起きなければ例外が上がる
            raise RuntimeError("Chrome を起こせませんでした（試験）")
        return [self._info(u) for u in urls]

    def find_sounds(self, song, video_urls=None, discover=True, **kw):
        return {"discover": "https://www.tiktok.com/discover/x", "found": len(self.sounds), "reads": [dict(r) for r in self.sounds]}

    def music_from_videos(self, urls):
        return []


class Base(unittest.TestCase):
    """一時フォルダの置き場・偽の LOCAL・知識ベースの仕事なし"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, pr._kb, pr.datetime)
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = self.base
        self.fake = FakeLocal()
        pr.LOCAL = self.fake
        pr._kb = lambda: None   # 知識ベースの仕事をはさまない（本物の置き場を見ない）

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, pr._kb, pr.datetime = self.saved
        self.tmp.cleanup()

    def meta(self, aid):
        return json.loads((self.base / aid / "analysis.json").read_text(encoding="utf-8"))

    def make(self, aid, title, acq="done", created="2026-10-01T00:00:00+09:00", complete=None, artist="",
             owner="u1", music_url=U1, extra=None):
        """分析フォルダを手で作る。complete=True で AI の仕事が全部済み（レポート完成）、False で AI の仕事が残っている"""
        d = self.base / aid
        for sub in ("raw", "state", "outputs", "derived"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        m = {"analysis_id": aid, "title": title, "song": {"title": title, "artist": artist}, "owner": owner,
             "created_at": created, "music_url": music_url, "music_urls": [music_url],
             "acquisition": {"status": acq, "queued_at": created, "steps": {}}}
        if acq == "cancelled":
            m["acquisition"]["cancel_reason"] = "利用者がやめた"
        if acq == "failed":
            m["acquisition"]["error"] = "ログインが切れた（試験）"
        if extra:
            m.update(extra)
        (d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        if complete is not None:
            st = {"analysis_id": aid, "flow": "w1", "next_n": 1, "tasks": []}
            st["tasks"].append(flow_w1._task(st, "finish", "ai", "note 用に仕上げる"))
            st["tasks"].append(flow_w1._task(st, "done", "done", "完了"))
            if complete:
                for t in st["tasks"]:
                    t["status"] = "done"
            (d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        return aid

    def active(self, user="u1"):
        return [a.id for a in pr.list_analyses(user)
                if (a.meta.get("acquisition") or {}).get("status") in ("queued", "running")]


# ---------------------------------------------------------------------------
# (c) 曲名から分析を選ぶ所（resolve / _pick）
# ---------------------------------------------------------------------------
class TestPickByTitle(Base):
    def test_running_beats_older_finished(self):
        """(c) 完成済み（古い）と取得中（新しい）→ 取得中（通し試験で見たとおり）"""
        done = self.make("a20260930-2342-done", "シルエット", "done", "2026-09-30T23:42:14+09:00", complete=True)
        run = self.make("a20261006-0849-runn", "シルエット", "running", "2026-10-06T08:49:35+09:00")
        self.assertEqual(pr.resolve("u1", "シルエット").id, run)
        self.assertEqual(pr.resolve("u1", None).id, run)
        self.assertNotEqual(done, run)

    def test_running_beats_newer_finished(self):
        """(c) 境目: 取得中のほうが古くても、取得中を選ぶ（進行中を優先）"""
        run = self.make("a20261001-0000-runn", "シルエット", "running", "2026-10-01T00:00:00+09:00")
        self.make("a20261005-0000-done", "シルエット", "done", "2026-10-05T00:00:00+09:00", complete=True)
        self.assertEqual(pr.resolve("u1", "シルエット").id, run)

    def test_ai_work_left_beats_finished(self):
        """(c) 境目: 取得は済んだが AI の仕事が残っている分析は、完成済みより先"""
        left = self.make("a20261001-0000-left", "シルエット", "done", "2026-10-01T00:00:00+09:00", complete=False)
        self.make("a20261005-0000-done", "シルエット", "done", "2026-10-05T00:00:00+09:00", complete=True)
        self.assertEqual(pr.resolve("u1", "シルエット").id, left)

    def test_exact_id_wins(self):
        """(c) 境目: 分析 ID をそのまま渡せば、進行中が別にあってもその分析（大文字でも）"""
        done = self.make("a20260930-2342-done", "シルエット", "done", "2026-09-30T23:42:14+09:00", complete=True)
        self.make("a20261006-0849-runn", "シルエット", "running", "2026-10-06T08:49:35+09:00")
        self.assertEqual(pr.resolve("u1", done).id, done)
        self.assertEqual(pr.resolve("u1", done.upper()).id, done)

    def test_next_task_by_title_goes_to_running(self):
        """(c) 曲名で next_task → 取得中のほうの「待ち」を返す（完成済みのレポートには触らない）"""
        self.make("a20260930-2342-done", "シルエット", "done", "2026-09-30T23:42:14+09:00", complete=True)
        run = self.make("a20261006-0849-runn", "シルエット", "running", "2026-10-06T08:49:35+09:00")
        r = pr.next_task("u1", "シルエット")
        self.assertEqual((r["kind"], r["analysis_id"]), ("wait", run))
        self.assertIn("取得中", r["text"])

    def test_finished_beats_newer_cancelled(self):
        """疑い: 完成済み（古い）より新しい「やめた分析」があると、曲名でやめた分析を選ぶ。
        運営の Mac の本物の並び（シルエット: 9/30 完成・10/4 22:57 にやめた）をそのまま写した。今朝の取得が始まる前は、
        「シルエットのレポートの3章を直して」「シルエットの分析を続けて」がやめた分析に向かっていた"""
        done = self.make("a20260930-2342-0035", "シルエット", "done", "2026-09-30T23:42:14+09:00", complete=True)
        self.make("a20261004-2257-74d8", "シルエット", "cancelled", "2026-10-04T22:57:09+09:00")
        self.assertEqual(pr.resolve("u1", "シルエット").id, done)

    def test_revise_by_title_after_cancel(self):
        """疑いの影響: 上の並びで「シルエットのレポートの3章に〜を足して」（revise を曲名で）→ 完成済みのレポートを直すはず"""
        done = self.make("a20260930-2342-0035", "シルエット", "done", "2026-09-30T23:42:14+09:00", complete=True)
        self.make("a20261004-2257-74d8", "シルエット", "cancelled", "2026-10-04T22:57:09+09:00")
        try:
            r = pr.revise("u1", "シルエット", "3章に音楽面の話を足して")
        except Exception as e:   # やめた分析を選ぶと「レポートがまだできていません」か、仕事の列が作れずに例外
            self.fail(f"完成済みのレポートの直しにならなかった: {type(e).__name__}: {e}")
        self.assertEqual(r["analysis_id"], done)

    def test_finished_beats_older_failed(self):
        """疑い: 前に止まった（failed）まま放ってある分析は「進行中」扱いなので、あとから頼み直して完成した同じ曲より先に選ばれる。
        （start_analysis の find_active は failed を見ないので、頼み直すと止まった分析が残る）"""
        self.make("a20261001-0000-fail", "シルエット", "failed", "2026-10-01T00:00:00+09:00")
        done = self.make("a20261003-0000-done", "シルエット", "done", "2026-10-03T00:00:00+09:00", complete=True)
        self.assertEqual(pr.resolve("u1", "シルエット").id, done)

    def test_exact_title_beats_partial(self):
        """疑い: 曲名は部分一致で当てるので、「Lemon」で頼むと取得中の「Lemonade」を選ぶ（題がぴったり合う分析を先にするはず）"""
        lemon = self.make("a20261001-0000-lemo", "Lemon", "done", "2026-10-01T00:00:00+09:00", complete=True, artist="米津玄師")
        self.make("a20261005-0000-ade0", "Lemonade", "running", "2026-10-05T00:00:00+09:00", artist="だれか")
        self.assertEqual(pr.resolve("u1", "Lemon").id, lemon)


# ---------------------------------------------------------------------------
# (b) やめた分析・やめ方
# ---------------------------------------------------------------------------
class TestCancelled(Base):
    def test_restart_after_cancel(self):
        """(b) 「〇〇の取得をやめて」のあとに「このページでやり直して <URL>」→ restart_analysis が「もうやめてあります」で止まる。
        道具の説明は「取得中・順番待ち・止まった分析の取得をやめ…music_url があればその楽曲ページで取り直す」。
        正しい動き: やめた分析でも、そのページで取り直す（返信・再生の下限・界隈の確認を省く頼みを引き継ぐ）。
        直し方でエラーのままにするなら、AI が次に何を呼べばよいか（start_analysis と music_urls）をエラーに書く"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1], replies=True, min_plays=10000, skip_confirm=True)
        aid = r["analysis_id"]
        pr.cancel_analysis("u1", aid)
        try:
            r2 = pr.restart_analysis("u1", aid, music_url=U2)
        except pr.RunnerError as e:
            self.assertIn("start_analysis", str(e), "やめた分析のやり直しがエラーで、次に何を呼ぶかも書いていない")
            self.assertIn("music_urls", str(e))
            return
        new = r2["analysis_id"]
        self.assertNotEqual(new, aid)
        m = self.meta(new)
        self.assertEqual((m["music_url"], m["acquisition"]["status"]), (U2, "queued"))
        self.assertTrue(pr.wants_replies(m))
        self.assertEqual(pr.min_plays_of(m), 10000)
        self.assertTrue(pr._options(self.base / new).get("skip_confirm"))

    def test_next_task_on_cancelled_does_not_promise_resume(self):
        """疑い: やめた分析に next_task（「〇〇の分析を続けて」）→「この取得はやめました」のあとに
        「取得が終わったら、利用者が『〇〇の分析を続けて』と言えば再開する」と続く。やめた取得は終わらないので、利用者は待ち続ける"""
        self.make("a20261004-2257-74d8", "テスト", "cancelled", "2026-10-04T22:57:09+09:00")
        r = pr.next_task("u1", "テスト")
        self.assertEqual(r["kind"], "wait")
        self.assertIn("やめました", r["text"])
        self.assertNotIn("取得が終わったら", r["text"])

    def test_cancel_twice_is_quiet(self):
        """やめた分析をもう一度やめても、エラーにせず「もうやめてあります」（通る）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        aid = r["analysis_id"]
        pr.cancel_analysis("u1", aid)
        r2 = pr.cancel_analysis("u1", aid)
        self.assertIn("もうやめてあります", r2["text"])
        self.assertEqual(self.fake.stopped, [aid])   # 止める印は1回だけ

    def test_cancel_done_is_refused(self):
        """取得が済んだ分析はやめられない（通る）"""
        aid = self.make("a20261001-0000-done", "テスト", "done", complete=True)
        with self.assertRaises(pr.RunnerError):
            pr.cancel_analysis("u1", aid)
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "done")

    def test_cancel_without_id_when_two_active(self):
        """R3-11（2026-10-06 ユーザーの決定で直した）: analysis_id を省いて cancel_analysis（壊す道具）を呼ぶと、前は取得中・順番待ちが
        2つあっても新しいほうを黙ってやめていた。今は、どれもやめずに一覧を返し、どちらをやめるか AI に利用者へ1回聞かせる"""
        a = self.make("a20261006-0900-aaaa", "曲A", "queued", "2026-10-06T09:00:00+09:00")
        b = self.make("a20261006-0910-bbbb", "曲B", "queued", "2026-10-06T09:10:00+09:00")
        r = pr.cancel_analysis("u1", None)
        self.assertEqual(sorted(self.active()), sorted([a, b]), "どちらをやめるか分からないのに、1つやめた")
        self.assertEqual(self.fake.stopped, [])
        self.assertIsNone(r["analysis_id"])
        self.assertIn(f"取得中・順番待ちの分析が2つあります: 曲A（順番待ち。分析 ID: {a}）、曲B（順番待ち。分析 ID: {b}）。"
                      "まだどれもやめていません。", r["text"])
        self.assertIn("どちらをやめるか利用者に1回聞き、答えの曲名か分析 ID で cancel_analysis を呼び直す", r["text"])
        r2 = pr.cancel_analysis("u1", "曲A")   # 答えの曲名で呼び直す → そのほうだけやめる
        self.assertEqual(r2["analysis_id"], a)
        self.assertEqual(self.active(), [b])

    def test_cancel_without_id_counts_deepen(self):
        """取り足し（完成後の界隈の掘り下げ）も数える。取得中の段の名前と「取り足し中」を一覧に出す"""
        a = self.make("a20261006-0900-aaaa", "曲A", "done", "2026-10-06T09:00:00+09:00", complete=True,
                      extra={"deepen": {"status": "running", "round": 1, "community": "dancer", "est_min": 10,
                                        "targets": [{"video_id": "1"}]}})
        b = self.make("a20261006-0910-bbbb", "曲B", "running", "2026-10-06T09:10:00+09:00")
        m = self.meta(b)
        m["acquisition"]["step"] = "comments"
        (self.base / b / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        r = pr.cancel_analysis("u1", None)
        self.assertIn(f"曲A（取り足し中。分析 ID: {a}）、曲B（取得中・コメントを取る。分析 ID: {b}）", r["text"])
        self.assertEqual(self.fake.stopped, [])
        self.assertEqual(self.meta(b)["acquisition"]["status"], "running")

    def test_cancel_without_id_when_one_active(self):
        """やめられる分析が1つだけなら、今どおりそれをやめる（完成済みの分析は数えない）"""
        self.make("a20261001-0000-done", "曲A", "done", "2026-10-01T00:00:00+09:00", complete=True)
        b = self.make("a20261006-0910-bbbb", "曲B", "queued", "2026-10-06T09:10:00+09:00")
        r = pr.cancel_analysis("u1", None)
        self.assertEqual(r["analysis_id"], b)
        self.assertEqual(self.meta(b)["acquisition"]["status"], "cancelled")
        self.assertEqual(self.fake.stopped, [b])


# ---------------------------------------------------------------------------
# 取得のやり直しの安全さと、引き継ぎ
# ---------------------------------------------------------------------------
class TestRestart(Base):
    def test_restart_keeps_old_when_new_start_fails(self):
        """疑い: restart_analysis は前の取得を先にやめてから、新しいページを開いて比べる。楽曲ページが2つ以上（「それも入れて」）で
        Chrome が起きないと、前の取得はやめたまま新しい分析も無い。AI がもう一度呼ぶと「もうやめてあります」で、取得が1つも残らない"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        aid = r["analysis_id"]
        self.fake.fail_many = True
        with self.assertRaises(Exception):
            pr.restart_analysis("u1", aid, music_urls=[U1, U2])
        self.assertTrue(self.active(), "前の取得をやめたのに、新しい取得が無い（Chrome が起きなかっただけで取得が消える）")

    def test_restart_checks_url_before_cancelling(self):
        """URL の形が違えば、前の取得をやめる前に断る（通る）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        aid = r["analysis_id"]
        with self.assertRaises(pr.RunnerError):
            pr.restart_analysis("u1", aid, music_url="https://www.tiktok.com/@x/video/123")
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "queued")
        self.assertEqual(self.fake.stopped, [])

    def test_restart_with_url_inherits_skip_confirm(self):
        """やり直しは界隈の確認を省く頼みも引き継ぐ（通る）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1], skip_confirm=True)
        self.assertIn(pr.CONFIRM_SKIPPED, r["text"])
        r2 = pr.restart_analysis("u1", r["analysis_id"], music_url=U2)
        self.assertTrue(pr._options(self.base / r2["analysis_id"]).get("skip_confirm"))
        self.assertIn(pr.CONFIRM_SKIPPED, r2["text"])
        self.assertTrue(r2["text"].startswith("「テスト」の前の取得"))
        self.assertEqual(r2["cancelled"], r["analysis_id"])

    def test_restart_from_search_keeps_all_notes(self):
        """楽曲ページ探しからのやり直し: 返信・再生の下限・確認を省く、の3つを AI への注に残す（通る）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1], replies=True, min_plays=10000, skip_confirm=True)
        aid = r["analysis_id"]
        res = pr.restart_analysis("u1", aid)
        for s in ("replies=true", "min_plays=10000", "skip_confirm=true"):
            self.assertIn(s, res["text"])
        self.assertEqual((res["analysis_id"], res["cancelled"]), (None, aid))
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "cancelled")
        self.assertEqual(self.fake.stopped, [aid])

    def test_restart_without_id_when_two(self):
        """analysis_id を省いた restart_analysis で、やり直せる分析が2つ以上 → どれもやめずに一覧を返し、どちらをやり直すか聞かせる
        （止まった分析も数える。同じ曲を頼み直して放ってある止まった分析は数えない）"""
        a = self.make("a20261006-0900-aaaa", "曲A", "failed", "2026-10-06T09:00:00+09:00")
        b = self.make("a20261006-0910-bbbb", "曲B", "queued", "2026-10-06T09:10:00+09:00", music_url=U2)
        r = pr.restart_analysis("u1", None, music_url=U3)
        self.assertIsNone(r["analysis_id"])
        self.assertIn(f"取得中・順番待ち・止まった分析が2つあります: 曲A（止まっている。分析 ID: {a}）、曲B（順番待ち。分析 ID: {b}）。"
                      "まだどれもやめていません。", r["text"])
        self.assertIn("どちらをやり直すか利用者に1回聞き、答えの曲名か分析 ID で restart_analysis を呼び直す。"
                      "music_url・music_urls などの指定は今回と同じものを渡す", r["text"])
        self.assertEqual(self.fake.stopped, [])
        self.assertEqual(sorted(x.id for x in pr.list_analyses("u1")), sorted([a, b]))   # 新しく作っていない
        self.assertEqual((self.meta(a)["acquisition"]["status"], self.meta(b)["acquisition"]["status"]), ("failed", "queued"))

    def test_restart_without_id_when_one(self):
        """やり直せる分析が1つだけなら、今どおりそれをやり直す（頼み直して放ってある止まった分析・完成済みは数えない）"""
        self.make("a20261001-0000-fail", "曲B", "failed", "2026-10-01T00:00:00+09:00")
        self.make("a20261002-0000-done", "曲A", "done", "2026-10-02T00:00:00+09:00", complete=True)
        b = self.make("a20261006-0910-bbbb", "曲B", "queued", "2026-10-06T09:10:00+09:00", music_url=U2)
        r = pr.restart_analysis("u1", None, music_url=U3)
        self.assertEqual(r["cancelled"], b)
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [U3])


# ---------------------------------------------------------------------------
# 日本の地域で使えない楽曲ページ（2026-10-06 ユーザーの決定。「この楽曲はご利用になれません」と出るページ）
# ---------------------------------------------------------------------------
UNAVAILABLE = {**UNREADABLE, "unavailable": True}
REGION_TEXT = "この楽曲はご利用になれません\nこのサウンドはお住いの国または地域ではご利用になれません"


class TestUnavailablePage(Base):
    BIBI = "https://www.tiktok.com/music/bibbidiba-7000000000000000031"
    BIBI2 = "https://www.tiktok.com/music/bibbidiba-2-7000000000000000032"

    def _sounds(self, *urls_and_counts):
        return TestUnreadablePage._sounds(self, *urls_and_counts)

    def _id(self, url):
        return url.rsplit("-", 1)[-1]

    def test_only_unavailable_page_gives_hint(self):
        """動画の音源から見つけた楽曲ページが1つで、日本の地域で使えない → 分析を作らず、ほかのページの探し方を返す（利用者には聞かない）"""
        self.fake.sounds = self._sounds((self.BIBI, 3))
        self.fake.pages = {self._id(self.BIBI): UNAVAILABLE}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい", skip_confirm=True)
        self.assertIsNone(r["analysis_id"])
        self.assertEqual(pr.list_analyses("u1"), [])
        t = r["text"]
        self.assertTrue(t.startswith("まだ取得を始めていない。この楽曲ページは日本では使えない（TikTok の地域の制限。"), t)
        self.assertIn(self.BIBI, t)
        self.assertIn("同じ曲のほかの公式の楽曲ページ（`https://www.tiktok.com/music/…`。sped up 版・別のアップロードなど）を探す", t)
        self.assertIn("見つかれば、その URL を music_urls に入れて start_analysis を呼び直す", t)
        self.assertIn("利用者に「この曲は TikTok の日本の地域で使えないため分析できません」と伝えて止まる", t)
        self.assertIn("skip_confirm=true", t)   # 呼び直すときも頼みを引き継ぐ注（ほかの探し方の返事と同じ）

    def test_all_candidates_unavailable_gives_hint(self):
        """候補が2つとも使えない → 分析を作らず、両方を挙げて探し方を返す"""
        self.fake.sounds = self._sounds((self.BIBI, 2), (self.BIBI2, 2))
        self.fake.pages = {self._id(u): UNAVAILABLE for u in (self.BIBI, self.BIBI2)}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい")
        self.assertIsNone(r["analysis_id"])
        self.assertIn("これらの楽曲ページは日本では使えない", r["text"])
        self.assertIn(self.BIBI, r["text"])
        self.assertIn(self.BIBI2, r["text"])
        self.assertEqual(pr.list_analyses("u1"), [])

    def test_user_given_unavailable_url(self):
        """利用者が渡した URL（music_urls）でも同じ: 1つでも2つでも、全部使えなければ作らない"""
        self.fake.pages = {self._id(u): UNAVAILABLE for u in (self.BIBI, self.BIBI2)}
        for urls in ([self.BIBI], [self.BIBI, self.BIBI2]):
            r = pr.start_analysis("u1", "ビビデバ", "星街すいせい", music_urls=urls)
            self.assertIsNone(r["analysis_id"], urls)
            self.assertIn("music_urls に入れて start_analysis を呼び直す", r["text"])
        self.assertEqual(pr.list_analyses("u1"), [])

    def test_mixed_drops_unavailable(self):
        """使えるページと混ざっていれば、使えないほうを外して始める（「それも入れて」とは言わない）"""
        self.fake.sounds = self._sounds((self.BIBI, 3), (self.BIBI2, 2))
        self.fake.pages = {self._id(self.BIBI): UNAVAILABLE, self._id(self.BIBI2): _page("ビビデバ", "星街すいせい", 52000)}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい")
        self.assertTrue(r["created"])
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [self.BIBI2])
        self.assertIn("次の楽曲ページで進めます", r["text"])
        self.assertIn(f"外した楽曲ページ:\n  - （UGC 読めず）{self.BIBI} … 日本の地域では使えない（TikTok の地域の制限）", r["text"])
        self.assertNotIn("それも入れて", r["text"])
        self.assertNotRegex(r["text"].split("外した楽曲ページ", 1)[0], WARN_RE)   # 使えるページには注意を出さない

    def test_mixed_user_urls_drops_unavailable(self):
        """利用者が渡した2つのうち1つが使えない → 使えるほうだけで始め、外したと伝える"""
        self.fake.pages = {self._id(self.BIBI): UNAVAILABLE, self._id(self.BIBI2): _page("ビビデバ", "星街すいせい", 52000)}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい", music_urls=[self.BIBI, self.BIBI2])
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [self.BIBI2])
        self.assertIn("（渡された楽曲ページ 2 つのうち、日本の地域で使える 1 つで取る）", r["text"])
        self.assertIn("日本の地域では使えない（TikTok の地域の制限）", r["text"])

    def test_restart_to_unavailable_keeps_old(self):
        """「このページでやり直して」の新しいページが使えない → 前の取得はやめずに残し、restart_analysis で呼び直すよう返す"""
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい", music_urls=[U1], skip_confirm=True)
        aid = r["analysis_id"]
        self.fake.pages = {self._id(self.BIBI): UNAVAILABLE}
        r2 = pr.restart_analysis("u1", aid, music_url=self.BIBI)
        self.assertIsNone(r2["analysis_id"])
        self.assertNotIn("cancelled", r2)
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "queued")
        self.assertEqual(self.fake.stopped, [])
        self.assertTrue(r2["text"].startswith(f"「ビビデバ」の前の取得（{U1}）はまだやめていません。"), r2["text"])
        self.assertIn(f"music_urls に入れて restart_analysis（analysis_id={aid}）を呼び直す", r2["text"])
        self.assertNotIn("start_analysis を呼び直す", r2["text"])
        self.assertEqual(len(pr.list_analyses("u1")), 1)

    def test_read_music_page_marks_unavailable(self):
        """楽曲ページを読む所: 題も UGC 数も読めず、本文に地域の制限の文（日本語・英語）があるときだけ unavailable=True。待たずに返す"""
        def driver(head, body):
            def run(js):
                if js == pipeline.MUSIC_PAGE_JS:
                    return dict(head)
                if js == pipeline.PAGE_TEXT_JS:
                    return body
                raise AssertionError(js)
            return types.SimpleNamespace(execute_script=run)
        empty = {"title": None, "creator": None, "video_count_text": None}
        t0 = time.time()
        self.assertTrue(pipeline.read_music_page(driver(empty, REGION_TEXT), wait=30).get("unavailable"))
        self.assertTrue(pipeline.read_music_page(driver(empty, "This sound isn't available in your country or region"),
                                                 wait=30).get("unavailable"))
        self.assertLess(time.time() - t0, 5, "使えないページで待った")
        # 文が無い（読み込みがたまたま失敗した）・題が読めた・UGC 数が読めた、のどれも使えないとはみなさない
        self.assertNotIn("unavailable", pipeline.read_music_page(driver(empty, "読み込み中"), wait=0))
        self.assertNotIn("unavailable", pipeline.read_music_page(driver({**empty, "title": "ビビデバ"}, REGION_TEXT), wait=0))
        ok = pipeline.read_music_page(driver({**empty, "video_count_text": "52K 動画"}, REGION_TEXT), wait=0)
        self.assertEqual((ok.get("unavailable"), ok["video_count"]), (None, 52000))

    def _run_list(self, body):
        """一覧の段（step_list）を、偽の Chrome（題も動画も出ない楽曲ページ）で走らせる"""
        aid = "a20261006-0900-bibi"
        self.make(aid, "ビビデバ", "running", music_url=self.BIBI,
                  extra={"acquisition_settings": {"list_sets": 1, "list_scrolls": 2, "list_stall": 1}})

        class Driver:
            def get(self, u):
                pass

            def execute_script(self, js):
                if js == pipeline.MUSIC_PAGE_JS:
                    return {"title": None, "creator": None, "video_count_text": None}
                if js == pipeline.PAGE_TEXT_JS:
                    return body
                return 0 if js == "COUNT" else []

            def find_element(self, *a):
                return types.SimpleNamespace(send_keys=lambda *k: None)

            def quit(self):
                pass
        fake = types.SimpleNamespace(create_headless_driver=Driver, PAGE_LOAD_TIME=0, SCROLL_PAUSE_TIME=0, _COUNT_LINKS_JS="COUNT")
        old_mod, old_wait = sys.modules.get("scraper"), pipeline.read_music_page.__defaults__
        sys.modules["scraper"] = fake
        pipeline.read_music_page.__defaults__ = (0,)
        try:
            run = pipeline.Run(aid)
            run.lock = lambda what: None
            with self.assertRaises(pipeline.StepError) as cm:
                run.step_list()
            return aid, str(cm.exception)
        finally:
            pipeline.read_music_page.__defaults__ = old_wait
            if old_mod is not None:
                sys.modules["scraper"] = old_mod
            else:
                sys.modules.pop("scraper", None)

    def test_list_step_stops_with_region_text(self):
        """一覧の段: 使えないページで動画が0本 → 止まる文は別のページでのやり直し方。アプリは取り直さない（jobs.retriable が False）。
        「〇〇の分析を続けて」にも、続きから再開や「取得が終わったら」を言わない"""
        from collector_app import jobs
        aid, err = self._run_list(REGION_TEXT)
        self.assertEqual(err, "この楽曲ページは日本の地域では使えません（TikTok の地域の制限）。ほかの楽曲ページの URL で"
                              "『ビビデバの取得をやめて、このページでやり直して』と頼んでください")
        self.assertFalse(jobs.retriable({"acquisition": {"status": "failed", "error": err}}))
        m = self.meta(aid)
        m["acquisition"].update({"status": "failed", "error": err})
        (self.base / aid / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        w = pr.next_task("u1", aid)
        self.assertEqual((w["kind"], w["state"]), ("wait", "failed"))
        self.assertIn("取得が止まりました。この楽曲ページは日本の地域では使えません", w["text"])
        self.assertNotIn("続きから", w["text"])
        self.assertNotIn("取得が終わったら", w["text"])

    def test_list_step_plain_empty_page_keeps_old_text(self):
        """くらべ: 地域の制限の文が無い（ただ動画が出ない）ページは、今までの止まる文のまま"""
        _aid, err = self._run_list("")
        self.assertEqual(err, "楽曲ページから動画が1本も見つかりませんでした（URL が違うか、TikTok 側の表示制限）")


# ---------------------------------------------------------------------------
# (a) 開けない楽曲ページ（地域の制限など）
# ---------------------------------------------------------------------------
class TestUnreadablePage(Base):
    BIBI = "https://www.tiktok.com/music/bibbidiba-7000000000000000031"
    BIBI2 = "https://www.tiktok.com/music/bibbidiba-2-7000000000000000032"

    def _sounds(self, *urls_and_counts):
        reads = []
        for url, k in urls_and_counts:
            for i in range(k):
                reads.append({"id": url.rsplit("-", 1)[-1], "title": "ビビデバ", "author": "星街すいせい",
                              "music_url": url, "video": f"https://www.tiktok.com/@x/video/{len(reads)}"})
        return reads

    def test_single_unreadable_page_warns(self):
        """(a) 動画の音源からは楽曲ページが見つかったが、そのページは題・作者・UGC 数が1つも読めない（ログインなしで開くと
        「この楽曲はご利用になれません…お住いの国または地域では…」）。それでも注意なしで「約2時間」と言って始める。
        一覧の段も同じログインなしの読み方なので、取得は数分で「動画が1本も見つかりませんでした」で止まる。
        正しい動き: 始めない（AI に知らせる）か、始めるなら読めなかったこと・止まるかもしれないことを返事に書く"""
        self.fake.sounds = self._sounds((self.BIBI, 3))
        self.fake.pages = {self.BIBI.rsplit("-", 1)[-1]: UNREADABLE}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい")
        if r.get("analysis_id") is None:
            return
        self.assertIn("UGC 読めず", r["text"])
        self.assertRegex(r["text"], WARN_RE, "楽曲ページが何も読めないのに、注意なしで始めた:\n" + r["text"])

    def test_all_candidates_unreadable_warns(self):
        """(a) 候補が2つで、どちらも何も読めない → 1つ目を「一番使われているものを選んだ」と言って始める（比べられていない）"""
        self.fake.sounds = self._sounds((self.BIBI, 2), (self.BIBI2, 2))
        self.fake.pages = {u.rsplit("-", 1)[-1]: UNREADABLE for u in (self.BIBI, self.BIBI2)}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい")
        if r.get("analysis_id") is None:
            return
        self.assertRegex(r["text"], WARN_RE, "楽曲ページが何も読めないのに、注意なしで始めた:\n" + r["text"])

    def test_readable_page_has_no_warning(self):
        """くらべ: 読めるページ（公式・UGC 数あり）では注意を出さない（通る。直したあとも注意を出しすぎないため）"""
        self.fake.sounds = self._sounds((self.BIBI, 3))
        self.fake.pages = {self.BIBI.rsplit("-", 1)[-1]: _page("ビビデバ", "星街すいせい", 52000)}
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい")
        self.assertTrue(r["created"])
        self.assertIn("『ビビデバ／星街すいせい』（UGC 52000 動画）", r["text"])
        self.assertNotRegex(r["text"], WARN_RE)


# ---------------------------------------------------------------------------
# 道具の引数の組み合わせ
# ---------------------------------------------------------------------------
class TestStartArgs(Base):
    def test_existing_reply_shows_the_page_in_use(self):
        """疑い: 同じ曲の取得がすでに順番待ちのとき、別の楽曲ページで頼み直すと「取得はもう受け付けています」と言いながら、
        本文は「次の楽曲ページで進めます: （UGC 読めず）<今回渡したページ>」。実際に取るのは前のページ（U1）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        r2 = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U2])
        self.assertFalse(r2["created"])
        self.assertEqual(r2["analysis_id"], r["analysis_id"])
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [U1])
        self.assertIn(U1, r2["text"], "実際に取るページが返事に無い:\n" + r2["text"])

    def test_start_again_when_acquired_but_not_written(self):
        """R3-6（2026-10-06 ユーザーの決定 B で直した）: 取得が済んで（通知が出て）レポートはまだの曲に、利用者が「続けて」でなく
        「UGC Analyzer で〇〇を分析して」と言い直す。前は find_active が取得中・順番待ちしか見ず、同じ曲の取得をもう一度（2〜3時間）始めていた。
        今は新しく作らず、済んだ分析でそのまま書き始める（返事にその分析 ID と「このまま書き始めます」、AI には next_task から進める注）"""
        old = self.make("a20261006-0900-old0", "テスト", "done", "2026-10-06T09:00:00+09:00", artist="だれか")
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        self.assertFalse(r["created"])
        self.assertEqual((r["analysis_id"], r["kind"]), (old, "resume"))
        self.assertTrue(r["text"].startswith(f"『テスト』はもう集め終わっています（分析 ID: {old}）。このまま書き始めます。"), r["text"])
        self.assertIn(f"すぐ next_task（analysis_id={old}）を呼んで、「テストの分析を続けて」と言われたときと同じに進める。取得を待たない",
                      r["text"])
        self.assertEqual([a.id for a in pr.list_analyses("u1")], [old], "新しい分析を作った")
        self.assertEqual(self.fake.stopped, [])

    def test_resume_does_not_open_tiktok(self):
        """取得済みの曲の頼み直しは、TikTok を開く前に見る（曲名だけで頼まれても、楽曲ページ探しをしない）"""
        old = self.make("a20261006-0900-old0", "テスト", "done", "2026-10-06T09:00:00+09:00", complete=False)

        def no_tiktok(*a, **kw):
            raise AssertionError("TikTok を開いた")
        self.fake.find_sounds = self.fake.inspect_many = self.fake.inspect_music = no_tiktok
        r = pr.start_analysis("u1", "テスト", "だれか")
        self.assertEqual((r["analysis_id"], r["created"]), (old, False))
        self.assertIn("このまま書き始めます", r["text"])

    def test_resume_by_same_music_page(self):
        """曲名の書き方が違っても、同じ楽曲ページを取り終えた分析なら書き始める（find_active と同じ当て方）"""
        old = self.make("a20261006-0900-old0", "Bibbidiba", "done", "2026-10-06T09:00:00+09:00", complete=False)
        r = pr.start_analysis("u1", "ビビデバ", "星街すいせい", music_urls=[U1])
        self.assertEqual((r["analysis_id"], r["created"]), (old, False))

    def test_resume_with_skip_confirm_marks_analysis(self):
        """skip_confirm=true で頼み直したら、その分析に確認を省く印を付ける（start_analysis の _set_options と同じ）。
        返信・再生の下限の指定は効かないと伝える（黙って捨てない）"""
        old = self.make("a20261006-0900-old0", "テスト", "done", "2026-10-06T09:00:00+09:00", complete=False)
        r = pr.start_analysis("u1", "テスト", "だれか", skip_confirm=True, replies=True, min_plays=5000)
        self.assertEqual(r["analysis_id"], old)
        self.assertTrue(pr._options(self.base / old).get("skip_confirm"))
        self.assertIn("（返信も取る・再生の下限の指定は、もう集め終わったこの分析には効きません）", r["text"])
        self.assertNotIn("start_analysis を呼び直す", r["text"])   # 探し方の注は付けない

    def test_finished_song_starts_new(self):
        """レポートが完成済みの曲は、今どおり新しく始める（直しや掘り下げで仕事が足されていても完成済み）"""
        old = self.make("a20261001-0000-done", "テスト", "done", "2026-10-01T00:00:00+09:00", complete=True)
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        self.assertTrue(r["created"])
        self.assertNotEqual(r["analysis_id"], old)
        # 完成のあとに直しの仕事が足された（_all_done ではない）分析も、完成済みとして扱う
        st = json.loads((self.base / old / "state" / "tasks.json").read_text(encoding="utf-8"))
        st["tasks"].append(flow_w1._task(st, "finish", "ai", "直し"))
        (self.base / old / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        pr.cancel_analysis("u1", r["analysis_id"])
        r2 = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        self.assertTrue(r2["created"])
        self.assertNotIn(r2["analysis_id"], (old, r["analysis_id"]))

    def test_failed_or_cancelled_song_starts_new(self):
        """止まった（failed）・やめた分析は対象外（今どおり新しく始める）"""
        for i, acq in enumerate(("failed", "cancelled")):
            song = f"テスト{i}"
            old = self.make(f"a2026100{i + 1}-0000-old{i}", song, acq, f"2026-10-0{i + 1}T00:00:00+09:00",
                            music_url=f"https://www.tiktok.com/music/x-700000000000000009{i}")
            r = pr.start_analysis("u1", song, "だれか", music_urls=[f"https://www.tiktok.com/music/x-700000000000000009{i}"])
            self.assertTrue(r["created"], acq)
            self.assertNotEqual(r["analysis_id"], old)
            pr.cancel_analysis("u1", r["analysis_id"])

    def test_restart_does_not_resume(self):
        """「取得をやめて、このページでやり直して」（restart_analysis）は、取得済みの同じ曲があっても新しく取る（利用者が取り直しを頼んだ）"""
        self.make("a20261006-0800-old0", "テスト", "done", "2026-10-06T08:00:00+09:00", complete=False)
        q = self.make("a20261006-0900-que0", "テスト", "queued", "2026-10-06T09:00:00+09:00", music_url=U2)
        r = pr.restart_analysis("u1", q, music_url=U3)
        self.assertTrue(r["created"])
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [U3])
        self.assertEqual(r["cancelled"], q)

    def test_existing_min_plays_not_silently_dropped(self):
        """疑い: すでに受け付けた取得に min_plays を付けて頼み直すと、効かないのに何も言わない（replies には「効きません」と言う）"""
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        r2 = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1], min_plays=10000)
        self.assertFalse(r2["created"])
        applied = pr.min_plays_of(self.meta(r["analysis_id"])) == 10000
        self.assertTrue(applied or "効き" in r2["text"],
                        "再生の下限の頼みが黙って捨てられた:\n" + r2["text"])

    def test_existing_replies_says_not_applied(self):
        """くらべ: replies は「すでに受け付けている取得には効きません」と言う（通る）"""
        pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        r2 = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1], replies=True)
        self.assertIn("効きません", r2["text"])

    def test_music_url_and_music_urls_together(self):
        """music_url と music_urls を両方渡す → 全部取る・主は UGC が一番多いページ（通る）"""
        self.fake.pages = {U1.rsplit("-", 1)[-1]: _page("曲", "歌手", 1000), U2.rsplit("-", 1)[-1]: _page("曲", "歌手", 5000)}
        r = pr.start_analysis("u1", "曲", "歌手", music_url=U1, music_urls=[U2])
        m = self.meta(r["analysis_id"])
        self.assertEqual((m["music_url"], m["music_urls"]), (U2, [U2, U1]))
        self.assertIn("渡された楽曲ページ 2 つを全部合わせて取る", r["text"])

    def test_search_hint_keeps_options(self):
        """楽曲ページが見つからず探し方を返すときも、返信・再生の下限・確認を省く、を呼び直しの注に残す（通る）"""
        r = pr.start_analysis("u1", "みつからない", "だれか", replies=True, min_plays=5000, skip_confirm=True)
        self.assertIsNone(r["analysis_id"])
        for s in ("replies=true", "min_plays=5000", "skip_confirm=true"):
            self.assertIn(s, r["text"])
        self.assertEqual(pr.list_analyses("u1"), [])

    def test_join_ratio_boundary(self):
        """20% ちょうどは合わせて取り、それ未満・UGC が読めない・個人の音源は外す（通る）"""
        urls = [f"https://www.tiktok.com/music/x-70000000000000001{i}" for i in range(5)]
        self.fake.pages = {urls[0].rsplit("-", 1)[-1]: _page("曲", "歌手", 10000),
                           urls[1].rsplit("-", 1)[-1]: _page("曲 (sped up)", "歌手", 2000),
                           urls[2].rsplit("-", 1)[-1]: _page("曲", "歌手", 1999),
                           urls[3].rsplit("-", 1)[-1]: _page("曲", "歌手", None),
                           urls[4].rsplit("-", 1)[-1]: _page("オリジナル楽曲 - someone", "someone", 90000)}
        take, dropped = pr._pick_music_pages("曲", "歌手", urls)
        self.assertEqual([u for u, _ in take], urls[:2])
        why = {u: w for u, _, w in dropped}
        self.assertIn("20%未満", why[urls[2]])
        self.assertIn("読めなかった", why[urls[3]])
        self.assertIn("個人の音源", why[urls[4]])


# ---------------------------------------------------------------------------
# 状態の保存が壊れたとき・置き場に分析でないものがあるとき
# ---------------------------------------------------------------------------
class TestBrokenStore(Base):
    def _status_or_fail(self):
        try:
            return pr.status("u1")
        except Exception as e:
            self.fail(f"status が例外で止まった（道具は全部「サービス側のエラーです（{type(e).__name__}）」になる）: {e}")

    def test_ds_store_in_analyses_dir(self):
        """疑い（重い）: 分析の置き場に .DS_Store（Finder が作るファイル）があると、list_analyses が NotADirectoryError で止まり、
        status・next_task・submit・start_analysis… 全部の道具がエラーになる。メニュー「そのほか → データのフォルダを開く」で
        Finder がこの置き場を開く。運営の Mac でもデータのルートとレポートのフォルダにはもう .DS_Store がある"""
        aid = self.make("a20261001-0000-done", "テスト", "done", complete=True)
        (self.base / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        r = self._status_or_fail()
        self.assertEqual([x["analysis_id"] for x in r["analyses"]], [aid])

    def test_ds_store_blocks_start(self):
        """疑い（重い）: 同じく .DS_Store があると start_analysis も止まる（launch.find_active・progress も同じ読み方）"""
        (self.base / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        try:
            r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1])
        except Exception as e:
            self.fail(f"start_analysis が例外で止まった: {type(e).__name__}: {e}")
        self.assertTrue(r["created"])

    def test_broken_analysis_json_does_not_block_others(self):
        """疑い: 1つの分析の analysis.json が壊れている（電源が落ちた・途中で切れた写し）と、ほかの分析の道具も全部止まる"""
        good = self.make("a20261001-0000-good", "テスト", "done", complete=True)
        bad = self.base / "a20261002-0000-bad0"
        bad.mkdir()
        (bad / "analysis.json").write_text('{"analysis_id": "a20261002-0000-bad0", "title": "こわ', encoding="utf-8")
        r = self._status_or_fail()
        self.assertIn(good, [x["analysis_id"] for x in r["analyses"]])

    def test_broken_analysis_json_does_not_block_deepen_view(self):
        """疑い: 掘り下げの取り足しが順番待ちのとき、_deepen_view が置き場の目録を全部読んで「いま取得中の分析」を探す所で、
        壊れた analysis.json が1つあると JSONDecodeError で止まる（status・next_task が全部エラー）。読めない目録は飛ばすはず"""
        aid = self.make("a20261001-0000-deep", "テスト", "done", complete=True,
                        extra={"deepen": {"status": "queued", "round": 1, "community": "dancer", "est_min": 10,
                                          "targets": [{"video_id": "1"}]}})
        bad = self.base / "a20261002-0000-bad0"
        bad.mkdir()
        (bad / "analysis.json").write_text('{"analysis_id": "a20261002-0000-bad0", "title": "こわ', encoding="utf-8")
        r = self._status_or_fail()
        self.assertEqual([x["state"] for x in r["analyses"] if x["analysis_id"] == aid], ["deepening"])

    def test_write_json_crash_keeps_old_state(self):
        """仕事の状態の書き込みが途中で落ちても、前の tasks.json はそのまま読める（一時ファイルに書いてから置き換える。通る）"""
        aid = self.make("a20261001-0000-good", "テスト", "done", complete=False)
        a = pr.Analysis(aid)
        before = pr._read_json(a.state_path)
        orig = pr.json.dump

        def broken_dump(obj, f, **kw):
            f.write('{"tasks": [')
            raise OSError("ディスクがいっぱい（試験）")
        pr.json.dump = broken_dump
        try:
            with self.assertRaises(OSError):
                pr._write_json(a.state_path, {"tasks": []})
        finally:
            pr.json.dump = orig
        self.assertEqual(pr._read_json(a.state_path), before)
        self.assertEqual(pr.resolve("u1", aid).id, aid)   # 書きかけの .tmp が残っても、ほかの道具は動く


# ---------------------------------------------------------------------------
# 道具のプロセスが2つ（Claude デスクトップは --mcp を2つ起こすことがある）
# ---------------------------------------------------------------------------
class TestLockAcrossProcesses(unittest.TestCase):
    def test_other_process_waits(self):
        """mcp_local.FileRLock: 別のプロセスが持っている間は待つ・同じプロセスの中では入れ子にできる（通る）"""
        from collector_app.mcp_local import FileRLock
        with tempfile.TemporaryDirectory() as tmp:
            lock, flag = Path(tmp) / "locks" / "runner.lock", Path(tmp) / "held"
            code = textwrap.dedent(f"""
                import sys, time
                from pathlib import Path
                sys.path.insert(0, {str(ROOT / 'collector')!r})
                from collector_app.mcp_local import FileRLock
                lk = FileRLock(Path({str(lock)!r}))
                with lk:
                    with lk:
                        Path({str(flag)!r}).write_text("1")
                        time.sleep(0.8)
            """)
            p = subprocess.Popen([sys.executable, "-c", code])
            try:
                t0 = time.time()
                while not flag.exists() and time.time() - t0 < 10:
                    time.sleep(0.02)
                self.assertTrue(flag.exists(), "子のプロセスがロックを取れなかった")
                lk = FileRLock(lock)
                t1 = time.time()
                with lk:
                    with lk:
                        waited = time.time() - t1
                self.assertGreater(waited, 0.3)
            finally:
                p.wait(timeout=10)


# ---------------------------------------------------------------------------
# 返事の文に出る時刻と長さ
# ---------------------------------------------------------------------------
class _FixedNow(datetime.datetime):
    FIXED = None

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED


def _shim(y, mo, d, h, mi):
    _FixedNow.FIXED = datetime.datetime(y, mo, d, h, mi).astimezone()
    return types.SimpleNamespace(datetime=_FixedNow, timedelta=datetime.timedelta, timezone=datetime.timezone)


class TestTimeWords(Base):
    def test_deepen_view_time_not_in_past(self):
        """疑い: 取り足しの残りが短いと、終わる時刻を30分刻みに切り下げて、今より前の時刻を出す。
        13:05 に残り10分 →「終わるのは今日の13時ごろの見込み（あと約10分）」。取り足しは7〜25分なので毎回起きうる
        （取得の段の _acq_view も同じ書き方。come_back_line は短いとき _clock_fine を使っている）"""
        pr.datetime = _shim(2026, 10, 6, 13, 5)
        now = _FixedNow.FIXED
        aid = self.make("a20261001-0000-deep", "テスト", "done", complete=True,
                        extra={"deepen": {"status": "running", "round": 1, "community": "dancer", "est_min": 10,
                                          "targets": [{"video_id": "1"}], "started_at": now.isoformat()}})
        dv = pr._deepen_view(pr.Analysis(aid))
        m = re.search(r"終わるのは(?:今日の)?(\d+)時(?:(\d+)分|(半))?ごろ", dv["message"])
        self.assertIsNotNone(m, dv["message"])
        shown = int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 30 if m.group(3) else 0)
        self.assertGreaterEqual(shown, now.hour * 60 + now.minute, "今より前の時刻を出した: " + dv["message"])

    def test_hm_whole_hours(self):
        """疑い（言葉）: 残りがちょうど何時間のとき「あと約2時間0分」と出る（status・取得中の待ち）"""
        self.assertEqual(pr._hm(7200), "2時間")
        self.assertEqual(pr._hm(3600 + 30), "1時間")

    def test_come_back_line_examples(self):
        """come_back_line の型（docs/DEEPEN_COMMUNITY.md 3-1）: 2時間未満は5分単位と「14時25分ごろ」、2時間以上は30分単位と
        「明日（10/7）の2時半ごろ」（通る）"""
        pr.datetime = _shim(2026, 10, 6, 14, 0)
        s = pr.come_back_line(1500, "この界隈のコメントを取り足すの", "テスト", "取り足した分でレポートを書き直します。")
        self.assertEqual(s, "この界隈のコメントを取り足すのに約25分かかります（14時25分ごろに終わる見込み）。"
                            "その間 AI は待てないため、Mac に通知が出たら「テストの分析を続けて」と頼んでください。取り足した分でレポートを書き直します。")
        s = pr.come_back_line(45000, "TikTok から動画とコメントを集めるの", "テスト", "そこからレポートを書きます。")
        self.assertIn("約12時間半かかります（明日（10/7）の2時半ごろに終わる見込み）", s)
        self.assertIn("に時間がかかります。", pr.come_back_line(None, "集めるの", "テスト", ""))


# ---------------------------------------------------------------------------
# 道具の説明（mcp_proto.py）とコードの動き
# ---------------------------------------------------------------------------
def _text(result) -> str:
    return "".join(getattr(c, "text", "") or "" for c in result.content)


def _code_tab_ctx():
    """Claude の Code タブ（Claude Code）から呼ばれたことにする ctx（初期化のときの clientInfo.name）"""
    info = types.SimpleNamespace(name="claude-code")
    return types.SimpleNamespace(session=types.SimpleNamespace(client_params=types.SimpleNamespace(client_info=info)))


class TestToolDescriptions(Base):
    def setUp(self):
        super().setUp()
        self.server = mcp_proto._build_server(user_of=lambda ctx: "u1", local=True)
        self.tools = {t.name: t for t in asyncio.run(self.server.list_tools())}

    def test_tools_and_rule(self):
        """道具は13。start_analysis の説明に名指しの決まり、cancel・restart の説明に「頼んだときだけ」（通る）"""
        self.assertEqual(len(self.tools), 13)
        self.assertIn("名指し", self.tools["start_analysis"].description)
        for n in ("cancel_analysis", "restart_analysis"):
            self.assertIn("ときだけ使う", self.tools[n].description)
        self.assertIn(mcp_proto.USE_RULE, mcp_proto.INSTRUCTIONS)

    def test_start_description_time(self):
        """食い違い: start_analysis の説明は「取得は…半日ほどかけてやり」。返事の一文（come_back_line）は約2時間、
        友達向けの紙は2〜3時間。AI が説明を読んで「半日」と伝えうる"""
        self.assertNotIn("半日", self.tools["start_analysis"].description)

    def test_restart_in_code_tab_gets_wait_line(self):
        """食い違い（準備中の Code タブ）: start_analysis の返事には Code タブ向けの「./ugc-wait を裏で走らせる」一文が付くが、
        restart_analysis（同じ「その間 AI は待てない」の一文を返す）には付かない（_waiting_text を通していない）"""
        ctx = _code_tab_ctx()
        t1 = _text(asyncio.run(self.server.call_tool("start_analysis", {"song": "テスト", "artist": "だれか",
                                                                         "music_urls": [U1]}, ctx)))
        self.assertIn("ugc-wait", t1)
        aid = re.search(r"分析 ID: (a[\w-]+)", t1).group(1)
        t2 = _text(asyncio.run(self.server.call_tool("restart_analysis", {"analysis_id": aid, "music_url": U2}, ctx)))
        self.assertIn("その間 AI は待てない", t2)
        self.assertIn("ugc-wait", t2)

    def test_descriptions_for_new_flows(self):
        """道具の説明: start_analysis に「このまま書き始めます」なら next_task から進める、cancel・restart に一覧が返ったら利用者に聞く"""
        self.assertIn("返事に「このまま書き始めます」とあれば（その曲はもう集め終わっている）、止まらずに、"
                      "「〇〇の分析を続けて」と言われたときと同じに next_task から進める", self.tools["start_analysis"].description)
        self.assertIn("どれをやめるか利用者に1回聞いて、答えの曲名か分析 ID で呼び直す", self.tools["cancel_analysis"].description)
        self.assertIn("どれをやり直すか利用者に1回聞いて、答えの曲名か分析 ID で呼び直す", self.tools["restart_analysis"].description)

    def test_resume_in_code_tab_has_no_wait_line(self):
        """取得済みの曲の頼み直し（このまま書き始めます）は待ちではないので、Code タブでも ./ugc-wait の一文を付けない"""
        old = self.make("a20261006-0900-old0", "テスト", "done", "2026-10-06T09:00:00+09:00", complete=False)
        t = _text(asyncio.run(self.server.call_tool("start_analysis", {"song": "テスト", "artist": "だれか"}, _code_tab_ctx())))
        self.assertIn(f"『テスト』はもう集め終わっています（分析 ID: {old}）。このまま書き始めます。", t)
        self.assertNotIn("ugc-wait", t)
        self.assertNotIn("Code タブ", t)


if __name__ == "__main__":
    unittest.main()
