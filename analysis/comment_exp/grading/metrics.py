"""
機械で見る指標（AI を使わない）。版ごとに: 構成案の主張の数・コメントを根拠にした主張・引用した cid・検算・AI の作業量。

  python3 analysis/comment_exp/grading/metrics.py silhouette
"""
import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from blind import claims_of, strip_appendix, vdir, versions  # noqa: E402

CID_RE = re.compile(r"cid[ 　:：]*(\d{15,20})")
SEQ_RE = re.compile(r"seq[ 　]*(\d+)")


def work(d: Path) -> dict:
    """plan より後の AI の作業（task_log）。参考記事を固定した写しでは ref_select・ref_digest は入らない"""
    st = json.loads((d / "state" / "tasks.json").read_text(encoding="utf-8"))
    plan = next((t for t in st["tasks"] if t["type"] == "plan"), None)
    post = {t["task_id"] for t in st["tasks"] if plan and t["n"] > plan["n"] and t["kind"] == "ai"}
    c = collections.Counter()
    t0 = t1 = None
    for line in open(d / "state" / "task_log.jsonl", encoding="utf-8"):
        e = json.loads(line)
        if e.get("task_id") in post or (e.get("event") == "read" and t0):
            if e["event"] == "issue":
                c["issue_chars"] += e.get("chars", 0)
                t0 = t0 or e["ts"]
            elif e["event"] == "read":
                c["read_chars"] += e.get("chars", 0) or 0
                c["reads"] += 1
            elif e["event"] == "done":
                c["done"] += 1
                c["submit_chars"] += e.get("chars_in", 0)
                t1 = e["ts"]
            elif e["event"] == "reject":
                c["rejects"] += 1
    import datetime
    mins = None
    if t0 and t1:
        mins = round((datetime.datetime.fromisoformat(t1) - datetime.datetime.fromisoformat(t0)).total_seconds() / 60, 1)
    return {**c, "minutes": mins}


def one(d: Path) -> dict:
    o = d / "outputs"
    if not (o / "REPORT.md").exists():
        return {}
    rep = strip_appendix((o / "REPORT.md").read_text(encoding="utf-8"))
    cl = claims_of(d)
    ver = json.loads((o / "verify.json").read_text(encoding="utf-8")) if (o / "verify.json").exists() else {}
    meta = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    exp = meta.get("experiment") or {}
    return {"claims": len(cl), "claims_cid": sum(c["cid"] for c in cl), "claims_guess": sum(c["guess"] for c in cl),
            "report_chars": len(rep), "cids": sorted(set(CID_RE.findall(rep))), "seqs": len(set(SEQ_RE.findall(rep))),
            "thin": rep.count("根拠薄"), "verify_errors": len(ver.get("errors") or []),
            "verify_warnings": len(ver.get("warnings") or []), "cuts": exp.get("cuts"),
            "comments": (exp.get("comments_after") or {}).get("comments"), "work": work(d)}


def main():
    song = sys.argv[1]
    spec = versions(song)
    rows = {n: one(vdir(v["dir"])) for n, v in spec["versions"].items()}
    rows = {n: r for n, r in rows.items() if r}
    bases = [n for n, v in spec["versions"].items() if v.get("role") == "base" and n in rows]
    print(f"| 版 | 削り方 | コメント | 主張 | うち cid | 推測 | 本文字数 | 引用 cid | 元の版との重なり（Jaccard） | 根拠薄 | 検算エラー | AI の仕事 | 差し戻し | 読んだ字数 | 分 |")
    print("|" + "---|" * 15)
    for n, r in rows.items():
        cs = set(r["cids"])
        ov = []
        for b in bases:
            if b == n:
                continue
            bs = set(rows[b]["cids"])
            ov.append(f"{b} {len(cs & bs) / max(1, len(cs | bs)):.2f}")
        w = r["work"]
        print(f"| {n} | {'+'.join(r['cuts'] or []) or '—'} | {r['comments'] or '—'} | {r['claims']} | {r['claims_cid']} | {r['claims_guess']} | "
              f"{r['report_chars']:,} | {len(cs)} | {'、'.join(ov)} | {r['thin']} | {r['verify_errors']} | "
              f"{w.get('done', 0)} | {w.get('rejects', 0)} | {w.get('read_chars', 0):,} | {w.get('minutes')} |")


if __name__ == "__main__":
    main()
