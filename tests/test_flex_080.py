"""0.8.0: 頼まれたやり直しに応える・取れて当然の情報を出す（docs/PLAN_0_8.md）。道具の本体（proto_runner・analysis_info・acquire/launch）。

2026-10-12 友達の会話をそのまま確かめる: 0.6.4 で集め終わった・書く前のきゃわに、「最初から」「この音源も入れて」「3ページまとめて」が
どの道具でも通らなかった（start_analysis は前の分析に戻し、restart・cancel は「取得はもう終わっています」で断った）。
「自己紹介の音源は入ってる？」に、道具で答えられず推測で答えた。

TikTok・Chrome・取得の係・アプリには触らない（取得アプリの形 proto_runner.LOCAL は偽物）。置き場は一時フォルダ。

  collector/.venv/bin/python -m unittest tests.test_flex_080 -v
"""
import json
import unittest

from tests.test_review_r3 import U1, U2, U3, Base, _page  # noqa: F401
import analysis_info  # noqa: E402
import flow_w1  # noqa: E402
import proto_runner as pr  # noqa: E402
from acquire import launch  # noqa: E402

KUREA = "https://www.tiktok.com/music/%E3%82%AA%E3%83%AA%E3%82%B8%E3%83%8A%E3%83%AB%E6%A5%BD%E6%9B%B2-7643306519803906823"


class FlexBase(Base):
    def old_kyawa(self, aid="a20261006-1530-3a52", complete=False, urls=(U1, U2), extra=None):
        """友達の分析の形: 0.6.4 で集め終わった（音源探しの記録・作った版が無い）・レポートは書く前"""
        self.make(aid, "きゃわぽっぴんどぅー", "done", "2026-10-06T15:30:00+09:00", complete=complete, artist="iLiFE!",
                  music_url=urls[0], extra={"music_urls": list(urls), **(extra or {})})
        raw = self.base / aid / "raw"
        (raw / "enriched.jsonl").write_text("".join(json.dumps({"video_id": str(i)}) + "\n" for i in range(5)), encoding="utf-8")
        (raw / "comments.jsonl").write_text(json.dumps({"video_id": "1", "status": "ok", "fetched": 20}) + "\n", encoding="utf-8")
        (raw / "covers").mkdir(exist_ok=True)
        (raw / "covers" / "1.jpg").write_bytes(b"x")
        (raw / "music_pages.json").write_text(json.dumps([
            {"url": urls[0], "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!【あいらいふ】", "video_count": 31100, "video_count_text": "31.1K 動画"},
            {"url": urls[1], "title": "きゃわぽっぴんどぅー", "creator": "iLiFE!【あいらいふ】", "video_count": 17700, "video_count_text": "17.7K 動画"},
        ], ensure_ascii=False), encoding="utf-8")
        return aid


