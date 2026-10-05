"""
界隈の掘り下げ: 完成したレポートの、ある界隈のコメントを取り足す（2026-10-06 ユーザー）。docs/DEEPEN_COMMUNITY.md

- plan(): 取る動画を決め、約25分に収まるように配る（AI にも TikTok にも触らない）
- request(): analysis.json の "deepen" に積む（道具 deepen が呼ぶ。取得の係が拾う）
- Run: 取得の係（acquire/worker.py）が取る。ロック・設定・Chrome・要求の間隔は本線（acquire/pipeline.py の Run）のものをそのまま使う
- merge(): 取った行を原本（raw/comments.jsonl）に足し、derived/comments を作り直す

取る動画（ユーザーの答え Q1・Q2）:
  新しく取る … その界隈のラベルがあり、コメントが無い動画。再生の下限はその分析の週ごとの下限（min_plays_weekly）
  続きを取る … その界隈でコメントはあるが、上限40件で止まり、上位リストがまだ続く動画。上限120件で取り直す
  新しく取る動画を再生の多い順に、残った時間で続きを再生の多い順に、目安25分に詰める

本線の analysis/pool.py・acquire/pipeline.py の DEFAULTS は変えない（コメント取得の戦略のセッションが触る）。spatest も引数だけで使う。

  python -m acquire.deepen plan <分析フォルダ> <界隈> [--minutes 25]   … 何を取るかだけ出す
"""
import csv
import datetime
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from acquire import pipeline  # noqa: E402

# 時間の見込み（分）と上限（件）
EST = {
    "budget_min": 25.0,        # 1回の取得の目安（2026-10-06 ユーザー「25分程度の取得時間に収まるように配分」）
    "page_min": 3.0,           # 楽曲ページ1つあたりの準備: Chrome・ページを開く・グリッドで対象を探す（本番の記録で探すのに1〜2分）
    "new_min": 1.6,            # 上限40件の動画1本（毎分3回の実走: 40本で62分。output/pacetest-20261005-3cpm/）
    "more_min": 3.2,           # 上限120件で取り直す1本（毎分2回の実走で40件の動画の約2倍: 209秒／105秒。推測）
    "new_cap": 40, "more_cap": 120,
    "deadline_extra_min": 10,  # 打ち切り = 見込み＋これ
}
GONE = ("no_video", "no_comments", "open_failed")   # 取りに行っても取れない（削除・コメント無し・開けない）


# ---------------------------------------------------------------------------
# 読む
# ---------------------------------------------------------------------------
def _jsonl(p: Path) -> list:
    out = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _records(d: Path) -> dict:
    return {str(r["video_id"]): r for r in _jsonl(d / "derived" / "llm_input" / "records.jsonl")}


def _labels(d: Path) -> dict:
    p = d / "outputs" / "labels.tsv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(r["seq"]): r for r in csv.DictReader(f, delimiter="\t")}


def _rows(p: Path) -> dict:
    """動画 ID → 最後の行（後の行が勝つ）"""
    return {str(r.get("video_id")): r for r in _jsonl(p)}


def _ok_rows(p: Path) -> dict:
    """動画 ID → 最後の ok の行（コメントあり）。flow_w1.fetched と同じ見方"""
    return {str(r["video_id"]): r for r in _jsonl(p) if r.get("status") == "ok" and r.get("comments")}


def _sources(d: Path) -> dict:
    """動画 ID → 見つけた楽曲ページ（1〜）。pipeline.Run.link_sources と同じ"""
    return {str(g.get("video_id")): int(g.get("source") or 1) for g in _jsonl(d / "raw" / "grid_links.jsonl")}


def min_plays_of(d: Path) -> int:
    m = pipeline.read_json(d / "analysis.json", {}) or {}
    v = (m.get("acquisition_settings") or {}).get("min_plays_weekly")
    return int(v if v is not None else pipeline.DEFAULTS["min_plays_weekly"])


def _can_continue(r: dict) -> bool:
    """上限で止まり、上位リストがまだ続いていた動画（spatest は「上位リストを取り切った」でも止まる。そのときは続きを取っても増えない）"""
    n = len(r.get("comments") or [])
    return bool(r.get("has_more")) and int(r.get("cap") or 40) < EST["more_cap"] and int(r.get("top_list") or 0) >= n > 0


