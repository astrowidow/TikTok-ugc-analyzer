"""走行の要求の記録（comments_summary.json の requests）から、実際の速さ・60秒窓の最大・要求の間隔を出す（2026-10-05、間隔の実験）

  python3 analysis/comment_pace/pace_actual.py output/pacetest-20261005/comments_summary.json [...]
"""
import json
import statistics as st
import sys

for p in sys.argv[1:]:
    s = json.load(open(p))
    req = sorted(r["sentAt"] for r in s.get("requests") or [] if r.get("sentAt") is not None)
    rows = [r for r in s["rows"] if r.get("status") in ("ok", "no_comments")]
    if len(req) < 2:
        print(p, "要求の記録が足りない"); continue
    span = (req[-1] - req[0]) / 1000 / 60
    j = 0
    win = []
    for i, t in enumerate(req):
        while req[j] < t - 60_000:
            j += 1
        win.append(i - j + 1)
    gaps = [(b - a) / 1000 for a, b in zip(req, req[1:])]
    empty = sum(1 for r in s.get("requests") or [] if r.get("status") == 200 and not r.get("len"))
    bad = sum(1 for r in s.get("requests") or [] if (r.get("status") or 200) >= 400)
    print(f"{p}\n  本数 {len(rows)}・要求 {len(req)}回・最初から最後の要求まで {span:.0f}分 → 平均 {len(req)/span:.2f}回/分"
          f"\n  60秒窓の最大 {max(win)}回（3回以上 {sum(1 for w in win if w >= 3)}回・4回以上 {sum(1 for w in win if w >= 4)}回）"
          f"\n  要求の間隔: 中央値 {st.median(gaps):.1f}秒・20秒未満 {sum(1 for g in gaps if g < 20)}回・25秒未満 {sum(1 for g in gaps if g < 25)}回"
          f"\n  空応答 {empty}回・4xx/5xx {bad}回")