# ---------------------------------------------------------------------------
# 友達の行き止まり（2026-10-12 の会話）
# ---------------------------------------------------------------------------
class TestFriendDeadEnd(FlexBase):
    def test_add_page_to_acquired_unwritten(self):
        """「この音源も入れて」: 集め終わった・書く前の分析に、ページを足して取り直す。前の分析は残り、属性・コメント・サムネを使い回す"""
        old = self.old_kyawa()
        r = pr.restart_analysis("u1", "きゃわぽっぴんどぅー", add_music_urls=[KUREA])
        new = r["analysis_id"]
        self.assertTrue(new and new != old, r["text"])
        m = self.meta(new)
        self.assertEqual(m["music_urls"], [U1, U2, KUREA])
        self.assertEqual(self.meta(old)["superseded_by"], new)
        self.assertEqual(self.meta(old)["acquisition"]["status"], "done")   # 前の取得は「やめた」にしない
        self.assertTrue((self.base / new / "raw" / "enriched.jsonl").exists())
        self.assertTrue((self.base / new / "raw" / "comments.jsonl").exists())
        self.assertTrue((self.base / new / "raw" / "covers" / "1.jpg").exists())
        self.assertIn("取り直します", r["text"])
        self.assertIn("消さずに残してあります", r["text"])
        # 前の分析は一覧・曲名での引き当てから外れ、新しい分析が選ばれる
        self.assertEqual(pr.resolve("u1", "きゃわぽっぴんどぅー").id, new)
        ids = [i["analysis_id"] for i in pr.status("u1")["analyses"]]
        self.assertEqual(ids, [new])
        self.assertIn(old, pr.status("u1", old)["analyses"][0]["analysis_id"])

    def test_start_with_three_pages_is_not_pulled_back(self):
        """「3ページまとめて」: start_analysis に3ページを渡すと、前の2ページの分析に戻さずに取り直す（曲名を変えなくても）"""
        old = self.old_kyawa()
        r = pr.start_analysis("u1", "きゃわぽっぴんどぅー", "iLiFE!", music_urls=[U1, U2, KUREA])
        self.assertNotEqual(r.get("analysis_id"), old, r["text"])
        self.assertNotIn("このまま書き始めます", r["text"])
        self.assertEqual(self.meta(r["analysis_id"])["music_urls"], [U1, U2, KUREA])
        self.assertEqual(self.meta(old)["superseded_by"], r["analysis_id"])

    def test_same_pages_still_resume(self):
        """何も違わない「分析して」は、今までどおり集め終わった分析から書き始める（決定 B）"""
        old = self.old_kyawa()
        r = pr.start_analysis("u1", "きゃわぽっぴんどぅー", "iLiFE!")
        self.assertEqual((r["analysis_id"], r.get("kind")), (old, "resume"))

    def test_fresh_from_start(self):
        """「最初から分析し直して」: fresh なら、同じ曲の集め終わった分析があっても新しく集める"""
        old = self.old_kyawa()
        self.fake.sounds = [{"music_url": U1, "title": "きゃわぽっぴんどぅー", "author": "iLiFE!"}] * 3
        r = pr.start_analysis("u1", "きゃわぽっぴんどぅー", "iLiFE!", fresh=True)
        self.assertTrue(r.get("analysis_id") and r["analysis_id"] != old, r["text"])
        self.assertEqual(self.meta(old)["superseded_by"], r["analysis_id"])

    def test_restart_without_args_is_from_start(self):
        """restart_analysis を何も付けずに → 最初から（楽曲ページ探しから）。集め終わった分析でも断らない"""
        old = self.old_kyawa()
        self.fake.sounds = [{"music_url": U1, "title": "きゃわぽっぴんどぅー", "author": "iLiFE!"}] * 3
        r = pr.restart_analysis("u1", old)
        self.assertTrue(r.get("analysis_id") and r["analysis_id"] != old, r["text"])
        self.assertIn("楽曲ページ探しから最初に取り直します", r["text"])
        self.assertFalse((self.base / r["analysis_id"] / "raw" / "enriched.jsonl").exists())   # 最初からは使い回さない

    def test_restart_completed_analysis(self):
        """完成済みでも取り直せる。前のレポートの分析は残る"""
        old = self.old_kyawa(complete=True)
        r = pr.restart_analysis("u1", old, add_music_urls=[KUREA])
        self.assertTrue(r["analysis_id"] and r["analysis_id"] != old)

    def test_remove_page_and_read_more(self):
        old = self.old_kyawa()
        r = pr.restart_analysis("u1", old, remove_music_urls=[U2], read_more="all")
        m = self.meta(r["analysis_id"])
        self.assertEqual(m["music_urls"], [U1])
        self.assertEqual(m["acquisition_settings"]["read_select"], 0)
        with self.assertRaises(pr.RunnerError):
            pr.restart_analysis("u1", r["analysis_id"], remove_music_urls=[U1])   # 1つも残らない

    def test_replies_does_not_reuse_comments(self):
        old = self.old_kyawa()
        r = pr.restart_analysis("u1", old, replies=True)
        self.assertFalse((self.base / r["analysis_id"] / "raw" / "comments.jsonl").exists())
        self.assertTrue((self.base / r["analysis_id"] / "raw" / "enriched.jsonl").exists())

    def test_resume_failed_same_analysis(self):
        aid = self.make("a20261012-0900-fail", "テスト", "failed")
        r = pr.restart_analysis("u1", aid, resume=True)
        self.assertEqual(r["analysis_id"], aid)
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "queued")

    def test_superseded_points_to_new(self):
        old = self.old_kyawa()
        r = pr.restart_analysis("u1", old, add_music_urls=[KUREA])
        with self.assertRaises(pr.RunnerError) as e:
            pr.restart_analysis("u1", old, add_music_urls=[U3])
        self.assertIn(r["analysis_id"], str(e.exception))

    def test_many_user_pages_not_cut(self):
        """利用者が渡したページは6つで切らない"""
        urls = [f"https://www.tiktok.com/music/p{i}-70000000000000001{i:02d}" for i in range(8)]
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=urls)
        self.assertEqual(len(self.meta(r["analysis_id"])["music_urls"]), 8)

    def test_no_limit_on_active(self):
        """頼める数で断らない（順番に並べる）"""
        for i in range(3):
            self.make(f"a2026101{i}-0900-act{i}", f"曲{i}", "queued")
        r = pr.start_analysis("u1", "新しい曲", "だれか", music_urls=[U3])
        self.assertTrue(r["analysis_id"])

    def test_page_choice_written(self):
        r = pr.start_analysis("u1", "テスト", "だれか", music_urls=[U1, U2])
        pc = json.loads((self.base / r["analysis_id"] / "raw" / "page_choice.json").read_text(encoding="utf-8"))
        self.assertEqual([c["url"] for c in pc["chosen"]], [U1, U2])


