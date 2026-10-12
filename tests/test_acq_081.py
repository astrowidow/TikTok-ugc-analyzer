"""0.8.1 の取得側（docs/PLAN_0_8_1.md 2-3・2-4）の試験。TikTok・Chrome・AI・本物の置き場には触らない
（分析は一時フォルダ、属性の取得（enrich）と spatest の取得の係は作り物に差し替え、ロックは取らない）。

- 動画ごとの取り足し（deepen.plan_videos）: 新しく・続き・件数・"all"・外した動画・取らない理由・前に見つからなかった動画・台帳に無い動画の音源のページ
- 全体の取り足し（deepen.plan_all）: 界隈ごとの候補を合わせる・unknown を入れない・per_video
- 回の種類（deepen.request の kind）: videos・exclude・include・取り足しの途中に積まない
- 取り足しの係（deepen.Run）: ページごとに spatest を回す・--cap は行の上限の最大・120件を超える上限の歯止め・音源のページ
- 動画を足す（deepen.request_add と係の add）: 一覧に足す → 属性 → 通し番号を変えずに作り直す → コメント → 原本に足す
- 通し番号を変えずに作り直す（prep_sample.py の --keep-seqs・pipeline.Run.derive_keep_seqs）
- 一覧の段（pipeline.Run.step_list）で利用者が足した動画（user_videos）を必ず読む
- 台帳に無い動画（source 0）があっても、コメントの段のページ分け（pipeline.Run.comment_pages）が壊れない
- メニューの文（collector_app.jobs.describe_deepen）

  collector/.venv/bin/python -m unittest tests.test_acq_081
"""
import contextlib
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

from acquire import deepen, pipeline, recut, spatest, worker  # noqa: E402
from tests import test_deepen as td  # noqa: E402
from tests.test_ledger import MID1, MID2, U1, U2, item, vid_at  # noqa: E402

E = deepen.EST
UA = "https://www.tiktok.com/music/a-7000000000000000001"
UB = "https://www.tiktok.com/music/b-7000000000000000002"
MIDX = "7000000000000000099"   # どの取るページでもない音源


def jsonl(p: Path) -> list:
    return [json.loads(ln) for ln in Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


def put(p: Path, rows) -> None:
    td.write_jsonl(Path(p), rows)


def comment_rows_for(vid: str, n: int, cap: int) -> dict:
    """spatest の行（ok）。cid は動画ごとに重ならない"""
    cs = [{"cid": f"9{vid[-7:]}{i:06d}", "text": f"コメント{i}", "digg_count": 100 - i, "aweme_id": vid,
           "comment_language": "ja", "user": {"nickname": "u"}, "sort_tags": json.dumps({"top_list": 1})} for i in range(n)]
    return {"video_id": vid, "status": "ok", "comments": cs, "cap": cap, "has_more": 1, "top_list": n, "total": 500}


class FakeCollector:
    """spatest.SpaCollector の代わり。プールの動画（cap 1以上）を、上限と50件の少ないほうまで「取れた」ことにして --out に書く"""
    made = []

    def __init__(self, a):
        self.a, self.rows, self.d, self.between_videos = a, [], None, None
        FakeCollector.made.append(self)

    def log(self, msg):
        pass

    def run(self):
        with open(self.a.pool, encoding="utf-8") as f:
            pool = [r for r in csv.DictReader(f, delimiter="\t") if int(r["cap"] or 0) > 0]
        with open(self.a.out, "a", encoding="utf-8") as f:
            for p in pool:
                r = comment_rows_for(p["video_id"], min(int(p["cap"]), 50), int(p["cap"]))
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.rows.append({"video_id": r["video_id"], "status": "ok"})
        Path(self.a.summary).write_text(json.dumps({"missing": [], "not_fetched_time": []}), encoding="utf-8")

    def pool_rows(self) -> list:
        with open(self.a.pool, encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))


