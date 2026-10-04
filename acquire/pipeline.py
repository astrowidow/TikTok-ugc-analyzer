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
    # コメント取得の速さ（acquire/spatest.py 冒頭の 2026-10-05 の説明）。要求の間隔の設定は変えない。
    # 試験（きゃわぽっぴんどぅー20本、直した版）で 84.5分 → 61.6分、20本とも取得・混入0・頭打ち0（docs/COMMENT_SPEED.md）
    "stop_on_no_more": True, "prescroll": True, "only_open_video": True, "remount_on_stall": True,
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

    def music_urls(self) -> list:
        """取得する楽曲ページ（1つ目が主）。同じ曲の配信版・sped up 版などで複数あるときは、全部から一覧を集めて合わせる
        （2026-10-04 ユーザー「2つ以上の楽曲ページを参照させたい場合もある」）"""
        m = self.meta
        urls = [u for u in (m.get("music_urls") or []) if u]
        if m.get("music_url") and m["music_url"] not in urls:
            urls.insert(0, m["music_url"])
        return urls

    def link_sources(self) -> dict:
        """動画 ID → どの楽曲ページ（1〜）のグリッドで見つけたか（コメントは見つけたページのグリッドから取る）"""
        out = {}
        p = self.p("raw", "grid_links.jsonl")
        if p.exists():
            for line in open(p, encoding="utf-8"):
                try:
                    g = json.loads(line)
                except ValueError:
                    continue
                out[str(g.get("video_id"))] = int(g.get("source") or 1)
        return out

    def step_list(self):
        """scraper.py と同じ手順（未ログイン・ヘッドレス・END キーでスクロール）でリンクだけ集める（D16）。
        楽曲ページが複数なら、ページごとに集めて重ねない（どのページで見つけたかを source に残す）"""
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
        urls = self.music_urls()
        # 受け付けのときに読んだ題・作者・UGC 数（ここで読めなかったときの控え）
        before = {p.get("url"): p for p in (read_json(self.p("raw", "music_pages.json"), []) or []) if isinstance(p, dict)}
        if not before:
            m0 = read_json(self.p("raw", "music_page.json"), {}) or {}
            if m0.get("url"):
                before[m0["url"]] = m0
        seen, order = {}, 0
        pages = []
        driver = scraper.create_headless_driver()
        try:
            for k, url in enumerate(urls, 1):
                tag = f"{k}ページ目 " if len(urls) > 1 else ""
                page_info = None
                n0 = len(seen)
                for set_i in range(int(s["list_sets"])):
                    driver.get(url)
                    time.sleep(scraper.PAGE_LOAD_TIME)
                    if page_info is None:   # 楽曲ページの UGC 数（「131.9K 動画」）。記事は全体の UGC 数で語るため（2026-10-03 ユーザー）
                        page_info = read_music_page(driver)
                        self.log(f"    楽曲ページ{('（' + str(k) + '）') if len(urls) > 1 else ''}: {page_info}")
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
                                       "source": k, "collected_at": now()}
                            order += 1
                            new += 1
                    self.log(f"    一覧 {tag}{set_i + 1}/{s['list_sets']}セット: このセット{len(hrefs)}件 / 新規{new} / 累計{len(seen)}"
                             f"（スクロール{scroll_i + 1}回）")
                info = {**(before.get(url) or {}), **{key: v for key, v in (page_info or {}).items() if v is not None}}
                info.pop("how", None)
                pages.append({**info, "url": url, "page": k, "links": len(seen) - n0, "at": now()})
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        if not seen:
            raise StepError("楽曲ページから動画が1本も見つかりませんでした（URL が違うか、TikTok 側の表示制限）")
        for pg in pages:   # 楽曲ページが作られた日時（番号から）
            pg["created_at"] = iso_time(id_time(pg["url"].rstrip("/").rsplit("-", 1)[-1]))
        dropped = self.drop_before_release(seen, urls)
        if not seen:
            raise StepError("楽曲ページの動画が、全部曲の公開より前の日付でした（楽曲ページが違う可能性）")
        write_json(self.p("raw", "music_pages.json"), pages)
        write_json(self.p("raw", "music_page.json"), pages[0])   # 主のページ（前からの形）
        with open(out, "w", encoding="utf-8") as f:
            for r in seen.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._write_links_csv()
        counts = [p.get("video_count") for p in pages]
        res = {"links": len(seen), "ugc_total": sum(c for c in counts if c) or None,
               "release": iso_time(release_time(urls)), "dropped_before_release": dropped}
        if len(pages) > 1:
            res["pages"] = [{"url": p["url"], "links": p["links"], "ugc": p.get("video_count")} for p in pages]
        return res

    def drop_before_release(self, seen: dict, urls: list) -> int:
        """曲の公開（楽曲ページが作られた時刻）より前に投稿された動画を、一覧から除く。音源はページができる前には使えないので、
        ページに載っていても、あとから音源が付いた投稿（ノイズ）。すべての分析から外す（2026-10-04 ユーザー
        「曲の公開日より前の投稿は全ての分析から外す。動画収集の段階で弾けるなら弾く」）。除いたものは取得の記録に残す"""
        rel = release_time(urls)
        if not rel:
            return 0
        out = []
        for h, r in list(seen.items()):
            t = id_time(r.get("video_id"))
            if t and t < rel:
                out.append({**r, "posted_at": iso_time(t)})
                del seen[h]
        write_json(self.p("fetch_log", "before_release.json"),
                   {"release": iso_time(rel), "dropped": len(out), "videos": out, "at": now()})
        if out:
            self.log(f"    曲の公開（{iso_time(rel)}）より前の日付の投稿 {len(out)}本を除きました（あとから音源が付いたもの）")
        return len(out)

    def apply_release_filter(self) -> dict:
        """取得済みの分析に、曲の公開より前の投稿の除外をあとからかける（この直し（2026-10-04）より前に取った分析用）。
        一覧・集計・AI の入力・コメントの整形を作り直す。コメントは取り直さない。AI の仕事の状態は、呼ぶ側で初めに戻す。
        作り直す前の derived/ は呼ぶ側で控えを取っておく"""
        p = self.p("raw", "grid_links.jsonl")
        rows = [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]
        seen = {r["url"]: r for r in rows}
        n = self.drop_before_release(seen, self.music_urls())
        if not n:
            return {"dropped_before_release": 0}
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                if r["url"] in seen:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._write_links_csv()
        n_rows = self._write_list_csv()
        derive = self.step_derive()
        vids = {str(r["video_id"]) for r in seen.values()}
        for pool in sorted(self.p("derived").glob("pool*.tsv")):   # コメントを取る動画の表からも外す
            lines = [ln for ln in pool.read_text(encoding="utf-8").splitlines() if ln.strip()]
            vi = lines[0].split("\t").index("video_id")
            pool.write_text("\n".join([lines[0]] + [ln for ln in lines[1:] if ln.split("\t")[vi] in vids]) + "\n",
                            encoding="utf-8")
        import shutil
        for sub in ("comments", "ai", "review"):   # seq（通し番号）で引くものは、番号が変わるので消して作り直させる
            shutil.rmtree(self.p("derived", sub), ignore_errors=True)
        md = self.step_comments_md()
        rel = iso_time(release_time(self.music_urls()))

        def fn(m):
            st = m["acquisition"].setdefault("steps", {})
            (st.setdefault("list", {}).setdefault("detail", {}) or {}).update(
                {"links": len(seen), "release": rel, "dropped_before_release": n})
            m["acquisition"]["release_filter_applied_at"] = now()
        self.update(fn)
        return {"dropped_before_release": n, "release": rel, "list_rows": n_rows, "derive": derive, "comments_md": md}

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

    def comment_pages(self) -> list:
        """コメントを取る楽曲ページごとの (URL, プール, 要約, 差し替えの記録)。コメントは楽曲ページのグリッドから動画へ移って取るので、
        動画を見つけたページから取る。楽曲ページが1つなら前と同じファイル"""
        urls = self.music_urls()
        pool = self.p("derived", "pool.tsv")
        if len(urls) <= 1:
            return [(urls[0], pool, self.p("fetch_log", "comments_summary.json"), self.p("fetch_log", "substitutions.tsv"))]
        src = self.link_sources()
        lines = [ln for ln in pool.read_text(encoding="utf-8").splitlines() if ln.strip()]
        head, body = lines[0], lines[1:]
        vi = head.split("\t").index("video_id")
        out = []
        for k, url in enumerate(urls, 1):
            mine = [r for r in body if src.get(r.split("\t")[vi], 1) == k]
            if not mine:
                continue
            pk = self.p("derived", f"pool_p{k}.tsv")
            pk.write_text("\n".join([head, *mine]) + "\n", encoding="utf-8")
            sfx = "" if k == 1 else f"_p{k}"   # 1ページ目は前と同じ名前（進み具合の表示がそのまま読める）
            out.append((url, pk, self.p("fetch_log", f"comments_summary{sfx}.json"),
                        self.p("fetch_log", f"substitutions{sfx}.tsv")))
        return out

    def step_comments(self):
        s = self.settings()
        ensure_chrome(s["chrome_port"], self.log)
        from acquire import spatest
        spent = float(((self.meta.get("acquisition") or {}).get("steps", {}).get("comments") or {}).get("active_hours") or 0)
        deadline = float(s.get("comment_deadline_hours") or 0)
        retries = 0
        blocked = False
        pages = self.comment_pages()
        for k, (url, pool, summary, subs) in enumerate(pages, 1):
            if len(pages) > 1:
                self.log(f"    楽曲ページ {k}/{len(pages)} のグリッドから取ります（プール {sum(1 for _ in open(pool, encoding='utf-8')) - 1}本、{url}）")
            while True:
                left = (deadline - spent) if deadline else 0.0      # 0 は spatest で「打ち切らない」
                if deadline and left <= 0.02:
                    self.log("    時間の上限に達しています")
                    break
                args = ["--port", str(s["chrome_port"]), "--music-url", url,
                        "--pool", str(pool),
                        "--candidates", str(self.p("derived", "llm_input", "records.jsonl")),
                        "--subs-out", str(subs),
                        "--resume", "--collect-scrolls", str(s["collect_scrolls"]),
                        "--reply-policy", "targets", "--reply-top", str(s["reply_top"]),
                        "--reply-questions", str(s["reply_questions"]), "--reply-author", str(s["reply_author"]),
                        "--cap", "40", "--min-comments", "20",
                        "--calls-per-min", str(s["calls_per_min"]), "--max-calls-per-min", str(s["max_calls_per_min"]),
                        "--interval", str(s["interval"]), "--jitter", "0.5", "--deadline-hours", f"{left:.3f}",
                        "--out", str(self.p("raw", "comments.jsonl")), "--log", str(self.p("fetch_log", "comments.log")),
                        "--summary", str(summary)]
                args += [f"--{k.replace('_', '-')}" for k in ("stop_on_no_more", "prescroll", "only_open_video",
                                                               "remount_on_stall") if s.get(k)]
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
            if blocked:   # 空けても塞がれたまま: ほかの楽曲ページへ進まない
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
        summs = [read_json(p, {}) or {} for p in summary_files(self.dir)]   # 楽曲ページごとの要約を合わせる
        n_pool = sum(1 for _ in open(self.p("derived", "pool.tsv"), encoding="utf-8")) - 1
        res = {"videos_ok": len(ok), "pool": n_pool,
               "comments": sum(len(r.get("comments") or []) for r in ok),
               "replies": sum(len(r.get("reply_comments") or []) for r in ok),
               "mixed_comments": mixed, "stuck_at_20": capped20,
               "substituted": sum(1 for summ in summs for s in (summ.get("substitutions") or []) if s.get("substitute")),
               "unreachable": sum(len(summ.get("missing") or []) for summ in summs),
               "not_fetched_time": sum(len(summ.get("not_fetched_time") or []) for summ in summs),
               "blocked": blocked, "spa_no": sum(1 for r in ok if r.get("spa") is False)}
        if len(self.music_urls()) > 1:
            src = self.link_sources()
            by = {}
            for r in ok:
                k = src.get(str(r["video_id"]), 1)
                by[k] = by.get(k, 0) + 1
            res["videos_ok_by_page"] = {str(k): v for k, v in sorted(by.items())}
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


