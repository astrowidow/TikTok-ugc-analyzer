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


class TestReaderWords(unittest.TestCase):
    """読者（Web の記事）に見せない言葉の検査（2026-10-03 ユーザー「用語集の通り」「今回集めた20本」）"""

    def test_reader_re(self):
        import flow_w1
        bad = ["用語集の通り、友人を召喚する口実", "因果パターンで言えば", "今回集めた動画は 20本です", "20本中11本が", "ラベル付きの動画"]
        good = ["UGC 数は約13万", "TikTok のラベルは Lip-sync", "友達を誘う口実として機能した", "再生は 410万"]
        for t in bad:
            self.assertTrue(flow_w1.READER_RE.search(t), t)
        for t in good:
            self.assertFalse(flow_w1.READER_RE.search(t), t)

    def test_parse_count(self):
        from acquire import pipeline
        self.assertEqual(pipeline.parse_count("131.9K 動画"), 131900)
        self.assertEqual(pipeline.parse_count("3.5万"), 35000)
        self.assertIsNone(pipeline.parse_count(""))


class TestCancelInApp(unittest.TestCase):
    """取得のやり直し: 止める印があれば、メニューバーのアプリがその分析を取っている係を止める"""

    def test_handle_cancels(self):
        import logging
        import subprocess
        tmp = tempfile.mkdtemp()
        os.environ["UGC_COLLECTOR_HOME"] = tmp
        sys.path.insert(0, str(ROOT / "collector"))
        try:
            import collector_app.config as config
            importlib.reload(config)
            config.setup_env()
            import collector_app.app as app
            importlib.reload(app)
            ctl = app.Controller(config.code_dir(), logging.getLogger("t"))
            proc = subprocess.Popen(["sleep", "60"], start_new_session=True)   # 取得の係の代わり
            ctl.worker.proc = proc
            aid = "a20991231-0000-test"
            d = config.ANALYSES_DIR / aid
            d.mkdir(parents=True)
            (d / "analysis.json").write_text(json.dumps({"analysis_id": aid, "title": "試験",
                                                         "acquisition": {"status": "running", "pid": proc.pid}}), encoding="utf-8")
            (config.LOCK_DIR / f"cancel-{aid}").write_text("1", encoding="utf-8")
            app.notify.send = lambda *a, **k: None
            ctl._handle_cancels()
            self.assertIsNotNone(proc.poll())   # 係が止まった
            m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(m["acquisition"]["status"], "cancelled")
            self.assertFalse((config.LOCK_DIR / f"cancel-{aid}").exists())
        finally:
            os.environ.pop("UGC_COLLECTOR_HOME", None)
            shutil.rmtree(tmp, ignore_errors=True)


