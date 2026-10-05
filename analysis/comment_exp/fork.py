"""
分析の写しを、plan の直後まで戻して作る（コメントを削ることもできる）。docs/COMMENT_STRATEGY_HANDOVER.md 第5章 段0 の 2。

  python3 analysis/comment_exp/setup_home.py                     # 先に一度（実験の置き場を作る）
  python3 analysis/comment_exp/fork.py a20260930-2342-0035 x-sil-base1 --pin-refs
  python3 analysis/comment_exp/fork.py a20261004-0837-4e69 x-kyw-norep1 --cut no_replies --pin-refs

- 元の分析は本物の置き場（~/Library/Application Support/UGC Analyzer/analyses/）から読むだけ。写しは output/comment_exp/home/analyses/<新しい ID>/
- 写すもの: raw（covers を除く）・fetch_log・derived・analysis.json・plan より前の outputs・state
- ID を全部書き換える（tasks.json の analysis_id と全 task_id。書き換えないと提出が元の分析に届いて黙って捨てられる。第8章の 7）
- 題を変える（同じ題だとレポートのフォルダを上書きする。第8章の 8）
- --cut で raw/comments.jsonl を削り（cuts.py）、目録のコメント件数を直し、derived/comments を pipeline と同じ引数で作り直す
- plan をここで走らせる（サービス。ccomments 以降の仕事を足す）。AI は使わない
- --pin-refs: 参考記事（ref_select・ref_digest×3）を元の版と同じにして済ませる（回ごとのぶれと AI の作業を減らす）
"""
import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

common.setup_env()
import cuts  # noqa: E402

# plan より前に作られる成果物（outputs/ の直下）。prompts・settings_used.jsonl は仕事の番号で切る
PRE_PLAN_OUTPUTS = ["taxonomy_proposal.json", "taxonomy.json", "confirm_answer.json", "labels", "labels.tsv",
                    "phases.json", "pathway.md"]
