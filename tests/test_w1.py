"""AI の段（W1）の部品の単体テスト（TikTok にも AI にも触らない）。

  python -m unittest tests.test_w1

- flow_w1: 界隈の代表例の検査・note 用原稿の検査（内部の印・key・URL）・数字の書き方
- user_settings: 見る・変える・戻す・指示書に差し込む欄
- downloads: 鍵と名前の照合（決まった名前以外・知らない鍵は見つからない）
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeAnalysis:
    """check_finished が使う最小限（確定した分類軸だけ）"""

    def __init__(self, tax):
        self.tax = tax


class TestFlowW1(unittest.TestCase):
    def setUp(self):
        import flow_w1
        import proto_runner
        self.f = flow_w1
        self.pr = proto_runner

    def test_examples(self):
        tax = {"community": {"jp_student": "d", "sea_general": "d", "unknown": "x"}}
        self.assertTrue(self.f.check_examples(tax, {1, 2, 3}))                     # examples が無い
        tax["examples"] = {"jp_student": [1, 2], "sea_general": [9]}
        errs = self.f.check_examples(tax, {1, 2, 3})
        self.assertTrue(any("sea_general" in e for e in errs))                     # サンプルに無い seq
        tax["examples"] = {"jp_student": [1, 2], "sea_general": [3]}
        self.assertEqual(self.f.check_examples(tax, {1, 2, 3}), [])
        tax["examples"]["nope"] = [1]
        self.assertTrue(self.f.check_examples(tax, {1, 2, 3}))                     # 界隈に無い key

    def test_finished_checks(self):
        tax = {"community": {"jp_student": "d", "unknown": "x"}, "format": {"hand_dance": "d"}, "motive": {"trend_ride": "d"}}
        orig = self.pr._taxonomy
        self.pr._taxonomy = lambda a, confirmed=True: tax
        try:
            ok_url = "https://www.tiktok.com/@a/video/1"
            good = f"## 見出し\n\n@a（2025-07-10、280万再生）— 起点\n{ok_url}\n\n「すごい」（10 いいね）\n" + "本文。" * 60
            self.assertEqual(self.f.check_finished(None, good, {ok_url}), [])
            bad = good + "\nseq 57 と cid 123 と P2、jp_student\n"
            errs = self.f.check_finished(None, bad, {ok_url})
            self.assertTrue(any("内部の印" in e for e in errs))
            self.assertTrue(any("jp_student" in e for e in errs))
            inline = good.replace(f"\n{ok_url}\n", f"\nこれ {ok_url} を見て\n")
            self.assertTrue(any("それだけの行" in e for e in self.f.check_finished(None, inline, {ok_url})))
            other = good.replace(ok_url, "https://www.tiktok.com/@b/video/2")
            self.assertTrue(any("一覧に無い" in e for e in self.f.check_finished(None, other, {ok_url})))
        finally:
            self.pr._taxonomy = orig

    def test_fmt_plays(self):
        self.assertEqual(self.f.fmt_plays(2800000), "280万")
        self.assertEqual(self.f.fmt_plays(763460000), "7.6億")
        self.assertEqual(self.f.fmt_plays(9345), "9,345")

    def test_labels_in_zip(self):
        """F4: Excel 用 ZIP にラベルの表（地域つき）が入る。地域はサービスが付けた値"""
        import csv
        import io
        import zipfile
        f = self.f
        saved = (f.records, f.labels, f.videos, f.phases, self.pr._taxonomy)
        f.records = lambda a: {0: {"date": "2025-07-20", "plays": 10, "author": {"id": "u0"}, "location_created": "MM",
                                   "text_language": "ja"},
                               1: {"date": "2025-10-27", "plays": 20, "author": {"id": "u1"}, "location_created": "JP",
                                   "text_language": "ja"}}
        f.labels = lambda a: {1: {"community": "jp_student", "format": "hand_dance", "motive": "trend", "tier": "general",
                                  "conf": "H", "reason": "#08", "region": "JP"}}
        f.videos = lambda a: {0: {"url": "https://www.tiktok.com/@u0/video/111"}, 1: {"url": "https://www.tiktok.com/@u1/video/222"}}
        f.phases = lambda a: [{"id": "P1", "name": "イノベーター", "start": "2025-07-01", "end": "2025-12-31"}]
        self.pr._taxonomy = lambda a, confirmed=True: {"community": {"jp_student": "日本の中高生。制服など"}}
        try:
            with tempfile.TemporaryDirectory() as td:
                zp = Path(td) / "data.zip"
                with zipfile.ZipFile(zp, "w") as z:
                    z.writestr("README.txt", "readme\n")
                    z.writestr("videos.csv", "x\n")
                self.assertEqual(f.add_labels_to_zip(None, zp), 2)
                with zipfile.ZipFile(zp) as z:
                    names = z.namelist()
                    raw = z.read("labels.csv")
                    readme = z.read("README.txt").decode("utf-8")
                self.assertIn("videos.csv", names)
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                     # Excel で文字化けしない
                rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
                self.assertIn("地域", rows[0])
                by = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
                self.assertEqual(by["1"]["地域"], "JP")
                self.assertEqual(by["1"]["界隈の説明"], "日本の中高生")
                self.assertEqual(by["0"]["地域"], "MM")                              # ラベルの無い動画も地域は入る
                self.assertEqual(by["0"]["界隈"], "")
                self.assertIn("labels.csv", readme)
        finally:
            f.records, f.labels, f.videos, f.phases, self.pr._taxonomy = saved

    def test_cited(self):
        seqs, cids = self.f.cited("seq 57（@a）と seq 3、『x』（1 いいね、cid 7551355023343521040）")
        self.assertEqual(seqs, {57, 3})
        self.assertEqual(cids, {"7551355023343521040"})


class TestUserSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UGC_USERS_DIR"] = self.tmp.name
        import user_settings
        self.us = importlib.reload(user_settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_block_is_empty(self):
        self.assertEqual(self.us.prompt_block("u1", ["style", "focus"]), "")

    def test_set_and_reset(self):
        self.us.set_item("u1", "focus", "界隈ごとの伸び方")
        self.us.set_item("u1", "style", "です・ます調", use_style_guide=False)
        block = self.us.prompt_block("u1", ["style", "focus"])
        self.assertIn("界隈ごとの伸び方", block)
        self.assertIn("文体ガイドには合わせない", block)
        self.assertIn("出力の形の決まり", block)
        self.us.reset("u1")
        self.assertEqual(self.us.prompt_block("u1", ["style", "focus"]), "")
        hist = json.loads((Path(self.tmp.name) / "u1" / "settings.json").read_text(encoding="utf-8"))["history"]
        self.assertGreaterEqual(len(hist), 4)                       # 変更も戻しも履歴に残る

    def test_limits(self):
        with self.assertRaises(self.us.SettingsError):
            self.us.set_item("u1", "nope", "x")
        with self.assertRaises(self.us.SettingsError):
            self.us.set_item("u1", "focus", "あ" * 401)
        with self.assertRaises(self.us.SettingsError):
            self.us.get("../etc")


class TestDownloads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UGC_ANALYSES_DIR"] = self.tmp.name
        import downloads
        self.dl = importlib.reload(downloads)
        d = Path(self.tmp.name) / "a1"
        (d / "outputs").mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({"analysis_id": "a1", "download_key": "k" * 32}), encoding="utf-8")
        (d / "outputs" / "REPORT.md").write_text("# r", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_find(self):
        self.assertEqual(self.dl.find("k" * 32)[1]["analysis_id"], "a1")
        self.assertIsNone(self.dl.find("x" * 32))
        self.assertIsNone(self.dl.find("short"))
        self.assertIsNone(self.dl.find("../a1"))
        self.assertNotIn("analysis.json", self.dl.FILES)


if __name__ == "__main__":
    unittest.main()