# ---------------------------------------------------------------------------
# 決める
# ---------------------------------------------------------------------------
def candidates(d: Path, community: str, min_plays: int | None = None) -> dict:
    """界隈の「新しく取る」「続きを取る」候補（再生の多い順）と、外した理由ごとの本数"""
    recs = _records(d)
    by_seq = {int(r["seq"]): r for r in recs.values()}
    ok = _ok_rows(d / "raw" / "comments.jsonl")
    last = _rows(d / "raw" / "comments.jsonl")
    tried = {}
    for p in sorted((d / "raw" / "deepen").glob("r*.jsonl")) if (d / "raw" / "deepen").exists() else []:
        tried.update(_rows(p))
    src = _sources(d)
    floor = min_plays_of(d) if min_plays is None else int(min_plays)
    new, more = [], []
    skipped = {"below_min_plays": 0, "gone": 0}
    for s, lab in _labels(d).items():
        if lab.get("community") != community or s not in by_seq:
            continue
        r = by_seq[s]
        vid = str(r["video_id"])
        plays = int(r.get("plays") or 0)
        item = {"video_id": vid, "seq": s, "plays": plays, "date": r.get("date"), "week": r.get("week"), "page": src.get(vid, 1)}
        if vid in ok:
            if _can_continue(ok[vid]):
                more.append({**item, "cap": EST["more_cap"], "kind": "more", "had": len(ok[vid].get("comments") or [])})
            continue
        if (not r.get("enriched") or r.get("comments") == 0 or (last.get(vid) or {}).get("status") in GONE
                or (tried.get(vid) or {}).get("status") in GONE):
            skipped["gone"] += 1
            continue
        if plays < floor:
            skipped["below_min_plays"] += 1
            continue
        new.append({**item, "cap": EST["new_cap"], "kind": "new"})
    key = lambda x: (-x["plays"], x["seq"])
    return {"new": sorted(new, key=key), "more": sorted(more, key=key), "skipped": skipped, "min_plays": floor}


def plan(d: Path, community: str, minutes: float | None = None, min_plays: int | None = None) -> dict:
    """取る動画を決めて、目安の時間に収まるように配る。新しく取る動画を先に、残った時間で続きを"""
    budget = float(minutes or EST["budget_min"])
    c = candidates(Path(d), community, min_plays)
    pages, used, take = set(), 0.0, []
    for kind, cost in (("new", EST["new_min"]), ("more", EST["more_min"])):
        for x in c[kind]:
            add = cost + (EST["page_min"] if x["page"] not in pages else 0.0)
            if used + add <= budget:
                take.append(x)
                pages.add(x["page"])
                used += add
    new = [x for x in take if x["kind"] == "new"]
    more = [x for x in take if x["kind"] == "more"]
    return {"community": community, "minutes": budget, "min_plays": c["min_plays"], "est_min": round(used, 1),
            "targets": take, "n_new": len(new), "n_more": len(more),
            "left_new": len(c["new"]) - len(new), "left_more": len(c["more"]) - len(more),
            "skipped": c["skipped"], "pages": sorted(pages)}


def request(d: Path, community: str, instruction: str, pl: dict) -> dict:
    """analysis.json の "deepen" に今の回を積む（前の回は deepen_history へ）。取る動画が無ければ取得は済んだことにする"""
    d = Path(d)
    p = d / "analysis.json"
    m = pipeline.read_json(p, {}) or {}
    hist = m.setdefault("deepen_history", [])
    if m.get("deepen"):
        hist.append(m["deepen"])
    n = len(hist) + 1
    now = pipeline.now()
    job = {"round": n, "community": community, "instruction": instruction, "requested_at": now,
           "plan": {k: v for k, v in pl.items() if k != "targets"}, "targets": pl["targets"], "est_min": pl["est_min"]}
    if pl["targets"]:
        job.update({"status": "queued", "queued_at": now})
    else:
        job.update({"status": "done", "finished_at": now, "result": {"skipped": "取り足せる動画が無い"}})
    m["deepen"] = job
    pipeline.write_json(p, m)
    return job


# ---------------------------------------------------------------------------
# 取る（取得の係）
# ---------------------------------------------------------------------------
def queued(m: dict) -> bool:
    """係が拾う掘り下げの取得か（順番待ち、または途中で係が止まったもの）"""
    import tiktok_lock
    dp = m.get("deepen") or {}
    st = dp.get("status")
    return st == "queued" or (st == "running" and not tiktok_lock.pid_alive(dp.get("pid") or -1))


