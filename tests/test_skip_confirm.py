"""界隈の確認を省く（skip_confirm、2026-10-06 ユーザー「界隈の確認はいらないのでそのまま最後まで書いて、のオプションも欲しい」）"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import flow_w1  # noqa: E402
import proto_runner as pr  # noqa: E402

AID = "a20990101-0000-skip"


class TestSkipConfirm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pr.ANALYSES_DIR, flow_w1.accept, pr._taxonomy)
        pr.ANALYSES_DIR = Path(self.tmp.name)
        d = pr.ANALYSES_DIR / AID
        (d / "state").mkdir(parents=True)
        (d / "analysis.json").write_text(json.dumps({"analysis_id": AID, "owner": "local", "title": "テスト曲"}), encoding="utf-8")
        st = {"analysis_id": AID, "flow": "w1", "next_n": 3, "tasks": []}
        st["tasks"].append(flow_w1._task(st, "confirm", "ask_user", "界隈の確認"))
        st["tasks"].append(flow_w1._task(st, "label", "ai", "ラベル"))
        st["tasks"][0]["status"] = "issued"
        self.a = pr.Analysis(AID)
        self.st = st

        def accept(a, t, raw, st):
            pr._write_json(a.outputs("confirm_answer.json"), {"user_answer": json.loads(raw)["user_answer"], "modified": False})
            return []
        flow_w1.accept = accept
        pr._taxonomy = lambda a, confirmed=True: {"community": {"dancer": "ダンスの人。屋外も", "_note": "x", "unknown": "y"}}

    def tearDown(self):
        pr.ANALYSES_DIR, flow_w1.accept, pr._taxonomy = self.saved
        self.tmp.cleanup()

    def test_auto_confirm_moves_on(self):
        t = pr._auto_confirm(self.a, self.st, self.st["tasks"][0])
        self.assertEqual(t["type"], "label")
        self.assertEqual(self.st["tasks"][0]["status"], "done")
        c = json.loads(self.a.outputs("confirm_answer.json").read_text(encoding="utf-8"))
        self.assertTrue(c["auto"])
        self.assertEqual(c["user_answer"], pr.SKIP_CONFIRM_ANSWER)
        saved = json.loads(self.a.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"][0]["status"], "done")
        s = flow_w1.confirm_skipped_summary(self.a)
        self.assertIn("使った界隈（1個）: ダンスの人", s)
        self.assertIn("界隈の切り直しで書き直せます", s)

    def test_falls_back_when_rejected(self):
        flow_w1.accept = lambda a, t, raw, st: ["案のままでは通らない"]
        t = pr._auto_confirm(self.a, self.st, self.st["tasks"][0])
        self.assertEqual(t["type"], "confirm")
        self.assertEqual(self.st["tasks"][0]["status"], "issued")
        self.assertEqual(flow_w1.confirm_skipped_summary(self.a), "")

    def test_options_and_first_reply(self):
        self.assertEqual(pr._options(self.a.dir), {})
        pr._set_options(self.a.dir, skip_confirm=True)
        pr._set_options(self.a.dir, other=1)
        self.assertEqual(pr._options(self.a.dir), {"skip_confirm": True, "other": 1})
        self.assertIn(pr.CONFIRM_ONCE, pr.come_back_line(3600, "集めるの", "x", "そこからレポートを書きます" + pr.CONFIRM_ONCE + "。"))
        self.assertNotEqual(pr.CONFIRM_ONCE, pr.CONFIRM_SKIPPED)


if __name__ == "__main__":
    unittest.main()
