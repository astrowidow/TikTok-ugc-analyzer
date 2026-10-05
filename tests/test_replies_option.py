"""返信を取るかどうかを、分析ごとに道具（start_analysis / restart_analysis の replies）で選べること（2026-10-05）。

既定は取らない（acquire/pipeline.py の DEFAULTS の reply_* が0）。replies=True の分析だけ、目録の acquisition_settings に reply_* を書く。
取得アプリ（proto_runner.LOCAL）は偽物に差し替える（TikTok にも本物の置き場にも触らない）。

  python3 -m unittest tests.test_replies_option
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import proto_runner as pr  # noqa: E402
from acquire import pipeline  # noqa: E402

URL = "https://www.tiktok.com/music/test-7000000000000000011"
URL2 = "https://www.tiktok.com/music/test2-7000000000000000012"


class FakeLocal:
    def __init__(self):
        self.stopped = []

    def acquisition_settings(self):
        return {"chrome_port": "9250"}

    def inspect_music(self, url):
        return {"title": "テスト", "creator": "だれか", "video_count": 100}

    def find_sounds(self, *a, **k):
        raise AssertionError("楽曲ページを渡したときは探さない")

    def ensure_app(self):
        return {"started": False}

    def app_state(self):
        return {"running": True, "login_wanted": False}

    def request_stop(self, aid):
        self.stopped.append(aid)


class TestRepliesOption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL)
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = Path(self.tmp.name)
        pr.LOCAL = FakeLocal()

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL = self.saved
        self.tmp.cleanup()

    def meta(self, aid):
        return json.loads((Path(self.tmp.name) / aid / "analysis.json").read_text(encoding="utf-8"))

    def test_default_no_replies(self):
        self.assertEqual((pipeline.DEFAULTS["reply_top"], pipeline.DEFAULTS["reply_questions"],
                          pipeline.DEFAULTS["reply_author"]), (0, 0, 0))
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[URL])
        m = self.meta(r["analysis_id"])
        self.assertFalse(pr.wants_replies(m))
        self.assertEqual(m["acquisition_settings"], {"chrome_port": "9250"})
        self.assertNotIn("返信も取ります", r["text"])

    def test_replies_true_writes_settings(self):
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[URL], replies=True)
        m = self.meta(r["analysis_id"])
        self.assertTrue(pr.wants_replies(m))
        self.assertEqual({k: m["acquisition_settings"][k] for k in pr.REPLIES_ON}, pr.REPLIES_ON)
        self.assertEqual(m["acquisition_settings"]["chrome_port"], "9250")   # 取得アプリの設定は残す
        self.assertIn("返信も取ります", r["text"])
        # 取得のときの設定は、目録が既定より優先される
        merged = {**pipeline.DEFAULTS, **m["acquisition_settings"]}
        self.assertEqual(merged["reply_top"], 1)

    def test_restart_inherits_and_overrides(self):
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[URL], replies=True)
        r2 = pr.restart_analysis("u1", r["analysis_id"], music_url=URL2)        # 省けば引き継ぐ
        self.assertTrue(pr.wants_replies(self.meta(r2["analysis_id"])))
        r3 = pr.restart_analysis("u1", r2["analysis_id"], music_url=URL, replies=False)   # 明示すれば変えられる
        self.assertFalse(pr.wants_replies(self.meta(r3["analysis_id"])))

    def test_restart_from_search_keeps_note(self):
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[URL], replies=True)
        res = pr.restart_analysis("u1", r["analysis_id"])                         # 楽曲ページ探しから
        self.assertIn("replies=true", res["text"])


if __name__ == "__main__":
    unittest.main()
