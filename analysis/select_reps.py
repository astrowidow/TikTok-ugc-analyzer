"""
答えなし条件のラベル（clean/labels.tsv）から、界隈ごとに代表動画を選ぶ（E2E テスト用の暫定アルゴリズム）。

  python3 analysis/select_reps.py output/trial_silhouette [--per-community 5]

規則:
  - unknown 界隈は除外。写真投稿・削除済み（属性取得失敗）は除外
  - 各界隈: 最初期1本 + 最大再生1本 + 残りは「まだ含まれていない段階」の最大再生を順に → それでも余れば再生順
  - 段階は clean/pathway.md の区切り（P1 7/10-8/20, P2 8/21-10/19, P3 10/20-11/23, P4 11/24-）
出力: reps.tsv（seq, video_id, community, phase, date, plays, username, url）と reps_urls.txt（spatest --videos 用）
"""
import argparse
import csv
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("dir")
ap.add_argument("--per-community", type=int, default=5)
a = ap.parse_args()
d = pathlib.Path(a.dir)

videos = {json.loads(l)["seq"]: json.loads(l) for l in open(d / "videos.jsonl", encoding="utf-8")}
alive = {json.loads(l)["video_id"] for l in open(d / "enriched.jsonl", encoding="utf-8") if "error" not in json.loads(l)}
labels = {}
with open(d / "clean" / "labels.tsv", encoding="utf-8") as f:
    for r in csv.DictReader(f, delimiter="\t"):
        labels[int(r["seq"])] = r


def phase(date):
    if date <= "2025-08-20":
        return "P1"
    if date <= "2025-10-19":
        return "P2"
    if date <= "2025-11-23":
        return "P3"
    return "P4"


by_comm = {}
for seq, lab in labels.items():
    v = videos[seq]
    if lab["community"] == "unknown" or v["type"] != "Video" or v["video_id"] not in alive or v["plays"] <= 0:
        continue
    by_comm.setdefault(lab["community"], []).append({**v, "community": lab["community"], "phase": phase(v["date"]), "conf": lab["conf"]})

picked = []
for comm, vs in sorted(by_comm.items(), key=lambda kv: -len(kv[1])):
    chosen, seen = [], set()

    def take(v):
        if v["seq"] not in seen:
            seen.add(v["seq"])
            chosen.append(v)

    take(min(vs, key=lambda v: v["seq"]))
    take(max(vs, key=lambda v: v["plays"]))
    covered = {v["phase"] for v in chosen}
    for ph in ("P1", "P2", "P3", "P4"):
        if len(chosen) >= a.per_community:
            break
        if ph in covered:
            continue
        cand = [v for v in vs if v["phase"] == ph and v["seq"] not in seen]
        if cand:
            take(max(cand, key=lambda v: v["plays"]))
    for v in sorted(vs, key=lambda v: -v["plays"]):
        if len(chosen) >= a.per_community:
            break
        take(v)
    picked += sorted(chosen, key=lambda v: v["seq"])

with open(d / "reps.tsv", "w", encoding="utf-8") as f:
    f.write("seq\tvideo_id\tcommunity\tphase\tdate\tplays\tusername\tconf\turl\n")
    for v in picked:
        f.write(f"{v['seq']}\t{v['video_id']}\t{v['community']}\t{v['phase']}\t{v['date']}\t{v['plays']}\t{v['username']}\t{v['conf']}\t{v['url']}\n")
with open(d / "reps_urls.txt", "w", encoding="utf-8") as f:
    for v in picked:
        f.write(v["url"] + "\n")

print(f"界隈 {len(by_comm)} / 代表 {len(picked)}本")
for comm, vs in sorted(by_comm.items(), key=lambda kv: -len(kv[1])):
    ch = [v for v in picked if v["community"] == comm]
    print(f"  {comm:16} 候補{len(vs):3} → {len(ch)}本  " + ", ".join(f"{v['phase']}:{v['plays']//10000}万" for v in ch))