class TestMultiPage(unittest.TestCase):
    """同じ曲の楽曲ページを合わせて取る（2026-10-04 ユーザー「20%でよい」）。TikTok には触らない"""
    U1 = "https://www.tiktok.com/music/a-7000000000000000101"
    U2 = "https://www.tiktok.com/music/a-sped-up-7000000000000000102"

    def setUp(self):
        from acquire import pipeline
        self.pl = pipeline
        self.tmp = tempfile.mkdtemp()
        self.old = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = Path(self.tmp)
        aid = "a20991231-0000-mult"
        self.d = d = Path(self.tmp) / aid
        for sub in ("raw", "derived", "fetch_log"):
            (d / sub).mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({"analysis_id": aid, "title": "t", "music_url": self.U1,
                                                     "music_urls": [self.U1, self.U2],
                                                     "acquisition": {"status": "running"}}), encoding="utf-8")
        with open(d / "raw" / "grid_links.jsonl", "w", encoding="utf-8") as f:
            for vid, src in (("101", 1), ("102", 1), ("201", 2)):
                f.write(json.dumps({"url": f"https://www.tiktok.com/@x/video/{vid}", "video_id": vid, "source": src}) + "\n")
        (d / "derived" / "pool.tsv").write_text("video_id\tweek\tpriority\treasons\tcap\n101\tw1\t1\tkey\t120\n201\tw1\t2\tweek\t40\n",
                                                encoding="utf-8")
        self.run_ = pipeline.Run(aid)

    def tearDown(self):
        self.pl.ANALYSES_DIR = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_music_urls_keeps_main_first(self):
        self.assertEqual(self.run_.music_urls(), [self.U1, self.U2])
        self.assertEqual(self.run_.link_sources(), {"101": 1, "102": 1, "201": 2})

    def test_comment_pages_split_pool(self):
        pages = self.run_.comment_pages()
        self.assertEqual([p[0] for p in pages], [self.U1, self.U2])
        self.assertEqual(pages[0][2].name, "comments_summary.json")   # 1ページ目は前と同じ名前
        self.assertEqual(pages[1][2].name, "comments_summary_p2.json")
        self.assertEqual(pages[1][3].name, "substitutions_p2.tsv")
        p1 = pages[0][1].read_text(encoding="utf-8").splitlines()
        p2 = pages[1][1].read_text(encoding="utf-8").splitlines()
        self.assertEqual((p1[0], len(p1), p1[1][:3]), ("video_id\tweek\tpriority\treasons\tcap", 2, "101"))
        self.assertEqual((len(p2), p2[1][:3]), (2, "201"))

    def test_comment_pages_single(self):
        m = json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))
        m["music_urls"] = [self.U1]
        (self.d / "analysis.json").write_text(json.dumps(m), encoding="utf-8")
        pages = self.run_.comment_pages()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][1].name, "pool.tsv")   # 1ページなら前と同じファイル

    def test_check_comments_merges_pages(self):
        fl = self.d / "fetch_log"
        (fl / "comments_summary.json").write_text(json.dumps({"rows": [{"status": "ok"}], "missing": ["9"],
                                                              "substitutions": [{"substitute": "102"}]}), encoding="utf-8")
        (fl / "comments_summary_p2.json").write_text(json.dumps({"rows": [{"status": "ok"}, {"status": "error"}],
                                                                 "missing": ["8", "7"]}), encoding="utf-8")
        with open(self.d / "raw" / "comments.jsonl", "w", encoding="utf-8") as f:
            for vid in ("101", "201"):
                f.write(json.dumps({"video_id": vid, "status": "ok", "comments": [{"aweme_id": vid}] * 3}) + "\n")
        self.assertEqual(self.pl.comments_ok(self.d), 2)
        res = self.run_._check_comments(False)
        self.assertEqual((res["videos_ok"], res["unreachable"], res["substituted"]), (2, 3, 1))
        self.assertEqual(res["videos_ok_by_page"], {"1": 1, "2": 1})

    def test_ugc_total_sums_pages(self):
        import types
        import flow_w1
        a = types.SimpleNamespace(dir=self.d)
        (self.d / "raw" / "music_pages.json").write_text(json.dumps([
            {"url": self.U1, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!", "video_count": 31200,
             "video_count_text": "31.2K 動画", "at": "2026-10-04T12:00:00+09:00", "links": 400},
            {"url": self.U2, "title": "きゃわぽっぴんどぅー (sped up)", "creator": "iLiFE!", "video_count": 17700,
             "video_count_text": "17.7K 動画", "at": "2026-10-04T12:05:00+09:00", "links": 150}]), encoding="utf-8")
        u = flow_w1.ugc_total(a)
        self.assertEqual((u["n"], u["text"], u["at"], len(u["parts"]), u["partial"]),
                         (48900, "31.2K＋17.7K", "2026-10-04", 2, False))
        self.assertIn("『きゃわぽっぴんどぅー (sped up)／iLiFE!』17.7K", flow_w1.ugc_parts_line(u))
        (self.d / "raw" / "music_pages.json").unlink()
        (self.d / "raw" / "music_page.json").write_text(json.dumps({"video_count": 1632, "video_count_text": "1632 動画",
                                                                    "at": "2026-10-04"}), encoding="utf-8")
        u = flow_w1.ugc_total(a)
        self.assertEqual((u["n"], u["parts"]), (1632, []))   # 1ページなら前と同じ


