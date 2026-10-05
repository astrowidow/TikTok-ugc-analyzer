"""
採点（grade.json）を集めて、版ごとの点数・元の版どうしのぶれ・「同点」の判定を出す。

  python3 analysis/comment_exp/grading/aggregate.py silhouette

点の付け方（docs/COMMENT_STRATEGY_HANDOVER.md 4-3 の案）:
- 主張の再現: 立つ 1／弱い 0.5／無い・逆 0。参照の組ごとに平均し、組と採点者で平均する。コメントを根拠にした主張（cid つき）だけの値も出す
- 記事の表: ○ 1／△ 0.5／× 0 の合計
- D・E・G: 1〜5 を採点者で平均
- ぶれ: 元の版（role=base）どうしの点の幅。元の版は自分以外の元の版の組で、削った版は元の版全部の組で点を付ける
- 同点: 主張の再現（全体・cid つき）が元の版の最小値以上／記事の表と D・E・G が元の版の最小値以上／★の項目が × でない／検算エラー0
"""
import collections
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402
from blind import versions, vdir  # noqa: E402

V = {"立つ": 1.0, "弱い": 0.5, "無い": 0.0, "逆": 0.0}
I = {"○": 1.0, "△": 0.5, "×": 0.0}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def grade_rows(song: str):
    key = json.loads((common.EXP / "keys" / f"{song}.json").read_text(encoding="utf-8"))
    out = collections.defaultdict(list)
    for code, k in key.items():
        p = common.EXP / "grading" / song / code / "grade.json"
        if not p.exists():
            continue
        g = json.loads(p.read_text(encoding="utf-8"))
        sets = {}
        for lab, info in k["sets"].items():
            got = {x["id"]: x.get("verdict") for x in (g.get("claims") or {}).get(lab, [])}
            ids = [f"{lab}-{j:02d}" for j in range(1, len(info["cid"]) + 1)]
            miss = [i for i in ids if got.get(i) not in V]
            vals = [V.get(got.get(i), 0.0) for i in ids]
            cvals = [v for v, c in zip(vals, info["cid"]) if c]
            sets[info["version"]] = {"all": mean(vals), "cid": mean(cvals), "rev": sum(1 for i in ids if got.get(i) == "逆"),
                                     "missing": len(miss)}
        items = {x["id"]: x.get("verdict") for x in g.get("items") or []}
        out[k["version"]].append({"grader": k["grader"], "code": code, "sets": sets,
                                  "items": sum(I.get(v, 0.0) for v in items.values()), "items_raw": items,
                                  **{a: (g.get(a) or {}).get("score") for a in "DEG"}})
    return out


def main():
    song = sys.argv[1]
    spec = versions(song)
    rows = grade_rows(song)
    star = [ln.split("|")[1].strip().split()[0] for ln in (HERE / spec["items"]).read_text(encoding="utf-8").splitlines()
            if "★" in ln and ln.startswith("|")]
    res = {}
    for name, gs in rows.items():
        res[name] = {
            "all": mean([s["all"] for g in gs for s in g["sets"].values()]),
            "cid": mean([s["cid"] for g in gs for s in g["sets"].values()]),
            "rev": sum(s["rev"] for g in gs for s in g["sets"].values()),
            "missing": sum(s["missing"] for g in gs for s in g["sets"].values()),
            "items": mean([g["items"] for g in gs]), **{a: mean([g[a] for g in gs]) for a in "DEG"},
            "star_x": any(g["items_raw"].get(s) == "×" for g in gs for s in star),
            "graders": len(gs),
            "by_grader": {g["grader"]: round(mean([s["all"] for s in g["sets"].values()]) or 0, 3) for g in gs},
        }
    bases = [n for n, v in spec["versions"].items() if v.get("role") == "base" and n in res]
    vals = {m: [res[b][m] for b in bases if res[b][m] is not None] for m in ("all", "cid", "items", "D", "E", "G")}
    lo = {m: min(xs) for m, xs in vals.items() if xs}
    hi = {m: max(xs) for m, xs in vals.items() if xs}
    errs = {}
    from metrics import one
    for n, v in spec["versions"].items():
        m = one(vdir(v["dir"]))
        errs[n] = m.get("verify_errors") if m else None
    f = lambda x, d=2: "—" if x is None else f"{x:.{d}f}"
    print(f"## {song}\n")
    print("| 版 | 役 | 主張の再現 | うち cid つき | 逆 | 記事の表 | D | E | G | 採点者ごと | 検算エラー | 判定 |")
    print("|" + "---|" * 12)
    for n, v in spec["versions"].items():
        if n not in res:
            continue
        r = res[n]
        verdict = ""
        if v.get("role") != "base" and lo:
            ok = all(r[m] is not None and r[m] >= lo[m] for m in lo) and not r["star_x"] and not errs.get(n)
            verdict = "同点" if ok else "下回る（" + "・".join(m for m in lo if r[m] is None or r[m] < lo[m]) + \
                      ("・★×" if r["star_x"] else "") + ("・検算" if errs.get(n) else "") + "）"
        print(f"| {n} | {v.get('role')} | {f(r['all'])} | {f(r['cid'])} | {r['rev']} | {f(r['items'], 1)} | {f(r['D'], 1)} | "
              f"{f(r['E'], 1)} | {f(r['G'], 1)} | {r['by_grader']} | {errs.get(n)} | {verdict} |"
              + (f" 抜け{r['missing']}" if r["missing"] else ""))
    if lo:
        print("\n元の版のぶれ（最小〜最大）: " + "、".join(f"{m} {f(lo[m])}〜{f(hi[m])}" for m in lo))


if __name__ == "__main__":
    main()
