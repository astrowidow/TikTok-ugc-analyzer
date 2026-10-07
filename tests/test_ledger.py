"""一覧の段の台帳と、詳しく読む動画の選び方（2026-10-07）。TikTok に触らない。

  python -m unittest tests.test_ledger

楽曲ページのグリッドが読み込む一覧データ（/api/music/item_list/）に、再生数・投稿日時・投稿者・画面の文字まで入っている。
一覧の段で全部拾って台帳（raw/ledger.jsonl）にし、詳しく読む（1本ずつ開く）のはそこから選んだ動画だけにする。
- 枠（read_quotas）とスクロールの回数（ledger_scrolls）: ユーザーの決めた数（普段500を台帳の再生の合計の割合で配る・最低50・絶対の上限1,000）
- 選び方（choose_to_read）: 枠は再生順だけ。本人は全部・最初期は枠の5分の1を枠の外で
- 一覧の段（Run.step_list）を偽のブラウザで: 台帳と選んだ一覧、楽曲ページごとの本数
- 集計（prep_sample の --ledger）と AI の材料（flow_w1.sound_lines）は台帳で数える
"""
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

from acquire import pipeline  # noqa: E402

S = pipeline.DEFAULTS
MID1, MID2 = "7644119804865808400", "7643306519803906823"   # きゃわの公式60秒・ファンの音源 쿠레아
U1 = f"https://www.tiktok.com/music/きゃわぽっぴんどぅー-{MID1}"
U2 = f"https://www.tiktok.com/music/オリジナル楽曲-{MID2}"


def ts(day: str) -> int:
    return int(datetime.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())


def vid_at(day: str, n: int) -> str:
    """その日に作られた動画の番号（上の32ビットが時刻）"""
    return str((ts(day) << 32) + n)


def item(vid, day, plays, uid="fan", nick="ふぁん", followers=100, stickers=(), verified=False, duration=15):
    """LEDGER_HOOK_JS が取っておく形"""
    return {"id": vid, "createTime": ts(day), "desc": f"#きゃわ {vid[-3:]}", "isAd": False, "photo": False,
            "stats": {"playCount": str(plays), "diggCount": "10", "commentCount": "1", "shareCount": "0", "collectCount": "0"},
            "author": {"uniqueId": uid, "nickname": nick, "verified": verified, "signature": ""},
            "authorStats": {"followerCount": followers, "videoCount": 10},
            "music": {"id": MID1, "title": "きゃわぽっぴんどぅー", "authorName": "iLiFE!", "original": False, "duration": 60},
            "duration": duration, "hashtags": ["きゃわ"], "challenges": ["きゃわ"], "stickers": list(stickers)}


def rows_of(items_by_page):
    out, order = [], 0
    for k, its in enumerate(items_by_page, 1):
        for it in its:
            out.append(pipeline.ledger_row(it["id"], None, k, order, it))
            order += 1
    return out


class TestQuota(unittest.TestCase):
    def test_read_quotas(self):
        """2026-10-07 ユーザー「最低50本。通常は500本。絶対の上限として1000本」「4万本の音源と1万本の音源で同じ数は変」→ 台帳の再生の合計の割合で配る"""
        self.assertEqual(pipeline.read_quotas([750, 629], S), [272, 228])                      # シルエット（2020年・2025年の公式）
        self.assertEqual(pipeline.read_quotas([192, 246, 122, 75, 15], S), [148, 189, 94, 58, 50])   # きゃわの5音源（百万の単位。片栗粉は最低の50）
        self.assertEqual(pipeline.read_quotas([5], S), [500])
        self.assertEqual(pipeline.read_quotas([0, 0], S), [250, 250], "再生の合計が分からなければ均等")
        self.assertEqual(sum(pipeline.read_quotas([1] * 20, S)), 1000)
        self.assertEqual(pipeline.read_quotas([1] * 30, S), [33] * 30)
        self.assertEqual(pipeline.read_quotas([1] * 100, S), [10] * 100)
        q = pipeline.read_quotas([100] + [1] * 24, S)   # 大きい音源1つと小さい音源24個: 最低50×25＝1,250 > 1,000 → 最低は 40
        self.assertLessEqual(sum(q), 1000)
        self.assertEqual(min(q), 40)
        self.assertEqual({k: pipeline.read_quota(k, S) for k in (1, 2, 5, 10, 20, 30, 100)},
                         {1: 500, 2: 250, 5: 100, 10: 50, 20: 50, 30: 33, 100: 10}, "見込み用の均等の枠")

    def test_ledger_scrolls(self):
        """普段は1音源60回。合計300回を超えるなら割る（最低10回）"""
        got = {k: pipeline.ledger_scrolls(k, S) for k in (1, 5, 6, 10, 30, 100)}
        self.assertEqual(got, {1: 60, 5: 60, 6: 50, 10: 30, 30: 10, 100: 10})