class Base(unittest.TestCase):
    """一時フォルダに掘り下げの試験の作り物の分析（tests/test_deepen.py の make_analysis）を置く"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = Path(self.tmp.name)
        self.d = td.make_analysis(Path(self.tmp.name))

    def tearDown(self):
        pipeline.ANALYSES_DIR = self.saved
        self.tmp.cleanup()

    def meta(self) -> dict:
        return json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))

    def put_meta(self, **kv) -> None:
        m = self.meta()
        m.update(kv)
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def set_sources(self, src: dict) -> None:
        """動画を見つけた楽曲ページ（seq → 0〜。0 は台帳に無い動画）"""
        put(self.d / "raw" / "grid_links.jsonl", [{"video_id": td.vid(s), "source": src.get(s, 1)} for s in td.VIDEOS])

    def set_music(self, music: dict) -> None:
        """属性の音源（seq → (音源の id, 題)）"""
        put(self.d / "raw" / "enriched.jsonl", [{"video_id": td.vid(s), "music": {"id": m, "title": t}} for s, (m, t) in music.items()])

    def replace_comment_row(self, s: int, **kw) -> None:
        rows = [r for r in jsonl(self.d / "raw" / "comments.jsonl") if r["video_id"] != td.vid(s)]
        put(self.d / "raw" / "comments.jsonl", rows + [td.row(s, kw.pop("n", 40), **kw)])


# ---------------------------------------------------------------------------
# 動画ごとの取り足し
# ---------------------------------------------------------------------------
class TestPlanVideos(Base):
    def test_new_more_and_skips(self):
        """名指しの動画: コメントが無ければ新しく、あれば続き。再生の下限（4 は5万）では外さない。取らない動画は理由つき"""
        url7 = f"https://www.tiktok.com/@u/video/{td.vid(7)}?is_from_webapp=1"
        pl = deepen.plan_videos(self.d, [td.vid(5), td.vid(0), td.vid(4), td.vid(1), td.vid(2), url7, 6, "seq 3", "999", td.vid(5)])
        self.assertEqual(pl["community"], "videos")
        self.assertEqual([(x["seq"], x["kind"], x["cap"]) for x in pl["targets"]],
                         [(5, "new", 40), (7, "new", 40), (3, "new", 40), (4, "new", 40), (0, "more", 120), (6, "more", 120)])
        self.assertEqual((pl["n_new"], pl["n_more"], pl["left_new"], pl["left_more"]), (4, 2, 0, 0))
        self.assertEqual(pl["skipped"], {"all_fetched": 1, "gone": 1, "not_in_analysis": 1})
        why = {x["seq"] if x["seq"] is not None else x["given"]: x["reason"] for x in pl["skipped_videos"]}
        self.assertEqual(why, {1: "all_fetched", 2: "gone", "999": "not_in_analysis"})
        self.assertEqual({x["seq"]: x.get("community") for x in pl["targets"]}, {5: "dancer", 7: "student", 3: "dancer", 4: "dancer",
                                                                               0: "dancer", 6: "student"})
        self.assertEqual(pl["targets"][4]["had"], 40)
        self.assertEqual(pl["pages"], [1])
        self.assertAlmostEqual(pl["est_min"], round(E["page_min"] + 4 * E["new_min"] + 2 * E["more_min"], 1))

    def test_per_video_number_and_all(self):
        """件数ならその件数、"all" ならその動画のコメント数（records の comments。作り物は300）まで。見込みは件数に比例"""
        pl = deepen.plan_videos(self.d, [td.vid(5), td.vid(0)], per_video=200)
        self.assertEqual([(x["seq"], x["cap"]) for x in pl["targets"]], [(5, 200), (0, 200)])
        self.assertAlmostEqual(pl["est_min"], round(E["page_min"] + 2 * E["new_min"] * 200 / 40, 1))
        pl = deepen.plan_videos(self.d, [td.vid(5), td.vid(0)], per_video="all")
        self.assertEqual([x["cap"] for x in pl["targets"]], [300, 300])
        pl = deepen.plan_videos(self.d, [td.vid(0)], per_video=30)        # もう40件ある
        self.assertEqual((pl["targets"], pl["skipped"]), ([], {"enough": 1}))
        pl = deepen.plan_videos(self.d, [td.vid(5)], per_video="20")      # 数の文字列も受ける
        self.assertEqual(pl["targets"][0]["cap"], 20)
        with self.assertRaises(ValueError):
            deepen.plan_videos(self.d, [td.vid(5)], per_video=0)

    def test_top_list_done_and_no_gain(self):
        """上位リストを取り切った動画・前の取り足しで同じ上限まで取り直して増えなかった動画は取らない（上限が上がれば取る）"""
        self.replace_comment_row(6, top_list=10)
        put(self.d / "raw" / "deepen" / "r1.jsonl", [td.row(0, 30, cap=120)])
        pl = deepen.plan_videos(self.d, [td.vid(6), td.vid(0)])
        self.assertEqual(pl["skipped"], {"top_list_done": 1, "no_gain": 1})
        pl = deepen.plan_videos(self.d, [td.vid(0)], per_video="all")
        self.assertEqual([(x["seq"], x["cap"]) for x in pl["targets"]], [(0, 300)])

    def test_excluded_videos_are_skipped_everywhere(self):
        """外した動画（analysis.json の excluded_videos）は、名指し・界隈・全体・切り直しのどの候補にも出さない"""
        self.put_meta(excluded_videos=[{"video_id": td.vid(5), "url": "u", "seq": 5, "at": "x", "why": "関係ない"}])
        pl = deepen.plan_videos(self.d, [td.vid(5), 5])
        self.assertEqual((pl["targets"], pl["skipped"]), ([], {"excluded": 1}))
        c = deepen.candidates(self.d, "dancer")
        self.assertEqual([x["seq"] for x in c["new"]], [3])
        self.assertEqual(c["skipped"], {"below_min_plays": 1, "gone": 1, "excluded": 1})
        self.assertNotIn(5, [x["seq"] for x in deepen.plan(self.d, "dancer")["targets"]])
        self.assertNotIn(5, [x["seq"] for x in deepen.plan_all(self.d)["targets"]])
        self.assertNotIn(td.vid(5), [x["video_id"] for x in recut.plan(self.d)["targets"]])
        self.assertIn(td.vid(5), deepen._records(self.d, keep_excluded=True))

    def test_unreachable_is_retried_with_search_time(self):
        """前に一覧で見つからなかった動画も、名指しなら探し直す（見込みにそのページの search_min を足す）。界隈の候補からは今までどおり外す"""
        (self.d / "fetch_log").mkdir(exist_ok=True)
        (self.d / "fetch_log" / "comments_summary.json").write_text(json.dumps({"missing": [td.vid(3)]}), encoding="utf-8")
        pl = deepen.plan_videos(self.d, [td.vid(3)])
        self.assertEqual([(x["seq"], x.get("retry")) for x in pl["targets"]], [(3, True)])
        self.assertAlmostEqual(pl["est_min"], round(E["page_min"] + E["search_min"] + E["new_min"], 1))
        self.assertNotIn(3, [x["seq"] for x in deepen.candidates(self.d, "dancer")["new"]])

    def test_outside_ledger_video_uses_its_sound_page(self):
        """台帳に無い動画（source 0）は、その動画の音源の楽曲ページから: 取るページの音源ならその番号、ほかの音源なら music_url（page 0）。
        音源が分からなければ取らない"""
        self.put_meta(music_url=UA, music_urls=[UA, UB])
        self.set_sources({3: 0, 7: 0, 5: 0})
        self.set_music({3: (MIDX, "オリジナル楽曲 - だれか"), 7: (pipeline.music_id_of(UB), "b")})
        pl = deepen.plan_videos(self.d, [td.vid(3), td.vid(7), td.vid(5)])
        got = {x["seq"]: (x["page"], x.get("music_url")) for x in pl["targets"]}
        self.assertEqual(got, {3: (0, pipeline.music_url_of(MIDX, "オリジナル楽曲 - だれか")), 7: (2, None)})
        self.assertEqual(pl["skipped"], {"no_page": 1})
        self.assertEqual((pl["pages"], pl["music_urls"]), ([2], [got[3][1]]))
        self.assertAlmostEqual(pl["est_min"], round(2 * E["page_min"] + 2 * E["new_min"], 1))


class TestPlanAll(Base):
    def test_all_communities(self):
        """全部の界隈の候補を合わせる（新しく取る動画を先に、再生の多い順）。target に界隈を付ける"""
        pl = deepen.plan_all(self.d)
        self.assertEqual(pl["community"], "all")
        self.assertEqual([(x["seq"], x["kind"], x["community"]) for x in pl["targets"]],
                         [(5, "new", "dancer"), (7, "new", "student"), (3, "new", "dancer"),
                          (0, "more", "dancer"), (6, "more", "student")])
        self.assertEqual(pl["skipped"], {"below_min_plays": 1, "gone": 1})
        self.assertEqual(pl["communities"], {"dancer": {"new": 2, "more": 1}, "student": {"new": 1, "more": 1}})
        self.assertAlmostEqual(pl["est_min"], round(E["page_min"] + 3 * E["new_min"] + 2 * E["more_min"], 1))

    def test_unknown_is_left_out_and_per_video(self):
        """unknown（分けられなかった動画）は入れない。per_video "all" なら、上限120件で止まった動画も続きの候補になる"""
        labs = list(csv.DictReader(open(self.d / "outputs" / "labels.tsv", encoding="utf-8"), delimiter="\t"))
        with open(self.d / "outputs" / "labels.tsv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(labs[0]), delimiter="\t")
            w.writeheader()
            for r in labs:
                w.writerow({**r, "community": "unknown" if r["seq"] == "3" else r["community"]})
        self.replace_comment_row(6, n=120, cap=120)
        self.assertNotIn(6, [x["seq"] for x in deepen.plan_all(self.d)["targets"]])     # 今の上限（120件）では続きなし
        pl = deepen.plan_all(self.d, per_video="all")
        self.assertEqual([(x["seq"], x["cap"]) for x in pl["targets"]], [(5, 300), (7, 300), (0, 300), (6, 300)])


# ---------------------------------------------------------------------------
# 回の種類
# ---------------------------------------------------------------------------
class TestRequest(Base):
    def test_kinds(self):
        pl = deepen.plan_videos(self.d, [td.vid(5)], per_video="all")
        job = deepen.request(self.d, "videos", "この動画のコメントを全部読んで", pl, kind="videos",
                             video_ids=[td.vid(5)], per_video="all")
        self.assertEqual((job["round"], job["kind"], job["status"], job["video_ids"], job["per_video"]),
                         (1, "videos", "queued", [td.vid(5)], "all"))
        self.assertEqual(job["est_min"], pl["est_min"])
        self.assertNotIn("targets", job["plan"])
        self.assertEqual(worker.queue(), [td.AID])                  # 係が拾う
        with self.assertRaises(RuntimeError):                       # 順番待ちの回を上書きしない
            deepen.request(self.d, "videos", "外して", {}, kind="exclude")
        m = self.meta()
        m["deepen"]["status"] = "done"
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        for n, kind in ((2, "exclude"), (3, "include")):
            job = deepen.request(self.d, "videos", "外して", None, kind=kind, video_ids=[td.vid(5)])
            self.assertEqual((job["round"], job["kind"], job["status"], job["result"]), (n, kind, "done", {"skipped": "取得なし"}))
        self.assertEqual([h["kind"] for h in self.meta()["deepen_history"]], ["videos", "exclude"])
        self.assertEqual(worker.queue(), [])
        with self.assertRaises(ValueError):
            deepen.request(self.d, "videos", "x", {}, kind="bogus")

    def test_no_targets_and_deepen_kind(self):
        job = deepen.request(self.d, "videos", "x", deepen.plan_videos(self.d, [td.vid(1)]), kind="videos")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["skipped"], "取り足せる動画が無い")
        self.assertEqual(job["result"]["reasons"], {"all_fetched": 1})
        self.assertEqual(job["result"]["videos"][0]["seq"], 1)
        job = deepen.request(self.d, "dancer", "x", deepen.plan(self.d, "dancer"))
        self.assertEqual((job["kind"], job["status"], job["round"]), ("deepen", "queued", 2))


# ---------------------------------------------------------------------------
# 取り足しの係（動画ごと・全体）
# ---------------------------------------------------------------------------
class RunBase(Base):
    def setUp(self):
        super().setUp()
        self.patched = (pipeline.ensure_chrome, spatest.SpaCollector)
        pipeline.ensure_chrome = lambda port, log: None
        spatest.SpaCollector = FakeCollector
        FakeCollector.made = []

    def tearDown(self):
        pipeline.ensure_chrome, spatest.SpaCollector = self.patched
        super().tearDown()

    def run_job(self, aid=td.AID) -> str:
        r = deepen.Run(aid)
        r.base.lock = lambda what: None          # 本物の TikTok のロックは取らない
        r.base.yield_lock = lambda: 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            return r.run()


class TestRunVideos(RunBase):
    def test_pages_cap_and_brakes(self):
        """ページごとに spatest を回す（台帳のページ → 台帳に無い動画の音源のページ）。行ごとの上限は pool の cap、--cap はその最大。
        120件を超える上限では、spatest の1本あたりの歯止め（段数・要求の回数）を上限に比例して広げる"""
        self.put_meta(music_url=UA, music_urls=[UA, UB])
        self.set_sources({3: 0, 7: 0})
        self.set_music({3: (MIDX, "x"), 7: (pipeline.music_id_of(UB), "b")})
        pl = deepen.plan_videos(self.d, [td.vid(5), td.vid(3), td.vid(7), td.vid(0)], per_video=200)
        deepen.request(self.d, "videos", "全部読んで", pl, kind="videos", per_video=200)
        self.assertEqual(self.run_job(), "done")
        mx = pipeline.music_url_of(MIDX, "x")
        self.assertEqual([c.a.music_url for c in FakeCollector.made], [UA, UB, mx])
        ap = spatest.build_parser()
        for c in FakeCollector.made:
            self.assertEqual(c.a.cap, 200)
            self.assertEqual(c.a.max_steps, -(-ap.get_default("max_steps") * 200 // 120))
            self.assertEqual(c.a.max_calls_per_video, -(-ap.get_default("max_calls_per_video") * 200 // 120))
            self.assertIsNone(c.a.candidates)
        self.assertEqual([(r["video_id"], r["cap"]) for r in FakeCollector.made[0].pool_rows()], [(td.vid(5), "200"), (td.vid(0), "200")])
        self.assertTrue(FakeCollector.made[2].a.pool.endswith(f"r1_pool_m{MIDX}.tsv"))
        res = self.meta()["deepen"]["result"]
        self.assertEqual((res["videos_new"], res["videos_more"]), (3, 1))
        ok = deepen._ok_rows(self.d / "raw" / "comments.jsonl")
        self.assertEqual(ok[td.vid(5)]["pool_reason"], "videos:r1:dancer")
        self.assertEqual(ok[td.vid(7)]["pool_reason"], "videos:r1:student")
        text = next((self.d / "derived" / "comments").glob(f"5_{td.vid(5)}.md")).read_text(encoding="utf-8")
        self.assertIn("コメントの取り足しで取り足した（dancer）", text)

    def test_default_caps_keep_old_args(self):
        """上限が120件まで（per_video なし）なら、--cap は行の上限の最大で、歯止めは渡さない（spatest の既定のまま）"""
        self.put_meta(music_url=UA, music_urls=[UA])
        pl = deepen.plan_all(self.d)
        deepen.request(self.d, "all", "全体でもっと", pl, kind="videos")
        self.assertEqual(self.run_job(), "done")
        self.assertEqual(len(FakeCollector.made), 1)
        a = FakeCollector.made[0].a
        ap = spatest.build_parser()
        self.assertEqual((a.cap, a.max_steps, a.max_calls_per_video),
                         (120, ap.get_default("max_steps"), ap.get_default("max_calls_per_video")))
        self.assertEqual(deepen.what_of(self.meta()["deepen"]), "全体のコメントの取り足し")


# ---------------------------------------------------------------------------
# 動画を足す（取得のあと）
# ---------------------------------------------------------------------------
AID2 = "a20991231-0000-add1"
LED = [vid_at(f"2026-06-0{k + 1}", 100 + k) for k in range(6)]   # 台帳の動画（L0〜L3 は詳しく読んだ、L4・L5 は読んでいない）
X1 = vid_at("2026-05-30", 7)      # 台帳に無い、ほかの音源（ファンの音源）の動画
X2 = vid_at("2026-07-01", 9)      # 台帳に無い、動画のページを読めない動画


def enriched_row(vid: str, day: str, plays: int, music=(MID1, "きゃわぽっぴんどぅー")) -> dict:
    return {"video_id": vid, "url": f"https://www.tiktok.com/@fan/video/{vid}", "create_time": f"{day} 12:00:00",
            "desc": f"#きゃわ {vid[-3:]}", "hashtags": ["きゃわ"], "challenges": ["きゃわ"],
            "stats": {"playCount": plays, "diggCount": 10, "commentCount": 50, "shareCount": 1, "collectCount": 0, "repostCount": 0},
            "author": {"unique_id": f"fan{vid[-3:]}", "nickname": "ふぁん", "follower_count": 100, "video_count": 3, "verified": False},
            "video": {"duration": 15}, "music": {"id": music[0], "title": music[1], "duration": 60}}


class FakeEnrich:
    """analysis/enrich.py の代わり。CSV の URL のうち、まだ出力に無い動画を「読んだ」ことにする（rows に無ければ読めなかった）"""
    rows, opened = {}, []

    @staticmethod
    def run(csv_path, out_path, covers=None, limit=0, sleep=2.0, between=None, log=print, restart_every=30):
        out = Path(out_path)
        done = {json.loads(ln)["video_id"] for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()} if out.exists() else set()
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            urls = [r["URL"] for r in csv.DictReader(f)]
        ok = fail = 0
        with open(out, "a", encoding="utf-8") as fo:
            for u in urls:
                vid = u.rstrip("/").rsplit("/", 1)[-1]
                if vid in done:
                    continue
                FakeEnrich.opened.append(vid)
                rec = FakeEnrich.rows.get(vid) or {"video_id": vid, "url": u, "error": "itemStruct not found"}
                ok, fail = (ok + 1, fail) if "error" not in rec else (ok, fail + 1)
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"ok": ok, "fail": fail, "skipped": len(done)}


def make_acquired(base: Path) -> Path:
    """取得の済んだ分析（台帳・一覧・属性・集計と AI の入力・コメント）を、本物の作り直しの部品（prep_sample・build_llm_input）で作る"""
    d = base / AID2
    (d / "raw").mkdir(parents=True)
    (d / "analysis.json").write_text(json.dumps({
        "analysis_id": AID2, "title": "きゃわ", "song": {"title": "きゃわぽっぴんどぅー", "artist": "iLiFE!"},
        "music_url": U1, "music_urls": [U1], "acquisition": {"status": "done"}, "acquisition_settings": {}}, ensure_ascii=False),
        encoding="utf-8")
    led = []
    for k, v in enumerate(LED):
        r = pipeline.ledger_row(v, f"https://www.tiktok.com/@fan/video/{v}", 1, k, item(v, f"2026-06-0{k + 1}", 900_000 - k * 1000))
        led.append({**r, "read": "再生順" if k < 4 else None})
    put(d / "raw" / "ledger.jsonl", led)
    put(d / "raw" / "grid_links.jsonl", [{"url": r["url"], "video_id": r["video_id"], "order": r["order"], "type": "Video", "source": 1,
                                          "plays": r["plays"], "why": "再生順", "collected_at": r["collected_at"]} for r in led[:4]])
    put(d / "raw" / "enriched.jsonl", [enriched_row(v, f"2026-06-0{k + 1}", 900_000 - k * 1000) for k, v in enumerate(LED[:4])])
    run = pipeline.Run(AID2)
    run._write_links_csv()
    run._write_list_csv()
    with contextlib.redirect_stdout(io.StringIO()):
        run.step_derive()
    put(d / "raw" / "comments.jsonl", [comment_rows_for(v, 20, 20) for v in LED[:2]])
    (d / "derived" / "pool.tsv").write_text("video_id\tseq\tcap\tpriority\treasons\n" + "".join(
        f"{v}\t{k}\t{20 if k < 2 else 0}\t1\ttop_hit\n" for k, v in enumerate(LED[:4])), encoding="utf-8")
    deepen.prep(d)
    (d / "outputs").mkdir(exist_ok=True)
    (d / "outputs" / "labels.tsv").write_text("seq\tcommunity\n" + "".join(f"{k}\tdancer\n" for k in range(4)), encoding="utf-8")
    return d


class TestAdd(RunBase):
    def setUp(self):
        super().setUp()
        self.d2 = make_acquired(Path(self.tmp.name))
        self.saved_enrich = sys.modules.get("enrich")
        sys.modules["enrich"] = types.SimpleNamespace(run=FakeEnrich.run)
        FakeEnrich.rows = {LED[4]: enriched_row(LED[4], "2026-06-05", 896_000),
                           X1: enriched_row(X1, "2026-05-30", 2_000_000, music=(MID2, "オリジナル楽曲 - 쿠레아"))}
        FakeEnrich.opened = []
        der = self.d2 / "derived"
        self.before = {"records": {r["video_id"]: ln for ln, r in ((ln, json.loads(ln)) for ln in
                                   (der / "llm_input" / "records.jsonl").read_text(encoding="utf-8").splitlines())},
                       "videos": (der / "videos.jsonl").read_text(encoding="utf-8"),
                       "taxonomy": (der / "sample_taxonomy.md").read_text(encoding="utf-8"),
                       "label": (der / "sample_label.jsonl").read_text(encoding="utf-8").splitlines(),
                       "weekly": (der / "weekly.tsv").read_text(encoding="utf-8")}

    def tearDown(self):
        if self.saved_enrich is None:
            sys.modules.pop("enrich", None)
        else:
            sys.modules["enrich"] = self.saved_enrich
        super().tearDown()

    def request(self) -> dict:
        return deepen.request_add(self.d2, [{"url": f"https://www.tiktok.com/@fan/video/{LED[4]}?lang=ja"},
                                            {"video_id": X1, "url": f"https://www.tiktok.com/@kurea/video/{X1}"},
                                            {"video_id": X2}], "この動画も入れて")

    def test_request_add(self):
        """動画を足す回: 動画の ID は URL からも取り、URL は一覧の形に。見込みは属性・作り直し・ページ・コメント"""
        job = self.request()
        self.assertEqual((job["kind"], job["status"], job["community"], job["round"]), ("add", "queued", "add", 1))
        self.assertEqual([t["video_id"] for t in job["targets"]], [LED[4], X1, X2])
        self.assertEqual(job["targets"][1]["url"], f"https://www.tiktok.com/@kurea/video/{X1}")
        self.assertEqual(job["targets"][2]["url"], f"https://www.tiktok.com/@user/video/{X2}")
        # 属性3本・作り直し・ページの準備3つ（台帳の1ページ＋台帳に無い2本）・コメント3本
        self.assertAlmostEqual(job["est_min"], round(3 * (E["enrich_min"] + E["new_min"]) + 3 * E["page_min"] + E["derive_min"], 1))
        self.assertEqual(job["plan"]["in_ledger"], 1)
        self.assertEqual(worker.queue(), [AID2])
        m = json.loads((self.d2 / "analysis.json").read_text(encoding="utf-8"))
        m["deepen"]["status"] = "done"
        (self.d2 / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        job = deepen.request_add(self.d2, [{"url": "https://vt.tiktok.com/ZSabc/"}], "x")   # 短縮の URL は ID が分からない
        self.assertEqual((job["round"], job["status"]), (2, "done"))
        self.assertIn("分からない", job["result"]["skipped"])

    def test_run_add(self):
        self.request()
        self.assertEqual(self.run_job(AID2), "done")
        d, der = self.d2, self.d2 / "derived"
        # (1) 一覧: 台帳にある L4 は見つけたページと再生数を台帳から、台帳に無い X1・X2 は source 0。台帳の L4 に読む印
        links = {g["video_id"]: g for g in jsonl(d / "raw" / "grid_links.jsonl")}
        self.assertEqual(len(links), 7)
        self.assertEqual((links[LED[4]]["source"], links[LED[4]]["plays"], links[LED[4]]["why"]), (1, 896_000, "利用者が足した"))
        self.assertEqual((links[X1]["source"], links[X2]["source"], links[X1]["url"]), (0, 0, f"https://www.tiktok.com/@kurea/video/{X1}"))
        self.assertEqual({r["video_id"]: r["read"] for r in jsonl(d / "raw" / "ledger.jsonl")}[LED[4]], "利用者が足した")
        self.assertEqual(len(jsonl(d / "raw" / "ledger.jsonl")), 6, "台帳には足さない")
        self.assertEqual(sorted(FakeEnrich.opened), sorted([LED[4], X1, X2]), "属性は足した動画だけ取る")
        # (3) 通し番号を変えずに作り直す: 前の行はそのまま、新しい動画は前の最大（3）の次から日付順
        recs = {r["video_id"]: r for r in jsonl(der / "llm_input" / "records.jsonl")}
        lines = {json.loads(ln)["video_id"]: ln for ln in (der / "llm_input" / "records.jsonl").read_text(encoding="utf-8").splitlines()}
        for v, ln in self.before["records"].items():
            self.assertEqual(lines[v], ln, "前からある動画の行は seq も中身も同じ")
        self.assertEqual({v: recs[v]["seq"] for v in (X1, LED[4], X2)}, {X1: 4, LED[4]: 5, X2: 6})
        self.assertEqual((recs[X1]["enriched"], recs[X2]["enriched"]), (True, False))
        self.assertTrue((der / "videos.jsonl").read_text(encoding="utf-8").startswith(self.before["videos"]))
        self.assertEqual((der / "sample_taxonomy.md").read_text(encoding="utf-8"), self.before["taxonomy"])
        label = (der / "sample_label.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(label[:len(self.before["label"])], self.before["label"])
        self.assertEqual([json.loads(ln)["seq"] for ln in label[len(self.before["label"]):]], [4, 5, 6])
        self.assertIn("seq 5", (der / "llm_input" / "label_set.md").read_text(encoding="utf-8"))
        self.assertEqual((der / "weekly.tsv").read_text(encoding="utf-8"), self.before["weekly"], "週次は台帳で数える（足しても同じ）")
        self.assertEqual((der / "_before_add_r1" / "videos.jsonl").read_text(encoding="utf-8"), self.before["videos"])
        # (4) コメント: L4 は台帳のページ、X1 はその音源の楽曲ページ。X2 は動画のページが読めないので取らない
        self.assertEqual([c.a.music_url for c in FakeCollector.made], [U1, pipeline.music_url_of(MID2, "オリジナル楽曲 - 쿠레아")])
        self.assertEqual([[r["video_id"] for r in c.pool_rows()] for c in FakeCollector.made], [[LED[4]], [X1]])
        ok = deepen._ok_rows(d / "raw" / "comments.jsonl")
        self.assertEqual((ok[LED[4]]["pool_reason"], ok[X1]["pool_reason"]), ("add:r1:add", "add:r1:add"))
        self.assertIn("利用者が足した動画", (der / "comments" / f"5_{LED[4]}.md").read_text(encoding="utf-8"))
        self.assertTrue((der / "comments" / f"4_{X1}.md").exists())
        # (5) 結果
        m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        res = m["deepen"]["result"]
        self.assertEqual(m["deepen"]["status"], "done")
        self.assertEqual([(a["video_id"], a["seq"], a["enriched"], a["comments"]) for a in res["added"]],
                         [(LED[4], 5, True, 40), (X1, 4, True, 40), (X2, 6, False, 0)])
        self.assertIn("取れない", res["added"][2]["comments_why"])
        self.assertEqual([x["video_id"] for x in res["not_found"]], [X2])
        self.assertEqual((res["comments_added"], res["videos_new"], res["comment_videos"]), (80, 2, 2))
        self.assertFalse(res["blocked"])

    def test_run_add_again_changes_nothing(self):
        """係が止まってもう一度回っても、一覧に二重に足さない・控えを上書きしない・番号は同じ・属性は取り直さない・コメントも取り直さない"""
        self.request()
        self.run_job(AID2)
        der = self.d2 / "derived"
        recs1 = (der / "llm_input" / "records.jsonl").read_text(encoding="utf-8")
        m = json.loads((self.d2 / "analysis.json").read_text(encoding="utf-8"))
        m["deepen"]["status"] = "queued"
        (self.d2 / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        FakeEnrich.opened, FakeCollector.made = [], []
        self.assertEqual(self.run_job(AID2), "done")
        self.assertEqual((FakeEnrich.opened, FakeCollector.made), ([], []))
        self.assertEqual(len(jsonl(self.d2 / "raw" / "grid_links.jsonl")), 7)
        self.assertEqual((der / "llm_input" / "records.jsonl").read_text(encoding="utf-8"), recs1)
        self.assertEqual((der / "_before_add_r1" / "videos.jsonl").read_text(encoding="utf-8"), self.before["videos"])
        self.assertEqual(json.loads((self.d2 / "analysis.json").read_text(encoding="utf-8"))["deepen"]["result"]["comments_added"], 0)

    def test_derive_failure_restores(self):
        """作り直しが失敗したら、derived/ を控えから戻して、回は failed（前の分析のまま使える）"""
        self.request()
        r = deepen.Run(AID2)
        r.base.lock = lambda what: None
        orig = r.base._script

        def script(*args):
            if "build_llm_input" in str(args[0]):
                raise pipeline.StepError("build_llm_input.py が失敗しました")
            return orig(*args)
        r.base._script = script
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.run(), "failed")
        self.assertEqual((self.d2 / "derived" / "videos.jsonl").read_text(encoding="utf-8"), self.before["videos"])
        self.assertEqual((self.d2 / "derived" / "sample_label.jsonl").read_text(encoding="utf-8").splitlines(), self.before["label"])
        m = json.loads((self.d2 / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(m["deepen"]["status"], "failed")
        self.assertIn("build_llm_input", m["deepen"]["error"])
        self.assertEqual(FakeCollector.made, [])

    def test_add_is_refused_while_another_round_waits(self):
        self.request()
        with self.assertRaises(RuntimeError):
            deepen.request_add(self.d2, [{"video_id": X1}], "もう1本")



# ---------------------------------------------------------------------------
# 通し番号を変えずに作り直す（prep_sample.py の --keep-seqs）
# ---------------------------------------------------------------------------
class TestKeepSeqs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t_keep_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_csv(self, rows) -> Path:
        p = self.tmp / "list.csv"
        with open(p, "w", encoding="utf-8") as f:
            f.write("Index,URL,Created Date,Description,Likes,Shares,Comments,Plays,Saves,Reposts,Type,Username,Project Name\n")
            for i, (v, day, plays) in enumerate(rows):
                f.write(f"{i},https://www.tiktok.com/@a/video/{v},{day} 12:00:00,#tag {v},1,0,3,{plays},0,0,Video,a,t\n")
        return p

    def prep(self, out: Path, *extra) -> str:
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "prep_sample.py"), str(self.tmp / "list.csv"), str(out), *extra],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_keep_seqs(self):
        """前からある動画は前の seq と行のまま。新しい動画は前の最大の次から日付順（前より早い日付でも）。一覧から消えた動画も行を残す。
        見本は前のまま、ラベルの対象に新しい動画を足す"""
        der, prev = self.tmp / "derived", self.tmp / "prev"
        self.write_csv([("1001", "2026-06-01", 500), ("1002", "2026-06-02", 400), ("1003", "2026-06-03", 300)])
        self.prep(der)
        shutil.copytree(der, prev)
        before = {json.loads(ln)["video_id"]: ln for ln in (der / "videos.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertEqual({v: json.loads(ln)["seq"] for v, ln in before.items()}, {"1001": 0, "1002": 1, "1003": 2})
        self.write_csv([("1001", "2026-06-01", 500), ("1003", "2026-06-03", 300), ("2001", "2026-06-10", 900),
                        ("2002", "2026-05-01", 100)])
        out = self.prep(der, "--keep-seqs", str(prev / "videos.jsonl"))
        self.assertIn("kept=3 new=2", out)
        lines = (der / "videos.jsonl").read_text(encoding="utf-8").splitlines()
        rows = [json.loads(ln) for ln in lines]
        self.assertEqual([(r["video_id"], r["seq"]) for r in rows], [("1001", 0), ("1002", 1), ("1003", 2), ("2002", 3), ("2001", 4)])
        for ln in lines[:3]:
            self.assertEqual(ln, before[json.loads(ln)["video_id"]])
        self.assertEqual((der / "sample_taxonomy.md").read_text(encoding="utf-8"), (prev / "sample_taxonomy.md").read_text(encoding="utf-8"))
        old = (prev / "sample_label.jsonl").read_text(encoding="utf-8").splitlines()
        new = (der / "sample_label.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(new[:len(old)], old)
        self.assertEqual([json.loads(ln)["seq"] for ln in new[len(old):]], [3, 4])
        self.assertEqual((der / "weekly.tsv").read_text(encoding="utf-8").splitlines()[0], "week\tn\tplays")


# ---------------------------------------------------------------------------
# 一覧の段: 利用者が足した動画（user_videos）を必ず読む
# ---------------------------------------------------------------------------
class TestUserVideosInList(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        old_dir = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = tmp
        self.addCleanup(setattr, pipeline, "ANALYSES_DIR", old_dir)
        its = [item(vid_at("2026-06-01", j), "2026-06-01", 1_000_000 - j * 1000) for j in range(30)]
        self.top, self.low = its[0]["id"], its[-1]["id"]
        self.out = vid_at("2026-06-03", 999)
        grid = [f"https://www.tiktok.com/@fan/video/{it['id']}" for it in its]

        class Driver:
            url = None

            def execute_cdp_cmd(self, cmd, args):
                pass

            def get(self, u):
                self.url = u

            def execute_script(self, js):
                if js == pipeline.MUSIC_PAGE_JS:
                    return {"title": "きゃわぽっぴんどぅー", "creator": "iLiFE!", "video_count_text": "31.1K 動画"}
                if js == "COUNT":
                    return len(grid)
                if js == pipeline.LEDGER_READ_JS:
                    return list(its)
                if js == pipeline.PAGE_TEXT_JS:
                    return ""
                return list(grid)

            def find_element(self, *a):
                return types.SimpleNamespace(send_keys=lambda *k: None)

            def quit(self):
                pass
        old_mod = sys.modules.get("scraper")
        sys.modules["scraper"] = types.SimpleNamespace(create_headless_driver=Driver, PAGE_LOAD_TIME=0, SCROLL_PAUSE_TIME=0,
                                                       _COUNT_LINKS_JS="COUNT")
        self.addCleanup(lambda: sys.modules.__setitem__("scraper", old_mod) if old_mod else sys.modules.pop("scraper", None))
        old_wait = pipeline.read_music_page.__defaults__
        pipeline.read_music_page.__defaults__ = (0,)
        self.addCleanup(setattr, pipeline.read_music_page, "__defaults__", old_wait)
        aid = "a20991231-0000-user"
        self.d = tmp / aid
        (self.d / "raw").mkdir(parents=True)
        (self.d / "analysis.json").write_text(json.dumps({
            "analysis_id": aid, "title": "t", "music_url": U1, "music_urls": [U1],
            "song": {"title": "きゃわぽっぴんどぅー", "artist": "iLiFE!"},
            "acquisition_settings": {"list_stall": 1, "read_normal": 5, "read_floor": 5},
            "user_videos": [{"video_id": self.low, "url": f"https://www.tiktok.com/@fan/video/{self.low}"},
                            {"url": f"https://www.tiktok.com/@someone/video/{self.out}?is_from_webapp=1"},
                            {"video_id": self.top}],
            "acquisition": {"status": "running"}}), encoding="utf-8")
        self.run_ = pipeline.Run(aid)
        self.run_.lock = lambda what: None

    def test_user_videos_are_read(self):
        with contextlib.redirect_stdout(io.StringIO()):
            res = self.run_.step_list()
        links = {g["video_id"]: g for g in jsonl(self.d / "raw" / "grid_links.jsonl")}
        ledger = {r["video_id"]: r for r in jsonl(self.d / "raw" / "ledger.jsonl")}
        self.assertEqual(len(ledger), 30, "台帳には足さない")
        self.assertEqual(len(links), 6 + 2, "枠（再生順5・最初期1）の外に、台帳の低い1本と台帳に無い1本")
        self.assertEqual((links[self.low]["why"], links[self.low]["source"], ledger[self.low]["read"]), ("利用者が足した", 1, "利用者が足した"))
        self.assertEqual((links[self.out]["why"], links[self.out]["source"], links[self.out]["url"]),
                         ("利用者が足した", 0, f"https://www.tiktok.com/@someone/video/{self.out}"))
        self.assertNotEqual(links[self.top]["why"], "利用者が足した", "もう選んでいた動画はその理由のまま")
        self.assertEqual((res["links"], res["user_videos"], res["reasons"]["利用者が足した"]), (8, {"n": 3, "outside_ledger": 1}, 2))
        self.assertEqual((self.d / "raw" / "grid_links.csv").read_text(encoding="utf-8").count("\n"), 9)
        # 一覧を書いたあとで足された動画も、続きから回せば足す
        m = json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))
        late = vid_at("2026-06-04", 5)
        m["user_videos"].append({"video_id": late})
        (self.d / "analysis.json").write_text(json.dumps(m), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            res = self.run_.step_list()
        self.assertTrue(res["resumed"])
        self.assertEqual(res["user_videos"]["added"], [late])
        self.assertEqual(len(jsonl(self.d / "raw" / "grid_links.jsonl")), 9)


# ---------------------------------------------------------------------------
# コメントの段: 台帳に無い動画（source 0）があっても壊れない
# ---------------------------------------------------------------------------
class TestCommentPagesWithOutside(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        old_dir = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = tmp
        self.addCleanup(setattr, pipeline, "ANALYSES_DIR", old_dir)
        aid = "a20991231-0000-src0"
        self.d = d = tmp / aid
        for sub in ("raw", "derived", "fetch_log"):
            (d / sub).mkdir(parents=True)
        put(d / "raw" / "grid_links.jsonl", [{"url": f"https://www.tiktok.com/@x/video/{v}", "video_id": v, "source": s}
                                             for v, s in (("101", 1), ("201", 2), ("301", 0), ("302", 0), ("303", 0))])
        (d / "derived" / "pool.tsv").write_text("video_id\tweek\tpriority\treasons\tcap\n101\tw1\t1\tkey\t20\n201\tw1\t1\tkey\t20\n"
                                                "301\tw1\t1\tkey\t20\n302\tw1\t1\tkey\t20\n303\tw1\t2\tweek\t0\n", encoding="utf-8")
        put(d / "raw" / "enriched.jsonl", [{"video_id": "301", "music": {"id": MIDX, "title": "x"}}])   # 302 は音源が分からない
        self.aid = aid

    def pages(self, urls):
        (self.d / "analysis.json").write_text(json.dumps({"analysis_id": self.aid, "music_url": urls[0], "music_urls": urls}),
                                              encoding="utf-8")
        run = pipeline.Run(self.aid)
        with contextlib.redirect_stdout(io.StringIO()):
            return run.comment_pages(), run

    @staticmethod
    def vids(pool: Path) -> list:
        return [r["video_id"] for r in pipeline.comment_rows(pool)]

    def test_multi_page(self):
        pages, run = self.pages([U1, U2])
        mx = pipeline.music_url_of(MIDX, "x")
        self.assertEqual([p[0] for p in pages], [U1, U2, mx])
        self.assertEqual([self.vids(p[1]) for p in pages], [["101"], ["201"], ["301"]])
        self.assertEqual((pages[2][1].name, pages[2][2].name, pages[2][3].name),
                         (f"pool_m{MIDX}.tsv", f"comments_summary_m{MIDX}.json", f"substitutions_m{MIDX}.tsv"))
        self.assertIn("303", pages[2][1].read_text(encoding="utf-8"), "cap 0 の行は全部のプールに残す（差し替え先にしない）")
        self.assertEqual(run.link_sources()["301"], 0)

    def test_single_page(self):
        """1ページでも、ほかの音源の動画はその音源のページへ。同じ音源なら前と同じファイルのまま"""
        pages, _ = self.pages([U1])
        self.assertEqual([(p[0], p[1].name) for p in pages], [(U1, "pool_p1.tsv"), (pipeline.music_url_of(MIDX, "x"), f"pool_m{MIDX}.tsv")])
        self.assertEqual(self.vids(pages[0][1]), ["101", "201"])
        put(self.d / "raw" / "enriched.jsonl", [{"video_id": v, "music": {"id": pipeline.music_id_of(U1)}} for v in ("301", "302")])
        pages, _ = self.pages([U1])
        self.assertEqual([(p[0], p[1].name) for p in pages], [(U1, "pool.tsv")])

    def test_pool_left_and_check(self):
        pages, run = self.pages([U1, U2])
        self.assertEqual([run.pool_left(p[1]) for p in pages], [1, 1, 1])
        put(self.d / "raw" / "comments.jsonl", [{"video_id": v, "status": "ok", "comments": [{"aweme_id": v}]} for v in ("101", "301")])
        res = run._check_comments(False)
        self.assertEqual(res["videos_ok_by_page"], {"0": 1, "1": 1})


# ---------------------------------------------------------------------------
# メニューの文
# ---------------------------------------------------------------------------
class TestMenuText(unittest.TestCase):
    def test_describe_deepen(self):
        sys.path.insert(0, str(ROOT / "collector"))
        from collector_app import jobs
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = jobs.config.ANALYSES_DIR
        jobs.config.ANALYSES_DIR = tmp
        self.addCleanup(setattr, jobs.config, "ANALYSES_DIR", saved)

        def text(status, **dp):
            return jobs.describe_deepen({"analysis_id": "a1", "title": "曲", "deepen": {"status": status, "round": 1, **dp}})
        self.assertEqual(text("queued", kind="videos", community="videos"), "順番待ち: 曲（動画のコメントの取り足し）")
        self.assertEqual(text("queued", kind="videos", community="all"), "順番待ち: 曲（全体のコメントの取り足し）")
        self.assertEqual(text("queued", kind="add"), "順番待ち: 曲（動画の追加（属性とコメント））")
        self.assertEqual(text("running", kind="add", targets=[1, 2, 3]), "動画を足しています: 曲（属性とコメント）3本")
        self.assertEqual(text("running", kind="videos", community="videos", targets=[1, 2]), "取り足し中: 曲（動画のコメント）0/2本")
        # 今までの掘り下げ・切り直しの文はそのまま
        self.assertEqual(text("queued"), "順番待ち: 曲（界隈の掘り下げ）")
        self.assertEqual(text("queued", kind="recut"), "順番待ち: 曲（界隈の切り直し）")
        self.assertEqual(text("running", community="dancer", targets=[1]), "取り足し中: 曲（界隈 dancer）0/1本")
        self.assertEqual(text("running", kind="recut", targets=[1]), "取り足し中: 曲（切り直した界隈）0/1本")


if __name__ == "__main__":
    unittest.main()
