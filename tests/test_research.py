"""ウェブで調べる・時代背景の材料・空けておかない検査・確認メモ（docs/WEB_RESEARCH.md）の試験。ウェブにも AI にも本物の置き場にも触らない。

- research（ウェブで調べる）と era（時代背景の材料）の受け取りと、W番号の付け方
- 仕事の列: 構成案の前に research → era。切り直しのあとは使い回す
- 執筆・仕上げ・検算: 空けておく書き方（「人の考察を入れる場所」など）と、材料に無い W番号を差し戻す
- 確認メモ（前は「書き足すところのメモ」）と、利用者のフォルダに置く名前

  python3 -m unittest tests.test_research
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1  # noqa: E402
import proto_runner as pr  # noqa: E402
from acquire import pipeline  # noqa: E402
from tests.test_deepen import AID, HEAD, FakeLocal  # noqa: E402
from tests.test_recut import setup_analysis  # noqa: E402

RESEARCH = {"web_search": True,
            "song": [{"fact": "作詞・作曲は山田、編曲は佐藤", "source": "https://example.com/disco", "source_name": "公式サイト"}],
            "outside": [{"metric": "YouTube の MV の再生数", "value": "約123万回", "as_of": "2026-10-06",
                         "source": "https://www.youtube.com/watch?v=abc", "source_name": "YouTube（公式）", "note": "2025-07-01 公開"}],
            "people": [{"who": "@user3（だれか）", "fact": "〇〇の元メンバー", "source": "https://example.com/p"}],
            "not_found": ["Spotify のチャートに入った記録は見つからなかった"]}
F1, F2, F3 = "2025-04-03_n111111111111", "2025-05-10_n222222222222", "2025-07-21_n333333333333"
CARDS = [{"file": f"{F1}.md", "kind": "楽曲分析", "date": "2025-04-03", "song": "かがみ", "artist": "FRUITS ZIPPER", "buzz_type": "⑧アイドル曲"},
         {"file": f"{F2}.md", "kind": "楽曲分析", "date": "2025-05-10", "song": "倍倍FIGHT!", "artist": "CANDY TUNE", "buzz_type": "⑧"},
         {"file": f"{F3}.md", "kind": "流行曲Report", "date": "2025-07-21", "title": "【2025.6ver】月報", "song": "Yummy/野良猫"}]
GLOSSARY = "## A. 枠組み\nx\n\n## B. バズの定義と分類\n\n**十四分類**— 14分類。\n  - **⑧可愛いアイドル曲**— 定型例。\n\n## C. 界隈\ny\n"


def era_md(files=(F1, F2)):
    return ("### この曲の型\n⑧可愛いアイドル曲。理由は踊ってみたと口パクが中心だから\n\n### 似た位置づけの曲\n" +
            "".join(f"- 曲 / だれか（2025年）— 似ている点と違う点（{f}）\n" for f in files) +
            f"\n### 同じ時期の流行\n- 陽キャ系が強かった（{F3}）\n\n### この曲の立ち位置\n- 可愛いアイドル曲の流れの中で、台詞の口パクが新しい\n")


def music_md(extra=""):
    return (HEAD["music"] + "\n\n作詞・作曲は山田、編曲は佐藤です [W1]。" + "画面の文字は台詞が多い。" * 40 +
            "\n\n## 4. 楽曲構成の整理（切り出し箇所）\n\n台詞の部分が切り出された。\n\n## 5. 時代背景における本楽曲の立ち位置\n\n"
            "可愛いアイドル曲の流れの中で、台詞の口パクが新しい。\n" + extra)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR, flow_w1.KB_DIR)
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = base / "analyses"
        pr.LOCAL = FakeLocal()
        flow_w1.REPORTS_DIR = None
        kb = base / "kb"
        (kb / "distilled").mkdir(parents=True)
        (kb / "notes").mkdir()
        (kb / "distilled" / "cards.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CARDS), encoding="utf-8")
        (kb / "distilled" / "GLOSSARY.md").write_text(GLOSSARY, encoding="utf-8")
        for c in CARDS:
            (kb / "notes" / c["file"]).write_text(f"# {c.get('song') or c['title']}\n\n## 時代背景\n本文\n", encoding="utf-8")
        flow_w1.KB_DIR = kb
        flow_w1._SAME_SONG.clear()
        self.d = setup_analysis(pr.ANALYSES_DIR)
        self.a = pr.Analysis(AID)

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, flow_w1.REPORTS_DIR, flow_w1.KB_DIR = self.saved
        flow_w1._SAME_SONG.clear()
        self.tmp.cleanup()

    def research(self, obj=None):
        return flow_w1.accept_research(self.a, json.dumps(obj or RESEARCH, ensure_ascii=False))


class TestAccept(Base):
    def test_research_numbers_items(self):
        self.assertEqual(self.research(), [])
        res = json.loads((self.d / "outputs" / "research.json").read_text(encoding="utf-8"))
        self.assertEqual([(it["id"], it["kind"]) for it in res["items"]], [("W1", "song"), ("W2", "outside"), ("W3", "people")])
        self.assertEqual(flow_w1.research_ids(self.a), {1, 2, 3})
        md = (self.d / "outputs" / "research.md").read_text(encoding="utf-8")
        self.assertIn("[W2] YouTube の MV の再生数: 約123万回（2026-10-06 時点。2025-07-01 公開）", md)
        self.assertIn("Spotify のチャートに入った記録は見つからなかった", md)
        self.assertIn("[W2]", pr.read("u1", AID, "research")["text"])

    def test_research_rejects(self):
        bad = json.loads(json.dumps(RESEARCH))
        del bad["outside"][0]["source"]
        bad["outside"][0]["as_of"] = "最近"
        errs = self.research(bad)
        self.assertTrue(any("source" in e for e in errs), errs)
        self.assertTrue(any("as_of" in e for e in errs), errs)
        self.assertTrue(self.research({"song": []}))                                 # web_search が無い
        self.assertTrue(self.research({"web_search": True}))                         # 何も無く、見つからなかったものも無い
        self.assertEqual(self.research({"web_search": True, "not_found": ["MV は無かった"]}), [])
        self.assertFalse((self.d / "outputs" / "era.md").exists())

    def test_research_without_web(self):
        self.assertEqual(self.research({"web_search": False}), [])
        self.assertEqual(flow_w1.research_ids(self.a), set())
        self.assertIn("ウェブ検索が使えず", flow_w1.research_block(self.a, ("outside",), "外"))

    def test_era(self):
        self.assertEqual(flow_w1.accept_era(self.a, era_md()), [])
        self.assertIn("台詞の口パクが新しい", pr.read("u1", AID, "era")["text"])
        errs = flow_w1.accept_era(self.a, era_md((F1, "2024-01-01_n999999999999")))
        self.assertTrue(any("一覧に無い記事" in e for e in errs), errs)
        self.assertTrue(any("2本以上" in e for e in flow_w1.accept_era(self.a, era_md((F1,)))))
        self.assertTrue(any("この曲の型" in e for e in flow_w1.accept_era(self.a, era_md().replace("### この曲の型", "### 型"))))


class TestPlanAndWrite(Base):
    def plan_state(self, params=None):
        st = {"analysis_id": AID, "flow": "w1", "next_n": 2, "tasks": []}
        t = flow_w1._task(st, "plan", "service", "以降の仕事の準備（サービス）", params)
        st["tasks"].append(t)
        flow_w1.service_plan(self.a, t, st)
        return [x["type"] for x in st["tasks"]], st

    def test_plan_order(self):
        types, _ = self.plan_state()
        i = types.index("outline")
        self.assertEqual(types[i - 2:i], ["research", "era"])
        self.assertLess(types.index("ref_digest"), types.index("research"))

    def test_recut_reuses_research(self):
        self.research()
        flow_w1.accept_era(self.a, era_md())
        types, _ = self.plan_state({"recut": 1})
        self.assertNotIn("research", types)
        self.assertNotIn("era", types)

    def test_prompts(self):
        self.research()
        flow_w1.accept_era(self.a, era_md())
        _, st = self.plan_state()
        get = lambda typ, ch=None: next(x for x in st["tasks"] if x["type"] == typ and (ch is None or x["params"].get("chapter") == ch))
        r = flow_w1.render(self.a, get("research"), st)
        self.assertIn("ウェブ検索", r["text"])
        self.assertIn("要のアカウント", r["text"])
        e = flow_w1.render(self.a, get("era"), st)["text"]
        for s in (F1, F2, F3, "⑧可愛いアイドル曲", "似た位置づけの曲"):
            self.assertIn(s, e)
        m = flow_w1.render(self.a, get("write", "music"), st)["text"]
        for s in ("[W1] 作詞・作曲は山田", "台詞の口パクが新しい", "書き切る"):
            self.assertIn(s, m)
        self.assertNotIn("ここは人の考察を入れる場所」と書いて空ける", m)
        res = flow_w1.render(self.a, get("write", "result"), st)["text"]
        self.assertIn("[W2] YouTube の MV の再生数", res)
        self.assertNotIn("Spotify のチャートに入った記録は見つからなかった", res)   # 見つからなかったものは本文の材料に渡さない
        self.assertIn("[W3] @user3", flow_w1.render(self.a, get("write", "path_P1"), st)["text"])
        self.assertNotIn("No.—", flow_w1.render(self.a, get("write", "intro"), st)["text"])
        o = flow_w1.render(self.a, get("outline"), st)
        self.assertIn("台詞の口パクが新しい", o["text"])
        self.assertIn("research", "".join(o["catalog"]))

    def test_chapter_checks(self):
        self.research()
        h = "## 3. 楽曲の音楽的特徴（内的要因）"
        self.assertEqual(flow_w1.check_chapter(self.a, music_md(), h), [])
        errs = flow_w1.check_chapter(self.a, music_md("声質の分析は、ここは人の考察を入れる場所です。"), h)
        self.assertTrue(any("空けておく書き方" in e for e in errs), errs)
        errs = flow_w1.check_chapter(self.a, music_md("TikTok外の指標は本データでは扱えない。"), h)
        self.assertTrue(any("空けておく書き方" in e for e in errs), errs)
        errs = flow_w1.check_chapter(self.a, music_md("チャートで1位 [W9]。"), h)
        self.assertTrue(any("W9" in e for e in errs), errs)
        errs = flow_w1.check_chapter(self.a, music_md(f"（{F1}）"), h)
        self.assertTrue(any("記事の名前" in e for e in errs), errs)

    def test_outline_web_evidence(self):
        self.research()
        good = [{"claim": "MV も伸びた", "evidence": [{"web": "W2"}]}]
        self.assertEqual(flow_w1._check_evidence(self.a, "result", good), [])
        self.assertTrue(flow_w1._check_evidence(self.a, "result", [{"claim": "x", "evidence": [{"web": "W7"}]}]))

    def test_finished_checks(self):
        self.research()
        allowed = {r["url"] for r in flow_w1.videos(self.a).values()}
        body = "## 6. バズった結果得られたもの\n\n" + "MV は約123万回再生。" * 30
        self.assertEqual(flow_w1.check_finished(self.a, body, allowed), [])
        self.assertTrue(any("内部の印" in e for e in flow_w1.check_finished(self.a, body + "[W2]", allowed)))
        self.assertTrue(any("空けておく" in e for e in flow_w1.check_finished(self.a, body + "ここでは扱えません。", allowed)))


class TestMemoAndNames(Base):
    def finish(self, meta):
        (self.d / "outputs" / "note_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        st = json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))
        flow_w1.service_assemble(self.a, st)
        return (self.d / "outputs" / "EDITOR_NOTES.md").read_text(encoding="utf-8")

    def test_memo(self):
        self.research()
        memo = self.finish({"title": "題", "guesses_md": "- **1章(1)**: 元メンバーかは推測", "changes_md": "c"})
        for s in ("# 確認メモ（テスト曲 / だれか）", "書き切ってあります", "[W2] YouTube の MV の再生数", "https://www.youtube.com/watch?v=abc",
                  "探したが見つからなかったもの", "## 推測で書いたところ", "元メンバーかは推測"):
            self.assertIn(s, memo)
        self.assertNotIn("書き足すと完成", memo)
        self.assertNotIn("仕上げで変えたこと", memo)
        rep = (self.d / "outputs" / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("### ウェブで調べたことの出どころ", rep)                     # 根拠の番号つきの版の付録
        self.assertIn("[W1] 作詞・作曲は山田", rep)

    def test_memo_without_web_and_old_meta(self):
        self.research({"web_search": False})
        memo = self.finish({"title": "題", "guesses_md": "なし"})
        self.assertIn("ウェブ検索が使えなかった", memo)
        self.assertIn("ウェブ検索", flow_w1.done_materials(self.a)["verify_note"])   # 完了の知らせで一言添える
        old = self.finish({"title": "題", "editor_notes_md": "### 書き足すと完成する箇所\n- 3章", "changes_md": "c"})
        self.assertIn("書き足すと完成する箇所", old)                                 # 前の形の仕上げは、そのまま

    def test_title_without_number(self):
        t = {"type": "finish_title", "params": {}}
        errs = flow_w1.accept(self.a, t, json.dumps({"title": "【曲 / だれか】Hitの理由分析レポート 〜TikTok今週の1曲 [No.— - 26/10-1]",
                                                      "guesses_md": "なし"}, ensure_ascii=False), {})
        self.assertTrue(any("号数" in e for e in errs), errs)
        self.assertTrue(flow_w1.accept(self.a, t, json.dumps({"title": "題"}, ensure_ascii=False), {}))   # guesses_md が無い
        self.assertEqual(flow_w1.accept(self.a, t, json.dumps({"title": "【曲 / だれか】Hitの理由分析レポート 〜TikTok今週の1曲",
                                                               "guesses_md": "なし"}, ensure_ascii=False), {}), [])

    def test_folder_names(self):
        rep = Path(self.tmp.name) / "reports"
        flow_w1.REPORTS_DIR = str(rep)
        self.research()
        self.finish({"title": "題", "guesses_md": "なし"})
        folder = flow_w1.report_folder(self.a)
        folder.mkdir(parents=True)
        (folder / "REPORT.md").write_text("前の版の名前で置いた写し", encoding="utf-8")
        links = flow_w1.done_materials(self.a)["links"]
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, sorted(["レポート.md", "レポート（根拠の番号つき）.md", "確認メモ.md", flow_w1.WEEKLY_CSV]))
        self.assertLess(links.index("[レポート.md]"), links.index("[レポート（根拠の番号つき）.md]"))   # 読むほうを先に
        for s in ("読む・note に貼るのはこれ", "仕上げる前の原稿", "推測で書いたところ"):
            self.assertIn(s, links)
        self.assertNotIn("書き足すところ", links)

    def test_verify_unknown_web(self):
        self.research()
        p = self.d / "outputs" / "chapters" / "result.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nチャート1位 [W9]。\n", encoding="utf-8")
        st = json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))
        flow_w1.service_assemble(self.a, st)
        flow_w1.service_verify(self.a, st)
        ver = json.loads((self.d / "outputs" / "verify.json").read_text(encoding="utf-8"))
        self.assertIn("ウェブで調べたことに無い出どころの番号: W9", ver["errors"])


if __name__ == "__main__":
    unittest.main()
