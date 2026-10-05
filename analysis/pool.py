"""候補プール（コメントを取る動画）を、AI を使わず数字と属性の規則で決める（docs/BLUEPRINT.md 第3章）。

analysis/pool_verify.py（2026-09-30 の検証）を本番の規則に直したもの。シルエット固有の部分を汎用化した:
  - 本人: 楽曲ページの音源の作者名（enriched の music.author。最多の音源 ID のもの）を正規化し、
          投稿者の unique_id か表示名に含む動画
  - 公式企画のタグ: 本人のアカウントが使ったタグのうち「曲名を含むが曲名そのものではない」もの
          （シルエットでは みんなでシルエット / silhouettetogether）。そのタグを使った認証アカウントは公式扱い
「再生数で一括に切らない」: 最初期・本人と公式・週ごと（地域ごと・TikTok のカテゴリラベルごと）に入れる。

予算は時間から: --hours 12 の分数を、1本あたりの見込み時間（--min-per-video、120件の動画は --min-per-key-video）で配る。
既定は両方3分＝240本分（検証版と同じ覆い。シルエットで 222本、E2E の代表65本中53・セル38中34）。
取得の段は試走の実測（2026-09-30: 40件の動画 4.2分、120件 7.2分、返信を各2件開いた場合）から、返信を各1件にして
3.1分・6.1分で配る（acquire/pipeline.py の DEFAULTS。docs/IMPLEMENTATION_LOG.md D20）。
はみ出た分は取得の12時間打ち切りで落ちるが、取得は「必ず入れる動画（priority=1）を先に」取るので、落ちるのは週ごとに配った動画だけ。

  python analysis/pool.py <videos.jsonl> <enriched.jsonl> <出力 pool.tsv> [--hours 12] [--min-per-video 3]

出力 pool.tsv の列: video_id seq date week plays username cap priority reasons（理由は , 区切り。priority 1＝必ず入れる）
"""
import argparse
import collections
import json
import math
import random
import re
import unicodedata

CAP_STANDARD = 40
CAP_KEY = 120
# 120件にする理由のうち、本人（artist）以外のもの。本人だけが理由の動画は、選んだあとで40件に下げる（build の末尾）
KEY_REASONS_BUT_ARTIST = {"origin", "top_hit", "official"}
# 週ごとに配る動画の再生の下限（2026-10-05）。元の量の版3本のレポートが引用・熟読・名指しした動画に、再生1万未満は両曲とも0本だった
# （docs/COMMENT_STRATEGY_HANDOVER.md 第9章、analysis/comment_exp/select_study.py）。必ず入れる動画（起点など）には掛けない
MIN_PLAYS_WEEKLY = 10_000


