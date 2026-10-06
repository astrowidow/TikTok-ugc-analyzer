"""分析の取得（または掘り下げ・切り直しの取り足し）が終わるまで見張る。段が変わるたびに1行出し、終わったら抜ける（output/case_kyawa_20261006 の写し）。
  python watch_acq.py <分析フォルダ> [acquisition|deepen] [上限の分]"""
import json, sys, time
from pathlib import Path
d = Path(sys.argv[1]); key = sys.argv[2] if len(sys.argv) > 2 else "acquisition"
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 360
t0 = time.time(); last = None
while True:
    try:
        m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    except Exception as e:
        time.sleep(10); continue
    a = m.get(key) or {}
    st = a.get("status")
    steps = {k: v.get("status") for k, v in (a.get("steps") or {}).items()}
    cur = (st, json.dumps(steps, ensure_ascii=False), json.dumps(a.get("progress") or a.get("result") or "", ensure_ascii=False)[:200])
    if cur[:2] != (last or (None, None))[:2]:
        print(time.strftime("%H:%M:%S"), st, cur[1], cur[2], flush=True)
    last = cur
    if st not in ("queued", "running", None, "waiting", "paused_retry"):
        print("終わり:", st, json.dumps({k: (v.get("seconds"), v.get("detail")) for k, v in (a.get("steps") or {}).items()}, ensure_ascii=False)[:3000])
        break
    if time.time() - t0 > limit * 60:
        print("見張りの上限に達した"); break
    time.sleep(60)
