"""0.8.1 の試験（docs/PLAN_0_8_1.md）: 設定の字数・一人称・章立ての変更（restructure）・動画を外す／戻す／足す（videos）・
動画ごと・全体の取り足し（deepen の videos・all・per_video）・比べる（compare）・消す（archive_analysis の delete）・容量（info の storage）。
TikTok・AI・Chrome・本物の置き場には触らない（取得の口 acquire.deepen は偽物に差し替える）。

土台は tests/test_redo_080 の RedoBase（完成済みの分析。seq 0〜7、界隈 dancer（0〜5）・student（6・7）、コメントは seq 0・1・6、
章 intro・path_P1（seq 0・1 を名指し）・path_P2（seq 6）・branch・music・result）。

  collector/.venv/bin/python -m unittest tests.test_flex_081 -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1 as fw  # noqa: E402
import proto_runner as pr  # noqa: E402
from tests.test_deepen import AID, vid  # noqa: E402
from tests.test_redo_080 import RedoBase  # noqa: E402
from tests.test_review_r4 import long_body  # noqa: E402


def outline_json(chs, extra=None):
    return {"thesis": "全体の主張", "thesis_changed": False,
            "chapters": [{"id": c, "role": "役割", "claims": [{"claim": "主張", "evidence": [{"seq": 0}]}], "bridge": "つなぎ"}
                         for c in chs],
            "rewrite": [{"chapter": c, "why": "直す"} for c in (extra or chs)], "note_to_user": "構成案を直した"}


class Flow(RedoBase):
    """道具（next_task・submit）で仕事を片付ける手伝い"""

    def next(self):
        return pr.next_task("u1", AID)

    def submit(self, t, out):
        r = pr.submit("u1", t["task_id"], out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
        self.assertTrue(r["ok"], r.get("text"))
        return r

    def meta(self) -> dict:
        return json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))

    def set_meta(self, **kv) -> None:
        m = self.meta()
        m.update(kv)
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        self.a = pr.Analysis(AID)


# ---------------------------------------------------------------------------
# 1・2 設定の字数・一人称
# ---------------------------------------------------------------------------
class TestVoice(unittest.TestCase):
    def test_no_fixed_first_person(self):
        for name in ("finish.md", "write.md"):
            text = (ROOT / "service_prompts" / "w1" / name).read_text(encoding="utf-8")
            self.assertNotIn("僕は考えています", text, name)
            self.assertIn("利用者の設定", text, name)
        self.assertIn("一人称", fw.STYLE_OVERRIDE)

    def test_settings_limit_and_description(self):
        import user_settings
        self.assertEqual(user_settings.MAX_CHARS, 2000)
        src = (ROOT / "mcp_proto.py").read_text(encoding="utf-8")
        self.assertIn("各2000字まで", src)
        self.assertIn("一人称は『私』", src)


# ---------------------------------------------------------------------------
# 3 章立て
# ---------------------------------------------------------------------------
class TestChapterPlan(Flow):
    def test_default_order_and_renumber(self):
        self.assertEqual(fw.chapter_order(self.a), ["intro", "path_P1", "path_P2", "branch", "music", "result"])
        self.assertEqual(fw._write_order(self.a), ["path_P1", "path_P2", "branch", "music", "result", "intro"])
        parts = [("path_P1", "## 1. バズの拡大経路\nx"), ("branch", "## 2. バズの分岐点\n### 分岐点①"),
                 ("music", "## 3. 楽曲\n## 4. 切り出し\n## 5. 時代"), ("result", "## 6. 結果\n## 7. 核\n## 8. 再現"), ("appendix", "## 9. 付録")]
        self.assertEqual(fw.renumber(parts), [t for _, t in parts])     # 既定の章立てなら番号は今と同じ

    def test_renumber_custom(self):
        parts = [("intro", "# 題\n## 本楽曲に着目すべき理由"), ("path_P1", "## 1. バズの拡大経路"), ("c1", "## ファン心理\n### 層"),
                 ("result", "## 6. 結果\n## 7. 核"), ("appendix", "## 9. 付録")]
        out = fw.renumber(parts)
        self.assertEqual(out[0], "# 題\n## 本楽曲に着目すべき理由")      # 番号の無い見出しはそのまま
        self.assertEqual(out[2], "## 2. ファン心理\n### 層")
        self.assertEqual(out[3], "## 3. 結果\n## 4. 核")
        self.assertEqual(out[4], "## 5. 付録")

    def test_plan_validation(self):
        with self.assertRaises(pr.RunnerError):
            fw.restructure_plan(self.a, remove=["nope"])
        with self.assertRaises(pr.RunnerError):
            fw.restructure_plan(self.a, order=["intro", "path", "branch", "music", "result"])   # 変わらない
        p = fw.restructure_plan(self.a, remove=["music"], add=[{"title": "ファン心理", "about": "ファンが何を感じたか", "after": "branch"}],
                                order=["intro", "path_P1", "result"])
        self.assertEqual(p["after"], ["intro", "path", "result", "branch", "c1"])
        self.assertEqual(p["removed"], ["music"])

    def test_restructure_after_done(self):
        music = (self.d / "outputs" / "chapters" / "music.md").read_text(encoding="utf-8")
        r = pr.restructure("u1", AID, remove=["music"], add=[{"title": "ファン心理", "about": "ファンが何を感じたかの章", "after": "branch"}],
                           instruction="音楽の章はいらない。代わりにファン心理の章を")
        self.assertIn("章立てを変えます", r["text"])
        self.assertIn("見込み", r["text"])
        a = pr.Analysis(AID)
        self.assertEqual(fw.chapter_order(a), ["intro", "path_P1", "path_P2", "branch", "c1", "result"])
        kept = self.d / "outputs" / "history" / "change_r1"
        self.assertEqual((kept / "chapters" / "music.md").read_text(encoding="utf-8"), music)   # 前の版を残す（章立ての表は前の版に無い）
        self.assertFalse((kept / "chapter_plan.json").exists())
        self.assertTrue((self.d / "outputs" / "chapters" / "music.md").exists())               # 外した章も消さない
        t = self.next()                                                                       # change_prep はサービスが片付ける
        self.assertIn("outline_revise", t["task_id"])
        self.assertIn("☆`c1`", t["text"])
        self.assertNotIn("{{", t["text"])
        self.assertIn("`intro`、`c1`", t["text"])
        self.assertIn("ファンが何を感じたかの章", t["text"])
        bad = pr.submit("u1", t["task_id"], json.dumps(outline_json(["path_P1"]), ensure_ascii=False))
        self.assertFalse(bad["ok"])
        self.assertIn("c1", bad["text"])
        self.submit(t, outline_json(["c1", "intro"]))
        t = self.next()
        self.assertIn("write", t["task_id"])
        self.assertIn("利用者が頼んで足した章「ファン心理」", t["text"])
        self.assertIn("## ファン心理", t["text"])
        self.submit(t, long_body("## ファン心理"))
        t = self.next()
        self.assertIn("write", t["task_id"])
        self.assertIn("書き直し", t["text"])
        self.submit(t, long_body(fw.heading_of(a, "intro") + " 〜TikTok今週の1曲"))
        t = self.next()
        self.assertIn("review", t["task_id"])
        self.assertIn("章立てを変えた", t["text"])
        self.assertNotIn("{{", t["text"])
        self.submit(t, {"fixes": [], "note_to_user": "通して読んだ"})
        for _ in range(2):
            t = self.next()
            self.assertIn("finish", t["task_id"])
            self.submit(t, "## ファン心理\n\n" + "仕上げた本文。" * 40 if "ファン心理）" in t["text"].splitlines()[0]
                        else fw.heading_of(a, "intro") + "\n\n" + "仕上げた本文。" * 40)
        t = self.next()
        self.assertEqual(t["kind"], "done")
        self.assertIn("章立てを変えた", t["text"])
        rep = (self.d / "outputs" / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("## 2. ファン心理", rep)
        self.assertIn("## 3. バズった結果得られたもの", rep)
        self.assertIn("## 4. 付録", rep)
        self.assertNotIn("楽曲の音楽的特徴", rep)
        self.assertIn("ファン心理", fw.chapters_listing(a))
        self.assertIn("music", fw.chapters_listing(a).split("章立てから外した章")[1])
        # 前の版に戻すと章立ても戻る
        pr.redo("u1", AID, restore="前の版")
        self.assertEqual(fw.chapter_order(pr.Analysis(AID)), ["intro", "path_P1", "path_P2", "branch", "music", "result"])

    def test_restructure_before_outline(self):
        """構成案の前（第1版を書く前）なら、章立ての表を書いて、積んである執筆・仕上げを積み直すだけ"""
        o = self.d / "outputs"
        for f in ("outline.json",):
            (o / f).unlink()
        shutil.rmtree(o / "chapters")
        shutil.rmtree(o / "note_chapters")
        st = {"analysis_id": AID, "flow": "w1", "next_n": 1, "tasks": []}
        st["tasks"] = [fw._task(st, "outline", "ai", "構成案")] + fw._flow_tail(self.a, st, write=None, finish=None)
        self.save(st)
        r = pr.restructure("u1", AID, remove=["music"], add=[{"title": "ファン心理", "about": "x"}])
        self.assertIn("最初からこの章立てで書きます", r["text"])
        writes = [x["params"]["chapter"] for x in self.st()["tasks"] if x["type"] == "write"]
        self.assertEqual(writes, ["path_P1", "path_P2", "branch", "result", "c1", "intro"])
        fins = [x["params"]["chapter"] for x in self.st()["tasks"] if x["type"] == "finish"]
        self.assertEqual(fins, ["intro", "path_P1", "path_P2", "branch", "result", "c1"])
        self.assertFalse((o / "history" / "change_r1").exists())

    def test_restructure_queued_while_writing(self):
        st = self.st()
        st["tasks"] = [x for x in st["tasks"] if x["kind"] != "done"]   # まだ第1版の途中
        st["tasks"].append(fw._task(st, "write", "ai", "執筆", {"chapter": "music", "i": 1, "n": 1}))
        self.save(st)
        r = pr.restructure("u1", AID, remove=["music"])
        self.assertEqual(r.get("queued"), "restructure")
        self.assertEqual(fw.chapter_order(pr.Analysis(AID))[-2:], ["music", "result"])   # まだ変えない
        self.assertEqual(pr._pending(pr.Analysis(AID))[0]["remove"], ["music"])


# ---------------------------------------------------------------------------
# 4 動画を外す・戻す
# ---------------------------------------------------------------------------
class TestExclude(Flow):
    def test_filters(self):
        pr.videos("u1", AID, "exclude", ["6"], "関係ない")
        a = pr.Analysis(AID)
        self.assertNotIn(6, fw.records(a))
        self.assertIn(6, fw.records(a, all_=True))
        self.assertNotIn(6, fw.labels(a))
        self.assertIn(6, fw.labels(a, all_=True))
        self.assertNotIn(6, fw.videos(a))
        self.assertNotIn(vid(6), fw.fetched(a))
        self.assertNotIn(6, fw.label_seqs(a))
        self.assertEqual(fw.excluded_seqs(a), {6})
        self.assertEqual(self.meta()["excluded_videos"][0]["why"], "関係ない")

    def test_exclude_cited_video(self):
        r = pr.videos("u1", AID, "exclude", [f"https://www.tiktok.com/@user6/video/{vid(6)}"], "これは関係ない")
        self.assertIn("外しました", r["text"])
        self.assertIn("拡大経路 P2", r["text"])
        t = self.next()
        self.assertIn("outline_revise", t["task_id"])
        self.assertIn("seq 6", t["text"])
        self.assertIn("★`path_P2`", t["text"])
        bad = pr.submit("u1", t["task_id"], json.dumps(outline_json(["intro"]), ensure_ascii=False))
        self.assertFalse(bad["ok"])
        self.assertIn("path_P2", bad["text"])
        self.submit(t, outline_json(["path_P2"]))
        t = self.next()
        self.assertIn("write", t["task_id"])
        cited = pr.submit("u1", t["task_id"], long_body("### (2) 広がり", "seq 6（@user6）が伸びた。"))
        self.assertFalse(cited["ok"])                                       # 外した動画を書くと差し戻す
        self.assertIn("存在しない seq", cited["text"])

    def test_exclude_unused_video_only_reassembles(self):
        """どこにも出てこず、コメントも読んでいない動画: AI の作業なしで、組み立てと検算だけ"""
        pr.videos("u1", AID, "exclude", ["4"])
        t = self.next()
        self.assertEqual(t["kind"], "done")
        self.assertIn("動画を外した", t["text"])
        self.assertIn("なし（付録の数字だけ組み立て直した）", t["text"])

    def test_include_back(self):
        pr.videos("u1", AID, "exclude", ["4"])
        self.next()
        r = pr.videos("u1", AID, "include", ["4"])
        self.assertIn("戻しました", r["text"])
        self.assertIn(4, fw.records(pr.Analysis(AID)))
        t = self.next()
        self.assertIn("outline_revise", t["task_id"])
        self.assertIn("戻した", t["text"])
        again = pr.videos("u1", AID, "include", ["4"])
        self.assertIn("外していません", again["text"])

    def test_exclude_while_acquiring_marks_only(self):
        self.set_meta(acquisition={"status": "running"})
        r = pr.videos("u1", AID, "exclude", ["3"])
        self.assertIn("書き直しは要りません", r["text"])
        self.assertEqual(fw.excluded_seqs(pr.Analysis(AID)), {3})

    def test_exclude_while_writing_queues_rewrite(self):
        st = self.st()
        st["tasks"].append(fw._task(st, "write", "ai", "執筆", {"chapter": "music", "i": 1, "n": 1}))
        self.save(st)
        r = pr.videos("u1", AID, "exclude", ["6"])
        self.assertEqual(r.get("queued"), "videos_change")
        self.assertEqual(fw.excluded_seqs(pr.Analysis(AID)), {6})            # 印はすぐ付く
        self.assertEqual(pr._pending(pr.Analysis(AID))[0]["video_ids"], [vid(6)])

    def test_unknown_target(self):
        with self.assertRaises(pr.RunnerError):
            pr.videos("u1", AID, "exclude", ["だれかの動画"])
        with self.assertRaises(pr.RunnerError):
            pr.videos("u1", AID, "hide", ["1"])


# ---------------------------------------------------------------------------
# 5・6 足す・動画ごと／全体の取り足し（取得の口は偽物）
# ---------------------------------------------------------------------------
class FakeDeepen:
    def __init__(self, d):
        self.d = d
        self.calls = []

    def _job(self, kind, community, instruction, targets, **extra):
        m = json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))
        hist = m.setdefault("deepen_history", [])
        if m.get("deepen"):
            hist.append(m["deepen"])
        job = {"round": len(hist) + 1, "kind": kind, "community": community, "instruction": instruction, "targets": targets,
               "status": "done", "result": {}, "est_min": 3, **extra}
        m["deepen"] = job
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return job

    def plan_videos(self, d, video_ids, per_video=None):
        self.calls.append(("plan_videos", list(video_ids), per_video))
        return {"community": "videos", "targets": [{"video_id": v, "kind": "more"} for v in video_ids], "n_new": 0,
                "n_more": len(video_ids), "est_min": 3.0, "left_new": 0, "left_more": 0, "skipped": {}, "pages": [1]}

    def plan_all(self, d, per_video=None):
        self.calls.append(("plan_all", per_video))
        return {"community": "all", "targets": [], "n_new": 0, "n_more": 0, "est_min": 0.0, "left_new": 0, "left_more": 0,
                "skipped": {}, "pages": []}

    def request(self, d, community, instruction, pl, kind="deepen", **extra):
        self.calls.append(("request", community, kind, extra))
        return self._job(kind, community, instruction, pl["targets"], **extra)

    def request_add(self, d, targets, instruction):
        self.calls.append(("request_add", targets))
        job = self._job("add", "add", instruction, targets)
        return job


class AcqBase(Flow):
    def setUp(self):
        super().setUp()
        from acquire import deepen as dp
        self.dp = dp
        self.fake = FakeDeepen(self.d)
        self.saved_dp = {k: getattr(dp, k, None) for k in ("plan_videos", "plan_all", "request", "request_add")}
        for k in self.saved_dp:
            setattr(dp, k, getattr(self.fake, k))

    def tearDown(self):
        for k, v in self.saved_dp.items():
            if v is None:
                delattr(self.dp, k)
            else:
                setattr(self.dp, k, v)
        super().tearDown()


class TestDeepenVideos(AcqBase):
    def test_videos_all_comments(self):
        r = pr.deepen("u1", AID, "", "この動画のコメントを全部読んで", videos=["0"], per_video="all")
        self.assertIn("動画1本のコメントを取り足して", r["text"])
        self.assertIn("その動画のコメントを全部", r["text"])
        self.assertEqual(self.fake.calls[0], ("plan_videos", [vid(0)], "all"))
        self.assertEqual(self.fake.calls[1][2], "videos")
        t = self.next()                                                     # 取り足しは済んだ（偽物）→ dancer のコメント分析のやり直し
        self.assertIn("dcomments", t["task_id"])
        self.assertIn("dancer", t["task_id"] + t["text"])

    def test_all_communities(self):
        pr.deepen("u1", AID, "all", "全体でコメントをもっと")
        self.assertEqual(self.fake.calls[0], ("plan_all", None))
        t = self.next()
        self.assertIn("dcomments", t["task_id"])
        st = self.st()
        comms = [x["params"]["community"] for x in st["tasks"] if x["type"] == "dcomments"]
        self.assertEqual(comms, ["dancer", "student"])
        self.assertTrue(any(x["type"] == "outline_revise" and x["params"].get("generic") for x in st["tasks"]))

    def test_unread_video_is_added_first(self):
        r = pr.deepen("u1", AID, "", "全部読んで", videos=["https://www.tiktok.com/@new/video/7999999999999999999"])
        self.assertIn("先に分析に足します", r["text"])
        self.assertEqual(self.fake.calls[0][0], "request_add")

    def test_bad_per_video(self):
        with self.assertRaises(pr.RunnerError):
            pr.deepen("u1", AID, "dancer", "x", per_video="たくさん")


class TestAddVideos(AcqBase):
    URL = "https://www.tiktok.com/@new/video/7999999999999999999"

    def test_before_list_stage(self):
        self.set_meta(acquisition={"status": "running", "steps": {"resolve": {"status": "done"}}})
        r = pr.videos("u1", AID, "add", [self.URL])
        self.assertIn("必ず詳しく読みます", r["text"])
        self.assertEqual(self.meta()["user_videos"][0]["video_id"], "7999999999999999999")
        self.assertEqual(self.fake.calls, [])

    def test_after_done_queues_job_and_labels(self):
        r = pr.videos("u1", AID, "add", [self.URL, "0"], "この動画も入れて")
        self.assertIn("属性とコメントを取ります", r["text"])
        self.assertIn("もう詳しく読んでいる動画: seq 0", r["text"])
        self.assertEqual(self.fake.calls[0], ("request_add", [{"video_id": "7999999999999999999", "url": self.URL}]))
        # 係が足し終えた（records に seq 8 が足され、result に added がある）ところを作る
        recs = self.d / "derived" / "llm_input" / "records.jsonl"
        row = json.loads(recs.read_text(encoding="utf-8").splitlines()[0])
        row.update({"seq": 8, "video_id": "7999999999999999999", "date": "2025-08-20", "plays": 1_500_000})
        for p in (recs, self.d / "derived" / "videos.jsonl"):
            p.write_text(p.read_text(encoding="utf-8") + json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        m = self.meta()
        m["deepen"]["result"] = {"added": [{"video_id": "7999999999999999999", "seq": 8, "enriched": True, "comments": 0}],
                                 "comments_added": 0}
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        t = self.next()
        self.assertIn("label", t["task_id"])
        self.assertIn("seq 8", t["text"])
        st = self.st()
        lab = next(x for x in st["tasks"] if x["type"] == "label")
        self.assertEqual(lab["params"]["seqs"], [8])
        self.assertGreaterEqual(lab["params"]["batch"], 1)
        self.assertIn(8, fw.label_seqs(pr.Analysis(AID)))                    # 足した動画は必ずラベルの対象

    def test_add_queued_while_writing(self):
        st = self.st()
        st["tasks"].append(fw._task(st, "write", "ai", "執筆", {"chapter": "music", "i": 1, "n": 1}))
        self.save(st)
        r = pr.videos("u1", AID, "add", [self.URL])
        self.assertEqual(r.get("queued"), "videos")
        self.assertEqual(self.fake.calls, [])


# ---------------------------------------------------------------------------
# 7 比べる
# ---------------------------------------------------------------------------
class TestCompare(Flow):
    OLD = "a20260901-0000-test"

    def make_old(self):
        old = self.d.parent / self.OLD
        shutil.copytree(self.d, old)
        m = json.loads((old / "analysis.json").read_text(encoding="utf-8"))
        m.update({"analysis_id": self.OLD, "created_at": "2026-09-01T00:00:00+09:00",
                  "acquisition": {"status": "done", "started_at": "2026-09-01T00:00:00+09:00"}})
        (old / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        recs = old / "derived" / "llm_input" / "records.jsonl"
        rows = [json.loads(x) for x in recs.read_text(encoding="utf-8").splitlines() if x.strip()]
        for r in rows:
            r["plays"] = r["plays"] // 2
        recs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        self.set_meta(acquisition={"status": "done", "started_at": "2026-10-06T00:00:00+09:00"})

    def test_no_partner(self):
        r = pr.compare("u1", ["テスト曲"])
        self.assertIn("比べる相手がありません", r["text"])
        self.assertIn("refresh", r["text"])

    def test_same_song_previous(self):
        self.make_old()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fw.REPORTS_DIR = tmp.name
        r = pr.compare("u1", ["テスト曲"], "前回からどれだけ伸びた？")
        self.assertIn("比べたレポートを書きます", r["text"])
        self.assertEqual(r["analysis_id"], AID)                               # 新しいほうの分析に足す
        t = self.next()
        self.assertIn("compare", t["task_id"])
        self.assertIn(self.OLD, t["text"])
        self.assertIn("再生が伸びた動画", t["text"])
        self.assertIn("150万 → 300万", t["text"])
        self.assertNotIn("{{", t["text"])
        bad = pr.submit("u1", t["task_id"], "# 比べ\n\nseq 0 が伸びた。" + "本文。" * 300)
        self.assertFalse(bad["ok"])
        self.submit(t, "# 『テスト曲』の前回と今回の比べ\n\n前回より再生が倍になった。" + "本文。" * 300)
        t = self.next()
        self.assertEqual(t["kind"], "done")
        self.assertIn("比較レポート.md", t["text"])
        found = list(Path(tmp.name).glob("比較/*/比較レポート.md"))
        self.assertEqual(len(found), 1)

    def test_compare_two_titles(self):
        self.make_old()
        r = pr.compare("u1", [AID, self.OLD])
        self.assertIn("比べたレポートを書きます", r["text"])
        self.assertEqual(fw.compare_record(pr.Analysis(AID), 1)["ids"], [AID, self.OLD])


# ---------------------------------------------------------------------------
# 8 消す・容量
# ---------------------------------------------------------------------------
class TestDelete(Flow):
    def setUp(self):
        super().setUp()
        self.trash = tempfile.TemporaryDirectory()
        os.environ["UGC_TRASH_DIR"] = self.trash.name

    def tearDown(self):
        os.environ.pop("UGC_TRASH_DIR", None)
        self.trash.cleanup()
        super().tearDown()

    def test_delete_to_trash(self):
        other = self.d.parent / "a20260901-0000-old"
        shutil.copytree(self.d, other)
        m = json.loads((other / "analysis.json").read_text(encoding="utf-8"))
        m.update({"analysis_id": other.name, "superseded_by": AID})
        (other / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        info = pr.info("u1", None, "storage")["text"]
        self.assertIn("分析の容量", info)
        self.assertIn(AID, info)
        self.assertIn("取り直し済み", info)
        r = pr.archive_analysis("u1", AID, delete=True)
        self.assertTrue(r["deleted"])
        self.assertIn("ゴミ箱に入れました", r["text"])
        self.assertIn("一覧に戻しました", r["text"])
        self.assertFalse(self.d.exists())
        self.assertEqual(len(list(Path(self.trash.name).iterdir())), 1)
        self.assertNotIn("superseded_by", json.loads((other / "analysis.json").read_text(encoding="utf-8")))
        log = (self.d.parent / ".deleted.jsonl").read_text(encoding="utf-8")
        self.assertIn(AID, log)

    def test_refuse_while_acquiring(self):
        self.set_meta(acquisition={"status": "running"})
        with self.assertRaises(pr.RunnerError):
            pr.archive_analysis("u1", AID, delete=True)
        self.assertTrue(self.d.exists())


# ---------------------------------------------------------------------------
# 道具の口
# ---------------------------------------------------------------------------
class TestTools(unittest.TestCase):
    def test_tools_and_rule(self):
        import asyncio
        import mcp_proto
        mcp = mcp_proto._build_server(local=True)
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in ("restructure", "videos", "compare"):
            self.assertIn(name, tools)
        schema = lambda t: json.dumps(getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or getattr(t, "parameters", None))  # noqa: E731
        self.assertIn("per_video", schema(tools["deepen"]))
        self.assertIn("delete", schema(tools["archive_analysis"]))
        self.assertIn("storage", tools["info"].description)
        for word in ("restructure", "videos", "compare", "archive_analysis の delete"):
            self.assertIn(word, mcp_proto.ASK_RULE)
        from collector.collector_app import codex_link
        self.assertEqual(codex_link.ASK_RULE, mcp_proto.ASK_RULE)
        self.assertEqual(sorted(codex_link.TOOLS), sorted(tools))


if __name__ == "__main__":
    unittest.main()