class TestFanPageGiven(FlexBase):
    def test_official_page_is_main_and_fan_is_marked(self):
        """利用者が渡したページにファンの音源（作者がアーティストと違う）があっても、UGC が多い公式のページを主にし、ファンの音源に印を付ける
        （2026-10-12 の確かめ: 쿠레아 38.6K が主になり、主の作者を本人とみなす部品があった）"""
        old = self.old_kyawa()
        self.fake.pages = {U1.rsplit("-", 1)[-1]: _page("きゃわぽっぴんどぅー", "iLiFE!【あいらいふ】", 31100),
                           U2.rsplit("-", 1)[-1]: _page("きゃわぽっぴんどぅー", "iLiFE!【あいらいふ】", 17700),
                           KUREA.rsplit("-", 1)[-1]: _page("オリジナル楽曲 - 쿠레아", "쿠레아", 38600)}
        r = pr.restart_analysis("u1", old, add_music_urls=[KUREA])
        m = self.meta(r["analysis_id"])
        self.assertEqual(m["music_urls"][0], U1)
        self.assertEqual(m["music_url"], U1)
        self.assertEqual(m["fan_music_urls"], [KUREA])
        pages = json.loads((self.base / r["analysis_id"] / "raw" / "music_pages.json").read_text(encoding="utf-8"))
        self.assertEqual([p.get("kind") for p in pages], [None, None, "fan"])

    def test_cancel_superseding_restores_old(self):
        """取り直しの新しい分析を「やめて」と言われたら、前の分析を元に戻す（前のデータで続けられる）"""
        old = self.old_kyawa()
        r = pr.restart_analysis("u1", old, add_music_urls=[KUREA])
        c = pr.cancel_analysis("u1", r["analysis_id"])
        self.assertNotIn("superseded_by", self.meta(old))
        self.assertIn("前の分析", c["text"])
        self.assertEqual(pr.start_analysis("u1", "きゃわぽっぴんどぅー", "iLiFE!")["analysis_id"], old)


