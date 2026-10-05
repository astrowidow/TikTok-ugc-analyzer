"""
採点用の束を作る（条件名とファイル名を伏せる）。docs/COMMENT_STRATEGY_HANDOVER.md 第5章 段0 の 5。

  python3 analysis/comment_exp/grading/blind.py silhouette            # versions.json の版のうち、まだ束の無いものを全部
  python3 analysis/comment_exp/grading/blind.py silhouette --only b1   # 1つだけ

束: output/comment_exp/grading/<曲>/<符号>/ に report.md・claims.json・items.md・article.md（採点者は GRADER.md に従い grade.json を書く）
- report.md は REPORT.md の付録（「付録」の見出しから後ろ。取った件数が条件をばらすので）を外したもの
- claims.json は、採点する版以外の**元の版（role=base）**の構成案（outline.json）の主張。組の名前（R1…）と並びは束ごとに無作為
- 採点者2人（g1・g2）に別々の束を作る（符号と組の並びが違う）
- 対応表（どの符号がどの版か・組がどの版か・主張がコメントを根拠にしているか）は output/comment_exp/keys/<曲>.json。束とは別の場所に置き、採点者には見せない
"""
import argparse
import json
import random
import re
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402

GRADERS = ["g1", "g2"]


def versions(song: str) -> dict:
    v = json.loads((HERE / "versions.json").read_text(encoding="utf-8"))
    if song not in v:
        sys.exit(f"versions.json に {song} がありません")
    return v[song]


def vdir(p: str) -> Path:
    p = p.replace("{APP}", str(common.APP_DATA)).replace("{HOME}", str(common.HOME))
    return Path(p) if Path(p).is_absolute() else common.ROOT / p


def claims_of(d: Path) -> list:
    o = json.loads((d / "outputs" / "outline.json").read_text(encoding="utf-8"))
    out = []
    for ch in o.get("chapters") or []:
        for c in ch.get("claims") or []:
            ev = c.get("evidence") or []
            out.append({"chapter": ch.get("id"), "claim": c.get("claim", ""), "guess": bool(c.get("guess")),
                        "cid": any(isinstance(e, dict) and e.get("cid") for e in ev),
                        "seq": any(isinstance(e, dict) and e.get("seq") is not None for e in ev)})
    return out


def strip_appendix(md: str) -> str:
    m = re.search(r"(?m)^#{1,3} .*付録.*$", md)
    return md[:m.start()].rstrip() + "\n" if m else md


def article_text(spec) -> str:
    """article: [ファイル] か [[ファイル, 開始行, 終わり行], ...]"""
    parts = []
    for a in spec:
        if isinstance(a, str):
            parts.append((common.ROOT / a).read_text(encoding="utf-8"))
        else:
            lines = (common.ROOT / a[0]).read_text(encoding="utf-8").splitlines()
            parts.append(f"<!-- {Path(a[0]).name} の {a[1]}〜{a[2]} 行 -->\n" + "\n".join(lines[a[1] - 1:a[2]]))
    return "\n\n---\n\n".join(parts) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("song")
    ap.add_argument("--only", help="この版だけ（, 区切り）")
    ap.add_argument("--redo", action="store_true", help="束があっても作り直す（grade.json は消える）")
    ap.add_argument("--partial", action="store_true", help="元の版がそろっていなくても作る（道具の試し用）")
    ap.add_argument("--graders", default=",".join(GRADERS), help="採点者（, 区切り）。シルエットで2人の差が 0.01 以内だったので、節約するときは g1 だけ")
    args = ap.parse_args()
    spec = versions(args.song)
    out = common.EXP / "grading" / args.song
    out.mkdir(parents=True, exist_ok=True)
    key_path = common.EXP / "keys" / f"{args.song}.json"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.exists() else {}
    have = {(k["version"], k["grader"]): code for code, k in key.items()}
    bases = {n: vdir(v["dir"]) for n, v in spec["versions"].items() if v.get("role") == "base"}
    base_claims = {n: claims_of(d) for n, d in bases.items() if (d / "outputs" / "outline.json").exists()}
    if len(base_claims) < len(bases) and not args.partial:
        sys.exit(f"元の版がそろっていません（{sorted(set(bases) - set(base_claims))} の outline.json が無い）。"
                 "参照の組が欠けた束になるので作らない（試しなら --partial）")
    only = set(args.only.split(",")) if args.only else None
    made = []
    for name, v in spec["versions"].items():
        if only and name not in only:
            continue
        d = vdir(v["dir"])
        if not (d / "outputs" / "REPORT.md").exists():
            print(f"  {name}: REPORT.md がまだ無い（{d}）")
            continue
        for g in args.graders.split(","):
            if (name, g) in have and not args.redo:
                continue
            if (name, g) in have:
                shutil.rmtree(out / have[(name, g)], ignore_errors=True)
                key.pop(have[(name, g)], None)
            code = secrets.token_hex(3)
            p = out / code
            p.mkdir()
            (p / "report.md").write_text(strip_appendix((d / "outputs" / "REPORT.md").read_text(encoding="utf-8")),
                                         encoding="utf-8")
            refs = [n for n in base_claims if n != name]
            random.shuffle(refs)
            sets, kmap = {}, {}
            for i, rn in enumerate(refs, 1):
                lab = f"R{i}"
                cl = base_claims[rn]
                sets[lab] = [{"id": f"{lab}-{j:02d}", "claim": c["claim"], "guess": c["guess"]} for j, c in enumerate(cl, 1)]
                kmap[lab] = {"version": rn, "cid": [c["cid"] for c in cl], "chapter": [c["chapter"] for c in cl]}
            (p / "claims.json").write_text(json.dumps({"sets": sets}, ensure_ascii=False, indent=1), encoding="utf-8")
            shutil.copy2(HERE / spec["items"], p / "items.md")
            (p / "article.md").write_text(article_text(spec["article"]), encoding="utf-8")
            key[code] = {"version": name, "grader": g, "sets": kmap}
            made.append((name, g, code))
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, g, code in made:
        print(f"  {name} {g} → {out / code}")
    print(f"束 {len(made)} 個（{out}）")


if __name__ == "__main__":
    main()
