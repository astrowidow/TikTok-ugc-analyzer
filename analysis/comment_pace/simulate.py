"""今のコードの待ちの規則を再現して、改善案ごとの所要時間を見積もる（TikTok には触らない）

動画ごとの「仕事」（開いた瞬間に飛んだ要求数・ページ送りの回数・返信の回数・底で粘ったか）は実走の記録から取り、
待ちの規則（acquire/spatest.py の pace_wait / note_call / 60秒窓）と、各操作の所要（スクロール1段など）を当てて走らせ直す。
"""
import json
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from breakdown import label, load  # noqa: E402


def scripts(path):
    """動画ごとの仕事を記録から作る"""
    s, rows, order, groups = load(path)
    opens = [i for i, g in enumerate(groups) if label(g) == "open"]
    assert len(opens) == len(rows), (len(opens), len(rows))
    out = []
    for k, r in enumerate(rows):
        a = opens[k]
        b = opens[k + 1] if k + 1 < len(opens) else len(groups)
        g_open = groups[a]
        body = groups[a + 1:b]
        n_open = len(g_open["calls"])
        pool_ids = set(order)
        # 開いた瞬間の要求のうち、取らない動画（プール外）への先読み
        wasted_prefetch = sum(1 for c in g_open["calls"] if c["aw"] not in pool_ids)
        seq = []
        for g in body:
            seq.append("reply" if label(g) == "reply" else "page")
        stalled = (r.get("scrolls") or 0) >= 45 and not r.get("has_more")
        out.append({"n_open": n_open, "wasted": wasted_prefetch, "seq": seq, "stalled": stalled,
                    "load_fast": (r.get("load_seconds") or 0) < 10})
    return out


class Pacer:
    def __init__(self, cpm=1.8, max_cpm=2.0, rng=None):
        self.spacing = 60.0 / cpm
        self.min_spacing = 60.0 / max_cpm
        self.max_cpm = max_cpm
        self.last = -1e9
        self.pending = self.min_spacing
        self.times = []
        self.rng = rng
        self.gaps = {}
        self.prev_kind = None

    def wait(self, t, need=1):
        need = min(need, int(self.max_cpm))
        t = max(t, self.last + self.pending)
        while True:
            self.times = [x for x in self.times if t - x < 60.0]
            if len(self.times) + need <= self.max_cpm:
                return t
            t = self.times[0] + 60.0 + 0.01

    def note(self, t, n=1, kind=None):
        if kind and self.prev_kind:
            self.gaps.setdefault((self.prev_kind, kind), []).append(t - self.last)
        if kind:
            self.prev_kind = kind
        self.last = t
        self.times.extend([t] * max(n, 1))
        lo, hi = self.min_spacing, max(2 * self.spacing - self.min_spacing, self.min_spacing)
        self.pending = self.rng.uniform(lo, hi)


