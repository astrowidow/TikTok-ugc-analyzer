"""
AI 役の道具（実験の置き場の分析に、UGC Analyzer の道具 next_task / read / submit と同じことをする CLI）。
docs/COMMENT_STRATEGY_HANDOVER.md 第5章 段0 の 3。中身は proto_runner をそのまま呼ぶ（利用者は "local"）。

  python3 analysis/comment_exp/ai.py next   <分析ID>                 次の仕事の指示書（全文を表示し、work/ にも写す）
  python3 analysis/comment_exp/ai.py read   <分析ID> <名前> [ページ]  資料を1ページ
  python3 analysis/comment_exp/ai.py submit <task_id> <ファイル>     出力をファイルから提出
  python3 analysis/comment_exp/ai.py status <分析ID>                 仕事の進み具合
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

common.setup_env()
import proto_runner as pr  # noqa: E402

PRINT_MAX = 25_000   # これより長い指示書は、表示せずファイルの場所だけ出す（Bash の表示が切れるため）


def work_dir(aid: str) -> Path:
    d = common.HOME / "work" / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_next(aid: str) -> None:
    r = pr.next_task(common.USER, aid)
    text = r["text"]
    tid = r.get("task_id") or "none"
    path = work_dir(aid) / (tid.split("/", 1)[-1] + ".md")
    path.write_text(text, encoding="utf-8")
    print(f"[next] task_id={tid} kind={r.get('kind')} progress={r.get('progress')} chars={len(text)} file={path}")
    if len(text) <= PRINT_MAX:
        print(text)
    else:
        print(f"（指示書が {len(text)} 字あるので表示しない。上の file を Read で全部読むこと）")


def cmd_read(aid: str, name: str, page: int) -> None:
    r = pr.read(common.USER, aid, name, page)
    if "image" in r:
        print("（画像の資料はこの道具では読めない）" + r.get("text", ""))
        return
    print(r["text"])


def cmd_submit(task_id: str, file: str) -> None:
    raw = Path(file).read_text(encoding="utf-8")
    r = pr.submit(common.USER, task_id, raw)
    print(f"[submit] ok={r.get('ok')} progress={r.get('progress')}")
    print(r.get("text", ""))


def cmd_status(aid: str) -> None:
    a = pr.Analysis(aid)
    st = pr._state(a)
    cur = pr._current(st)
    print(json.dumps({"analysis": aid, "progress": pr._progress(st),
                      "current": cur and {k: cur[k] for k in ("task_id", "type", "status", "rejects")}},
                     ensure_ascii=False))


def main():
    a = sys.argv[1:]
    try:
        if a[:1] == ["next"] and len(a) == 2:
            cmd_next(a[1])
        elif a[:1] == ["read"] and len(a) in (3, 4):
            cmd_read(a[1], a[2], int(a[3]) if len(a) == 4 else 1)
        elif a[:1] == ["submit"] and len(a) == 3:
            cmd_submit(a[1], a[2])
        elif a[:1] == ["status"] and len(a) == 2:
            cmd_status(a[1])
        else:
            sys.exit(__doc__)
    except pr.RunnerError as e:
        print(f"[エラー] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
