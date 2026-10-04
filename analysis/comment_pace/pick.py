"""速さの実験に使う20本を、前回の走行（きゃわぽっぴんどぅー 1ページ目）から選ぶ"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from breakdown import label, load  # noqa: E402

A = Path.home() / "Library/Application Support/UGC Analyzer/analyses/a20261004-0837-4e69"
OUT = Path("/Users/belle/workspace/TikTok-ugc-analyzer/output/speedtest-20261005")
OUT.mkdir(parents=True, exist_ok=True)

s, rows, order, groups = load(A / "fetch_log/comments_summary.json")
byid = {r["video_id"]: r for r in rows}
opens = [g for g in groups if label(g) == "open"]
# 隣の先読みが、次に開いた動画だった組
pairs = []
for k, g in enumerate(opens[:-1]):
    nxt = order[k + 1]
    if any(c["aw"] == nxt for c in g["calls"]) and order[k] in byid:
        pairs.append((order[k], nxt))
stall = [v for v in order if (byid[v].get("scrolls") or 0) >= 45 and not byid[v].get("has_more")]
big = [v for v in order if byid[v].get("cap") == 120 and byid[v].get("has_more")]
rest = [v for v in order if byid[v].get("cap") == 40 and byid[v].get("has_more")]
rng = random.Random(20261005)
pick = []
for a, b in rng.sample(pairs, min(3, len(pairs))):
    pick += [a, b]
for pool, n in ((stall, 4), (big, 4)):
    cand = [v for v in pool if v not in pick]
    pick += rng.sample(cand, min(n, len(cand)))
cand = [v for v in rest if v not in pick]
pick += rng.sample(cand, 20 - len(pick))
pick = set(pick)

lines = (A / "derived/pool_p1.tsv").read_text(encoding="utf-8").splitlines()
head, body = lines[0], lines[1:]
vi = head.split("\t").index("video_id")
sel = [ln for ln in body if ln.split("\t")[vi] in pick]
(OUT / "pool.tsv").write_text("\n".join([head, *sel]) + "\n", encoding="utf-8")
meta = json.load(open(A / "analysis.json"))
(OUT / "music_url.txt").write_text(meta["music_urls"][0] + "\n", encoding="utf-8")
print(f"組 {len(pairs)}個（使う3組）/ 粘った {len(stall)}本 / 上限120で続きあり {len(big)}本 / 上限40で続きあり {len(rest)}本")
print(f"選んだ {len(sel)}本 → {OUT/'pool.tsv'}")
for v in [v for v in order if v in pick]:
    r = byid[v]
    print(v, f"cap{r.get('cap')} 取得{r.get('fetched')} 続き{r.get('has_more')} 段{r.get('scrolls')} {r.get('seconds')}秒 返信{r.get('replies_opened')}")
