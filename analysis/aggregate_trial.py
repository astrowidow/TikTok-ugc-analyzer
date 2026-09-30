"""
ラベル付け結果を集計し、記事（答え合わせ）と突き合わせる。

  python3 analysis/aggregate_trial.py output/trial_silhouette
"""
import collections
import csv
import json
import pathlib
import sys

d = pathlib.Path(sys.argv[1])
videos = {json.loads(l)["seq"]: json.loads(l) for l in open(d / "videos.jsonl", encoding="utf-8")}
labels = {}
with open(d / "labels.tsv", encoding="utf-8") as f:
    for r in csv.DictReader(f, delimiter="\t"):
        labels[int(r["seq"])] = r

rows = []
for seq, lab in labels.items():
    v = videos[seq]
    rows.append({**v, **{k: lab[k] for k in ("community", "format", "motive", "region", "tier", "conf", "reason")}})
with open(d / "labels.jsonl", "w", encoding="utf-8") as f:
    for r in sorted(rows, key=lambda r: r["seq"]):
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

takeoff = "2025-W43"
pre = [r for r in rows if r["week"] < takeoff]
post = [r for r in rows if r["week"] >= takeoff]


def dist(rs, key):
    c = collections.Counter(r[key] for r in rs)
    p = collections.Counter()
    for r in rs:
        p[r[key]] += r["plays"]
    return c, p


print(f"ラベル付け {len(rows)}本（離陸前 {len(pre)} / 離陸後 {len(post)}）")
print(f"confidence: {dict(collections.Counter(r['conf'] for r in rows))}")
unk = sum(1 for r in rows if r["community"] == "unknown")
print(f"unknown: {unk}本 = {unk/len(rows)*100:.0f}%（説明文なし・写真）\n")

for name, rs in (("離陸前（7/10〜10/19、全件）", pre), ("離陸後（10/20〜、上位70+無作為50）", post)):
    print(f"=== {name} ===")
    for key in ("community", "region", "format", "motive"):
        c, p = dist(rs, key)
        print(f"  [{key}]")
        for k, n in c.most_common():
            print(f"    {k:24} {n:3}本  再生 {p[k]:>13,}")
    print()

# 週 × 界隈（本数）
print("=== 週 × 界隈（本数。離陸後は上位+無作為の抜粋なので比率のみ意味がある）===")
weeks = sorted({r["week"] for r in rows})
comms = [k for k, _ in collections.Counter(r["community"] for r in rows).most_common()]
print("week      " + " ".join(f"{c[:8]:>8}" for c in comms))
for w in weeks:
    rs = [r for r in rows if r["week"] == w]
    cnt = collections.Counter(r["community"] for r in rs)
    print(f"{w}  " + " ".join(f"{cnt.get(c, 0):>8}" for c in comms))

# 各界隈の最初期・最大
print("\n=== 界隈ごとの 最初の投稿 / 最大再生（＝代表候補の自動抽出）===")
for c in comms:
    rs = sorted([r for r in rows if r["community"] == c], key=lambda r: r["seq"])
    if not rs:
        continue
    first = rs[0]
    top = max(rs, key=lambda r: r["plays"])
    print(f"[{c}] 初出 {first['date']} @{first['username']} {first['plays']:,} / 最大 {top['date']} @{top['username']} {top['plays']:,}")

# 答え合わせ: #みんなでシルエット（全880本から）
allv = list(videos.values())
tag = [v for v in allv if "みんなでシルエット" in (v["desc"] or "")]
tag100k = [v for v in tag if v["plays"] >= 100_000]
print(f"\n=== 答え合わせ: #みんなでシルエット ===")
print(f"全件 {len(tag)}本 / ≥10万再生 {len(tag100k)}本（記事: 8件、うち公式4件）")
for v in sorted(tag100k, key=lambda v: v["date"]):
    print(f"  {v['date']} @{v['username']:24} {v['plays']:>12,}  {v['desc'][:60]}")
