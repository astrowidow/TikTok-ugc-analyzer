"""
動画の一覧を楽曲ページごとに上から一部だけ取ったら、レポートに要る動画をどれだけ失うか（2026-10-06 ユーザー「通常は音源がひとつ
なわけだから、音源の数で割り算した分しか取得しないとしても、全然いけちゃうと思う。下の方に出てくる動画って弱小動画が多いから、
どっちにしろ弾かれる」）。AI も TikTok も使わない。

  .venv/bin/python analysis/comment_exp/list_cut_study.py

楽曲ページのグリッドの並び（grid_links.jsonl の order。ページごと）の上から keep の割合だけ残したと見なして数える:
  正解の動画  select_study と同じ（元の量の版3本のうち2本以上で、引用・熟読・言及された動画）
  必ず入れる動画  プールの起点・大型ヒット・本人・公式・山の上位など（pool.tsv の reasons。週ごとの動画は除く）
  最初期  投稿日の早い20本（起点・イノベーターの材料）
"""
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
import select_study as ss  # noqa: E402

KEEPS = [1.0, 1 / 2, 1 / 3, 1 / 5]
EARLY = 30   # 再生順で選ぶときも必ず残す、投稿日時（動画の番号）の早い本数


def grid_rank(d: Path) -> dict:
    """動画 → (ページ, ページの中の順位 0〜, ページの本数)"""
    pages = collections.defaultdict(list)
    for x in ss.jl(d / "raw/grid_links.jsonl"):
        pages[int(x.get("source") or 1)].append(x)
    out = {}
    for k, xs in pages.items():
        xs.sort(key=lambda x: x["order"])
        for i, x in enumerate(xs):
            out[str(x["video_id"])] = (k, i, len(xs))
    return out


def kept(rank: dict, keep: float) -> set:
    """ユーザーの案: 楽曲ページごとに、グリッドの上から keep の割合"""
    return {v for v, (_k, i, n) in rank.items() if i < max(1, round(n * keep))}


def kept_plays(rank: dict, recs: dict, keep: float) -> set:
    """もう一つの案: 全ページを通して、同じ本数を「投稿日時の早い EARLY 本」＋「再生の多い順」で（再生はグリッドのサムネに出る数の代わりに属性の再生）"""
    budget = sum(max(1, round(n * keep)) for n in {k: n for k, _i, n in rank.values()}.values())
    vids = list(rank)
    early = sorted(vids, key=lambda v: int(v))[:EARLY]   # 動画の番号の上の桁は作られた時刻
    out = set(early)
    for v in sorted(vids, key=lambda v: -int((recs.get(v) or {}).get("plays") or 0)):
        if len(out) >= budget:
            break
        out.add(v)
    return out


def main():
    for name, aid, bases in ss.SONGS:
        d = common.APP_DATA / "analyses" / aid
        L = ss.load(aid, bases)
        recs, pool, labs = L["recs"], L["pool"], L["labs"]
        rank = grid_rank(d)
        must = {v for v, p in pool.items() if any(r and not r.startswith("week") for r in (p.get("reasons") or "").split(","))}
        early = sorted(recs, key=lambda v: (recs[v]["date"], -int(recs[v]["plays"])))[:20]
        truth_all = set().union(*L["truth"].values())
        print(f"\n## {name}（{aid}）: 一覧 {len(rank)}本・ページ {len({k for k, _i, _n in rank.values()})}つ・正解の動画 {len(truth_all)}本"
              f"（引用 {len(L['truth']['引用'])}・熟読 {len(L['truth']['熟読'])}・言及 {len(L['truth']['言及'])}）・必ず入れる {len(must)}本")
        print("| 残す割合 | 一覧 | 再生の合計 | 引用 | 熟読 | 言及 | 必ず入れる | 最初期20本 | ラベル | 失う正解の動画（再生・日付・界隈・ページの順位） |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        total = sum(int(r["plays"]) for r in recs.values())
        for keep, how, K in [(k, h, kept(rank, k) if h == "上から" else kept_plays(rank, recs, k))
                             for k in KEEPS for h in (["上から"] if k == 1.0 else ["上から", "再生順"])]:
            lost = sorted(truth_all - K, key=lambda v: -int(recs[v]["plays"]))
            desc = "、".join(f"{int(recs[v]['plays']):,}・{recs[v]['date']}・{labs.get(v, '-')}・p{rank[v][0]} {rank[v][1] + 1}/{rank[v][2]}"
                            for v in lost[:8]) + ("…" if len(lost) > 8 else "")
            frac = lambda s: f"{len(s & K)}/{len(s)}"   # noqa: E731
            print(f"| {keep:.2f} {how} | {len(K)} | {sum(int(recs[v]['plays']) for v in K if v in recs) / total:.0%} | "
                  f"{frac(L['truth']['引用'])} | {frac(L['truth']['熟読'])} | {frac(L['truth']['言及'])} | {frac(must)} | "
                  f"{len(set(early) & K)}/20 | {len(set(labs) & K)}/{len(labs)} | {desc or '—'} |")
        for keep in KEEPS[1:]:
            for how, K in (("上から", kept(rank, keep)), ("再生順", kept_plays(rank, recs, keep))):
                why = collections.Counter(r for v in must - K for r in (pool[v].get("reasons") or "").split(",")
                                          if r and not r.startswith("week"))
                print(f"- 残す {keep:.2f} {how} で落ちる必ず入れる動画の理由: {dict(why.most_common()) or 'なし'}")
        # ぶれの目安: 元の量の版それぞれが、正解の動画（3本のうち2本以上）をどれだけ名指ししているか
        print(f"- 元の版ごとの言及の再現（ぶれの目安）: " + "、".join(f"{x:.0%}" for x in L.get("base_recall", [])))


if __name__ == "__main__":
    main()
