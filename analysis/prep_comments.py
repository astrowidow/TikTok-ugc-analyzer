"""
spatest.py の出力（コメント + 返信の jsonl）を、LLM が読みやすい動画ごとの Markdown に変換する。

  # E2E の形（今までどおり）
  python3 analysis/prep_comments.py output/trial_silhouette output/trial_silhouette/e2e/e2e.jsonl
  # 分析フォルダの形（acquire/pipeline.py が使う）
  python3 analysis/prep_comments.py <分析>/derived <分析>/raw/comments.jsonl \\
      --records <分析>/derived/llm_input/records.jsonl --out <分析>/derived/comments --pool <分析>/derived/pool.tsv

出力: <out>/<seq>_<video_id>.md と INDEX.md
各動画: 属性（投稿者・説明文・提案語）+ コメント（いいね順、cid 付き）+ 返信（親の下にぶら下げ）
返信は、返信欄を開いて取ったもの（reply_comments）に加えて、本体のコメントに同梱されて届いたもの（reply_comment）も拾う。
E2E の形では見出しに代表表（reps.tsv）の界隈・段階を、分析フォルダの形ではプールに入れた理由を書く。
"""
import argparse
import collections
import csv
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("dir"); ap.add_argument("src")
ap.add_argument("--cap", type=int, default=0, help="取得順の先頭N件だけ使う（cap N の走行を再現）")
ap.add_argument("--no-replies", action="store_true")
ap.add_argument("--suffix", default="")
ap.add_argument("--records", default=None, help="records.jsonl（既定は dir/llm_input/records.jsonl）")
ap.add_argument("--out", default=None, help="出力先（既定は dir/e2e/comments<suffix>）")
ap.add_argument("--pool", default=None, help="pool.tsv（分析フォルダの形。見出しにプールの理由を書く）")
args = ap.parse_args()
d = pathlib.Path(args.dir)
src = pathlib.Path(args.src)
out = pathlib.Path(args.out) if args.out else d / "e2e" / ("comments" + args.suffix)
out.mkdir(parents=True, exist_ok=True)