class TestPickPages(unittest.TestCase):
    """楽曲ページの選び方: 公式のページのうち一番使われているものを主に、その20%以上を合わせる。個人の音源は入れない"""

    def test_rule(self):
        import proto_runner as pr
        urls = [f"https://www.tiktok.com/music/x-{n}" for n in (1, 2, 3, 4)]
        infos = {urls[0]: ("きゃわぽっぴんどぅー", "iLiFE!", 1632), urls[1]: ("きゃわぽっぴんどぅー", "iLiFE!", 31200),
                 urls[2]: ("きゃわぽっぴんどぅー (sped up)", "iLiFE!", 17700),
                 urls[3]: ("オリジナル楽曲 - someone", "someone", 50000)}

        class Fake:
            def inspect_many(self, us):
                return [{"title": infos[u][0], "creator": infos[u][1], "video_count": infos[u][2],
                         "video_count_text": f"{infos[u][2]} 動画"} for u in us]
        old = pr.LOCAL
        pr.LOCAL = Fake()
        try:
            take, dropped = pr._pick_music_pages("きゃわぽっぴんどぅー", "iLiFE!", urls)
            self.assertEqual([u for u, _ in take], [urls[1], urls[2]])
            why = {u: w for u, _, w in dropped}
            self.assertIn("20%未満", why[urls[0]])
            self.assertIn("個人の音源", why[urls[3]])
            take, dropped = pr._pick_music_pages("きゃわぽっぴんどぅー", "iLiFE!", urls, take_all=True)
            self.assertEqual((len(take), dropped), (4, []))   # 利用者が渡したものは全部
        finally:
            pr.LOCAL = old