class Run:
    """掘り下げ1回ぶんの取得。本線の pipeline.Run（ロック・設定・ログ・楽曲ページ）を借りる"""

    def __init__(self, analysis_id: str):
        self.base = pipeline.Run(analysis_id)
        self.id = analysis_id
        self.dir = self.base.dir

    def job(self) -> dict:
        return (self.base.meta.get("deepen") or {})

    def set(self, **kv) -> None:
        self.base.update(lambda m: m.setdefault("deepen", {}).update(kv))

    def p(self, *parts) -> Path:
        return self.base.p(*parts)

    def run(self) -> str:
        job = self.job()
        n = job["round"]
        self.set(status="running", pid=os.getpid(), started_at=job.get("started_at") or pipeline.now())
        log = self.base.log
        log(f"=== 界隈の掘り下げ（{n}回目・{job['community']}）の取得を始めます: 新しく{job['plan'].get('n_new')}本・"
            f"続き{job['plan'].get('n_more')}本（見込み{job.get('est_min')}分）")
        out = self.p("raw", "deepen", f"r{n}.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        error, blocked = None, False
        try:
            blocked = self._fetch(job, out)
        except pipeline.StepError as e:
            error = str(e)
            log(f"!! 掘り下げの取得を止めました: {e}")
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log(f"!! 掘り下げの取得が例外で止まりました: {error}\n{traceback.format_exc()}")
        finally:
            self.base.unlock()
        # 止まっても、取れた分は足す（待って取り直すことはしない。短い仕事なので）
        try:
            res = merge(self.dir, n, job["community"])
        except Exception as e:
            log(f"!! 取った行を原本に足せませんでした: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.set(status="failed", error=f"原本に足せなかった（{type(e).__name__}）", failed_at=pipeline.now())
            return "failed"
        res.update({"blocked": blocked, "error": error})
        self.set(status="done", finished_at=pipeline.now(), result=res)
        log(f"=== 界隈の掘り下げの取得が終わりました: {json.dumps(res, ensure_ascii=False)}")
        return "done"

    def _fetch(self, job: dict, out: Path) -> bool:
        s = self.base.settings()
        pipeline.ensure_chrome(s["chrome_port"], self.base.log)   # アプリでは worker_entry が差し替えた Chrome の用意
        from acquire import spatest
        urls = self.base.music_urls()
        n = job["round"]
        t_start = time.time()
        limit_h = (float(job.get("est_min") or EST["budget_min"]) + EST["deadline_extra_min"]) / 60
        blocked = False
        by_page = {}
        for x in job["targets"]:
            by_page.setdefault(int(x.get("page") or 1), []).append(x)
        for k in sorted(by_page):
            if k > len(urls):
                self.base.log(f"    楽曲ページ {k} が分かりません（{len(by_page[k])}本は取らない）")
                continue
            pool = self.p("derived", "deepen", f"r{n}_pool_p{k}.tsv")
            write_pool(pool, by_page[k], job["community"], n)
            left = limit_h - (time.time() - t_start) / 3600
            if left <= 0.01:
                self.base.log("    時間の上限に達しています")
                break
            args = ["--port", str(s["chrome_port"]), "--music-url", urls[k - 1], "--pool", str(pool),
                    "--subs-out", str(self.p("fetch_log", f"deepen_r{n}_subs_p{k}.tsv")),
                    "--resume", "--collect-scrolls", str(s["collect_scrolls"]),
                    "--reply-policy", "targets", "--reply-top", str(s["reply_top"]),
                    "--reply-questions", str(s["reply_questions"]), "--reply-author", str(s["reply_author"]),
                    "--cap", str(EST["new_cap"]), "--min-comments", "20",
                    "--calls-per-min", str(s["calls_per_min"]), "--max-calls-per-min", str(s["max_calls_per_min"]),
                    "--interval", str(s["interval"]), "--jitter", "0.5", "--deadline-hours", f"{left:.3f}",
                    "--out", str(out), "--log", str(self.p("fetch_log", f"deepen_r{n}.log")),
                    "--summary", str(self.p("fetch_log", f"deepen_r{n}_summary_p{k}.json"))]
            # 候補（--candidates）は渡さない: グリッドに無い動画を同じ週の別の動画に差し替えると、界隈の外の動画になるため
            args += [f"--{f.replace('_', '-')}" for f in ("stop_on_no_more", "prescroll", "only_open_video", "remount_on_stall")
                     if s.get(f)]
            a = spatest.build_parser().parse_args(args)
            self.base.lock("界隈の掘り下げ")
            c = spatest.SpaCollector(a)
            c.between_videos = self.base.yield_lock
            try:
                c.run()
            except spatest.Blocked as e:
                c.log(f"!! 開始前にブロック検知: {e}")
                blocked = True
            finally:
                try:
                    if c.d:
                        c.d.service.stop()
                except Exception:
                    pass
            blocked = blocked or any(r.get("status") == "blocked" for r in c.rows)
            if blocked:
                self.base.log("    ブロックを検知したので、ここまでで止めます（取れた分は足します）")
                break
        return blocked


def write_pool(path: Path, targets: list, community: str, n: int) -> None:
    """spatest の --pool の形（pool.tsv と同じ列）。新しく取る動画を先に（priority 1）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("video_id\tseq\tdate\tweek\tplays\tcap\tpriority\treasons\n")
        for x in targets:
            f.write(f"{x['video_id']}\t{x['seq']}\t{x.get('date') or ''}\t{x.get('week') or ''}\t{x['plays']}\t{x['cap']}\t"
                    f"{'1' if x['kind'] == 'new' else '2'}\tdeepen:r{n}:{community}:{x['kind']}\n")


# ---------------------------------------------------------------------------
# 原本に足す
# ---------------------------------------------------------------------------
def merge(d: Path, n: int, community: str) -> dict:
    """r<n>.jsonl の ok の行を raw/comments.jsonl に足し、derived/comments を作り直す。
    続きを取った動画は前のコメントと cid で合わせ、増えなければ足さない（取り直しで減っても前の分は失わない）"""
    d = Path(d)
    src = d / "raw" / "deepen" / f"r{n}.jsonl"
    got = _ok_rows(src)
    old = _ok_rows(d / "raw" / "comments.jsonl")
    add, n_new, n_more, added = [], 0, 0, 0
    for vid, r in got.items():
        r = dict(r)
        r.update({"pool_reason": f"deepen:r{n}:{community}", "deepen_round": n})
        o = old.get(vid)
        if o:
            have = {str(c.get("cid")) for c in r.get("comments") or []}
            extra = [c for c in o.get("comments") or [] if str(c.get("cid")) not in have]
            merged = (r.get("comments") or []) + extra
            before = len(o.get("comments") or [])
            if len(merged) <= before:
                continue
            rc_have = {str(c.get("cid")) for c in r.get("reply_comments") or []}
            r["reply_comments"] = (r.get("reply_comments") or []) + [c for c in o.get("reply_comments") or []
                                                                      if str(c.get("cid")) not in rc_have]
            r.update({"comments": merged, "fetched": len(merged), "deepen_kind": "more", "had": before})
            n_more += 1
            added += len(merged) - before
        else:
            r["deepen_kind"] = "new"
            n_new += 1
            added += len(r.get("comments") or [])
        add.append(r)
    tried = _rows(src)
    res = {"videos_new": n_new, "videos_more": n_more, "comments_added": added,
           "tried": len(tried), "not_ok": sum(1 for r in tried.values() if r.get("status") != "ok")}
    summs = [pipeline.read_json(p, {}) or {} for p in sorted((d / "fetch_log").glob(f"deepen_r{n}_summary_p*.json"))]
    res["unreachable"] = sum(len(s.get("missing") or []) for s in summs)
    res["not_fetched_time"] = sum(len(s.get("not_fetched_time") or []) for s in summs)
    if not add:
        return res
    with open(d / "raw" / "comments.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for r in add:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    res["prep_comments"] = prep(d)
    return res


def prep(d: Path) -> str:
    """derived/comments を作り直す（本線の comments_md と同じ引数。プールの理由は掘り下げで取った動画の印を足した写しを渡す）"""
    d = Path(d)
    pool_all = d / "derived" / "deepen" / "pool_all.tsv"
    pool_all.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    head = None
    if (d / "derived" / "pool.tsv").exists():
        with open(d / "derived" / "pool.tsv", encoding="utf-8") as f:
            rd = csv.DictReader(f, delimiter="\t")
            head = rd.fieldnames
            rows = list(rd)
    head = list(head or ["video_id", "seq", "cap", "priority", "reasons"])
    have = {r["video_id"] for r in rows}
    for r in _jsonl(d / "raw" / "comments.jsonl"):
        reason = str(r.get("pool_reason") or "")
        if reason.startswith("deepen:") and str(r["video_id"]) not in have:
            rows.append({"video_id": str(r["video_id"]), "reasons": f"界隈の掘り下げで取り足した（{reason.split(':')[2]}）",
                         "priority": "3"})
            have.add(str(r["video_id"]))
    with open(pool_all, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=head, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in head})
    cmd = [sys.executable, str(BASE_DIR / "analysis" / "prep_comments.py"), str(d / "derived"), str(d / "raw" / "comments.jsonl"),
           "--records", str(d / "derived" / "llm_input" / "records.jsonl"), "--out", str(d / "derived" / "comments"),
           "--pool", str(pool_all)]
    r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"prep_comments.py が失敗: {(r.stderr or r.stdout)[-300:]}")
    return (r.stdout or "").strip().splitlines()[-1:][0] if (r.stdout or "").strip() else ""


# ---------------------------------------------------------------------------
def _main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="界隈の掘り下げ: 何を取るかを出す（TikTok に触らない）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("dir")
    pp.add_argument("community", nargs="?")
    pp.add_argument("--minutes", type=float, default=None)
    pp.add_argument("--min-plays", type=int, default=None)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    comms = [a.community] if a.community else sorted({l["community"] for l in _labels(d).values()} - {"unknown"})
    for k in comms:
        pl = plan(d, k, a.minutes, a.min_plays)
        print(f"{k:22s} 新しく{pl['n_new']:3d}本（残り{pl['left_new']}）・続き{pl['n_more']:3d}本（残り{pl['left_more']}）"
              f" 見込み{pl['est_min']:5.1f}分 ページ{pl['pages']} 外した{pl['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
