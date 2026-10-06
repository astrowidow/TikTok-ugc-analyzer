"""
界隈の切り直し: 完成したレポートの界隈を利用者の指示で切り直したあと、新しい界隈ごとに「必ず読みたい動画」にコメントがあるかを確かめ、
足りなければ取り足す（2026-10-06 ユーザー）。docs/RECUT_COMMUNITY.md

- plan(): 界隈ごとの必ず読みたい動画と、コメントの無いもの（＝取り足す動画）を決める（AI にも TikTok にも触らない）
- request(): analysis.json の "deepen" の枠に kind="recut" で積む。取得の係（acquire/worker.py）・取る（acquire/deepen.py の Run）・
  原本に合わせる（deepen.merge）は界隈の掘り下げと同じものを使う

必ず読みたい動画（RULE）: 最初の取得（analysis/pool.py）は界隈が決まる前に曲全体で選ぶ（起点・大型ヒット・認証アカウント・本人・公式）。
その「必ず入れる」の考え方を、界隈の中に当てはめた:
  再生上位 … その界隈の再生上位3本
  最初期   … その界隈で最初に下限を超えた投稿1本（その界隈の入口）
  認証     … その界隈の認証アカウントの再生最上位1本・最初期1本
どれも再生の下限（その分析の週ごとの下限 min_plays_weekly、既定10万）以上から選ぶ（ユーザーの価値観「10万以下の動画の話は大局を見たいレポートではむしろ不適切」）。
確かめるのは、切り直しで動画の顔ぶれが変わった界隈だけ（RULE["scope"]="changed"。"all" で全部の界隈）。
時間では削らない（足りるまで取る）。上限は本線のコメントの取り方に合わせる（1本1ページ＝analysis/pool.py の CAP_PAGE、前の取り方なら40件）。

  python -m acquire.recut plan <分析フォルダ>   … 今の界隈（全部）に当てはめると何を取るかだけ出す
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from acquire import deepen, pipeline  # noqa: E402

RULE = {
    "top": 3,             # 界隈の再生上位
    "earliest": 1,        # 界隈の最初期（下限を超えたものの中で）
    "verified_top": 1,    # 界隈の認証アカウントの再生最上位
    "verified_early": 1,  # 界隈の認証アカウントの最初期
    "scope": "changed",   # changed＝切り直しで顔ぶれが変わった界隈だけ確かめる / all＝全部の界隈
}
WHY = {"top": "再生上位", "earliest": "最初期", "verified_top": "認証の再生最上位", "verified_early": "認証の最初期"}


def _pool_module():
    """analysis/pool.py（パッケージではないので場所から読む）。コメントの上限の数字はそこから読む（写さない）"""
    spec = importlib.util.spec_from_file_location("ugc_pool", BASE_DIR / "analysis" / "pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_cost(d: Path) -> dict:
    """1本の上限（件）と見込み（分）。本線のコメントの取り方（acquire/pipeline.py の comment_plan）に合わせる"""
    m = pipeline.read_json(Path(d) / "analysis.json", {}) or {}
    s = {**pipeline.DEFAULTS, **(m.get("acquisition_settings") or {})}
    pool = _pool_module()
    page = s.get("comment_plan", "page") == "page"
    return {"cap": int(getattr(pool, "CAP_PAGE", 20) if page else pool.CAP_STANDARD),
            "min_per_video": float(pipeline.minutes_per_comment_video(s)), "page_min": float(deepen.EST["page_min"])}


# 前の取得で楽曲ページの一覧に見つからなかった動画。掘り下げ（acquire/deepen.py の candidates）と同じ範囲を外すので、そちらに置いた
unreachable = deepen.unreachable


def picks(recs: list, floor: int, rule: dict | None = None) -> dict:
    """界隈1つの必ず読みたい動画 {seq: [理由]}。recs はその界隈の動画（records.jsonl の行）"""
    rule = {**RULE, **(rule or {})}
    alive = [r for r in recs if r.get("enriched") and r.get("comments") != 0 and int(r.get("plays") or 0) >= floor]
    out = {}

    def add(rs, why):
        for r in rs:
            out.setdefault(int(r["seq"]), []).append(why)

    by_plays = sorted(alive, key=lambda r: (-int(r.get("plays") or 0), int(r["seq"])))
    by_date = sorted(alive, key=lambda r: (r.get("date") or "", int(r["seq"])))
    ver = [r for r in alive if (r.get("author") or {}).get("verified")]
    add(by_plays[:rule["top"]], "top")
    add(by_date[:rule["earliest"]], "earliest")
    add([r for r in by_plays if r in ver][:rule["verified_top"]], "verified_top")
    add([r for r in by_date if r in ver][:rule["verified_early"]], "verified_early")
    return out


def plan(d: Path, communities=None, rule: dict | None = None) -> dict:
    """界隈ごとに必ず読みたい動画を決め、コメントの無いもの（取りに行けるもの）を取り足す動画にする。
    communities: 確かめる界隈の key（None なら unknown 以外の全部）"""
    d = Path(d)
    recs = {int(r["seq"]): r for r in deepen._records(d).values()}
    labs = deepen._labels(d)
    ok = deepen._ok_rows(d / "raw" / "comments.jsonl")
    last = deepen._rows(d / "raw" / "comments.jsonl")
    tried = {}
    for p in sorted((d / "raw" / "deepen").glob("r*.jsonl")) if (d / "raw" / "deepen").exists() else []:
        tried.update(deepen._rows(p))
    gone = ({v for v, r in last.items() if r.get("status") in deepen.GONE}
            | {v for v, r in tried.items() if r.get("status") in deepen.GONE} | unreachable(d))
    src = deepen._sources(d)
    floor = deepen.min_plays_of(d)
    cost = fetch_cost(d)
    by_c = {}
    for s, lab in labs.items():
        k = lab.get("community")
        if k and k != "unknown" and s in recs:
            by_c.setdefault(k, []).append(recs[s])
    keys = sorted(by_c) if communities is None else [k for k in communities if k in by_c]
    report, targets = {}, []
    for k in keys:
        pk = picks(by_c[k], floor, rule)
        have, need, lost = [], [], []
        for s in sorted(pk, key=lambda s: -int(recs[s].get("plays") or 0)):
            vid = str(recs[s]["video_id"])
            (have if vid in ok else lost if vid in gone else need).append(s)
        report[k] = {"labeled": len(by_c[k]), "with_comments": sum(1 for r in by_c[k] if str(r["video_id"]) in ok),
                     "picks": {s: pk[s] for s in pk}, "have": have, "need": need, "unreachable": lost}
        for s in need:
            r = recs[s]
            targets.append({"video_id": str(r["video_id"]), "seq": s, "plays": int(r.get("plays") or 0), "date": r.get("date"),
                            "week": r.get("week"), "page": src.get(str(r["video_id"]), 1), "cap": cost["cap"],
                            "kind": "new", "community": k, "why": pk[s]})
    targets.sort(key=lambda x: (-x["plays"], x["seq"]))
    pages = sorted({x["page"] for x in targets})
    est = round(len(pages) * cost["page_min"] + len(targets) * cost["min_per_video"], 1) if targets else 0.0
    return {"communities": report, "checked": keys, "targets": targets, "n_new": len(targets), "n_more": 0,
            "pages": pages, "est_min": est, "min_plays": floor, "cap": cost["cap"]}


def request(d: Path, instruction: str, pl: dict, recut_round: int) -> dict:
    """analysis.json の "deepen" の枠に、切り直しの取り足しを積む（前の回は deepen_history へ。回の数え方は掘り下げと共通）"""
    d = Path(d)
    p = d / "analysis.json"
    m = pipeline.read_json(p, {}) or {}
    hist = m.setdefault("deepen_history", [])
    if m.get("deepen"):
        hist.append(m["deepen"])
    n = len(hist) + 1
    now = pipeline.now()
    job = {"round": n, "kind": "recut", "recut_round": recut_round, "community": "recut", "instruction": instruction,
           "requested_at": now, "plan": {k: v for k, v in pl.items() if k not in ("targets", "communities")},
           "targets": pl["targets"], "est_min": pl["est_min"], "status": "queued", "queued_at": now}
    m["deepen"] = job
    pipeline.write_json(p, m)
    return job


# ---------------------------------------------------------------------------
def _main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="界隈の切り直し: 今の界隈に当てはめると何を取り足すかを出す（TikTok に触らない）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("dir")
    a = ap.parse_args(argv)
    pl = plan(Path(a.dir))
    for k, c in pl["communities"].items():
        recs = {int(r["seq"]): r for r in deepen._records(Path(a.dir)).values()}
        need = "、".join(f"seq {s}（{recs[s]['plays'] // 10000}万・{'/'.join(WHY[w] for w in c['picks'][s])}）" for s in c["need"])
        print(f"{k:22s} ラベル{c['labeled']:4d} コメントあり{c['with_comments']:3d} 必ず{len(c['picks'])} "
              f"足りない{len(c['need'])} 届かない{len(c['unreachable'])} {need}")
    print(json.dumps({k: v for k, v in pl.items() if k not in ("communities", "targets", "checked")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