class TestListMultiPage(unittest.TestCase):
    """一覧の段を、偽のブラウザで楽曲ページ2つから回す（TikTok には触らない）"""

    def test_step_list(self):
        import types
        from acquire import pipeline
        tmp = tempfile.mkdtemp()
        old_dir = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = Path(tmp)
        U1, U2 = TestMultiPage.U1, TestMultiPage.U2
        grids = {U1: [f"https://www.tiktok.com/@x/video/{v}?q=1" for v in ("101", "102", "300")],
                 U2: [f"https://www.tiktok.com/@x/video/{v}" for v in ("300", "201")]}   # 300 は両方に出る
        heads = {U1: {"title": "曲", "creator": "歌手", "video_count_text": "31.2K 動画"},
                 U2: {"title": "曲 (sped up)", "creator": "歌手", "video_count_text": None}}   # 2つ目は読めない

        class Driver:
            url = None

            def get(self, u):
                self.url = u

            def execute_script(self, js):
                if js == pipeline.MUSIC_PAGE_JS:
                    return dict(heads[self.url])
                if js == "COUNT":
                    return len(grids[self.url])
                return list(grids[self.url])

            def find_element(self, *a):
                return types.SimpleNamespace(send_keys=lambda *k: None)

            def quit(self):
                pass
        fake = types.SimpleNamespace(create_headless_driver=Driver, PAGE_LOAD_TIME=0, SCROLL_PAUSE_TIME=0,
                                     _COUNT_LINKS_JS="COUNT")
        old_mod = sys.modules.get("scraper")
        sys.modules["scraper"] = fake
        old_wait = pipeline.read_music_page.__defaults__
        pipeline.read_music_page.__defaults__ = (0,)
        try:
            aid = "a20991231-0000-list"
            d = Path(tmp) / aid
            (d / "raw").mkdir(parents=True)
            (d / "analysis.json").write_text(json.dumps({"analysis_id": aid, "title": "t", "music_url": U1,
                                                         "music_urls": [U1, U2], "acquisition_settings":
                                                         {"list_sets": 2, "list_scrolls": 2, "list_stall": 1},
                                                         "acquisition": {"status": "running"}}), encoding="utf-8")
            # 受け付けのときに読んだ UGC 数（2つ目はここでしか読めていない）
            (d / "raw" / "music_pages.json").write_text(json.dumps([
                {"url": U1, "video_count": 31000, "video_count_text": "31K 動画", "how": "受け付け"},
                {"url": U2, "video_count": 17700, "video_count_text": "17.7K 動画", "how": "受け付け"}]), encoding="utf-8")
            run = pipeline.Run(aid)
            run.lock = lambda what: None
            res = run.step_list()
            links = [json.loads(x) for x in (d / "raw" / "grid_links.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(g["video_id"], g["source"]) for g in links],
                             [("101", 1), ("102", 1), ("300", 1), ("201", 2)])   # 重ねない・見つけたページを残す
            pages = json.loads((d / "raw" / "music_pages.json").read_text(encoding="utf-8"))
            self.assertEqual([(p["video_count"], p["links"], p["page"]) for p in pages], [(31200, 3, 1), (17700, 1, 2)])
            self.assertNotIn("how", pages[0])
            self.assertEqual(json.loads((d / "raw" / "music_page.json").read_text(encoding="utf-8"))["url"], U1)
            self.assertEqual((res["links"], res["ugc_total"], len(res["pages"])), (4, 48900, 2))
            self.assertEqual((d / "raw" / "grid_links.csv").read_text(encoding="utf-8").count("\n"), 5)
        finally:
            pipeline.read_music_page.__defaults__ = old_wait
            if old_mod is not None:
                sys.modules["scraper"] = old_mod
            else:
                sys.modules.pop("scraper", None)
            pipeline.ANALYSES_DIR = old_dir
            shutil.rmtree(tmp, ignore_errors=True)


class TestCommentsMultiPage(TestMultiPage):
    """コメントの段: 楽曲ページごとに、そのページのプールで、そのページのグリッドから取る（取得の係は偽物）"""

    def test_step_comments(self):
        from acquire import spatest
        calls = []
        d = self.d

        class Fake:
            def __init__(self, a):
                self.a, self.d, self.yielded, self.between_videos = a, None, 0.0, None
                pool = open(a.pool, encoding="utf-8").read().splitlines()[1:]
                self.rows = [{"video_id": r.split("\t")[0], "status": "ok"} for r in pool]

            def run(self):
                calls.append((self.a.music_url, Path(self.a.pool).name, Path(self.a.summary).name, Path(self.a.subs_out).name))
                with open(self.a.out, "a", encoding="utf-8") as f:
                    for r in self.rows:
                        f.write(json.dumps({**r, "comments": [{"aweme_id": r["video_id"]}]}) + "\n")
                Path(self.a.summary).write_text(json.dumps({"rows": self.rows}), encoding="utf-8")

            def log(self, m):
                pass
        old = (spatest.SpaCollector, self.pl.ensure_chrome)
        spatest.SpaCollector = Fake
        self.pl.ensure_chrome = lambda port, log: None
        (d / "derived" / "llm_input").mkdir(parents=True)
        try:
            self.run_.lock = lambda what: None
            m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
            m["acquisition"]["steps"] = {}
            (d / "analysis.json").write_text(json.dumps(m), encoding="utf-8")
            res = self.run_.step_comments()
        finally:
            spatest.SpaCollector, self.pl.ensure_chrome = old
        self.assertEqual(calls, [(self.U1, "pool_p1.tsv", "comments_summary.json", "substitutions.tsv"),
                                 (self.U2, "pool_p2.tsv", "comments_summary_p2.json", "substitutions_p2.tsv")])
        self.assertEqual((res["videos_ok"], res["videos_ok_by_page"]), (2, {"1": 1, "2": 1}))


class TestReleaseFilter(TestMultiPage):
    """曲の公開（楽曲ページが作られた時刻）より前の日付の投稿は、一覧の段で除く（2026-10-04 ユーザー「ノイズなので全ての分析から外す」）"""
    PAGE = "https://www.tiktok.com/music/x-7643096893593897748"   # 2026-05-23 に作られた楽曲ページ

    def test_id_time(self):
        from acquire import pipeline
        self.assertEqual(pipeline.iso_time(pipeline.id_time("7644119804865808400"))[:10], "2026-05-26")
        self.assertIsNone(pipeline.id_time("101"))   # 番号の形が違うものは読まない
        self.assertEqual(pipeline.iso_time(pipeline.release_time([self.PAGE, "https://www.tiktok.com/music/y-7644119804865808400"]))[:10],
                         "2026-05-23")

    def test_drop_before_release(self):
        seen = {f"u{v}": {"url": f"u{v}", "video_id": v} for v in ("7195615227549977857", "7646403619218099476")}
        n = self.run_.drop_before_release(seen, [self.PAGE])
        self.assertEqual((n, list(seen)), (1, ["u7646403619218099476"]))
        b = json.loads((self.d / "fetch_log" / "before_release.json").read_text(encoding="utf-8"))
        self.assertEqual((b["dropped"], b["videos"][0]["video_id"], b["release"][:10]), (1, "7195615227549977857", "2026-05-23"))
        import types
        import flow_w1
        self.assertEqual(flow_w1.release_info(types.SimpleNamespace(dir=self.d)), {"date": "2026-05-23", "dropped": 1})


class TestCommunityGuide(unittest.TestCase):
    """界隈の名付け方の手引き（2026-10-04 ユーザー「固定の2本でなく、蒸留して全レポート参照。名付け方と粒度だけを参考に」）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["UGC_KB_DIR"] = self.tmp
        dd = Path(self.tmp) / "distilled"
        dd.mkdir(parents=True)
        (dd / "COMMUNITY_GUIDE.md").write_text("# 界隈の名付け方の手引き\n\n## 1. 名付け方\n\n- 投稿者の種類で呼ぶ\n", encoding="utf-8")
        rows = [{"file": "a.md", "communities": [{"name": "歌い手", "signals": "歌ってみたを上げる個人"}]},
                {"file": "b.md", "communities": [{"name": "歌い手", "signals": "カバー投稿が主"}, {"name": "ダンス界隈", "signals": "踊ってみた"}]},
                {"file": "c.md", "communities": [{"name": "推し活層", "signals": "推しの切り抜き"}]}]
        (dd / "community_defs.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        import kb_update
        self.kb = importlib.reload(kb_update)

    def tearDown(self):
        os.environ.pop("UGC_KB_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_text(self):
        t = self.kb.community_guide_text()
        self.assertIn("## 1. 名付け方", t)
        self.assertIn("過去レポート 3本から", t)
        self.assertIn("**歌い手**（2本）", t)   # 2本以上で使われた名前
        self.assertIn("1本のレポートだけで使われた名前", t)
        self.assertLess(t.index("**歌い手**"), t.index("ダンス界隈"))
        self.assertIn("そのまま当てはめない", t)
        self.assertEqual(self.kb._community_key("Top TikToker界隈"), self.kb._community_key("TopTikToker"))   # 表記揺れ
        t2 = self.kb.community_guide_text(exclude_files={"c"})   # 分析する曲を扱った記事の例は外す
        self.assertNotIn("推し活層", t2)

    def test_card_check(self):
        base = {"card": {k: ([] if k in self.kb.CARD_LISTS else "x") for k in self.kb.CARD_KEYS if k not in ("file", "date")}}
        base["card"].update({"kind": self.kb.KINDS[0], "platform": self.kb.PLATFORMS[0]})
        self.assertEqual(self.kb.check_card({**base, "community_defs": [{"name": "歌い手", "signals": "歌ってみたの個人"}]}), [])
        errs = self.kb.check_card({**base, "community_defs": [{"name": "歌い手"}]})
        self.assertTrue(any("community_defs" in e for e in errs))

    def test_sync_shipped(self):
        sys.path.insert(0, str(ROOT / "collector"))
        from collector_app import config
        src = Path(self.tmp) / "ship"
        src.mkdir()
        (src / "COMMUNITY_GUIDE.md").write_text("# 新しい手引き\n", encoding="utf-8")
        (src / "community_defs.jsonl").write_text(json.dumps({"file": "a.md", "communities": []}) + "\n", encoding="utf-8")
        dst = Path(self.tmp) / "distilled"
        with open(dst / "community_defs.jsonl", "a", encoding="utf-8") as f:   # この Mac で新しい記事から足した分
            f.write(json.dumps({"file": "new.md", "communities": [{"name": "x", "signals": "y"}], "from": "kb_update"}) + "\n")
        done = config.sync_shipped_knowledge(src, dst)
        self.assertEqual(sorted(done), ["COMMUNITY_GUIDE.md", "community_defs.jsonl"])
        self.assertEqual((dst / "COMMUNITY_GUIDE.md").read_text(encoding="utf-8"), "# 新しい手引き\n")
        files = [json.loads(x)["file"] for x in (dst / "community_defs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(files, ["a.md", "new.md"])   # アプリの分＋この Mac で足した分
        self.assertEqual(config.sync_shipped_knowledge(src, dst), [])   # 2回目は何もしない


class TestCodexLink(unittest.TestCase):
    """「ChatGPT につなぐ」: Codex の設定ファイル（TOML）に道具の節を足す・置き換える・控え・スキル（2026-10-04）。
    ChatGPT にも本物の設定ファイルにも触らない（一時フォルダで）"""
    # 運営の Mac の ChatGPT が書いた設定ファイルの形（ChatGPT 自身の設定・同梱の道具・手で足した節・「常に許可」の記録）
    EXISTING = (
        'notify = ["/x/SkyComputerUseClient", "turn-ended"]\n\n[desktop]\nsansFontSize = 14\n\n'
        '[plugins."browser@openai-bundled"]\nenabled = true\n\n'
        '[mcp_servers.node_repl]\nargs = []\ncommand = "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"\n\n'
        '[mcp_servers.node_repl.env]\nCODEX_HOME = "/Users/x/.codex"\n\n'
        '# UGC Collector の道具（試し。2026-10-04）\n[mcp_servers.ugc-analyzer]\n'
        'command = "/Applications/UGC Collector.app/Contents/MacOS/UGC Collector"\nargs = ["--mcp"]\ntool_timeout_sec = 180\n\n'
        '[mcp_servers.ugc-analyzer.tools.start_analysis]\napproval_mode = "approve"\n\n'
        '[mcp_servers."ugc-analyzer".tools.cancel_analysis]\napproval_mode = "approve"\n\n'
        '[features]\nfoo = true\n')

    def setUp(self):
        import tomllib
        self.toml = tomllib
        self.tmp = tempfile.mkdtemp()
        os.environ["UGC_CODEX_HOME"] = str(Path(self.tmp) / "codex")
        os.environ["UGC_CODEX_SKILLS"] = str(Path(self.tmp) / "skills")
        sys.path.insert(0, str(ROOT / "collector"))
        from collector_app import codex_link
        self.cl = importlib.reload(codex_link)

    def tearDown(self):
        os.environ.pop("UGC_CODEX_HOME", None)
        os.environ.pop("UGC_CODEX_SKILLS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self) -> dict:
        return self.toml.loads(self.cl.CONFIG.read_text(encoding="utf-8"))

    def test_connect_keeps_others(self):
        cl = self.cl
        cl.CONFIG.parent.mkdir(parents=True)
        cl.CONFIG.write_text(self.EXISTING, encoding="utf-8")
        os.chmod(cl.CONFIG, 0o600)
        self.assertEqual(cl.status(), "outdated")   # 前の場所のアプリ（手で足した試しの節）・スキルが無い
        backup = cl.connect()
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), self.EXISTING)   # 控えは元のまま
        d = self._cfg()
        self.assertEqual(d["desktop"], {"sansFontSize": 14})
        self.assertEqual(d["features"], {"foo": True})
        self.assertEqual(d["mcp_servers"]["node_repl"]["env"], {"CODEX_HOME": "/Users/x/.codex"})
        self.assertTrue(d["plugins"]["browser@openai-bundled"]["enabled"])
        u = d["mcp_servers"]["ugc-analyzer"]
        self.assertEqual((u["command"], u["args"]), (cl.entry()["command"], cl.entry()["args"]))
        self.assertEqual((u["startup_timeout_sec"], u["tool_timeout_sec"]), (60, 300))
        self.assertEqual(sorted(u["tools"]), sorted(cl.TOOLS))   # 道具ごとの許可（聞かれないように）
        self.assertTrue(all(v == {"approval_mode": "approve"} for v in u["tools"].values()))
        text = cl.CONFIG.read_text(encoding="utf-8")
        self.assertEqual(text.count(cl.MARK), 1)
        self.assertNotIn("試し。2026-10-04", text.split(cl.MARK)[1])   # 前の節は置き換えた（目印より後ろに残らない）
        self.assertEqual(cl.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertIn("name: ugc-analyzer", (cl.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((cl.SKILL_DIR / "agents" / "openai.yaml").exists())
        self.assertEqual(cl.status(), "connected")
        cl.connect()   # 2回押しても同じ
        self.assertEqual(cl.CONFIG.read_text(encoding="utf-8"), text)

    def test_new_file_and_states(self):
        cl = self.cl
        self.assertEqual(cl.status(), "none")
        self.assertEqual(cl.connect(), "")   # 元の設定ファイルが無い（ChatGPT をまだ開いていない Mac）
        self.assertEqual(cl.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cl.status(), "connected")
        (cl.SKILL_DIR / "SKILL.md").write_text("古いスキル", encoding="utf-8")
        self.assertEqual(cl.status(), "outdated")   # アプリを新しくしたら、起動のときに書き直す
        cl.connect()
        cl.CONFIG.write_text(cl.CONFIG.read_text(encoding="utf-8").replace(
            "startup_timeout_sec", "enabled = false\nstartup_timeout_sec"), encoding="utf-8")
        self.assertEqual(cl.status(), "disabled")   # ChatGPT の設定で切られている（勝手には戻さない）
        cl.disconnect()
        self.assertEqual(cl.status(), "none")
        self.assertFalse(cl.SKILL_DIR.exists())

    def test_broken_file_untouched(self):
        cl = self.cl
        cl.CONFIG.parent.mkdir(parents=True)
        cl.CONFIG.write_text("[desktop\nx = 1\n", encoding="utf-8")
        self.assertEqual(cl.status(), "none")
        with self.assertRaises(cl.LinkError):
            cl.connect()
        self.assertEqual(cl.CONFIG.read_text(encoding="utf-8"), "[desktop\nx = 1\n")
        self.assertFalse(cl.SKILL_DIR.exists())

    def test_disconnect_keeps_others(self):
        cl = self.cl
        cl.CONFIG.parent.mkdir(parents=True)
        cl.CONFIG.write_text(self.EXISTING, encoding="utf-8")
        cl.connect()
        cl.disconnect()
        d = self._cfg()
        self.assertNotIn("ugc-analyzer", d["mcp_servers"])
        self.assertEqual(d["features"], {"foo": True})
        self.assertIn("node_repl", d["mcp_servers"])

    def test_frozen_entry(self):
        cl = self.cl
        cl.config.FROZEN = True
        try:
            block = cl._block()
            self.assertNotIn(".env]", block)   # アプリにしたときは環境変数を書かない
            self.assertIn('args = ["--mcp"]', block)
        finally:
            cl.config.FROZEN = False

    def test_ai_where(self):
        os.environ["UGC_COLLECTOR_HOME"] = self.tmp
        try:
            from collector_app import app
            orig = (app.claude_link.status, app.codex_link.status)
            try:
                for c, g, want in [("connected", "none", "Claude"), ("none", "connected", "ChatGPT の Work"),
                                   ("connected", "connected", "Claude か ChatGPT の Work"), ("none", "outdated", "")]:
                    app.claude_link.status, app.codex_link.status = (lambda c=c: c), (lambda g=g: g)
                    self.assertEqual(app.ai_where(), want)
            finally:
                app.claude_link.status, app.codex_link.status = orig
        finally:
            os.environ.pop("UGC_COLLECTOR_HOME", None)
