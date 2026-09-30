"""
答えを見ていない分類（clean/labels.tsv）を、記事の要点と、答えを見た分類（_contaminated/labels.tsv）の両方と突き合わせる。

  python3 analysis/compare_clean.py output/trial_silhouette
"""
import collections
import csv
import json
import pathlib
import sys

d = pathlib.Path(sys.argv[1])
videos = {json.loads(l)["seq"]: json.loads(l) for l in open(d / "videos.jsonl", encoding="utf-8")}


def load(p):
    with open(p, encoding="utf-8") as f:
        return {int(r["seq"]): r for r in csv.DictReader(f, delimiter="\t")}


clean = load(d / "clean" / "labels.tsv")
cont = load(d / "_contaminated" / "labels.tsv")
print(f"clean {len(clean)}本 / contaminated {len(cont)}本 / 共通 {len(set(clean) & set(cont))}本")
print("clean conf:", dict(collections.Counter(r["conf"] for r in clean.values())))
print("clean unknown(community):", sum(1 for r in clean.values() if r["community"] == "unknown"))
print("cont  unknown(community):", sum(1 for r in cont.values() if r["community"] == "unknown"))

print("\n=== clean: 界隈ごとの初出 / 最大（動線の骨格が出ているか）===")
comms = collections.Counter(r["community"] for r in clean.values())
for c, n in comms.most_common():
    rs = sorted([videos[s] for s, r in clean.items() if r["community"] == c], key=lambda v: v["seq"])
    top = max(rs, key=lambda v: v["plays"])
    print(f"[{c}] {n}本 | 初出 {rs[0]['date']} @{rs[0]['username']} {rs[0]['plays']:,} | 最大 {top['date']} @{top['username']} {top['plays']:,}")

print("\n=== 記事の要点に対応する動画の clean ラベル ===")
key_users = {
    "jacksonnsmith": "ミャンマー発明者（7/10）", "swt_naiilu": "次の海外UGC", "kyokostar000": "国内コスプレ第1段階（8/21）",
    "mimijun14": "起点『この手の動き真似できる？』3本", "_cow.boys": "ダンススタジオ（10/17）", "ryuji_nishihiro": "男子高校生インフルエンサー",
    "minami.0819": "300万フォロワー女性インフルエンサー", "jo1_gotothetop": "男性アイドル公式", "kanaboon_official": "楽曲公式",
}
for s, r in sorted(clean.items()):
    u = videos[s]["username"]
    if u in key_users:
        print(f"  seq {s:3} @{u:18} {videos[s]['date']} {videos[s]['plays']:>11,} → {r['community']:22} {r['format']:20} {r['motive']:22} {r['region']:8} {r['conf']}  | {key_users[u]}")
        print(f"        reason: {r['reason'][:90]}")

# 一致率（カテゴリ名が違うので、clean の各カテゴリを contaminated 側の多数派に写像してから測る）
print("\n=== clean × contaminated の突き合わせ ===")
for axis in ("community", "format", "motive", "region"):
    pairs = [(clean[s][axis], cont[s][axis]) for s in clean if s in cont]
    cross = collections.defaultdict(collections.Counter)
    for a, b in pairs:
        cross[a][b] += 1
    mapping = {a: c.most_common(1)[0][0] for a, c in cross.items()}
    agree = sum(1 for a, b in pairs if mapping[a] == b)
    print(f"[{axis}] 写像後の一致 {agree}/{len(pairs)} = {agree/len(pairs)*100:.0f}%")
    for a, c in sorted(cross.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"    clean:{a:26} → " + ", ".join(f"{b}:{n}" for b, n in c.most_common(4)))
