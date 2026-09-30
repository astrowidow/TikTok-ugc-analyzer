"""store.py の単体テスト（標準ライブラリの unittest。pytest 不要）。

実行: .venv/bin/python -m unittest tests.test_store -v

合成データで、実データでは踏めない境界を確かめる:
  エスケープの往復 / 13列目の名前違い / 同梱返信と返信APIの重複 / 混入の拒否 /
  CSVに無い動画 / 返信への返信 / 投稿者ラベルの2形式 / run_id の上書き
"""

import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402

VID = "7284621209680022785"
VID2 = "7284621209680022786"
VID_ONLY_JSONL = "7999999999999999999"


def comment(cid, aweme=VID, text="hi", reply_id="0", reply_to="0", **kw):
    c = {
        "cid": cid, "aweme_id": aweme, "text": text, "reply_id": reply_id,
        "reply_to_reply_id": reply_to, "create_time": 1700000000, "digg_count": 3,
        "reply_comment_total": 1 if reply_id == "0" else None,
        "user": {"uid": "6686719937756988417", "unique_id": "alice", "nickname": "Alice"},
        "author_pin": False, "is_author_digged": False, "comment_language": "ja",
        "sort_tags": '{"top_list":1}' if reply_id == "0" else None, "status": 1,
        "label_list": None, "image_list": None, "reply_comment": None,
    }
    c.update(kw)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ugc.db")
        self.out = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def write_csv(self, rows, head13_last="Song Name", name="videos_in.csv"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(store.FIXED_HEAD + [head13_last])
            w.writerows(rows)
        return p

    def write_jsonl(self, rows, name="c.jsonl"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def build(self, csv_rows, jsonl_rows, head13_last="Song Name", **kw):
        vc = self.write_csv(csv_rows, head13_last) if csv_rows is not None else None
        jl = self.write_jsonl(jsonl_rows)
        stats = store.ingest(self.db, "proj", vc, jl, run_id=kw.pop("run_id", "r1"), **kw)
        zp, meta = store.export_zip(self.db, stats["run_id"], out_dir=self.out)
        good, res = store.check_zip(zp, comments_jsonl=jl, videos_csv=vc)
        return stats, zp, meta, good, res, vc, jl

    @staticmethod
    def read_zip_csv(zp, name):
        with zipfile.ZipFile(zp) as z:
            rows = list(csv.reader(io.StringIO(z.read(name).decode("utf-8-sig"))))
        return rows[0], rows[1:]


def video_row(idx, vid, desc="", likes="10", plays="100", user="alice", song="song"):
    return [str(idx), f"https://www.tiktok.com/@{user}/video/{vid}", "2024-01-01 00:00:00", desc,
            likes, "1", "2", plays, "0", "0", "Video", user, song]


class TestEscapeRoundTrip(Base):
    def test_formula_prefix_escaped_in_csv_but_raw_in_sqlite(self):
        texts = ["=SUM(1)", "+1", "-1", "@alice", "'already", "a,b", 'say "hi"', "line1\nline2", "🎉絵文字", ""]
        jl = [{"video_id": VID, "status": "ok", "total": 10, "has_more": 0,
               "comments": [comment(f"70000000000000000{i:02d}", text=t) for i, t in enumerate(texts)]}]
        stats, zp, meta, good, res, vc, _ = self.build([video_row(0, VID, desc="=1+1")], jl)
        self.assertTrue(good, [r for r in res if not r[1]])

        _, crows = self.read_zip_csv(zp, "comments.csv")
        got = {r[0]: r[5] for r in crows}
        for i, t in enumerate(texts):
            cid = f"70000000000000000{i:02d}"
            expected = "'" + t if t[:1] in "=+-@" and t else t
            self.assertEqual(got[cid], expected, t)
            self.assertEqual(store.unescape_cell(got[cid]), t)
        # 既存13列の Description も同じ規則
        _, vrows = self.read_zip_csv(zp, "videos.csv")
        self.assertEqual(vrows[0][3], "'=1+1")
        self.assertEqual(meta["escaped_cells"].get("videos.Description"), 1)
        self.assertEqual(meta["escaped_cells"].get("comments.text"), 4)   # = + - @ の4件
        # SQLite には ' が付いていない
        conn = sqlite3.connect(self.db)
        raw = {r[0]: r[1] for r in conn.execute("SELECT comment_id, text FROM comments")}
        for i, t in enumerate(texts):
            self.assertEqual(raw[f"70000000000000000{i:02d}"], t)
        self.assertEqual(conn.execute("SELECT description FROM videos").fetchone()[0], "=1+1")

    def test_ids_are_text_in_sqlite_and_csv(self):
        jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]}]
        _, zp, _, good, res, _, _ = self.build([video_row(0, VID)], jl)
        self.assertTrue(good)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT typeof(video_id), typeof(comment_id), typeof(user_id) FROM comments").fetchone(),
                         ("text", "text", "text"))
        self.assertEqual(conn.execute("SELECT typeof(video_id), typeof(likes) FROM videos").fetchone(), ("text", "integer"))
        _, crows = self.read_zip_csv(zp, "comments.csv")
        self.assertEqual(crows[0][0], "7000000000000000001")
        self.assertEqual(crows[0][1], VID)


