"""速さの実験の結果を、同じ動画の前回の取得と比べる"""
import json
import statistics as st
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from breakdown import label, load  # noqa: E402

OLD = Path.home() / "Library/Application Support/UGC Analyzer/analyses/a20261004-0837-4e69/fetch_log/comments_summary.json"
NEW = Path("/Users/belle/workspace/TikTok-ugc-analyzer/output/speedtest-20261005") / (sys.argv[1] if len(sys.argv) > 1 else "comments_summary.json")


def per_video(path):
    """動画ごとの「開いてから次の動画を開くまで」の秒数と、要求の数"""
    s, rows, order, groups = load(path)
    opens = [i for i, g in enumerate(groups) if label(g) == "open"]
    out = {}
    for k, v in enumerate(order):
        if k >= len(opens):
            break
        a = opens[k]
        b = opens[k + 1] if k + 1 < len(opens) else None
        n = sum(len(g["calls"]) for g in groups[a:(b if b is not None else len(groups))])
        dur = (groups[b]["t"] - groups[a]["t"]) if b is not None else None
        out[v] = {"dur": dur, "calls": n}
    return s, rows, order, groups, out


so, rows_o, order_o, groups_o, pv_o = per_video(OLD)
sn, rows_n, order_n, groups_n, pv_n = per_video(NEW)
ro = {r["video_id"]: r for r in rows_o}
rn = {r["video_id"]: r for r in rows_n}
vids = [v for v in order_n if v in ro]
print(f"今回 {len(rows_n)}本（前回と比べられる {len(vids)}本）/ 状態 {dict(Counter(r.get('status') for r in sn['rows']))}")

# 時間
both = [v for v in vids if pv_o[v]["dur"] is not None and pv_n.get(v, {}).get("dur") is not None]
to = sum(pv_o[v]["dur"] for v in both)
tn = sum(pv_n[v]["dur"] for v in both)
print(f"\n■ 時間（開いてから次を開くまで、両方そろう{len(both)}本の合計）: 前回 {to/60:.1f}分 → 今回 {tn/60:.1f}分（{100*tn/to:.0f}%）")
for c in (40, 120):
    vs = [v for v in both if ro[v].get("cap") == c]
    if vs:
        print(f"  上限{c}: {len(vs)}本 1本 平均 前回{st.mean(pv_o[v]['dur'] for v in vs):.0f}秒 → 今回{st.mean(pv_n[v]['dur'] for v in vs):.0f}秒")
t_first, t_last = groups_n[0]["t"], groups_n[-1]["t"]
calls_n = sum(len(g["calls"]) for g in groups_n)
print(f"  今回の走行: 最初の要求から最後の要求まで {(t_last-t_first)/60:.1f}分 / 要求 {calls_n}回（毎分{calls_n/((t_last-t_first)/60):.2f}回）")

# 要求の速さの決まりを守れているか
times = sorted(c["t"] for g in groups_n for c in g["calls"])
win = max(sum(1 for x in times if t <= x < t + 60) for t in times)
gaps = [b - a for a, b in zip(times, times[1:])]
print(f"  60秒窓の最大 {win}回 / 間隔の最短 {min(gaps):.1f}秒 / 30秒未満の間隔 {sum(1 for g in gaps if g < 30)}回（うち2秒未満 {sum(1 for g in gaps if g < 2)}回）")

# 要求の数
co = sum(pv_o[v]["calls"] for v in vids)
cn = sum(pv_n[v]["calls"] for v in vids if v in pv_n)
print(f"\n■ 要求の数（同じ{len(vids)}本）: 前回 {co}回 → 今回 {cn}回")
kinds = Counter(label(g) for g in groups_n)
print(f"  今回のかたまりの種類 {dict(kinds)} / 開いた瞬間の要求数 {dict(Counter(len(g['calls']) for g in groups_n if label(g)=='open'))}")
drops = sn.get("dropped_prefetch") or []
aw = Counter()
for d in drops:
    import re
    m = re.search(r"aweme_id=(\d+)", d.get("url", ""))
    aw[d.get("aweme") or (m.group(1) if m else "?")] += 1
print(f"  送らなかった先読み {len(drops)}回（動画{len(aw)}本・1本あたり最多{max(aw.values()) if aw else 0}回）")
print(f"  待ちの間に呼んでしまった {sn.get('early_triggers')}回 / 手前の幅 {sn.get('prescroll_margin')}px / "
      f"次のページを呼んだときの底までの距離 {sorted(sn.get('trigger_dists') or [])}")
pool_ids = set(order_n)
print(f"  止めた先読みのうち、取る予定の動画だったもの: {sum(n for a, n in aw.items() if a in pool_ids)}回 {[a[-5:] for a in aw if a in pool_ids]}")
miss = [(r['video_id'][-5:], r.get('neighbor_pred'), r.get('neighbor_seen')) for r in rows_n
        if 'neighbor_pred' in r and r.get('neighbor_seen') and r.get('neighbor_pred') not in (r.get('neighbor_seen') or [])]
print(f"  隣の見込みが外れた動画: {len(miss)}本 {miss[:5]}")
print(f"  開き直し {sum(r.get('remounted') or 0 for r in rows_n)}回（{[r['video_id'][-5:] for r in rows_n if r.get('remounted')]}）")

# データが同じか
print("\n■ データ（前回 → 今回）")
print(f"{'動画':<8}{'上限':>5}{'取得':>11}{'上位':>11}{'続き':>8}{'返信':>9}{'秒':>13}  SPA 混入")
lost = []
for v in vids:
    o, n = ro[v], rn[v]
    mixed = sum(1 for c in (n.get("comments") or []) if str(c.get("aweme_id")) != v) if "comments" in n else "-"
    print(f"{v[-6:]:<8}{o.get('cap'):>5}{o.get('fetched'):>5} → {n.get('fetched'):<4}{o.get('top_list'):>5} → {n.get('top_list'):<4}"
          f"{str(o.get('has_more')):>3}→{str(n.get('has_more')):<4}{o.get('replies'):>3} → {n.get('replies'):<3}"
          f"{(pv_o[v]['dur'] or 0):>6.0f}→{(pv_n.get(v, {}).get('dur') or 0):<5.0f} {'はい' if n.get('spa') else 'いいえ'}")
    # 前回より少ない取得で、前回は上限まで取れていたもの
    if (n.get("fetched") or 0) < min(o.get("fetched") or 0, o.get("cap") or 0):
        lost.append(v)
print(f"\n前回より少なく、上限にも届いていない動画: {len(lost)}本 {lost}")
capped20 = [r["video_id"] for r in rows_n if r.get("fetched") == 20 and r.get("has_more") and (r.get("top_list") or 0) >= 20]
print(f"20件で頭打ちの疑い: {len(capped20)}本 / SPA でない: {sum(1 for r in rows_n if r.get('spa') is False)}本")
