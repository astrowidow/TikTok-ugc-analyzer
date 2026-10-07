"""
動画の一覧を絞った写しを、取得が済んだ直後（AI の最初の仕事の前）の形で作る（2026-10-06 ユーザー「音源の数で割り算した分しか
取得しないとしても、全然いけちゃうと思う。レポートの質が下がらないことを検証して」）。AI も TikTok も使わない。

  python3 analysis/comment_exp/fork_list.py a20261004-0837-4e69 x-kyw-l100 --keep 1      # 絞らない（基準）
  python3 analysis/comment_exp/fork_list.py a20261004-0837-4e69 x-kyw-l50 --keep 0.5     # 楽曲ページごとにグリッドの上から半分

- 元の分析は本物の置き場から読むだけ。写しは実験の置き場（common.HOME）の analyses/<新しい ID>/
- 一覧（raw/grid_links.jsonl）を楽曲ページごとにグリッドの上から keep の割合だけ残し（list_cut_study.kept）、
  属性（enriched.jsonl）も残した動画だけにして、一覧の CSV・集計と AI の入力・プールを今のコードで作り直す
- コメントは今の本番の取り方にそろえる: 作り直したプールのコメントを取る動画（cap>0）だけ、先頭20件（1ページ。cuts の all20）。
  元の取得で取っていない動画は、コメントなし（取得の失敗と同じ扱い）
- 界隈の確認は省く（skip_confirm）。AI 役が最初の仕事（界隈の軸）から最後まで回す
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

common.setup_env()
import cuts  # noqa: E402
import list_cut_study as lcs  # noqa: E402
from acquire import pipeline  # noqa: E402


def jl(p: Path) -> list:
    return [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()] if p.exists() else []


def wjl(p: Path, rows: list) -> None:
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("new")
    ap.add_argument("--keep", type=float, required=True, help="楽曲ページごとに残す割合（グリッドの上から）")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    src = common.APP_DATA / "analyses" / a.src
    dst = common.analysis_dir(a.new)
    if dst.exists():
        sys.exit(f"{dst} はもうある")
    for sub in ("raw", "fetch_log", "derived", "outputs", "eval", "state"):
        (dst / sub).mkdir(parents=True)

    rank = lcs.grid_rank(src)
    keep = lcs.kept(rank, a.keep)
    wjl(dst / "raw/grid_links.jsonl", [g for g in jl(src / "raw/grid_links.jsonl") if str(g["video_id"]) in keep])
    wjl(dst / "raw/enriched.jsonl", [e for e in jl(src / "raw/enriched.jsonl") if str(e.get("video_id")) in keep])
    for f in ("music_pages.json", "music_page.json"):
        if (src / "raw" / f).exists():
            shutil.copy2(src / "raw" / f, dst / "raw" / f)
    if (src / "fetch_log/before_release.json").exists():
        shutil.copy2(src / "fetch_log/before_release.json", dst / "fetch_log/before_release.json")

    m = json.loads((src / "analysis.json").read_text(encoding="utf-8"))
    title = a.title or f"{m['title']}（一覧{int(round(a.keep * 100))}%）"
    steps = (m.get("acquisition") or {}).get("steps") or {}
    meta = {k: m[k] for k in ("song", "music_url", "music_urls", "layout") if k in m}
    meta.update({"analysis_id": a.new, "title": title, "owner": common.USER, "created_at": pipeline.now(),
                 "forked_from": a.src, "list_keep": a.keep,
                 "acquisition": {"status": "done", "steps": {s: {"status": "done", "detail": (steps.get(s) or {}).get("detail") or {}}
                                                             for s in pipeline.STEPS}}})
    (dst / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    run = pipeline.Run(a.new)
    run._write_links_csv()
    n_rows = run._write_list_csv()
    derive = run.step_derive()
    pool = run.step_pool()
    caps = {r["video_id"]: int(r.get("cap") or 0) for r in pipeline.comment_rows(dst / "derived/pool.tsv")}
    want = {v for v, c in caps.items() if c > 0}
    rows = cuts.apply([r for r in jl(src / "raw/comments.jsonl") if r.get("status") == "ok"], ["all20"], dst, keep=want)
    wjl(dst / "raw/comments.jsonl", rows)
    n_c = sum(len(r.get("comments") or []) for r in rows)

    def fn(mm):
        st = mm["acquisition"]["steps"]
        st["list"]["detail"] = {**st["list"]["detail"], "links": len(keep)}
        st["comments"]["detail"] = {"videos_ok": len(rows), "comments": n_c, "pool": len(want), "substituted": 0,
                                    "not_fetched_time": len(want) - len(rows)}
    run.update(fn)
    md = run.step_comments_md()
    import proto_runner as pr
    pr._set_options(dst, skip_confirm=True, skip_confirm_at=pipeline.now())
    print(json.dumps({"id": a.new, "title": title, "links": len(keep), "list_rows": n_rows, "derive": derive, "pool": pool,
                      "comment_videos": len(rows), "comment_videos_wanted": len(want), "comments": n_c, "comments_md": md},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
