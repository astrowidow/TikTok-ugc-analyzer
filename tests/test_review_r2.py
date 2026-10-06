"""通し試験の読み合わせ（領域 R2: どの動画のコメントを何件取るか・コメントの取り方）で足した試験。

  collector/.venv/bin/python -m unittest tests.test_review_r2 -v

TikTok・Chrome・ネットワーク・本物の置き場（~/Library/Application Support/UGC Analyzer）には触らない。
spatest のブラウザに触る部分は、ページの代わり（FakePage）と時計の代わり（Clock）で動かす。

正しい動きを期待する形で書いてある。失敗する試験は、見つけた不具合を確かめるもの（docstring の【不具合】）。
- analysis/pool.py: 仕様の表（docs/COMMENT_TARGETS.md 第2・3章）どおりに選ぶか・境目（同じ再生数・同じ日付）・本人の判定・属性の山の上位の足し方
- analysis/attr_cluster.py: 山ごとの再生上位の数え方
- acquire/spatest.py: 差し替え・先読みの仕分け（aweme_id）・has_more・1ページで止める判定・要求の間隔（平均と60秒の上限）・止まったときの落とし方
"""
import datetime
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))
# pipeline・tiktok_lock は読み込むだけ（Run は作らず、ロックも取らない。置き場には書かない）

import attr_cluster  # noqa: E402
import pool  # noqa: E402
from acquire import pipeline, spatest  # noqa: E402

TITLE = "SongX"
ARTIST = "Zzartist"


# ---------------------------------------------------------------------------
# pool.py の入力（videos.jsonl と enriched.jsonl の形）を作る
def spec(vid, date, plays, uid=None, nick=None, verified=False, tags=(), loc="JP", label="Dance", followers=1000, bio=""):
    return {"video_id": str(vid), "date": date, "plays": plays, "uid": uid or f"user{vid}", "nick": nick,
            "verified": verified, "tags": list(tags), "loc": loc, "label": label, "followers": followers, "bio": bio}


def dataset(specs, music_author=ARTIST, title=TITLE):
    """prep_sample.py と同じ並び（日付・再生の多い順）で seq を振る"""
    rows = sorted(specs, key=lambda s: (s["date"], -s["plays"]))
    videos, enriched = [], {}
    for i, s in enumerate(rows):
        y, m, d = map(int, s["date"].split("-"))
        iso = datetime.date(y, m, d).isocalendar()
        videos.append({"video_id": s["video_id"], "date": s["date"], "week": f"{iso[0]}-W{iso[1]:02d}",
                       "plays": s["plays"], "seq": i, "username": s["uid"]})
        enriched[s["video_id"]] = {
            "video_id": s["video_id"],
            "author": {"unique_id": s["uid"], "nickname": s["nick"] or s["uid"], "verified": s["verified"],
                       "follower_count": s["followers"], "signature": s["bio"]},
            "music": {"id": "m1", "title": title, "author": music_author},
            "challenges": [], "hashtags": s["tags"], "location_created": s["loc"], "diversification_labels": [s["label"]]}
    return videos, enriched


def day(i, base=datetime.date(2026, 1, 5)):
    return (base + datetime.timedelta(days=i)).isoformat()


def must_of(p):
    return {v for v, x in p.items() if any(not r.startswith("week:") for r in x["reasons"])}


def with_reason(p, reason):
    return {v for v, x in p.items() if reason in x["reasons"]}


