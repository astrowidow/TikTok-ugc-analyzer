"""
対の比較（コメントに基づく発見だけを比べる。PAIRWISE.md）。主張の再現（GRADER.md）は、主張の多くが plan より前の材料
（界隈・段階）で決まるので、コメントを削った影響が平均に埋もれる（2026-10-05、シルエットで1本5件まで削っても 0.91）。そこで足した。

  python3 analysis/comment_exp/grading/pair.py build silhouette b1:b2 prod:b1 prod:b2 all1:b1 all1:b2 top5:b1 top5:b2
  python3 analysis/comment_exp/grading/pair.py agg silhouette

- 対「X:Y」は、X を比べられる側（削った版など）、Y を基準（元の版）として集計する。元の版どうしの対は、ぶれの基準
- 束: output/comment_exp/pairs/<曲>/<符号>/ に A.md・B.md（並びは無作為。付録は外す）。対応表は output/comment_exp/keys/<曲>_pairs.json
"""
import json
import random
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402
from blind import strip_appendix, vdir, versions  # noqa: E402

SCORE = {"A がはっきり良い": 2, "A がやや良い": 1, "同じくらい": 0, "B がやや良い": -1, "B がはっきり良い": -2}
# --macro: 大局の物差し（PAIRWISE_MACRO.md。流れ・理由づけ・核・示唆だけを比べる。2026-10-05 ユーザー
# 「レポートの価値は大きなバズの流れをとらえて理由を言語化すること。網羅に重きを置きたいわけではない」）
MACRO = "--macro" in sys.argv
if MACRO:
    sys.argv.remove("--macro")
SUFFIX = "_macro" if MACRO else ""


def key_path(song: str) -> Path:
    p = common.EXP / "keys" / f"{song}_pairs{SUFFIX}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build(song: str, pairs: list) -> None:
    spec = versions(song)["versions"]
    kp = key_path(song)
    key = json.loads(kp.read_text(encoding="utf-8")) if kp.exists() else {}
    have = {(k["x"], k["y"]) for k in key.values()}
    out = common.EXP / f"pairs{SUFFIX}" / song
    for pr in pairs:
        x, y = pr.split(":")
        if (x, y) in have:
            continue
        code = secrets.token_hex(3)
        d = out / code
        d.mkdir(parents=True)
        ab = [x, y]
        random.shuffle(ab)
        for lab, v in zip("AB", ab):
            md = (vdir(spec[v]["dir"]) / "outputs" / "REPORT.md").read_text(encoding="utf-8")
            (d / f"{lab}.md").write_text(strip_appendix(md), encoding="utf-8")
        key[code] = {"x": x, "y": y, "A": ab[0], "B": ab[1]}
        print(f"  {x}:{y} → {d}")
    kp.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")


def agg(song: str) -> None:
    spec = versions(song)["versions"]
    key = json.loads(key_path(song).read_text(encoding="utf-8"))
    rows = []
    for code, k in key.items():
        p = common.EXP / f"pairs{SUFFIX}" / song / code / "judge.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        s = SCORE.get(j.get("verdict"), None)
        x_is_a = k["A"] == k["x"]
        sx = None if s is None else (s if x_is_a else -s)       # X から見た点（+ なら X が良い）
        ox, oy = (j.get("only_A") or [], j.get("only_B") or []) if x_is_a else (j.get("only_B") or [], j.get("only_A") or [])
        big = lambda xs: sum(1 for f in xs if f.get("weight") == "大")
        axes = {a: (lambda v: None if v is None else (v if x_is_a else -v))(SCORE.get((j.get("axes") or {}).get(a, {}).get("verdict")))
                for a in ("flow", "reasons", "core", "lessons")}
        rows.append({"x": k["x"], "y": k["y"], "score": sx, "verdict": j.get("verdict"), "axes": axes,
                     "x_only_big": big(ox), "y_only_big": big(oy), "x_only": len(ox), "y_only": len(oy),
                     "lost": [(f.get("finding") or f.get("claim") or "")[:60] for f in oy if MACRO or f.get("weight") == "大"]})
    rows.sort(key=lambda r: (spec.get(r["x"], {}).get("role") != "base", r["x"], r["y"]))
    if MACRO:
        print(f"## {song}（大局の対の比較。点は X から見て +2 はっきり良い〜-2 はっきり悪い）\n")
        print("| X | Y（基準） | 総合 | 流れ | 理由づけ | 核 | 示唆 | X にだけの大局の主張 | Y にだけ＝X に無い大局の主張 |")
        print("|---|---|---|---|---|---|---|---|---|")
        f = lambda v: "—" if v is None else f"{v:+d}"
        for r in rows:
            a = r["axes"]
            print(f"| {r['x']} | {r['y']} | {f(r['score'])} | {f(a['flow'])} | {f(a['reasons'])} | {f(a['core'])} | {f(a['lessons'])} | "
                  f"{r['x_only']} | {r['y_only']}：{'／'.join(r['lost']) or '—'} |")
        return
    print(f"## {song}（対の比較。点は X から見て +2 はっきり良い〜-2 はっきり悪い）\n")
    print("| X | Y（基準） | 点 | X にだけ（大/全） | Y にだけ（大/全）＝X が落とした | X が落とした大きな発見 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['x']} | {r['y']} | {r['score']:+d} | {r['x_only_big']}/{r['x_only']} | {r['y_only_big']}/{r['y_only']} | "
              f"{'／'.join(r['lost']) or '—'} |")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2], sys.argv[3:])
    elif sys.argv[1] == "agg":
        agg(sys.argv[2])
    else:
        sys.exit(__doc__)
