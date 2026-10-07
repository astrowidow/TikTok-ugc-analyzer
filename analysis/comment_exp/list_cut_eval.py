"""
一覧を絞った写し（fork_list.py）のレポートが、正解の動画をどれだけ名指ししているか（機械の指標。list_cut_study と同じ正解）。
写しごとに通し番号（seq）が変わるので、レポートの seq を写しの records.jsonl で動画 ID に直して数える。

  .venv/bin/python analysis/comment_exp/list_cut_eval.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
import select_study as ss  # noqa: E402

VERS = {"シルエット": ["x-sil-l100", "x-sil-l50", "x-sil-l20"], "きゃわ": ["x-kyw-l100", "x-kyw-l50", "x-kyw-l20"]}


def mentioned(vd: Path) -> set:
    rep = (vd / "outputs/REPORT.md").read_text(encoding="utf-8")
    rep = rep[:rep.find("付録")] if "付録" in rep else rep
    s2v = {int(r["seq"]): str(r["video_id"]) for r in ss.jl(vd / "derived/llm_input/records.jsonl")}
    return {s2v[int(s)] for s in set(ss.SEQ.findall(rep)) if int(s) in s2v}


def main():
    for name, aid, bases in ss.SONGS:
        L = ss.load(aid, bases)
        truth = L["truth"]["言及"]
        big = {v for v in truth if int(L["recs"][v]["plays"]) >= 1_000_000}
        print(f"\n## {name}: 正解の言及 {len(truth)}本（うち100万再生以上 {len(big)}本）")
        print("| 版 | 名指しした動画 | 正解の言及の再現 | 100万以上の再現 | 正解に無い名指し |")
        print("|---|---|---|---|---|")
        rows = [("本番", common.APP_DATA / "analyses" / aid)] + [(b, common.HOME / "analyses" / b) for b in bases] + \
               [(v, common.HOME / "analyses" / v) for v in VERS[name]]
        for label, vd in rows:
            if not (vd / "outputs/REPORT.md").exists():
                print(f"| {label} | （まだ） | | | |")
                continue
            m = mentioned(vd)
            print(f"| {label} | {len(m)} | {len(m & truth) / len(truth):.0%} | {len(m & big)}/{len(big)} | {len(m - truth)} |")


if __name__ == "__main__":
    main()