class TestHeaderPreservation(Base):
    def test_13th_column_name_kept_verbatim(self):
        for last in ("Song Name", "Project Name"):
            with self.subTest(last=last):
                self.db = os.path.join(self.tmp.name, f"{last}.db")
                jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]}]
                _, zp, _, good, res, _, _ = self.build([video_row(0, VID)], jl, head13_last=last, run_id=last)
                self.assertTrue(good, [r for r in res if not r[1]])
                vh, _ = self.read_zip_csv(zp, "videos.csv")
                self.assertEqual(vh[:13], store.FIXED_HEAD + [last])
                self.assertEqual(vh[13:], store.VIDEO_EXTRA)

    def test_wrong_header_rejected(self):
        p = os.path.join(self.tmp.name, "bad.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["URL", "Index"] + store.FIXED_HEAD[2:] + ["Song Name"])
            csv.writer(f).writerow(video_row(0, VID))
        with self.assertRaises(store.StoreError):
            store.read_videos_csv(p)

    def test_empty_numeric_cells_round_trip(self):
        # Photo 行は Shares 等が空。空のまま戻る（0 にしない・float にしない）
        row = ["0", f"https://www.tiktok.com/@u/photo/{VID}", "2024-01-01 00:00:00", "", "2098",
               "", "", "", "", "", "Photo", "", "song"]
        _, zp, _, good, res, _, _ = self.build([row], [])
        self.assertTrue(good, [r for r in res if not r[1]])
        _, vrows = self.read_zip_csv(zp, "videos.csv")
        self.assertEqual(vrows[0][:13], row)
        self.assertEqual(vrows[0][13:17], [VID, "0", "0", "skipped"])


class TestRepliesAndDedup(Base):
    def test_embedded_reply_deduped_against_reply_api(self):
        parent = comment("7000000000000000001", reply_comment=[
            comment("7000000000000000002", reply_id="7000000000000000001", label_type=1, label_text="投稿者"),
            comment("7000000000000000003", reply_id="7000000000000000001"),
        ])
        jl = [{"video_id": VID, "status": "ok", "comments": [parent],
               "reply_comments": [comment("7000000000000000002", reply_id="7000000000000000001",
                                          reply_to="0", label_list=[{"type": 1, "text": "投稿者"}])]}]
        stats, zp, _, good, res, _, _ = self.build([video_row(0, VID)], jl)
        self.assertTrue(good, [r for r in res if not r[1]])
        self.assertEqual(stats["comments"]["by_source"], {"main": 1, "reply_api": 1, "embedded": 1})
        self.assertEqual(stats["comments"]["skipped_duplicate_cid"]["embedded"], 1)
        ch, crows = self.read_zip_csv(zp, "comments.csv")
        by = {r[0]: dict(zip(ch, r)) for r in crows}
        self.assertEqual(by["7000000000000000002"]["source"], "reply_api")
        self.assertEqual(by["7000000000000000002"]["is_author"], "1")   # label_list 形式
        self.assertEqual(by["7000000000000000003"]["source"], "embedded")
        self.assertEqual(by["7000000000000000003"]["is_author"], "0")
        self.assertEqual(by["7000000000000000002"]["parent_comment_id"], "7000000000000000001")
        self.assertEqual(by["7000000000000000002"]["level"], "2")
        # 本体の直後にその返信が並ぶ
        self.assertEqual([r[0] for r in crows], ["7000000000000000001", "7000000000000000002", "7000000000000000003"])
        _, vrows = self.read_zip_csv(zp, "videos.csv")
        self.assertEqual(vrows[0][14:16], ["1", "2"])   # comments_fetched / replies_fetched

    def test_reply_to_reply_keeps_top_parent_and_records_target(self):
        jl = [{"video_id": VID, "status": "ok",
               "comments": [comment("7000000000000000001")],
               "reply_comments": [
                   comment("7000000000000000002", reply_id="7000000000000000001"),
                   comment("7000000000000000003", reply_id="7000000000000000001", reply_to="7000000000000000002"),
               ]}]
        _, zp, _, good, res, _, _ = self.build([video_row(0, VID)], jl)
        self.assertTrue(good, [r for r in res if not r[1]])
        ch, crows = self.read_zip_csv(zp, "comments.csv")
        by = {r[0]: dict(zip(ch, r)) for r in crows}
        self.assertEqual(by["7000000000000000003"]["parent_comment_id"], "7000000000000000001")
        self.assertEqual(by["7000000000000000003"]["reply_to_comment_id"], "7000000000000000002")
        self.assertEqual(by["7000000000000000002"]["reply_to_comment_id"], "")

    def test_embedded_label_type_marks_author(self):
        parent = comment("7000000000000000001", reply_comment=[
            comment("7000000000000000002", reply_id="7000000000000000001", label_type=1, label_text="投稿者")])
        jl = [{"video_id": VID, "status": "ok", "comments": [parent]}]
        _, zp, _, good, _, _, _ = self.build([video_row(0, VID)], jl)
        ch, crows = self.read_zip_csv(zp, "comments.csv")
        by = {r[0]: dict(zip(ch, r)) for r in crows}
        self.assertEqual(by["7000000000000000002"]["is_author"], "1")


class TestVideoMatching(Base):
    def test_mixed_aweme_id_is_rejected(self):
        jl = [{"video_id": VID, "status": "ok",
               "comments": [comment("7000000000000000001"), comment("7000000000000000002", aweme=VID2)]}]
        vc = self.write_csv([video_row(0, VID)])
        with self.assertRaises(store.StoreError) as cm:
            store.ingest(self.db, "proj", vc, self.write_jsonl(jl), run_id="r1")
        self.assertIn("混入", str(cm.exception))
        # DB を開く前に止まるので、何も書かれない
        self.assertFalse(os.path.exists(self.db))

    def test_video_only_in_jsonl_is_appended_with_blank_13_columns(self):
        jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]},
              {"video_id": VID_ONLY_JSONL, "status": "ok", "total": 5, "has_more": 1, "top_list": 1,
               "comments": [comment("7000000000000000009", aweme=VID_ONLY_JSONL)]},
              {"video_id": VID2, "status": "no_comments", "comments": []}]
        stats, zp, _, good, res, _, _ = self.build([video_row(0, VID), video_row(1, VID2), video_row(2, "7000000000000000777")], jl)
        self.assertTrue(good, [r for r in res if not r[1]])
        self.assertEqual(stats["videos"], {**stats["videos"], "in_csv": 3, "in_jsonl": 3, "matched": 2,
                                           "only_in_csv": 1, "only_in_jsonl": 1, "total_rows": 4})
        vh, vrows = self.read_zip_csv(zp, "videos.csv")
        self.assertEqual([r[13] for r in vrows], [VID, VID2, "7000000000000000777", VID_ONLY_JSONL])
        self.assertEqual(vrows[3][:13], [""] * 13)
        st = dict(zip([r[13] for r in vrows], [r[16] for r in vrows]))
        self.assertEqual(st, {VID: "ok", VID2: "empty", "7000000000000000777": "skipped", VID_ONLY_JSONL: "ok"})
        rec = dict(zip(vh, vrows[3]))
        self.assertEqual((rec["comments_total_reported"], rec["comments_has_more"], rec["top_list_count"]), ("5", "1", "1"))

    def test_duplicate_jsonl_lines_last_wins(self):
        jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]},
              {"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001"), comment("7000000000000000002")]}]
        stats, _, _, good, res, _, _ = self.build([video_row(0, VID)], jl)
        self.assertTrue(good, [r for r in res if not r[1]])
        self.assertEqual(stats["videos"]["jsonl_duplicate_rows"], 1)
        self.assertEqual(stats["comments"]["level1"], 2)

    def test_same_run_id_needs_replace(self):
        jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]}]
        vc = self.write_csv([video_row(0, VID)])
        jlp = self.write_jsonl(jl)
        store.ingest(self.db, "proj", vc, jlp, run_id="r1")
        with self.assertRaises(store.StoreError):
            store.ingest(self.db, "proj", vc, jlp, run_id="r1")
        store.ingest(self.db, "proj", vc, jlp, run_id="r1", replace=True)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM comments").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_two_runs_join_on_comment_id_for_time_series(self):
        vc = self.write_csv([video_row(0, VID)])
        for run, likes in (("day1", 3), ("day2", 8)):
            jl = self.write_jsonl([{"video_id": VID, "status": "ok",
                                    "comments": [comment("7000000000000000001", digg_count=likes)]}], name=f"{run}.jsonl")
            store.ingest(self.db, "proj", vc, jl, run_id=run)
        conn = sqlite3.connect(self.db)
        rows = conn.execute("""
            SELECT a.comment_id, b.like_count - a.like_count FROM comments a
            JOIN comments b ON a.comment_id = b.comment_id AND a.run_id='day1' AND b.run_id='day2'""").fetchall()
        self.assertEqual(rows, [("7000000000000000001", 5)])


class TestCheckDetectsBreakage(Base):
    def test_check_fails_when_csv_tampered(self):
        jl = [{"video_id": VID, "status": "ok", "comments": [comment("7000000000000000001")]}]
        _, zp, _, good, _, vc, jlp = self.build([video_row(0, VID)], jl)
        self.assertTrue(good)
        # comments.csv の video_id を壊した ZIP を作り直す
        with zipfile.ZipFile(zp) as z:
            files = {n: z.read(n) for n in z.namelist()}
        files["comments.csv"] = files["comments.csv"].replace(VID.encode(), b"1")
        bad = os.path.join(self.tmp.name, "bad.zip")
        with zipfile.ZipFile(bad, "w") as z:
            for n, b in files.items():
                z.writestr(n, b)
        good, res = store.check_zip(bad, comments_jsonl=jlp, videos_csv=vc)
        self.assertFalse(good)
        self.assertIn("孤児", " ".join(n for n, g, _ in res if not g))


if __name__ == "__main__":
    unittest.main()
