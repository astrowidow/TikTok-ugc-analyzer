"""
分類の試行用: 既存スクレイパーの CSV から、分類軸の提案とラベル付けに使うサンプルを作る。

  python3 analysis/prep_sample.py "シルエット.csv" output/trial_silhouette

出力:
  videos.jsonl      … 全行（正規化済み。week / hashtags / plays を数値化）
  sample_taxonomy.md … 分類軸の提案に読ませる層化サンプル（上位 + 最初期 + 週ごとの無作為）
  sample_label.jsonl … ラベル付け対象（離陸前は全件 + 離陸後は上位と無作為）
  weekly.tsv        … 週次の本数と再生数

サンプルの選び方は決定論的（seed 固定）。LLM に渡すのは sample_*。
"""
import csv
import datetime
import json
import pathlib
import random
import re
import sys

src = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
random.seed(20260911)


def num(x):
    try:
        return int(float(str(x).replace(",", "")))
    except Exception:
        return None


rows = []
with open(src, encoding="utf-8-sig", newline="") as fh:
    for r in csv.DictReader(fh):
        d = (r.get("Created Date") or "")[:10]
        if not d:
            continue
        y, m, dd = map(int, d.split("-"))
        iso = datetime.date(y, m, dd).isocalendar()
        desc = (r.get("Description") or "").strip()
        rows.append({
            "video_id": (r.get("URL") or "").rstrip("/").rsplit("/", 1)[-1],
            "url": r.get("URL"),
            "date": d,
            "week": f"{iso[0]}-W{iso[1]:02d}",
            "type": r.get("Type"),
            "username": r.get("Username") or "",
            "desc": desc,
            "hashtags": [t.lower() for t in re.findall(r"#([^\s#]+)", desc)],
            "plays": num(r.get("Plays")) or 0,
            "likes": num(r.get("Likes")) or 0,
            "comments": num(r.get("Comments")) or 0,
            "shares": num(r.get("Shares")) or 0,
        })
rows.sort(key=lambda r: (r["date"], -r["plays"]))
for i, r in enumerate(rows):
    r["seq"] = i  # 時系列の通し番号（最初期の判定に使う）

with open(out / "videos.jsonl", "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

# 週次
weeks = {}
for r in rows:
    w = weeks.setdefault(r["week"], {"n": 0, "plays": 0})
    w["n"] += 1
    w["plays"] += r["plays"]
with open(out / "weekly.tsv", "w", encoding="utf-8") as f:
    f.write("week\tn\tplays\n")
    for k in sorted(weeks):
        f.write(f"{k}\t{weeks[k]['n']}\t{weeks[k]['plays']}\n")

# 離陸週: 週の本数が初めて全体の 5% を超えた週
total = len(rows)
takeoff = next((k for k in sorted(weeks) if weeks[k]["n"] >= total * 0.05), sorted(weeks)[-1])

# --- 分類軸の提案用サンプル ---
top = sorted(rows, key=lambda r: -r["plays"])[:100]
earliest = rows[:40]
later = [r for r in rows if r["week"] >= takeoff and r not in top and r not in earliest]
by_week = {}
for r in later:
    by_week.setdefault(r["week"], []).append(r)
rand = []
for k in sorted(by_week):
    rand += random.sample(by_week[k], min(6, len(by_week[k])))
sample_tax = {r["video_id"]: r for r in top + earliest + rand}
sample_tax = sorted(sample_tax.values(), key=lambda r: r["seq"])


def line(r):
    d = r["desc"].replace("\n", " ")
    if len(d) > 110:
        d = d[:110] + "…"
    return (f"| {r['seq']} | {r['date']} | {r['plays']:,} | {r['likes']:,} | {r['comments']:,} | "
            f"{r['type'][0]} | @{r['username'] or '(不明)'} | {d or '(説明文なし)'} |")


with open(out / "sample_taxonomy.md", "w", encoding="utf-8") as f:
    f.write(f"# 分類軸提案用サンプル: {src.name}\n\n")
    f.write(f"全{total}本 / 期間 {rows[0]['date']}〜{rows[-1]['date']} / 離陸週 {takeoff}\n")
    f.write(f"サンプル {len(sample_tax)}本 = 再生上位100 + 最初期40 + 離陸後の各週から無作為6\n\n")
    f.write("| seq | 日付 | 再生 | いいね | コメント | 型 | 投稿者 | 説明文 |\n|---|---|---|---|---|---|---|---|\n")
    for r in sample_tax:
        f.write(line(r) + "\n")

# --- ラベル付け対象 ---
pre = [r for r in rows if r["week"] < takeoff]
post = [r for r in rows if r["week"] >= takeoff]
post_top = sorted(post, key=lambda r: -r["plays"])[:70]
post_rest = [r for r in post if r not in post_top]
post_rand = random.sample(post_rest, min(50, len(post_rest)))
label_set = {r["video_id"]: r for r in pre + post_top + post_rand}
label_set = sorted(label_set.values(), key=lambda r: r["seq"])
with open(out / "sample_label.jsonl", "w", encoding="utf-8") as f:
    for r in label_set:
        f.write(json.dumps({k: r[k] for k in ("seq", "video_id", "date", "week", "type", "username", "desc", "plays", "likes", "comments")}, ensure_ascii=False) + "\n")

print(f"rows={total} takeoff={takeoff} taxonomy_sample={len(sample_tax)} label_set={len(label_set)} "
      f"(pre-takeoff {len(pre)} + post top {len(post_top)} + post random {len(post_rand)})")
