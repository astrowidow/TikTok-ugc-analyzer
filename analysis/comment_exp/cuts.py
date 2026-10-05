"""
取得済みのコメント（raw/comments.jsonl の行）を削って、「取る量を減らした取得」を再現する。
docs/COMMENT_STRATEGY_HANDOVER.md 第5章 段2 の削り方。コメントの並びは取得順（上位リスト順）なので、先頭から切ればよい。

  no_replies   返信を開かない: 開いて取った返信（reply_comments）を空に。本体に同梱されて届いた返信（reply_comment）は残す
  artist40     上限120件の対象を絞る: 120件の理由が本人（artist）だけの動画を、先頭40件に
               （起点・大型ヒット・公式の理由も持つ動画は120件のまま）
  std20        標準40件 → 20件: 上限40件の動画を先頭20件に。実際の取得は1ページ17〜20件で、20件に届かなければ2ページ目も取るので、
               ここでの20件は実際より少なめ（点数には厳しい側の近似）
  top5         【物差しの確かめ用。取得の案ではない】どの動画もコメントを先頭5件だけ・返信なし。これでも同点なら物差しが鈍い
  half_weekly  プールの「残りを週ごとに配る」分を半分に: 週ごとに、前に選んだ動画の中から pool.build と同じ選び方で半分を選び直す
               （必ず入れる動画は残す。前に選んでいない動画は取っていないので、選び直しの候補は前に選んだ動画だけ。近似）

どの削り方でも、残したコメントにぶら下がらない返信（reply_comments）は捨て、行の fetched・replies を数え直す。
"""
import collections
import copy
import csv
import json
import math
import random
from pathlib import Path

CUTS = ["no_replies", "artist40", "std20", "half_weekly", "top5"]
KEY_REASONS = {"origin", "top_hit", "official"}   # artist 以外で120件になる理由（analysis/pool.py の CAP_KEY を付ける add）


def reasons(row) -> list:
    return [x for x in str(row.get("pool_reason") or "").split(",") if x]


def _trim(row, n: int) -> None:
    cs = row.get("comments") or []
    if len(cs) > n:
        row["comments"] = cs[:n]


def _recount(row) -> None:
    keep = {str(c.get("cid")) for c in row.get("comments") or []}
    row["reply_comments"] = [rp for rp in row.get("reply_comments") or [] if str(rp.get("reply_id")) in keep]
    if row.get("status") == "ok":
        row["fetched"] = len(row.get("comments") or [])
    row["replies"] = len(row["reply_comments"])


def _pool_rows(adir: Path) -> dict:
    p = adir / "derived" / "pool.tsv"
    with open(p, encoding="utf-8") as f:
        return {r["video_id"]: r for r in csv.DictReader(f, delimiter="\t")}


def _enriched(adir: Path) -> dict:
    out = {}
    for line in open(adir / "raw" / "enriched.jsonl", encoding="utf-8"):
        e = json.loads(line)
        out[str(e.get("video_id"))] = e
    return out


def half_weekly_keep(adir: Path, seed: int = 7) -> tuple:
    """(残す video_id, 週ごとに配った video_id 全部)。pool.build の週の中の選び方（地域・カテゴリごとの最大再生 → 上位半分 → 無作為）で半分を選ぶ"""
    pool = _pool_rows(adir)
    enr = _enriched(adir)
    weeks = collections.defaultdict(list)
    for vid, p in pool.items():
        rs = [x for x in p["reasons"].split(",") if x]
        if rs and all(x.startswith("week:") for x in rs):
            weeks[rs[0][5:]].append({"video_id": vid, "plays": int(p.get("plays") or 0)})
    keep = set()
    random.seed(seed)
    for w, cand in sorted(weeks.items()):
        k = max(1, round(len(cand) / 2))
        chosen = []
        for keyf in (lambda v: (enr.get(v["video_id"]) or {}).get("location_created") or "?",
                     lambda v: ((enr.get(v["video_id"]) or {}).get("diversification_labels") or ["?"])[0]):
            groups = collections.defaultdict(list)
            for v in cand:
                groups[keyf(v)].append(v)
            for _, gv in groups.items():
                best = max(gv, key=lambda v: v["plays"])
                if best not in chosen and len(chosen) < k:
                    chosen.append(best)
        rest = [v for v in cand if v not in chosen]
        top = sorted(rest, key=lambda v: -v["plays"])[:max(0, math.ceil((k - len(chosen)) / 2))]
        chosen += top
        rest = [v for v in rest if v not in top]
        chosen += random.sample(rest, min(len(rest), max(0, k - len(chosen))))
        keep |= {v["video_id"] for v in chosen}
    weekly_all = {v["video_id"] for vs in weeks.values() for v in vs}
    return keep, weekly_all


def apply(rows: list, cuts: list, adir: Path, keep: set | None = None) -> list:
    """削った行の写しを返す（元の rows は変えない）。keep があれば、その動画の行だけ残す（取る動画を絞った場合の再現）"""
    bad = [c for c in cuts if c not in CUTS]
    if bad:
        raise ValueError(f"知らない削り方: {bad}（{CUTS}）")
    out = copy.deepcopy(rows)
    if keep is not None:
        out = [r for r in out if str(r.get("video_id")) in keep]
    if "half_weekly" in cuts:
        keep, weekly = half_weekly_keep(adir)
        out = [r for r in out if str(r.get("video_id")) not in weekly or str(r.get("video_id")) in keep]
    for r in out:
        if "no_replies" in cuts:
            r["reply_comments"] = []
            r["replies_opened"] = 0
        if "artist40" in cuts and r.get("cap") == 120:
            rs = set(reasons(r))
            if "artist" in rs and not (rs & KEY_REASONS):
                _trim(r, 40)
        if "std20" in cuts and r.get("cap") != 120:
            _trim(r, 20)
        if "top5" in cuts:
            r["reply_comments"] = []
            _trim(r, 5)
        _recount(r)
    return out


def stats(rows: list) -> dict:
    ok = [r for r in rows if r.get("status") == "ok" and r.get("comments")]
    emb = sum(1 for r in ok for c in r["comments"] for rp in (c.get("reply_comment") or [])
              if str(rp.get("aweme_id", r["video_id"])) == str(r["video_id"]))
    return {"videos_ok": len(ok), "comments": sum(len(r["comments"]) for r in ok),
            "replies_opened": sum(len(r.get("reply_comments") or []) for r in ok), "replies_embedded": emb,
            "cap120": sum(1 for r in ok if r.get("cap") == 120)}


def load(adir: Path) -> list:
    return [json.loads(l) for l in open(adir / "raw" / "comments.jsonl", encoding="utf-8")]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from common import APP_DATA
    # 削り方ごとの残る量（AI は使わない）
    combos = [[], ["no_replies"], ["artist40"], ["std20"], ["half_weekly"], ["no_replies", "artist40"],
              ["no_replies", "artist40", "std20"], ["no_replies", "artist40", "std20", "half_weekly"]]
    for aid in sys.argv[1:] or ["a20260930-2342-0035", "a20261004-0837-4e69"]:
        adir = APP_DATA / "analyses" / aid
        rows = load(adir)
        print(f"##### {aid}")
        for cs in combos:
            s = stats(apply(rows, cs, adir))
            print(f"  {'+'.join(cs) or '元の量':<40} " + " ".join(f"{k}={v}" for k, v in s.items()))
