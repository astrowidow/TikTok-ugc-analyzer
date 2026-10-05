"""記事を正解にした大局の採点の束を作る（ARTICLE_MACRO.md。シルエットだけ）。集計も。

  python3 analysis/comment_exp/grading/article_macro.py build c0 m1p
  python3 analysis/comment_exp/grading/article_macro.py agg
"""
import json
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402
from blind import article_text, strip_appendix, vdir, versions  # noqa: E402

SONG = "silhouette"
KEY = common.EXP / "keys" / "silhouette_article_macro.json"
OUT = common.EXP / "article_macro" / SONG


def build(names):
    spec = versions(SONG)
    key = json.loads(KEY.read_text(encoding="utf-8")) if KEY.exists() else {}
    for name in names:
        code = secrets.token_hex(3)
        d = OUT / code
        d.mkdir(parents=True)
        (d / "report.md").write_text(strip_appendix((vdir(spec["versions"][name]["dir"]) / "outputs/REPORT.md").read_text(encoding="utf-8")),
                                     encoding="utf-8")
        (d / "article.md").write_text(article_text(spec["article"]), encoding="utf-8")
        key[code] = name
        print(name, d)
    KEY.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")


def agg():
    key = json.loads(KEY.read_text(encoding="utf-8"))
    print("| 版 | 流れ | 理由づけ | 核 | 示唆 | 計 |")
    print("|---|---|---|---|---|---|")
    for code, name in key.items():
        p = OUT / code / "grade.json"
        if not p.exists():
            print(f"| {name} | （まだ） |||||")
            continue
        g = json.loads(p.read_text(encoding="utf-8"))
        sc = [g[a]["score"] for a in ("flow", "reasons", "core", "lessons")]
        print(f"| {name} | {' | '.join(map(str, sc))} | {sum(sc)} |")


if __name__ == "__main__":
    build(sys.argv[2:]) if sys.argv[1] == "build" else agg()
