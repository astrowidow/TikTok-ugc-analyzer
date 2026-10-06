"""通し試験（2026-10-06）の領域 R5: 完成後の界隈の掘り下げ（deepen）と切り直し（recut）の、コードを読んで見つけた疑いの試験。

正しい動きを期待する形で書いてある（不具合なら失敗する）。TikTok・Chrome・AI・本物の置き場には触らない
（分析は一時フォルダ、spatest の取得の係は作り物に差し替え、ロックは取らない）。作り物の分析は tests/test_deepen.py・test_recut.py のものを使う。

  collector/.venv/bin/python -m unittest tests.test_review_r5 -v
"""
import contextlib
import copy
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1  # noqa: E402
import proto_runner as pr  # noqa: E402
from acquire import deepen, pipeline, recut, spatest  # noqa: E402
from tests import test_deepen as td  # noqa: E402
from tests import test_recut as tr  # noqa: E402

AID = td.AID


class Base(unittest.TestCase):
    """一時フォルダに作り物の分析を置き、置き場・アプリ・知識ベースを差し替える"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR, os.environ.get("UGC_KB_DIR"))
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = Path(self.tmp.name)
        pr.LOCAL = td.FakeLocal()
        flow_w1.REPORTS_DIR = None
        os.environ["UGC_KB_DIR"] = str(Path(self.tmp.name) / "_kb")   # 知識ベースの仕事をはさまない
        self.d = self.make()

    def make(self) -> Path:
        return td.make_analysis(Path(self.tmp.name))

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR, kb = self.saved
        if kb is None:
            os.environ.pop("UGC_KB_DIR", None)
        else:
            os.environ["UGC_KB_DIR"] = kb
        self.tmp.cleanup()

    def meta(self) -> dict:
        return json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))

    def put_meta(self, m: dict) -> None:
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def set_deepen(self, **kv) -> None:
        m = self.meta()
        m["deepen"].update(kv)
        self.put_meta(m)

    def raw_rows(self) -> list:
        return [json.loads(ln) for ln in (self.d / "raw" / "comments.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]

    def set_sources(self, src: dict) -> None:
        """動画を見つけた楽曲ページ（seq → 1〜）"""
        td.write_jsonl(self.d / "raw" / "grid_links.jsonl",
                       [{"video_id": td.vid(s), "source": src.get(s, 1)} for s in td.VIDEOS])

    def summary(self, n: int, missing: list, page: int = 1) -> None:
        (self.d / "fetch_log").mkdir(exist_ok=True)
        (self.d / "fetch_log" / f"deepen_r{n}_summary_p{page}.json").write_text(
            json.dumps({"missing": missing, "not_fetched_time": []}), encoding="utf-8")


# ---------------------------------------------------------------------------
# 取る動画の決め方（acquire/deepen.py の candidates・plan）
# ---------------------------------------------------------------------------
class TestDeepenCandidates(Base):
    def test_new_skips_video_unreachable_in_previous_round(self):
        """docs/DEEPEN_COMMUNITY.md 4章「前の掘り下げで取れなかった動画は外す」。グリッドで見つからなかった動画は
        r<回>.jsonl に行が無く（要約の missing にだけ載る）、次の掘り下げでまた「新しく取る」に入る（グリッドを上限まで探して5分前後むだにする）"""
        job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        n = job["round"]
        td.write_jsonl(self.d / "raw" / "deepen" / f"r{n}.jsonl", [td.row(3, 20)])   # 3 は取れた
        self.summary(n, [td.vid(5)])                                                  # 5 はグリッドに無かった
        res = deepen.merge(self.d, n, "dancer")
        self.assertEqual(res["unreachable"], 1)
        self.assertNotIn(5, [x["seq"] for x in deepen.candidates(self.d, "dancer")["new"]])

    def test_more_skips_video_unreachable_in_previous_round(self):
        """「続きを取る」も同じ: 前の掘り下げでグリッドに無かった動画を、次の回にまた上限120件で取りに行く（1本3分の枠を使う）"""
        job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        n = job["round"]
        td.write_jsonl(self.d / "raw" / "deepen" / f"r{n}.jsonl", [td.row(5, 20)])
        self.summary(n, [td.vid(0)])                                                  # 続きの 0 が見つからなかった
        deepen.merge(self.d, n, "dancer")
        self.assertNotIn(0, [x["seq"] for x in deepen.candidates(self.d, "dancer")["more"]])

    def test_more_skips_video_gone_in_previous_round(self):
        """「続きを取る」には「動画が無い・コメントが無い・開けない」の判定が掛かっていない（新しく取る動画には掛かっている）。
        前の回に削除済み（no_video）と分かった動画を、次の回にまた続きの候補にする"""
        job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        n = job["round"]
        td.write_jsonl(self.d / "raw" / "deepen" / f"r{n}.jsonl", [{"video_id": td.vid(0), "status": "no_video"}])
        deepen.merge(self.d, n, "dancer")
        self.assertNotIn(0, [x["seq"] for x in deepen.candidates(self.d, "dancer")["more"]])

    def test_one_page_rows_are_more_candidates(self):
        """docs/COMMENT_TARGETS.md 6章: 第1版で1ページ（上限20件）だけ取った動画は「続きを取る」候補になる"""
        rows = self.raw_rows()
        rows = [r for r in rows if r["video_id"] != td.vid(1)] + [td.row(1, 18, cap=20, has_more=1, top_list=18)]
        td.write_jsonl(self.d / "raw" / "comments.jsonl", rows)
        more = deepen.candidates(self.d, "dancer")["more"]
        self.assertEqual([x["seq"] for x in more], [0, 1])
        self.assertEqual({x["cap"] for x in more}, {deepen.EST["more_cap"]})

    def test_plan_charges_page_once_per_page(self):
        """25分の枠への配り方: 楽曲ページ1つにつき準備 page_min を1回だけ。新しく取る動画を先に、残りで続き"""
        self.set_sources({5: 2})
        E = deepen.EST
        pl = deepen.plan(self.d, "dancer")
        self.assertEqual(([x["seq"] for x in pl["targets"]], pl["pages"]), ([5, 3, 0], [1, 2]))
        self.assertAlmostEqual(pl["est_min"], round(2 * E["page_min"] + 2 * E["new_min"] + E["more_min"], 1))
        small = deepen.plan(self.d, "dancer", minutes=2 * E["page_min"] + 2 * E["new_min"])   # 新しく2本（2ページ）でちょうど
        self.assertEqual(([x["seq"] for x in small["targets"]], small["left_more"]), ([5, 3], 1))
        one = deepen.plan(self.d, "dancer", minutes=E["page_min"] + E["new_min"] + 0.5)
        self.assertEqual(([x["seq"] for x in one["targets"]], one["pages"]), ([5], [2]))


# ---------------------------------------------------------------------------
# 原本に合わせる（merge・prep）
# ---------------------------------------------------------------------------
class TestMerge(Base):
    def test_merge_twice_adds_nothing(self):
        """係が「原本に足した」直後・「済んだ」と書く前に止まり、もう一度回っても、同じ行を二重に足さない"""
        job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        n = job["round"]
        td.write_jsonl(self.d / "raw" / "deepen" / f"r{n}.jsonl", [td.row(5, 20), td.row(0, 60, start=20, cap=120)])
        first = deepen.merge(self.d, n, "dancer")
        lines = (self.d / "raw" / "comments.jsonl").read_text(encoding="utf-8").count("\n")
        again = deepen.merge(self.d, n, "dancer")
        self.assertEqual((first["videos_new"], first["videos_more"]), (1, 1))
        self.assertEqual((again["videos_new"], again["videos_more"], again["comments_added"]), (0, 0, 0))
        self.assertEqual((self.d / "raw" / "comments.jsonl").read_text(encoding="utf-8").count("\n"), lines)

    def test_prep_marks_weekly_video_fetched_by_deepen(self):
        """docs/DEEPEN_COMMUNITY.md 5章「プールの理由は掘り下げの印を足した写しを渡す」。1ページの取り方（0.5.4〜）では
        週ごとの動画も pool.tsv に cap 0 で載るので、prep() は印を足さない（新しく取る動画のほとんどがこれ）。
        derived/comments の見出しは「プールに入れた理由: week:…」のまま"""
        with open(self.d / "derived" / "pool.tsv", "w", encoding="utf-8", newline="\n") as f:
            f.write("video_id\tseq\tcap\tpriority\treasons\n")
            f.write(f"{td.vid(0)}\t0\t20\t1\t起点\n{td.vid(5)}\t5\t0\t2\tweek:2025-W31\n")
        job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        td.write_jsonl(self.d / "raw" / "deepen" / f"r{job['round']}.jsonl", [td.row(5, 20)])
        deepen.merge(self.d, job["round"], "dancer")
        text = next((self.d / "derived" / "comments").glob(f"5_{td.vid(5)}.md")).read_text(encoding="utf-8")
        self.assertIn("界隈の掘り下げで取り足した", text)


# ---------------------------------------------------------------------------
# 取る（deepen.Run）。spatest の取得の係を作り物に差し替える
# ---------------------------------------------------------------------------
class FakeCollector:
    """spatest.SpaCollector の代わり。プールの動画を「取れた」ことにして --out に書く（上限40件→40件、120件→60件）"""
    made = []
    how = {}          # 楽曲ページの URL → "ok" / "blocked" / "raise"

    def __init__(self, a):
        self.a, self.rows, self.d, self.between_videos = a, [], None, None
        FakeCollector.made.append(self)

    def log(self, msg):
        pass

    def run(self):
        how = FakeCollector.how.get(self.a.music_url, "ok")
        with open(self.a.pool, encoding="utf-8") as f:
            pool = list(csv.DictReader(f, delimiter="\t"))
        if how == "blocked":
            self.rows.append({"video_id": pool[0]["video_id"], "status": "blocked"})
            return
        with open(self.a.out, "a", encoding="utf-8") as f:
            for p in pool[:1] if how == "raise" else pool:
                cap = int(p["cap"])
                r = td.row(int(p["video_id"][-5:]), min(cap, 60), cap=cap)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.rows.append({k: v for k, v in r.items() if k != "comments"})
        Path(self.a.summary).write_text(json.dumps({"missing": [], "not_fetched_time": []}), encoding="utf-8")
        if how == "raise":
            raise RuntimeError("途中で落ちた")


class TestRun(Base):
    URLS = ["https://www.tiktok.com/music/a-7000000000000000001", "https://www.tiktok.com/music/b-7000000000000000002"]

    def setUp(self):
        super().setUp()
        self.patched = (pipeline.ensure_chrome, spatest.SpaCollector)
        pipeline.ensure_chrome = lambda port, log: None
        spatest.SpaCollector = FakeCollector
        FakeCollector.made, FakeCollector.how = [], {}
        m = self.meta()
        m.update({"music_url": self.URLS[0], "music_urls": self.URLS})
        self.put_meta(m)
        self.set_sources({5: 2, 3: 3})          # 5 は2ページ目、3 は分からない3ページ目、0（続き）は1ページ目
        self.job = deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))

    def tearDown(self):
        pipeline.ensure_chrome, spatest.SpaCollector = self.patched
        super().tearDown()

    def run_job(self) -> str:
        r = deepen.Run(AID)
        r.base.lock = lambda what: None          # 本物の TikTok のロックは取らない
        r.base.yield_lock = lambda: 0.0
        with contextlib.redirect_stdout(io.StringIO()):   # 係のログ（標準出力）は出さない
            return r.run()

    def test_pages_deadline_and_no_substitution(self):
        self.assertEqual(self.run_job(), "done")
        self.assertEqual([c.a.music_url for c in FakeCollector.made], self.URLS)      # 3ページ目は取らない
        limit = (self.job["est_min"] + deepen.EST["deadline_extra_min"]) / 60
        for c in FakeCollector.made:
            self.assertIsNone(c.a.candidates)                                         # 差し替えをしない（界隈の外の動画になる）
            self.assertTrue(limit - 0.01 <= c.a.deadline_hours <= limit + 0.001, c.a.deadline_hours)
        res = self.meta()["deepen"]["result"]
        self.assertEqual((self.meta()["deepen"]["status"], res["videos_new"], res["videos_more"], res["comments_added"]),
                         ("done", 1, 1, 40 + 20))
        ok = deepen._ok_rows(self.d / "raw" / "comments.jsonl")
        self.assertNotIn(td.vid(3), ok)

    def test_blocked_stops_before_next_page(self):
        FakeCollector.how = {self.URLS[0]: "blocked"}
        self.assertEqual(self.run_job(), "done")
        self.assertEqual(len(FakeCollector.made), 1)
        self.assertTrue(self.meta()["deepen"]["result"]["blocked"])

    def test_exception_still_merges_what_was_fetched(self):
        FakeCollector.how = {self.URLS[0]: "raise"}
        self.assertEqual(self.run_job(), "done")
        res = self.meta()["deepen"]["result"]
        self.assertIn("RuntimeError", res["error"])
        self.assertEqual(res["videos_more"], 1)                                      # 落ちる前に取れた続きは足した
        self.assertEqual(len(deepen._ok_rows(self.d / "raw" / "comments.jsonl")[td.vid(0)]["comments"]), 60)


# ---------------------------------------------------------------------------
# 回の数え方・待ち・排他（proto_runner の deepen・recut・next_task・status）
# ---------------------------------------------------------------------------
class TestRoundsAndWaits(Base):
    def test_rounds_continue_across_deepen_and_recut(self):
        """掘り下げ → 切り直しの取り足し → 掘り下げ、と続けても、回の番号は重ならず、前の回を読める"""
        deepen.request(self.d, "dancer", "浅い", deepen.plan(self.d, "dancer"))
        self.set_deepen(status="done", result={"videos_new": 2})
        j2 = recut.request(self.d, "分けて", recut.plan(self.d, ["student"]), 1)
        self.set_deepen(status="done", result={"videos_new": 1})
        j3 = deepen.request(self.d, "student", "浅い", deepen.plan(self.d, "student"))
        self.assertEqual((j2["round"], j3["round"]), (2, 3))
        a = pr.Analysis(AID)
        self.assertEqual((flow_w1.deepen_job(a, 1)["community"], flow_w1.deepen_job(a, 2)["kind"]), ("dancer", "recut"))
        self.assertEqual(flow_w1.deepen_job(a, 2)["result"], {"videos_new": 1})

    def test_progress_while_running(self):
        """取り足しの途中で「続けて」→ 何本中何本かを返して止まる"""
        pr.deepen("u1", AID, "dancer", "浅い")
        self.set_deepen(status="running", pid=os.getpid(), started_at=pipeline.now())
        td.write_jsonl(self.d / "raw" / "deepen" / "r1.jsonl", [td.row(5, 20)])
        w = pr.next_task("u1", AID)
        self.assertEqual(w["kind"], "wait")
        self.assertIn("（1/3本）", w["text"])

    def test_broken_line_in_round_file_does_not_break_next_task(self):
        """アプリが取得の途中で落ちて r<回>.jsonl の最後の行が途中で切れると、係が続きから取り直すあいだ、
        next_task と status（利用者の分析の全部）が JSONDecodeError で落ちる（_deepen_view が1行ずつ json.loads する）"""
        pr.deepen("u1", AID, "dancer", "浅い")
        self.set_deepen(status="running", pid=os.getpid(), started_at=pipeline.now())
        p = self.d / "raw" / "deepen" / "r1.jsonl"
        td.write_jsonl(p, [td.row(5, 20)])
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"video_id": "' + td.vid(3) + '", "status": "ok", "comments": [{"cid": "75')
        for name, call in (("next_task", lambda: pr.next_task("u1", AID)), ("status", lambda: pr.status("u1"))):
            with self.subTest(name):
                try:
                    r = call()
                except Exception as e:   # noqa: BLE001
                    self.fail(f"{name} が {type(e).__name__} で落ちた: {e}")
                if name == "next_task":
                    self.assertEqual(r["kind"], "wait")

    def test_recut_refused_while_deepen_tasks_pending(self):
        """取り足しが無い掘り下げ（すぐ AI の仕事）の途中に切り直しを頼まれても受けない"""
        rows = self.raw_rows()
        td.write_jsonl(self.d / "raw" / "comments.jsonl", rows + [td.row(7, 30, has_more=0), td.row(6, 100, cap=120, has_more=0)])
        deepen.prep(self.d)
        pr.deepen("u1", AID, "student", "浅い")
        with self.assertRaises(pr.RunnerError) as e:
            pr.recut("u1", AID, "分けて")
        self.assertIn("掘り下げの途中", str(e.exception))

    def test_match_community_does_not_pick_contrast_mention(self):
        """呼び名での当て方は、定義の全文に呼び名が含まれる界隈が1つなら、それを選ぶ。切り直しの指示書は「分けた界隈どうしの境目を
        はっきり書く」ので、ほかの界隈の名前が対比で定義に入る。
        本物の分類軸（切り直し後のきゃわを含む6つ）では、どの定義も自分の呼び名で始まるので、対比で名前が出ても候補が2つになり、
        一覧が返る（AI が key を選び直す＝安全側）。2026-10-06 確かめ役の判定で直さないことにした。ここでは本物の形でそれを確かめる
        （はじめは「定義が自分の呼び名で始まらない」作りの試験だった）"""
        tax = {"community": {"underground_idol": "地下アイドル。ライブハウスやコンカフェで活動する人。チェキ・物販がサムネに映る",
                             "major_idol": "メジャーアイドル。事務所に所属するアイドル。地下アイドルとは違い、テレビや大きな会場に出る",
                             "unknown": "判断できない"}}
        with self.assertRaises(pr.RunnerError) as e:
            pr._match_community(tax, "地下アイドル界隈")
        self.assertIn("複数", str(e.exception))
        self.assertIn("`underground_idol`", str(e.exception))
        self.assertEqual(pr._match_community(tax, "underground_idol"), "underground_idol")


# ---------------------------------------------------------------------------
# 切り直し（flow_w1 の accept_recut・service_recut_check・完了の知らせ）と、続けて掘り下げ
# ---------------------------------------------------------------------------
class TestRecutR5(Base):
    def make(self) -> Path:
        return tr.setup_analysis(Path(self.tmp.name))

    def recut_split(self, tax: dict, keep: dict) -> None:
        """道具 recut → 分類軸の直し → ラベルの付け直し（dancer だった 0〜5 を屋外・室内に）"""
        pr.recut("u1", AID, "ダンスの界隈を、屋外で踊る人と室内で踊る人に分けて")
        t = pr.next_task("u1", AID)
        r = pr.submit("u1", t["task_id"], json.dumps({"taxonomy": tax, "keep": keep, "note_to_user": "屋外と室内に分けた"},
                                                      ensure_ascii=False))
        self.assertTrue(r["ok"], r.get("text"))
        t = pr.next_task("u1", AID)
        self.assertIn("ラベルの付け直し", t["text"])
        r = pr.submit("u1", t["task_id"], tr.labels_tsv(range(6), tr.NEW_COMM))
        self.assertTrue(r["ok"], r.get("text"))

    def test_renamed_community_is_not_rechecked(self):
        """key を付け替えただけの界隈（keep で student → pupil）は顔ぶれが変わっていないので確かめない
        （pupil の必ず読みたい動画 7 にコメントが無くても取り足さない。RULE["scope"]="changed"）"""
        tax = copy.deepcopy(tr.SPLIT)
        tax["community"]["pupil"] = tax["community"].pop("student")
        self.recut_split(tax, {"student": "pupil"})
        w = pr.next_task("u1", AID)
        self.assertEqual(w["kind"], "wait", w["text"][:300])
        rec = flow_w1.recut_record(pr.Analysis(AID), 1)
        self.assertEqual(rec["changed"], ["dancer_room", "dancer_street"])
        self.assertEqual(sorted(x["seq"] for x in self.meta()["deepen"]["targets"]), [3, 5])

    def test_requests_during_recut_remainder_say_continue(self):
        """切り直しの取り足しが済み、利用者がまだ「続けて」と言っていない（段階から先が残っている）ときに掘り下げ・切り直しを頼むと、
        「レポートがまだできていません。完成してから頼んでください」が返る（_recut_pending は recut・recut_check・付け直しだけを見る）。
        利用者は「続けて」と言えば進むことが分からない"""
        self.recut_split(tr.SPLIT, {"student": "student"})
        w = pr.next_task("u1", AID)
        self.assertEqual(w["kind"], "wait")
        self.set_deepen(status="done", result={"videos_new": 2, "comments_added": 35})
        for name, call in (("deepen", lambda: pr.deepen("u1", AID, "student", "浅い")),
                           ("recut", lambda: pr.recut("u1", AID, "もう一度"))):
            with self.subTest(name):
                with self.assertRaises(pr.RunnerError) as e:
                    call()
                self.assertRegex(str(e.exception), "切り直し|続けて")

    def test_fetched_note_when_must_read_video_is_unreachable(self):
        """必ず読みたい動画が「動画が無い・グリッドに無い」で取りに行けず、取り足しが0本のとき、
        完了の知らせが「要らなかった（読むべき動画にはコメントがあった）」と言う（test_recut の dancer_room の seq 2 がこの形）"""
        a = pr.Analysis(AID)
        pr._write_json(flow_w1.recut_record_path(a, 1), {
            "round": 1, "instruction": "分けて", "note_to_user": "分けた", "changed": ["dancer_room"],
            "check": {"dancer_room": {"labeled": 3, "with_comments": 0, "have": 0, "need": [], "unreachable": [2]}},
            "n_targets": 0, "est_min": 0.0})
        self.assertNotIn("読むべき動画にはコメントがあった", flow_w1.recut_fetched_note(a, 1))


# ---------------------------------------------------------------------------
# 前の版を残す（掘り下げの書き直し）
# ---------------------------------------------------------------------------
class TestDeepenHistory(Base):
    def test_previous_note_version_is_kept(self):
        """掘り下げの完了の知らせで AI は「前の版は残してある」と伝える（deepen_summary）。構成案・章の下書き・界隈の分析は history に残るが、
        note 用に仕上げた章（note_chapters/）は仕上げ直しで上書きされ、前の版がどこにも残らない（NOTE_BODY.md・REPORT.md も同じ）"""
        note = self.d / "outputs" / "note_chapters" / "path_P2.md"
        old = note.read_text(encoding="utf-8")
        rows = self.raw_rows()
        td.write_jsonl(self.d / "raw" / "comments.jsonl", rows + [td.row(7, 30, has_more=0), td.row(6, 100, cap=120, has_more=0)])
        deepen.prep(self.d)
        pr.deepen("u1", AID, "student", "浅い")                      # 取り足しなし → すぐ AI の仕事

        def do(kind_in_id, out):
            t = pr.next_task("u1", AID)
            self.assertIn(kind_in_id, t["task_id"], t["text"][:300])
            r = pr.submit("u1", t["task_id"], out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
            self.assertTrue(r["ok"], r.get("text"))

        do("dcomments", "### student\n#### コメントの全体傾向\n根拠薄\n#### この界隈が曲を採用した文脈\nx\n#### 動画が伸びた理由（見た人の受け取り方）\nx\n"
                        "#### 段階による変化\nx\n#### 具体例で確かめた引用\nseq 6 根拠薄\n#### ラベルとのずれ\nなし\n#### 前回からの変化\n- 大きな変化なし\n")
        do("outline_revise", {"thesis": "全体の主張", "thesis_changed": False,
                              "chapters": [{"id": "path_P2", "role": "r", "claims": [{"claim": "c", "evidence": [{"seq": 6}]}], "bridge": "b"}],
                              "rewrite": [{"chapter": "path_P2", "why": "層を足す"}], "note_to_user": "広がりの章に層を足した"})
        do("write", td.chapter_md("path_P2", (6,)))
        do("review", {"fixes": [], "note_to_user": "ずれなし"})
        do("finish", "## 見出し\n\n" + "新しい仕上げの本文。" * 40 + "\n")
        self.assertEqual(pr.next_task("u1", AID)["kind"], "done")
        self.assertNotEqual(note.read_text(encoding="utf-8"), old)
        kept = [p for p in (self.d / "outputs").rglob("*.md") if p != note and p.read_text(encoding="utf-8") == old]
        self.assertTrue(kept, "掘り下げの前の note 用の章（path_P2）がどこにも残っていない")


# ---------------------------------------------------------------------------
# 切り直しの「足りるか」の基準（acquire/recut.py の picks）
# ---------------------------------------------------------------------------
class TestRecutPicks(unittest.TestCase):
    def test_earliest_is_first_above_floor_and_verified(self):
        """最初期は「下限を超えたものの中で」いちばん早い投稿。認証は再生最上位と最初期（下限以上）"""
        def r(s, date, plays, ver=False):
            return {"seq": s, "video_id": str(s), "date": date, "plays": plays, "enriched": True, "comments": 10,
                    "author": {"verified": ver}}
        recs = [r(0, "2025-07-01", 50_000, True), r(1, "2025-07-02", 200_000), r(2, "2025-07-03", 5_000_000, True),
                r(3, "2025-07-04", 300_000), r(4, "2025-07-05", 400_000), r(5, "2025-07-06", 900_000)]
        self.assertEqual(recut.picks(recs, 100_000),
                         {2: ["top", "verified_top", "verified_early"], 5: ["top"], 4: ["top"], 1: ["earliest"]})


if __name__ == "__main__":
    unittest.main()
