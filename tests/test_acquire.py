"""取得の段の部品の単体テスト（TikTok に触らない）。

  python -m unittest tests.test_acquire

- tiktok_lock: 取る・待つ・譲る・死んだ持ち主の掃除
- analysis/pool.py: 検証版（pool_verify.py）と同じ数字が出るか（シルエットの手持ちデータがあるときだけ）
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
        self.assertGreaterEqual(sum(1 for r in reps if r["seq"] in seqs), 53)   # 検証版: 代表65本中53

    def test_origin_gets_120_and_artist_only_gets_40(self):
        import pool
        videos, enriched = pool.load(TRIAL / "videos.jsonl", TRIAL / "enriched.jsonl")
        p, ex = pool.build(videos, enriched, 240)
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

    def test_replies_not_opened_by_default(self):
        from acquire import pipeline
        d = pipeline.DEFAULTS
        self.assertEqual((d["reply_top"], d["reply_questions"], d["reply_author"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
