"""
属性付きデータ（enriched.jsonl）から、LLM に渡す入力を作る。

  python3 analysis/build_llm_input.py output/trial_silhouette
  python3 analysis/build_llm_input.py <分析>/derived --enriched <分析>/raw/enriched.jsonl   # 分析フォルダの形

出力（output/trial_silhouette/llm_input/）:
  records.jsonl        … 全動画の圧縮レコード（分類に効く項目だけ）
  taxonomy_sample.md   … 分類軸の提案用（prep_sample.py と同じ層化: 上位100 + 最初期40 + 週ごと無作為）
  label_set.md         … ラベル付け対象 200本（prep_sample.py の sample_label.jsonl と同じ video_id）
  sheets/sheet_NN.jpg  … ラベル付け対象と提案用サンプルのサムネイル一覧（seq 番号付き、20枚/枚）
"""
import argparse
import json
import pathlib

from PIL import Image, ImageDraw, ImageFont

ap = argparse.ArgumentParser()
ap.add_argument("dir", help="videos.jsonl・sample_taxonomy.md・sample_label.jsonl のあるフォルダ")
ap.add_argument("--enriched", default=None, help="enriched.jsonl（既定は dir/enriched.jsonl）")
ap.add_argument("--out", default=None, help="出力先（既定は dir/llm_input）")
args = ap.parse_args()
d = pathlib.Path(args.dir)
out = pathlib.Path(args.out) if args.out else d / "llm_input"
(out / "sheets").mkdir(parents=True, exist_ok=True)

videos = {json.loads(l)["video_id"]: json.loads(l) for l in open(d / "videos.jsonl", encoding="utf-8")}
enriched = {}
for l in open(pathlib.Path(args.enriched) if args.enriched else d / "enriched.jsonl", encoding="utf-8"):
    r = json.loads(l)
    enriched[r["video_id"]] = r

SONG_MUSIC_ID = None
ids = [e["music"]["id"] for e in enriched.values() if e.get("music") and e["music"].get("id")]
if ids:
    SONG_MUSIC_ID = max(set(ids), key=ids.count)

# 音源（楽曲ページ）が2つ以上の分析: 動画ごとにどの音源か（A・B・C…。ページの順）。動画の音源の id で当て、読めなければ見つけたページで。
# 同じ曲でも、切り抜いた部分（音源）ごとに使われ方が違うことがある（2026-10-06 きゃわぽっぴんどぅー: ファンが上げた12秒の音源で、
# 2番の「血液型とかMBTIとか」を使う自己紹介の波）。音源ごとの要約は llm_input/sounds.json（flow_w1.sounds）
RAW = pathlib.Path(args.enriched).parent if args.enriched else d.parent / "raw"


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


PAGES = [p for p in (_read_json(RAW / "music_pages.json", []) or []) if isinstance(p, dict) and p.get("url")]
SOUNDS, SOUND_OF_ID, SOUND_OF_PAGE, SOURCE = [], {}, {}, {}
if len(PAGES) >= 2:
    if (RAW / "grid_links.jsonl").exists():
        for line in open(RAW / "grid_links.jsonl", encoding="utf-8"):
            try:
                g = json.loads(line)
            except ValueError:
                continue
            SOURCE[str(g.get("video_id"))] = int(g.get("source") or 1)
    for k, p in enumerate(PAGES, 1):
        tag = chr(ord("A") + k - 1) if k <= 26 else str(k)
        mid = p["url"].rstrip("/").split("?")[0].rsplit("-", 1)[-1]
        durs = [e["music"]["duration"] for e in enriched.values()
                if str((e.get("music") or {}).get("id")) == mid and (e.get("music") or {}).get("duration")]
        SOUND_OF_ID[mid], SOUND_OF_PAGE[k] = tag, tag
        SOUNDS.append({"tag": tag, "page": k, "url": p["url"], "title": p.get("title"), "creator": p.get("creator"), "kind": p.get("kind") or "",
                       "duration": p.get("duration") or (max(set(durs), key=durs.count) if durs else None),
                       "video_count": p.get("video_count"), "video_count_text": p.get("video_count_text")})


def sound_of(v, e):
    if not SOUNDS:
        return None
    return SOUND_OF_ID.get(str((e.get("music") or {}).get("id"))) or SOUND_OF_PAGE.get(SOURCE.get(str(v["video_id"]), 1))


def compact(v):
    e = enriched.get(v["video_id"]) or {}
    a = e.get("author") or {}
    sug = []
    for w in (e.get("suggested_words") or []) + (e.get("video_suggest_words") or []):
        if w and w not in sug:
            sug.append(w)
    rec = {
        "seq": v["seq"],
        "video_id": v["video_id"],
        "date": v["date"],
        "week": v["week"],
        "type": v["type"],
        "plays": v["plays"], "likes": v["likes"], "comments": v["comments"], "shares": v["shares"],
        "duration_s": (e.get("video") or {}).get("duration"),
        "author": {
            "id": a.get("unique_id") or v["username"],
            "nickname": a.get("nickname"),
            "bio": (a.get("signature") or "")[:160],
            "verified": a.get("verified"),
            "followers": a.get("follower_count"),
            "videos": a.get("video_count"),
        },
        "location_created": e.get("location_created"),
        "text_language": e.get("text_language"),
        "tiktok_labels": e.get("diversification_labels"),
        "desc": (e.get("desc") if e.get("desc") is not None else v["desc"])[:300],
        "hashtags": e.get("challenges") or v["hashtags"],
        "mentions": e.get("mentions") or [],
        "sticker_texts": e.get("sticker_texts") or [],
        "effect_stickers": e.get("effect_stickers") or [],
        "suggested_words": sug[:10],
        "uses_original_sound": ((e.get("music") or {}).get("id") == SONG_MUSIC_ID) if e.get("music") else None,
        "sound": sound_of(v, e),
        "is_ad": e.get("is_ad"),
        "enriched": bool(e) and "error" not in e,
        "cover_file": e.get("cover_file"),
    }
    return rec