def id_time(i):
    """TikTok の番号（動画・楽曲ページ。19桁）に入っている、作られた時刻（1970年からの秒）。上の32ビット。読めなければ None"""
    try:
        t = int(str(i).strip()) >> 32
    except (TypeError, ValueError):
        return None
    return t if t > 1_400_000_000 else None   # 2014年より前は番号の形が違う（読み違えを防ぐ）


def iso_time(t) -> str:
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else ""


def release_time(urls: list):
    """曲の公開の時刻: 合わせて取る楽曲ページのうち、一番早く作られたもの（URL の末尾の番号から）"""
    ts = [id_time(u.rstrip("/").split("?")[0].rsplit("-", 1)[-1]) for u in urls or []]
    ts = [t for t in ts if t]
    return min(ts) if ts else None


def summary_files(d: Path) -> list:
    """コメント取得の要約（1ページ目は comments_summary.json、2ページ目からは comments_summary_p2.json …）"""
    return sorted((d / "fetch_log").glob("comments_summary*.json"))


def comments_ok(d: Path) -> int:
    """コメントが取れた動画の数（楽曲ページごとの要約を合わせる。要約がまだ無ければ原本の行数）"""
    n = 0
    for p in summary_files(d):
        n += sum(1 for r in (read_json(p, {}) or {}).get("rows", []) if r.get("status") == "ok")
    if not n and (d / "raw" / "comments.jsonl").exists():
        n = sum(1 for _ in open(d / "raw" / "comments.jsonl", encoding="utf-8"))
    return n


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
