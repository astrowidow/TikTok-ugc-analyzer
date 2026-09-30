"""
コメント数・返信の有無を変えたコメント分析（video_analysis.jsonl）を、フル版（120件＋返信）と比べる。

  python3 analysis/compare_ablation.py output/trial_silhouette/e2e/report/video_analysis.jsonl \
      output/trial_silhouette/e2e/ablation/cap20/video_analysis.jsonl [...]

見るもの（動画ごと → 平均）:
  - 反応の型の集合の一致（Jaccard）と、最多（share=多）の型の一致
  - 引用 cid の重なり（フル版の引用のうち、その版でも引用されたもの）
  - why_this_song の語の重なり（粗い代理指標）
"""
import json
import re
import sys


def load(p):
    return {json.loads(l)["seq"]: json.loads(l) for l in open(p, encoding="utf-8")}


def types(r):
    return {t["type"] for t in r.get("reaction_types") or []}


def major(r):
    return {t["type"] for t in r.get("reaction_types") or [] if t.get("share") == "多"}


def words(s):
    return set(re.findall(r"[一-龠ぁ-んァ-ヶA-Za-z0-9]{2,}", s or ""))


full = load(sys.argv[1])
print(f"フル版 {len(full)}本")
for p in sys.argv[2:]:
    v = load(p)
    common = [s for s in full if s in v]
    jac = maj = quo = why = 0.0
    for s in common:
        a, b = full[s], v[s]
        ta, tb = types(a), types(b)
        jac += len(ta & tb) / max(1, len(ta | tb))
        ma, mb = major(a), major(b)
        maj += 1.0 if (ma & mb) or (not ma and not mb) else 0.0
        qa = {q["cid"] for q in a.get("quotes") or []}
        qb = {q["cid"] for q in b.get("quotes") or []}
        quo += (len(qa & qb) / len(qa)) if qa else 1.0
        wa, wb = words(a.get("why_this_song")), words(b.get("why_this_song"))
        why += len(wa & wb) / max(1, len(wa | wb))
    n = max(1, len(common))
    print(f"{p}: {len(common)}本 | 反応の型 Jaccard {jac/n:.2f} | 最多の型一致 {maj/n:.2f} | 引用の再現 {quo/n:.2f} | 動機の語の重なり {why/n:.2f}")