class TestChoose(unittest.TestCase):
    def test_single_sound(self):
        """1音源（枠500）: 枠は再生の多い順だけ。本人は全部・最初期は枠の5分の1（100本）を枠の外で。週の一番は無い"""
        its = [item(vid_at("2026-05-27", 1), "2026-05-27", 300, uid="early1"),            # 最初期（小さい。音源ができたのは 5/26）
               item(vid_at("2026-05-28", 2), "2026-05-28", 200, uid="ilife_official", nick="iLiFE!【あいらいふ】",
                    followers=579400, verified=True),                                        # 本人（小さい）
               item(vid_at("2026-05-20", 3), "2026-05-20", 100, uid="before")]               # 音源ができる前（最初期にしない）
        n = 4
        for day in range(1, 29):   # 6月: 毎日 40本（再生 1,000〜600万）
            d = f"2026-06-{day:02d}"
            for j in range(40):
                plays = [6_000_000, 800_000, 520_000][j] if j < 3 else 1000 + j * 997
                its.append(item(vid_at(d, n), d, plays))
                n += 1
        quiet = vid_at("2026-08-20", 9999)
        its.append(item(quiet, "2026-08-20", 15_000))   # 静かな週の一番（1.5万再生）。週の一番はやめた
        rows = rows_of([its])
        why = pipeline.choose_to_read(rows, [U1], S, "iLiFE!")
        c = __import__("collections").Counter(why.values())
        self.assertEqual((c["本人"], c["最初期"], c["再生順"]), (1, 100, 500))
        self.assertEqual(why[its[0]["id"]], "最初期")
        self.assertEqual(why[its[1]["id"]], "本人")
        self.assertNotIn(its[2]["id"], why)
        self.assertNotIn(quiet, why)
        line = min(r["plays"] for r in rows if why.get(r["video_id"]) == "再生順")
        self.assertTrue(all(r["video_id"] in why for r in rows if r["plays"] > line), "枠は再生の多い順")

    def test_quota_follows_plays(self):
        """大きい音源ほど枠が大きい。小さい音源も最低の50本は読む"""
        pages = []
        for k in range(5):
            its = []
            for j in range(300):
                day = f"2026-07-{1 + j % 28:02d}"
                plays = (600_000 + j) if k == 0 else (5_000 + j * (10 if k == 4 else 1000))
                its.append(item(vid_at(day, k * 1000 + j), day, plays))
            pages.append(its)
        urls = [f"https://www.tiktok.com/music/x-{MID1[:-1]}{k}" for k in range(5)]
        rows = rows_of(pages)
        why = pipeline.choose_to_read(rows, urls, S, "iLiFE!")
        per = [sum(1 for r in rows if r["source"] == k and why.get(r["video_id"]) == "再生順") for k in range(1, 6)]
        q = pipeline.read_quotas([sum(r["plays"] for r in rows if r["source"] == k) for k in range(1, 6)], S)
        early0 = sum(1 for r in rows if r["source"] == 1 and why.get(r["video_id"]) == "最初期")
        self.assertEqual(per[1:], q[1:])
        self.assertEqual(per[0], min(q[0], 300 - early0), "台帳が枠より小さければ、最初期を除いた残り全部")
        self.assertGreater(q[0], q[1])
        self.assertEqual(q[4], 50)

    def test_cap_1000(self):
        """音源100（枠10・最初期2）でも合計は1,000本まで。超えたら再生順の再生の少ないほうから外す（最初期は残す）"""
        pages = []
        for k in range(100):
            pages.append([item(vid_at("2026-07-01", k * 100 + j), "2026-07-01", 900_000 + j) for j in range(15)])
        urls = [f"https://www.tiktok.com/music/x-{MID1[:-3]}{k:03d}" for k in range(100)]
        why = pipeline.choose_to_read(rows_of(pages), urls, S, "")
        c = __import__("collections").Counter(why.values())
        self.assertEqual(len(why), 1000)
        self.assertEqual(c["最初期"], 200)

    def test_without_plays_or_select_off_reads_all(self):
        """一覧データが拾えなかったページ（再生数が無い）と、read_select 0 は前と同じく全部読む"""
        rows = [pipeline.ledger_row(vid_at("2026-07-01", j), f"https://www.tiktok.com/@a/video/{j}", 1, j) for j in range(700)]
        self.assertEqual(len(pipeline.choose_to_read(rows, [U1], S, "")), 700)
        rows2 = rows_of([[item(vid_at("2026-07-01", j), "2026-07-01", 100) for j in range(700)]])
        self.assertEqual(len(pipeline.choose_to_read(rows2, [U1], {**S, "read_select": 0}, "")), 700)


