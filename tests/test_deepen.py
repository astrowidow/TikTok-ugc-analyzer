"""完成後の界隈の掘り下げ（docs/DEEPEN_COMMUNITY.md）の試験。TikTok にも AI にも本物の置き場にも触らない。

- acquire/deepen.py: 取る動画の決め方（新しく取る・続きを取る・外す）と時間での配り方、原本への合わせ方
- 道具 deepen → 取り足しの待ち → dcomments → outline_revise → 書き直し → review → 仕上げ → 組み立て・検算 → 完了、を作り物の出力で通す
- 取得の係の待ち行列に、掘り下げの取り足しが先に並ぶこと

  python3 -m unittest tests.test_deepen
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1  # noqa: E402
import proto_runner as pr  # noqa: E402
from acquire import deepen, pipeline, worker  # noqa: E402

AID = "a20261006-0000-test"
CH = ["intro", "path_P1", "path_P2", "branch", "music", "result"]
HEAD = {"intro": "# 【テスト曲 / だれか】Hitの理由分析レポート", "path_P1": "## 1. バズの拡大経路",
        "path_P2": "### (2) 広がり（2025-08-01〜2025-08-31）", "branch": "### ここまでの拡大経路をまとめると",
        "music": "## 3. 楽曲の音楽的特徴", "result": "## 6. バズった結果得られたもの"}
# seq → (界隈, 再生, 取得の状態)。ok40m = 上限40で止まり続きあり、ok25 = 上位リストを取り切った
VIDEOS = {0: ("dancer", 3_000_000, "ok40m"), 1: ("dancer", 1_000_000, "ok25"), 2: ("dancer", 800_000, "no_video"),
          3: ("dancer", 500_000, None), 4: ("dancer", 50_000, None), 5: ("dancer", 2_000_000, None),
          6: ("student", 900_000, "ok40m"), 7: ("student", 700_000, None)}


def vid(s):
    return f"70000000000000{s:05d}"


def comment(v, i, likes=1):
    return {"cid": f"75{v[-5:]}{i:012d}", "text": f"コメント{i} すごい", "digg_count": likes, "aweme_id": v,
            "comment_language": "ja", "user": {"nickname": "u"}, "sort_tags": json.dumps({"top_list": 1})}


def row(s, n, start=0, **kw):
    v = vid(s)
    r = {"video_id": v, "status": "ok", "comments": [comment(v, i, 100 - i) for i in range(start, start + n)],
         "cap": 40, "has_more": 1, "top_list": n, "total": 300}
    r.update(kw)
    return r


def write_jsonl(p: Path, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def chapter_md(ch, seqs=(), extra=""):
    body = "".join(f"seq {s}（@u、2025-07-0{s % 9 + 1}、100万再生）の動画が伸びた。" for s in seqs)
    return HEAD[ch] + "\n\n" + (body + "本文の段落。" * 80) + extra + "\n"


def make_analysis(base: Path) -> Path:
    d = base / AID
    recs = []
    for s, (_, plays, _) in VIDEOS.items():
        date = f"2025-07-{s + 1:02d}" if s < 4 else f"2025-08-{s + 1:02d}"
        recs.append({"seq": s, "video_id": vid(s), "date": date, "week": "2025-W30", "plays": plays, "likes": plays // 10,
                     "comments": 300, "enriched": True, "author": {"id": f"user{s}", "nickname": "u"},
                     "url": f"https://www.tiktok.com/@user{s}/video/{vid(s)}", "desc": "説明"})
    write_jsonl(d / "derived" / "llm_input" / "records.jsonl", recs)
    write_jsonl(d / "derived" / "videos.jsonl", recs)
    write_jsonl(d / "raw" / "grid_links.jsonl", [{"video_id": vid(s), "source": 1} for s in VIDEOS])
    (d / "derived" / "weekly.tsv").write_text("week\tn\tplays\n2025-W30\t8\t9000000\n", encoding="utf-8")
    rows = []
    for s, (_, _, how) in VIDEOS.items():
        if how == "ok40m":
            rows.append(row(s, 40))
        elif how == "ok25":
            rows.append(row(s, 25, has_more=0, top_list=20))
        elif how == "no_video":
            rows.append({"video_id": vid(s), "status": "no_video"})
    write_jsonl(d / "raw" / "comments.jsonl", rows)
    (d / "outputs").mkdir(parents=True)
    with open(d / "outputs" / "labels.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["seq", "community", "format", "motive", "region", "tier", "conf", "reason"])
        for s, (k, _, _) in VIDEOS.items():
            w.writerow([s, k, "dance", "fun", "JP", "general", "H", "r"])
    (d / "outputs" / "taxonomy.json").write_text(json.dumps({
        "community": {"dancer": "ダンスを主な発信内容にしている人。屋外で踊る人も含む", "student": "一般の中高生。", "unknown": "x"},
        "format": {"dance": "d"}, "motive": {"fun": "m"}}, ensure_ascii=False), encoding="utf-8")
    (d / "outputs" / "phases.json").write_text(json.dumps({"phases": [
        {"id": "P1", "name": "立ち上がり", "start": "2025-07-01", "end": "2025-07-31", "summary": "s1", "reps": [0]},
        {"id": "P2", "name": "広がり", "start": "2025-08-01", "end": "2025-08-31", "summary": "s2", "reps": [6]}]},
        ensure_ascii=False), encoding="utf-8")
    for ch in CH:
        seqs = (0, 1) if ch == "path_P1" else (6,) if ch == "path_P2" else ()
        (d / "outputs" / "chapters").mkdir(parents=True, exist_ok=True)
        (d / "outputs" / "chapters" / f"{ch}.md").write_text(chapter_md(ch, seqs), encoding="utf-8")
        (d / "outputs" / "note_chapters").mkdir(parents=True, exist_ok=True)
        (d / "outputs" / "note_chapters" / f"{ch}.md").write_text(HEAD[ch] + "\n\n" + "仕上げた本文。" * 40 + "\n", encoding="utf-8")
    (d / "outputs" / "outline.json").write_text(json.dumps({"thesis": "全体の主張", "title_idea": "題", "chapters": [
        {"id": ch, "role": "役割", "claims": [{"claim": "主張", "evidence": [{"seq": 0}]}], "bridge": "つなぎ"} for ch in CH]},
        ensure_ascii=False), encoding="utf-8")
    (d / "outputs" / "synthesis").mkdir(parents=True)
    (d / "outputs" / "synthesis" / "dancer.md").write_text("### dancer\n#### コメントの全体傾向\n前回の傾向\n", encoding="utf-8")
    (d / "outputs" / "note_meta.json").write_text(json.dumps({"title": "題", "editor_notes_md": "メモ"}, ensure_ascii=False),
                                                  encoding="utf-8")
    st = {"analysis_id": AID, "flow": "w1", "next_n": 3, "tasks": [
        {"n": 1, "task_id": f"{AID}/001-write", "type": "write", "kind": "ai", "title": "執筆", "params": {}, "status": "done",
         "first_issued_at": None, "issued_at": None, "issue_count": 1, "done_at": "x", "rejects": 0},
        {"n": 2, "task_id": f"{AID}/002-done", "type": "done", "kind": "done", "title": "完了", "params": {}, "status": "done",
         "first_issued_at": None, "issued_at": None, "issue_count": 1, "done_at": "x", "rejects": 0}]}
    (d / "state").mkdir(parents=True)
    (d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    (d / "analysis.json").write_text(json.dumps({
        "analysis_id": AID, "owner": "u1", "title": "テスト曲", "song": {"title": "テスト曲", "artist": "だれか"},
        "created_at": "2026-10-06T00:00:00+09:00", "acquisition": {"status": "done"}, "acquisition_settings": {}},
        ensure_ascii=False), encoding="utf-8")
    deepen.prep(d)   # derived/comments を作る（本線の comments_md と同じ）
    return d


class FakeLocal:
    def __init__(self):
        self.ensured = 0

    def acquisition_settings(self):
        return {"chrome_port": "9250"}

    def ensure_app(self):
        self.ensured += 1
        return {"started": False}

    def app_state(self):
        return {"running": True, "login_wanted": False}


class TestPlanAndMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_analysis(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates(self):
        c = deepen.candidates(self.d, "dancer")
        self.assertEqual([x["seq"] for x in c["new"]], [5, 3])          # 再生の多い順。4 は下限（10万）未満、2 は削除済み
        self.assertEqual([x["seq"] for x in c["more"]], [0])            # 1 は上位リストを取り切った（続きを取っても増えない）
        self.assertEqual(c["skipped"], {"below_min_plays": 1, "gone": 1})
        self.assertEqual(deepen.candidates(self.d, "dancer", min_plays=0)["new"][-1]["seq"], 4)

    def test_plan_fits_budget(self):
        pl = deepen.plan(self.d, "dancer")
        self.assertEqual((pl["n_new"], pl["n_more"]), (2, 1))
        E = deepen.EST
        self.assertAlmostEqual(pl["est_min"], round(E["page_min"] + E["new_min"] * 2 + E["more_min"], 1))
        small = deepen.plan(self.d, "dancer", minutes=E["page_min"] + E["new_min"] + 0.5)   # 準備＋1本しか入らない
        self.assertEqual((small["n_new"], small["n_more"], small["left_new"], small["left_more"]), (1, 0, 1, 1))
        self.assertEqual(small["targets"][0]["seq"], 5)

    def test_merge_unions_and_never_shrinks(self):
        job = deepen.request(self.d, "dancer", "なぜ", deepen.plan(self.d, "dancer"))
        n = job["round"]
        out = self.d / "raw" / "deepen" / f"r{n}.jsonl"
        write_jsonl(out, [row(5, 20, cap=40),                           # 新しく取れた
                          row(0, 60, start=20, cap=120),                # 続き: 先頭20件ずれて60件 → 前の40件と合わせて80件
                          row(1, 10, cap=120),                          # 取り直しで減った → 足さない
                          {"video_id": vid(3), "status": "no_video"}])
        res = deepen.merge(self.d, n, "dancer")
        self.assertEqual((res["videos_new"], res["videos_more"], res["comments_added"], res["not_ok"]), (1, 1, 20 + 40, 1))
        ok = deepen._ok_rows(self.d / "raw" / "comments.jsonl")
        self.assertEqual(len(ok[vid(0)]["comments"]), 80)
        self.assertEqual(len(ok[vid(1)]["comments"]), 25)
        self.assertEqual(ok[vid(5)]["deepen_round"], n)
        files = {p.name.split("_")[0] for p in (self.d / "derived" / "comments").glob("*_*.md")}
        self.assertIn("5", files)
        text = next((self.d / "derived" / "comments").glob(f"5_{vid(5)}.md")).read_text(encoding="utf-8")
        self.assertIn("界隈の掘り下げで取り足した", text)
        # もう一度決めると、取り足した動画は「新しく取る」から外れる
        self.assertNotIn(5, [x["seq"] for x in deepen.candidates(self.d, "dancer")["new"]])
        # 前の回で「動画が無い」だった動画も外れる
        self.assertNotIn(3, [x["seq"] for x in deepen.candidates(self.d, "dancer")["new"]])

    def test_pool_file_for_spatest(self):
        pl = deepen.plan(self.d, "dancer")
        p = self.d / "derived" / "deepen" / "r1_pool_p1.tsv"
        deepen.write_pool(p, pl["targets"], "dancer", 1)
        with open(p, encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        self.assertEqual([(r["seq"], r["cap"], r["priority"]) for r in rows], [("5", "40", "1"), ("3", "40", "1"), ("0", "120", "2")])


class TestVerifyPlays(unittest.TestCase):
    """検算の再生数の照合（2026-10-06 に直した: カンマ入り・ほかの動画をまたがない・「再生 数字」の形）"""
    def test_patterns(self):
        def found(text):
            return [(int(m.group(1)), m.group(2), m.group(3)) for m in [*flow_w1.PLAY_RE.finditer(text), *flow_w1.PLAY_RE_PRE.finditer(text)]]
        self.assertEqual(found("seq 19（1,420万再生）"), [(19, "1,420", "万")])
        self.assertEqual(found("動画（seq 14）や開封（seq 23、1,150万再生）"), [(23, "1,150", "万")])   # seq 14 の数字にしない
        self.assertEqual(found("seq 0、@a、2025-07-20、再生 4,100,000）"), [(0, "4,100,000", None)])

    def test_claims_and_mismatch(self):
        """「〇〇回再生（seq M …）」は M の数字。（ ）の中で言い終えたあとの数字は別の話。丸めた数字は丸めの幅まで一致（2026-10-06 きゃわの事例の誤報）"""
        plays = {20: 2600000, 28: 308800, 39: 1600000, 707: 1300000}

        def bad(text):
            return [(s, num) for s, num, u, _ in flow_w1.play_claims(text) if s in plays and flow_w1.play_mismatch(num, u, plays[s])]
        ok = ["（seq 20（@a、再生 2,600,000））、5/28の公式の投稿も160万回再生まで伸びています（seq 39（@b、再生 1,600,000））",
              "ネイルチェンジ（seq 707（@y、再生 1,300,000））と、100万回再生を超える美容系の投稿が続きます",
              "2人並び（seq 28、@c、再生 30万）", "2人並びが30.9万回再生（seq 28（@c、再生 308,800））"]
        for t in ok:
            self.assertEqual(bad(t), [], t)
        self.assertEqual(bad("2人並びが50万回再生（seq 28（@c、再生 308,800））"), [(28, "50")])
        self.assertEqual(bad("seq 28 の2人並びは50万回再生まで伸びた。"), [(28, "50")])
        self.assertEqual(bad("2人並びが30.9万回再生（seq 28（@c、再生 500,000））"), [(28, "500,000")])


class TestComeBack(unittest.TestCase):
    def test_come_back_line(self):
        s = pr.come_back_line(25 * 60, "この界隈のコメントを取り足すの", "シルエット", "取り足した分でレポートを書き直します。")
        self.assertRegex(s, r"^この界隈のコメントを取り足すのに約25分かかります（(明日（\d+/\d+）の)?\d+時(\d\d分)?ごろに終わる見込み）。")
        self.assertIn("その間 AI は待てないため、Mac に通知が出たら「シルエットの分析を続けて」と頼んでください。", s)
        long = pr.come_back_line(12 * 3600, "TikTok から動画とコメントを集めるの", "シルエット", "そこからレポートを書きます。")
        self.assertIn("集めるのに約12時間かかります（", long)
        self.assertEqual([pr._span(x) for x in (23.8 * 60, 61 * 60, 85 * 60, 12.4 * 3600, 12.6 * 3600)],
                         ["25分", "1時間", "1時間25分", "12時間半", "12時間半"])
        self.assertIn("時間がかかります", pr.come_back_line(None, "集めるの", "x", ""))


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, pr.DEEPEN_WAIT_S)
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = Path(self.tmp.name)
        pr.LOCAL = FakeLocal()
        pr.DEEPEN_WAIT_S = 0      # 試験では待たない
        self.d = make_analysis(Path(self.tmp.name))

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, pr.DEEPEN_WAIT_S = self.saved
        self.tmp.cleanup()

    def meta(self):
        return json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))

    def submit(self, out):
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], "ai", t["text"][:300])
        r = pr.submit("u1", t["task_id"], out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
        self.assertTrue(r["ok"], r.get("text"))
        return t

    def test_match_community(self):
        tax = json.loads((self.d / "outputs" / "taxonomy.json").read_text(encoding="utf-8"))
        self.assertEqual(pr._match_community(tax, "dancer"), "dancer")
        self.assertEqual(pr._match_community(tax, "屋外で踊る人"), "dancer")
        with self.assertRaises(pr.RunnerError) as e:
            pr._match_community(tax, "ゲーム実況界隈")
        self.assertIn("`student`", str(e.exception))

    def test_queue_puts_deepen_first(self):
        pr.deepen("u1", AID, "dancer", "なぜバズったのかが弱い")
        other = Path(self.tmp.name) / "a20261006-0001-other"
        other.mkdir()
        (other / "analysis.json").write_text(json.dumps({"analysis_id": other.name, "acquisition": {
            "status": "queued", "queued_at": "2026-10-06T00:00:00+09:00"}}), encoding="utf-8")
        self.assertEqual(worker.queue(), [AID, other.name])

    def test_end_to_end(self):
        r = pr.deepen("u1", AID, "屋外で踊る人", "なぜバズったのかが弱い")
        self.assertIn("新しく2本・続きを1本", r["text"])
        self.assertIn("止まる。取得を待たない", r["text"])                  # 2段: 取り足しのあと利用者が「続けて」
        # 最初の返事に、何分後に戻って頼むかを事情と共に（come_back_line。言い換えずに入れる）
        self.assertRegex(r["text"], r"「この界隈のコメントを取り足すのに約\d+分かかります（.+ごろに終わる見込み）。"
                                    r"その間 AI は待てないため、Mac に通知が出たら「テスト曲の分析を続けて」と頼んでください。"
                                    r"取り足した分でレポートを書き直します。」")
        self.assertEqual(pr.LOCAL.ensured, 1)
        # 取り足しの間に「続けて」と言われたら、待ち（止まって伝える）
        w = pr.next_task("u1", AID)
        self.assertEqual(w["kind"], "wait")
        self.assertIn("繰り返し呼ばない", w["text"])
        self.assertIn("界隈の掘り下げの取り足し", pr.status("u1", AID)["text"] + pr.status("u1", AID)["analyses"][0]["progress"])
        with self.assertRaises(pr.RunnerError):                         # 途中で2つ目は受けない
            pr.deepen("u1", AID, "student", "こっちも")
        # 係が取ったことにする
        write_jsonl(self.d / "raw" / "deepen" / "r1.jsonl", [row(5, 20), row(0, 60, start=20, cap=120)])
        res = deepen.merge(self.d, 1, "dancer")
        m = self.meta()
        m["deepen"].update({"status": "done", "result": res})
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

        # 1. dcomments: 前回の分析・利用者の言葉・取り足しの結果・増やした件数が指示書にある
        t = pr.next_task("u1", AID)
        self.assertIn("dcomments", t["task_id"])
        for s in ("なぜバズったのかが弱い", "前回の傾向", "新しく 1本・続き 1本", "dcomments:dancer", "【取り足し】", "`path_P1`"):
            self.assertIn(s, t["text"])
        cid_new = comment(vid(5), 3)["cid"]
        md = ("### dancer\n#### コメントの全体傾向\nx\n#### この界隈が曲を採用した文脈\nx\n#### 動画が伸びた理由（見た人の受け取り方）\nx\n"
              f"#### 段階による変化\nx\n#### 具体例で確かめた引用\n- seq 5: 『コメント3』（97 いいね、cid {cid_new}）\n"
              "#### ラベルとのずれ\nなし\n#### 前回からの変化\n- 屋外で踊る技量を見せたい層が見えた\n")
        bad = pr.submit("u1", t["task_id"], md.replace("#### 前回からの変化", "#### その他"))
        self.assertFalse(bad["ok"])
        ok = pr.submit("u1", t["task_id"], md)
        self.assertTrue(ok["ok"], ok.get("text"))
        self.assertTrue(list((self.d / "outputs" / "synthesis" / "history").glob("dancer_r1_*.md")))

        # 2. outline_revise: 変わったこと・★の章が指示書にある。path_P1 と branch を直す
        t = pr.next_task("u1", AID)
        self.assertIn("outline_revise", t["task_id"])
        self.assertIn("技量を見せたい層", t["text"])
        self.assertIn("★`path_P1`", t["text"])
        plan = {"thesis": "全体の主張", "thesis_changed": False,
                "chapters": [{"id": "path_P1", "role": "r", "claims": [{"claim": "c", "evidence": [{"cid": cid_new}]}], "bridge": "b"}],
                "rewrite": [{"chapter": "branch", "why": "分岐点にも足す"}, {"chapter": "path_P1", "why": "層を足す"}],
                "note_to_user": "立ち上がりの章に層を足した"}
        bad = pr.submit("u1", t["task_id"], json.dumps({**plan, "rewrite": [{"chapter": "branch", "why": "x"}]}, ensure_ascii=False))
        self.assertFalse(bad["ok"])                                     # 計画を直した章が rewrite に無い
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(plan, ensure_ascii=False))["ok"])
        o = json.loads((self.d / "outputs" / "outline.json").read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in o["chapters"]], CH)                 # 並びはそのまま
        self.assertEqual(o["chapters"][1]["claims"][0]["evidence"], [{"cid": cid_new}])

        # 3. 書き直し: 執筆の順（path → branch）。前の原稿を読む指示がある
        t = pr.next_task("u1", AID)
        self.assertIn("write", t["task_id"])
        self.assertIn("chapter:path_P1", t["text"])
        self.assertIn("層を足す", t["text"])
        self.assertTrue(pr.submit("u1", t["task_id"], chapter_md("path_P1", (0, 5), f"『コメント3』（97 いいね、cid {cid_new}）"))["ok"])
        self.assertTrue(list((self.d / "outputs" / "chapters" / "history").glob("path_P1_deepen-r1_*.md")))
        self.submit(chapter_md("branch", (5,)))

        # 4. review: 直した章が渡る。result のずれを1つ見つける
        t = pr.next_task("u1", AID)
        self.assertIn("review", t["task_id"])
        self.assertIn("`path_P1`: 層を足す", t["text"])
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(
            {"fixes": [{"chapter": "result", "what": "核に層の話をつなぐ"}], "note_to_user": "結果の章を直す"}, ensure_ascii=False))["ok"])
        t = pr.next_task("u1", AID)
        self.assertIn("通し読みで見つけたずれ: 核に層の話をつなぐ", t["text"])
        self.assertTrue(pr.submit("u1", t["task_id"], chapter_md("result", (5,)))["ok"])

        # 5. 仕上げは直した章だけ（レポートの順: path_P1 → branch → result）。題名は変えない
        done_types = []
        for _ in range(3):
            t = pr.next_task("u1", AID)
            done_types.append(t["task_id"].rsplit("-", 1)[-1])
            self.assertTrue(pr.submit("u1", t["task_id"], "## 見出し\n\n" + "仕上げた本文。" * 40 + "\n")["ok"], t["text"][:200])
        self.assertEqual(done_types, ["finish"] * 3)
        st = json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual([x["params"]["chapter"] for x in st["tasks"] if x["type"] == "finish"], ["path_P1", "branch", "result"])
        self.assertFalse([x for x in st["tasks"] if x["type"] == "finish_title"])

        # 6. 組み立て・検算・ZIP はサービス → 完了の知らせに、今回の直しが載る
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], "done")
        for s in ("今回の直し（界隈の掘り下げ: dancer）", "新しく 1本・続き 1本", "立ち上がりの章に層を足した", "結果の章を直す"):
            self.assertIn(s, t["text"])
        rep = (self.d / "outputs" / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn(cid_new, rep)
        ver = json.loads((self.d / "outputs" / "verify.json").read_text(encoding="utf-8"))
        self.assertEqual(ver["errors"], [])
        self.assertEqual(pr.status("u1", AID)["analyses"][0]["state"], "done")

    def test_wait_in_chat_switch(self):
        # 切り替え（DEEPEN_WAIT_IN_CHAT）を入れると、会話の中で待つ形になる（2026-10-06 の判断では切）
        saved = pr.DEEPEN_WAIT_IN_CHAT
        pr.DEEPEN_WAIT_IN_CHAT = True
        try:
            r = pr.deepen("u1", AID, "dancer", "浅い")
            self.assertIn("この会話で取得の終わりを待つ", r["text"])
            w = pr.next_task("u1", AID)
            self.assertEqual(w["kind"], "wait")
            self.assertIn("すぐにもう一度 next_task", w["text"])
        finally:
            pr.DEEPEN_WAIT_IN_CHAT = saved

    def test_behind_long_acquisition_stops(self):
        # 別の曲の半日の取得が走っていたら、会話の中では待たない（止まって「続けて」を待つ）
        other = Path(self.tmp.name) / "a20261006-0001-other"
        other.mkdir()
        (other / "analysis.json").write_text(json.dumps({"analysis_id": other.name, "title": "別の曲",
                                                         "acquisition": {"status": "running"}}), encoding="utf-8")
        pr.deepen("u1", AID, "dancer", "浅い")
        w = pr.next_task("u1", AID)
        self.assertEqual(w["kind"], "wait")
        self.assertIn("「別の曲」が終わってから", w["text"])
        self.assertIn("繰り返し呼ばない", w["text"])

    def test_leftover_done_task_does_not_block(self):
        st = json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))
        st["tasks"][-1]["status"] = "pending"            # 前の完了の知らせを受け取らずに会話が終わった
        (self.d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        pr.deepen("u1", AID, "dancer", "浅い")
        m = self.meta()
        m["deepen"]["status"] = "done"
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        self.assertIn("dcomments", pr.next_task("u1", AID)["task_id"])

    def test_no_targets_goes_straight_to_ai(self):
        # 界隈 student の候補を全部取ったことにする → 取得なしで、すぐに dcomments
        write_jsonl(self.d / "raw" / "comments.jsonl",
                    [json.loads(l) for l in open(self.d / "raw" / "comments.jsonl", encoding="utf-8")] +
                    [row(7, 30, has_more=0), row(6, 100, cap=120, has_more=0)])
        deepen.prep(self.d)
        r = pr.deepen("u1", AID, "student", "浅い")
        self.assertIn("取り足せる動画", r["text"])
        self.assertEqual(self.meta()["deepen"]["status"], "done")
        self.assertIn("dcomments", pr.next_task("u1", AID)["task_id"])


if __name__ == "__main__":
    unittest.main()