def norm(s: str) -> str:
    """照合用: NFKC・小文字・英数と仮名漢字だけ（KANA-BOON → kanaboon）"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(ch for ch in s if ch.isalnum())


def load(videos_path, enriched_path):
    videos = [json.loads(l) for l in open(videos_path, encoding="utf-8")]
    enriched = {}
    for l in open(enriched_path, encoding="utf-8"):
        e = json.loads(l)
        enriched[str(e.get("video_id"))] = e
    return videos, enriched


def song_info(enriched: dict) -> dict:
    """最も多く使われている音源（＝対象の曲）の ID・タイトルの揺れ・作者"""
    ids = collections.Counter()
    titles = collections.defaultdict(collections.Counter)
    authors = collections.defaultdict(collections.Counter)
    for e in enriched.values():
        m = e.get("music") or {}
        if m.get("id"):
            ids[m["id"]] += 1
            titles[m["id"]][m.get("title") or ""] += 1
            authors[m["id"]][m.get("author") or ""] += 1
    if not ids:
        return {"id": None, "titles": [], "author": ""}
    mid = ids.most_common(1)[0][0]
    return {"id": mid,
            "titles": [t for t, _ in titles[mid].most_common() if t],
            "author": authors[mid].most_common(1)[0][0]}


def artist_accounts(alive, enriched, author_name) -> set:
    """音源の作者名を投稿者名に含むアカウント（unique_id）"""
    key = norm(author_name)
    if len(key) < 3:
        return set()
    out = set()
    for v in alive:
        a = enriched[v["video_id"]]["author"]
        if key in norm(a.get("unique_id")) or key in norm(a.get("nickname")):
            out.add(a.get("unique_id"))
    return out


def campaign_tags(alive, enriched, artists, titles) -> set:
    """本人が使ったタグのうち、曲名を含むが曲名そのものではないもの"""
    tkeys = {norm(t) for t in titles if len(norm(t)) >= 2}
    tags = set()
    for v in alive:
        e = enriched[v["video_id"]]
        if e["author"].get("unique_id") not in artists:
            continue
        for t in (e.get("challenges") or []) + (e.get("hashtags") or []):
            nt = norm(t)
            if any(k in nt and nt != k for k in tkeys):
                tags.add(nt)
    return tags


def build(videos, enriched, budget_units, seed=7, cost_std=1.0, cost_key=None):
    """プールを決める。返り値 {video_id: {"cap": int, "reasons": [..]}} と説明。
    budget_units と cost_* は同じ単位（本数なら 1、時間なら分）。cost_key は120件の動画1本の重さ（既定は cost_std）"""
    cost_key = cost_std if cost_key is None else cost_key
    alive = [v for v in videos if (enriched.get(v["video_id"]) or {}).get("author")]
    info = song_info(enriched)
    artists = artist_accounts(alive, enriched, info["author"])
    ctags = campaign_tags(alive, enriched, artists, info["titles"])

    pool = {}

    def add(vs, reason, cap=CAP_STANDARD):
        for v in vs:
            p = pool.setdefault(v["video_id"], {"cap": CAP_STANDARD, "reasons": []})
            if reason not in p["reasons"]:
                p["reasons"].append(reason)
            p["cap"] = max(p["cap"], cap)

    def tags_of(v):
        e = enriched[v["video_id"]]
        return {norm(t) for t in (e.get("challenges") or []) + (e.get("hashtags") or [])}

    by_date = sorted(alive, key=lambda v: (v["date"], v["seq"]))
    by_plays = sorted(alive, key=lambda v: -v["plays"])
    ver = [v for v in alive if enriched[v["video_id"]]["author"].get("verified")]

    # 必ず入れる
    add(by_date[:10], "origin", CAP_KEY)                 # 起点（最初期10本）
    add(by_date[10:20], "earliest")
    add(by_plays[:10], "top_hit", CAP_KEY)               # 大型ヒット（再生上位10本）
    add(by_plays[10:20], "top_plays")
    add(sorted(ver, key=lambda v: v["date"])[:10], "verified_early")
    add(sorted(ver, key=lambda v: -v["plays"])[:15], "verified_top")
    add([v for v in alive if enriched[v["video_id"]]["author"].get("unique_id") in artists], "artist", CAP_KEY)
    tagged = [v for v in alive if tags_of(v) & ctags]
    add(tagged, "campaign_tag")
    add([v for v in tagged if enriched[v["video_id"]]["author"].get("verified")], "official", CAP_KEY)

    def units(vids):
        return sum(cost_key if pool[x]["cap"] >= CAP_KEY else cost_std for x in vids)

    # 残りを週ごとに、週の本数の平方根に比例して配る
    weeks = collections.defaultdict(list)
    for v in alive:
        weeks[v["week"]].append(v)
    rem = max(0, budget_units - units(pool)) / cost_std   # 残りを「40件の動画の本数」にする
    wsum = sum(math.sqrt(len(x)) for x in weeks.values()) or 1
    random.seed(seed)
    alloc = {w: max(1, round(rem * math.sqrt(len(x)) / wsum)) for w, x in weeks.items()}
    for w, vs in sorted(weeks.items()):
        cand = [v for v in vs if v["video_id"] not in pool and (v.get("plays") or 0) >= MIN_PLAYS_WEEKLY]
        k = alloc[w]
        chosen = []
        # 週の中: 投稿地域ごと・TikTok のカテゴリラベルごとに最大再生を1本ずつ
        for keyf in (lambda v: enriched[v["video_id"]].get("location_created") or "?",
                     lambda v: (enriched[v["video_id"]].get("diversification_labels") or ["?"])[0]):
            groups = collections.defaultdict(list)
            for v in cand:
                groups[keyf(v)].append(v)
            for _, gv in groups.items():
                best = max(gv, key=lambda v: v["plays"])
                if best not in chosen and len(chosen) < k:
                    chosen.append(best)
        # 残りは上位半分＋無作為半分
        rest = [v for v in cand if v not in chosen]
        top = sorted(rest, key=lambda v: -v["plays"])[:max(0, math.ceil((k - len(chosen)) / 2))]
        chosen += top
        rest = [v for v in rest if v not in top]
        chosen += random.sample(rest, min(len(rest), max(0, k - len(chosen))))
        add(chosen, f"week:{w}")

    units_used = units(pool)
    # 120件の理由が本人（artist）だけの動画は40件にする（2026-10-05。docs/COMMENT_STRATEGY_HANDOVER.md 第9章）。
    # 本人の投稿の41〜120件目はレポートの引用にほとんど使われず、40件にした写しで回し直してもレポートの点は元の版のぶれの内側だった。
    # 起点・大型ヒット・公式も兼ねる動画は120件のまま。本数の配り方は今までどおり120件の重さで数える（浮いた分は本数を増やさず、時間の短縮に回す）
    n_artist_capped = 0
    for p in pool.values():
        if p["cap"] >= CAP_KEY and not set(p["reasons"]) & KEY_REASONS_BUT_ARTIST:
            p["cap"] = CAP_STANDARD
            n_artist_capped += 1

    explain = {"song": info, "artist_accounts": sorted(a for a in artists if a),
               "campaign_tags": sorted(ctags), "alive": len(alive), "videos": len(videos),
               "budget_units": budget_units, "units_used": round(units_used, 1),
               "n_pool": len(pool), "n_cap_key": sum(1 for p in pool.values() if p["cap"] >= CAP_KEY),
               "n_artist_capped": n_artist_capped}
    return pool, explain


def write(pool, videos, path):
    by_id = {v["video_id"]: v for v in videos}
    rows = sorted(pool.items(), key=lambda kv: by_id[kv[0]]["seq"])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("video_id\tseq\tdate\tweek\tplays\tusername\tcap\tpriority\treasons\n")
        for vid, p in rows:
            v = by_id[vid]
            prio = 1 if any(not r.startswith("week:") for r in p["reasons"]) else 2
            f.write(f"{vid}\t{v['seq']}\t{v['date']}\t{v['week']}\t{v['plays']}\t{v.get('username', '')}\t"
                    f"{p['cap']}\t{prio}\t{','.join(p['reasons'])}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos")
    ap.add_argument("enriched")
    ap.add_argument("out")
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--min-per-video", type=float, default=3.0, help="40件の動画1本の見込み（分）")
    ap.add_argument("--min-per-key-video", type=float, default=0.0, help="120件の動画1本の見込み（分）。0なら --min-per-video と同じ")
    ap.add_argument("--budget", type=int, default=0, help="本数で直接指定（時間より優先）")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    videos, enriched = load(a.videos, a.enriched)
    if a.budget:   # 本数で直接
        pool, explain = build(videos, enriched, a.budget, seed=a.seed)
    else:          # 時間（分）で
        pool, explain = build(videos, enriched, a.hours * 60, seed=a.seed, cost_std=a.min_per_video,
                              cost_key=a.min_per_key_video or a.min_per_video)
    write(pool, videos, a.out)
    print(json.dumps(explain, ensure_ascii=False))


if __name__ == "__main__":
    main()
