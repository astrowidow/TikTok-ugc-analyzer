"""取得アプリの形（各自の Mac で全部を回す）で足した部品の単体テスト（TikTok にも note にも Claude にも触らない）。

  python -m unittest tests.test_local_app

- prompt_store: 写す・編集した指示書は残す・編集していないものは新しい初期に入れ替える・使えない指示書は初期を使う・出力の形は初期のもの
- kb_update: 新着の見つけ方（一覧の止めどころ）・本文の取り込み・カードの検査・整理の仕事
- proto_runner: 終わる時刻の言い方
"""
import datetime
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestPromptStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UGC_PROMPTS_DIR"] = self.tmp.name
        import prompt_store
        self.ps = importlib.reload(prompt_store)

    def tearDown(self):
        os.environ.pop("UGC_PROMPTS_DIR", None)
        self.tmp.cleanup()

    def test_seed_and_edit(self):
        done = self.ps.seed()
        self.assertEqual(len(done["copied"]), len(self.ps.TITLES))
        p = Path(self.tmp.name) / "w1" / "label.md"
        text = p.read_text(encoding="utf-8")
        p.write_text("【編集】\n" + text, encoding="utf-8")
        self.assertTrue(self.ps.load("label.md").startswith("【編集】"))
        # 初期の指示書が変わった（アプリを新しくした）ことにする: 編集したものは残り、ほかは入れ替わる
        man = json.loads((Path(self.tmp.name) / "w1" / ".defaults.json").read_text(encoding="utf-8"))
        man = {k: "old" for k in man}
        (Path(self.tmp.name) / "w1" / ".defaults.json").write_text(json.dumps(man), encoding="utf-8")
        q = Path(self.tmp.name) / "w1" / "axes.md"
        done = self.ps.seed()
        self.assertIn("label.md", done["kept_edited"])
        self.assertTrue(p.read_text(encoding="utf-8").startswith("【編集】"))
        self.assertEqual(q.read_text(encoding="utf-8"), self.ps.default_text("axes.md"))

    def test_broken_falls_back(self):
        self.ps.seed()
        p = Path(self.tmp.name) / "w1" / "label.md"
        p.write_text(p.read_text(encoding="utf-8").replace("{{entries}}", ""), encoding="utf-8")
        self.assertEqual(self.ps.load("label.md"), self.ps.default_text("label.md"))
        row = next(r for r in self.ps.status() if r["name"] == "label.md")
        self.assertFalse(row["ok"])
        self.assertTrue(self.ps.put("label.md", "中身\n{{song}}"))   # 印が足りないので断る

    def test_output_section_is_fixed(self):
        self.ps.seed()
        p = Path(self.tmp.name) / "w1" / "label.md"
        text = p.read_text(encoding="utf-8")
        head, tail = text.split("### 出力の形", 1)
        p.write_text(head + "### 出力の形（勝手に変えた）\n好きな形で\n", encoding="utf-8")
        got = self.ps.load("label.md")
        self.assertIn("seq\tcommunity\tformat", got)
        self.assertNotIn("好きな形で", got)

    def test_without_user_dir(self):
        os.environ.pop("UGC_PROMPTS_DIR", None)
        ps = importlib.reload(self.ps)
        self.assertEqual(ps.load("axes.md"), ps.default_text("axes.md"))
        with self.assertRaises(ps.PromptError):
            ps.status()


class TestKbUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        kb = Path(self.tmp.name)
        (kb / "notes").mkdir()
        (kb / "distilled").mkdir()
        (kb / "distilled" / "GLOSSARY.md").write_text(
            "# GLOSSARY\n\n## A. 型\n\n**語A** — 説明\n\n## D. 界隈\n\n**界隈** — 説明" + "あ" * 200 + "\n\n## E. 形\n\n**語E** — 説明\n",
            encoding="utf-8")
        (kb / "distilled" / "cards.jsonl").write_text('{"file": "old.md", "title": "古い"}\n', encoding="utf-8")
        (kb / "INDEX.json").write_text(json.dumps([{"key": "nold1", "file": "old.md"}, {"key": "nold2", "file": "old2.md"}]),
                                       encoding="utf-8")
        os.environ["UGC_KB_DIR"] = str(kb)
        import kb_update
        self.kb = importlib.reload(kb_update)
        self.kbdir = kb

    def tearDown(self):
        os.environ.pop("UGC_KB_DIR", None)
        self.tmp.cleanup()

    def fake_fetch(self, url):
        if "contents" in url:
            page = int(url.rsplit("=", 1)[1])
            pages = {1: [{"key": "npinned", "isPinned": True, "publishAt": "2025-01-01"},
                         {"key": "nnew1", "noteUrl": "https://note.com/x/n/nnew1", "publishAt": "2026-10-01T10:00:00+09:00",
                          "name": "新しい記事"},
                         {"key": "nold1"}],
                     2: [{"key": "nold2"}, {"key": "../../evil"}], 3: [{"key": "never"}]}
            self.calls.append(page)
            return {"data": {"contents": pages[page], "isLastPage": page == 3}}
        return {"data": {"body": "<p>こんにちは</p><h2>見出し</h2><p>本文<br>二行目</p>", "name": "新しい記事",
                         "publish_at": "2026-10-01T10:00:00.000+09:00", "can_read": True}}

    def test_check_new_stops_at_known(self):
        self.calls = []
        res = self.kb.check_new(log=lambda m: None, fetch=self.fake_fetch, sleep=0)
        self.assertEqual(len(res["new"]), 2)
        self.assertEqual(self.calls, [1, 2])   # 2ページ目が全部知っている記事なので3ページ目は見ない
        files = sorted(x["file"] for x in self.kb.index())
        self.assertIn("2026-10-01_nnew1.md", files)
        self.assertTrue(any(f.endswith("_npinned.md") for f in files))   # 固定表示でも知らない記事は取り込む
        body = (self.kbdir / "notes" / "2026-10-01_nnew1.md").read_text(encoding="utf-8")
        self.assertIn("こんにちは\n見出し\n本文\n二行目", body)
        self.assertEqual(len(self.kb.state()["pending"]), 2)
        self.assertFalse(self.kb.due())

    def test_card_and_merge(self):
        self.calls = []
        self.kb.check_new(log=lambda m: None, fetch=self.fake_fetch, sleep=0)
        t = self.kb.next_task()
        self.assertEqual(t["type"], "kb_card")
        self.assertIn("こんにちは", self.kb.render(t))
        self.assertTrue(self.kb.accept(t["task_id"], '{"card": {}}'))
        card = {"title": "t", "kind": "コラム", "song": None, "artist": None, "platform": "TikTok", "buzz_type": None,
                "pathway": "p", "communities": [], "formats": [], "why_claims": [], "reproducible": [],
                "evidence_style": "e", "coined_terms": [], "numbers": "", "reusable_insight": "r"}
        big = [{"section": "D", "term": f"語{i}", "text": "い" * 290} for i in range(5)]
        self.assertEqual(self.kb.accept(t["task_id"], json.dumps({"card": card, "glossary": big}, ensure_ascii=False)), [])
        t1 = self.kb.next_task()
        self.assertEqual(t1["type"], "kb_card")   # 足した語はまだ MERGE_AT に届かないので、2本目の記事
        big2 = [{"section": "D", "term": f"語{i}", "text": "う" * 290} for i in range(5, 10)]
        self.assertEqual(self.kb.accept(t1["task_id"], json.dumps({"card": card, "glossary": big2}, ensure_ascii=False)), [])
        # 追記が MERGE_AT を超えたので、D 章の整理が次に来る
        t2 = self.kb.next_task()
        self.assertEqual((t2["type"], t2["section"]), ("kb_merge", "D"))
        self.assertIn("語0", self.kb.glossary_text())
        self.assertTrue(self.kb.accept(t2["task_id"], "## E. 違う章\n"))   # 見出しが違う
        new_d = "## D. 界隈\n\n**界隈** — 説明" + "あ" * 200 + "\n\n**語0** — まとめた\n"
        self.assertEqual(self.kb.accept(t2["task_id"], new_d), [])
        g = (self.kbdir / "distilled" / "GLOSSARY.md").read_text(encoding="utf-8")
        self.assertIn("**語0** — まとめた", g)
        self.assertIn("## E. 形", g)
        self.assertIn("## A. 型", g)
        self.assertNotIn("語1", self.kb.glossary_text())   # 整理した章の追記は消える
        self.assertIsNone(self.kb.next_task())   # 全部済んだ
        self.assertEqual(len((self.kbdir / "distilled" / "cards.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_html_to_text(self):
        self.assertEqual(self.kb.html_to_text("<p>a&amp;b</p><ul><li>x</li><li>y</li></ul>"), "a&b\n・x\n・y\n")


class TestClock(unittest.TestCase):
    def test_clock(self):
        import proto_runner as pr
        now = datetime.datetime(2026, 10, 3, 20, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        self.assertEqual(pr._clock(3600 * 2, now), "今日の22時ごろ")
        self.assertEqual(pr._clock(3600 * 10 + 1500, now), "明日（10/4）の6時半ごろ")
        self.assertEqual(pr._clock(3600 * 3 + 2700, now), "明日（10/4）の0時ごろ")
        self.assertEqual(pr._clock(3600 * 40, now), "10/5 の12時ごろ")

    def test_title_from_url(self):
        import proto_runner as pr
        self.assertEqual(pr._title_from_music_url("https://www.tiktok.com/music/mosi-mosi-Sped-up-7617134698027961106"),
                         "mosi mosi Sped up")


if __name__ == "__main__":
    unittest.main()