# ---------------------------------------------------------------------------
class TestPoolSpecTable(unittest.TestCase):
    """docs/COMMENT_TARGETS.md 第2・3章の表とコードが合っているか（作った90本で、答えをコードと別に数える）"""

    def setUp(self):
        specs = []
        for i in range(90):
            vid = 7_100_000_000_000_000_000 + i
            plays = ((i * 37) % 90) * 20_000 + 1_000 + i          # どれも違う再生数
            uid = "zzartist_official" if i in (40, 60, 80) else None
            nick = "Zzartist" if uid else None
            tags = []
            if i == 40:
                tags = ["songxchallenge", "songx"]                   # 本人が使った「曲名入りのタグ」と曲名そのもの
            elif i in (7, 21, 55):
                tags = ["songxchallenge"]
            elif i == 30:
                tags = ["songx"]                                     # 曲名そのものは公式企画のタグではない
            specs.append(spec(vid, day(i), plays, uid=uid, nick=nick, verified=(i % 3 == 0), tags=tags,
                              loc="JP" if i % 2 else "US", label="Dance" if i % 4 else "Comedy"))
        self.specs = specs
        self.videos, self.enriched = dataset(specs)
        self.p, self.ex = pool.build(self.videos, self.enriched, 200)
        self.idx = {s["video_id"]: i for i, s in enumerate(specs)}

    def ids(self, idxs):
        return {self.specs[i]["video_id"] for i in idxs}

    def test_each_category_matches_table(self):
        by_plays = sorted(range(90), key=lambda i: -self.specs[i]["plays"])
        ver = [i for i in range(90) if i % 3 == 0]
        self.assertEqual(with_reason(self.p, "origin"), self.ids(range(10)))                      # 早い順に1〜10本目
        self.assertEqual(with_reason(self.p, "earliest"), self.ids(range(10, 20)))                # 11〜20本目
        self.assertEqual(with_reason(self.p, "top_hit"), self.ids(by_plays[:10]))                 # 再生の多い順に1〜10本目
        self.assertEqual(with_reason(self.p, "top_plays"), self.ids(by_plays[10:20]))             # 11〜20本目
        self.assertEqual(with_reason(self.p, "verified_early"), self.ids(ver[:10]))               # 認証: 早い順に10本
        self.assertEqual(with_reason(self.p, "verified_top"),
                         self.ids(sorted(ver, key=lambda i: -self.specs[i]["plays"])[:15]))        # 認証: 再生順に15本
        self.assertEqual(with_reason(self.p, "artist"), self.ids((40, 60, 80)))                   # 本人の投稿は全部
        self.assertEqual(self.ex["campaign_tags"], ["songxchallenge"])
        self.assertEqual(with_reason(self.p, "campaign_tag"), self.ids((7, 21, 40, 55)))           # 公式企画のタグは全部
        self.assertEqual(with_reason(self.p, "official"), self.ids((21,)))                        # そのうち認証

    def test_caps_page_and_weekly(self):
        must = must_of(self.p)
        weekly = set(self.p) - must
        self.assertTrue(must and weekly)
        self.assertTrue(all(self.p[v]["cap"] == pool.CAP_PAGE == 20 for v in must))                # 1本1ページ
        self.assertTrue(all(self.p[v]["cap"] == 0 for v in weekly))                                # 週ごとは取らない
        plays = {s["video_id"]: s["plays"] for s in self.specs}
        self.assertTrue(all(plays[v] >= pool.MIN_PLAYS_WEEKLY for v in weekly))                   # 週ごとだけ10万以上
        self.assertTrue(any(plays[v] < pool.MIN_PLAYS_WEEKLY for v in must))                       # 必ず取る動画には下限を掛けない
        self.assertEqual(self.p[self.specs[0]["video_id"]]["cap"], 20)                             # 再生1,000の起点も1ページ
        self.assertEqual(self.ex["n_pool"], len(must))
        self.assertEqual(self.ex["n_no_comments"], len(weekly))
        # 取得の側と同じ「1ページ」の数字
        self.assertEqual(spatest.PAGE_CAP, pool.CAP_PAGE)
        self.assertEqual(pipeline.subs_cap(pipeline.DEFAULTS), pool.CAP_PAGE)

    def test_min_plays_weekly_changes_only_weekly(self):
        """道具の min_plays（下限）を変えても、必ず取る動画は変わらない"""
        p1, _ = pool.build(self.videos, self.enriched, 200, min_plays_weekly=1)
        self.assertEqual(must_of(p1), must_of(self.p))
        self.assertGreater(len(p1), len(self.p))                                                  # 週ごとの動画だけ増える

    def test_pool_tsv_cap_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "pool.tsv"
            pool.write(self.p, self.videos, out)
            import csv
            rows = list(csv.DictReader(open(out, encoding="utf-8"), delimiter="\t"))
        must = must_of(self.p)
        for r in rows:
            self.assertEqual((r["cap"], r["priority"]), ("20", "1") if r["video_id"] in must else ("0", "2"))
        self.assertEqual([int(r["seq"]) for r in rows], sorted(int(r["seq"]) for r in rows))


class TestPoolBoundaries(unittest.TestCase):
    """境目: 同じ再生数・同じ日付（決まった順で切る。何度作っても同じ）"""

    def test_same_plays_at_rank_10_11_goes_to_earlier_post(self):
        def build(x_first):
            specs = [spec(8_000 + i, day(i), 2_000_000 - i * 100_000) for i in range(9)]   # 上位9本
            specs += [spec(9_001, day(20 if x_first else 21), 500_000), spec(9_002, day(21 if x_first else 20), 500_000)]
            specs += [spec(10_000 + i, day(30 + i), 10_000 + i) for i in range(15)]
            v, e = dataset(specs)
            return pool.build(v, e, 30)[0]
        p = build(True)
        self.assertIn("top_hit", p["9001"]["reasons"])         # 同じ再生数なら、先に投稿したほうが10本目
        self.assertIn("top_plays", p["9002"]["reasons"])
        self.assertEqual(build(True), p)                       # 作り直しても同じ
        p2 = build(False)
        self.assertIn("top_hit", p2["9002"]["reasons"])
        self.assertIn("top_plays", p2["9001"]["reasons"])

    def test_same_date_at_rank_10_11_goes_to_more_plays(self):
        """同じ日付の10本目・11本目は、再生の多いほうが起点（prep_sample.py の seq の並び。時刻は見ない）"""
        specs = [spec(8_000 + i, day(i), 1_000 + i) for i in range(9)]
        specs += [spec(9_001, day(9), 5_000), spec(9_002, day(9), 7_000)]
        specs += [spec(10_000 + i, day(30 + i), 100 + i) for i in range(10)]
        v, e = dataset(specs)
        p, _ = pool.build(v, e, 30)
        self.assertIn("origin", p["9002"]["reasons"])
        self.assertIn("earliest", p["9001"]["reasons"])

    def test_videos_without_author_are_not_selected(self):
        """属性の取れなかった投稿（写真・消えた動画。author 無し）は、どの区分にも入れない"""
        specs = [spec(8_000 + i, day(i), 1_000_000 - i) for i in range(25)]
        v, e = dataset(specs)
        e["8000"]["author"] = {}                    # いちばん早く、いちばん再生の多い投稿
        p, ex = pool.build(v, e, 30)
        self.assertNotIn("8000", p)
        self.assertEqual(ex["alive"], 24)
        self.assertEqual(len(with_reason(p, "origin")), 10)


