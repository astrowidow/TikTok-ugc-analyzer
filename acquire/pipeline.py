"""
分析1つの取得の段（段2）: 楽曲 → 一覧 → 属性・サムネ → 集計と AI の入力 → 候補プール → コメント → コメント整形 → 知らせ。

docs/IMPLEMENTATION_HANDOVER.md 第5章、docs/BLUEPRINT.md 第5章。取得の係（acquire/worker.py）が呼ぶ。
入出力は全部、分析フォルダ（output/analyses/<分析ID>/）:
  raw/        ①原本        grid_links.jsonl（楽曲ページで集めたリンク）・enriched.jsonl（動画ページの属性）・covers/・comments.jsonl
  fetch_log/  ②取得の記録  pipeline.log・comments.log・comments_summary.json・substitutions.tsv・notify.log（すべて日時つき）
  derived/    ③計算物      list.csv（13列）・videos.jsonl・weekly.tsv・llm_input/・pool.tsv・comments/
状態は analysis.json の "acquisition"。工程ごとに done を持つので、どこで止まっても続きから（各工程も再開できる）。

TikTok に触る工程（resolve・list・enrich・comments）は tiktok_lock で既存の CSV ジョブと直列。
CSV ジョブが待っていたら、区切り（属性は20本ごと、コメントは1本ごと）でロックを譲る。
"""
import csv
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "analysis"))

import tiktok_lock  # noqa: E402

ANALYSES_DIR = Path(os.environ.get("UGC_ANALYSES_DIR", BASE_DIR / "output" / "analyses"))
STEPS = ["resolve", "list", "enrich", "derive", "pool", "comments", "comments_md", "notify"]
STEP_LABELS = {"resolve": "楽曲ページを探す", "list": "動画の一覧を集める", "enrich": "動画ごとの属性とサムネを取る",
               "derive": "集計と AI の入力を作る", "pool": "コメントを取る動画を決める", "comments": "コメントを取る",
               "comments_md": "コメントを整形する", "notify": "完了を知らせる"}

# 取得の設定（analysis.json の "acquisition_settings" で上書きできる。試走は pool_budget を小さくする）
DEFAULTS = {
    "comment_hours": 12.0,       # コメント取得にかける時間の見積もり（プールの本数をこの時間で配る）
    # 取得を打ち切る時間の上限（0 は打ち切らない）。2026-10-01 ユーザー判断: 12時間の打ち切りはやめ、
    # 「シルエットはモデルケースなので、上限に引っ掛かる状況は望ましくない」「上限は20時間くらい。あったほうがいい」
    "comment_deadline_hours": 20,
    # プールの予算の見積もり（1本あたりの分）。試走の実測（2026-09-30）: 40件の動画4.2分・120件7.2分（返信を各2件開いた場合）。
    # 返信を各1件にして 3.1分・6.1分の見込み（docs/IMPLEMENTATION_LOG.md D20）
    "min_per_video": 3.1, "min_per_key_video": 6.1,
    "reply_top": 1, "reply_questions": 1, "reply_author": 1,   # 返信欄を開くコメント（返信数の多い順・質問形・投稿者本人）
    "pool_budget": 0,            # プールの本数を直接指定（0なら時間から）
    "list_sets": 3, "list_scrolls": 30, "list_stall": 4,   # 一覧: scraper.py と同じ 3セット×30スクロール。増えなくなったら早めに止める
    "enrich_sleep": 2.0,
    "collect_scrolls": 150,      # コメント: グリッドでプールを探すスクロールの上限
    "calls_per_min": 1.8, "max_calls_per_min": 2.0, "interval": 15.0,
    "chrome_port": "9222",
    "blocked_wait_min": 90,      # ブロックを検知したら空ける時間（COMMENT_ACQUISITION_HANDOVER 2-6: 約1.5時間で回復）
    "blocked_retries": 2,
}


class StepError(Exception):
    """工程を失敗として止める（理由は利用者向けの短い日本語）"""


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


