"""コメント取得の時間の内訳（要求の記録から）"""
import json
import statistics as st
import sys
from collections import Counter, defaultdict

SPACING = (30.0 + (2 * 60 / 1.8 - 30.0)) / 2   # pending_spacing の平均（U(30, 36.7)）


def load(path):
    s = json.load(open(path))
    rows = [r for r in s["rows"] if r.get("status") in ("ok", "no_comments")]
    order = [r["video_id"] for r in rows]
    # 同時に飛んだ要求（2秒以内）を1つのかたまりにする
    groups = []
    for q in s["requests"]:
        if not q.get("path"):
            continue
        t = q["sentAt"] / 1000
        rep = "reply" in q["path"]
        aw = q.get("req_aweme") or q.get("aweme")
        c = {"t": t, "rep": rep, "aw": aw, "cur": q.get("url_cursor"), "n": q.get("n")}
        if groups and t - groups[-1]["t"] < 2.0:
            groups[-1]["calls"].append(c)
        else:
            groups.append({"t": t, "calls": [c]})
    return s, rows, order, groups


def label(g):
    if any(not c["rep"] and c["cur"] == "0" for c in g["calls"]):
        return "open"
    if any(c["rep"] for c in g["calls"]):
        return "reply"
    return "page"


def main(path):
    s, rows, order, groups = load(path)
    calls = [c for g in groups for c in g["calls"]]
    total = groups[-1]["t"] - groups[0]["t"]
    print(f"走行 {total/60:.0f}分 / 要求 {len(calls)}回（平均 毎分{len(calls)/(total/60):.2f}回）/ 動画 {len(order)}本")

    # 動画を開いた瞬間のかたまりの中身: 自分の1ページ目を要求したか
    opens = [g for g in groups if label(g) == "open"]
    p1_count = Counter(c["aw"] for c in calls if not c["rep"] and c["cur"] == "0")
    opened = set(order)
    refetch = sum(1 for v in order if p1_count.get(v, 0) >= 2)
    never_opened = sum(n for v, n in p1_count.items() if v not in opened)
    print(f"1ページ目の要求 {sum(p1_count.values())}回: 取った動画{len(order)}本のうち {refetch}本は2回以上要求（先読み＋開いたとき）"
          f" / 取らない動画への先読み {never_opened}回")
    sizes = Counter(len([c for c in g['calls'] if not c['rep'] and c['cur'] == '0']) for g in opens)
    print(f"開いた瞬間のかたまり: {dict(sizes)}（1ページ目の要求が何本同時に飛んだか）")

    gaps = defaultdict(list)
    for a, b in zip(groups, groups[1:]):
        gaps[(label(a), label(b))].append(b["t"] - a["t"])
    print(f"\n{'前→後':<14}{'回数':>6}{'中央値':>8}{'平均':>8}{'合計(分)':>9}{'割合':>7}")
    for k, v in sorted(gaps.items(), key=lambda kv: -sum(kv[1])):
        print(f"{k[0]+'→'+k[1]:<14}{len(v):>6}{st.median(v):>8.1f}{st.mean(v):>8.1f}{sum(v)/60:>9.1f}{100*sum(v)/total:>6.1f}%")

    # 待ちの規則で決まる最短（平均 SPACING 秒、ただし直近60秒に2回まで）と、それを超えた分
    over = 0.0
    window_extra = 0.0
    for i in range(1, len(groups)):
        g, prev = groups[i], groups[i - 1]
        gap = g["t"] - prev["t"]
        # 60秒窓: 直近60秒の要求数 + これから飛ぶ数 <= 2
        n_new = len(g["calls"])
        recent = [c["t"] for gg in groups[max(0, i - 6):i] for c in gg["calls"]]
        need_t = prev["t"] + SPACING
        ts = sorted(recent, reverse=True)
        # 窓に空きができる時刻
        allowed = 2 - n_new
        if allowed < len(ts):
            need_t = max(need_t, ts[allowed] + 60.0) if allowed >= 0 else need_t
        rule = need_t - prev["t"]
        if rule > SPACING:
            window_extra += min(gap, rule) - SPACING
        over += max(0.0, gap - rule)
    print(f"\n待ちの規則の合計（平均間隔{SPACING:.1f}秒＋60秒窓）: {(total-over)/60:.0f}分 / うち60秒窓で延びた分 {window_extra/60:.0f}分")
    print(f"規則を超えて空いた時間（スクロール・ページの待ちなど）: {over/60:.0f}分（{100*over/total:.0f}%）")

    caps = Counter(r.get("cap") for r in rows)
    for c in sorted(caps):
        rs = [r for r in rows if r.get("cap") == c]
        print(f"上限{c}: {len(rs)}本 1本 平均{st.mean(r['seconds'] for r in rs):.0f}秒 "
              f"取得 平均{st.mean(r.get('fetched') or 0 for r in rs):.0f}件 スクロール{st.mean(r.get('scrolls') or 0 for r in rs):.0f}段")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print("#####", p)
        main(p)
        print()