class TestArtistDetection(unittest.TestCase):
    """本人のアカウントの判定（音源の作者名を、投稿者の unique_id か表示名に部分一致で探している）"""

    def _alive(self, specs, author):
        v, e = dataset(specs, music_author=author)
        return v, e

    def test_short_artist_name_does_not_match_unrelated_accounts(self):
        """【不具合】作者名が短い（Ado → "ado"）と、名前に ado を含むだけの無関係なアカウントまで本人になる"""
        specs = [spec(1, day(0), 1_000_000, uid="ado1024", nick="Ado", verified=True),
                 spec(2, day(1), 500_000, uid="tornado_dance", nick="トルネード"),
                 spec(3, day(2), 400_000, uid="adorable_cat", nick="猫"),
                 spec(4, day(3), 300_000, uid="someone", nick="dancer")]
        v, e = self._alive(specs, "Ado")
        self.assertEqual(pool.artist_accounts(v, e, "Ado"), {"ado1024"})

    def test_fan_accounts_with_artist_name_are_not_the_artist(self):
        """【不具合】きゃわ（a20261006-0407-60f4）の実例: フォロワー数百のまとめ・切り抜きのアカウントが本人になり、
        AI に渡すコメントの資料に「プールに入れた理由: artist」と出ていた"""
        specs = [spec(1, day(0), 838_000, uid="ilife_official", nick="iLiFE!【あいらいふ】", followers=580_400,
                      bio="私とあなたで作るアイドルライフ！ iLiFE!【アイライフ】公式TikTokです！"),
                 spec(2, day(1), 315_600, uid="ilife_karen", nick="そなたかれん", followers=188_800, bio="iLiFE! あおいろ"),
                 spec(3, day(2), 269_200, uid="ilife_lyrics", nick="いつまでもずっとアイライファー！", followers=277,
                      bio="iLiFE!を中心にヒロインズパート・セリフまとめ リクエストコメントお願いします"),
                 spec(4, day(3), 50_000, uid="ilife_short", nick="毎日iLiFE", followers=412,
                      bio="YouTube切り抜き＆ショート投稿 iLiFE!多めに発信中")]
        v, e = self._alive(specs, "iLiFE!")
        got = pool.artist_accounts(v, e, "iLiFE!")
        self.assertIn("ilife_official", got)
        self.assertNotIn("ilife_lyrics", got)
        self.assertNotIn("ilife_short", got)

    def test_small_unverified_artist_with_exact_name_is_kept(self):
        """認証の無い小さなアーティスト（フォロワー3,000）の本人のアカウントは、名前がぴったり同じなら本人のまま。
        フォロワーの下限は「本人名＋区切り」で始まるだけのアカウント（ファン・まとめもありうる）にだけ掛ける（2026-10-06 試験の係）"""
        specs = [spec(1, day(0), 120_000, uid="hoshinoyoru", nick="星野ヨル", followers=3_000),
                 spec(2, day(1), 80_000, uid="hoshinoyoru_fan", nick="ヨルの切り抜き", followers=900)]
        v, e = self._alive(specs, "hoshinoyoru")
        got = pool.artist_accounts(v, e, "hoshinoyoru")
        self.assertIn("hoshinoyoru", got)
        self.assertNotIn("hoshinoyoru_fan", got)
        specs = [spec(1, day(0), 120_000, uid="yoru_music_jp", nick="星野ヨル", followers=3_000)]
        v, e = self._alive(specs, "星野ヨル")
        self.assertEqual(pool.artist_accounts(v, e, "星野ヨル"), {"yoru_music_jp"})   # 表示名がぴったり同じ

    def test_fan_tag_does_not_become_campaign_tag(self):
        """【不具合（上の続き）】本人と取り違えたアカウントが使った「曲名入りのタグ」が公式企画のタグになり、
        そのタグの付いた動画が全部「必ず取る動画」になる"""
        specs = [spec(1, day(0), 1_000_000, uid="ado1024", nick="Ado", verified=True, tags=["うっせぇわ"]),
                 spec(2, day(1), 500_000, uid="tornado_dance", nick="トルネード", tags=["うっせぇわ踊ってみた"])]
        specs += [spec(100 + i, day(10 + i), 1_000 + i, tags=["うっせぇわ踊ってみた"]) for i in range(30)]
        v, e = dataset(specs, music_author="Ado", title="うっせぇわ")
        p, ex = pool.build(v, e, 30)
        self.assertEqual(ex["campaign_tags"], [])
        self.assertFalse(with_reason(p, "campaign_tag"))

    def test_collab_author_finds_each_artist(self):
        """【不具合】作者名が「A × B」の曲では、どちらの公式アカウントも本人にならない（本人・公式企画が空になる）"""
        specs = [spec(1, day(0), 1_000_000, uid="yoasobi_staff", nick="YOASOBI", verified=True, tags=["songxdance"]),
                 spec(2, day(1), 900_000, uid="ado1024", nick="Ado", verified=True),
                 spec(3, day(2), 500_000, uid="someone", nick="dancer")]
        v, e = dataset(specs, music_author="YOASOBI × Ado")
        got = pool.artist_accounts(v, e, "YOASOBI × Ado")
        self.assertIn("yoasobi_staff", got)
        self.assertIn("ado1024", got)

    def test_exact_artist_found_by_nickname(self):
        """日本語の作者名でも、表示名に含まれていれば本人（romaji の unique_id でも拾える）"""
        specs = [spec(1, day(0), 1_000_000, uid="yorushika_official", nick="ヨルシカ", followers=1_200_000),
                 spec(2, day(1), 500_000, uid="someone", nick="dancer")]
        v, e = dataset(specs, music_author="ヨルシカ")
        self.assertEqual(pool.artist_accounts(v, e, "ヨルシカ"), {"yorushika_official"})


