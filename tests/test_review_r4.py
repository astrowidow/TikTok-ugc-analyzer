"""領域 R4 の見直しの試験: レポートを書く仕事の流れ（W1、flow_w1.py）と、AI の出力の検査（accept）・検算・指示書の置き場・利用者の設定。

TikTok・AI・Chrome・本物の置き場（~/Library/Application Support/UGC Analyzer）に触れない。分析フォルダ・知識ベース・指示書・設定は
すべて一時フォルダに作る（土台は tests/test_deepen.make_analysis の分析フォルダに、指示書の組み立てに要る欄を足したもの）。

  collector/.venv/bin/python -m unittest tests.test_review_r4 -v

試験は「正しい動き」を期待する形で書いてある。失敗する試験は、見直しで見つけた不具合の疑い（docstring の【疑い】）。
"""
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_w1 as fw  # noqa: E402
import prompt_store as ps  # noqa: E402
import proto_runner as pr  # noqa: E402
import user_settings as us  # noqa: E402
from tests.test_deepen import AID, CH, comment, make_analysis, vid  # noqa: E402

GOOD_TAX = {
    "community": {"dancer": "ダンスを主な発信内容にしている人。屋外で踊る人も含む", "student": "一般の中高生。制服・学校のタグ",
                  "unknown": "判断できない"},
    "format": {"dance": "曲に合わせて踊る投稿", "lipsync": "口パクで歌う投稿", "unknown": "判断できない"},
    "motive": {"fun": "楽しいから使ったと読める", "trend": "流行りに乗ったと読める", "unknown": "判断できない"},
    "tier": ["official_artist", "official_brand", "large_creator", "general", "unknown"],
    "examples": {"dancer": [0, 1], "student": [6]},
}
CARDS = [{"file": f"2024-01-0{i}_{c}.md", "title": f"記事{c}", "song": f"別の曲{c}", "date": f"2024-01-0{i}", "kind": "楽曲分析"}
         for i, c in enumerate("abcd", 1)]
PLAYS = {0: 3_000_000, 1: 1_000_000, 2: 800_000, 3: 500_000, 4: 50_000, 5: 2_000_000, 6: 900_000, 7: 700_000}


def cid(s, i):
    """make_analysis が作ったコメントの cid（動画 seq s の i 件目）"""
    return comment(vid(s), i)["cid"]