records_path = pathlib.Path(args.records) if args.records else d / "llm_input" / "records.jsonl"
records = {json.loads(l)["video_id"]: json.loads(l) for l in open(records_path, encoding="utf-8")}
reps = {}
if (d / "reps.tsv").exists() and not args.pool:
    with open(d / "reps.tsv", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            reps[r["video_id"]] = r
pool = {}
if args.pool:
    with open(args.pool, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            pool[r["video_id"]] = r

rows = []
for l in open(src, encoding="utf-8"):
    r = json.loads(l)
    if r.get("status") in ("ok", "no_comments"):
        rows.append(r)
# 同じ動画を2回取っていたら後のものを使う（再開・取り直し）
rows = list({str(r.get("video_id")): r for r in rows}.values())

index = collections.defaultdict(list)
n_embedded = 0
for row in rows:
    vid = str(row.get("video_id"))
    rec = records.get(vid, {})
    rep = reps.get(vid, {})
    comments = row.get("comments") or []
    if args.cap:
        comments = comments[: args.cap]          # 取得順（上位リスト順）の先頭N件
    replies = [] if args.no_replies else list(row.get("reply_comments") or [])
    if not args.no_replies:
        # 本体のコメントに同梱されて届いた返信（無料で取れている分）
        have = {str(rp.get("cid")) for rp in replies}
        for c in comments:
            for rp in c.get("reply_comment") or []:
                if str(rp.get("cid")) not in have and str(rp.get("aweme_id", vid)) == vid:
                    replies.append(rp)
                    have.add(str(rp.get("cid")))
                    n_embedded += 1
    if args.cap:
        keep = {str(c.get("cid")) for c in comments}
        replies = [rp for rp in replies if str(rp.get("reply_id")) in keep]
    by_parent = collections.defaultdict(list)
    for rp in replies:
        by_parent[str(rp.get("reply_id"))].append(rp)
    bad = sum(1 for c in comments if str(c.get("aweme_id")) != vid)
    seq = rec.get("seq", rep.get("seq", "?"))
    a = rec.get("author") or {}
    if args.pool:
        head = f"# seq {seq} | {vid} | {rec.get('date')}"
        why = (pool.get(vid) or {}).get("reasons") or "（プール外。差し替えで取った動画）"
        extra = f"- プールに入れた理由: {why}"
    else:
        head = f"# seq {seq} | {vid} | {rec.get('date')} | 界隈={rep.get('community')} 段階={rep.get('phase')}"
        extra = None
    plays = rec.get("plays")
    likes = rec.get("likes")
    lines = [
        head,
        f"- 投稿者: @{a.get('id')}（{a.get('nickname')}）fol={a.get('followers')} verified={a.get('verified')} 地域={rec.get('location_created')}",
        f"- bio: {a.get('bio')}" if a.get("bio") else "",
        f"- 再生 {plays:,} / いいね {likes:,} / コメント総数 {row.get('total')} / 取得 {len(comments)}件（上位リスト {row.get('top_list')}）/ 返信 {len(replies)}件 / 混入 {bad}件 / status={row.get('status')}"
        if isinstance(plays, int) and isinstance(likes, int) else
        f"- 再生 {plays} / いいね {likes} / コメント総数 {row.get('total')} / 取得 {len(comments)}件（上位リスト {row.get('top_list')}）/ 返信 {len(replies)}件 / 混入 {bad}件 / status={row.get('status')}",
        extra,
        f"- 説明文: {rec.get('desc')}",
        f"- TikTok提案語: {rec.get('suggested_words')}" if rec.get("suggested_words") else "",
        f"- TikTokラベル: {rec.get('tiktok_labels')}",
        "",
        "## コメント（いいね順。cid は引用用の ID。★=投稿者がいいね ／ 📌=投稿者が固定）",
        "",
    ]
    for c in sorted(comments, key=lambda c: -(c.get("digg_count") or 0)):
        marks = ("★" if c.get("is_author_digged") else "") + ("📌" if c.get("author_pin") else "")
        u = (c.get("user") or {}).get("nickname") or ""
        text = (c.get("text") or "").replace("\n", " ")
        lines.append(f"- [{c.get('cid')}] 👍{c.get('digg_count', 0)} 返信{c.get('reply_comment_total', 0)} ({c.get('comment_language')}) {marks} @{u}: {text}")
        for rp in sorted(by_parent.get(str(c.get("cid")), []), key=lambda r: -(r.get("digg_count") or 0)):
            ru = (rp.get("user") or {}).get("nickname") or ""
            rt = (rp.get("text") or "").replace("\n", " ")
            rm = "★" if rp.get("is_author_digged") else ""
            lines.append(f"    - ↳ [{rp.get('cid')}] 👍{rp.get('digg_count', 0)} {rm} @{ru}: {rt}")
    path = out / f"{seq}_{vid}.md"
    path.write_text("\n".join(l for l in lines if l is not None) + "\n", encoding="utf-8")
    group = rep.get("community", "?") if not args.pool else ("必ず入れる" if (pool.get(vid) or {}).get("priority") == "1"
                                                              else "週ごと" if vid in pool else "差し替え")
    index[group].append((seq, vid, rec.get("date"), plays, len(comments), len(replies), bad, path.name))

with open(out / "INDEX.md", "w", encoding="utf-8") as f:
    f.write(f"# コメント取得結果の索引（{len(rows)}本）\n\n")
    for comm, items in sorted(index.items(), key=lambda kv: -len(kv[1])):
        f.write(f"## {comm}（{len(items)}本）\n\n| seq | video_id | 日付 | 再生 | コメント | 返信 | 混入 | ファイル |\n|---|---|---|---|---|---|---|---|\n")
        for it in sorted(items, key=lambda x: str(x[0])):
            pl = f"{it[3]:,}" if isinstance(it[3], int) else str(it[3])
            f.write(f"| {it[0]} | {it[1]} | {it[2]} | {pl} | {it[4]} | {it[5]} | {it[6]} | {it[7]} |\n")
        f.write("\n")
tot_c = sum(len(r.get("comments") or []) for r in rows)
tot_r = sum(len(r.get("reply_comments") or []) for r in rows)
print(f"videos={len(rows)} comments={tot_c} replies={tot_r}+同梱{n_embedded} files={out}")
