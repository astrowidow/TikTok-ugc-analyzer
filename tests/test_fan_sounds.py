"""同じ曲のファンの音源を探して合わせて取る（2026-10-06）。TikTok に触らない。

  python -m unittest tests.test_fan_sounds

きゃわぽっぴんどぅーで、2番の「血液型とかMBTIとか」で自己紹介する波が、ファンが上げた12秒の音源（39K）に乗っていて、
公式の音源（31.1K・17.7K）だけを取ったレポートから丸ごと抜けた。
- 表記ゆれの語（kana_key・related_words）と、探す本体（search_fan_sounds）: TikTok の照合した曲の番号（MetaSongId）で同じ曲を見分ける
- 取得の段（Run.add_fan_sounds・step_resolve・release_urls）: 主の UGC の2割以上を足す。公開の時刻からは外す。探さない場合
- AI の入力（build_llm_input の音源の印・sounds.json、flow_w1.sound_lines・vline）
"""
import collections
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

from acquire import pipeline  # noqa: E402

META = "7644122555377502225"
U1 = "https://www.tiktok.com/music/きゃわぽっぴんどぅー-7644119804865808400"
U2 = "https://www.tiktok.com/music/きゃわぽっぴんどぅー-7643096893593897748"
FAN = "7643306519803906823"
NAGI = "7643423091901434642"


def mus(mid, title, author="だれか", meta=(META,), duration=12, desc="", words=()):
    return {"id": mid, "title": title, "author": author, "duration": duration, "meta_song_ids": list(meta),
            "desc": desc, "suggested_words": list(words)}


class TestWords(unittest.TestCase):
    def test_kana_key_ignores_spelling(self):
        k = pipeline.kana_key("きゃわぽっぴんどぅー")
        self.assertEqual(pipeline.kana_key("キャワポッピンドゥー"), k)
        self.assertEqual(pipeline.kana_key("きゃわほっぴんどぅ"), k)   # 半濁点と伸ばし棒の違い
        self.assertEqual(pipeline.kana_key("ＩＬｉＦＥ！"), "ilife")

    def test_related_words(self):
        c = collections.Counter({"きゃわぽっぴんどぅー": 9, "iLiFE! ダンス": 8, "ダンス": 7, "キャワポッピンドゥー": 5,
                                 "きゃわほっぴんどぅ": 6, "きゃわぽっぴんどぅー ダンス": 4, "きゃわぽっぴんどぅー  ダンス": 3,
                                 "アイライフ": 9})
        self.assertEqual(pipeline.related_words("きゃわぽっぴんどぅー", c, 3),
                         ["きゃわほっぴんどぅ", "キャワポッピンドゥー", "きゃわぽっぴんどぅー ダンス"])
        self.assertEqual(pipeline.related_words("きゃわぽっぴんどぅー", c, 0), [])

    def test_fits_song(self):
        self.assertTrue(pipeline.fits_song("きゃわぽっぴんどぅー", "iLiFE!", "きゃわぽっぴんどぅー", "iLiFE!【あいらいふ】"))
        self.assertFalse(pipeline.fits_song("きゃわぽっぴんどぅー", "iLiFE!", "オリジナル楽曲 - 쿠레아", "쿠레아"))


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.pages = {
            "きゃわぽっぴんどぅー": ["v1", "v2", "v3"],
            "きゃわほっぴんどぅ": ["v2", "v4", "v5"],
            "キャワポッピンドゥー": ["v6", "v7"],
        }
        self.music = {
            "v1": mus("7644119804865808400", "きゃわぽっぴんどぅー", "iLiFE!", duration=60, words=["きゃわほっぴんどぅ", "アイライフ"]),
            "v2": mus(NAGI, "オリジナル楽曲 - なぎ", duration=190, desc="#キャワポッピンドゥー #ilife"),
            "v3": mus("7000000000000000001", "オリジナル楽曲 - べつの曲", meta=("999",)),
            "v4": mus(FAN, "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚"),
            "v5": mus(FAN, "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚"),
            "v6": mus(FAN, "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚"),
            "v7": mus("7000000000000000002", "オリジナル楽曲 - 照合なし", meta=()),
        }
        self.read = []

    def music_of(self, u):
        self.read.append(u)
        return self.music.get(u) or {}

    def test_finds_fan_sound_through_spelling_pages(self):
        r = pipeline.search_fan_sounds("きゃわぽっぴんどぅー", {"7644119804865808400", "7643096893593897748"},
                                       lambda w: self.pages.get(w, []), self.music_of, 3)
        self.assertEqual(r["words"], ["きゃわぽっぴんどぅー", "きゃわほっぴんどぅ", "キャワポッピンドゥー"])
        self.assertEqual(r["meta_song_ids"], [META])
        self.assertEqual([(c["id"], c["hits"]) for c in r["candidates"]], [(FAN, 3), (NAGI, 1)])
        self.assertEqual(self.read.count("v2"), 1)   # 2つのページに出た動画は1回だけ読む
        # 曲の番号が違う音源・番号の無い音源は候補にしない
        self.assertNotIn("7000000000000000001", [c["id"] for c in r["candidates"]])
        self.assertNotIn("7000000000000000002", [c["id"] for c in r["candidates"]])

    def test_learns_song_id_from_page_when_discover_has_no_official(self):
        self.music["v1"] = mus(NAGI, "オリジナル楽曲 - なぎ")
        self.music["p1"] = mus("7644119804865808400", "きゃわぽっぴんどぅー", "iLiFE!", duration=60)
        r = pipeline.search_fan_sounds("きゃわぽっぴんどぅー", {"7644119804865808400"}, lambda w: self.pages.get(w, []),
                                       self.music_of, 3, page_links=lambda: ["p1"])
        self.assertEqual(r["meta_song_ids"], [META])
        self.assertIn(FAN, [c["id"] for c in r["candidates"]])

    def test_no_song_id_no_candidates(self):
        self.music["v1"] = mus("7644119804865808400", "きゃわぽっぴんどぅー", "iLiFE!", meta=())
        r = pipeline.search_fan_sounds("きゃわぽっぴんどぅー", {"7644119804865808400"}, lambda w: self.pages.get(w, []),
                                       self.music_of, 3)
        self.assertEqual(r["candidates"], [])