class TestClusterTopsInPool(unittest.TestCase):
    """属性の山の上位: 12個の山それぞれの再生上位3本のうち、必ず取る動画でないものを足す（上限20件・再生の下限なし）"""

    def test_cluster_tops_join_must_without_floor(self):
        specs = [spec(7_200_000_000_000_000_000 + i, day(i), ((i * 37) % 90) * 20_000 + 1_000 + i,
                      verified=(i % 3 == 0)) for i in range(90)]
        v, e = dataset(specs)
        base, ex0 = pool.build(v, e, 200)
        must0 = must_of(base)
        weekly = sorted(set(base) - must0)
        outside = sorted((x for x in v if x["video_id"] not in base and x["plays"] < pool.MIN_PLAYS_WEEKLY),
                         key=lambda x: x["seq"])
        self.assertTrue(weekly and outside)
        m, w, o = sorted(must0)[0], weekly[0], outside[0]["video_id"]
        records = {x["video_id"]: {} for x in v}
        with mock.patch.object(pool.attr_cluster, "tops", return_value=[m, w, o]) as tops:
            p, ex = pool.build(v, e, 200, records=records)
            self.assertTrue(tops.called)
        self.assertEqual(p[m]["reasons"], base[m]["reasons"])                  # すでに必ず取る動画には足さない
        self.assertIn("cluster_top", p[w]["reasons"])                          # 週ごとの動画は必ず取る動画に変わる
        self.assertTrue(any(r.startswith("week:") for r in p[w]["reasons"]))   # ラベルを付ける動画としては残る
        self.assertEqual((p[w]["cap"], p[o]["cap"]), (20, 20))                 # 下限（10万）未満でも足す
        self.assertEqual(ex["n_cluster_top"], 2)
        self.assertEqual(ex["n_pool"], ex0["n_pool"] + 2)
        self.assertEqual(set(base) | {o}, set(p))
        with mock.patch.object(pool.attr_cluster, "tops", return_value=[m, w, o]) as tops:
            pf, exf = pool.build(v, e, 200, records=records, plan="full")
            self.assertFalse(tops.called)                                      # full では足さない
        self.assertEqual(exf["n_cluster_top"], 0)


class TestAttrClusterTops(unittest.TestCase):
    def _data(self):
        records, enriched, plays = {}, {}, {}
        for g, (tags, loc, lang, words) in enumerate((("dance kpop", "KR", "ko", "dance kpop cover idol"),
                                                       ("anime cosplay", "JP", "ja", "anime cosplay character art"))):
            for j in range(6):
                vid = f"{g}{j:02d}"
                records[vid] = {"hashtags": tags.split(), "tiktok_labels": [], "suggested_words": [],
                                "location_created": loc, "text_language": lang, "desc": words}
                enriched[vid] = {"author": {"follower_count": 5000, "verified": False, "signature": "", "nickname": ""},
                                 "diversification_labels": []}
                plays[vid] = (j + 1) * 1000 + g
        return records, enriched, plays

    def test_top_n_per_cluster_by_plays(self):
        records, enriched, plays = self._data()
        ids = list(records)
        got = attr_cluster.tops(records, enriched, ids, plays, k=2, n=3)
        self.assertEqual(len(got), len(set(got)))                              # 重なりなし
        self.assertEqual(set(got), {"003", "004", "005", "103", "104", "105"}) # 山ごとに再生上位3本
        self.assertEqual(got, attr_cluster.tops(records, enriched, ids, plays, k=2, n=3))   # 何度作っても同じ

    def test_few_videos_each_own_cluster(self):
        records, enriched, plays = self._data()
        ids = list(records)[:5]
        self.assertEqual(attr_cluster.clusters(records, enriched, ids, k=12), {v: i for i, v in enumerate(ids)})
        self.assertEqual(attr_cluster.tops(records, enriched, ids, plays, k=12, n=3), ids)


# ---------------------------------------------------------------------------
# spatest の試験の道具
class Clock:
    """time.time / time.sleep の代わり（眠らずに進める）"""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, float(s))


def resp(vid, n=20, start=0, cursor=20, has_more=1, total=500, reply_of=None):
    cs = [{"cid": f"{vid}{start + i:04d}", "aweme_id": str(vid), "reply_id": str(reply_of) if reply_of else "0",
           "text": "x", "digg_count": 0} for i in range(n)]
    path = "/api/comment/list/reply/" if reply_of else "/api/comment/list/"
    return {"url": f"https://www.tiktok.com{path}?aweme_id={vid}&cursor={start}&count=20",
            "body": json.dumps({"comments": cs, "cursor": cursor, "has_more": has_more, "total": total}),
            "status": 200, "via": "fetch", "sentAt": 0, "gotAt": 0}


def harvest_recs(hits):
    """spatest.harvest の JS と同じ形の記録"""
    out = []
    for h in hits:
        b = json.loads(h["body"])
        cs = b.get("comments") or []
        out.append({"path": re.search(r"tiktok\.com([^?]+)", h["url"]).group(1),
                    "req_aweme": re.search(r"aweme_id=(\d+)", h["url"]).group(1), "req_count": "20",
                    "url_cursor": re.search(r"cursor=(\d+)", h["url"]).group(1), "via": "fetch", "sentAt": 0, "gotAt": 0,
                    "status": 200, "len": len(h["body"]), "n": len(cs), "resp_cursor": b.get("cursor"),
                    "aweme": str(cs[0]["aweme_id"]) if cs else None, "has_more": b.get("has_more"),
                    "main": (str(cs[0].get("reply_id") or "0") == "0") if cs else None, "body": h["body"]})
    return out