REF_TYPES = ("ref_select", "ref_digest")


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def rid(task_id: str, old: str, new: str) -> str:
    return new + task_id[len(old):] if task_id.startswith(old + "/") else task_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="元の分析 ID（本物の置き場）")
    ap.add_argument("new", help="新しい分析 ID（英数字と - _）")
    ap.add_argument("--src-dir", help="元の分析フォルダ（省くと本物の置き場の analyses/<src>）")
    ap.add_argument("--cut", default="", help="削り方（, 区切り）: " + ",".join(cuts.CUTS))
    ap.add_argument("--pin-refs", action="store_true", help="参考記事を元の版と同じにして済ませる")
    ap.add_argument("--keep-videos", help="この一覧（1行1 video_id）の動画のコメントだけ残す（select_study.py keep で作る）")
    ap.add_argument("--title", help="題（省くと「元の題〔新しい ID〕」）")
    ap.add_argument("--force", action="store_true", help="同じ ID の写しがあれば消して作り直す")
    args = ap.parse_args()

    src = Path(args.src_dir) if args.src_dir else common.APP_DATA / "analyses" / args.src
    dst = common.analysis_dir(args.new)
    if not (src / "analysis.json").exists():
        sys.exit(f"元の分析がありません: {src}")
    if not (common.HOME / "knowledge").exists():
        sys.exit("実験の置き場がありません。先に setup_home.py を走らせてください")
    if dst.exists():
        if not args.force:
            sys.exit(f"もうあります: {dst}（--force で作り直す）")
        shutil.rmtree(dst)
    cut_list = [c for c in args.cut.split(",") if c]
    old = json.loads((src / "analysis.json").read_text(encoding="utf-8"))["analysis_id"]

    st0 = json.loads((src / "state" / "tasks.json").read_text(encoding="utf-8"))
    plan = next(t for t in st0["tasks"] if t["type"] == "plan")
    if plan["status"] != "done":
        sys.exit("元の分析の plan が済んでいません")

    # --- 写す ---
    dst.mkdir(parents=True)
    shutil.copytree(src / "raw", dst / "raw", ignore=shutil.ignore_patterns("covers"))
    shutil.copytree(src / "fetch_log", dst / "fetch_log")
    shutil.copytree(src / "derived", dst / "derived", ignore=shutil.ignore_patterns("comments"))
    (dst / "eval").mkdir()
    (dst / "outputs" / "prompts").mkdir(parents=True)
    for name in PRE_PLAN_OUTPUTS:
        p = src / "outputs" / name
        if p.is_dir():
            shutil.copytree(p, dst / "outputs" / name)
        elif p.exists():
            shutil.copy2(p, dst / "outputs" / name)
    for p in sorted((src / "outputs" / "prompts").glob("*.md")):
        if int(p.name.split("-", 1)[0]) < plan["n"]:
            shutil.copy2(p, dst / "outputs" / "prompts" / p.name)
    pre_ids = {t["task_id"] for t in st0["tasks"] if t["n"] < plan["n"]}
    su = src / "outputs" / "settings_used.jsonl"
    if su.exists():
        keep = [l for l in su.read_text(encoding="utf-8").splitlines() if json.loads(l).get("task_id") in pre_ids]
        (dst / "outputs" / "settings_used.jsonl").write_text(
            "".join(json.dumps({**json.loads(l), "task_id": rid(json.loads(l)["task_id"], old, args.new)},
                               ensure_ascii=False) + "\n" for l in keep), encoding="utf-8")
    (dst / "state" / "submissions").mkdir(parents=True)
    for p in sorted((src / "state" / "submissions").glob("*.txt")):
        if int(p.name.split("-", 1)[0]) < plan["n"]:
            shutil.copy2(p, dst / "state" / "submissions" / p.name)
    # 記録は plan の手前まで（plan より後の時間を、この写しの回し直しだけで測れるように）
    log_rows = []
    for l in (src / "state" / "task_log.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(l)
        if e.get("task_id") == plan["task_id"] or e.get("ts", "") >= (plan.get("done_at") or "9"):
            break
        if e.get("task_id"):
            e["task_id"] = rid(e["task_id"], old, args.new)
        log_rows.append(json.dumps(e, ensure_ascii=False))
    (dst / "state" / "task_log.jsonl").write_text("\n".join(log_rows) + "\n", encoding="utf-8")

    # --- 仕事の列: plan まで残し、plan をまだに戻す。ID を書き換える ---
    tasks = [t for t in st0["tasks"] if t["n"] <= plan["n"]]
    for t in tasks:
        t["task_id"] = rid(t["task_id"], old, args.new)
    p_ = tasks[-1]
    p_.update({"status": "pending", "first_issued_at": None, "issued_at": None, "issue_count": 0, "done_at": None,
               "rejects": 0})
    p_.pop("detail", None)
    st = {**st0, "analysis_id": args.new, "created_at": now(), "next_n": plan["n"] + 1, "tasks": tasks}
    (dst / "state" / "tasks.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- コメントを削る ---
    rows = cuts.load(src)
    before = cuts.stats(rows)
    keep = set(Path(args.keep_videos).read_text(encoding="utf-8").split()) if args.keep_videos else None
    rows2 = cuts.apply(rows, cut_list, src, keep) if (cut_list or keep is not None) else rows
    after = cuts.stats(rows2)
    with open(dst / "raw" / "comments.jsonl", "w", encoding="utf-8") as f:
        for r in rows2:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # --- 目録 ---
    meta = json.loads((src / "analysis.json").read_text(encoding="utf-8"))
    meta["analysis_id"] = args.new
    meta["title"] = args.title or f"{meta.get('title', '')}〔{args.new}〕"
    meta["owner"] = common.USER
    meta.pop("download_key", None)
    det = (((meta.get("acquisition") or {}).get("steps") or {}).get("comments") or {}).get("detail")
    if isinstance(det, dict) and (cut_list or keep is not None):
        det.update({"videos_ok": after["videos_ok"], "comments": after["comments"], "replies": after["replies_opened"]})
    meta["experiment"] = {"source": old, "source_dir": str(src), "cuts": cut_list, "pin_refs": args.pin_refs,
                          "keep_videos": args.keep_videos,
                          "forked_at": now(), "comments_before": before, "comments_after": after,
                          "source_comments_md5": md5(src / "raw" / "comments.jsonl")}
    (dst / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- derived/comments を作り直す（acquire/pipeline.py の step_comments_md と同じ引数） ---
    d = dst
    r = subprocess.run([sys.executable, str(common.ROOT / "analysis" / "prep_comments.py"), str(d / "derived"),
                        str(d / "raw" / "comments.jsonl"), "--records", str(d / "derived" / "llm_input" / "records.jsonl"),
                        "--out", str(d / "derived" / "comments"), "--pool", str(d / "derived" / "pool.tsv")],
                       capture_output=True, text=True, cwd=str(common.ROOT))
    if r.returncode != 0:
        sys.exit("prep_comments が失敗: " + r.stderr[-2000:])
    prep_line = r.stdout.strip().splitlines()[-1]

    # --- plan を走らせる（サービス） ---
    import proto_runner as pr
    a = pr.Analysis(args.new)
    st = pr._load_state(a)
    cur = pr._run_services(a, st)
    if cur is None or cur["type"] != "ccomments":
        sys.exit(f"plan のあとが ccomments になりません: {cur and cur['type']}")

    # --- 参考記事を元の版に固定 ---
    pinned = []
    if args.pin_refs:
        orig = {(t["type"], t["params"].get("i")): t for t in st0["tasks"] if t["type"] in REF_TYPES}
        for t in st["tasks"]:
            if t["type"] not in REF_TYPES:
                continue
            o = orig.get((t["type"], t["params"].get("i")))
            if not o or o["status"] != "done":
                sys.exit(f"元の分析に済んだ {t['type']} がありません")
            t["params"] = dict(o["params"])
            t["title"] = o["title"]
            t.update({"status": "done", "done_at": now(), "detail": {"pinned_from": o["task_id"]}})
            pinned.append(t["task_id"])
        shutil.copytree(src / "outputs" / "references", dst / "outputs" / "references")
        pr._write_json(a.state_path, st)
        a.log(event="pinned_refs", tasks=pinned, source=old)

    from collections import Counter
    kinds = Counter(t["type"] for t in st["tasks"] if t["n"] > plan["n"])
    plan_t = next(t for t in st["tasks"] if t["type"] == "plan")
    print(json.dumps({"analysis": args.new, "dir": str(dst), "cuts": cut_list, "before": before, "after": after,
                      "prep": prep_line, "plan": plan_t.get("detail"), "tasks_after_plan": dict(kinds),
                      "pinned": len(pinned)}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