def simulate(vids, opt, seed=0):
    rng = random.Random(seed)
    P = Pacer(opt.get("cpm", 1.8), opt.get("max_cpm", 2.0), rng)
    step = opt.get("step", 1.05)            # スクロール1段（1秒の待ち＋操作）
    steps_per_page = opt.get("steps_per_page", 7)
    t = 0.0
    calls = 0
    gaps = {}
    last = [None, None]

    def rec(tt, kind):
        if last[0] is not None:
            gaps.setdefault((last[1], kind), []).append(tt - last[0])
        last[0], last[1] = tt, kind
    for i, v in enumerate(vids):
        if i:
            t += 15.0 * rng.uniform(1.0, 1.5)        # 動画の間の間隔
        n_open = v["n_open"] - (v["wasted"] if opt.get("drop_prefetch") else 0)
        n_open = max(n_open, 0)
        t = P.wait(t, need=max(n_open, 1) if opt.get("need_actual") else 2)
        t += 0.5 + 1.0                                # クリック〜要求
        if n_open:
            calls += n_open
            rec(t, "open")
        t += 1.5                                      # 表示を待つ（1秒刻みの確認）
        if n_open:
            P.note(t, n_open)
        has_pages = "page" in v["seq"]
        skip_scroll = opt.get("stop_on_no_more") and v["stalled"] and not has_pages
        if not skip_scroll:
            # 開き直し（閉じる→待つ→開く）
            t += 2.5
            t = P.wait(t)
            t += 2.5
        seq = list(v["seq"])
        n_pages = 0
        while seq and seq[0] == "page":
            seq.pop(0)
            n_pages += 1
        for k in range(n_pages):
            t = P.wait(t)
            t += step * opt.get("steps_to_trigger", steps_per_page)
            calls += 1
            rec(t, "page")
            t += 1.0
            # 上限・上位リストで抜けるときは、最後のページを数える前に抜ける（今のコードの動き）
            if k < n_pages - 1 or v["stalled"]:
                P.note(t, 1)
        if v["stalled"] and not opt.get("stop_on_no_more"):
            t += opt.get("stall_secs", 75)            # 底で粘る（12段×(下り直し3+1)）
        elif not v["stalled"]:
            t += step * 1                             # 上限・上位リストの確認の1段
        # 返信
        if seq:
            t += 1.5                                  # 一番上へ戻す
        for kind in seq:
            t = P.wait(t)
            t += 0.6
            calls += 1
            rec(t, "reply")
            t += 1.0
            P.note(t, 1)
            t += 4.0                                  # 続けて届くのを待つ
            t += 0.5                                  # 畳む
        t += 2.5                                      # 楽曲ページに戻る
    simulate.gaps = gaps
    return t / 60, calls


def run(path, actual_min):
    vids = scripts(path)
    variants = [
        ("今のまま（再現）", {}),
        ("A 続きが無いと分かったら粘らない", {"stop_on_no_more": True}),
        ("B ページ送りの待ちの間に下まで送っておく", {"steps_to_trigger": 1.5}),
        ("A+B", {"stop_on_no_more": True, "steps_to_trigger": 1.5}),
        ("C 取らない動画への先読みを止める", {"drop_prefetch": True, "need_actual": True}),
        ("A+B+C", {"stop_on_no_more": True, "steps_to_trigger": 1.5, "drop_prefetch": True, "need_actual": True}),
        ("D 60秒窓を2回→3回（平均1.8回/分は同じ）", {"max_cpm": 3.0}),
        ("A+B+D", {"stop_on_no_more": True, "steps_to_trigger": 1.5, "max_cpm": 3.0}),
        ("A+B+C+D", {"stop_on_no_more": True, "steps_to_trigger": 1.5, "drop_prefetch": True, "need_actual": True,
                     "max_cpm": 3.0}),
    ]
    base = None
    print(f"実績 {actual_min:.0f}分 / 動画 {len(vids)}本")
    print(f"{'案':<40}{'分':>6}{'比':>7}{'要求':>6}{'毎分':>6}")
    for name, opt in variants:
        ms, cs = zip(*(simulate(vids, opt, seed) for seed in range(20)))
        m, c = st.mean(ms), st.mean(cs)
        base = base or m
        print(f"{name:<40}{m:>6.0f}{100*m/base:>6.0f}%{c:>6.0f}{c/m:>6.2f}")
        if CAL and not opt:
            for k, g in sorted(simulate.gaps.items(), key=lambda kv: -sum(kv[1])):
                print(f"     {k[0]}→{k[1]:<8}{len(g):>5}{st.mean(g):>7.1f}秒")


CAL = "--cal" in sys.argv
if __name__ == "__main__":
    base = Path.home() / "Library/Application Support/UGC Analyzer/analyses"
    for rel, actual in [("a20260930-2342-0035/fetch_log/comments_summary.json", 630.2),
                        ("a20261004-0837-4e69/fetch_log/comments_summary.json", None),
                        ("a20261004-0837-4e69/fetch_log/comments_summary_p2.json", None)]:
        p = base / rel
        s = json.load(open(p))
        print("#####", rel)
        run(p, actual or s.get("elapsed_min"))
        print()
