"""完成後の界隈の切り直し（docs/RECUT_COMMUNITY.md）の試験。TikTok にも AI にも本物の置き場にも触らない。

- acquire/recut.py: 界隈ごとの必ず読みたい動画（再生上位・最初期・認証）と、コメントの無いものの決め方
- 道具 recut → 分類軸の直し → ラベルの付け直し（残した界隈は付け直さない）→ 確かめ:
  足りていれば止まらずに段階へ（着席1回）、足りなければ取り足しを積んでその場で止まり、何分後に戻るかを伝える（着席2回）
- 前の版の写し・作り直すものの片付け・参考記事の使い回し・前の版の章の読み方・完了の知らせ

  python3 -m unittest tests.test_recut
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
from acquire import deepen, pipeline, recut, worker  # noqa: E402
from tests.test_deepen import AID, FakeLocal, make_analysis, row, vid, write_jsonl  # noqa: E402

TAX = {"community": {"dancer": "ダンスを主な発信内容にしている人。屋外で踊る人も含む", "student": "一般の中高生。制服や学校の話題が出る",
                     "musician": "歌ってみた・演奏を投稿する人。楽器が映る", "unknown": "判断できない"},
       "format": {"dance": "踊ってみた。振り付けを踊る", "lipsync": "口パクで歌詞を演じる投稿", "talk": "カメラに向かって話す投稿",
                  "unknown": "判断できない"},
       "motive": {"fun": "楽しいから使った。ノリで使う", "trend": "流行っているから使った", "fan": "曲や歌手が好きだから使った",
                  "unknown": "判断できない"},
       "tier": ["official_artist", "large_creator", "general", "unknown"],
       "examples": {"dancer": [0, 1], "student": [6]}}
# dancer を屋外（0・1・5）と室内（2・3・4）に分ける。student はそのまま
SPLIT = {k: v for k, v in TAX.items() if k not in ("community", "examples")}
SPLIT["community"] = {"dancer_street": "屋外で踊る人。公園・路上・校庭がサムネに映る", "dancer_room": "室内で踊る人。部屋・スタジオがサムネに映る",
                      "student": TAX["community"]["student"], "musician": TAX["community"]["musician"], "unknown": "判断できない"}
NEW_COMM = {0: "dancer_street", 1: "dancer_street", 5: "dancer_street", 2: "dancer_room", 3: "dancer_room", 4: "dancer_room"}


def setup_analysis(base: Path) -> Path:
    """掘り下げの試験の作り物に、ラベル付けに要るもの（正しい分類軸・属性・ラベル対象・参考記事）を足す"""
    d = make_analysis(base)
    p = d / "derived" / "llm_input" / "records.jsonl"
    recs = [json.loads(l) for l in open(p, encoding="utf-8")]
    for r in recs:
        r.update({"type": "video", "shares": 0, "duration_s": 15, "text_language": "ja", "tiktok_labels": [],
                  "uses_original_sound": True, "is_ad": False})
        r["author"].update({"followers": 1000, "videos": 10, "verified": r["seq"] == 3})
    write_jsonl(p, recs)
    write_jsonl(d / "derived" / "sample_label.jsonl", [{"seq": r["seq"]} for r in recs])
    (d / "outputs" / "taxonomy.json").write_text(json.dumps(TAX, ensure_ascii=False), encoding="utf-8")
    labs = list(csv.DictReader(open(d / "outputs" / "labels.tsv", encoding="utf-8"), delimiter="\t"))
    with open(d / "outputs" / "labels.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["seq", "community", "format", "motive", "region", "tier", "conf", "reason"])
        for r in labs:
            w.writerow([r["seq"], r["community"], "dance", "fun", "JP", "general", "H", "前のラベル"])
    ref = d / "outputs" / "references"
    ref.mkdir(parents=True)
    (ref / "selected.json").write_text(json.dumps({"references": []}), encoding="utf-8")
    for i in (1, 2, 3):
        (ref / f"{i:02d}.md").write_text(f"### 参考{i}: 題\n#### 章立てと各章の役割\nx\n#### 論理の運び\nx\n#### この曲の記事に使える組み立て\nx\n",
                                          encoding="utf-8")
    (d / "outputs" / "revisions").mkdir(parents=True)
    (d / "outputs" / "revisions" / "005.json").write_text(json.dumps(
        {"instruction": "音楽の章に、サビ前のタメが踊りやすいという私の見立てを足して", "chapter": "music"}, ensure_ascii=False),
        encoding="utf-8")
    return d


def labels_tsv(seqs, comm):
    rows = ["seq\tcommunity\tformat\tmotive\ttier\tconf\treason"]
    rows += [f"{s}\t{comm[s]}\tdance\tfun\tgeneral\tH\tサムネ: 屋外か室内か" for s in seqs]
    return "\n".join(rows) + "\n"


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = setup_analysis(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_picks_rule(self):
        recs = [r for r in deepen._records(self.d).values() if r["seq"] <= 5]
        pk = recut.picks(recs, 100_000)
        # 再生上位3本（0: 300万・5: 200万・1: 100万）、最初期（0）、認証（3）。4 は下限（10万）未満
        self.assertEqual(pk, {0: ["top", "earliest"], 5: ["top"], 1: ["top"], 3: ["verified_top", "verified_early"]})
        self.assertEqual(recut.picks(recs, 2_500_000), {0: ["top", "earliest"]})   # 下限を上げると 0 だけ

    def test_plan_needs_and_skips(self):
        pl = recut.plan(self.d)
        c = pl["communities"]["dancer"]
        self.assertEqual(c["have"], [0, 1])            # もうコメントがある
        self.assertEqual(c["need"], [5, 3])             # 再生の多い順
        self.assertEqual(c["unreachable"], [])
        # student: 6 はコメントあり、7（70万）は無い
        self.assertEqual(pl["communities"]["student"]["need"], [7])
        self.assertEqual([x["seq"] for x in pl["targets"]], [5, 7, 3])
        self.assertEqual({x["cap"] for x in pl["targets"]}, {20})          # 本線の1本1ページに合わせる
        cost = recut.fetch_cost(self.d)
        self.assertAlmostEqual(pl["est_min"], round(cost["page_min"] + 3 * cost["min_per_video"], 1))
        self.assertEqual(recut.plan(self.d, ["student"])["checked"], ["student"])

    def test_unreachable_and_gone_are_skipped(self):
        (self.d / "fetch_log").mkdir(exist_ok=True)
        (self.d / "fetch_log" / "comments_summary.json").write_text(json.dumps({"missing": [vid(5)]}), encoding="utf-8")
        write_jsonl(self.d / "raw" / "deepen" / "r1.jsonl", [{"video_id": vid(3), "status": "no_comments"}])
        c = recut.plan(self.d)["communities"]["dancer"]
        self.assertEqual((c["need"], sorted(c["unreachable"])), ([], [3, 5]))


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR)
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = Path(self.tmp.name)
        pr.LOCAL = FakeLocal()
        flow_w1.REPORTS_DIR = None
        self.d = setup_analysis(Path(self.tmp.name))

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR = self.saved
        self.tmp.cleanup()

    def meta(self):
        return json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))

    def submit(self, out, kind="ai"):
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], kind, t["text"][:400])
        r = pr.submit("u1", t["task_id"], out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
        self.assertTrue(r["ok"], r.get("text"))
        return t

    def add_comments(self, seqs):
        write_jsonl(self.d / "raw" / "comments.jsonl",
                    [json.loads(l) for l in open(self.d / "raw" / "comments.jsonl", encoding="utf-8")] + [row(s, 20) for s in seqs])
        deepen.prep(self.d)

    def recut_and_relabel(self):
        """道具 recut → 分類軸の直し（student は残す）→ dancer だった動画だけ付け直す"""
        r = pr.recut("u1", AID, "ダンスの界隈を、屋外で踊る人と室内で踊る人に分けて")
        self.assertIn("付け直したところで一度止まり", r["text"])          # 最初に、止まるかもしれないことを伝える
        t = pr.next_task("u1", AID)
        self.assertIn("recut", t["task_id"])
        for s in ("屋外で踊る人と室内で踊る人", "`dancer`: ダンスを主な発信内容", "ラベル 6本（コメントのある動画", "labeled:<界隈の key>"):
            self.assertIn(s, t["text"])
        out = {"taxonomy": SPLIT, "keep": {"student": "student"}, "note_to_user": "ダンスの界隈を屋外と室内に分けた"}
        bad = pr.submit("u1", t["task_id"], json.dumps({**out, "keep": {"dancer": "dancer_street"}}, ensure_ascii=False))
        self.assertFalse(bad["ok"])                                       # 分けた界隈を keep に入れた（定義が変わっている）
        self.assertIn("定義が変わっています", bad["text"])
        bad = pr.submit("u1", t["task_id"], json.dumps({**out, "keep": {"gamer": "student"}}, ensure_ascii=False))
        self.assertFalse(bad["ok"])                                       # 前に無い key
        same = pr.submit("u1", t["task_id"], json.dumps({**out, "taxonomy": TAX}, ensure_ascii=False))
        self.assertFalse(same["ok"])                                      # 分類軸が前と同じ
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(out, ensure_ascii=False))["ok"])
        # 前の版は写して、作り直すものは片付けた。参考記事・直しの記録は残す
        prev = self.d / "outputs" / "history" / "recut_r1"
        self.assertTrue((prev / "chapters" / "music.md").exists())
        self.assertTrue((prev / "labels.tsv").exists())
        self.assertFalse((self.d / "outputs" / "chapters").exists())
        self.assertFalse((self.d / "outputs" / "synthesis").exists())
        self.assertTrue((self.d / "outputs" / "references" / "01.md").exists())
        # 残した界隈（student）の動画は前のラベルのまま、付け直すのは dancer だった動画だけ
        labs = flow_w1.labels(pr.Analysis(AID))
        self.assertEqual({s: l["community"] for s, l in labs.items()}, {6: "student", 7: "student"})
        t = pr.next_task("u1", AID)
        self.assertIn("ラベルの付け直し（1/1）", t["text"])
        self.assertIn("屋外で踊る人と室内で踊る人に分けて", t["text"])          # 利用者の指示を添える
        st = self.state()
        lab = next(x for x in st["tasks"] if x["type"] == "label" and x["params"].get("recut"))
        self.assertEqual(lab["params"]["seqs"], [0, 1, 2, 3, 4, 5])
        self.assertTrue(pr.submit("u1", t["task_id"], labels_tsv(range(6), NEW_COMM))["ok"])

    def phases_out(self):
        return {"phases": [{"id": "P1", "name": "立ち上がり", "start": "2025-07-01", "end": "2025-07-31", "summary": "s1", "reps": [0]},
                           {"id": "P2", "name": "広がり", "start": "2025-08-01", "end": "2025-08-31", "summary": "s2", "reps": [6]}],
                "overlooked": [], "pathway_md": "屋外で踊る人から室内へ"}

    def test_enough_comments_goes_straight_on(self):
        # 新しい界隈の必ず読みたい動画（dancer_street: 0・5・1、dancer_room: 3・2）に、もうコメントがある → 止まらない（着席1回）
        self.add_comments([5, 3])
        self.recut_and_relabel()
        t = pr.next_task("u1", AID)                                       # 確かめ（サービス）を済ませて、そのまま段階へ
        self.assertEqual(t["kind"], "ai", t["text"][:300])
        self.assertIn("phases", t["task_id"])
        self.assertFalse((self.meta().get("deepen") or {}).get("kind"))   # 取り足しは積んでいない
        rec = flow_w1.recut_record(pr.Analysis(AID), 1)
        self.assertEqual(rec["changed"], ["dancer_room", "dancer_street"])
        self.assertEqual(rec["check"]["dancer_room"]["unreachable"], [2])   # 2 は前に「動画が無い」
        self.assertNotIn("acq_round", rec)
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(self.phases_out(), ensure_ascii=False))["ok"])
        # plan: 参考記事は使い回す（ref_select が無い）。執筆に切り直しの回が渡る
        t = pr.next_task("u1", AID)
        self.assertIn("ccomments", t["task_id"])
        st = self.state()
        tail = [x for x in st["tasks"] if x["status"] != "done"]
        self.assertFalse([x for x in tail if x["type"] in ("ref_select", "ref_digest")])
        self.assertTrue(all(x["params"].get("recut") == 1 for x in tail if x["type"] == "write"))
        # 執筆の指示書: 前の版の章と、前の版で利用者が頼んだ直し
        a = pr.Analysis(AID)
        w = next(x for x in st["tasks"] if x["type"] == "write" and x["params"].get("chapter") == "music" and x["status"] != "done")
        text = flow_w1.render(a, w, st)["text"]
        for s in ("界隈の切り直し（この版を書く理由）", "`prev:music`", "サビ前のタメが踊りやすい"):
            self.assertIn(s, text)
        self.assertIn("`prev:music`", "".join(flow_w1.render(a, w, st)["catalog"]))
        self.assertIn("本文の段落", pr.read("u1", AID, "prev:music")["text"])
        self.assertIn("dancer_street", pr.read("u1", AID, "labeled:dancer_street")["text"])
        # 完了の知らせに、切り直しの中身が載る
        done = next(x for x in st["tasks"] if x["type"] == "done" and x["status"] != "done")
        self.assertEqual(done["params"], {"recut": 1})
        text = flow_w1.render(a, done, st)["text"]
        for s in ("今回の直し（界隈の切り直し）", "ダンスの界隈を屋外と室内に分けた", "`dancer_street`", "コメントの取り足し: 要らなかった"):
            self.assertIn(s, text)
        self.assertIn("界隈の分け方: 初めの版のあと、利用者の指示で切り直した（1回", flow_w1.appendix(a))

    def test_missing_comments_stops_and_says_when_to_come_back(self):
        self.recut_and_relabel()
        w = pr.next_task("u1", AID)                                       # 確かめが取り足しを積み、その場で止まる（着席2回）
        self.assertEqual(w["kind"], "wait", w["text"][:300])
        self.assertIn("ダンスの界隈を屋外と室内に分けた", w["text"])
        self.assertIn("`dancer_room` 1本、`dancer_street` 1本", w["text"])
        self.assertRegex(w["text"], r"「切り直した界隈のコメントを集めるのに約\d+分かかります（.+ごろに終わる見込み）。"
                                    r"その間 AI は待てないため、Mac に通知が出たら「テスト曲の分析を続けて」と頼んでください。"
                                    r"新しい界隈の分け方でレポートを書き直します。」")
        self.assertEqual(pr.LOCAL.ensured, 1)
        m = self.meta()
        self.assertEqual((m["deepen"]["kind"], m["deepen"]["status"], len(m["deepen"]["targets"])), ("recut", "queued", 2))
        self.assertEqual(worker.queue(), [AID])                          # 取得の係が拾う（掘り下げと同じ枠）
        # もう一度「続けて」と言われても、待ち（一文はもう出さない）
        w2 = pr.next_task("u1", AID)
        self.assertEqual(w2["kind"], "wait")
        self.assertIn("切り直した界隈のコメントの取り足しの開始待ち", w2["text"])
        self.assertNotIn("その間 AI は待てない", w2["text"])
        self.assertEqual(pr.status("u1", AID)["analyses"][0]["progress"], "界隈の切り直しの取り足し")
        with self.assertRaises(pr.RunnerError):                         # 途中で掘り下げ・切り直しは受けない
            pr.deepen("u1", AID, "student", "浅い")
        with self.assertRaises(pr.RunnerError):
            pr.recut("u1", AID, "もう一度")
        # 係が取ったことにする（取り方・合わせ方は掘り下げと同じ。印は切り直し）
        job = m["deepen"]
        write_jsonl(self.d / "raw" / "deepen" / f"r{job['round']}.jsonl", [row(5, 20, cap=20), row(3, 15, cap=20)])
        res = deepen.merge(self.d, job["round"], job["community"], "recut", {x["video_id"]: x["community"] for x in job["targets"]})
        self.assertEqual(res["videos_new"], 2)
        ok = deepen._ok_rows(self.d / "raw" / "comments.jsonl")
        self.assertEqual(ok[vid(5)]["pool_reason"], f"recut:r{job['round']}:dancer_street")
        text = next((self.d / "derived" / "comments").glob(f"3_{vid(3)}.md")).read_text(encoding="utf-8")
        self.assertIn("界隈の切り直しで取り足した（dancer_room）", text)
        m["deepen"].update({"status": "done", "result": res})
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        # 「続けて」→ 段階から先へ
        t = pr.next_task("u1", AID)
        self.assertIn("phases", t["task_id"])
        a = pr.Analysis(AID)
        self.assertIn("2本・コメント 35件", flow_w1.recut_summary(a, 1))

    def test_changing_other_axes_relabels_everything(self):
        pr.recut("u1", AID, "型も見直して")
        t = pr.next_task("u1", AID)
        tax = json.loads(json.dumps(SPLIT))
        tax["format"]["duet"] = "デュエット機能で重ねた投稿"
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(
            {"taxonomy": tax, "keep": {"student": "student"}, "note_to_user": "型も足した"}, ensure_ascii=False))["ok"])
        lab = next(x for x in self.state()["tasks"] if x["type"] == "label" and x["params"].get("recut"))
        self.assertEqual(lab["params"]["seqs"], list(range(8)))         # keep は使えない（全部付け直す）

    def test_merge_keeps_old_comments_unchecked(self):
        # dancer と student をそのまままとめる（keep で両方を新しい key に）→ 付け直しなし・顔ぶれは前と同じなので確かめない（取り足しなし）
        pr.recut("u1", AID, "ダンスの人と学生をひとつにまとめて")
        t = pr.next_task("u1", AID)
        tax = json.loads(json.dumps(TAX))
        tax["community"] = {"young": "踊る人と一般の中高生をまとめた若い投稿者。制服・ダンス", "musician": TAX["community"]["musician"],
                            "other": "どれにも当たらない投稿者。企業など", "unknown": "判断できない"}
        tax.pop("examples")
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(
            {"taxonomy": tax, "keep": {"dancer": "young", "student": "young"}, "note_to_user": "まとめた"}, ensure_ascii=False))["ok"])
        self.assertFalse([x for x in self.state()["tasks"] if x["type"] == "label" and x["params"].get("recut")])
        t = pr.next_task("u1", AID)
        self.assertIn("phases", t["task_id"])
        rec = flow_w1.recut_record(pr.Analysis(AID), 1)
        self.assertEqual((rec["changed"], rec["removed"], rec["n_targets"]), ([], ["dancer", "student"], 0))
        self.assertEqual(json.loads((self.d / "outputs" / "taxonomy.json").read_text(encoding="utf-8"))["examples"],
                         {"young": [0, 1, 6]})

    def test_guards(self):
        st = self.state()
        st["tasks"].append(flow_w1._task(st, "write", "ai", "執筆", {"chapter": "intro"}))
        (self.d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(pr.RunnerError) as e:                     # レポートがまだできていない
            pr.recut("u1", AID, "分けて")
        self.assertIn("完成してから", str(e.exception))
        with self.assertRaises(pr.RunnerError):
            pr.recut("u1", AID, "  ")


if __name__ == "__main__":
    unittest.main()
