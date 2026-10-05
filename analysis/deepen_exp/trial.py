"""界隈の掘り下げの実機の試験（docs/DEEPEN_COMMUNITY.md 9 章の段1・段2）。本物の置き場には書き込まない。

実験の置き場（output/comment_exp/home/、analysis/comment_exp/common.py）の完成済みの写しを、もう一度写して使う。

  python3 analysis/deepen_exp/trial.py prepare <元の写し> <新しい写し> <界隈> <利用者の言葉>
      … 写して ID を書き換え、道具 deepen と同じ処理（proto_runner.deepen）で取る動画を決めて仕事を足す（TikTok に触らない）
  python3 analysis/deepen_exp/trial.py fetch <新しい写し>
      … 取り足す。Chrome をアプリのプロファイルでポート 9251 に最小化で起こし、アプリと同じロックで取る（acquire/deepen.py の Run）。
        終わったら Chrome を閉じる。そのあと AI 役（analysis/comment_exp/AI_ROLE.md）が ai.py next … で dcomments から done まで回す
"""
import json
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "analysis" / "comment_exp"))
import common  # noqa: E402

common.setup_env()
APP = common.APP_DATA
PORT = 9251


def rid(task_id: str, old: str, new: str) -> str:
    return new + task_id[len(old):] if task_id.startswith(old + "/") else task_id


def prepare(src_id: str, new_id: str, community: str, instruction: str) -> None:
    import proto_runner as pr
    src, dst = common.analysis_dir(src_id), common.analysis_dir(new_id)
    if dst.exists():
        sys.exit(f"{dst} はもうある")
    shutil.copytree(src, dst)
    meta = json.loads((dst / "analysis.json").read_text(encoding="utf-8"))
    old = meta["analysis_id"]
    meta.update({"analysis_id": new_id, "title": meta.get("title", "") + f"〔{new_id}〕",
                 "experiment": {**(meta.get("experiment") or {}), "deepen_trial_from": src_id}})
    meta.setdefault("acquisition_settings", {})["chrome_port"] = str(PORT)
    (dst / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    st_p = dst / "state" / "tasks.json"
    st = json.loads(st_p.read_text(encoding="utf-8"))
    st["analysis_id"] = new_id
    for t in st["tasks"]:
        t["task_id"] = rid(t["task_id"], old, new_id)
    st_p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    log_p = dst / "state" / "task_log.jsonl"
    if log_p.exists():
        rows = [json.loads(l) for l in log_p.read_text(encoding="utf-8").splitlines() if l.strip()]
        log_p.write_text("".join(json.dumps({**r, **({"task_id": rid(r["task_id"], old, new_id)} if r.get("task_id") else {})},
                                            ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    class NoApp:   # アプリを起こさない（取得はこの試験の fetch で、アプリの外から）
        def ensure_app(self):
            return {"started": False}

        def app_state(self):
            return {"running": True, "login_wanted": False}

        def acquisition_settings(self):
            return {"chrome_port": str(PORT)}

    pr.LOCAL = NoApp()
    r = pr.deepen(common.USER, new_id, community, instruction)
    print(r["text"])
    job = json.loads((dst / "analysis.json").read_text(encoding="utf-8"))["deepen"]
    print(json.dumps({k: v for k, v in job.items() if k != "targets"}, ensure_ascii=False, indent=1))
    for x in job["targets"]:
        print(f"  {x['kind']:4s} seq {x['seq']:4d} 再生 {x['plays']:>10,} 上限 {x['cap']}")


def fetch(aid: str) -> int:
    os.environ["UGC_LOCK_DIR"] = str(APP / "locks")
    sys.path.insert(0, str(ROOT / "collector"))
    import tiktok_lock
    from acquire import deepen, pipeline
    from collector_app import chrome as chrome_mod
    d = common.analysis_dir(aid)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(d / "fetch_log" / "deepen_trial.log", encoding="utf-8")])
    log = logging.getLogger("deepen-trial")
    if tiktok_lock.holder():
        log.info("ロックが取れない（アプリが取得中）: %s", tiktok_lock.holder())
        return 2
    ch = chrome_mod.Chrome(PORT, APP / "chrome-profile", log)
    try:
        ch.ensure(minimized=True)
        if not ch.logged_in():
            log.info("TikTok のログインが切れているので走らせない")
            return 3
        log.info("Chrome 準備よし（ログイン済み・%s）", ch.window_state())
        pipeline.ensure_chrome = lambda port, plog: None   # 上で起こした
        res = deepen.Run(aid).run()
        m = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        log.info("終わり: %s %s", res, json.dumps((m.get("deepen") or {}).get("result"), ensure_ascii=False))
        return 0 if res == "done" else 1
    finally:
        try:
            ch.quit()
        except Exception:
            pass
        log.info("Chrome を閉じた")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare" and len(sys.argv) == 6:
        prepare(*sys.argv[2:6])
    elif cmd == "fetch" and len(sys.argv) == 3:
        sys.exit(fetch(sys.argv[2]))
    else:
        sys.exit(__doc__)