class TestPoolArtist(unittest.TestCase):
    def test_artist_overrides_most_used_sound_author(self):
        """コメントを取る動画の部品: アーティスト名を渡せば、一番多く使われた音源の作者（ファン）でなく、その名前で本人を見分ける"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
        import pool
        fan = {"id": "9", "title": "オリジナル楽曲 - 쿠레아", "author": "쿠레아"}
        enriched = {}
        videos = []
        for i, (uid, music) in enumerate([("ilife_official", {"id": "1", "title": "きゃわ", "author": "iLiFE!"})]
                                         + [(f"fan{i}", fan) for i in range(5)] + [("kurea", fan)]):
            vid = str(100 + i)
            nick = "쿠레아" if uid == "kurea" else uid   # ファンの音源の作者のアカウント
            enriched[vid] = {"video_id": vid, "music": music, "author": {"unique_id": uid, "nickname": nick, "verified": uid == "ilife_official",
                                                                         "follower_count": 50000}}
            videos.append({"video_id": vid, "date": "2026-06-01", "week": "2026-W22", "plays": 100000, "username": uid})
        alive = [v for v in videos]
        self.assertIn("kurea", pool.artist_accounts(alive, enriched, pool.song_info(enriched)["author"]))
        self.assertEqual(pool.artist_accounts(alive, enriched, "iLiFE!"), {"ilife_official"})


class TestSameSong(FlexBase):
    def test_other_artist_same_title(self):
        """同名の別の曲（アーティスト違い）は前の分析に吸われない"""
        self.make("a20261001-0000-kana", "シルエット", "done", artist="KANA-BOON", complete=False)
        m = self.meta("a20261001-0000-kana")
        self.assertFalse(launch._same_song(m, "シルエット", [], "Aimer"))
        self.assertTrue(launch._same_song(m, "シルエット", [], "KANA-BOON"))
        self.assertTrue(launch._same_song(m, "シルエット", [], ""))
        self.assertTrue(launch._same_song(m, "別名", [U1], "Aimer"))   # 同じ楽曲ページなら同じ曲


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
class TestInfo(FlexBase):
    def test_sounds_not_searched_says_so(self):
        """友達の質問「自己紹介の音源は入ってないよね？」: 音源探しの記録が無い分析は「探していない」とはっきり答える"""
        old = self.old_kyawa()
        t = pr.info("u1", old, "sounds", KUREA)["text"]
        self.assertIn("探していない", t)
        self.assertIn("入っていない", t)
        self.assertIn("add_music_urls", t)
        self.assertIn("推測で埋めない", t)

    def test_sounds_searched_lists_joined_and_dropped(self):
        old = self.old_kyawa(urls=(U1, U2, KUREA), extra={"fan_music_urls": [KUREA]})
        (self.base / old / "raw" / "sound_search.json").write_text(json.dumps({
            "words": ["きゃわぽっぴんどぅー"], "videos": 60, "candidates": 2, "main_video_count": 31100, "at": "2026-10-08T00:50:00+09:00",
            "checked": [{"id": "7643306519803906823", "title": "オリジナル楽曲 - 쿠레아", "author": "쿠레아", "duration": 12,
                         "video_count": 38900, "video_count_text": "38.9K 動画", "url": KUREA, "joined": True, "match": "曲の番号"},
                        {"id": "7643423091901434642", "title": "オリジナル楽曲 - なぎ", "author": "なぎ", "duration": 190,
                         "video_count": 8581, "video_count_text": "8581 動画", "url": "https://www.tiktok.com/music/x-7643423091901434642",
                         "joined": False, "match": "曲の番号"}]}, ensure_ascii=False), encoding="utf-8")
        t = pr.info("u1", old, "sounds", KUREA)["text"]
        self.assertIn("入れた: 『オリジナル楽曲 - 쿠레아』", t)
        self.assertIn("外した（30%未満）: 『オリジナル楽曲 - なぎ』", t)
        self.assertIn("入っている（音源C）", t)

    def test_overview_without_version_record(self):
        old = self.old_kyawa()
        t = pr.info("u1", old, "overview")["text"]
        self.assertIn("記録なし（0.8.0 より前の版で作った分析）", t)
        self.assertIn("31.1K", t)
        self.assertIn("受け付けのときに外した楽曲ページ", t)

    def test_video_lookup(self):
        old = self.old_kyawa()
        d = self.base / old
        (d / "derived" / "llm_input").mkdir(parents=True, exist_ok=True)
        (d / "derived" / "llm_input" / "records.jsonl").write_text(json.dumps(
            {"seq": 7, "video_id": "1", "date": "2026-06-23", "plays": 120000, "sound": "A", "enriched": True}) + "\n", encoding="utf-8")
        t = pr.info("u1", old, "video", "https://www.tiktok.com/@a/video/1")["text"]
        self.assertIn("詳しく読んだ（seq 7", t)
        self.assertIn("コメント: 20件取った", t)
        t = pr.info("u1", old, "video", "https://www.tiktok.com/@a/video/999")["text"]
        self.assertIn("記録には無い", t)

    def test_app_without_local_info(self):
        t = pr.info("u1", None, "app")["text"]
        self.assertIn("スクリプトエディタ", t)

    def test_bad_about(self):
        with self.assertRaises(pr.RunnerError):
            pr.info("u1", None, "nanika")


class TestArchive(FlexBase):
    def test_archive_and_restore(self):
        old = self.old_kyawa(complete=True)
        pr.archive_analysis("u1", old)
        self.assertEqual(pr.status("u1")["analyses"], [])
        self.assertIn("やめた・取り直した・しまった分析が 1件", pr.status("u1")["text"])
        pr.archive_analysis("u1", "きゃわぽっぴんどぅー", restore=True)
        self.assertEqual(len(pr.status("u1")["analyses"]), 1)


# ---------------------------------------------------------------------------
# 途中の頼みを積む・「続けて」の選び方・取り足しをやめる
# ---------------------------------------------------------------------------
class TestQueueAndPick(FlexBase):
    def test_revise_during_writing_is_queued_then_started_at_done(self):
        aid = self.old_kyawa(complete=False)
        r = pr.revise("u1", aid, "3章に音楽面の話を足して")
        self.assertEqual(r["queued"], "revise")
        st = json.loads((self.base / aid / "state" / "tasks.json").read_text(encoding="utf-8"))
        for t in st["tasks"]:
            if t["type"] != "done":
                t["status"] = "done"
        (self.base / aid / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        orig = pr._render
        pr._render = lambda a, t, st=None: {"text": "完了の知らせ", "catalog": []}
        try:
            n = pr.next_task("u1", aid)
        finally:
            pr._render = orig
        self.assertIn("途中で頼まれていたレポートの直し", n["text"])
        st = json.loads((self.base / aid / "state" / "tasks.json").read_text(encoding="utf-8"))
        self.assertTrue(any(t["type"] == "revise" and t["status"] == "pending" for t in st["tasks"]))
        self.assertEqual(pr._pending(pr.Analysis(aid)), [])

    def test_next_task_without_name_prefers_writable(self):
        w = self.old_kyawa(complete=False)
        self.make("a20261012-1000-acqu", "別の曲", "running", "2026-10-12T10:00:00+09:00")
        self.assertEqual(pr._pick_for_work("u1").id, w)

    def test_cancel_deepen(self):
        aid = self.old_kyawa(complete=True, extra={"deepen": {"round": 1, "status": "running", "community": "x", "targets": [1]}})
        r = pr.cancel_analysis("u1", aid)
        self.assertIn("取り足しをやめました", r["text"])
        self.assertEqual(self.meta(aid)["deepen"]["status"], "cancelled")
        self.assertEqual(self.meta(aid)["acquisition"]["status"], "done")
        self.assertEqual(self.fake.stopped, [aid])


class TestReadNames(FlexBase):
    def test_unknown_name_lists_names(self):
        aid = self.old_kyawa(complete=True)
        with self.assertRaises(pr.RunnerError) as e:
            pr.read("u1", aid, "nanika")
        self.assertIn("読める名前", str(e.exception))
        self.assertIn("info", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class TestVerifyMemo(unittest.TestCase):
    def test_play_claim_does_not_cross_closing_paren(self):
        """検算: 「seq 637（…2,100,000再生））や…（seq 598」を、seq 598 の再生数と読み違えない（2026-10-12 のきゃわで2件の誤報）"""
        t = ("コストコの新商品紹介（seq 637（@tekoteko_goo、2026-09-23、2,100,000再生））や建築の勉強量の比較"
             "（seq 598（@kenchikushi_sho、2026-09-06、2,400,000再生））など。")
        self.assertEqual({(s, n) for s, n, *_ in flow_w1.play_claims(t, bounds=True)}, {(637, "2,100,000"), (598, "2,400,000")})

    def test_memo_gets_one_verify_section(self):
        """検算の結果は確認メモの最後に1つだけ（何度検算しても重ならない。合えば消える）"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "EDITOR_NOTES.md").write_text("# 確認メモ\n\n## 推測で書いたところ\n- x\n", encoding="utf-8")
            fake = type("A", (), {"outputs": lambda self, *p: out.joinpath(*p)})()
            flow_w1.memo_verify(fake, ["存在しない動画への言及: seq 9"], ["seq 1 の再生数"])
            flow_w1.memo_verify(fake, ["存在しない動画への言及: seq 9"], [])
            t = (out / "EDITOR_NOTES.md").read_text(encoding="utf-8")
            self.assertEqual(t.count(flow_w1.VERIFY_MEMO_HEAD), 1)
            self.assertIn("seq 9", t)
            self.assertNotIn("seq 1 の再生数", t)
            flow_w1.memo_verify(fake, [], [])
            self.assertNotIn(flow_w1.VERIFY_MEMO_HEAD, (out / "EDITOR_NOTES.md").read_text(encoding="utf-8"))
