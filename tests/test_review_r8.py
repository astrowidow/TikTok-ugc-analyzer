"""領域 R8 の見直しの試験: 0.6.2「レポートを人が書き足さずに書き切る」の差分（git diff 5cdf10a e6718c1）と、
追加の直し a7ca685（著者の名乗り・執筆クレジット・会社の案内を書かせない AUTHOR_RE）。

見るところ:
- 新しい仕事（ウェブで調べる research・時代背景 era）と、変わった指示書（finish_title・outline・write・finish・done）の差し込みの印
- accept の検査の抜け（形の違う出力で例外にならないか。0.6.1 で accept 全体を包んだ作りを壊していないか）
- ウェブ検索が使えない AI・何も見つからないときに、差し戻しが続かないか
- 3〜6章を空けずに書く変更で、材料の無いことを書かせる道が無いか・検査（PLACEHOLDER_RE・INTERNAL_RE・AUTHOR_RE）の誤りと抜け
- 成果物の名前の変更（レポート.md・レポート（根拠の番号つき）.md・確認メモ.md）と、0.6.1 までに完成した分析の直し
- 「ChatGPT につなぐ」が足す web_search = "live"（利用者の設定を壊さない・冪等）

TikTok・AI・Chrome・ネットワーク・本物の置き場（~/Library/Application Support/UGC Analyzer）・本物の Codex の設定に触れない。
分析フォルダ・知識ベース・設定・指示書・Codex の設定はすべて一時フォルダに作る。

  collector/.venv/bin/python -m unittest tests.test_review_r8 -v

試験は「正しい動き」を期待する形で書いてある。失敗する試験は、見直しで見つけた不具合の疑い（docstring の【疑い】）。
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1 as fw  # noqa: E402
import prompt_store as ps  # noqa: E402
import proto_runner as pr  # noqa: E402
import user_settings as us  # noqa: E402
from acquire import pipeline  # noqa: E402
from tests.test_deepen import AID, CH, HEAD, FakeLocal  # noqa: E402
from tests.test_recut import setup_analysis  # noqa: E402

RESEARCH = {"web_search": True,
            "song": [{"fact": "作詞・作曲は山田、編曲は佐藤", "source": "https://example.com/disco", "source_name": "公式サイト"}],
            "outside": [{"metric": "YouTube の MV の再生数", "value": "約123万回", "as_of": "2026-10-06",
                         "source": "https://www.youtube.com/watch?v=abc", "source_name": "YouTube（公式）"}],
            "people": [{"who": "@user3（だれか）", "fact": "〇〇の元メンバー", "source": "https://example.com/p"}],
            "not_found": ["Spotify のチャートに入った記録は見つからなかった"]}
F1, F2, F3 = "2025-04-03_n111111111111", "2025-05-10_n222222222222", "2025-07-21_n333333333333"
CARDS = [{"file": f"{F1}.md", "kind": "楽曲分析", "date": "2025-04-03", "song": "かがみ", "artist": "FRUITS ZIPPER", "buzz_type": "⑧"},
         {"file": f"{F2}.md", "kind": "楽曲分析", "date": "2025-05-10", "song": "倍倍FIGHT!", "artist": "CANDY TUNE", "buzz_type": "⑧"},
         {"file": f"{F3}.md", "kind": "流行曲Report", "date": "2025-07-21", "title": "【2025.6ver】月報", "song": "Yummy/野良猫"}]
GLOSSARY = "## A. 枠組み\nx\n\n## B. バズの定義と分類\n\n**十四分類**— 14分類。\n  - **⑧可愛いアイドル曲**— 定型例。\n\n## C. 界隈\ny\n"
LEFT_RE = re.compile(r"\{\{[a-z_]+\}\}")
GENERIC = "検査できませんでした"   # accept の中で例外が起きたときの差し戻し（accept の包みが拾った）
TITLE = "【テスト曲 / だれか】Hitの理由分析レポート 〜TikTok今週の1曲"


def era_md(files=(F1, F2), h="###"):
    return (f"{h} この曲の型\n⑧可愛いアイドル曲。踊ってみたと口パクが中心だから\n\n{h} 似た位置づけの曲\n" +
            "".join(f"- 曲 / だれか（2025年）— 似ている点と違う点（{f}）\n" for f in files) +
            f"\n{h} 同じ時期の流行\n- 陽キャ系が強かった（{F3}）\n\n{h} この曲の立ち位置\n- 可愛いアイドル曲の流れの中で、台詞の口パクが新しい\n")


def music_md(extra=""):
    return (HEAD["music"] + "\n\n作詞・作曲は山田、編曲は佐藤です [W1]。" + "画面の文字は台詞が多い。" * 40 +
            "\n\n## 4. 楽曲構成の整理（切り出し箇所）\n\n台詞の部分が切り出された。\n\n## 5. 時代背景における本楽曲の立ち位置\n\n"
            "可愛いアイドル曲の流れの中で、台詞の口パクが新しい。\n" + extra)


def task(typ, params=None, n=50, kind="ai", status="pending"):
    return {"n": n, "task_id": f"{AID}/{n:03d}-{typ}", "type": typ, "kind": kind, "title": typ, "params": params or {},
            "status": status, "first_issued_at": None, "issued_at": None, "issue_count": 0, "done_at": None, "rejects": 0}


def state(*tasks) -> dict:
    return {"analysis_id": AID, "flow": "w1", "next_n": 200, "tasks": list(tasks)}


class Base(unittest.TestCase):
    """一時フォルダに分析（tests.test_recut.setup_analysis: 0.6.1 までの形で完成した分析。research.json・era.md が無く、
    note_meta は editor_notes_md の前の形）・知識ベース・設定を作り、モジュールの置き場を差し替える（終わったら戻す）"""

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, fw.REPORTS_DIR, fw.KB_DIR, us.USERS_DIR,
                      os.environ.get("UGC_PROMPTS_DIR"))
        pr.ANALYSES_DIR = pipeline.ANALYSES_DIR = base / "analyses"
        pr.LOCAL = FakeLocal()
        fw.REPORTS_DIR = None            # 成果物を本物のレポートのフォルダに写さない
        us.USERS_DIR = base / "users"
        os.environ.pop("UGC_PROMPTS_DIR", None)
        kb = base / "kb"
        (kb / "distilled").mkdir(parents=True)
        (kb / "notes").mkdir()
        (kb / "distilled" / "cards.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CARDS), encoding="utf-8")
        (kb / "distilled" / "GLOSSARY.md").write_text(GLOSSARY, encoding="utf-8")
        for c in CARDS:
            (kb / "notes" / c["file"]).write_text(f"# {c.get('song') or c['title']}\n\n## 時代背景\n本文\n", encoding="utf-8")
        fw.KB_DIR = kb
        fw._SAME_SONG.clear()
        self.d = setup_analysis(pr.ANALYSES_DIR)
        self.a = pr.Analysis(AID)

    def tearDown(self):
        pr.ANALYSES_DIR, pipeline.ANALYSES_DIR, pr.LOCAL, fw.REPORTS_DIR, fw.KB_DIR, us.USERS_DIR, env = self.saved
        if env is None:
            os.environ.pop("UGC_PROMPTS_DIR", None)
        else:
            os.environ["UGC_PROMPTS_DIR"] = env
        fw._SAME_SONG.clear()
        self.tmp.cleanup()

    def research(self, obj=None):
        return fw.accept_research(self.a, json.dumps(RESEARCH if obj is None else obj, ensure_ascii=False))

    def era(self, md=None):
        return fw.accept_era(self.a, md or era_md())

    def st(self) -> dict:
        return json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))

    def acc(self, t, raw, st=None):
        """accept は形の違う出力を、例外でなく差し戻し（理由の一覧）で返すはず"""
        st = st or state(t)
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        try:
            return fw.accept(self.a, t, raw, st)
        except Exception as e:   # noqa: BLE001
            self.fail(f"差し戻しにならず例外で落ちた: {type(e).__name__}: {e}")

    def note_body(self) -> str:
        return (self.d / "outputs" / "NOTE_BODY.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 指示書の差し込みの印（{{…}}）
# ---------------------------------------------------------------------------
class TestPlaceholders(Base):
    CASES = [("research", {}), ("era", {}), ("outline", {}), ("finish_title", {}), ("done", {}),
             *[("write", {"chapter": ch, "i": i, "n": 6, "first": ch == "path_P1"}) for i, ch in enumerate(CH, 1)],
             *[("finish", {"chapter": ch, "i": 1, "n": 3}) for ch in ("intro", "music", "result")]]

    def _check_all(self, label):
        for typ, params in self.CASES:
            t = task(typ, params)
            with self.subTest(case=label, typ=typ, chapter=params.get("chapter")):
                text = fw.render(self.a, t, state(t))["text"]
                self.assertIsNone(LEFT_RE.search(text), f"{typ}: 埋まらない印 {LEFT_RE.findall(text)}")

    def test_new_and_changed_prompts_fill_all_placeholders(self):
        """research・era と、変わった指示書（finish_title の {{guesses}} など）が、調べた後・ウェブ検索なし・前の形（調べる工程なし）の
        どれでも {{…}} を残さない"""
        self._check_all("前の形（research.json なし）")
        self.assertEqual(self.research({"web_search": False}), [])
        self._check_all("ウェブ検索なし")
        self.assertEqual(self.research(), [])
        self.assertEqual(self.era(), [])
        self._check_all("調べた後")

    def test_seeded_user_prompts_also_fill(self):
        """利用者の置き場に写した指示書（アプリの形。新しい research.md・era.md も写る）でも {{…}} を残さない"""
        os.environ["UGC_PROMPTS_DIR"] = str(Path(self.tmp.name) / "prompts")
        done = ps.seed()
        self.assertIn("research.md", done["copied"])
        self.assertIn("era.md", done["copied"])
        self.assertEqual(self.research(), [])
        self.assertEqual(self.era(), [])
        self._check_all("利用者の置き場")


# ---------------------------------------------------------------------------
# accept の検査の抜け（形の違う出力で例外にならないか）
# ---------------------------------------------------------------------------
class TestAcceptShapes(Base):
    def _no_crash(self, t, outs, must_reject=True, st=None):
        for raw in outs:
            with self.subTest(typ=t["type"], raw=str(raw)[:80]):
                errs = self.acc(t, raw, st)
                self.assertIsInstance(errs, list)
                self.assertFalse(any(GENERIC in e for e in errs), f"検査の中で例外が起きた: {errs}")
                if must_reject:
                    self.assertTrue(errs, "形の違う出力を受け付けた")

    def test_research_odd_shapes(self):
        """research の出力の形が違っても、例外にせず理由つきで差し戻す"""
        self._no_crash(task("research"), [
            [], "ただの文字列", {"web_search": "true"}, {"web_search": 1},
            {"web_search": True, "song": {"fact": "x", "source": "https://a"}},
            {"web_search": True, "song": ["事実だけの文字列"]},
            {"web_search": True, "outside": [{"metric": "m", "value": {"n": 1}, "as_of": ["2026"], "source": ["https://a"]}]},
            {"web_search": True, "people": [{"who": None, "fact": 1, "source": "https://x y"}]},
            {"web_search": True, "not_found": "なし"},
            {"web_search": True, "items": [{"kind": "song", "fact": "x", "source": "https://a"}]},
        ])
        self.assertFalse((self.d / "outputs" / "research.json").exists())

    def test_era_and_finish_title_odd_shapes(self):
        self._no_crash(task("era"), [[], {"この曲の型": "x"}, "", "### この曲の型\nだけ"])
        self._no_crash(task("finish_title"), [
            [], "題", {"title": ["題"], "guesses_md": "なし"}, {"title": "題", "guesses_md": ["- a"]},
            {"title": "題", "guesses_md": "なし", "changes_md": {"a": 1}}, {"title": " ", "guesses_md": "なし"}])

    def test_outline_web_evidence_odd_shapes(self):
        """構成案の根拠 {"web": …} の形が違っても例外にしない（W番号は research にあるものだけ）"""
        self.assertEqual(self.research(), [])
        chs = fw.chapter_order(self.a, self.st())
        for ev in ([{"web": ["W1"]}], [{"web": None}], [{"web": 1}], [{"web": "W99"}], [{"web": "w1"}]):
            obj = {"thesis": "主張", "title_idea": "題",
                   "chapters": [{"id": c_, "role": "r", "claims": [{"claim": "c", "evidence": ev}], "bridge": "b"} for c_ in chs]}
            self._no_crash(task("outline"), [obj])
        ok = {"thesis": "主張", "title_idea": "題",
              "chapters": [{"id": c_, "role": "r", "claims": [{"claim": "c", "evidence": [{"web": "[W2]"}, {"seq": "0"}]}], "bridge": "b"}
                           for c_ in chs]}
        self.assertEqual(self.acc(task("outline"), ok), [])


# ---------------------------------------------------------------------------
# ウェブ検索が使えない・見つからないとき
# ---------------------------------------------------------------------------
class TestWithoutWeb(Base):
    def test_no_web_path_does_not_stall(self):
        """ウェブ検索が使えない AI でも、research → era → 構成案 → 3〜5章 → 題名と確認メモが、差し戻されずに通る"""
        self.assertEqual(self.research({"web_search": False}), [])
        self.assertEqual(self.research({"web_search": True, "not_found": ["曲の情報は見つからなかった"]}), [])   # 探して何も無い
        self.assertEqual(self.era(), [])
        chs = fw.chapter_order(self.a, self.st())
        obj = {"thesis": "主張", "title_idea": "題",
               "chapters": [{"id": c_, "role": "r", "claims": [{"claim": "c", "evidence": [{"seq": 0}]}], "bridge": "b"} for c_ in chs]}
        self.assertEqual(self.acc(task("outline"), obj), [])
        self.assertEqual(fw.check_chapter(self.a, music_md().replace(" [W1]", ""), HEAD["music"]), [])
        self.assertEqual(self.acc(task("finish_title"), {"title": TITLE, "guesses_md": "なし"}), [])

    def test_following_the_suggestion_is_not_suggested_again(self):
        """【疑い】ウェブ検索が使えなかった分析の完了の知らせ・確認メモは「ウェブ検索をオンにして『〇〇のレポートの6章に、TikTok の外の数字を
        調べて足して』と頼めば足せる」と言う。利用者がそのとおり頼むと直し（revise）になるが、revise には調べた事実を research.json に
        足す道が無い（[W番号] は差し戻される）。直しの完了の知らせは同じ頼み方をもう一度勧め、確認メモも「入っていません」のまま"""
        self.assertEqual(self.research({"web_search": False}), [])
        ins = "テスト曲のレポートの6章に、TikTok の外の数字（YouTube・チャートなど）を調べて足して"
        rv = task("revise", {"instruction": ins}, n=60)
        st = state(rv, task("finish", {"chapter": None, "i": 1, "n": 1}, n=61), task("assemble", kind="service", n=62),
                   task("verify", kind="service", n=63), task("done", kind="done", n=64))
        md = (HEAD["result"] + "\n\nMV は約123万回再生（2026-10-06 時点）、Billboard JAPAN の Heatseekers で最高5位。" + "本文の段落。" * 60 +
              "\n\n## 7. 今回のヒットの核\n\n核。\n\n## 8. 再現性のある要素\n\n① 要素\n")
        self.assertEqual(self.acc(rv, {"chapter": "result", "markdown": md, "note_to_user": "6章に外の数字を足した"}, st), [])
        self.assertNotIn("頼めば足せる", fw.done_materials(self.a)["verify_note"])


# ---------------------------------------------------------------------------
# 0.6.1 までに完成した分析（research.json が無い・note_meta が前の形）を 0.6.2 で直す
# ---------------------------------------------------------------------------
class TestOldAnalysis(Base):
    def test_done_does_not_claim_nothing_is_left_to_add(self):
        """【疑い】前の形の分析（research.json なし）を 0.6.2 で直す・掘り下げると、書き直さない章（3〜5章・6章）には
        「ここは人の考察を入れる場所」が残る。なのに完了の知らせ（done.md の 3）は、無条件に「書き切ってあり、書き足す場所は無い」と言わせる"""
        p = self.d / "outputs" / "note_chapters" / "music.md"
        p.write_text(HEAD["music"] + "\n\n声質や編曲については、ここは人の考察を入れる場所です。\n" + "仕上げた本文。" * 40 + "\n", encoding="utf-8")
        fw.service_assemble(self.a, self.st())
        self.assertIn("人の考察を入れる場所", self.note_body())            # 前提: 読者向けのレポートに空けた場所が残っている
        t = task("done", kind="done", n=90)
        text = fw.render(self.a, t, state(t))["text"]
        self.assertNotIn("書き足す場所は無い", text)

    def test_old_analysis_revise_puts_new_names_and_keeps_old_memo(self):
        """前の形の分析の直し（revise → 仕上げ → 組み立て → 検算 → 完了）: 利用者のフォルダの前の名前の写しは消え、新しい名前で置く。
        確認メモは前の形のメモ（editor_notes_md）をそのまま"""
        rep = Path(self.tmp.name) / "reports"
        fw.REPORTS_DIR = str(rep)
        folder = fw.report_folder(self.a)
        folder.mkdir(parents=True)
        for n in ("REPORT.md", "NOTE_BODY.md", "EDITOR_NOTES.md"):
            (folder / n).write_text("0.6.1 で置いた写し", encoding="utf-8")
        rv, fin = task("revise", {"instruction": "2章を短く"}, n=60), task("finish", {"chapter": None, "i": 1, "n": 1}, n=61)
        st = state(rv, fin, task("assemble", kind="service", n=62), task("verify", kind="service", n=63), task("done", kind="done", n=64))
        self.assertEqual(self.acc(rv, {"chapter": "branch", "markdown": HEAD["branch"] + "\n\n" + "短くした本文。" * 60,
                                       "note_to_user": "短くした"}, st), [])
        self.assertEqual(fin["params"]["chapter"], "branch")
        self.assertEqual(self.acc(fin, HEAD["branch"] + "\n\n" + "仕上げた本文。" * 40, st), [])
        fw.service_assemble(self.a, st)
        fw.service_verify(self.a, st)
        links = fw.done_materials(self.a)["links"]
        names = {p.name for p in folder.iterdir()}
        self.assertEqual(names - {fw.WEEKLY_CSV}, {"レポート.md", "レポート（根拠の番号つき）.md", "確認メモ.md"})
        self.assertIn("[確認メモ.md]", links)
        self.assertTrue((folder / "確認メモ.md").read_text(encoding="utf-8").startswith("メモ"))   # 前の形のメモはそのまま

    def test_music_prompt_without_era_material(self):
        """【疑い】0.6.1 で仕事の列（plan）を作り終え、0.6.2 で書く分析には research・era の仕事が無い。それでも3〜5章の指示は
        「3つとも書き切る」「似た位置づけの曲（曲名・アーティスト・時期を具体的に）」を求め、材料は「（時代背景の材料は無い）」。
        過去の記事を読む道（note:・kb:cards）も目録に無いので、AI は記憶で曲名を挙げるしかない（事実でないことを書かせる道）。
        目録には無い資料 research・era を載せている"""
        t = task("write", {"chapter": "music", "i": 5, "n": 6, "first": False})
        r = fw.render(self.a, t, state(t))
        self.assertIn("時代背景の材料は無い", r["text"])                     # 前提
        with self.subTest("材料が無いのに具体的な似た曲を求めない"):
            self.assertNotIn("曲名・アーティスト・時期を具体的に", r["text"])
        with self.subTest("無い資料を目録に載せない"):
            self.assertNotIn("`research`", "".join(r["catalog"]))


# ---------------------------------------------------------------------------
# 検査（PLACEHOLDER_RE・INTERNAL_RE・題名・時代背景の材料・確認メモ）
# ---------------------------------------------------------------------------
class TestChecks(Base):
    def test_intro_title_number_is_caught(self):
        """【疑い】号数（No.—）の検査は finish_title の title にだけあるが、note 用の原稿の題名は冒頭の章の1行目
        （service_assemble は本文が「# 」で始まると note_meta の title を使わない）。冒頭の章に文体ガイドどおりの
        「[No.— - YY/MM-K]」が付いても、執筆・仕上げのどちらでも止まらない"""
        body = "# " + TITLE + " [No.— - 26/10-1]\n\n今回は『テスト曲 / だれか』についてのレポートになります。" + "本文の段落。" * 80
        errs = fw.check_chapter(self.a, body, fw.heading_of(self.a, "intro")) + fw.check_finished(self.a, body, set())
        self.assertTrue(any("号数" in e or "No." in e for e in errs), errs)

    def test_era_with_h4_headings(self):
        """【疑い】時代背景の材料の見出しを「####」で書くと、見出しの検査（部分一致）は通るのに、節を拾う _section_loose は「###」だけを
        見るので「似た位置づけの曲に記事の名前を2本以上」で差し戻す（名前は書いてある）。直し方の分からない差し戻しが続く"""
        errs = self.era(era_md(h="####"))
        self.assertTrue(errs == [] or any("見出し" in e for e in errs), errs)

    def test_youtube_url_line_is_not_an_internal_mark(self):
        """【疑い】仕上げは YouTube の公式の動画の URL をそれだけの行に置いてよいが、INTERNAL_RE（P\\d・seq など、大文字小文字を問わない）が
        動画 ID の一部を内部の印として拾う（例: v=P3_kZx9LmQa の「P3」）。消せない URL で差し戻しが続く"""
        self.assertEqual(self.research(), [])
        body = (HEAD["result"] + "\n\n公式の MV も公開されています。\n\nhttps://www.youtube.com/watch?v=P3_kZx9LmQa\n\n" +
                "MV は約123万回再生。" * 30)
        errs = fw.check_finished(self.a, body, set())
        self.assertFalse(any("内部の印" in e for e in errs), errs)

    def test_placeholder_re_keeps_a_reported_fact(self):
        """【疑い】PLACEHOLDER_RE の「書き足」は、調べた事実の文（本人がサビの歌詞を書き足した、など）も止める"""
        self.assertEqual(self.research(), [])
        errs = fw.check_chapter(self.a, music_md("本人は、サビの歌詞を後から書き足したと語っている [W1]。"), HEAD["music"])
        self.assertFalse(any("空けておく" in e for e in errs), errs)

    def test_guesses_md_is_checked_for_placeholders(self):
        """【疑い】確認メモ（利用者のフォルダの「確認メモ.md」）の「推測で書いたところ」（guesses_md）は検査が無い。
        前の書き足すところのメモの書き方（「ここは人の考察」「書き足すと完成」）で返しても受け付け、そのまま確認メモに載る"""
        errs = self.acc(task("finish_title"), {"title": TITLE,
                                               "guesses_md": "- 3章の声質: ここは人の考察を入れる場所。著者が書き足すと完成する"})
        self.assertTrue(errs)

    def test_verify_checks_web_refs_in_music_chapter(self):
        """検算は、新しく書き切る3〜5章（music）の [W番号] も research と照らす"""
        self.assertEqual(self.research(), [])
        (self.d / "outputs" / "chapters" / "music.md").write_text(music_md("ジャンルはハイパーポップ [W9]。"), encoding="utf-8")
        st = self.st()
        fw.service_assemble(self.a, st)
        fw.service_verify(self.a, st)
        ver = json.loads((self.d / "outputs" / "verify.json").read_text(encoding="utf-8"))
        self.assertIn("ウェブで調べたことに無い出どころの番号: W9", ver["errors"])
        self.assertNotIn("ウェブで調べたことに無い出どころの番号: W1", ver["errors"])


# ---------------------------------------------------------------------------
# 著者の名乗り・執筆クレジット・会社の案内（a7ca685 の AUTHOR_RE）
# ---------------------------------------------------------------------------
class TestAuthorRe(Base):
    def author_errs(self, text: str) -> list:
        return [e for e in fw.check_no_placeholder(text) if "著者の名乗り" in e]

    def chapter_author_errs(self, sentence: str) -> list:
        body = HEAD["path_P1"] + "\n\n" + sentence + "本文の段落。" * 80
        return [e for e in fw.check_chapter(self.a, body, HEAD["path_P1"]) if "著者の名乗り" in e]

    def test_catches_the_style_guide_forms(self):
        """文体ガイドに書かれた形（挨拶・クレジット・定型の導入の「弊社では」・募集・締め・連絡先・月報の名乗り）は止める"""
        for s in ("こんにちは、山本です！今回は『テスト曲 / だれか』についてのレポートになります。",
                  "執筆：スイ・山本慶太朗（株式会社ハイトリンク）", "弊社では【イノベーター理論】を用いて分析をしております。",
                  "【お仕事大募集中！】", "今回の執筆は『スイさん』でした。ありがとうございました！", "info@keitarocomp.com まで",
                  "【2025.6ver】今月のTikTok流行曲 Report written by Hi-Trink Inc."):
            with self.subTest(s=s):
                self.assertTrue(self.author_errs(s))

    def test_catches_other_forms_written_in_the_style_guide(self):
        """【疑い】文体ガイド（同梱の STYLE_GUIDE.md）に書かれているのに AUTHOR_RE が見逃す名乗り・会社の案内:
        一人称の「山本」「当社」（104行「一人称は『山本』『僕』『筆者』『弊社』『我々』『当社』が混在（山本＋弊社を維持）」、39行）、
        【山本の気づき・こばなし】（54行）、講座の締め「コンサル依頼募集中！」（26行）、クレジットの言い換え「執筆者：」"""
        for s in ("山本は、ここがこの曲の分岐点だと見ています。", "当社の内部分析ノートを一部お見せします。",
                  "【山本の気づき・こばなし】曲ではなく文化として広がった。", "コンサル依頼募集中！", "執筆者：スイ"):
            with self.subTest(s=s):
                self.assertTrue(self.author_errs(s), "見逃した")

    def test_does_not_flag_ordinary_words(self):
        """ふつうの言葉（お仕事帰り・別の人の「山本さん」・文体ガイドが使う「執筆時点」）は止めない"""
        for s in ("お仕事帰りの社会人が踊る投稿が続いた。", "振付を担当した山本さん（公式）の投稿", "執筆時点での UGC 数は約1.6万。",
                  "山本彩さんのカバーも話題になった。"):
            with self.subTest(s=s):
                self.assertEqual(self.chapter_author_errs(s), [])

    def test_does_not_flag_a_quoted_brand_caption(self):
        """【疑い】企業の公式アカウント（tier official_brand）の説明文やコメントの「弊社」を引用すると、著者の会社の案内として差し戻す。
        引用の文言は変えない決まり（finish.md）と食い違い、AI は引用を書き換えるか消すしかない"""
        s = "企業の公式アカウントが『弊社の新入社員で踊ってみました！』と投稿した動画（seq 3、@user3、2025-07-04、50万再生）が伸びた。"
        self.assertEqual(self.chapter_author_errs(s), [])

    def test_does_not_flag_a_quoted_creator_named_yamamoto(self):
        """【疑い】山本という名の投稿者の名乗りを引用すると（『どうも、山本です！』）、著者の名乗りとして差し戻す"""
        s = "ダンサーの投稿『どうも、山本です！今日はこの曲で踊ります』（seq 0、@user0、2025-07-01、300万再生）が起点になった。"
        self.assertEqual(self.chapter_author_errs(s), [])


# ---------------------------------------------------------------------------
# 「ChatGPT につなぐ」が足す web_search = "live"（codex_link.py）
# ---------------------------------------------------------------------------
class TestCodexWebSearch(unittest.TestCase):
    """Codex の設定ファイルは一時フォルダに（本物の ~/.codex には触らない）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("UGC_CODEX_HOME", "UGC_CODEX_SKILLS", "UGC_COLLECTOR_HOME")}
        os.environ["UGC_CODEX_HOME"] = str(Path(self.tmp) / "codex")
        os.environ["UGC_CODEX_SKILLS"] = str(Path(self.tmp) / "skills")
        os.environ["UGC_COLLECTOR_HOME"] = str(Path(self.tmp) / "home")
        if str(ROOT / "collector") not in sys.path:
            sys.path.insert(0, str(ROOT / "collector"))
        from collector_app import codex_link
        self.cl = importlib.reload(codex_link)
        self.assertTrue(str(self.cl.CONFIG).startswith(self.tmp))   # 本物の設定ファイルを指していない

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        self.cl.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        self.cl.CONFIG.write_text(text, encoding="utf-8")

    def cfg(self) -> dict:
        return tomllib.loads(self.cl.CONFIG.read_text(encoding="utf-8"))

    def test_multiline_string_with_a_bracket_line(self):
        """【疑い】いちばん上の階層の複数行の文字列（例: developer_instructions）に「[」で始まる行があると、_with_web はそこを
        最初の表の見出しと取り違え、文字列の中に web_search の行を差し込む。利用者の指示文が書き換わり、web_search も効かない"""
        orig = ('model = "gpt-5"\ndeveloper_instructions = """\n日本語で答える。\n\n[重要] 敬語を使う\n"""\n\n'
                '[desktop]\nsansFontSize = 14\n')
        self.write(orig)
        self.cl.connect()
        d = self.cfg()
        self.assertEqual(d["developer_instructions"], tomllib.loads(orig)["developer_instructions"])
        self.assertEqual(d.get("web_search"), "live")

    def test_multiline_nested_array_still_connects(self):
        """【疑い】いちばん上の階層の複数行の配列で、要素の行が「[」で始まる（配列の配列）と、web_search の行を配列の中に差し込み、
        TOML が崩れて「書き足すと設定ファイルの形が崩れるため、やめました」でつなげなくなる（0.6.1 ではつなげた形）"""
        orig = 'model = "gpt-5"\nfoo = [\n  ["a", "b"],\n  ["c", "d"],\n]\n\n[desktop]\nx = 1\n'
        self.write(orig)
        try:
            self.cl.connect()
        except self.cl.LinkError as e:
            self.fail(f"つなげなかった: {e}")
        d = self.cfg()
        self.assertEqual(d["foo"], [["a", "b"], ["c", "d"]])
        self.assertEqual(d.get("web_search"), "live")

    def test_user_choice_kept_and_round_trip(self):
        """利用者が決めた web_search は触らない。足した行は2回つないでも1つ、外すと元の設定と同じ中身に戻る"""
        mine = 'web_search = "disabled"\n\n[desktop]\nx = 1\n'
        self.write(mine)
        self.cl.connect()
        self.assertEqual(self.cfg()["web_search"], "disabled")
        self.assertEqual(self.cl.CONFIG.read_text(encoding="utf-8").count("web_search"), 1)
        for orig in ('model = "gpt-5"\n# 自分のメモ\n\n[desktop]\nx = 1\n', '[desktop]\nx = 1\n', ""):
            with self.subTest(orig=orig):
                self.write(orig)
                self.cl.connect()
                once = self.cl.CONFIG.read_text(encoding="utf-8")
                self.cl.connect()
                self.assertEqual(self.cl.CONFIG.read_text(encoding="utf-8"), once)
                self.assertEqual(once.count(self.cl.WEB_LINE), 1)
                self.assertEqual(self.cfg()["web_search"], "live")
                self.cl.disconnect()
                self.assertEqual(self.cfg(), tomllib.loads(orig))


if __name__ == "__main__":
    unittest.main()
