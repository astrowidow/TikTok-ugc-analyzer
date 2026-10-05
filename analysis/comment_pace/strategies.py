"""取る量を減らしたときの時間（A+B+C を入れた前提の模擬）"""
import copy, json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from simulate import scripts, simulate
sys.path.insert(0, str(Path(__file__).parent.parent / "comment_exp"))
import cuts  # noqa: E402  削り方（本人40件・週ごと半分）の選び方は写しの道具と同じにする

ABC = {"stop_on_no_more": True, "steps_to_trigger": 1.5, "drop_prefetch": True, "need_actual": True}


def shape(vids, rows, key_cap=None, std_pages=None, replies=True, artist_pages=None, keep=None):
    """pages は開いた後のページ送りの回数（0 = 1ページ目だけ＝20件、1 = 40件）。keep は残す video_id（行を落とす削り方）"""
    out = []
    for v, r in zip(vids, rows):
        if keep is not None and str(r.get("video_id")) not in keep:
            continue
        v = copy.deepcopy(v)
        pages = [x for x in v["seq"] if x == "page"]
        reps = [x for x in v["seq"] if x == "reply"]
        lim = None
        if r.get("cap") == 120 and key_cap is not None:
            lim = key_cap
        rs = set(cuts.reasons(r))
        if r.get("cap") == 120 and artist_pages is not None and "artist" in rs and not (rs & cuts.KEY_REASONS):
            lim = artist_pages
        if r.get("cap") != 120 and std_pages is not None:
            lim = std_pages
        if lim is not None:
            pages = pages[:lim]
        if not replies:
            reps = []
        v["seq"] = pages + reps
        out.append(v)
    return out


if __name__ == "__main__":
    base = Path.home() / "Library/Application Support/UGC Analyzer/analyses"
    for rel in ["a20260930-2342-0035/fetch_log/comments_summary.json", "a20261004-0837-4e69/fetch_log/comments_summary.json"]:
        p = base / rel
        s = json.load(open(p))
        rows = [r for r in s["rows"] if r.get("status") in ("ok", "no_comments")]
        vids = scripts(p)
        kept, weekly = cuts.half_weekly_keep(p.parent.parent)
        half = {str(r.get("video_id")) for r in rows if str(r.get("video_id")) not in weekly or str(r.get("video_id")) in kept}
        print("#####", rel.split("/")[0], f"{len(vids)}本")
        cases = [
            ("今のまま", vids, {}),
            ("A+B+C（今の取る量）", vids, ABC),
            ("+ 返信を開かない", shape(vids, rows, replies=False), ABC),
            ("+ 標準40→20件（1ページ）", shape(vids, rows, std_pages=0), ABC),
            ("+ 標準20件・返信なし", shape(vids, rows, std_pages=0, replies=False), ABC),
            ("+ 標準20件・120→40件・返信なし", shape(vids, rows, key_cap=1, std_pages=0, replies=False), ABC),
            # 段2 の削る順（docs/COMMENT_STRATEGY_HANDOVER.md 第5章）。2026-10-05 に足した
            ("段2-1 返信なし", shape(vids, rows, replies=False), ABC),
            ("段2-2 ＋本人だけの120件→40件", shape(vids, rows, replies=False, artist_pages=1), ABC),
            ("段2-3 ＋標準20件", shape(vids, rows, replies=False, artist_pages=1, std_pages=0), ABC),
            ("段2-4 ＋週ごとの本数を半分", shape(vids, rows, replies=False, artist_pages=1, std_pages=0, keep=half), ABC),
        ]
        b = None
        for name, vs, opt in cases:
            ms, cs = zip(*(simulate(vs, opt, seed) for seed in range(10)))
            m, c = st.mean(ms), st.mean(cs)
            b = b or m
            print(f"  {name:<30}{m/60:>6.1f}時間 {100*m/b:>5.0f}%  要求{c:>5.0f}回")