def _jsonl(p: Path) -> list:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_jsonl(p: Path, rows) -> None:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def build_template(base: Path) -> Path:
    """試験の土台の分析フォルダ（1回だけ作り、試験ごとに写す）"""
    d = make_analysis(base)
    extra = {"type": "video", "shares": 10, "duration_s": 15, "text_language": "ja", "location_created": "JP", "hashtags": ["ダンス"],
             "uses_original_sound": True, "suggested_words": ["ダンス"], "sticker_texts": [], "tiktok_labels": ["Dance"]}
    for p in (d / "derived" / "llm_input" / "records.jsonl", d / "derived" / "videos.jsonl"):
        _write_jsonl(p, [{**r, **extra} for r in _jsonl(p)])
    (d / "derived" / "llm_input" / "taxonomy_sample.md").write_text(
        "# 分類軸を考えるためのサンプル\n\n" + "".join(f"### seq {s} | 2025-07-01 | video | 再生 1\n- 説明文: x\n- サムネイル: sheets 内の seq {s}\n\n"
                                                for s in range(8)), encoding="utf-8")
    _write_jsonl(d / "derived" / "sample_label.jsonl", [{"seq": s} for s in range(8)])
    for name in ("taxonomy.json", "taxonomy_proposal.json"):
        (d / "outputs" / name).write_text(json.dumps(GOOD_TAX, ensure_ascii=False), encoding="utf-8")
    # 一覧 CSV（Excel 用 ZIP の元。store.py の13列）
    import store
    with open(d / "derived" / "list.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(store.FIXED_HEAD + ["Song Name"])
        for s in range(8):
            w.writerow([s, f"https://www.tiktok.com/@user{s}/video/{vid(s)}", "2025-07-01 00:00:00", "説明", "10", "1", "2",
                        str(PLAYS[s]), "0", "0", "Video", f"user{s}", "テスト曲"])
    return d


def task(typ, params=None, n=50, kind="ai", status="pending", title=None):
    return {"n": n, "task_id": f"{AID}/{n:03d}-{typ}", "type": typ, "kind": kind, "title": title or typ, "params": params or {},
            "status": status, "first_issued_at": None, "issued_at": None, "issue_count": 0, "done_at": None, "rejects": 0}


def state(*tasks) -> dict:
    return {"analysis_id": AID, "flow": "w1", "next_n": 200, "tasks": list(tasks)}


def long_body(head: str, text: str = "") -> str:
    return head + "\n\n" + text + "本文の段落。" * 80 + "\n"


class Base(unittest.TestCase):
    """一時フォルダに分析・知識ベース・設定・指示書の置き場を作り、モジュールの置き場を差し替える（終わったら戻す）"""

    @classmethod
    def setUpClass(cls):
        cls._tpl_tmp = tempfile.TemporaryDirectory()
        cls.template = build_template(Path(cls._tpl_tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tpl_tmp.cleanup()

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)   # flow_w1 の open() の閉じ忘れの警告（試験の結果に関わらない）
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "analyses").mkdir()
        self.d = base / "analyses" / AID
        shutil.copytree(self.template, self.d)
        kb = base / "kb"
        (kb / "distilled").mkdir(parents=True)
        (kb / "notes").mkdir()
        (kb / "distilled" / "cards.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CARDS), encoding="utf-8")
        for c in CARDS:
            (kb / "notes" / c["file"]).write_text(f"# {c['title']}\n\n## 1. 章\n本文\n", encoding="utf-8")
        self.saved = (pr.ANALYSES_DIR, pr.LOCAL, fw.KB_DIR, fw.REPORTS_DIR, us.USERS_DIR, os.environ.get("UGC_PROMPTS_DIR"))
        pr.ANALYSES_DIR = base / "analyses"
        pr.LOCAL = None
        fw.KB_DIR = kb
        fw.REPORTS_DIR = None            # 成果物を本物のレポートのフォルダに写さない
        us.USERS_DIR = base / "users"
        os.environ.pop("UGC_PROMPTS_DIR", None)
        fw._SAME_SONG.clear()
        self.a = pr.Analysis(AID)

    def tearDown(self):
        pr.ANALYSES_DIR, pr.LOCAL, fw.KB_DIR, fw.REPORTS_DIR, us.USERS_DIR, env = self.saved
        if env is None:
            os.environ.pop("UGC_PROMPTS_DIR", None)
        else:
            os.environ["UGC_PROMPTS_DIR"] = env
        fw._SAME_SONG.clear()
        self.tmp.cleanup()

    def accept(self, t, raw, st=None):
        """accept は形の違う出力を、例外でなく差し戻し（理由の一覧）で返すはず"""
        st = st or state(t)
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        try:
            return fw.accept(self.a, t, raw, st)
        except Exception as e:   # noqa: BLE001
            self.fail(f"差し戻しにならず例外で落ちた: {type(e).__name__}: {e}")

    def render(self, t, st=None):
        return fw.render(self.a, t, st or state(t))["text"]


# ---------------------------------------------------------------------------
# 指示書の差し込みの印（{{…}}）が全部埋まるか
# ---------------------------------------------------------------------------
RENDER_CASES = [
    ("axes", {}), ("confirm", {}), ("label", {"batch": 1, "n_batches": 1, "seqs": list(range(8))}), ("phases", {}),
    ("comments", {"seq": 0, "i": 1, "n": 1}), ("synthesis", {"community": "dancer", "i": 1, "n": 2}),
    ("ccomments", {"community": "dancer", "i": 1, "n": 2}),
    ("dcomments", {"community": "dancer", "round": 1, "instruction": "なぜバズったのかが弱い"}),
    ("outline_revise", {"community": "dancer", "round": 1, "instruction": "なぜ"}),
    ("review", {"community": "dancer", "round": 1, "instruction": "なぜ", "changed": [{"chapter": "path_P1", "why": "層"}],
                "thesis_changed": False}),
    ("ref_select", {}),
    ("ref_digest", {"i": 1, "n": 3, "file": CARDS[0]["file"], "title": "記事a", "date": "2024-01-01", "why": "近い"}),
    ("outline", {}),
    *[("write", {"chapter": ch, "i": i, "n": 6, "first": i == 1}) for i, ch in enumerate(CH, 1)],
    ("write", {"chapter": "path_P1", "i": 1, "n": 1, "first": False, "rewrite": True, "community": "dancer", "round": 1,
               "instruction": "なぜ", "why": "層を足す"}),
    *[("finish", {"chapter": ch, "i": i, "n": 6}) for i, ch in enumerate(CH, 1)],
    ("finish_title", {}), ("revise", {"instruction": "2章を短く"}),
    ("recut", {"instruction": "dancer を屋内と屋外に分けて", "round": 1}), ("done", {}),
]
LEFT_RE = re.compile(r"\{\{[a-z_]+\}\}")


class TestPlaceholders(Base):
    def test_every_task_type_fills_all_placeholders(self):
        """初期の指示書で、どの仕事の種類も {{…}} を残さずに AI に渡す"""
        for typ, params in RENDER_CASES:
            with self.subTest(typ=typ, params=params):
                text = self.render(task(typ, params))
                self.assertIsNone(LEFT_RE.search(text), f"{typ}: 埋まらない印 {LEFT_RE.findall(text)}")

    def test_seeded_user_prompts_also_fill(self):
        """利用者の置き場に写した指示書（アプリの形）でも、{{…}} を残さない。出力の形の節は初期のもの"""
        os.environ["UGC_PROMPTS_DIR"] = str(Path(self.tmp.name) / "prompts")
        ps.seed()
        for typ, params in RENDER_CASES:
            with self.subTest(typ=typ):
                self.assertIsNone(LEFT_RE.search(self.render(task(typ, params))))

    def test_user_edit_reaches_prompt(self):
        """利用者が本文に足した一文は、次に渡す仕事の指示書に入る（出力の形の節を直しても初期のものに戻る）"""
        os.environ["UGC_PROMPTS_DIR"] = str(Path(self.tmp.name) / "prompts")
        ps.seed()
        p = Path(self.tmp.name) / "prompts" / "w1" / "label.md"
        text = p.read_text(encoding="utf-8").replace("### 確定した分類軸", "（利用者の追記）制服は学生にする\n\n### 確定した分類軸")
        text = text.replace("- conf は H / M / L", "- conf は A / B / C")      # 出力の形の節の直しは使われない
        p.write_text(text, encoding="utf-8")
        out = self.render(task("label", {"batch": 1, "n_batches": 1, "seqs": [0, 1]}))
        self.assertIn("（利用者の追記）制服は学生にする", out)
        self.assertIn("- conf は H / M / L", out)
        self.assertNotIn("A / B / C", out)

    def test_user_prompt_missing_placeholder_falls_back(self):
        """本文の差し込みの印を消した指示書は使わず、初期の指示書で渡す（{{…}} の欠けた指示書を AI に渡さない）"""
        os.environ["UGC_PROMPTS_DIR"] = str(Path(self.tmp.name) / "prompts")
        ps.seed()
        p = Path(self.tmp.name) / "prompts" / "w1" / "label.md"
        p.write_text(p.read_text(encoding="utf-8").replace("{{entries}}", "（消した）"), encoding="utf-8")
        out = self.render(task("label", {"batch": 1, "n_batches": 1, "seqs": [0]}))
        self.assertNotIn("（消した）", out)
        self.assertIn("### seq 0 |", out)
        self.assertTrue(ps.put("label.md", "中身\n### 出力の形\n"))           # 道具からの書き換えも断る

    def test_prompt_change_while_task_is_out(self):
        """仕事を渡したあとに指示書が直されても、受け取りの検査は変わらない（出力の形は初期のもの）。渡し直すと新しい指示書になる"""
        t = task("label", {"batch": 1, "n_batches": 1, "seqs": [0, 6]})
        before = self.render(t)
        os.environ["UGC_PROMPTS_DIR"] = str(Path(self.tmp.name) / "prompts")
        ps.seed()
        p = Path(self.tmp.name) / "prompts" / "w1" / "label.md"
        p.write_text(p.read_text(encoding="utf-8").replace("## 指示書", "## 指示書\n\n（直した）"), encoding="utf-8")
        self.assertNotIn("（直した）", before)
        self.assertIn("（直した）", self.render(t))
        tsv = "seq\tcommunity\tformat\tmotive\ttier\tconf\treason\n0\tdancer\tdance\tfun\tgeneral\tH\tbio に「ダンス」\n" \
              "6\tstudent\tdance\tfun\tgeneral\tM\tタグ #学校\n"
        self.assertEqual(self.accept(t, tsv), [])


# ---------------------------------------------------------------------------
# accept: 形の違う JSON は差し戻す（例外で落とさない）
# ---------------------------------------------------------------------------
class TestAcceptShapes(Base):
    """【疑い】形の違う JSON（配列・文字列・要素が文字列）で accept が例外を出す。道具は「サービス側のエラーです（AttributeError）。
    少し待って同じ道具を呼び直してください」と返すので、AI は同じ出力で呼び直して同じ所で落ち続ける（直す手掛かりが無い）"""

    def test_axes_array_json(self):
        self.assertTrue(self.accept(task("axes"), [GOOD_TAX]))

    def test_axes_examples_value_not_list(self):
        self.assertTrue(self.accept(task("axes"), {**GOOD_TAX, "examples": {"dancer": 5, "student": [6]}}))

    def test_confirm_taxonomy_not_object(self):
        t = task("confirm", kind="ask_user")
        self.assertTrue(self.accept(t, {"user_answer": "dancer を2つに", "taxonomy": "dancer を屋内と屋外に分ける"}))

    def test_revise_array_json(self):
        self.assertTrue(self.accept(task("revise", {"instruction": "x"}), [{"chapter": "music", "markdown": "x"}]))

    def test_outline_claims_as_strings(self):
        chs = fw.chapter_order(self.a, None)
        obj = {"thesis": "主張", "chapters": [{"id": c, "role": "r", "claims": ["主張だけを書いた"], "bridge": "b"} for c in chs]}
        self.assertTrue(self.accept(task("outline"), obj))

    def test_outline_revise_claims_as_strings(self):
        obj = {"thesis": "主張", "chapters": [{"id": "path_P1", "claims": ["主張だけ"]}], "rewrite": [{"chapter": "path_P1", "why": "w"}],
               "note_to_user": "n"}
        self.assertTrue(self.accept(task("outline_revise", {"community": "dancer", "round": 1, "instruction": "x"}), obj))

    def test_ref_select_references_as_strings(self):
        self.assertTrue(self.accept(task("ref_select"), {"references": [c["file"] for c in CARDS[:3]]}))


# ---------------------------------------------------------------------------
# accept: 検査の中身（通る試験。壊れた・空・重複・存在しない seq や cid を止め、正しいものは受け取る）
# ---------------------------------------------------------------------------
class TestAcceptChecks(Base):
    def test_axes_and_confirm(self):
        self.assertTrue(self.accept(task("axes"), "これが案です: {"))                                   # JSON でない
        self.assertTrue(self.accept(task("axes"), {**GOOD_TAX, "examples": {"dancer": [0, 99], "student": [6]}}))   # サンプルに無い
        self.assertTrue(self.accept(task("axes"), {**GOOD_TAX, "examples": {"dancer": [0]}}))          # 界隈の代表が無い
        self.assertTrue(self.accept(task("axes"), {**GOOD_TAX, "community": {"dancer": "x", "unknown": "y"}}))
        self.assertEqual(self.accept(task("axes"), "```json\n" + json.dumps(GOOD_TAX, ensure_ascii=False) + "\n```"), [])
        t = task("confirm", kind="ask_user")
        self.assertTrue(self.accept(t, {"taxonomy": GOOD_TAX}))                                         # 利用者の答えが無い
        self.assertEqual(self.accept(t, {"user_answer": "OK"}), [])
        self.assertFalse(json.loads((self.d / "outputs" / "confirm_answer.json").read_text(encoding="utf-8"))["modified"])
        merged = {**GOOD_TAX, "community": {"dancer": GOOD_TAX["community"]["dancer"], "student": GOOD_TAX["community"]["student"],
                                            "outdoor": "屋外で踊る人。公園・路上のサムネ", "unknown": "判断できない"}}
        self.assertEqual(self.accept(t, {"user_answer": "屋外を分けて", "taxonomy": merged}), [])
        self.assertTrue(json.loads((self.d / "outputs" / "confirm_answer.json").read_text(encoding="utf-8"))["modified"])

    def test_label_tsv(self):
        seqs = [0, 1, 6]
        t = task("label", {"batch": 1, "n_batches": 1, "seqs": seqs})
        head = "seq\tcommunity\tformat\tmotive\ttier\tconf\treason"
        good = [f"{s}\t{'student' if s == 6 else 'dancer'}\tdance\tfun\tgeneral\tH\tbio に「ダンス」" for s in seqs]
        cases = {
            "空": "", "ヘッダ違い（地域の列）": head.replace("tier", "region\ttier") + "\n" + "\n".join(good),
            "重複": head + "\n" + "\n".join(good + [good[0]]), "足りない": head + "\n" + "\n".join(good[:2]),
            "対象外": head + "\n" + "\n".join(good + ["7\tstudent\tdance\tfun\tgeneral\tH\tbio に「学校」"]),
            "軸に無い key": head + "\n" + "\n".join(good).replace("dancer", "dancers", 1),
            "列が多い": head + "\n" + "\n".join(good).replace("ダンス」", "ダンス」\t余り", 1),
            "conf": head + "\n" + "\n".join(good).replace("\tH\t", "\th\t", 1),
            "seq が数字でない": head + "\n" + "\n".join(good).replace("0\t", "x\t", 1),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assertTrue(self.accept(t, raw))
        self.assertFalse((self.d / "outputs" / "labels" / "batch_01.tsv").exists())
        self.assertEqual(self.accept(t, "```tsv\n" + head + "\n" + "\n".join(reversed(good)) + "\n```"), [])
        labs = fw.labels(self.a)
        self.assertEqual(sorted(labs), seqs)
        self.assertEqual((labs[6]["community"], labs[6]["region"]), ("student", "JP"))    # 地域はサービスがデータから付ける

    def test_ccomments(self):
        t = task("ccomments", {"community": "dancer", "i": 1, "n": 2})
        body = ("### dancer\n#### コメントの全体傾向\nx\n#### この界隈が曲を採用した文脈\nx\n#### 段階による変化\nx\n"
                "#### 具体例で確かめた引用\n- seq 0（@user0、2025-07-01、再生 3,000,000）: 『コメント3 すごい』（97 いいね、cid {c}）\n"
                "#### ラベルとのずれ\nなし\n")
        self.assertTrue(self.accept(t, body.format(c="7500000999999999999")))                # 入力に無い cid
        self.assertTrue(self.accept(t, body.format(c="")))                                   # 引用も「根拠薄」も無い
        self.assertTrue(self.accept(t, body.format(c=cid(0, 3)).replace("seq 0", "seq 99")))
        self.assertTrue(self.accept(t, body.format(c=cid(0, 3)).replace("### dancer", "### student")))
        self.assertTrue(self.accept(t, body.format(c=cid(0, 3)).replace("#### 具体例で確かめた引用", "#### 引用")))
        self.assertEqual(self.accept(t, body.format(c=cid(0, 3))), [])
        self.assertEqual(self.accept(t, body.format(c="").replace("なし\n", "なし\n根拠薄\n")), [])

    def test_write_chapter(self):
        t = task("write", {"chapter": "music", "i": 5, "n": 6})
        ok = long_body("## 3. 楽曲の音楽的特徴（内的要因）",
                       f"seq 0（@user0、2025-07-01、再生 3,000,000）。『コメント3 すごい』（97 いいね、cid {cid(0, 3)}）\n")
        self.assertTrue(self.accept(t, ok.replace("seq 0", "seq 42")))                       # 存在しない動画
        self.assertTrue(self.accept(t, ok.replace(cid(0, 3), "7512345678901234567")))        # 入力に無い cid
        self.assertTrue(self.accept(t, ok.replace("## 3.", "## 3 .")))                        # 見出しの書き方を変えた
        self.assertTrue(self.accept(t, ok + "\n用語集でいう『口実』\n"))                       # 読者に見せない言葉
        self.assertTrue(self.accept(t, "## 3. 楽曲の音楽的特徴（内的要因）\n\n短い"))
        self.assertEqual(self.accept(t, "```markdown\n" + ok + "```"), [])
        self.assertTrue((self.d / "outputs" / "chapters" / "music.md").read_text(encoding="utf-8").startswith("## 3. 楽曲の"))
        p1 = task("write", {"chapter": "path_P1", "i": 1, "n": 6})
        self.assertTrue(self.accept(p1, long_body("### (1) 立ち上がり（2025-07-01〜2025-07-31）")))   # 1つ目の段階は章の見出しから
        self.assertEqual(self.accept(p1, long_body("## 1. バズの拡大経路")), [])

    def test_outline(self):
        chs = fw.chapter_order(self.a, None)

        def plan(ev):
            return {"thesis": "全体の主張", "title_idea": "題", "chapters": [
                {"id": c, "role": "r", "claims": [{"claim": "c", "evidence": ev}], "bridge": "b"} for c in chs]}
        self.assertTrue(self.accept(task("outline"), {**plan([{"seq": 0}]), "chapters": plan([{"seq": 0}])["chapters"][::-1]}))
        self.assertTrue(self.accept(task("outline"), {**plan([{"seq": 0}]), "chapters": plan([{"seq": 0}])["chapters"][:-1]}))
        self.assertTrue(self.accept(task("outline"), plan([{"cid": "7599999999999999999"}])))
        self.assertTrue(self.accept(task("outline"), plan([{"seq": 99}])))
        self.assertTrue(self.accept(task("outline"), {**plan([{"seq": 0}]), "thesis": " "}))
        bad = plan([{"seq": 0}])
        bad["chapters"][0]["claims"] = []
        self.assertTrue(self.accept(task("outline"), bad))
        self.assertEqual(self.accept(task("outline"), plan([{"seq": 0}, {"cid": cid(6, 1)}, {"number": "UGC 約13万"}])), [])
        self.assertIn(f"seq 0、cid {cid(6, 1)}、UGC 約13万", (self.d / "outputs" / "outline.md").read_text(encoding="utf-8"))

    def test_ref_select_and_digest(self):
        same = {"file": "2024-02-01_same.md", "title": "テスト曲の記事", "song": "テスト曲", "date": "2024-02-01", "kind": "楽曲分析"}
        with open(fw.KB_DIR / "distilled" / "cards.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(same, ensure_ascii=False) + "\n")
        (fw.KB_DIR / "notes" / same["file"]).write_text("# 【テスト曲 / だれか】の分析\n本文\n", encoding="utf-8")
        dig = [task("ref_digest", {"i": i, "n": 3, "file": None}, n=60 + i) for i in (1, 2, 3)]
        t = task("ref_select", n=59)
        st = state(t, *dig)
        refs = [{"file": c["file"], "why": "近い"} for c in CARDS[:3]]
        with self.assertRaises(pr.RunnerError):                                                # 選ぶ前の参考記事の仕事は渡さない
            self.render(dig[0], st)
        self.assertTrue(self.accept(t, {"references": refs[:2]}, st))                          # 本数が違う
        self.assertTrue(self.accept(t, {"references": refs[:2] + [{"file": "nope.md"}]}, st))
        self.assertTrue(self.accept(t, {"references": refs[:2] + [{"file": same["file"]}]}, st))   # 分析する曲を扱った記事
        self.assertTrue(self.accept(t, {"references": refs[:2] + [refs[0]]}, st))               # 同じ記事を2回
        self.assertEqual(self.accept(t, {"references": refs}, st), [])
        self.assertEqual([x["params"]["file"] for x in dig], [c["file"] for c in CARDS[:3]])
        md = "### 参考1: 記事a\n#### 章立てと各章の役割\nx\n#### 論理の運び\nx\n#### この曲の記事に使える組み立て\nx\n"
        self.assertTrue(self.accept(dig[0], md + "あ" * 2600, st))
        self.assertTrue(self.accept(dig[0], md.replace("#### 論理の運び", "#### 流れ"), st))
        self.assertEqual(self.accept(dig[0], md, st), [])
        self.assertTrue((self.d / "outputs" / "references" / "01.md").exists())

    def test_finish(self):
        t = task("finish", {"chapter": "path_P1", "i": 2, "n": 6})
        url = f"https://www.tiktok.com/@user0/video/{vid(0)}"
        ok = long_body("## 1. バズの拡大経路", f"@user0（2025-07-01、300万再生）— 起点\n{url}\n\n「すごい」（97 いいね）\n")
        for bad in (ok + "seq 0 の投稿\n", ok + "（12 いいね、cid 7551355023343521040）\n", ok + "（P2）の学生\n",
                    ok.replace(url, "https://www.tiktok.com/@zzz/video/1"), ok.replace(f"\n{url}\n", f"\nこちら {url} を見て\n"),
                    ok + "今回集めた動画のうち\n", "## 1. バズの拡大経路\n短い"):
            with self.subTest(bad=bad[-30:]):
                self.assertTrue(self.accept(t, bad))
        self.assertEqual(self.accept(t, ok), [])

    def test_finish_title_and_review(self):
        self.assertTrue(self.accept(task("finish_title"), {"title": "題"}))
        # 0.6.2〜: 確認メモはサービスが組み立てる。AI は推測で書いたところ（guesses_md）だけ。題名に号数（No.—）は付けない
        self.assertTrue(self.accept(task("finish_title"), {"title": "題", "editor_notes_md": "メモ"}))
        self.assertTrue(self.accept(task("finish_title"), {"title": "題 [No.— - 26/10-1]", "guesses_md": "なし"}))
        self.assertEqual(self.accept(task("finish_title"), {"title": "題", "guesses_md": "メモ"}), [])
        rv = task("review", {"community": "dancer", "round": 1, "instruction": "x", "changed": [{"chapter": "path_P1", "why": "w"}],
                             "thesis_changed": True}, n=70)
        st = state(rv, task("done", kind="done", n=71))
        self.assertTrue(self.accept(rv, {"fixes": [{"chapter": "result", "what": "a"}, {"chapter": "result", "what": "b"}]}, st))
        self.assertTrue(self.accept(rv, {"fixes": [{"chapter": "nope", "what": "a"}]}, st))
        self.assertTrue(self.accept(rv, {"fixes": [{"chapter": "result", "what": " "}]}, st))
        self.assertTrue(self.accept(rv, {"note_to_user": "x"}, st))
        self.assertEqual(len(st["tasks"]), 2)                                                    # 差し戻しでは仕事を足さない
        self.assertEqual(self.accept(rv, {"fixes": [{"chapter": "result", "what": "核につなぐ"}], "note_to_user": "n"}, st), [])
        self.assertEqual([(x["type"], x["params"].get("chapter")) for x in st["tasks"][1:-1]],
                         [("write", "result"), ("finish", "path_P1"), ("finish", "result"), ("finish_title", None)])

    def test_outline_revise(self):
        t = task("outline_revise", {"community": "dancer", "round": 1, "instruction": "x"}, n=90)
        rv = task("review", {"community": "dancer", "round": 1, "instruction": "x", "changed": [], "thesis_changed": False}, n=91)
        st = state(t, rv)
        plan = {"thesis": "全体の主張", "thesis_changed": True,
                "chapters": [{"id": "path_P1", "role": "r", "claims": [{"claim": "c", "evidence": [{"cid": cid(0, 2)}]}]}],
                "rewrite": [{"chapter": "path_P1", "why": "層"}, {"chapter": "intro", "why": "主張"}], "note_to_user": "n"}
        self.assertTrue(self.accept(t, {**plan, "rewrite": [{"chapter": "path_P1", "why": "層"}]}, st))   # 主張を変えたのに intro が無い
        self.assertTrue(self.accept(t, {**plan, "rewrite": []}, st))
        self.assertTrue(self.accept(t, {**plan, "rewrite": plan["rewrite"] + [{"chapter": "intro"}]}, st))
        self.assertTrue(self.accept(t, {**plan, "chapters": [{"id": "path_P9", "claims": [{"claim": "c"}]}]}, st))
        self.assertEqual(self.accept(t, plan, st), [])
        self.assertEqual([x["params"].get("chapter") for x in st["tasks"] if x["type"] == "write"], ["path_P1", "intro"])   # 執筆の順
        self.assertTrue(rv["params"]["thesis_changed"])


# ---------------------------------------------------------------------------
# 読者に見せない言葉・内部の印
# ---------------------------------------------------------------------------
class TestReaderWords(Base):
    def test_natural_atsumeta_is_not_work_word(self):
        """【疑い】「再生を集めた動画」「注目を集めた動画」はふつうの言い方なのに、READER_RE の「集めた動画」に当たり、
        執筆（check_chapter）と仕上げ（check_finished）で差し戻す（章の全文を書き直させる）"""
        md = long_body("## 3. 楽曲の音楽的特徴（内的要因）", "この時期に最も再生を集めた動画は seq 0（@user0、2025-07-01、再生 3,000,000）。\n")
        errs = fw.check_chapter(self.a, md, "## 3. 楽曲の音楽的特徴（内的要因）")
        self.assertFalse([e for e in errs if "読者に見せない" in e], errs)
        note = long_body("## 見出し", "いちばん注目を集めた動画は、屋外で踊る投稿でした。\n")
        self.assertFalse([e for e in fw.check_finished(self.a, note, set()) if "読者に見せない" in e])

    def test_card_in_content_is_not_work_word(self):
        """【疑い】動画の中身を書いた「メッセージカードに」「トレカの」も、作業の言葉（カードに・カードの）として差し戻す"""
        md = long_body("## 3. 楽曲の音楽的特徴（内的要因）", "seq 0（@user0、2025-07-01、再生 3,000,000）はメッセージカードに想いを書いて見せる投稿。\n")
        errs = fw.check_chapter(self.a, md, "## 3. 楽曲の音楽的特徴（内的要因）")
        self.assertFalse([e for e in errs if "読者に見せない" in e], errs)

    def test_work_words_still_caught(self):
        """作業の言葉・集めた本数の言い方は止める（執筆も仕上げも）"""
        for w in ("用語集でいう『口実』", "今回集めた投稿では", "30本中12本が", "ラベル付きの動画", "過去レポートでは", "知識ベースの J 章"):
            with self.subTest(w=w):
                md = long_body("## 3. 楽曲の音楽的特徴（内的要因）", w + "\n")
                self.assertTrue([e for e in fw.check_chapter(self.a, md, "## 3. 楽曲の音楽的特徴（内的要因）") if "読者に見せない" in e])
                self.assertTrue([e for e in fw.check_finished(self.a, long_body("## 見出し", w + "\n"), set()) if "読者に見せない" in e])

    def test_phase_id_next_to_japanese_is_caught(self):
        """【疑い】仕上げの検査の内部の印（P1〜）が、日本語に挟まれる（「P2の」「段階P2では」）と見逃される。
        INTERNAL_RE の \\bP\\d\\b は、日本語の文字も語の文字とみなすので境目にならない。検算の note の警告も同じ正規表現で見逃す"""
        for s in ("P2の段階では学生が踊り始めた。", "段階P2では学生が踊り始めた。"):
            with self.subTest(s=s):
                errs = fw.check_finished(self.a, long_body("## 見出し", s + "\n"), set())
                self.assertTrue([e for e in errs if "内部の印" in e], errs)

    def test_internal_marks_caught_when_separated(self):
        """空白や括弧で区切られた内部の印は止める"""
        for s in ("（P2）の学生", "は P2 で", "seq 3 の投稿", "（3 いいね、cid 7551355023343521040）", "本データでは"):
            with self.subTest(s=s):
                self.assertTrue([e for e in fw.check_finished(self.a, long_body("## 見出し", s + "\n"), set()) if "内部の印" in e])


# ---------------------------------------------------------------------------
# 検算（service_verify・play_claims・play_mismatch）
# ---------------------------------------------------------------------------
class TestVerify(Base):
    def verify(self, report, note=None):
        (self.d / "outputs" / "REPORT.md").write_text(report, encoding="utf-8")
        nb = self.d / "outputs" / "NOTE_BODY.md"
        if note is None:
            nb.unlink(missing_ok=True)
        else:
            nb.write_text(note, encoding="utf-8")
        fw.service_verify(self.a, state())
        return json.loads((self.d / "outputs" / "verify.json").read_text(encoding="utf-8"))

    def test_saisei_su_form_is_checked(self):
        """【疑い】「再生数 50万」「再生回数50万回」の書き方の数字は照合されない（間違っていても検算が見逃す）。データは 300万"""
        for s in ("seq 0（@user0、2025-07-01、再生数 50万）が起点。", "seq 0（@user0、2025-07-01、再生回数50万回）が起点。"):
            with self.subTest(s=s):
                self.assertTrue(self.verify(s)["warnings"])

    def test_lower_bound_phrase_is_not_mismatch(self):
        """【疑い】「100万回再生を突破」「100万回再生を超えた」は下限の言い方なのに、ちょうどの数字として照合して誤報する（データは 300万）。
        誤報が1件でもあると、完了の知らせに「数字の一部を運営が確認中」と出る"""
        for s in ("seq 0（@user0、2025-07-01）は100万回再生を突破した。", "seq 0（@user0、2025-07-01）は100万回再生を超えた。"):
            with self.subTest(s=s):
                self.assertEqual(self.verify(s)["warnings"], [])

    def test_verify_basics(self):
        """正しい本文は errors・warnings とも0。存在しない seq・入力に無い cid・ずれた再生数・note の一覧に無い URL・作業の言葉は拾う"""
        ok = ("seq 0（@user0、2025-07-01、再生 3,000,000）。2人並びが200万回再生（seq 5（@user5、再生 2,000,000））。"
              f"seq 1 は100万回再生。『x』（1 いいね、cid {cid(0, 1)}）\n")
        res = self.verify(ok)
        self.assertEqual((res["errors"], res["warnings"]), ([], []))
        self.assertEqual(res["cids"], 1)
        res = self.verify("seq 42 と cid 7599999999999999999。seq 0（@user0、2025-07-01、再生 1,000,000）。")
        self.assertEqual(len(res["errors"]), 2, res)
        self.assertEqual(len(res["warnings"]), 1, res)
        res = self.verify(ok, note="## 見出し\nhttps://www.tiktok.com/@zzz/video/1\n今回集めた投稿\n")
        self.assertTrue([e for e in res["errors"] if "一覧に無い URL" in e])
        self.assertTrue([w for w in res["warnings"] if "作業の言葉" in w])
        self.assertIn("数字の一部を運営が確認中", self.render(task("done", kind="done")))
        self.verify(ok)
        self.assertNotIn("運営が確認中", self.render(task("done", kind="done")))

    def test_play_mismatch_rounding(self):
        """丸めた数字は丸めの幅まで一致とみなす。6% を超えるずれは拾う"""
        self.assertFalse(fw.play_mismatch("285", "万", 2_850_000))
        self.assertFalse(fw.play_mismatch("2.8", "億", 280_400_000))
        self.assertFalse(fw.play_mismatch("12", "万", 125_000))              # fmt_plays の丸め（12.5 → 12）
        self.assertTrue(fw.play_mismatch("250", "万", 2_850_000))
        self.assertTrue(fw.play_mismatch("9,000", None, 9_700))


# ---------------------------------------------------------------------------
# 利用者の設定（user_settings）が効く所・効かない所
# ---------------------------------------------------------------------------
class TestSettingsReach(Base):
    def test_settings_reach_prompts(self):
        """4項目が、その項目を使う仕事の指示書に入る。渡したときの設定は分析フォルダに写る"""
        us.set_item("u1", "community_policy", "制服の有無で分ける")
        us.set_item("u1", "comment_lens", "本家探しを数える")
        us.set_item("u1", "focus", "海外での広がり")
        us.set_item("u1", "style", "です・ます調")
        cases = [("axes", {}, "制服の有無で分ける"), ("label", {"batch": 1, "n_batches": 1, "seqs": [0]}, "制服の有無で分ける"),
                 ("recut", {"instruction": "分けて", "round": 1}, "制服の有無で分ける"),
                 ("ccomments", {"community": "dancer", "i": 1, "n": 2}, "本家探しを数える"),
                 ("dcomments", {"community": "dancer", "round": 1, "instruction": "x"}, "本家探しを数える"),
                 ("write", {"chapter": "path_P1", "i": 1, "n": 6, "first": True}, "海外での広がり"),
                 ("write", {"chapter": "result", "i": 4, "n": 6}, "です・ます調"),
                 ("revise", {"instruction": "x"}, "海外での広がり"), ("finish", {"chapter": "music", "i": 1, "n": 6}, "です・ます調")]
        for typ, params, want in cases:
            with self.subTest(typ=typ, want=want):
                self.assertIn(want, self.render(task(typ, params)))
        rows = _jsonl(self.d / "outputs" / "settings_used.jsonl")
        self.assertEqual(rows[-1]["values"].get("style"), {"use_guide": True, "extra": "です・ます調"})

    def test_focus_reaches_outline(self):
        """【疑い】設定「レポートの重点（focus）」が構成案（outline）の指示書に入らない（outline.md に {{settings}} が無い）。
        構成案が記事全体の主張と章ごとの主張を決め、執筆は「計画に無い大きな主張を足さない」ので、重点の設定が効きにくい"""
        us.set_item("u1", "focus", "海外での広がりを重点に")
        self.assertIn("海外での広がりを重点に", self.render(task("outline")))

    def test_style_without_guide_does_not_point_to_guide(self):
        """【疑い】「著者の文体ガイドを使わない」にしても、執筆の指示書は kb:style（文体ガイド）を読ませ、
        「著者の枠組みと文体で」「著者の後期のトーン」と言い続ける（設定の欄と指示が食い違う）"""
        us.set_item("u1", "style", "", use_style_guide=False)
        text = self.render(task("write", {"chapter": "path_P1", "i": 1, "n": 6, "first": True}))
        self.assertIn("著者の文体ガイドには合わせない", text)
        self.assertNotIn("kb:style", text)


# ---------------------------------------------------------------------------
# 指示書の置き場（prompt_store）
# ---------------------------------------------------------------------------
class TestPromptStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("UGC_PROMPTS_DIR")
        os.environ["UGC_PROMPTS_DIR"] = self.tmp.name
        ps.seed()
        self.w1 = Path(self.tmp.name) / "w1"

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("UGC_PROMPTS_DIR", None)
        else:
            os.environ["UGC_PROMPTS_DIR"] = self.saved
        self.tmp.cleanup()

    def test_output_section_only_placeholder(self):
        """【疑い】「### 出力の形」の節は読むときに初期のものへ差し替わる（README も「直しても使われない」と言う）のに、
        その節にだけある印（write.md の {{length}}・{{heading}} など）を消すと、指示書の本文の編集まで丸ごと捨てて初期の指示書を使う"""
        text = ps.default_text("write.md").replace("### 守ること", "（利用者の追記）結論を先に書く\n\n### 守ること")
        text = text.replace("- 長さの目安: {{length}}", "- 長さは気にしない")
        (self.w1 / "write.md").write_text(text, encoding="utf-8")
        loaded = ps.load("write.md")
        self.assertIn("（利用者の追記）結論を先に書く", loaded)
        self.assertIn("{{length}}", loaded)

    def test_seed_keeps_edited_and_updates_untouched(self):
        """アプリを新しくしたとき、編集していない指示書は新しい初期に入れ替え、編集したものは残す"""
        man = json.loads((self.w1 / ".defaults.json").read_text(encoding="utf-8"))
        (self.w1 / "axes.md").write_text("前の初期の axes\n", encoding="utf-8")
        man["axes.md"] = ps._sha("前の初期の axes\n")
        (self.w1 / "label.md").write_text("利用者が直した label\n", encoding="utf-8")
        man["label.md"] = ps._sha("前の初期の label\n")
        (self.w1 / ".defaults.json").write_text(json.dumps(man), encoding="utf-8")
        done = ps.seed()
        self.assertIn("axes.md", done["updated"])
        self.assertIn("label.md", done["kept_edited"])
        self.assertEqual((self.w1 / "axes.md").read_text(encoding="utf-8"), ps.default_text("axes.md"))
        self.assertEqual((self.w1 / "label.md").read_text(encoding="utf-8"), "利用者が直した label\n")
        self.assertEqual(ps.load("label.md"), ps.default_text("label.md"))          # 使えない（印が無い）→ 初期
        row = next(r for r in ps.status() if r["name"] == "label.md")
        self.assertEqual((row["edited"], row["ok"]), (True, False))

    def test_put_and_reset(self):
        self.assertTrue(ps.put("write.md", ""))
        with self.assertRaises(ps.PromptError):
            ps.put("../secret.md", "x")
        text = ps.default_text("done.md") + "\n（追記）\n"
        self.assertEqual(ps.put("done", text), [])
        self.assertIn("（追記）", ps.load("done.md"))
        ps.reset("done.md")
        self.assertEqual(ps.load("done.md"), ps.default_text("done.md"))


# ---------------------------------------------------------------------------
# 段階の検査
# ---------------------------------------------------------------------------
class TestPhases(Base):
    @staticmethod
    def obj(p1=("2025-07-01", "2025-07-31"), p2=("2025-08-01", "2025-08-31"), **kw):
        o = {"phases": [{"id": "P1", "name": "立ち上がり", "start": p1[0], "end": p1[1], "summary": "s", "reps": [0]},
                        {"id": "P2", "name": "広がり", "start": p2[0], "end": p2[1], "summary": "s", "reps": [6]}],
             "overlooked": [3, 999], "pathway_md": "下書き"}
        o.update(kw)
        return o

    def test_first_phase_reversed_is_rejected(self):
        """【疑い】段階の検査は2つ目以降の start > end しか見ない。P1 の start が end より後（P1=7/1〜6/30）でも通り、
        中身の無い段階 P1 と、その章（拡大経路 (1)）ができる"""
        self.assertTrue(fw.accept_phases(self.a, self.obj(p1=("2025-07-01", "2025-06-30"), p2=("2025-07-01", "2025-08-31"))))

    def test_phases_checks(self):
        self.assertTrue(fw.accept_phases(self.a, self.obj(p2=("2025-08-02", "2025-08-31"))))          # すき間
        self.assertTrue(fw.accept_phases(self.a, self.obj(p1=("2025-07-02", "2025-07-31"), p2=("2025-08-01", "2025-08-31"))))
        self.assertTrue(fw.accept_phases(self.a, self.obj(p2=("2025-08-01", "2025-08-01"))))          # 最後の投稿まで届かない
        self.assertTrue(fw.accept_phases(self.a, self.obj(p1=("2025/07/01", "2025-07-31"))))
        self.assertTrue(fw.accept_phases(self.a, self.obj(pathway_md="")))
        o = self.obj()
        o["phases"][1]["reps"] = [99]
        self.assertTrue(fw.accept_phases(self.a, o))
        o = self.obj()
        o["phases"][1]["id"] = "P3"
        self.assertTrue(fw.accept_phases(self.a, o))
        self.assertTrue(self.accept(task("phases"), {"phases": [self.obj()["phases"][0]], "pathway_md": "x"}))   # 1段階だけ
        self.assertEqual(self.accept(task("phases"), self.obj()), [])
        self.assertEqual(json.loads((self.d / "outputs" / "phases.json").read_text(encoding="utf-8"))["overlooked"], [3])


# ---------------------------------------------------------------------------
# seq を文字列で書いたとき
# ---------------------------------------------------------------------------
class TestSeqAsString(Base):
    def test_seq_string_is_not_called_missing(self):
        """【疑い】seq を文字列（"0"）で書くと、seq 0 はあるのに「存在しない seq 0」「ラベルの付いていない seq: ['0']」と差し戻す。
        理由を読んでも何を直せばよいか分からず、同じ出力を出し直しやすい（構成案の根拠・段階の代表）"""
        chs = fw.chapter_order(self.a, None)
        obj = {"thesis": "主張", "chapters": [{"id": c, "role": "r", "claims": [{"claim": "c", "evidence": [{"seq": "0"}]}], "bridge": "b"}
                                             for c in chs]}
        errs = self.accept(task("outline"), obj)
        self.assertFalse([e for e in errs if "存在しない seq" in e], errs)
        ph = TestPhases.obj()
        ph["phases"][0]["reps"] = ["0"]
        errs = fw.accept_phases(self.a, ph)
        self.assertFalse([e for e in errs if "ラベルの付いていない seq" in e], errs)


# ---------------------------------------------------------------------------
# 掘り下げ: 「前回からの変化」が構成案の直しに渡るか
# ---------------------------------------------------------------------------
class TestDeepenChange(Base):
    def test_change_section_with_suffix_reaches_outline_revise(self):
        """【疑い】掘り下げのコメント分析は「#### 前回からの変化（3点）」のような見出しでも受け付ける（部分一致）が、
        構成案の直し（outline_revise）の指示書は見出しの行が「#### 前回からの変化」ちょうどのときしか中身を拾わず、
        「（掘り下げた分析に「前回からの変化」が無い…）」と渡す"""
        t = task("dcomments", {"community": "dancer", "round": 1, "instruction": "なぜ"})
        md = ("### dancer\n#### コメントの全体傾向\nx\n#### この界隈が曲を採用した文脈\nx\n#### 動画が伸びた理由（見た人の受け取り方）\nx\n"
              f"#### 段階による変化\nx\n#### 具体例で確かめた引用\n- 『コメント3』（97 いいね、cid {cid(0, 3)}）\n#### ラベルとのずれ\nなし\n"
              "#### 前回からの変化（3点）\n- 屋外で技を見せたい層が見えた\n")
        self.assertEqual(self.accept(t, md), [])
        text = self.render(task("outline_revise", {"community": "dancer", "round": 1, "instruction": "なぜ"}))
        self.assertIn("屋外で技を見せたい層が見えた", text)


# ---------------------------------------------------------------------------
# 直し（revise）・完了の知らせ・同じ仕事の2回 submit
# ---------------------------------------------------------------------------
class TestReviseAndDone(Base):
    def write_state(self, st):
        (self.d / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def test_revise_accept(self):
        rv = task("revise", {"instruction": "音楽の章に考察を足して"}, n=80)
        fin = task("finish", {"chapter": None, "i": 1, "n": 1}, n=81)
        st = state(rv, fin, task("assemble", kind="service", n=82))
        self.assertTrue(self.accept(rv, {"chapter": "nope", "markdown": "x"}, st))
        self.assertTrue(self.accept(rv, {"chapter": "music", "markdown": "## 3. 楽曲\n短い"}, st))
        self.assertIsNone(fin["params"]["chapter"])
        md = long_body("## 3. 楽曲の音楽的特徴（内的要因）", "考察を足した。")
        self.assertEqual(self.accept(rv, {"chapter": "music", "markdown": md, "note_to_user": "足した"}, st), [])
        self.assertEqual(fin["params"]["chapter"], "music")
        self.assertTrue(list((self.d / "outputs" / "chapters" / "history").glob("music_*.md")))
        rec = json.loads((self.d / "outputs" / "revisions" / "080.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["chapter"], rec["note"]), ("music", "足した"))
        self.assertIn("note 用に仕上げ直す（music）", fin["title"])

    def test_revise_done_does_not_repeat_confirm_skipped(self):
        """【疑い】界隈の確認を省いた分析で、直し（revise）の完了の知らせにも毎回「界隈の確認を省いた（利用者の頼み）…AI の案のまま書いた
        ことと使った界隈の名前を1行で伝え」が入る（直しの完了は params が空で、初めの完了と見分けていない）。切り直しのあとの直しでは、
        利用者が決めた界隈を「AI の案のまま」と伝えることになる"""
        (self.d / "outputs" / "confirm_answer.json").write_text(json.dumps(
            {"user_answer": pr.SKIP_CONFIRM_ANSWER, "modified": False, "auto": True}, ensure_ascii=False), encoding="utf-8")
        first = task("done", kind="done", status="done", n=10, title="完了")
        rv = task("revise", {"instruction": "x"}, status="done", n=11)
        done2 = task("done", kind="done", n=15, title="完了（直し）")
        st = state(first, rv, task("finish", {"chapter": "music", "i": 1, "n": 1}, status="done", n=12), done2)
        self.assertIn("界隈の確認を省いた", self.render(first, st))          # 初めの完了では伝える
        self.assertNotIn("界隈の確認を省いた", self.render(done2, st))

    def test_revise_tool_when_only_done_notice_left(self):
        """【疑い】完了の知らせ（kind done）だけが未済のまま残った分析に直しを頼むと、「レポートがまだできていません」で断る。
        掘り下げ（deepen）・切り直し（recut）は、残った完了の知らせを済んだことにして受け付ける（proto_runner.revise だけ扱いが違う）"""
        st = json.loads((self.d / "state" / "tasks.json").read_text(encoding="utf-8"))
        st["tasks"][-1]["status"] = "pending"
        self.write_state(st)
        try:
            r = pr.revise("u1", AID, "2章を短く")
        except pr.RunnerError as e:
            self.fail(f"直しを断った: {e}")
        self.assertIn("直しを受け付けました", r["text"])

    def test_double_submit_and_wrong_task(self):
        """同じ仕事を2回 submit しても2回目は何もしない（受領済み）。今の仕事でない task_id・無い task_id は断る"""
        self.write_state(state(task("finish_title", n=1), task("done", kind="done", n=2)))
        t = pr.next_task("u1", AID)
        self.assertIn("finish_title", t["task_id"])
        bad = pr.submit("u1", t["task_id"], json.dumps({"title": "題0"}))
        self.assertFalse(bad["ok"])
        r1 = pr.submit("u1", t["task_id"], json.dumps({"title": "題1", "guesses_md": "メモ1"}, ensure_ascii=False))
        self.assertTrue(r1["ok"], r1)
        r2 = pr.submit("u1", t["task_id"], json.dumps({"title": "題2", "guesses_md": "メモ2"}, ensure_ascii=False))
        self.assertTrue(r2["ok"])
        self.assertIn("受領済み", r2["text"])
        self.assertEqual(json.loads((self.d / "outputs" / "note_meta.json").read_text(encoding="utf-8"))["title"], "題1")
        with self.assertRaises(pr.RunnerError):
            pr.submit("u1", f"{AID}/999-write", "x")
        with self.assertRaises(pr.RunnerError):
            pr.submit("u1", f"{AID}/002-done", "x")


# ---------------------------------------------------------------------------
# Excel 用 ZIP（service_export → store.py → add_labels_to_zip）
# ---------------------------------------------------------------------------
class TestExport(Base):
    def test_export_zip(self):
        """ZIP に動画・コメント・ラベルの表と説明が入り、作業用のフォルダは残らない。2回作っても labels.csv は1つ"""
        import io
        for _ in range(2):
            res = fw.service_export(self.a)
            self.assertEqual(res.get("zip"), "data.zip", res)
        with zipfile.ZipFile(self.d / "outputs" / "data.zip") as z:
            names = z.namelist()
            readme = z.read("README.txt").decode("utf-8")
            labels = z.read("labels.csv").decode("utf-8-sig")
        self.assertTrue({"videos.csv", "comments.csv", "labels.csv", "README.txt"} <= set(names), names)
        self.assertEqual(names.count("labels.csv"), 1)
        self.assertEqual(readme.count("labels.csv    1行 = 1動画"), 1)
        rows = list(csv.reader(io.StringIO(labels)))
        by = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
        self.assertEqual(len(by), 8)
        self.assertEqual((by["6"]["界隈"], by["6"]["段階"], by["6"]["地域"]), ("student", "広がり", "JP"))
        self.assertFalse((self.d / "outputs" / "_zip").exists())

    def test_export_skips_without_sources(self):
        (self.d / "derived" / "list.csv").unlink()
        self.assertIn("skipped", fw.service_export(self.a))


# ---------------------------------------------------------------------------
# アプリの形（成果物をこの Mac のフォルダに置く）・最初の仕事の列・read の資料
# ---------------------------------------------------------------------------
class TestAppFormAndRead(Base):
    def test_done_puts_files_in_report_folder(self):
        """REPORTS_DIR があると、完了の知らせはフォルダのリンクを先頭に、成果物と週ごとの表（BOM つき CSV）をそのフォルダに写す。
        界隈の確認の確認用 CSV も同じフォルダに写す"""
        rep = Path(self.tmp.name) / "レポート"
        fw.REPORTS_DIR = str(rep)
        for name in ("REPORT.md", "NOTE_BODY.md"):
            (self.d / "outputs" / name).write_text("# r\n", encoding="utf-8")
        text = self.render(task("done", kind="done"))
        folder = rep / "テスト曲（2026-10-06）"
        self.assertIn("この Mac に保存しました", text)
        self.assertIn("file://", text)
        # 0.6.2〜: 利用者のフォルダには日本語の名前で置く（NOTE_BODY → レポート.md、REPORT → レポート（根拠の番号つき）.md）。読むほうを先に
        self.assertLess(text.index("フォルダ:"), text.index("[レポート.md]"))
        self.assertLess(text.index("[レポート.md]"), text.index("[レポート（根拠の番号つき）.md]"))
        self.assertTrue((folder / "レポート（根拠の番号つき）.md").exists())
        self.assertTrue((folder / "レポート.md").exists())
        self.assertFalse((folder / "REPORT.md").exists())
        self.assertFalse((folder / "data.zip").exists())                      # 無い成果物は写さず、リンクも出さない
        self.assertNotIn("data.zip", text)
        weekly = (folder / fw.WEEKLY_CSV).read_bytes()
        self.assertTrue(weekly.startswith(b"\xef\xbb\xbf"))
        self.assertIn("週,投稿数,再生の合計", weekly.decode("utf-8-sig"))
        self.assertIn("file://", self.render(task("confirm", kind="ask_user")))
        self.assertTrue((folder / "videos_review.csv").exists())

    def test_build_tasks(self):
        """最初の仕事の列: 界隈の案 → 確認 → ラベル（label_batch 本ずつ）→ 段階 → 以降の準備"""
        m = json.loads((self.d / "analysis.json").read_text(encoding="utf-8"))
        m["ai_settings"] = {"label_batch": 3}
        (self.d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        st = fw.build_tasks(pr.Analysis(AID))
        self.assertEqual([t["type"] for t in st["tasks"]], ["axes", "confirm", "label", "label", "label", "phases", "plan"])
        self.assertEqual([t["params"]["seqs"] for t in st["tasks"] if t["type"] == "label"], [[0, 1, 2], [3, 4, 5], [6, 7]])
        self.assertEqual(len({t["task_id"] for t in st["tasks"]}), 7)

    def test_read_names(self):
        """read の資料: 界隈のコメント・サンプル・章・構成案。分析する曲を扱った記事は読ませない。無い名前は日本語のエラー"""
        (fw.KB_DIR / "notes" / "2024-02-01_same.md").write_text("# 【テスト曲 / だれか】の分析\n本文\n", encoding="utf-8")
        self.assertIn("### seq 0", pr.read("u1", AID, "ccomments:dancer")["text"])
        self.assertIn("### seq 7", pr.read("u1", AID, "taxonomy_sample")["text"])
        self.assertIn("## 1. バズの拡大経路", pr.read("u1", AID, "chapter:path_P1")["text"])
        self.assertIn("seq 0", pr.read("u1", AID, "labeled:dancer")["text"])
        self.assertIn("記事a", pr.read("u1", AID, "note:2024-01-01_a")["text"])
        for name in ("note:2024-02-01_same", "chapter:nope", "kb:nope", "labeled:nope", "outline", "refs", "prev:intro"):
            with self.subTest(name=name), self.assertRaises(pr.RunnerError):
                pr.read("u1", AID, name)


if __name__ == "__main__":
    unittest.main()