class FakePage:
    """楽曲ページから動画へ移ったときのページの代わり。

    動画を開くと、その動画の1ページ目（まだ取っていなければ）と、グリッドで次の動画の先読み（取る予定の動画だけ。
    取らない動画への先読みはフックが送らない）を送る。応答は delay 秒後に届く。説明文とコメントのアイコンは 0.5秒後に出る
    （本番の記録: 表示 1.3〜1.5秒。説明文が出た時点ではまだ応答が届いていない）。一度届いた動画は、開き直しても取り直さない"""

    def __init__(self, clock, grid, allow, delay=1.2, totals=None):
        self.clock, self.grid, self.allow = clock, list(grid), set(allow)
        self.delay, self.totals = delay, totals or {}
        self.hits, self.pending, self.sends, self.cache = [], [], [], set()
        self.desc_at = None
        self.icon_clicks = 0

    def deliver(self):
        for item in sorted(self.pending, key=lambda x: x[0]):
            if item[0] <= self.clock.t:
                self.hits.append(item[1])
                self.pending.remove(item)

    def request(self, vid, after):
        self.sends.append(self.clock.t)
        self.pending.append((self.clock.t + after, resp(vid, total=self.totals.get(vid, 500))))
        self.cache.add(vid)

    def open(self, vid):
        self.desc_at = self.clock.t + 0.5
        if vid not in self.cache:
            self.request(vid, self.delay)
        i = self.grid.index(vid)
        nb = self.grid[i + 1] if i + 1 < len(self.grid) else None
        if nb and nb not in self.cache and nb in self.allow:
            self.request(nb, self.delay + 0.1)
        return "js", None

    def page_state(self):
        self.deliver()
        shown = self.desc_at is not None and self.clock.t >= self.desc_at
        return {"url": "https://www.tiktok.com/@u/video/1", "doc": "doc1", "desc": shown, "modal": False, "icon": shown,
                "items": 0, "hits": len(self.hits), "bad": 0, "empty": 0, "links": 10, "resources": 0, "challenge": False}


class FakeDriver:
    def __init__(self, page):
        self.page = page
        self.window_handles = [1]

    def find_elements(self, *a):
        return [object()]

    def execute_script(self, js, *args):
        pg = self.page
        pg.deliver()
        bodies = [json.loads(h["body"]) for h in pg.hits]
        if js == spatest.CIDS_JS:
            vid = str(args[0])
            return [c["cid"] for b in bodies for c in b["comments"] if c["aweme_id"] == vid and c["reply_id"] == "0"]
        if js == spatest.STEP_JS:
            vid = str(args[1])
            got = {c["cid"] for b in bodies for c in b["comments"] if c["aweme_id"] == vid and c["reply_id"] == "0"}
            mine = sum(1 for b in bodies if b["comments"] and b["comments"][0]["aweme_id"] == vid)
            return {"top": 0, "h": 1000, "client": 500, "atBottom": False, "hits": len(pg.hits), "got": len(got), "topn": 0,
                    "replies": 0, "zeros": 0, "mine": mine, "more0": False, "dist": 500, "empty": 0}
        if js == spatest.PRESCROLL_JS:
            return {"moved": False, "dist": 500, "sent": len(pg.sends)}
        if "req_aweme" in js:                       # harvest
            return harvest_recs(pg.hits)
        if "coalesced" in js:
            return 0
        if "__capReset" in js:
            pg.hits.clear()
            return None
        if "b.total" in js and args:                # api_meta(vid)
            vid = str(args[0])
            for b in reversed(bodies):
                cs = b["comments"]
                if cs and cs[0]["aweme_id"] == vid and cs[0]["reply_id"] == "0":
                    return {"total": b["total"], "has_more": b["has_more"], "cursor": b["cursor"]}
            return None
        if "drops" in js:
            return []
        if "click()" in js:
            pg.icon_clicks += 1
        return None


def collector_args(td, calls=3.0, max_calls=4.0, extra=()):
    """acquire/pipeline.py の step_comments と同じ引数"""
    return spatest.build_parser().parse_args([
        "--music-url", "https://www.tiktok.com/music/x-1", "--resume", "--collect-scrolls", "150",
        "--reply-policy", "targets", "--reply-top", "0", "--reply-questions", "0", "--reply-author", "0",
        "--cap", "40", "--min-comments", "20", "--subs-cap", "20",
        "--calls-per-min", str(calls), "--max-calls-per-min", str(max_calls), "--interval", "15.0", "--jitter", "0.5",
        "--out", str(Path(td) / "comments.jsonl"), "--log", str(Path(td) / "comments.log"),
        "--summary", str(Path(td) / "summary.json"),
        "--stop-on-no-more", "--prescroll", "--only-open-video", "--remount-on-stall", *extra])


def href(v):
    return f"https://www.tiktok.com/@u/video/{v}"


def max_in_60s(times):
    return max((sum(1 for u in times if t <= u < t + 60.0) for t in times), default=0)