records = [compact(v) for v in sorted(videos.values(), key=lambda v: v["seq"])]
with open(out / "records.jsonl", "w", encoding="utf-8") as f:
    for r in records:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
if SOUNDS:
    (out / "sounds.json").write_text(json.dumps(SOUNDS, ensure_ascii=False, indent=1), encoding="utf-8")
elif (out / "sounds.json").exists():
    (out / "sounds.json").unlink()
by_seq = {r["seq"]: r for r in records}

# 同じ層化サンプルを使う（prep_sample.py の出力から seq を拾う）
tax_seqs = []
for line in open(d / "sample_taxonomy.md", encoding="utf-8"):
    if line.startswith("| ") and line[2].isdigit():
        tax_seqs.append(int(line.split("|")[1].strip()))
label_seqs = [json.loads(l)["seq"] for l in open(d / "sample_label.jsonl", encoding="utf-8")]


def fmt(r):
    a = r["author"]
    parts = [
        f"### seq {r['seq']} | {r['date']} | {r['type']} | 再生 {r['plays']:,} / いいね {r['likes']:,} / コメント {r['comments']:,} / 共有 {r['shares']:,} | 尺 {r['duration_s']}s",
        f"- 投稿者: @{a['id']}（{a['nickname'] or '-'}）fol={a['followers']:,} videos={a['videos']} verified={a['verified']}" if isinstance(a.get("followers"), int) else f"- 投稿者: @{a['id']}（属性取得なし）",
        f"- bio: {a['bio']}" if a.get("bio") else None,
        f"- 地域={r['location_created']} 言語={r['text_language']} TikTokラベル={r['tiktok_labels']} "
        + (f"音源={r['sound']}" if r.get("sound") else f"元音源={r['uses_original_sound']}") + f" 広告={r['is_ad']}",
        f"- 説明文: {r['desc'] or '(なし)'}",
        f"- タグ: {' '.join('#'+t for t in r['hashtags'])}" if r["hashtags"] else None,
        f"- メンション: {' '.join('@'+m for m in r['mentions'])}" if r["mentions"] else None,
        f"- 画面上の文字: {r['sticker_texts']}" if r["sticker_texts"] else None,
        f"- エフェクト: {r['effect_stickers']}" if r["effect_stickers"] else None,
        f"- TikTok提案語（検索・コメント由来）: {r['suggested_words']}" if r["suggested_words"] else None,
        f"- サムネイル: sheets 内の seq {r['seq']}" if r.get("cover_file") else "- サムネイル: なし",
    ]
    return "\n".join(p for p in parts if p)


def write_md(path, title, seqs):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{len(seqs)}本。seq は時系列の通し番号（全{len(records)}本中）。\n\n")
        for s in seqs:
            f.write(fmt(by_seq[s]) + "\n\n")


write_md(out / "taxonomy_sample.md", "分類軸の提案用サンプル", tax_seqs)
write_md(out / "label_set.md", "ラベル付け対象", label_seqs)

# コンタクトシート
all_seqs = sorted(set(tax_seqs) | set(label_seqs))
with_cover = [s for s in all_seqs if by_seq[s].get("cover_file") and pathlib.Path(by_seq[s]["cover_file"]).exists()]
COLS, ROWS, W, H = 5, 4, 216, 384
font = None
for fp in ("/System/Library/Fonts/Helvetica.ttc", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
    try:
        font = ImageFont.truetype(fp, 28)
        break
    except Exception:
        continue
if font is None:
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
sheet_index = {}
for n in range(0, len(with_cover), COLS * ROWS):
    chunk = with_cover[n:n + COLS * ROWS]
    sheet = Image.new("RGB", (COLS * W, ROWS * (H + 36)), "white")
    for i, s in enumerate(chunk):
        try:
            im = Image.open(by_seq[s]["cover_file"]).convert("RGB")
            im.thumbnail((W, H))
        except Exception:
            continue
        x = (i % COLS) * W
        y = (i // COLS) * (H + 36)
        sheet.paste(im, (x + (W - im.width) // 2, y + 36))
        dr = ImageDraw.Draw(sheet)
        dr.rectangle([x, y, x + W, y + 34], fill="black")
        dr.text((x + 6, y + 2), f"seq {s}", fill="white", font=font)
        sheet_index[s] = f"sheet_{n // (COLS * ROWS):02d}.jpg"
    sheet.save(out / "sheets" / f"sheet_{n // (COLS * ROWS):02d}.jpg", quality=80)
with open(out / "sheets" / "index.json", "w", encoding="utf-8") as f:
    json.dump(sheet_index, f, ensure_ascii=False, indent=0)

enr = sum(1 for r in records if r["enriched"])
print(f"records={len(records)} enriched={enr} taxonomy_sample={len(tax_seqs)} label_set={len(label_seqs)} "
      f"covers={len(with_cover)} sheets={len(set(sheet_index.values()))}")
