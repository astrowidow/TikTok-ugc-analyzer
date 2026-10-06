#!/usr/bin/env python3
"""通し試験の走行の記録（log.jsonl）から、利用者の発言ごとの区間の時間・道具の回数・仕事の数・差し戻しを数える。
分析フォルダを渡すと、検算（outputs/verify.json）とレポートの字数・章の数も出す。

  python3 analysis/fulltest/summarize.py <走行のフォルダ> [<分析フォルダ>]
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def main():
    run = Path(sys.argv[1])
    rows = [json.loads(line) for line in (run / "log.jsonl").open(encoding="utf-8")]
    # 区間 = 利用者の発言から、その区間の最後の道具の呼び出しまで（AI の返事の記録は試験の係があとで足すので時刻に使わない）
    segs, cur = [], None
    for r in rows:
        if r["kind"] == "say" and r.get("who") == "user":
            cur = {"user": r["text"][:60], "start": ts(r["at"]), "end": ts(r["at"]), "tools": {}, "tasks": set(),
                   "submit_ok": 0, "submit_ng": 0, "ng_reasons": [], "ask_user": None, "kinds": []}
            segs.append(cur)
            continue
        if cur is None:
            continue
        if r["kind"] == "tool":
            cur["end"] = ts(r["at"])
            cur["tools"][r["tool"]] = cur["tools"].get(r["tool"], 0) + 1
            text = r.get("text") or ""
            if r["tool"] == "next_task":
                m = re.search(r"task_id: `([^`]+)`", text)
                k = re.search(r"kind: `(\w+)`", text)
                if m:
                    cur["tasks"].add(m.group(1))
                if k:
                    cur["kinds"].append(k.group(1))
                    if k.group(1) == "ask_user" and cur["ask_user"] is None:
                        cur["ask_user"] = ts(r["at"])
            if r["tool"] == "submit":
                if '"ok": true' in text:
                    cur["submit_ok"] += 1
                else:
                    cur["submit_ng"] += 1
                    cur["ng_reasons"].append(((r.get("args") or {}).get("task_id", ""), text[:160].replace("\n", " ")))
    print("| 利用者の発言 | 始め | 終わり | 分 | 道具の回数 | 仕事 | 受け取り | 差し戻し | 最後の kind |")
    print("|---|---|---|---|---|---|---|---|---|")
    for s in segs:
        mins = (s["end"] - s["start"]).total_seconds() / 60
        tools = sum(s["tools"].values())
        last = s["kinds"][-1] if s["kinds"] else "-"
        print(f"| {s['user']} | {s['start']:%H:%M:%S} | {s['end']:%H:%M:%S} | {mins:.1f} | {tools} | {len(s['tasks'])} | "
              f"{s['submit_ok']} | {s['submit_ng']} | {last} |")
        if s["ask_user"]:
            print(f"|  └ ask_user まで | | {s['ask_user']:%H:%M:%S} | {(s['ask_user'] - s['start']).total_seconds() / 60:.1f} | | | | | |")
    for s in segs:
        for tid, why in s["ng_reasons"]:
            print(f"- 差し戻し {tid}: {why}")
    if len(sys.argv) > 2:
        a = Path(sys.argv[2])
        v = a / "outputs" / "verify.json"
        if v.exists():
            j = json.loads(v.read_text(encoding="utf-8"))
            print(f"\n検算: errors {len(j.get('errors') or [])}・warnings {len(j.get('warnings') or [])}")
            for w in (j.get("errors") or []) + (j.get("warnings") or []):
                print("  -", str(w)[:200])
        rep = a / "outputs" / "REPORT.md"
        if rep.exists():
            t = rep.read_text(encoding="utf-8")
            print(f"REPORT.md: {len(t):,}字・章 {len(re.findall(r'(?m)^## ', t))}")
        note = a / "outputs" / "NOTE_BODY.md"   # 読者に見せる原稿（REPORT.md は根拠の番号 cid つきの版なので見ない）
        if note.exists():
            t = note.read_text(encoding="utf-8")
            print(f"NOTE_BODY.md: {len(t):,}字")
            # 英字の語（cid・seq など）は英字に挟まれていないものだけ（引用のスペイン語 felicidades の中の cid を数えない）
            ban = [w for w in ("用語集の通り", "用語集どおり", "指示書", "cid", "seq", "taxonomy", "synthesis", "界隈の key")
                   if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", t)]
            print("読者に見せない言葉（NOTE_BODY.md）:", ban or "なし")


if __name__ == "__main__":
    main()