class SpaHarness(unittest.TestCase):
    """SpaCollector.collect_one をページと時計の代わりで動かす（run() の動画の間の待ちは最短の15秒で入れる）"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.patch = mock.patch.object(spatest, "time", self.clock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def make(self, grid, targets, calls=3.0, max_calls=4.0, totals=None):
        c = spatest.SpaCollector(collector_args(self.td.name, calls, max_calls))
        c.log = lambda m: None
        page = FakePage(self.clock, grid, targets, totals=totals)
        c.d = FakeDriver(page)
        c.page_state = page.page_state
        c.open_video = lambda h: page.open(spatest.vid_of(h))
        c.close_video = lambda: (self.clock.sleep(0.5), "history_back")[1]
        c.grid_ids = list(grid)
        c.allow = set(targets)
        c.caps = {v: 20 for v in targets}
        return c, page

    def run_videos(self, c, vids):
        rows = []
        for i, v in enumerate(vids):
            if i:
                self.clock.sleep(c.a.interval)          # run() の動画の間の待ち（最短）
            rows.append(c.collect_one(href(v)))
        return rows


class TestPacingInPagePlan(SpaHarness):
    """要求の間隔（平均3回/分・60秒に4回。止まったら1.8回/分・60秒に2回）を、1本1ページの取り方でも守るか"""

    def test_open_request_is_counted(self):
        """【不具合】開いた瞬間の要求が、間隔の記録（note_call）に一度も数えられない。
        説明文が出た時点ではまだ応答が届いておらず（hits 0）、そのあと届いた1ページ目で reached_cap が送る前に止めるので、
        数える所を一度も通らない。本番（きゃわ a20261006-0407-60f4）の要約も api_calls 0・calls_per_min 0.0（実際の要求は121回）"""
        A, X = "7300000000000000001", "7300000000000000002"
        c, page = self.make([A, X], [A])
        r = c.collect_one(href(A))
        self.assertEqual((r["status"], r["fetched"], len(page.sends)), ("ok", 20, 1))
        self.assertEqual(c.calls, 1)
        self.assertGreater(c.pace_ready_at(), self.clock.time())   # 次の要求は間隔を空けてから

    def test_window_of_4_per_60s_holds(self):
        """【不具合】グリッドで並んだ取る動画を続けて開くと（開く瞬間に隣の先読みが飛ぶ）、60秒に5回送ってしまう"""
        vids = [f"73100000000000000{i:02d}" for i in range(6)]
        c, page = self.make(vids, vids)
        rows = self.run_videos(c, vids[:5])
        self.assertTrue(all(r["fetched"] == 20 for r in rows))
        self.assertLessEqual(max_in_60s(page.sends), 4, [round(t - page.sends[0], 1) for t in page.sends])

    def test_fallback_after_block_is_enforced(self):
        """【不具合】止まったあとに落とす間隔（平均1.8回/分・60秒に2回）が効かない。動画の間の待ち（15〜22.5秒）だけで進む"""
        A, B, C = "7320000000000000001", "7320000000000000003", "7320000000000000005"
        grid = [A, "7320000000000000002", B, "7320000000000000004", C, "7320000000000000006"]
        c, page = self.make(grid, [A, B, C], calls=1.8, max_calls=2.0)
        self.assertEqual((c.min_spacing, round(c.call_spacing, 1)), (30.0, 33.3))
        self.run_videos(c, [A, B, C])
        self.assertEqual(len(page.sends), 3)
        gaps = [b - a for a, b in zip(page.sends, page.sends[1:])]
        self.assertTrue(all(g >= 30.0 for g in gaps), gaps)
        self.assertLessEqual(max_in_60s(page.sends), 2)


class TestCollectOnePage(SpaHarness):
    def test_need_follows_neighbor(self):
        """開く前に空ける枠: 隣が取る予定なら2本ぶん、取らない動画なら1本ぶん、グリッドの最後なら2本ぶん（分からないので多めに）"""
        A, B, X = "7330000000000000001", "7330000000000000002", "7330000000000000003"
        c, page = self.make([A, B, X], [A, B, X])
        c.allow = {A, B}
        page.allow = {A, B}
        r = self.run_videos(c, [A, B])
        self.assertEqual((r[0]["need"], r[1]["need"]), (2, 1))
        c2, _ = self.make([A, B], [A, B])
        self.assertEqual(c2.collect_one(href(B))["need"], 2)

    def test_prefetched_comments_are_sorted_by_video(self):
        """先読みで届いた隣の動画のコメントは、その動画の取り分になる（aweme_id で仕分け。混入0）"""
        A, B, X = "7340000000000000001", "7340000000000000002", "7340000000000000003"
        c, page = self.make([A, B, X], [A, B])
        ra, rb = self.run_videos(c, [A, B])
        self.assertEqual(len(page.sends), 2)                                   # B の1ページ目は先読みの分だけ
        self.assertEqual((ra["fetched"], rb["fetched"]), (20, 20))
        self.assertTrue(all(x["aweme_id"] == A for x in ra["comments"]))
        self.assertTrue(all(x["aweme_id"] == B for x in rb["comments"]))
        self.assertEqual(rb["api_calls"], 0)

    def test_prefetched_video_does_not_wait_for_new_request(self):
        """【不具合】1ページ目が先読みで届いている動画（ページは取り直さない）を開くと、来ない要求を待ってアイコンを押し、
        8秒＋25秒待つ。本番（きゃわ）で21本が1本35〜36秒（ほかは3〜4秒）、計約11分"""
        A, B, X = "7350000000000000001", "7350000000000000002", "7350000000000000003"
        c, page = self.make([A, B, X], [A, B])
        ra, rb = self.run_videos(c, [A, B])
        self.assertEqual(rb["fetched"], 20)
        self.assertLess(rb["seconds"], 10.0, rb)
        self.assertFalse(rb["clicked_icon"])

    def test_total_of_prefetched_page_is_kept(self):
        """【不具合】1ページ目が先読みで届いた動画は、応答の総数（total）が記録に残らない（None）。
        AI に渡すコメントの資料に「コメント総数 None」と出る（きゃわで135本中45本）"""
        A, B, X = "7360000000000000001", "7360000000000000002", "7360000000000000003"
        c, page = self.make([A, B, X], [A, B], totals={A: 532, B: 99})
        ra, rb = self.run_videos(c, [A, B])
        self.assertEqual(ra["total"], 532)
        self.assertEqual(rb["total"], 99)


class TestReachedCapAndHarvest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.c = spatest.SpaCollector(collector_args(self.td.name))
        self.c.log = lambda m: None

    def tearDown(self):
        self.td.cleanup()

    def test_reached_cap_ignores_replies_and_other_videos(self):
        """1ページの判定は、その動画の本体のコメントだけで見る（返信・隣の動画のコメントでは止めない）"""
        A, B = "7370000000000000001", "7370000000000000002"
        page = FakePage(Clock(), [A, B], [A, B])
        page.hits = [resp(B), resp(A, n=3, reply_of="999")]
        self.c.d = FakeDriver(page)
        self.c.cur_cap = 20
        self.assertFalse(self.c.reached_cap(A))
        self.c.pool[A] = {"r1": {"cid": "r1", "aweme_id": A, "reply_id": "555"}}
        self.assertFalse(self.c.reached_cap(A))
        page.hits.append(resp(A, n=1))
        self.assertTrue(self.c.reached_cap(A))                     # 1件でも本体が届けば1ページ目が届いている
        self.c.cur_cap = 21                                         # 1ページより多い上限なら件数で見る
        self.assertFalse(self.c.reached_cap(A))

    def test_harvest_sorts_by_aweme_id_and_keeps_last_page_state(self):
        A, B = "7380000000000000001", "7380000000000000002"
        page = FakePage(Clock(), [A, B], [A, B])
        stray = resp(A, n=2, start=40, cursor=60, has_more=0)
        b = json.loads(stray["body"])
        b["comments"].append({"cid": "x1", "aweme_id": B, "reply_id": "0"})   # 1つの応答に別の動画のコメントが混ざっても
        stray["body"] = json.dumps(b)
        page.hits = [resp(A, start=0, cursor=20, has_more=1), resp(B, start=0, cursor=20, has_more=1),
                     stray, resp(A, n=3, start=100, cursor=3, has_more=1, reply_of="999")]
        self.c.d = FakeDriver(page)
        self.c.harvest()
        self.assertEqual(page.hits, [])                                         # ブラウザ側は空にする
        self.assertTrue(all(x["aweme_id"] == A for x in self.c.pool[A].values()))
        self.assertTrue(all(x["aweme_id"] == B for x in self.c.pool[B].values()))
        self.assertIn("x1", self.c.pool[B])
        self.assertEqual(len(self.c.gather(A)), 22)
        self.assertEqual(len(self.c.gather(A, "reply")), 3)
        self.assertEqual(self.c.page_state_of[A], (60, 0))                      # いちばん先のページ（返信の応答は見ない）
        self.assertEqual(self.c.page_state_of[B], (20, 1))
        self.assertTrue(self.c.no_more(A))
        self.assertFalse(self.c.no_more(B))
        self.assertTrue(self.c.no_more(B, {"more0": True}))


class TestPaceRules(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.patch = mock.patch.object(spatest, "time", self.clock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_spacing_average_and_minimum(self):
        for calls, mx, lo, hi in ((3.0, 4.0, 15.0, 25.0), (1.8, 2.0, 30.0, 60 / 1.8 * 2 - 30.0)):
            c = spatest.SpaCollector(collector_args(self.td.name, calls, mx))
            xs = [c.next_spacing() for _ in range(4000)]
            self.assertGreaterEqual(min(xs), lo)
            self.assertLessEqual(max(xs), hi + 1e-9)
            self.assertAlmostEqual(sum(xs) / len(xs), 60.0 / calls, delta=0.5)   # 平均は calls_per_min

    def test_pace_wait_spacing_then_window(self):
        c = spatest.SpaCollector(collector_args(self.td.name))
        now = self.clock.time()
        c.last_call_at, c.pending_spacing, c.call_times = now - 5, 15.0, []
        c.pace_wait()
        self.assertAlmostEqual(self.clock.time(), now + 10)                    # 前回から15秒
        now = self.clock.time()
        c.last_call_at, c.pending_spacing = now - 100, 15.0
        c.call_times = [now - 50, now - 40, now - 30, now - 20]                # 直近60秒に4回
        self.assertAlmostEqual(c.pace_ready_at(need=1), now + 10)
        self.assertAlmostEqual(c.pace_ready_at(need=2), now + 20)
        c.pace_wait(need=2)
        self.assertAlmostEqual(self.clock.time(), now + 20)                    # 2本ぶんの枠が空くまで
        self.assertEqual(len(c.call_times), 2)

    def test_note_call_records_each_request(self):
        c = spatest.SpaCollector(collector_args(self.td.name))
        c.note_call(2)
        self.assertEqual((c.calls, len(c.call_times), c.last_call_at), (2, 2, self.clock.time()))
        self.assertTrue(15.0 <= c.pending_spacing <= 25.0)


class TestPoolTargets(unittest.TestCase):
    HEAD = "video_id\tseq\tdate\tweek\tplays\tusername\tcap\tpriority\treasons\n"

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def collector(self, pool_p, cand, grid, out_lines=()):
        out = self.d / "comments.jsonl"
        if out_lines:
            out.write_text("".join(json.dumps(r) + "\n" for r in out_lines), encoding="utf-8")
        a = spatest.build_parser().parse_args(["--music-url", "https://www.tiktok.com/music/x-1", "--pool", str(pool_p),
                                               "--candidates", str(cand), "--subs-cap", "20", "--out", str(out),
                                               "--log", str(self.d / "log.txt")])
        c = spatest.SpaCollector(a)
        c.log = lambda m: None
        c.asked = {}

        def collect_links(n, want_ids=None):
            c.asked["want"] = list(want_ids or [])
            return [href(v) for v in grid]
        c.collect_links = collect_links
        return c

    def write_cand(self, rows):
        p = self.d / "records.jsonl"
        p.write_text("".join(json.dumps({"video_id": v, "week": w, "plays": pl}) + "\n" for v, w, pl in rows), encoding="utf-8")
        return p

    def test_substitution_rules(self):
        """見つからない動画は、同じ週の・プール外の・取得済みでない・グリッドに見えている動画のうち再生最大に差し替える。
        前の走行で差し替え済みの動画は、もう探さない"""
        pool_p = self.d / "pool.tsv"
        pool_p.write_text(self.HEAD +
                          "1000001\t0\t2025-07-20\tW1\t100\ta\t20\t1\torigin\n"
                          "1000002\t1\t2025-07-20\tW1\t500\tb\t20\t1\ttop_hit\n"        # 見つからない
                          "1000003\t2\t2025-07-27\tW2\t50\tc\t20\t1\tartist\n"          # 見つからない（前の走行で差し替え済み）
                          "1000004\t3\t2025-07-28\tW2\t60\tc\t20\t1\tartist\n"          # 見つからない（同じ週に候補なし）
                          "1000005\t4\t2025-07-21\tW1\t900\td\t0\t2\tweek:W1\n", encoding="utf-8")
        cand = self.write_cand([("1000005", "W1", 900), ("2000001", "W1", 800), ("2000002", "W1", 300),
                                ("2000003", "W1", 5000), ("2000004", "W3", 9000)])
        grid = ["2000002", "1000001", "1000005", "2000001", "2000003", "2000004"]
        c = self.collector(pool_p, cand, grid, out_lines=[
            {"video_id": "2000003", "status": "ok", "pool_reason": "substitute_for:1000099"},
            {"video_id": "2000009", "status": "ok", "pool_reason": "substitute_for:1000003"}])
        got = [spatest.vid_of(h) for h in c.pool_targets({"2000003"})]
        self.assertEqual(sorted(c.asked["want"]), ["1000001", "1000002", "1000004"])
        self.assertEqual(got, ["1000001", "2000001"])        # 取得済み（2000003）・週の違う（2000004）・cap 0（1000005）は選ばない
        self.assertEqual(c.caps["2000001"], 20)
        self.assertEqual(c.why["2000001"], "substitute_for:1000002")
        self.assertEqual(c.missing, ["1000004"])

    def test_multi_page_does_not_substitute_cap0(self):
        """【不具合】楽曲ページが2つ以上の分析では、ページごとのプールに cap 0 の行が渡らないので（pipeline.comment_pages）、
        週ごとの動画（コメントを取らない動画）が差し替え先に選ばれる（文書: 「cap 0 の動画は…差し替え先にもしない」）"""
        d = self.d
        (d / "derived").mkdir()
        (d / "raw").mkdir()
        (d / "fetch_log").mkdir()
        (d / "derived" / "pool.tsv").write_text(
            self.HEAD +
            "1000001\t0\t2025-07-20\tW1\t100\ta\t20\t1\torigin\n"
            "1000002\t1\t2025-07-20\tW1\t500\tb\t20\t1\ttop_hit\n"
            "1000005\t2\t2025-07-21\tW1\t900\td\t0\t2\tweek:W1\n"
            "1000007\t3\t2025-07-22\tW1\t700\te\t20\t1\tartist\n", encoding="utf-8")
        (d / "raw" / "grid_links.jsonl").write_text("".join(
            json.dumps({"video_id": v, "url": href(v), "source": s}) + "\n"
            for v, s in (("1000001", 1), ("1000002", 1), ("1000005", 1), ("2000001", 1), ("1000007", 2))), encoding="utf-8")

        class StubRun:
            dir = d
            p = pipeline.Run.p
            link_sources = pipeline.Run.link_sources

            def music_urls(self):
                return ["https://www.tiktok.com/music/a-1", "https://www.tiktok.com/music/b-2"]
        pages = pipeline.Run.comment_pages(StubRun())
        self.assertEqual(len(pages), 2)
        cand = self.write_cand([("1000005", "W1", 900), ("2000001", "W1", 300)])
        c = self.collector(pages[0][1], cand, ["1000001", "1000005", "2000001"])
        got = [spatest.vid_of(h) for h in c.pool_targets(set())]
        self.assertNotIn("1000005", got)
        self.assertEqual(got, ["1000001", "2000001"])

    def test_no_grid_scroll_when_nothing_left(self):
        """【不具合（低）】取る動画が残っていない（全部取得済み）のに、グリッドを最後まで（--collect-scrolls 150回・各2秒）送る"""
        pool_p = self.d / "pool.tsv"
        pool_p.write_text(self.HEAD + "1000001\t0\t2025-07-20\tW1\t100\ta\t20\t1\torigin\n", encoding="utf-8")
        cand = self.write_cand([])
        a = spatest.build_parser().parse_args(["--music-url", "https://www.tiktok.com/music/x-1", "--pool", str(pool_p),
                                               "--candidates", str(cand), "--collect-scrolls", "150",
                                               "--out", str(self.d / "o.jsonl"), "--log", str(self.d / "log.txt")])
        c = spatest.SpaCollector(a)
        c.log = lambda m: None
        presses = []

        class Body:
            def send_keys(self, k):
                presses.append(k)

        class D:
            def execute_script(self, js, *args):
                return [href("1000001"), href("1000009")]

            def find_element(self, *a):
                return Body()
        c.d = D()
        with mock.patch.object(spatest, "time", Clock()):
            links = c.pool_targets({"1000001"})
        self.assertEqual(links, [])
        self.assertEqual(len(presses), 0)


class TestDropBeforeRelease(unittest.TestCase):
    """曲の公開（楽曲ページの番号の時刻）より前の投稿は、プールを作る前の一覧から外れる（同じ秒は残す）"""

    def test_boundary(self):
        rel = 1_750_000_000
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "fetch_log").mkdir()

            class StubRun:
                dir = d
                p = pipeline.Run.p

                def log(self, m):
                    pass
            vid = lambda t, low: str((t << 32) | low)   # noqa: E731
            seen = {"a": {"video_id": vid(rel - 1, 5)}, "b": {"video_id": vid(rel, 7)},
                    "c": {"video_id": vid(rel + 86_400, 9)}, "d": {"video_id": "notanumber"}}
            n = pipeline.Run.drop_before_release(StubRun(), seen, [f"https://www.tiktok.com/music/x-{vid(rel, 1)}"])
        self.assertEqual(n, 1)
        self.assertEqual(sorted(seen), ["b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