class FakeDriver:
    def __init__(self):
        self.url = None

    def get(self, url):
        self.url = url

    def execute_script(self, js):
        return []

    def quit(self):
        pass


class TestAddFanSounds(unittest.TestCase):
    def setUp(self):
        import tiktok_lock
        self.tmp = Path(tempfile.mkdtemp(prefix="t_fan_"))
        self.adir = self.tmp / "analyses"
        self.adir.mkdir()
        for target, attr, val in ((pipeline, "ANALYSES_DIR", self.adir), (tiktok_lock, "LOCK_DIR", self.tmp),
                                  (tiktok_lock, "LOCK_FILE", self.tmp / "tiktok.lock")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("UGC_COLLECTOR_NO_INSPECT", None)
        import scraper
        for target, attr, val in ((scraper, "create_headless_driver", FakeDriver), (scraper, "PAGE_LOAD_TIME", 0)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def make(self, settings=None, pages=True):
        aid = "a20261006-2000-fan1"
        d = self.adir / aid
        for sub in ("raw", "fetch_log", "derived", "outputs", "eval", "state"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        meta = {"analysis_id": aid, "title": "きゃわぽっぴんどぅー", "song": {"artist": "iLiFE!", "title": "きゃわぽっぴんどぅー"},
                "music_url": U1, "music_urls": [U1, U2], "acquisition": {"status": "running", "steps": {}}}
        if settings:
            meta["acquisition_settings"] = settings
        (d / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if pages:
            pipeline.write_json(d / "raw" / "music_pages.json", [
                {"url": U1, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!【あいらいふ】", "video_count": 31100, "page": 1},
                {"url": U2, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!【あいらいふ】", "video_count": 17700, "page": 2}])
        return aid

    def run_with(self, aid, cands, counts):
        found = {"words": ["きゃわぽっぴんどぅー", "きゃわほっぴんどぅ"], "videos": 30, "meta_song_ids": [META], "candidates": cands}

        def read_page(driver, wait=15):
            mid = pipeline.music_id_of(driver.url)
            return dict(counts.get(mid) or {})
        with mock.patch.object(pipeline, "search_fan_sounds", lambda *a, **k: found), \
                mock.patch.object(pipeline, "read_music_page", read_page):
            return pipeline.Run(aid).step_resolve()

    def test_joins_fan_sound_over_20_percent(self):
        aid = self.make()
        cands = [{"id": NAGI, "title": "オリジナル楽曲 - なぎ", "author": "なぎ", "duration": 190, "hits": 6, "videos": []},
                 {"id": FAN, "title": "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚", "author": "쿠레아.⋆𝜗𝜚", "duration": 12, "hits": 5, "videos": []},
                 {"id": "7000000000000000003", "title": "オリジナル楽曲 - ちいさい", "author": "x", "duration": 13, "hits": 3,
                  "videos": []},
                 {"id": "7000000000000000004", "title": "オリジナル楽曲 - 地域", "author": "y", "duration": 13, "hits": 2,
                  "videos": []}]
        counts = {NAGI: {"title": "オリジナル楽曲 - なぎ", "creator": "なぎ", "video_count_text": "2.1K 動画", "video_count": 2100},
                  FAN: {"title": "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚", "creator": "쿠레아.⋆𝜗𝜚", "video_count_text": "39K 動画",
                        "video_count": 39000},
                  "7000000000000000003": {"video_count": 100},
                  "7000000000000000004": {"video_count": 90000, "unavailable": True}}
        res = self.run_with(aid, cands, counts)
        self.assertEqual(res["how"], "given")
        m = json.loads((self.adir / aid / "analysis.json").read_text(encoding="utf-8"))
        fan_url = pipeline.music_url_of(FAN, "オリジナル楽曲 - 쿠레아.⋆𝜗𝜚")
        self.assertEqual(m["music_urls"], [U1, U2, fan_url])   # 2割（6,220）以上の1つだけ。使えないページは足さない
        self.assertEqual(m["fan_music_urls"], [fan_url])
        pages = pipeline.read_json(self.adir / aid / "raw" / "music_pages.json")
        self.assertEqual([p.get("kind") for p in pages], [None, None, "fan"])
        self.assertEqual((pages[2]["duration"], pages[2]["video_count"]), (12, 39000))
        log = pipeline.read_json(self.adir / aid / "raw" / "sound_search.json")
        self.assertEqual([c["joined"] for c in log["checked"]], [False, True, False, False])
        self.assertEqual(res["fan_sounds"]["added"], [fan_url])
        # 曲の公開の時刻はファンの音源を除いて決める
        run = pipeline.Run(aid)
        self.assertEqual(run.release_urls(), [U1, U2])
        self.assertEqual(run.music_urls(), [U1, U2, fan_url])

    def test_joins_all_over_20_percent_largest_first(self):
        """2割以上は数の上限なく全部、大きい順に（discover に出た回数の順ではない）。見つけた音源は全部 UGC 数を読む。
        2026-10-06 の実走の並び。ユーザー「こういう変な制限はしなくていいよ」"""
        aid = self.make()
        cands = [{"id": NAGI, "title": "オリジナル楽曲 - なぎ", "author": "なぎ", "duration": 190, "hits": 5, "videos": []},
                 {"id": FAN, "title": "オリジナル楽曲 - 쿠레아", "author": "쿠레아", "duration": 12, "hits": 2, "videos": []},
                 {"id": "7653437523162942215", "title": "オリジナル楽曲 - 片栗粉", "author": "片栗粉", "duration": 13, "hits": 1,
                  "videos": []}] + \
                [{"id": str(7000000000000000010 + i), "title": f"オリジナル楽曲 - {i}", "author": "z", "duration": 12, "hits": 1,
                  "videos": []} for i in range(6)]
        counts = {NAGI: {"video_count": 8563}, FAN: {"video_count": 39000}, "7653437523162942215": {"video_count": 6881},
                  **{str(7000000000000000010 + i): {"video_count": 1000} for i in range(6)}}
        self.run_with(aid, cands, counts)
        log = pipeline.read_json(self.adir / aid / "raw" / "sound_search.json")
        self.assertEqual(len(log["checked"]), 9)
        self.assertEqual(log["added"], [pipeline.music_url_of(FAN, "オリジナル楽曲 - 쿠레아"),
                                        pipeline.music_url_of(NAGI, "オリジナル楽曲 - なぎ"),
                                        pipeline.music_url_of("7653437523162942215", "オリジナル楽曲 - 片栗粉")])
        m = json.loads((self.adir / aid / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(m["music_urls"][2:], log["added"])
        self.assertEqual(len(m["fan_music_urls"]), 3)

    def test_reads_main_count_when_not_recorded(self):
        aid = self.make(pages=False)
        cands = [{"id": FAN, "title": "オリジナル楽曲 - 쿠레아", "author": "쿠레아", "duration": 12, "hits": 5, "videos": []}]
        counts = {"7644119804865808400": {"video_count": 31100}, FAN: {"video_count": 39000}}
        self.run_with(aid, cands, counts)
        m = json.loads((self.adir / aid / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(len(m["music_urls"]), 3)

    def test_not_searched(self):
        for settings, env in (({"fan_sounds": 0}, {}), (None, {"UGC_COLLECTOR_NO_INSPECT": "1"})):
            aid = self.make(settings)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(pipeline, "search_fan_sounds", side_effect=AssertionError("探さない")):
                res = pipeline.Run(aid).step_resolve()
            self.assertEqual(res["fan_sounds"], {"searched": False})

    def test_failure_does_not_stop_acquisition(self):
        aid = self.make()
        with mock.patch.object(pipeline, "search_fan_sounds", side_effect=RuntimeError("boom")):
            res = pipeline.Run(aid).step_resolve()
        self.assertEqual((res["how"], res["fan_sounds"]), ("given", {"error": "RuntimeError"}))
        m = json.loads((self.adir / aid / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(m["music_urls"], [U1, U2])


class TestLlmInput(unittest.TestCase):
    """AI の入力: 音源が2つ以上なら、動画ごとの音源の印（A・B・C）と音源ごとの要約"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t_fan_in_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fan_url = pipeline.music_url_of(FAN, "オリジナル楽曲 - 쿠레아")
        raw, der = self.tmp / "raw", self.tmp / "derived"
        raw.mkdir()
        der.mkdir()
        pipeline.write_json(raw / "music_pages.json", [
            {"url": U1, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!", "video_count_text": "31.1K 動画", "video_count": 31100},
            {"url": U2, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!【あいらいふ】", "video_count_text": "17.7K 動画"},
            {"url": fan_url, "title": "オリジナル楽曲 - 쿠레아", "creator": "쿠레아", "kind": "fan", "duration": 12,
             "video_count_text": "39K 動画", "video_count": 39000}])
        rows = [("7650000000000000001", 1, "7644119804865808400", 60, "2026-05-26", "2026-W22", 1000),
                ("7650000000000000002", 2, "7643096893593897748", 16, "2026-05-27", "2026-W22", 2000),
                ("7650000000000000003", 3, FAN, 12, "2026-06-23", "2026-W26", 500000),
                ("7650000000000000004", 3, FAN, 12, "2026-06-24", "2026-W26", 3000),
                ("7650000000000000005", 3, None, None, "2026-06-30", "2026-W27", 100)]   # 属性が取れなかった動画は見つけたページで
        with open(raw / "grid_links.jsonl", "w", encoding="utf-8") as g, open(raw / "enriched.jsonl", "w", encoding="utf-8") as e, \
                open(der / "videos.jsonl", "w", encoding="utf-8") as v:
            for i, (vid, src, mid, dur, date, week, plays) in enumerate(rows, 1):
                g.write(json.dumps({"video_id": vid, "source": src}) + "\n")
                if mid:
                    e.write(json.dumps({"video_id": vid, "music": {"id": mid, "duration": dur},
                                        "sticker_texts": ["血液型とかなんだとか"] if mid == FAN else []}, ensure_ascii=False) + "\n")
                v.write(json.dumps({"seq": i, "video_id": vid, "date": date, "week": week, "type": "Video", "plays": plays,
                                    "likes": 0, "comments": 0, "shares": 0, "username": f"u{i}", "desc": "", "hashtags": []}) + "\n")
        (der / "sample_taxonomy.md").write_text("", encoding="utf-8")
        (der / "sample_label.jsonl").write_text("", encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "build_llm_input.py"), str(der),
                            "--enriched", str(raw / "enriched.jsonl")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.der = der

    def test_sound_tags_and_summary(self):
        recs = [json.loads(ln) for ln in (self.der / "llm_input" / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["sound"] for r in recs], ["A", "B", "C", "C", "C"])
        ss = json.loads((self.der / "llm_input" / "sounds.json").read_text(encoding="utf-8"))
        self.assertEqual([(s["tag"], s["kind"], s["duration"]) for s in ss], [("A", "", 60), ("B", "", 16), ("C", "fan", 12)])

        import flow_w1

        class A:
            def derived(_self, *p):
                return self.der.joinpath(*p)
        lines = flow_w1.sound_lines(A(), detail=True)
        self.assertEqual(len(lines), 3)
        self.assertIn("音源C『オリジナル楽曲 - 쿠레아／쿠레아』（ファンが上げた同じ曲の音源、12秒）: UGC 39K。", lines[2])
        self.assertIn("本数のピーク週 2026-W26（2本）", lines[2])
        self.assertIn("再生の最多 seq 3（@u3、2026-06-23、再生 500,000、音源C）", lines[2])
        self.assertIn("血液型とかなんだとか（2）", lines[2])

    def test_single_page_has_no_tags(self):
        pipeline.write_json(self.tmp / "raw" / "music_pages.json", [{"url": U1, "title": "きゃわぽっぴんどぅー"}])
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "build_llm_input.py"), str(self.der),
                            "--enriched", str(self.tmp / "raw" / "enriched.jsonl")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = [json.loads(ln) for ln in (self.der / "llm_input" / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["sound"] for r in recs}, {None})
        self.assertFalse((self.der / "llm_input" / "sounds.json").exists())


if __name__ == "__main__":
    unittest.main()