class TestStepList(unittest.TestCase):
    """一覧の段を、偽のブラウザ（一覧データの仕掛けに応える）で楽曲ページ2つから回す"""

    def test_ledger_and_selected_links(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        old_dir = pipeline.ANALYSES_DIR
        pipeline.ANALYSES_DIR = tmp
        self.addCleanup(setattr, pipeline, "ANALYSES_DIR", old_dir)
        api = {U1: [item(vid_at("2026-06-01", j), "2026-06-01", 700_000 if j < 3 else 2000 + j) for j in range(400)],
               U2: [item(vid_at("2026-07-01", 5000 + j), "2026-07-01", 3000 + j, stickers=["血液型とかなんだとか"]) for j in range(300)]}
        api[U1].append(item(vid_at("2026-04-01", 7), "2026-04-01", 9_000_000))   # 曲の公開より前（除く）
        grid = {u: [f"https://www.tiktok.com/@fan/video/{it['id']}?lang=ja" for it in its[:-20]] for u, its in api.items()}
        heads = {U1: {"title": "きゃわぽっぴんどぅー", "creator": "iLiFE!", "video_count_text": "31.1K 動画"},
                 U2: {"title": "オリジナル楽曲 - 쿠레아", "creator": "쿠레아", "video_count_text": "39K 動画"}}

        class Driver:
            url = None
            hooked = False

            def execute_cdp_cmd(self, cmd, args):
                assert cmd == "Page.addScriptToEvaluateOnNewDocument" and "item_list" in args["source"]
                Driver.hooked = True

            def get(self, u):
                self.url = u

            def execute_script(self, js):
                if js == pipeline.MUSIC_PAGE_JS:
                    return dict(heads[self.url])
                if js == "COUNT":
                    return len(grid[self.url])
                if js == pipeline.LEDGER_READ_JS:
                    return list(api[self.url]) if Driver.hooked else []
                if js == pipeline.PAGE_TEXT_JS:
                    return ""
                return list(grid[self.url])

            def find_element(self, *a):
                return types.SimpleNamespace(send_keys=lambda *k: None)

            def quit(self):
                pass
        fake = types.SimpleNamespace(create_headless_driver=Driver, PAGE_LOAD_TIME=0, SCROLL_PAUSE_TIME=0, _COUNT_LINKS_JS="COUNT")
        old_mod = sys.modules.get("scraper")
        sys.modules["scraper"] = fake
        self.addCleanup(lambda: sys.modules.__setitem__("scraper", old_mod) if old_mod else sys.modules.pop("scraper", None))
        old_wait = pipeline.read_music_page.__defaults__
        pipeline.read_music_page.__defaults__ = (0,)
        self.addCleanup(setattr, pipeline.read_music_page, "__defaults__", old_wait)
        aid = "a20991231-0000-ledg"
        d = tmp / aid
        (d / "raw").mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({
            "analysis_id": aid, "title": "t", "music_url": U1, "music_urls": [U1, U2], "fan_music_urls": [U2],
            "song": {"title": "きゃわぽっぴんどぅー", "artist": "iLiFE!"}, "acquisition_settings": {"list_stall": 1},
            "acquisition": {"status": "running"}}), encoding="utf-8")
        run = pipeline.Run(aid)
        run.lock = lambda what: None
        res = run.step_list()
        ledger = [json.loads(x) for x in (d / "raw" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
        links = [json.loads(x) for x in (d / "raw" / "grid_links.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(ledger), 700, "グリッドに出る前に止めた分（各20本）も一覧データから入る。公開より前の1本は除く")
        self.assertTrue(all(r["api"] and r["plays"] is not None for r in ledger))
        self.assertEqual(sum(1 for r in ledger if r["read"]), len(links))
        q = pipeline.read_quotas([sum(r["plays"] for r in ledger if r["source"] == k) for k in (1, 2)], S)
        self.assertEqual(q, [379, 121], "台帳の再生の合計（約297万・約94万）の割合")
        # 1ページ目は台帳400本が「最初期76＋再生順379」より少ないので全部。2ページ目は 24＋121
        self.assertEqual([sum(1 for g in links if g["source"] == k) for k in (1, 2)], [400, 145])
        self.assertEqual(sum(1 for g in links if (g["plays"] or 0) >= 500_000), 3)
        self.assertEqual({g["why"] for g in links}, {"最初期", "再生順"})
        self.assertTrue(all("?" not in g["url"] for g in links))
        self.assertEqual(ledger[0]["create_time"], "2026-06-01 12:00:00")
        self.assertEqual(ledger[-1]["sticker_texts"], ["血液型とかなんだとか"])
        pages = json.loads((d / "raw" / "music_pages.json").read_text(encoding="utf-8"))
        self.assertEqual([(p["ledger"], p["links"], p["quota"]) for p in pages], [(400, 400, 379), (300, 145, 121)])
        self.assertEqual((res["links"], res["ledger"], res["quota"], res["dropped_before_release"]), (545, 700, [379, 121], 1))
        self.assertEqual((d / "raw" / "grid_links.csv").read_text(encoding="utf-8").count("\n"), 546)


class TestDeriveFromLedger(unittest.TestCase):
    """集計（prep_sample の --ledger）と AI の材料（flow_w1）は、台帳の全動画で数える"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t_ledger_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        raw, der = self.tmp / "raw", self.tmp / "derived"
        raw.mkdir()
        der.mkdir()
        rows = rows_of([[item(vid_at("2026-06-01", j), "2026-06-01", 1000 + j) for j in range(30)],
                        [item(vid_at("2026-07-06", 100 + j), "2026-07-06", 500 + j, stickers=["血液型とかなんだとか"])
                         for j in range(50)]])
        with open(raw / "ledger.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({**r, "read": "再生順" if r["order"] % 10 == 0 else None}, ensure_ascii=False) + "\n")
        read = [r for r in rows if r["order"] % 10 == 0]
        with open(der / "list.csv", "w", encoding="utf-8") as f:
            f.write("Index,URL,Created Date,Description,Likes,Shares,Comments,Plays,Saves,Reposts,Type,Username,Project Name\n")
            for i, r in enumerate(read):
                f.write(f"{i},{r['url']},{r['create_time']},,1,0,0,{r['plays']},0,0,Video,fan,t\n")
        self.raw, self.der, self.read = raw, der, read

    def run_prep(self, *extra):
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "prep_sample.py"), str(self.der / "list.csv"), str(self.der),
                            *extra], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_weekly_from_ledger(self):
        out = self.run_prep("--ledger", str(self.raw / "ledger.jsonl"))
        self.assertIn("rows=8 ledger=80", out)
        weekly = (self.der / "weekly.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(weekly[1:], ["2026-W23\t30\t" + str(sum(1000 + j for j in range(30))),
                                      "2026-W28\t50\t" + str(sum(500 + j for j in range(50)))])
        by = (self.der / "weekly_sounds.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual([ln.split("\t")[:3] for ln in by[1:]], [["2026-W23", "1", "30"], ["2026-W28", "2", "50"]])
        self.assertIn("詳しく読んだ8本（楽曲ページのグリッドで見た80本から選んだもの）",
                      (self.der / "sample_taxonomy.md").read_text(encoding="utf-8"))
        n = sum(1 for _ in open(self.der / "videos.jsonl", encoding="utf-8"))
        self.assertEqual(n, 8, "AI に渡す一覧は詳しく読んだ動画だけ")

    def test_without_ledger_same_as_before(self):
        self.run_prep()
        weekly = (self.der / "weekly.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual([ln.split("\t")[1] for ln in weekly[1:]], ["3", "5"])
        self.assertFalse((self.der / "weekly_sounds.tsv").exists())

    def test_sound_lines_count_ledger(self):
        import flow_w1
        self.run_prep("--ledger", str(self.raw / "ledger.jsonl"))
        pipeline.write_json(self.raw / "music_pages.json", [
            {"url": U1, "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!", "video_count_text": "31.1K 動画"},
            {"url": U2, "title": "オリジナル楽曲 - 쿠레아", "creator": "쿠레아", "kind": "fan", "video_count_text": "39K 動画"}])
        with open(self.raw / "grid_links.jsonl", "w", encoding="utf-8") as g, open(self.raw / "enriched.jsonl", "w", encoding="utf-8") as e:
            for r in self.read:
                g.write(json.dumps({"video_id": r["video_id"], "source": r["source"]}) + "\n")
                e.write(json.dumps({"video_id": r["video_id"], "music": {"id": MID1 if r["source"] == 1 else MID2}}) + "\n")
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "build_llm_input.py"), str(self.der),
                            "--enriched", str(self.raw / "enriched.jsonl")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        tmp = self.tmp

        class A:
            dir = tmp

            def derived(_self, *p):
                return tmp.joinpath("derived", *p)
        lines = flow_w1.sound_lines(A(), detail=True)
        self.assertIn("グリッドで見た投稿 50本（うち詳しく読んだ 5本）", lines[1])
        self.assertIn("本数のピーク週 2026-W28（50本）", lines[1])
        self.assertIn("血液型とかなんだとか（50）", lines[1], "画面の文字は台帳の全動画で数える")


if __name__ == "__main__":
    unittest.main()
