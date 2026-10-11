"""0.8.0 の AI の段のやり直し（redo）・版の一覧（versions）・前の版に戻す（restore_version）・レポートのフォルダの前の版・
直し（revise）の複数章・段階の数の検査の試験（docs/PLAN_0_8.md 2-2・2-3）。TikTok・AI・Chrome・本物の置き場には触らない。

土台は tests/test_review_r4 の分析フォルダ（完成済みの分析。章・note 用の章・構成案・界隈の分析・ラベル・段階がある）。

  collector/.venv/bin/python -m unittest tests.test_redo_080 -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1 as fw  # noqa: E402
import proto_runner as pr  # noqa: E402
from tests.test_deepen import AID, CH, HEAD  # noqa: E402
from tests.test_review_r4 import CARDS, Base, long_body, state, task  # noqa: E402

REFS = ["### 参考{i}: 題\n#### 章立てと各章の役割\nx\n#### 論理の運び\nx\n#### この曲の記事に使える組み立て\nx\n"]
TSV_HEAD = "seq\tcommunity\tformat\tmotive\ttier\tconf\treason"


def labels_tsv(seqs) -> str:
    return TSV_HEAD + "\n" + "".join(f"{s}\t{'dancer' if s < 6 else 'student'}\tdance\tfun\tgeneral\tH\tサムネで踊っている\n" for s in seqs)


PHASES = {"phases": [{"id": "P1", "name": "立ち上がり", "start": "2025-07-01", "end": "2025-07-31", "summary": "s1", "reps": [0]},
                     {"id": "P2", "name": "広がり", "start": "2025-08-01", "end": "2025-08-31", "summary": "s2", "reps": [6]}],
          "overlooked": [], "pathway_md": "踊る人から学生へ"}


class RedoBase(Base):
    def setUp(self):
        super().setUp()
        o = self.d / "outputs"
        (o / "references").mkdir()
        (o / "references" / "selected.json").write_text(json.dumps({"references": []}), encoding="utf-8")
        for i in (1, 2, 3):
            (o / "references" / f"{i:02d}.md").write_text(REFS[0].format(i=i), encoding="utf-8")
        (o / "research.json").write_text(json.dumps({"web_search": False, "at": "2026-10-06T00:00:00+09:00", "items": [],
                                                     "not_found": []}), encoding="utf-8")
        (o / "research.md").write_text("## ウェブで調べたこと\n", encoding="utf-8")
        (o / "era.md").write_text("### この曲の型\nx\n### 似た位置づけの曲\nx\n### 同じ時期の流行\nx\n### この曲の立ち位置\nx\n", encoding="utf-8")
        for name in ("REPORT.md", "NOTE_BODY.md"):
            (o / name).write_text("# 第1版のレポート\n本文\n", encoding="utf-8")
        (self.d / "state" / "options.json").write_text(json.dumps({"skip_confirm": True}), encoding="utf-8")

    def st(self) -> dict:
        return json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))

    def save(self, st) -> None:
        (self.d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def redo(self, frm, chapters=None, instruction="音楽の話を厚くして"):
        st = self.st()
        r = fw.redo(self.a, st, frm, chapters, instruction)
        self.save(r["st"])
        return r

    def pending(self, st=None):
        return [x for x in (st or self.st())["tasks"] if x["status"] != "done"]


# ---------------------------------------------------------------------------
# やり直し: 段ごとの消す表・積み直す仕事・前の版・見込み・頼みの添え方
# ---------------------------------------------------------------------------
class TestRedoFromOutline(RedoBase):
    def test_outline(self):
        before_ids = {x["task_id"] for x in self.st()["tasks"]}
        music = (self.d / "outputs" / "chapters" / "music.md").read_text(encoding="utf-8")
        r = self.redo("outline")
        o = self.d / "outputs"
        # 今の版は history/redo_r1/ に残す（仕事の状態の写しと記録も）
        kept = o / "history" / "redo_r1"
        self.assertEqual((kept / "chapters" / "music.md").read_text(encoding="utf-8"), music)
        self.assertTrue((kept / "outline.json").exists())
        self.assertEqual(json.loads((kept / fw.VERSION_META).read_text(encoding="utf-8"))["kind"], "redo")
        self.assertEqual(len(json.loads((kept / fw.VERSION_TASKS).read_text(encoding="utf-8"))["tasks"]), 2)
        # 消す表: 構成・本文・仕上げ・組み立て。使い回す: 参考記事・ウェブの調べ・時代背景・界隈の分析・ラベル・段階
        for gone in ("outline.json", "chapters", "note_chapters", "note_meta.json", "REPORT.md", "NOTE_BODY.md"):
            self.assertFalse((o / gone).exists(), gone)
        for stay in ("references/01.md", "research.json", "era.md", "synthesis/dancer.md", "labels.tsv", "phases.json", "taxonomy.json"):
            self.assertTrue((o / stay).exists(), stay)
        self.assertEqual(json.loads((self.d / "state" / "options.json").read_text(encoding="utf-8")), {"skip_confirm": True})
        # 積み直す仕事: 構成 → 執筆×6 → 組み立て → 仕上げ×6 → 題名 → 組み立て・検算・Excel → 完了（全部にやり直しの回）
        types = [x["type"] for x in self.pending()]
        self.assertEqual(types, ["outline"] + ["write"] * 6 + ["assemble"] + ["finish"] * 6 +
                         ["finish_title", "assemble", "verify", "export", "done"])
        self.assertTrue(all(x["params"].get("redo") == 1 for x in self.pending()))
        ids = [x["task_id"] for x in self.st()["tasks"]]
        self.assertEqual(len(ids), len(set(ids)))                      # next_n は戻さない（task_id が重ならない）
        self.assertTrue(before_ids <= set(ids))                         # 済んだ仕事は残す
        self.assertEqual((r["from"], r["from_name"], r["round"], r["deferred"]), ("outline", "構成", 1, False))
        self.assertEqual(r["reused"], ["参考記事", "ウェブの調べ", "時代背景"])
        self.assertEqual(r["redone"], ["構成", "本文", "note 用の仕上げ"])
        self.assertEqual((r["queued"], r["kept_id"]), (19, "redo_r1"))
        self.assertGreater(r["est_min"], 0)
        self.assertIn("Claude Pro の5時間の利用枠", r["est_text"])
        rec = fw.redo_record(self.a, 1)
        self.assertEqual((rec["from"], rec["instruction"]), ("outline", "音楽の話を厚くして"))

    def test_instruction_is_added_after_prompt(self):
        """利用者の頼みは指示書の本文の外（後ろ）に足す。指示書そのものは変えない"""
        self.redo("outline", instruction="結論を音楽寄りに\n2行目も")
        st = self.st()
        t = self.pending(st)[0]
        text = fw.render(self.a, t, st)["text"]
        plain = fw.render(self.a, {**t, "params": {}}, st)["text"]
        self.assertTrue(text.startswith(plain))
        tail = text[len(plain):]
        self.assertIn("### 利用者の頼み（このやり直しのきっかけ）", tail)
        self.assertIn("> 結論を音楽寄りに\n> 2行目も", tail)
        self.assertIn("構成からやり直している", tail)

    def test_through_tools_and_done(self):
        """道具の流れ（next_task・submit）で構成から完了まで通り、完了の知らせにやり直しの頭が付く"""
        self.redo("outline")
        t = pr.next_task("u1", AID)
        self.assertIn("outline", t["task_id"])
        self.assertIn("利用者の頼み（このやり直しのきっかけ）", t["text"])
        chs = fw.chapter_order(self.a, None)
        out = {"thesis": "主張", "title_idea": "題", "chapters": [{"id": c, "role": "r", "claims": [{"claim": "c", "evidence": [{"seq": 0}]}],
                                                                   "bridge": "b"} for c in chs]}
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(out, ensure_ascii=False))["ok"])
        for ch in fw._write_order(self.a):
            t = pr.next_task("u1", AID)
            self.assertIn("write", t["task_id"])
            head = "## 1. バズの拡大経路" if ch == "path_P1" else fw.heading_of(self.a, ch)
            if ch == "intro":
                head = fw.heading_of(self.a, ch) + " 〜TikTok今週の1曲"
            r = pr.submit("u1", t["task_id"], long_body(head))
            self.assertTrue(r["ok"], r.get("text"))
        for ch in ["intro"] + [f"path_{p['id']}" for p in fw.phases(self.a)] + fw.REPORT_ORDER_FIXED:
            t = pr.next_task("u1", AID)
            self.assertIn("finish", t["task_id"])
            self.assertTrue(pr.submit("u1", t["task_id"], "## 見出し\n\n" + "仕上げた本文。" * 40)["ok"])
        t = pr.next_task("u1", AID)
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps({"title": "題", "guesses_md": "なし"}, ensure_ascii=False))["ok"])
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], "done", t["text"][:300])
        self.assertIn("今回の直し（構成からやり直し）", t["text"])
        self.assertIn("前のものをそのまま使った段: 参考記事、ウェブの調べ、時代背景", t["text"])


class TestRedoStages(RedoBase):
    def test_write_chapter(self):
        """本文を章で指定: その章の原稿と note 用の章だけ消して書き直す。前の版のその章は prev: で読める"""
        r = self.redo("write", ["music"], "サビの話を足して")
        o = self.d / "outputs"
        self.assertFalse((o / "chapters" / "music.md").exists())
        self.assertFalse((o / "note_chapters" / "music.md").exists())
        self.assertTrue((o / "chapters" / "branch.md").exists())
        self.assertTrue((o / "note_chapters" / "intro.md").exists())
        self.assertTrue((o / "outline.json").exists())
        self.assertEqual([x["type"] for x in self.pending()],
                         ["write", "assemble", "finish", "finish_title", "assemble", "verify", "export", "done"])
        self.assertEqual(r["chapters"], ["music"])
        st = self.st()
        w = self.pending(st)[0]
        self.assertEqual((w["params"]["chapter"], w["params"]["first"]), ("music", True))
        res = fw.render(self.a, w, st)
        self.assertIn("`prev:music`", "".join(res["catalog"]))
        self.assertIn("やり直す前の版", res["text"])
        self.assertIn("本文の段落", pr.read("u1", AID, "prev:music")["text"])
        # 見込みは章の数の割合で（本文 10.7分 × 1/6 ＋ 仕上げ 5.0分 × 1/6 ＋ 題名 0.6分）
        self.assertAlmostEqual(r["est_min"], round(10.7 / 6 + 5.0 / 6 + 0.6, 1))

    def test_finish_all(self):
        self.redo("finish")
        o = self.d / "outputs"
        self.assertFalse(any((o / "note_chapters").glob("*.md")))
        self.assertTrue((o / "chapters" / "music.md").exists())
        self.assertEqual([x["type"] for x in self.pending()], ["finish"] * 6 + ["finish_title", "assemble", "verify", "export", "done"])

    def test_research_redoes_era_and_after(self):
        """ウェブの調べから: W番号が変わるので、時代背景・構成から後ろも。参考記事は使い回す"""
        r = self.redo("research")
        o = self.d / "outputs"
        for gone in ("research.json", "era.md", "outline.json", "chapters"):
            self.assertFalse((o / gone).exists(), gone)
        self.assertTrue((o / "references" / "01.md").exists())
        self.assertEqual([x["type"] for x in self.pending()][:3], ["research", "era", "outline"])
        self.assertEqual(r["reused"], ["参考記事"])

    def test_refs_and_select_goes_to_new_tasks(self):
        """参考記事から: 選んだ記事は、新しく積んだ章立ての抜き出しに渡す（前の版の済んだ仕事に書き込まない）"""
        st = self.st()
        old = [task("ref_digest", {"i": i, "n": 3, "file": "old.md", "title": "前"}, n=90 + i, status="done") for i in (1, 2, 3)]
        st["tasks"] = old + st["tasks"]
        self.save(st)
        self.redo("refs")
        self.assertFalse((self.d / "outputs" / "references").exists())
        st = self.st()
        sel = next(x for x in st["tasks"] if x["type"] == "ref_select" and x["status"] != "done")
        out = {"references": [{"file": c["file"], "why": "近い"} for c in CARDS[:3]]}
        self.assertEqual(fw.accept(self.a, sel, json.dumps(out, ensure_ascii=False), st), [])
        new = [x for x in st["tasks"] if x["type"] == "ref_digest" and x["status"] != "done"]
        self.assertEqual([x["params"]["file"] for x in new], [c["file"] for c in CARDS[:3]])
        self.assertEqual({x["params"]["file"] for x in st["tasks"][:3]}, {"old.md"})

    def test_label_goes_through_plan_and_reuses(self):
        """ラベルから: ラベル → 段階 → plan（やり直しの回つき）。plan は参考記事・ウェブの調べ・時代背景を使い回す"""
        r = self.redo("label")
        o = self.d / "outputs"
        for gone in ("labels.tsv", "phases.json", "synthesis", "outline.json", "chapters", "note_chapters"):
            self.assertFalse((o / gone).exists(), gone)
        self.assertTrue((o / "taxonomy.json").exists())
        self.assertEqual([x["type"] for x in self.pending()], ["label", "phases", "plan"])
        self.assertEqual(self.pending()[0]["params"]["seqs"], list(range(8)))
        self.assertTrue(r["est_min"] > 6)
        t = pr.next_task("u1", AID)
        self.assertIn("音楽の話を厚くして", t["text"])
        self.assertTrue(pr.submit("u1", t["task_id"], labels_tsv(range(8)))["ok"])
        t = pr.next_task("u1", AID)
        self.assertIn("phases", t["task_id"])
        self.assertTrue(pr.submit("u1", t["task_id"], json.dumps(PHASES, ensure_ascii=False))["ok"])
        t = pr.next_task("u1", AID)                                      # plan を済ませて界隈ごとのコメント分析へ
        self.assertIn("ccomments", t["task_id"])
        st = self.st()
        types = [x["type"] for x in self.pending(st)]
        self.assertNotIn("ref_select", types)
        self.assertNotIn("research", types)
        self.assertNotIn("era", types)
        self.assertIn("outline", types)
        self.assertTrue(all(x["params"].get("redo") == 1 for x in self.pending(st)))
        self.assertEqual(self.pending(st)[-1]["title"], "完了（やり直し）")
        plan = next(x for x in st["tasks"] if x["type"] == "plan")
        self.assertTrue(plan["detail"]["reuse_refs"] and plan["detail"]["reuse_research"] and plan["detail"]["reuse_era"])

    def test_confirm_keeps_proposal_and_asks(self):
        """界隈の確認から: 案は残して確認を出し直す（確認を省く設定でも聞く印 no_auto）。axes からなら案から作り直す"""
        self.redo("confirm", instruction="ダンスを屋内と屋外に分けたい")
        o = self.d / "outputs"
        self.assertTrue((o / "taxonomy_proposal.json").exists())
        self.assertFalse((o / "taxonomy.json").exists())
        p = self.pending()
        self.assertEqual([x["type"] for x in p], ["confirm", "label", "phases", "plan"])
        self.assertTrue(p[0]["params"]["no_auto"])
        st = self.st()
        self.assertIn("ダンスを屋内と屋外に分けたい", fw.render(self.a, p[0], st)["text"])

    def test_confirm_through_tool_stops_even_with_skip_confirm(self):
        """道具の口（proto_runner.redo）から: 界隈の確認からのやり直しは、確認を省く設定でも利用者に聞く（_auto_confirm を通さない）"""
        r = pr.redo("u1", AID, "confirm", None, "ダンスを屋内と屋外に分けたい")
        self.assertIn("界隈の確認で一度止まる", r["text"])
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], "ask_user", t["text"][:300])

    def test_tool_redo_from_write_and_restore(self):
        """道具の口から: 章を指定して書き直し → 前の版に戻す。見込みと「戻せる」を返す"""
        r = pr.redo("u1", AID, "write", ["music"], "音楽の話を厚くして")
        self.assertIn("からやり直します", r["text"])
        self.assertIn("見込み", r["text"])
        self.assertIn("戻すこともできます", r["text"])
        with self.assertRaises(pr.RunnerError):
            pr.redo("u1", AID, "nanika")
        r = pr.redo("u1", AID, restore="前の版")
        self.assertIn("戻します", r["text"])

    def test_tool_redo_during_acquisition_does_not_break(self):
        """集めている途中に頼まれても落ちない（AI の作業はまだ。頼みの言葉は積む）"""
        m = json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))
        m["acquisition"] = {"status": "running", "steps": {}}
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        r = pr.redo("u1", AID, "phases", None, "3つに分けて")
        self.assertIn("まだ TikTok から集めている途中", r["text"])

    def test_axes(self):
        r = self.redo("axes")
        self.assertFalse((self.d / "outputs" / "taxonomy_proposal.json").exists())
        p = self.pending()
        self.assertEqual([x["type"] for x in p], ["axes", "confirm", "label", "phases", "plan"])
        self.assertNotIn("no_auto", p[1]["params"])                      # 案からなら、確認を省く設定どおり
        self.assertTrue(r["asks_user"])

    def test_bad_args(self):
        with self.assertRaises(pr.RunnerError):
            fw.redo(self.a, self.st(), "nope")
        with self.assertRaises(pr.RunnerError):
            fw.redo(self.a, self.st(), "write", ["chapter9"])
        self.assertFalse((self.d / "outputs" / "history").exists())        # 断ったときは何も残さない・消さない

    def test_measured_minutes(self):
        """見込みは、この分析の実測（task_log の elapsed_s の中央値）があればそちら"""
        rows = [{"event": "done", "task_id": f"{AID}/0{i}-finish", "elapsed_s": 60} for i in range(5)]
        rows.append({"event": "done", "task_id": f"{AID}/099-finish_title", "elapsed_s": 99999})   # 1時間を超えるものは除く
        (self.d / "state" / "task_log.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        r = self.redo("finish", ["music", "intro"])
        self.assertAlmostEqual(r["est_min"], round(2 * 1.0 + 0.6, 1))


class TestRedoQueueAndLater(RedoBase):
    def test_in_progress_run_is_replaced_and_requests_wait(self):
        """途中の流れの残りは外して積み直す。まだ受け付けていない頼み（直し）は、やり直しのあとに回す"""
        st = self.st()
        st["tasks"] += [task("write", {"chapter": "music", "i": 5, "n": 6}, n=40), task("finish", {"chapter": "intro", "i": 1, "n": 6}, n=41),
                        task("done", kind="done", n=42),
                        task("revise", {"instruction": "2章を短く"}, n=43, status="issued"),
                        task("finish", {"chapter": None, "i": 1, "n": 1}, n=44), task("done", kind="done", n=45)]
        st["tasks"][1]["status"] = "pending"                              # 前の完了の知らせ（受け取られていない）
        self.save(st)
        r = self.redo("outline")
        st = self.st()
        self.assertEqual(st["tasks"][1]["status"], "done")                # 受け取られないままの完了の知らせは済んだことに
        ids = [x["task_id"] for x in st["tasks"]]
        for gone in (f"{AID}/040-write", f"{AID}/041-finish", f"{AID}/042-done"):
            self.assertNotIn(gone, ids)
        self.assertEqual(r["dropped"], 3)
        tail = st["tasks"][-3:]
        self.assertEqual([x["type"] for x in tail], ["revise", "finish", "done"])
        self.assertEqual(tail[0]["status"], "pending")                    # 渡したまま受け付けていない頼みは、あとで渡し直す
        self.assertEqual(st["tasks"][2]["type"], "outline")

    def test_not_reached_yet_is_deferred(self):
        """まだその段に来ていない: 何も消さず、その段から後ろの仕事に頼みを付ける（plan が足す仕事にも）"""
        st = self.st()
        st["tasks"] = [task("axes", n=1, status="done"), task("confirm", n=2, kind="ask_user", status="done"),
                       task("label", {"batch": 1, "n_batches": 1, "seqs": list(range(8))}, n=3),
                       task("phases", n=4), task("plan", n=5, kind="service")]
        st["next_n"] = 6
        self.save(st)
        r = self.redo("outline", instruction="結論を音楽寄りに")
        self.assertTrue(r["deferred"])
        self.assertEqual((r["queued"], r["dropped"], r["kept"]), (0, 0, None))
        self.assertTrue((self.d / "outputs" / "outline.json").exists())   # 何も消さない
        self.assertFalse((self.d / "outputs" / "history").exists())
        st = self.st()
        self.assertEqual(st["redo_pending"], [{"stage": "outline", "round": 1, "chapters": []}])
        self.assertNotIn("redo", st["tasks"][2]["params"])                # ラベルは構成より前なので付けない
        # もう1つ、まだ来ていない段の頼み（音楽の章だけ）。前の頼みも引き継ぐ
        r2 = fw.redo(self.a, st, "write", ["music"], "サビの話を足して")
        st = r2["st"]
        self.assertEqual([p["round"] for p in st["redo_pending"]], [1, 2])
        self.assertEqual(fw.redo_record(self.a, 2)["also"], ["結論を音楽寄りに"])
        plan = st["tasks"][4]
        fw.service_plan(self.a, plan, st)
        self.assertNotIn("redo_pending", st)
        by = {x["type"]: x for x in st["tasks"]}
        self.assertEqual(by["outline"]["params"].get("redo"), 1)
        self.assertEqual(by["finish_title"]["params"].get("redo"), 1)
        self.assertEqual(by["done"]["params"].get("redo"), 2)
        self.assertNotIn("redo", by["ccomments"]["params"])
        self.assertNotIn("redo", by["research"]["params"])
        music = [x for x in st["tasks"] if x["type"] in ("write", "finish") and x["params"].get("chapter") == "music"]
        self.assertEqual([x["params"].get("redo") for x in music], [2, 2])
        other = next(x for x in st["tasks"] if x["type"] == "write" and x["params"].get("chapter") == "branch")
        self.assertEqual(other["params"].get("redo"), 1)
        self.assertIn("結論を音楽寄りに", fw.render(self.a, by["outline"], st)["text"])
        text = fw.render(self.a, music[0], st)["text"]
        self.assertLess(text.index("サビの話を足して"), text.index("前に受けた頼み"))
        self.assertIn("結論を音楽寄りに", text[text.index("前に受けた頼み"):])
        done = fw.render(self.a, by["done"], st)["text"]
        self.assertIn("本文の段に入る前に受けた頼み", done)

    def test_reused_side_stage_is_added_when_asked(self):
        """切り直しのあとで参考記事を使い回して進んでいる途中に「参考記事からやり直して」: 参考記事の仕事を構成の前に足す"""
        st = self.st()
        st["tasks"] += [task("ccomments", {"community": "dancer", "i": 1, "n": 1}, n=30),
                        task("outline", n=31), task("done", kind="done", n=32)]
        self.save(st)
        r = self.redo("refs")
        self.assertTrue(r["deferred"])
        types = [x["type"] for x in self.pending()]
        self.assertEqual(types, ["ccomments", "ref_select", "ref_digest", "ref_digest", "ref_digest", "outline", "done"])
        self.assertTrue((self.d / "outputs" / "history" / "redo_r1").exists())   # 使い回していたものを作り直すので残す


# ---------------------------------------------------------------------------
# 版の一覧と、前の版に戻す
# ---------------------------------------------------------------------------
class TestVersionsAndRestore(RedoBase):
    def test_versions_and_restore(self):
        music = (self.d / "outputs" / "chapters" / "music.md").read_text(encoding="utf-8")
        self.assertEqual([v["name"] for v in fw.versions(self.a)], ["第1版"])
        with self.assertRaises(pr.RunnerError):
            fw.restore_version(self.a, self.st(), "前の版")               # まだ戻せる版が無い
        self.redo("outline", instruction="結論を変えて")
        vs = fw.versions(self.a)
        self.assertEqual([(v["name"], v["id"], v["kind"], v["complete"], v["current"]) for v in vs],
                         [("第1版", "redo_r1", "first", True, False), ("第2版", "current", "redo", False, True)])
        self.assertIn("構成からやり直し", vs[1]["made"])
        self.assertIn("結論を変えて", vs[1]["made"])
        # 途中のやり直しから、第1版に戻す
        r = fw.restore_version(self.a, self.st(), "前の版")
        self.save(r["st"])
        self.assertEqual((r["restored"], r["complete"], r["kept_id"]), ("第1版", True, "restore_r1"))
        o = self.d / "outputs"
        self.assertEqual((o / "chapters" / "music.md").read_text(encoding="utf-8"), music)
        self.assertTrue((o / "outline.json").exists())
        self.assertTrue((o / "versions" / "redo_r1.json").exists())          # 記録は入れ替えない
        self.assertFalse((o / fw.VERSION_META).exists())
        p = self.pending()
        self.assertEqual([x["type"] for x in p], ["done"])                    # やり直しの残りは外して、完了の知らせを出し直す
        t = pr.next_task("u1", AID)
        self.assertIn("前の版に戻した", t["text"])
        self.assertIn("戻した版: 第1版", t["text"])
        vs = fw.versions(self.a)
        self.assertEqual([(v["name"], v["id"]) for v in vs], [("第1版", "redo_r1"), ("第2版", "restore_r1"), ("第3版", "current")])
        self.assertEqual(vs[2]["made"], "第1版に戻した")
        # 途中の版（第2版。やり直しの途中）に戻すと、その版の残りの仕事を積み直す
        r = fw.restore_version(self.a, self.st(), "第2版")
        self.save(r["st"])
        self.assertFalse(r["complete"])
        self.assertFalse((o / "chapters").exists())
        p = self.pending()
        self.assertEqual(p[0]["type"], "outline")
        self.assertEqual(p[-1]["params"].get("restore"), 2)
        self.assertEqual(p[0]["params"].get("redo"), 1)                       # 頼み（やり直しの回）も引き継ぐ
        with self.assertRaises(pr.RunnerError) as e:
            fw.restore_version(self.a, self.st(), "第9版")
        self.assertIn("戻せる版", str(e.exception))

    def test_revise_keeps_a_version(self):
        rv = task("revise", {"instruction": "音楽の章に考察を足して"}, n=80)
        fin = task("finish", {"chapter": None, "i": 1, "n": 1}, n=81)
        st = state(rv, fin, task("assemble", kind="service", n=82))
        md = long_body("## 3. 楽曲の音楽的特徴", "考察を足した。")
        self.assertEqual(fw.accept(self.a, rv, json.dumps({"chapter": "music", "markdown": md}, ensure_ascii=False), st), [])
        vs = fw.versions(self.a)
        self.assertEqual([v["id"] for v in vs], ["revise_r1", "current"])
        self.assertIn("直し（楽曲の音楽的特徴", vs[1]["made"])

    def test_legacy_edits_are_listed(self):
        (self.d / "outputs" / "revisions").mkdir()
        (self.d / "outputs" / "revisions" / "005.json").write_text(json.dumps(
            {"instruction": "2章を短く", "chapter": "branch", "at": "2026-10-06T10:00:00+09:00"}, ensure_ascii=False), encoding="utf-8")
        vs = fw.versions(self.a)
        self.assertEqual(vs[0]["edits"][0]["chapters"], ["branch"])


# ---------------------------------------------------------------------------
# レポートのフォルダ: 前の版を残す・同じ題・同じ日のフォルダを見分ける
# ---------------------------------------------------------------------------
class TestReportFolder(RedoBase):
    def setUp(self):
        super().setUp()
        self.rep = Path(self.tmp.name) / "レポート"
        fw.REPORTS_DIR = str(self.rep)

    def done_text(self):
        return fw.render(self.a, task("done", kind="done", n=99), state(task("done", kind="done", n=99)))["text"]

    def test_previous_version_is_moved(self):
        self.done_text()
        folder = self.rep / "テスト曲（2026-10-06）"
        self.assertEqual(json.loads((folder / fw.REPORT_MARK).read_text(encoding="utf-8"))["analysis_id"], AID)
        self.done_text()                                                   # 同じ版を出し直しても、前の版は作らない
        self.assertFalse((folder / fw.PREV_FOLDER).exists())
        self.redo("finish")
        (self.d / "outputs" / "NOTE_BODY.md").write_text("# 第2版のレポート\n", encoding="utf-8")
        (self.d / "outputs" / "REPORT.md").write_text("# 第2版\n", encoding="utf-8")
        text = self.done_text()
        prev = list((folder / fw.PREV_FOLDER).iterdir())
        self.assertEqual(len(prev), 1)
        self.assertTrue(prev[0].name.startswith("第1版（"), prev[0].name)    # 版の一覧と同じ名前と、日時
        self.assertNotIn(AID, prev[0].name)
        self.assertEqual((prev[0] / "レポート.md").read_text(encoding="utf-8"), "# 第1版のレポート\n本文\n")
        self.assertEqual((folder / "レポート.md").read_text(encoding="utf-8"), "# 第2版のレポート\n")
        self.assertIn("前の版: 同じフォルダの「前の版」", text)
        vs = fw.versions(self.a)
        self.assertEqual(vs[0]["folder"], str(prev[0]))
        self.assertEqual(vs[-1]["folder"], str(folder))

    def test_restore_puts_old_version_back_in_folder(self):
        """前の版に戻すと、完了の知らせでレポートのフォルダにも戻した版を置き、戻す前の版を「前の版」へ移す"""
        self.done_text()
        folder = self.rep / "テスト曲（2026-10-06）"
        self.redo("finish")
        (self.d / "outputs" / "NOTE_BODY.md").write_text("# 第2版のレポート\n", encoding="utf-8")
        self.done_text()
        r = fw.restore_version(self.a, self.st(), "第1版")
        self.save(r["st"])
        t = pr.next_task("u1", AID)
        self.assertEqual(t["kind"], "done")
        self.assertEqual((folder / "レポート.md").read_text(encoding="utf-8"), "# 第1版のレポート\n本文\n")
        names = sorted(p.name.split("（")[0] for p in (folder / fw.PREV_FOLDER).iterdir())
        self.assertEqual(names, ["第1版", "第2版"])
        prev2 = next(p for p in (folder / fw.PREV_FOLDER).iterdir() if p.name.startswith("第2版"))
        self.assertEqual((prev2 / "レポート.md").read_text(encoding="utf-8"), "# 第2版のレポート\n")

    def test_same_title_same_day_gets_another_folder(self):
        self.done_text()
        other = "a20261006-0001-test"
        d2 = self.d.parent / other
        import shutil
        shutil.copytree(self.d, d2)
        m = json.loads((d2 / "analysis.json").read_text(encoding="utf-8"))
        m["analysis_id"] = other
        (d2 / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        b = pr.Analysis(other)
        (b.outputs("NOTE_BODY.md")).write_text("# 別の分析のレポート\n", encoding="utf-8")
        f2 = fw.report_folder(b)
        self.assertEqual(f2.name, "テスト曲（2026-10-06）（2）")
        fw.done_materials(b)
        self.assertEqual((f2 / "レポート.md").read_text(encoding="utf-8"), "# 別の分析のレポート\n")
        self.assertEqual((self.rep / "テスト曲（2026-10-06）" / "レポート.md").read_text(encoding="utf-8"), "# 第1版のレポート\n本文\n")
        self.assertEqual(fw.report_folder(self.a).name, "テスト曲（2026-10-06）")

    def test_unmarked_folder_from_before(self):
        """印の無いフォルダ（0.8.0 より前）: 同じ名前になるほかの分析が無ければ、この分析のもの（印を置く）"""
        folder = self.rep / "テスト曲（2026-10-06）"
        folder.mkdir(parents=True)
        (folder / "レポート.md").write_text("前に置いたもの", encoding="utf-8")
        self.assertEqual(fw.report_folder(self.a), folder)
        self.done_text()
        self.assertEqual(json.loads((folder / fw.REPORT_MARK).read_text(encoding="utf-8"))["analysis_id"], AID)
        self.assertEqual(len(list((folder / fw.PREV_FOLDER).iterdir())), 1)


# ---------------------------------------------------------------------------
# 直し（revise）の複数章・全章
# ---------------------------------------------------------------------------
class TestReviseMany(RedoBase):
    def test_two_chapters_in_place(self):
        rv = task("revise", {"instruction": "2章と3章を短く"}, n=80)
        fin = task("finish", {"chapter": None, "i": 1, "n": 1}, n=81)
        st = state(rv, fin, task("assemble", kind="service", n=82), task("verify", kind="service", n=83), task("done", kind="done", n=84))
        out = {"chapters": [{"chapter": "music", "markdown": long_body("## 3. 楽曲の音楽的特徴", "短くした。")},
                            {"chapter": "branch", "markdown": long_body(HEAD["branch"], "短くした。")}], "note_to_user": "短くした"}
        self.assertEqual(fw.accept(self.a, rv, json.dumps(out, ensure_ascii=False), st), [])
        fins = [x for x in st["tasks"] if x["type"] == "finish"]
        self.assertEqual([x["params"]["chapter"] for x in fins], ["branch", "music"])
        self.assertEqual([(x["params"]["i"], x["params"]["n"]) for x in fins], [(1, 2), (2, 2)])
        self.assertEqual([x["type"] for x in st["tasks"]], ["revise", "finish", "finish", "assemble", "verify", "done"])
        rec = json.loads((self.d / "outputs" / "revisions" / "080.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["chapters"], rec["chapter"], rec["round"]), (["branch", "music"], "branch", 1))
        self.assertIn("短くした。", (self.d / "outputs" / "chapters" / "branch.md").read_text(encoding="utf-8"))
        self.assertTrue((self.d / "outputs" / "history" / "revise_r1" / "chapters" / "music.md").exists())

    def test_whole_report_by_rewrite_tasks(self):
        rv = task("revise", {"instruction": "全体をです・ます調に"}, n=80)
        fin = task("finish", {"chapter": None, "i": 1, "n": 1}, n=81)
        st = state(rv, fin, task("assemble", kind="service", n=82), task("verify", kind="service", n=83), task("done", kind="done", n=84))
        out = {"rewrite": [{"chapter": c, "why": "です・ます調にする"} for c in CH], "note_to_user": "全章を書き直す"}
        self.assertEqual(fw.accept(self.a, rv, json.dumps(out, ensure_ascii=False), st), [])
        types = [x["type"] for x in st["tasks"]]
        self.assertEqual(types, ["revise"] + ["write"] * 6 + ["finish"] * 6 + ["finish_title", "assemble", "verify", "done"])
        ws = [x for x in st["tasks"] if x["type"] == "write"]
        self.assertEqual([x["params"]["chapter"] for x in ws], fw._write_order(self.a))
        self.assertTrue(all(x["params"]["rewrite"] and x["params"]["revise"] == 1 for x in ws))
        text = fw.render(self.a, ws[0], st)["text"]
        self.assertIn("書き直し（利用者の直しの頼み）", text)
        self.assertIn("全体をです・ます調に", text)
        # 書き直しを受け取ると、前の原稿を chapters/history に残す
        self.assertEqual(fw.accept(self.a, ws[0], long_body("## 1. バズの拡大経路"), st), [])
        self.assertTrue(list((self.d / "outputs" / "chapters" / "history").glob("path_P1_revise-r1_*.md")))

    def test_bad_forms(self):
        rv = task("revise", {"instruction": "x"}, n=80)
        for out in ({}, {"chapters": "music"}, {"rewrite": [{"chapter": "music"}]},
                    {"chapters": [{"chapter": "music", "markdown": long_body("## 3. 楽曲の音楽的特徴")}], "rewrite": [{"chapter": "music", "why": "w"}]}):
            with self.subTest(out=out):
                self.assertTrue(fw.accept(self.a, rv, json.dumps(out, ensure_ascii=False), state(rv)))


class TestPhasesCount(RedoBase):
    def obj(self, n):
        days = [(f"2025-07-{d:02d}", f"2025-07-{d:02d}") for d in range(1, n)]
        ph = [{"id": f"P{i}", "name": f"段{i}", "start": s, "end": e, "summary": "s", "reps": [0]} for i, (s, e) in enumerate(days, 1)]
        ph.append({"id": f"P{n}", "name": "最後", "start": f"2025-07-{n:02d}", "end": "2025-08-31", "summary": "s", "reps": [6]})
        return {"phases": ph, "overlooked": [], "pathway_md": "x"}

    def test_up_to_ten(self):
        self.assertEqual(fw.accept_phases(self.a, self.obj(7)), [])
        self.assertEqual(fw.accept_phases(self.a, self.obj(10)), [])
        self.assertTrue(any("2〜10" in e for e in fw.accept_phases(self.a, self.obj(11))))


if __name__ == "__main__":
    unittest.main()


class TestAxisNames(unittest.TestCase):
    def test_name_and_clean_description(self):
        """界隈の案の見出し: 短い名前と、かっこを閉じた短い説明（2026-10-12: 「心花りり @…（`honke`）」と途中で切れていた）"""
        n, d = fw.axis_name_and_desc("本家: iLiFE! の公式アカウント（@ilife_official）とメンバー本人のアカウント（bio に「iLiFE!」「〇色担当」、"
                                     "ID が @ilife_〜、心花りり @kokohana_riri など）。シグナル: x")
        self.assertEqual(n, "本家")
        self.assertEqual(d.count("（"), d.count("）"))
        self.assertTrue(d.endswith("…"))
        self.assertEqual(fw.axis_name_and_desc("今日好き: 恋愛リアリティ番組『今日、好きになりました。』の出演者。")[1],
                         "恋愛リアリティ番組『今日、好きになりました。』の出演者")
        self.assertEqual(fw.axis_name_and_desc("本家（iLiFE!）。界隈ではなく本家として別に扱う。"), ("本家", "界隈ではなく本家として別に扱う"))
