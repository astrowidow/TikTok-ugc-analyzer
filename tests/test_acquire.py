"""取得の段の部品の単体テスト（TikTok に触らない）。

  python -m unittest tests.test_acquire

- tiktok_lock: 取る・待つ・譲る・死んだ持ち主の掃除
- analysis/pool.py: 検証版（pool_verify.py）と同じ数字が出るか（シルエットの手持ちデータがあるときだけ）
- 1本1ページ・週ごとは取らない（2026-10-06）: プールの上限の付け替え・spatest が上限0件の動画を開かないこと・見込み時間
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))


class TestTiktokLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UGC_LOCK_DIR"] = self.tmp.name
        import importlib
        import tiktok_lock
        self.lk = importlib.reload(tiktok_lock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_release(self):
        self.assertTrue(self.lk.try_acquire("a"))
        self.assertFalse(self.lk.try_acquire("b"))
        self.assertEqual(self.lk.holder()["owner"], "a")
        self.lk.release("a")
        self.assertIsNone(self.lk.holder())

    def test_release_only_by_owner(self):
        self.assertTrue(self.lk.try_acquire("a"))
        self.lk.release("b")
        self.assertEqual(self.lk.holder()["owner"], "a")

    def test_stale_lock_is_removed(self):
        # 死んだ PID の持ち主（大きな番号）のロックは取り除かれる
        Path(self.tmp.name, "tiktok.lock").write_text(json.dumps({"owner": "dead", "pid": 2 ** 22 + 12345}))
        self.assertIsNone(self.lk.holder())
        self.assertTrue(self.lk.try_acquire("a"))

    def test_waiters_and_yield(self):
        """別プロセスが待っていたら譲り、相手が終われば取り戻す"""
        self.assertTrue(self.lk.try_acquire("acq"))
        code = ("import os,sys,time; sys.path.insert(0, %r); os.environ['UGC_LOCK_DIR']=%r; import tiktok_lock as t;"
                "t.acquire('csv', poll=0.2); time.sleep(0.5); t.release('csv')") % (str(ROOT), self.tmp.name)
        p = subprocess.Popen([sys.executable, "-c", code])
        for _ in range(50):
            if self.lk.someone_waiting():
                break
            time.sleep(0.1)
        self.assertTrue(self.lk.someone_waiting())
        waited = self.lk.yield_if_waiting("acq")
        p.wait(timeout=30)
        self.assertGreater(waited, 0.3)
        self.assertEqual(self.lk.holder()["owner"], "acq")
        self.assertEqual(self.lk.someone_waiting(), [])
        self.lk.release("acq")

    def test_no_yield_without_waiters(self):
        self.assertTrue(self.lk.try_acquire("acq"))
        self.assertEqual(self.lk.yield_if_waiting("acq"), 0.0)
        self.lk.release("acq")


TRIAL = ROOT / "output" / "trial_silhouette"


@unittest.skipUnless((TRIAL / "enriched.jsonl").exists(), "シルエットの手持ちデータが無い")
class TestPool(unittest.TestCase):
    def test_same_as_verified(self):
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 240)
        self.assertEqual(ex["artist_accounts"], ["kanaboon_official"])
        self.assertEqual(ex["campaign_tags"], ["silhouettetogether", "みんなでシルエット"])
        import csv
        with open(TRIAL / "reps.tsv", encoding="utf-8") as f:
            reps = list(csv.DictReader(f, delimiter="\t"))
        seqs = {str(v["seq"]) for v in videos if v["video_id"] in p}
        # 検証版: 代表65本中53。2026-10-05 に週ごとの動画へ再生1万の下限を入れて51（外れたのは再生699〜1,978の、
        # 旧方式で界隈×段階の枠を埋めるために選ばれた代表。今の流れの本番レポートは1万未満の動画を使っていない）
        self.assertGreaterEqual(sum(1 for r in reps if r["seq"] in seqs), 51)

    def test_origin_gets_120_and_artist_only_gets_40(self):
        """前の取り方（full）の上限"""
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 240, plan="full")
        by_seq = {v["seq"]: v["video_id"] for v in videos}
        self.assertEqual(p[by_seq[0]]["cap"], 120)          # 起点
        artist = [vid for vid, x in p.items() if "artist" in x["reasons"]]
        self.assertTrue(artist)
        for v in artist:   # 2026-10-05: 120件の理由が本人だけなら40件。起点・大型ヒット・公式も兼ねれば120件
            other = set(p[v]["reasons"]) & pool.KEY_REASONS_BUT_ARTIST
            self.assertEqual(p[v]["cap"], 120 if other else 40, p[v]["reasons"])
        self.assertEqual(ex["n_artist_capped"], sum(1 for v in artist if not set(p[v]["reasons"]) & pool.KEY_REASONS_BUT_ARTIST))

    def test_artist_cap_does_not_change_selection(self):
        """本人だけの動画を40件にしても、選ぶ動画（本数）は変えない（浮いた分は時間の短縮に回す）"""
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1)
        keep = pool.KEY_REASONS_BUT_ARTIST
        try:
            pool.KEY_REASONS_BUT_ARTIST = keep | {"artist"}     # 下げない（2026-10-05 より前の決まり）
            p_old, _ = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1)
        finally:
            pool.KEY_REASONS_BUT_ARTIST = keep
        self.assertEqual(set(p), set(p_old))

    def test_page_plan(self):
        """2026-10-06: 必ず入れる動画は1本1ページ（20件）、週ごとの動画は0件（取らない）。選ぶ動画は前の取り方と同じ"""
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1)
        p_full, ex_full = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1, plan="full")
        self.assertEqual(set(p), set(p_full))                                  # ラベルを付ける動画は変わらない
        must = [v for v, x in p.items() if any(not r.startswith("week:") for r in x["reasons"])]
        weekly = [v for v in p if v not in must]
        self.assertTrue(must and weekly)
        self.assertTrue(all(p[v]["cap"] == pool.CAP_PAGE for v in must))
        self.assertTrue(all(p[v]["cap"] == 0 for v in weekly))
        self.assertEqual(ex["n_pool"], len(must))                               # 進み具合・見込みに使う本数
        self.assertEqual((ex["n_rows"], ex["n_no_comments"]), (len(p), len(weekly)))
        self.assertEqual(ex_full["n_pool"], len(p_full))
        with self.assertRaises(ValueError):
            pool.build(videos, enriched, 240, plan="?")

    def test_page_plan_writes_cap0_and_pipeline_counts_comment_rows(self):
        import pool
        from acquire import pipeline
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "pool.tsv"
            pool.write(p, videos, out)
            self.assertEqual(len(pipeline.comment_rows(out)), ex["n_pool"])

    def test_weekly_picks_skip_weak_videos(self):
        """2026-10-05: 週ごとに配る動画は再生1万以上だけ（必ず入れる動画には掛けない）"""
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, _ = pool.build(videos, enriched, 12 * 60, cost_std=3.1, cost_key=6.1)
        plays = {v["video_id"]: v.get("plays") or 0 for v in videos}
        weekly = [v for v, x in p.items() if all(r.startswith("week:") for r in x["reasons"])]
        self.assertTrue(weekly)
        self.assertTrue(all(plays[v] >= pool.MIN_PLAYS_WEEKLY for v in weekly))

    def test_page_plan_defaults_and_eta(self):
        """既定は page。見込み時間はコメントを取る本数×1本の見込み（前の取り方より大幅に短い）"""
        from acquire import pipeline, launch
        d = pipeline.DEFAULTS
        self.assertEqual(d["comment_plan"], "page")
        self.assertEqual(pipeline.subs_cap(d), 20)
        self.assertEqual(pipeline.subs_cap({**d, "comment_plan": "full"}), 40)
        self.assertLess(pipeline.minutes_per_comment_video(d), d["min_per_video"])
        m = {"analysis_id": "x", "acquisition": {"steps": {
            "resolve": {"status": "done"}, "list": {"status": "done", "detail": {"links": 900}}, "enrich": {"status": "done"},
            "derive": {"status": "done"},
            "pool": {"status": "done", "detail": {"n_pool": 60}}}}}
        page = launch._remaining_seconds(m)
        full = launch._remaining_seconds({**m, "acquisition_settings": {"comment_plan": "full"}})
        self.assertAlmostEqual(page, 60 * d["min_per_page_video"] * 60 + 600 + 30 + 5, delta=1)
        self.assertGreater(full, page * 2)
        # プールが決まる前は、コメントを取る本数を TYPICAL_COMMENT_VIDEOS と見る（前は12時間分の本数）
        m0 = {"analysis_id": "x", "acquisition": {"steps": {}}}
        self.assertLess(launch._remaining_seconds(m0), 3 * 3600)

    def test_replies_not_opened_by_default(self):
        from acquire import pipeline
        d = pipeline.DEFAULTS
        self.assertEqual((d["reply_top"], d["reply_questions"], d["reply_author"]), (0, 0, 0))

    def test_pace_and_fallback(self):
        """2026-10-05: 平均3回/分・60秒に4回。止まったら 1.8回/分・60秒に2回に落とす"""
        from acquire import pipeline
        d = pipeline.DEFAULTS
        self.assertEqual((d["calls_per_min"], d["max_calls_per_min"]), (3.0, 4.0))
        self.assertLess(d["fallback_calls_per_min"], d["calls_per_min"])
        self.assertLess(d["fallback_max_calls_per_min"], d["max_calls_per_min"])


class TestSpatestPool(unittest.TestCase):
    """spatest は上限0件の動画（週ごとの動画）を開かず、差し替え先にもしない（2026-10-06）"""
    def test_cap0_rows_are_skipped(self):
        from acquire import spatest
        with tempfile.TemporaryDirectory() as td:
            pool_p = Path(td) / "pool.tsv"
            pool_p.write_text("video_id\tseq\tdate\tweek\tplays\tusername\tcap\tpriority\treasons\n"
                              "1000001\t0\t2025-07-20\tW1\t100\ta\t20\t1\torigin\n"
                              "1000002\t1\t2025-07-20\tW1\t900\tb\t0\t2\tweek:W1\n"
                              "1000003\t2\t2025-07-21\tW1\t500\tc\t20\t1\ttop_hit\n", encoding="utf-8")
            cand = Path(td) / "records.jsonl"
            cand.write_text("".join(json.dumps({"video_id": v, "week": "W1", "plays": pl}) + "\n"
                                    for v, pl in (("1000002", 900), ("1000004", 300))), encoding="utf-8")
            a = spatest.build_parser().parse_args(["--music-url", "https://www.tiktok.com/music/x-1", "--pool", str(pool_p),
                                                   "--candidates", str(cand), "--subs-cap", "20",
                                                   "--out", str(Path(td) / "out.jsonl"), "--log", str(Path(td) / "log.txt")])
            c = spatest.SpaCollector(a)
            asked = {}

            def collect_links(n, want_ids=None):
                asked["want"] = list(want_ids or [])
                # グリッドに見えているのは 1000001・1000002・1000004（1000003 は見つからない）
                return [f"https://www.tiktok.com/@u/video/{v}" for v in ("1000001", "1000002", "1000004")]
            c.collect_links = collect_links
            got = [spatest.vid_of(h) for h in c.pool_targets(set())]
        self.assertNotIn("1000002", asked["want"])                  # 探しもしない
        self.assertNotIn("1000002", got)                            # 開かない
        self.assertEqual(got, ["1000001", "1000004"])               # 見つからない 1000003 は、プールの外の 1000004 に差し替え
        self.assertEqual(c.caps["1000004"], 20)


if __name__ == "__main__":
    unittest.main()