class Run:
    """分析1つぶんの取得の実行"""

    def __init__(self, analysis_id: str):
        self.id = analysis_id
        self.dir = ANALYSES_DIR / analysis_id
        if not (self.dir / "analysis.json").exists():
            raise StepError(f"分析 {analysis_id} がありません")
        for d in ("raw", "fetch_log", "derived", "outputs", "eval", "state"):
            (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.owner = f"acq:{analysis_id}"
        self.locked = False

    # --- 状態 ---
    @property
    def meta(self) -> dict:
        return read_json(self.dir / "analysis.json", {})

    def update(self, fn) -> dict:
        m = self.meta
        fn(m)
        write_json(self.dir / "analysis.json", m)
        return m

    def settings(self) -> dict:
        return {**DEFAULTS, **(self.meta.get("acquisition_settings") or {})}

    def log(self, msg: str) -> None:
        line = f"[{now()}] {msg}"
        print(line, flush=True)
        with open(self.dir / "fetch_log" / "pipeline.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def p(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    # --- ロック ---
    def lock(self, what: str) -> None:
        if self.locked:
            return
        self.log(f"TikTok のロックを取ります（{what}）")
        tiktok_lock.acquire(self.owner, on_wait=lambda h: self.log(
            f"  ほかの処理が TikTok を使っているので待ちます（{(h or {}).get('owner', '?')}）"))
        self.locked = True

    def unlock(self) -> None:
        if self.locked:
            tiktok_lock.release(self.owner)
            self.locked = False

    def yield_lock(self) -> float:
        if not self.locked:
            return 0.0
        return tiktok_lock.yield_if_waiting(self.owner, log=self.log)

    # --- 実行 ---
    def run(self) -> str:
        m = self.update(lambda m: m.setdefault("acquisition", {}).update(
            {"status": "running", "pid": os.getpid(), "started_at": (m.get("acquisition") or {}).get("started_at") or now()}))
        acq = m["acquisition"]
        acq.setdefault("steps", {})
        self.log(f"=== 取得の段を始めます: {self.id}（{m.get('title')}）")
        cleanup_chromedrivers(self.log)
        try:
            for step in STEPS:
                st = (self.meta.get("acquisition") or {}).get("steps", {}).get(step) or {}
                if st.get("status") == "done":
                    continue
                self._mark(step, {"status": "running", "started_at": st.get("started_at") or now()})
                self.update(lambda m: m["acquisition"].update({"step": step}))
                self.log(f"--- {STEP_LABELS[step]}（{step}）")
                t0 = time.time()
                detail = getattr(self, "step_" + step)() or {}
                self._mark(step, {"status": "done", "done_at": now(), "seconds": int(time.time() - t0), "detail": detail})
                self.log(f"    済み（{int(time.time() - t0)}秒）: {json.dumps(detail, ensure_ascii=False)[:500]}")
            self.update(lambda m: m["acquisition"].update({"status": "done", "finished_at": now(), "step": None}))
            self.log("=== 取得の段が終わりました")
            return "done"
        except StepError as e:
            self.log(f"!! 止めました: {e}")
            self.update(lambda m: m["acquisition"].update({"status": "failed", "error": str(e), "failed_at": now()}))
            try:
                import acquire.notify as notify
                notify.send(self, failed=str(e))
            except Exception:
                self.log("    失敗の知らせも送れませんでした: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
            return "failed"
        except Exception as e:
            self.log(f"!! 例外で止まりました: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.update(lambda m: m["acquisition"].update({"status": "failed", "error": f"{type(e).__name__}: {e}",
                                                           "failed_at": now()}))
            return "failed"
        finally:
            self.unlock()
            cleanup_chromedrivers(self.log)

    def _mark(self, step, values):
        def fn(m):
            s = m["acquisition"].setdefault("steps", {}).setdefault(step, {})
            s.update(values)
        self.update(fn)

    # --- 工程 ---
    def step_resolve(self):
        m = self.meta
        if m.get("music_url"):
            return {"music_url": m["music_url"], "how": "given"}
        song = m.get("song") or {}
        self.lock("楽曲ページを探す")
        import scraper
        job_id = f"acq-{self.id}"
        scraper.init_job(job_id, mode="auto")
        url = scraper.find_tiktok_music_url(job_id, song.get("artist", ""), song.get("title", ""))
        if not url:
            raise StepError("TikTok の楽曲ページが見つかりませんでした。楽曲ページの URL を教えてください")
        self.update(lambda m: m.update({"music_url": url}))
        return {"music_url": url, "how": "search"}

    def step_list(self):
        """scraper.py と同じ手順（未ログイン・ヘッドレス・END キーでスクロール）でリンクだけ集める（D16）"""
        s = self.settings()
        out = self.p("raw", "grid_links.jsonl")
        if out.exists() and out.stat().st_size:
            n = sum(1 for _ in open(out, encoding="utf-8"))
            self._write_links_csv()
            return {"links": n, "resumed": True}
        self.lock("一覧")
        import scraper
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        url = self.meta["music_url"]
        seen, order = {}, 0
        driver = scraper.create_headless_driver()
        page_info = None
        try:
            for set_i in range(int(s["list_sets"])):
                driver.get(url)
                time.sleep(scraper.PAGE_LOAD_TIME)
                if page_info is None:   # 楽曲ページの UGC 数（「131.9K 動画」）。記事は全体の UGC 数で語るため（2026-10-03 ユーザー）
                    page_info = read_music_page(driver)
                    write_json(self.p("raw", "music_page.json"), {**page_info, "at": now(), "url": url})
                    self.log(f"    楽曲ページ: {page_info}")
                last, stall = -1, 0
                for scroll_i in range(int(s["list_scrolls"])):
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
                    time.sleep(scraper.SCROLL_PAUSE_TIME)
                    n = driver.execute_script(scraper._COUNT_LINKS_JS) or 0
                    if n <= last:
                        stall += 1
                        if stall >= int(s["list_stall"]):
                            break
                    else:
                        stall = 0
                    last = n
                hrefs = driver.execute_script(
                    "return Array.from(document.querySelectorAll('a[href*=\"/video/\"], a[href*=\"/photo/\"]'))"
                    ".map(a => a.href).filter(h => h);") or []
                new = 0
                for h in hrefs:
                    h = h.split("?")[0]
                    if h not in seen:
                        seen[h] = {"url": h, "video_id": h.rstrip("/").rsplit("/", 1)[-1], "set": set_i,
                                   "order": order, "type": "Photo" if "/photo/" in h else "Video",
                                   "collected_at": now()}
                        order += 1
                        new += 1
                self.log(f"    一覧 {set_i + 1}/{s['list_sets']}セット: このセット{len(hrefs)}件 / 新規{new} / 累計{len(seen)}"
                         f"（スクロール{scroll_i + 1}回）")
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        if not seen:
            raise StepError("楽曲ページから動画が1本も見つかりませんでした（URL が違うか、TikTok 側の表示制限）")
        with open(out, "w", encoding="utf-8") as f:
            for r in seen.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._write_links_csv()
        return {"links": len(seen), "ugc_total": (page_info or {}).get("video_count")}

    def _write_links_csv(self):
        with open(self.p("raw", "grid_links.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["URL", "Type"])
            for line in open(self.p("raw", "grid_links.jsonl"), encoding="utf-8"):
                r = json.loads(line)
                w.writerow([r["url"], r["type"]])

    def step_enrich(self):
        s = self.settings()
        self.lock("属性")
        import enrich
        res = enrich.run(self.p("raw", "grid_links.csv"), self.p("raw", "enriched.jsonl"),
                         covers=self.p("raw", "covers"), sleep=float(s["enrich_sleep"]),
                         between=lambda i: self.yield_lock(), log=lambda m: self.log("    " + m), restart_every=30)
        self.unlock()
        n = self._write_list_csv()
        res["list_rows"] = n
        return res

    def _write_list_csv(self) -> int:
        """既存スクレイパーと同じ13列の CSV を、enriched から作る（derived/list.csv）"""
        title = f"{(self.meta.get('song') or {}).get('artist', '')} {(self.meta.get('song') or {}).get('title', '')}".strip()
        enriched = {}
        for line in open(self.p("raw", "enriched.jsonl"), encoding="utf-8"):
            e = json.loads(line)
            enriched[str(e.get("video_id"))] = e
        rows = []
        for line in open(self.p("raw", "grid_links.jsonl"), encoding="utf-8"):
            g = json.loads(line)
            e = enriched.get(g["video_id"]) or {}
            st = e.get("stats") or {}
            if e.get("create_time"):
                created = e["create_time"]
            else:
                try:  # 写真・削除済み: ID の上位32ビットが投稿時刻（scraper.py の写真と同じ）
                    created = datetime.datetime.fromtimestamp(int(g["video_id"]) >> 32, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
            rows.append({"URL": g["url"], "Created Date": created, "Description": e.get("desc") or "",
                         "Likes": st.get("diggCount", ""), "Shares": st.get("shareCount", ""),
                         "Comments": st.get("commentCount", ""), "Plays": st.get("playCount", ""),
                         "Saves": st.get("collectCount", ""), "Reposts": st.get("repostCount", ""),
                         "Type": g["type"], "Username": (e.get("author") or {}).get("unique_id") or
                         (g["url"].split("/@")[1].split("/")[0] if "/@" in g["url"] else "")})
        with open(self.p("derived", "list.csv"), "w", encoding="utf-8-sig", newline="") as f:
            cols = ["Index", "URL", "Created Date", "Description", "Likes", "Shares", "Comments", "Plays", "Saves",
                    "Reposts", "Type", "Username", "Project Name"]
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for i, r in enumerate(rows):
                w.writerow({"Index": i, **r, "Project Name": title})
        return len(rows)

    def _script(self, *args) -> str:
        cmd = [sys.executable, *[str(a) for a in args]]
        env = {**os.environ, "PYTHONUTF8": "1"}
        r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env)
        if r.returncode != 0:
            self.log(f"    {' '.join(cmd[1:3])} が失敗: {r.stderr[-1500:]}")
            raise StepError(f"{Path(str(args[0])).name} が失敗しました（ログを見てください）")
        return (r.stdout or "").strip()

    def step_derive(self):
        out1 = self._script("analysis/prep_sample.py", self.p("derived", "list.csv"), self.p("derived"))
        out2 = self._script("analysis/build_llm_input.py", self.p("derived"), "--enriched", self.p("raw", "enriched.jsonl"),
                            "--out", self.p("derived", "llm_input"))
        return {"prep_sample": out1.splitlines()[-1:], "build_llm_input": out2.splitlines()[-1:]}

    def step_pool(self):
        s = self.settings()
        out = self.p("derived", "pool.tsv")
        if out.exists():  # 再開: プールは一度決めたら変えない（取得済みと食い違わないように）
            n = sum(1 for _ in open(out, encoding="utf-8")) - 1
            return {"n_pool": n, "resumed": True}
        args = ["analysis/pool.py", self.p("derived", "videos.jsonl"), self.p("raw", "enriched.jsonl"), out,
                "--hours", s["comment_hours"], "--min-per-video", s["min_per_video"],
                "--min-per-key-video", s["min_per_key_video"]]
        if int(s["pool_budget"] or 0):
            args += ["--budget", int(s["pool_budget"])]
        return json.loads(self._script(*args).splitlines()[-1])

    def step_comments(self):
        s = self.settings()
        ensure_chrome(s["chrome_port"], self.log)
        from acquire import spatest
        spent = float(((self.meta.get("acquisition") or {}).get("steps", {}).get("comments") or {}).get("active_hours") or 0)
        deadline = float(s.get("comment_deadline_hours") or 0)
        retries = 0
        while True:
            left = (deadline - spent) if deadline else 0.0      # 0 は spatest で「打ち切らない」
            if deadline and left <= 0.02:
                self.log("    時間の上限に達しています")
                break
            args = ["--port", str(s["chrome_port"]), "--music-url", self.meta["music_url"],
                    "--pool", str(self.p("derived", "pool.tsv")),
                    "--candidates", str(self.p("derived", "llm_input", "records.jsonl")),
                    "--subs-out", str(self.p("fetch_log", "substitutions.tsv")),
                    "--resume", "--collect-scrolls", str(s["collect_scrolls"]),
                    "--reply-policy", "targets", "--reply-top", str(s["reply_top"]),
                    "--reply-questions", str(s["reply_questions"]), "--reply-author", str(s["reply_author"]),
                    "--cap", "40", "--min-comments", "20",
                    "--calls-per-min", str(s["calls_per_min"]), "--max-calls-per-min", str(s["max_calls_per_min"]),
                    "--interval", str(s["interval"]), "--jitter", "0.5", "--deadline-hours", f"{left:.3f}",
                    "--out", str(self.p("raw", "comments.jsonl")), "--log", str(self.p("fetch_log", "comments.log")),
                    "--summary", str(self.p("fetch_log", "comments_summary.json"))]
            a = spatest.build_parser().parse_args(args)
            self.lock("コメント")
            c = spatest.SpaCollector(a)
            c.between_videos = self.yield_lock
            t0 = time.time()
            blocked = False
            try:
                c.run()
            except spatest.Blocked as e:
                c.log(f"!! 開始前にブロック検知: {e}")
                blocked = True
            finally:
                # chromedriver だけを止める。ログイン済みの Chrome 本体には quit を送らない
                # （今までの走行も quit はしていない。送ったときの挙動は確かめていない）
                try:
                    if c.d:
                        c.d.service.stop()
                except Exception:
                    pass
            spent += max(0.0, (time.time() - t0 - c.yielded) / 3600)
            self._mark("comments", {"active_hours": round(spent, 3)})
            blocked = blocked or any(r.get("status") == "blocked" for r in c.rows)
            errors = [r for r in c.rows if r.get("status") == "error"]
            if blocked and retries < int(s["blocked_retries"]) and (not deadline or spent < deadline):
                retries += 1
                self.unlock()
                self.log(f"    ブロックを検知したので {s['blocked_wait_min']}分空けてから続きを取ります（{retries}回目）")
                time.sleep(float(s["blocked_wait_min"]) * 60)
                continue
            if len(errors) >= 2 and retries < int(s["blocked_retries"]):
                retries += 1
                self.log("    連続失敗で止まったので、少し空けて続きから取り直します")
                self.unlock()
                time.sleep(120)
                continue
            break
        self.unlock()
        return self._check_comments(blocked)

    def _check_comments(self, blocked: bool) -> dict:
        """走行後に必ず確かめること（COMMENT_ACQUISITION_HANDOVER 第9章）を機械で確かめて記録する"""
        rows = []
        p = self.p("raw", "comments.jsonl")
        if p.exists():
            for line in open(p, encoding="utf-8"):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        ok = [r for r in rows if r.get("status") == "ok"]
        ok = list({r["video_id"]: r for r in ok}.values())
        mixed = sum(1 for r in ok for c in (r.get("comments") or []) if str(c.get("aweme_id")) != str(r["video_id"]))
        # 20件で頭打ち（2-9）の疑い: 上位リストが20件以上あったのに20件で止まったもの。
        # 上位リストが20件未満なら、最低件数（--min-comments 20）で正しく止まっただけ（2026-10-01 の写しで1本、上位17件で誤報になった）
        capped20 = sum(1 for r in ok if len(r.get("comments") or []) == 20 and r.get("has_more")
                       and (r.get("top_list") or 0) >= 20)
        summ = read_json(self.p("fetch_log", "comments_summary.json"), {}) or {}
        n_pool = sum(1 for _ in open(self.p("derived", "pool.tsv"), encoding="utf-8")) - 1
        res = {"videos_ok": len(ok), "pool": n_pool,
               "comments": sum(len(r.get("comments") or []) for r in ok),
               "replies": sum(len(r.get("reply_comments") or []) for r in ok),
               "mixed_comments": mixed, "stuck_at_20": capped20,
               "substituted": sum(1 for s in (summ.get("substitutions") or []) if s.get("substitute")),
               "unreachable": len(summ.get("missing") or []),
               "not_fetched_time": len(summ.get("not_fetched_time") or []),
               "blocked": blocked, "spa_no": sum(1 for r in ok if r.get("spa") is False)}
        if mixed:
            self.log(f"    ★ 混入 {mixed}件（aweme_id が動画と違うコメント）")
        if capped20:
            self.log(f"    ★ 20件で頭打ちの動画 {capped20}本（描画が止まっていた可能性。2-9）")
        if not ok:
            raise StepError("コメントが1本も取れませんでした（ログインの状態か、TikTok 側の制限の可能性）")
        return res

    def step_comments_md(self):
        out = self._script("analysis/prep_comments.py", self.p("derived"), self.p("raw", "comments.jsonl"),
                           "--records", self.p("derived", "llm_input", "records.jsonl"),
                           "--out", self.p("derived", "comments"), "--pool", self.p("derived", "pool.tsv"))
        return {"prep_comments": out.splitlines()[-1:]}

    def step_notify(self):
        import acquire.notify as notify
        return notify.send(self)


# ---------------------------------------------------------------------------
MUSIC_PAGE_JS = """const g = k => { const e = document.querySelector('[data-e2e="' + k + '"]'); return e ? (e.innerText || '').trim() : null; };
return {title: g('music-title'), creator: g('music-creator'), video_count_text: g('music-video-count')};"""


def parse_count(text):
    """「131.9K 動画」「1.2M」「3.5万」「12,345」→ 数。読めなければ None"""
    m = re.search(r"([\d.,]+)\s*([KkMmBb万億]?)", text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mul = {"k": 1e3, "m": 1e6, "b": 1e9, "万": 1e4, "億": 1e8}.get(m.group(2).lower(), 1)
    return int(round(v * mul))


def read_music_page(driver, wait: float = 15) -> dict:
    """楽曲ページの題・作者・UGC 数（表示の文字と数）。ページを開いたあとで呼ぶ（要求は増えない）。
    表示は読み込みのあとから出てくるので、UGC 数が出るまで最大 wait 秒待つ"""
    info = {}
    t0 = time.time()
    while True:
        try:
            info = driver.execute_script(MUSIC_PAGE_JS) or {}
        except Exception as e:
            return {"error": f"{type(e).__name__}"}
        if info.get("video_count_text") or time.time() - t0 > wait:
            break
        time.sleep(1)
    info["video_count"] = parse_count(info.get("video_count_text"))
    return info


def read_video_music(driver, url: str, wait: float = 4) -> dict:
    """動画のページ（ログインなし）から、使っている音源の id・題・作者を読む（属性の段と同じ読み方）"""
    import enrich
    from selenium.webdriver.common.by import By
    driver.get(url)
    time.sleep(wait)
    tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
    it, _ = enrich.find_item_struct(json.loads(tag.get_attribute("innerHTML")))
    m = (it or {}).get("music") or {}
    return {"id": m.get("id"), "title": m.get("title"), "author": m.get("authorName"), "original": m.get("original")}


def ensure_chrome(port, log) -> None:
    """収集用 Chrome（ログイン済み、9222）が待ち受けているか。無ければ決まった手順（schtasks）で起こす"""
    def listening():
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                return True
        except OSError:
            return False
    if listening():
        return
    if sys.platform == "win32":
        log(f"    収集用 Chrome（{port}）が待ち受けていないので、schtasks /Run /TN tiktok-chrome で起こします")
        subprocess.run(["schtasks", "/Run", "/TN", "tiktok-chrome"], capture_output=True)
        for _ in range(30):
            time.sleep(2)
            if listening():
                time.sleep(5)
                return
    raise StepError(f"収集用の Chrome（ポート {port}）に接続できません")


def cleanup_chromedrivers(log) -> None:
    """親の死んだ chromedriver を PID で止める（出力ファイルを握ったまま残り、再開を無言で失敗させる。7-D）"""
    if sys.platform != "win32":
        return
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process -Filter \"Name='chromedriver.exe'\" | "
                            "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout or "").strip()
        if not out:
            return
        procs = json.loads(out)
        if isinstance(procs, dict):
            procs = [procs]
        for p in procs:
            if not tiktok_lock.pid_alive(int(p.get("ParentProcessId") or 0)):
                subprocess.run(["taskkill", "/PID", str(p["ProcessId"]), "/F"], capture_output=True)
                log(f"    取り残された chromedriver（PID {p['ProcessId']}）を止めました")
    except Exception as e:
        log(f"    chromedriver の掃除に失敗（続けます）: {e}")


if __name__ == "__main__":
    print(Run(sys.argv[1]).run())
