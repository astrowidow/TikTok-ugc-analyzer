"""間隔を詰めたときの時間（A+B+C を入れた前提）。シルエット本番の仕事で模擬"""
import json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from simulate import scripts, simulate
from strategies import shape
ABC = {"stop_on_no_more": True, "steps_to_trigger": 1.5, "drop_prefetch": True, "need_actual": True}
p = Path("/Users/belle/workspace/TikTok-ugc-analyzer/output/speedtest-20261005/old_silhouette_summary.json")
s = json.load(open(p)); rows = [r for r in s["rows"] if r.get("status") in ("ok", "no_comments")]
vids = scripts(p)
light = shape(vids, rows, std_pages=0, replies=False)
print(f"{'設定（平均/60秒の上限）':<22}{'今の量':>10}{'毎分':>6}{'標準20件・返信なし':>18}{'毎分':>6}")
for cpm, mx in [(1.8, 2.0), (2.0, 2.0), (2.0, 3.0), (2.2, 3.0), (2.5, 3.0)]:
    o = {**ABC, "cpm": cpm, "max_cpm": mx}
    a = [simulate(vids, o, k) for k in range(10)]; b = [simulate(light, o, k) for k in range(10)]
    ma, ca = st.mean(x[0] for x in a), st.mean(x[1] for x in a)
    mb, cb = st.mean(x[0] for x in b), st.mean(x[1] for x in b)
    print(f"{cpm}回/分・60秒に{int(mx)}回{'':<8}{ma/60:>8.1f}時間{ca/ma:>6.2f}{mb/60:>15.1f}時間{cb/mb:>6.2f}")
