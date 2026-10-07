"""
分析1つの取得の段（段2）: 楽曲 → 一覧 → 属性・サムネ → 集計と AI の入力 → 候補プール → コメント → コメント整形 → 知らせ。

docs/IMPLEMENTATION_HANDOVER.md 第5章、docs/BLUEPRINT.md 第5章。取得の係（acquire/worker.py）が呼ぶ。
入出力は全部、分析フォルダ（output/analyses/<分析ID>/）:
  raw/        ①原本        ledger.jsonl（台帳: 楽曲ページのグリッドで見た全動画）・grid_links.jsonl（そのうち詳しく読む動画）・
                           enriched.jsonl（動画ページの属性）・covers/・comments.jsonl
  fetch_log/  ②取得の記録  pipeline.log・comments.log・comments_summary.json・substitutions.tsv・notify.log（すべて日時つき）
  derived/    ③計算物      list.csv（13列。詳しく読んだ動画）・videos.jsonl・weekly.tsv（台帳の全動画）・llm_input/・pool.tsv・comments/
状態は analysis.json の "acquisition"。工程ごとに done を持つので、どこで止まっても続きから（各工程も再開できる）。

TikTok に触る工程（resolve・list・enrich・comments）は tiktok_lock で既存の CSV ジョブと直列。
CSV ジョブが待っていたら、区切り（属性は20本ごと、コメントは1本ごと）でロックを譲る。
"""
import collections
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
    # 返信を各1件にして 3.1分・6.1分の見込み（docs/IMPLEMENTATION_LOG.md D20）。
    # 2026-10-05 に返信を開くのをやめ、本人だけの動画を40件にしたが、見込みは変えない（本数を今のままにして、浮いた分は時間の短縮に回す）
    "min_per_video": 3.1, "min_per_key_video": 6.1,
    # コメントの取り方（analysis/pool.py の --comment-plan）。2026-10-06 ユーザー採用: "page"＝必ず入れる動画（起点・大型ヒット・認証・本人・公式など）は
    # 1本1ページ（最大20件）、週ごとに配った動画はコメントを取らない（ラベルを付ける動画としては残す）。"full"＝前の取り方（120件・40件、週ごとも取る）。
    # 選ぶ動画の本数は上の min_per_* の見込みで今までどおり配る（ラベルを付ける動画を変えないため）
    "comment_plan": "page",
    # "page" の動画1本の見込み（分）。2026-10-06 の実走（シルエットの79本、毎分3回・60秒に4回、docs/COMMENT_SPEED.md 第7章）:
    # 直す前の判定で 0.64分（2ページ以上取った21本を含む）、余分な要求を引くと 0.52分、直した判定の21本は 0.36分 → 少し余裕を見て 0.55
    "min_per_page_video": 0.55,
    # 返信欄を開くコメント（返信数の多い順・質問形・投稿者本人）。2026-10-05 から開かない（0）。
    # 開いて取った返信は本番レポート2本で引用0件、開かない写しで回し直してもレポートの点は元の版のぶれの内側（docs/COMMENT_STRATEGY_HANDOVER.md 第9章）。
    # 本体と一緒に届く返信（reply_comment）は今までどおり残る
    "reply_top": 0, "reply_questions": 0, "reply_author": 0,
    "pool_budget": 0,            # プールの本数を直接指定（0なら時間から）
    # 週ごとに配る動画の再生の下限（必ず入れる動画には掛けない）。分析ごとに道具（start_analysis の min_plays）で変えられる。analysis/pool.py の MIN_PLAYS_WEEKLY
    "min_plays_weekly": 100000,
    # 一覧＝台帳（2026-10-07 ユーザー採用。docs/LIST_CUT.md 第6章）: 楽曲ページを開き直さずに続けてスクロールし、グリッドが読み込む
    # 一覧データ（/api/music/item_list/）を全部拾う。再生数・投稿日時・投稿者・説明文・画面の文字まで入っている（第5章）。
    # 1回で約29本、60回で約1,700本・約4分（前の 30回×3セットは開き直すたびに同じ上から読み直し、約870本で頭打ちだった）。
    # 音源が多いときは合計 ledger_scrolls_total 回を音源の数で割る（最低 ledger_scrolls_min 回＝約300本）。増えなくなったら早めに止める
    "ledger_scrolls": 60, "ledger_scrolls_total": 300, "ledger_scrolls_min": 10, "list_stall": 4,
    # 台帳から詳しく読む（1本ずつ開いて属性を取る）動画（choose_to_read）。1音源の枠は read_normal を音源の数で割った本数、最低 read_floor。
    # ただし合計 read_cap が絶対の上限（音源100なら1音源10本）。枠の中は、再生 read_must_plays 以上を全部（枠を超えても）→
    # 本人・最初期（各 read_role 本、枠の5分の1まで）・週の一番（再生 read_week_plays 以上。枠の5分の1まで）→ 残りを再生の多い順。
    # 読まない動画は台帳（raw/ledger.jsonl）に残り、週ごとの本数・音源ごとの本数・画面の文字の出現数はそこから数える。
    # 2026-10-07 ユーザー「再生数が少ないものはリストに入れなくていい。この時期に何本投稿されたかだけ取っといて」
    # 「最低50本は読む。通常は500本が上限、音源の数で割る。絶対の上限として1000本」「50万再生以上は上限突破してでも全部読む」。
    # read_select 0 は前と同じ（台帳を全部読む）
    "read_select": 1, "read_normal": 500, "read_floor": 50, "read_cap": 1000, "read_must_plays": 500000,
    "read_role": 10, "read_week_plays": 10000,
    "enrich_sleep": 2.0,
    # 同じ曲のファンの音源（「オリジナル楽曲 - 〇〇」）も探し、よく使われていれば合わせて取る（Run.add_fan_sounds。0 で探さない。
    # 利用者が楽曲ページを渡した分析は 0）。2026-10-06 きゃわぽっぴんどぅー: 2番の「血液型とかMBTIとか」で自己紹介する波が、
    # ファンが上げた12秒の音源（39K。公式の2つは 31.1K・17.7K）に乗っていて、公式の音源だけを取ったレポートから丸ごと抜けた。
    # 読む discover のページは曲名と表記ゆれの語 fan_words 個、1ページ fan_videos 本。見つけた音源は全部 UGC 数を読み、
    # 主の UGC の2割以上なら全部足す（数の上限は付けない。2026-10-06 ユーザー「こういう変な制限はしなくていいよ」。
    # きゃわの実走では 39K・8.5K（フル版）・6.9K の3つ。詳しく読む本数は read_* で音源の数に合わせて割るので、足しても台帳の約4分が延びるだけ）
    # 2026-10-07: discover のページをスクロールして一覧データ（/api/seo/kap/video_list/）から音源を数える（1語 fan_scrolls 回で約60本。
    # 動画は開かない）。表記ゆれの語は上限なし（fan_words 0。2本以上に出た語を全部）。同じ曲かは候補の音源ごとに動画を1〜2本開いて確かめる。
    # 前（1語16本・3語まで、動画を全部開く）は、きゃわの 10/7 の取得で 32本を読んで쿠레아（自己紹介の波の音源）が1本も出ず、取りこぼした
    # （쿠레아の動画は説明文に曲名を書くのが15%だけで、#mbti が多い。discover の並びに出るのは1割ほど）
    "fan_sounds": 1, "fan_words": 0, "fan_videos": 16, "fan_scrolls": 10,
    "collect_scrolls": 150,      # コメント: グリッドでプールを探すスクロールの上限
    # 要求の間隔（平均と60秒の上限）。2026-10-05 にユーザーの判断で 1.8回/分・60秒に2回 → 3回/分・60秒に4回へ上げた。
    # 実走: 2.0回/分で5時間・123本、3回/分で1時間・40本、どちらも空応答・4xx 0。同じ動画の取得が68%に（docs/COMMENT_SPEED.md 第5章）。
    # 長時間で 2.5回/分を超える実績はまだ無いので、止まったら fallback_* に落として続きを取る（step_comments）
    "calls_per_min": 3.0, "max_calls_per_min": 4.0, "interval": 15.0,
    "fallback_calls_per_min": 1.8, "fallback_max_calls_per_min": 2.0,
    # コメント取得の速さ（acquire/spatest.py 冒頭の 2026-10-05 の説明）。
    # 試験（きゃわぽっぴんどぅー20本、直した版）で 84.5分 → 61.6分、20本とも取得・混入0・頭打ち0（docs/COMMENT_SPEED.md）
    "stop_on_no_more": True, "prescroll": True, "only_open_video": True, "remount_on_stall": True,
    "chrome_port": "9222",
    "blocked_wait_min": 90,      # ブロックを検知したら空ける時間（COMMENT_ACQUISITION_HANDOVER 2-6: 約1.5時間で回復）
    "blocked_retries": 2,
}


# ファンの音源は、主の楽曲ページの UGC のこの割合以上なら合わせて取る（同じ曲の公式のページと同じ基準。proto_runner.JOIN_RATIO）
FAN_JOIN_RATIO = 0.2


class StepError(Exception):
    """工程を失敗として止める（理由は利用者向けの短い日本語）"""


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # 無い・フォルダでない（.DS_Store/analysis.json）・壊れている
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
                values = {"status": "running", "started_at": st.get("started_at") or now()}
                if st.get("started_at"):   # 続きから: 始め直した時刻（残りの見込みはここから測る。acquire/launch.py）
                    values["resumed_at"] = now()
                self._mark(step, values)
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
            try:   # 探せなくても取得は止めない（受け付けで決めたページで進める）
                fan = self.add_fan_sounds()
            except Exception as e:
                self.log(f"    同じ曲のファンの音源を探せませんでした（受け付けで決めたページで進めます）: {type(e).__name__}: {e}")
                fan = {"error": type(e).__name__}
            return {"music_url": m["music_url"], "how": "given", "fan_sounds": fan}
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

    def add_fan_sounds(self) -> dict:
        """同じ曲のファンの音源（search_fan_sounds）のうち、主の楽曲ページの UGC の FAN_JOIN_RATIO 以上のものを、取る楽曲ページに足す。
        足したページは raw/music_pages.json に kind="fan"（音源の長さ・discover で出た回数つき）で残し、曲の公開の時刻からは外す（release_urls）。
        探した記録は raw/sound_search.json。設定 fan_sounds=0（利用者が楽曲ページを渡した分析）と、試験（UGC_COLLECTOR_NO_INSPECT）では探さない"""
        s = self.settings()
        song = ((self.meta.get("song") or {}).get("title") or "").strip()
        if not int(s.get("fan_sounds") or 0) or not song or os.environ.get("UGC_COLLECTOR_NO_INSPECT"):
            return {"searched": False}
        urls = self.music_urls()
        ids = {music_id_of(u) for u in urls}
        before = read_json(self.p("raw", "music_pages.json"), []) or []
        main_n = next((p.get("video_count") for p in before if isinstance(p, dict) and p.get("url") == urls[0]), None)
        artist = ((self.meta.get("song") or {}).get("artist") or "").strip()
        self.lock("同じ曲のファンの音源を探す")
        import scraper
        d = scraper.create_headless_driver()
        hook_discover(d)
        checked, added = [], []
        try:
            def music_of(u):
                m = {}
                for wait in (1.5, 4):   # 動画のデータはページの最初の HTML に入っている。読めなければ1回だけ待ち直す
                    try:
                        m = read_video_music(d, u, wait=wait)
                    except Exception as e:
                        m = {"error": type(e).__name__}
                    if m.get("id"):
                        break
                return m

            def page_links():   # 曲名の discover に公式の音源の動画が無かったとき、主の楽曲ページの動画から曲の番号を知る
                d.get(urls[0])
                time.sleep(scraper.PAGE_LOAD_TIME)
                return list(dict.fromkeys(h.split("?")[0] for h in (d.execute_script(
                    "return Array.from(document.querySelectorAll('a[href*=\"/video/\"]')).map(a => a.href);") or [])))

            found = search_fan_sounds(song, ids, lambda w: discover_items(d, w, int(s["fan_scrolls"]), int(s["fan_videos"])),
                                      music_of, int(s["fan_words"]) or None, page_links)
            self.log(f"    discover {len(found['words'])}ページ（{'・'.join(found['words'])}）で動画 {found['videos']}本の音源を読み"
                     f"（開いた動画 {found.get('opened', found['videos'])}本）、"
                     f"同じ曲（TikTok の照合 {'・'.join(found['meta_song_ids']) or 'なし'}）のほかの音源 {len(found['candidates'])}個")
            cands = found["candidates"]
            if cands and not main_n:
                d.get(urls[0])
                time.sleep(scraper.PAGE_LOAD_TIME)
                main_n = read_music_page(d).get("video_count")
            infos = {}
            for c in cands:
                url = music_url_of(c["id"], c.get("title"))
                d.get(url)
                time.sleep(scraper.PAGE_LOAD_TIME)
                infos[url] = info = read_music_page(d)
                checked.append({**c, "url": url, "video_count": info.get("video_count"),
                                "video_count_text": info.get("video_count_text"), "unavailable": bool(info.get("unavailable")),
                                "joined": False})
                self.log(f"    『{c.get('title')}』（{c.get('duration')}秒・discover で {c['hits']}回）: UGC {info.get('video_count_text') or '読めず'}")
        finally:
            try:
                d.quit()
            except Exception:
                pass
        # 主の UGC の2割以上を全部、大きい順に（ページの並び＝音源A・B・C の順になる）
        ok = [c for c in checked if main_n and c["video_count"] and not c["unavailable"]
              and c["video_count"] >= FAN_JOIN_RATIO * main_n]
        for c in sorted(ok, key=lambda c: -c["video_count"]):
            c["joined"] = True
            info = infos[c["url"]]
            kind = "official" if fits_song(song, artist, info.get("title") or c.get("title"),
                                           info.get("creator") or c.get("author")) else "fan"
            added.append({**{k: v for k, v in info.items() if v is not None}, "url": c["url"], "kind": kind,
                          "duration": c.get("duration"), "hits": c["hits"], "at": now(),
                          "how": "取得のはじめに、discover の人気の動画の音源から見つけた同じ曲の音源"})
        self.log(f"    主の楽曲ページの UGC {main_n or '読めず'} の{int(FAN_JOIN_RATIO * 100)}%以上を合わせて取る: " +
                 ("、".join(f"『{p.get('title')}』（{p.get('video_count_text')}）" for p in added) or "なし"))
        write_json(self.p("raw", "sound_search.json"), {**found, "main_video_count": main_n, "checked": checked,
                                                         "added": [p["url"] for p in added], "at": now()})
        if added:
            def fn(m):
                have = music_urls_of(m)
                m["music_urls"] = have + [p["url"] for p in added if p["url"] not in have]
                m["fan_music_urls"] = sorted(set(m.get("fan_music_urls") or []) |
                                             {p["url"] for p in added if p["kind"] == "fan"})
            self.update(fn)
            known = {p.get("url") for p in before if isinstance(p, dict)}
            write_json(self.p("raw", "music_pages.json"), before + [p for p in added if p["url"] not in known])
        return {"searched": True, "words": found["words"], "videos": found["videos"],
                "candidates": len(found["candidates"]), "added": [p["url"] for p in added]}

    def music_urls(self) -> list:
        """取得する楽曲ページ（1つ目が主）。同じ曲の配信版・sped up 版などで複数あるときは、全部から一覧を集めて合わせる
        （2026-10-04 ユーザー「2つ以上の楽曲ページを参照させたい場合もある」）"""
        return music_urls_of(self.meta)

    def release_urls(self) -> list:
        """曲の公開の時刻を決める楽曲ページ: 取るページからファンの音源（add_fan_sounds で足したもの）を除く
        （ファンの音源は曲の公開より前に作られることがある）"""
        fan = set(self.meta.get("fan_music_urls") or [])
        return [u for u in self.music_urls() if u not in fan] or self.music_urls()

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
        """一覧の段: 楽曲ページを開き直さずに続けてスクロールし（未ログイン・ヘッドレス・END キー）、グリッドが読み込む一覧データを全部拾って
        台帳（raw/ledger.jsonl）を作る。そこから詳しく読む動画を選んで raw/grid_links.jsonl に書く（choose_to_read。2026-10-07）。
        楽曲ページが複数なら、ページごとに集めて重ねない（どのページで見つけたかを source に残す）。
        一覧データが拾えなかったページ（ブラウザが対応していない・TikTok の形が変わった）は、前と同じく台帳を全部読む"""
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
        scrolls = ledger_scrolls(len(urls), s)
        seen, order = {}, 0      # 動画 ID → 台帳の行（ページの順・グリッドの順）
        pages = []
        driver = scraper.create_headless_driver()
        hooked = hook_item_list(driver)
        try:
            for k, url in enumerate(urls, 1):
                tag = f"{k}ページ目 " if len(urls) > 1 else ""
                driver.get(url)
                time.sleep(scraper.PAGE_LOAD_TIME)
                # 楽曲ページの UGC 数（「131.9K 動画」）。記事は全体の UGC 数で語るため（2026-10-03 ユーザー）
                page_info = read_music_page(driver)
                self.log(f"    楽曲ページ{('（' + str(k) + '）') if len(urls) > 1 else ''}: {page_info}")
                last, stall, scroll_i = -1, 0, 0
                for scroll_i in range(scrolls):
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
                api = {}
                if hooked:
                    try:
                        for it in driver.execute_script(LEDGER_READ_JS) or []:
                            if isinstance(it, dict) and it.get("id"):
                                api.setdefault(str(it["id"]), it)
                    except Exception as e:
                        self.log(f"    一覧データを読めませんでした（このページは台帳を全部読みます）: {type(e).__name__}: {e}")
                n0, n_api = len(seen), 0
                for h in hrefs:
                    h = h.split("?")[0]
                    vid = h.rstrip("/").rsplit("/", 1)[-1]
                    if vid not in seen:
                        seen[vid] = ledger_row(vid, h, k, order, api.get(vid))
                        n_api += vid in api
                        order += 1
                for vid, it in api.items():   # グリッドの表示に出る前に止めた分（一覧データには届いている）
                    if vid not in seen:
                        seen[vid] = ledger_row(vid, None, k, order, it)
                        n_api += 1
                        order += 1
                n_grid = len({h.split("?")[0] for h in hrefs})   # 1本の動画にリンクが2つずつある
                self.log(f"    一覧 {tag}: グリッド {n_grid}本・一覧データ {len(api)}本 / 新規 {len(seen) - n0}（うち再生数あり {n_api}）"
                         f"（スクロール{scroll_i + 1}回）")
                info = {**(before.get(url) or {}), **{key: v for key, v in (page_info or {}).items() if v is not None}}
                info.pop("how", None)
                pages.append({**info, "url": url, "page": k, "ledger": len(seen) - n0, "at": now()})
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        if not seen:
            if pages and all(p.get("unavailable") for p in pages):
                # 日本の地域で使えない楽曲ページ（read_music_page がその文を見た）。取り直しても同じなので、別のページでのやり直し方を書く
                # （collector_app/jobs.retriable はこの文で取り直さない）
                raise StepError(f"{UNAVAILABLE_STOP}。ほかの楽曲ページの URL で"
                                f"『{self.meta.get('title') or self.id}の取得をやめて、このページでやり直して』と頼んでください")
            raise StepError("楽曲ページから動画が1本も見つかりませんでした（URL が違うか、TikTok 側の表示制限）")
        for pg in pages:   # 楽曲ページが作られた日時（番号から）
            pg["created_at"] = iso_time(id_time(pg["url"].rstrip("/").rsplit("-", 1)[-1]))
        dropped = self.drop_before_release(seen, self.release_urls())
        if not seen:
            raise StepError("楽曲ページの動画が、全部曲の公開より前の日付でした（楽曲ページが違う可能性）")
        for k, pg in enumerate(pages, 1):
            pg["ledger"] = sum(1 for r in seen.values() if r["source"] == k)
        song = self.meta.get("song") or {}
        why = choose_to_read(list(seen.values()), urls, s, song.get("artist") or (pages[0].get("creator") or ""))
        for k, pg in enumerate(pages, 1):
            pg["links"] = sum(1 for v, r in seen.items() if r["source"] == k and v in why)
        write_json(self.p("raw", "music_pages.json"), pages)
        write_json(self.p("raw", "music_page.json"), pages[0])   # 主のページ（前からの形）
        with open(self.p("raw", "ledger.jsonl"), "w", encoding="utf-8") as f:
            for v, r in seen.items():
                f.write(json.dumps({**r, "read": why.get(v)}, ensure_ascii=False) + "\n")
        with open(out, "w", encoding="utf-8") as f:
            for v, r in seen.items():
                if v in why:
                    f.write(json.dumps({"url": r["url"], "video_id": v, "order": r["order"], "type": r["type"],
                                        "source": r["source"], "plays": r.get("plays"), "why": why[v],
                                        "collected_at": r["collected_at"]}, ensure_ascii=False) + "\n")
        self._write_links_csv()
        reasons = collections.Counter(why.values())
        q = read_quota(len(urls), s)
        self.log(f"    台帳 {len(seen)}本から、詳しく読む動画 {len(why)}本（1音源の枠 {q}本。"
                 + "・".join(f"{k} {n}" for k, n in reasons.most_common()) + "）")
        counts = [p.get("video_count") for p in pages]
        res = {"links": len(why), "ledger": len(seen), "quota": q, "reasons": dict(reasons),
               "ugc_total": sum(c for c in counts if c) or None,
               "release": iso_time(release_time(self.release_urls())), "dropped_before_release": dropped}
        if len(pages) > 1:
            res["pages"] = [{"url": p["url"], "links": p["links"], "ledger": p["ledger"], "ugc": p.get("video_count")}
                            for p in pages]
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
        n = self.drop_before_release(seen, self.release_urls())
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
        rel = iso_time(release_time(self.release_urls()))

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
        led = self.p("raw", "ledger.jsonl")   # 台帳（2026-10-07〜）。週ごとの本数は台帳の全動画で数える
        out1 = self._script("analysis/prep_sample.py", self.p("derived", "list.csv"), self.p("derived"),
                            *(["--ledger", led] if led.exists() else []))
        out2 = self._script("analysis/build_llm_input.py", self.p("derived"), "--enriched", self.p("raw", "enriched.jsonl"),
                            "--out", self.p("derived", "llm_input"))
        return {"prep_sample": out1.splitlines()[-1:], "build_llm_input": out2.splitlines()[-1:]}

    def step_pool(self):
        s = self.settings()
        out = self.p("derived", "pool.tsv")
        if out.exists():  # 再開: プールは一度決めたら変えない（取得済みと食い違わないように）
            return {"n_pool": len(comment_rows(out)), "resumed": True}
        args = ["analysis/pool.py", self.p("derived", "videos.jsonl"), self.p("raw", "enriched.jsonl"), out,
                "--hours", s["comment_hours"], "--min-per-video", s["min_per_video"],
                "--min-per-key-video", s["min_per_key_video"], "--min-plays-weekly", int(s["min_plays_weekly"]),
                "--comment-plan", s["comment_plan"], "--records", self.p("derived", "llm_input", "records.jsonl")]
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
        ci = head.split("\t").index("cap")
        label_only = [r for r in body if int(r.split("\t")[ci] or 0) <= 0]
        body = [r for r in body if int(r.split("\t")[ci] or 0) > 0]   # コメントを取らない動画（cap 0）では楽曲ページを開かない
        out = []
        for k, url in enumerate(urls, 1):
            mine = [r for r in body if src.get(r.split("\t")[vi], 1) == k]
            if not mine:
                continue
            pk = self.p("derived", f"pool_p{k}.tsv")
            # cap 0 の行は全ページのプールに残す（spatest は開かず、差し替え先にもしない。抜くと週ごとの動画を差し替え先に選ぶ）
            pk.write_text("\n".join([head, *mine, *label_only]) + "\n", encoding="utf-8")
            sfx = "" if k == 1 else f"_p{k}"   # 1ページ目は前と同じ名前（進み具合の表示がそのまま読める）
            out.append((url, pk, self.p("fetch_log", f"comments_summary{sfx}.json"),
                        self.p("fetch_log", f"substitutions{sfx}.tsv")))
        return out

    def pool_left(self, pool: Path) -> int:
        """そのプールで、コメントを取る動画のうちまだ取っていないものの数（取れた動画と、差し替えで取った動画の元を除く。
        spatest の pool_targets と同じ数え方）"""
        done = set()
        p = self.p("raw", "comments.jsonl")
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or r.get("status") != "ok":
                        continue
                    done.add(str(r.get("video_id")))
                    reason = str(r.get("pool_reason") or "")
                    if reason.startswith("substitute_for:"):
                        done.add(reason.split(":", 1)[1])
        return sum(1 for r in comment_rows(pool) if r["video_id"] not in done)

    def step_comments(self):
        s = self.settings()
        cm = (self.meta.get("acquisition") or {}).get("steps", {}).get("comments") or {}
        # ブロックのあと空けている途中で係が止められていた（ふたを閉じた・アプリを閉じた）: 落とした速さと空ける時刻を引き継ぐ
        if cm.get("slowed_to"):
            s.update({k: cm["slowed_to"][k] for k in ("calls_per_min", "max_calls_per_min") if k in cm["slowed_to"]})
        try:
            wait_left = (datetime.datetime.fromisoformat(cm["blocked_until"]) - datetime.datetime.now().astimezone()).total_seconds()
        except (KeyError, TypeError, ValueError):
            wait_left = 0
        if wait_left > 0:
            self.log(f"    ブロックのあと空けている途中で止まっていたので、あと{int(wait_left // 60) + 1}分空けてから、"
                     f"要求の間隔を平均{s['calls_per_min']}回/分にして続きを取ります")
            time.sleep(wait_left)
        ensure_chrome(s["chrome_port"], self.log)
        from acquire import spatest
        spent = float(cm.get("active_hours") or 0)
        deadline = float(s.get("comment_deadline_hours") or 0)
        retries = 0
        blocked = False
        pages = self.comment_pages()
        for k, (url, pool, summary, subs) in enumerate(pages, 1):
            if not self.pool_left(pool):
                # 取り終えたページ（続きから取るとき）は spatest に渡さない。渡すと、探す動画が0本でもグリッドを上限まで（約5分）送り、
                # そのページの要約も空で上書きする
                self.log(f"    楽曲ページ {k}/{len(pages)} のコメントは取り終えているので飛ばします（{url}）" if len(pages) > 1
                         else "    コメントを取る動画は取り終えています")
                continue
            if len(pages) > 1:
                self.log(f"    楽曲ページ {k}/{len(pages)} のグリッドから取ります（コメントを取る動画 {len(comment_rows(pool))}本、{url}）")
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
                        "--cap", "40", "--min-comments", "20", "--subs-cap", str(subs_cap(s)),
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
                    # 止まったら、元の速さ（実績の長い 1.8回/分・60秒に2回）に落として続きを取る（2026-10-05 に3回/分へ上げたときの安全網）
                    s["calls_per_min"], s["max_calls_per_min"] = s["fallback_calls_per_min"], s["fallback_max_calls_per_min"]
                    # 空けている途中で係が止められても、続きから始めたときに空けと落とした速さを引き継ぐ（step_comments の頭で読む）
                    until = datetime.datetime.now().astimezone() + datetime.timedelta(minutes=float(s["blocked_wait_min"]))
                    self._mark("comments", {"blocked_until": until.isoformat(timespec="seconds"), "slowed_to": {
                        "calls_per_min": s["calls_per_min"], "max_calls_per_min": s["max_calls_per_min"]}})
                    self.log(f"    ブロックを検知したので {s['blocked_wait_min']}分空けてから、要求の間隔を平均{s['calls_per_min']}回/分に落として"
                             f"続きを取ります（{retries}回目）")
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
                       and (r.get("top_list") or 0) >= 20 and int(r.get("cap") or 40) > 20)   # 上限20件（1本1ページ）で止めたものは除く
        summs = [read_json(p, {}) or {} for p in summary_files(self.dir)]   # 楽曲ページごとの要約を合わせる
        n_pool = len(comment_rows(self.p("derived", "pool.tsv")))
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
# 日本の地域で使えない楽曲ページ（2026-10-06 ビビデバ。ログインなしで開くと「この楽曲はご利用になれません。このサウンドはお住いの国または地域では
# ご利用になれません」と出て、題・作者・UGC 数も動画も出ない。一覧の段は「動画が1本も見つかりませんでした」で止まり、取り直しても同じ）。
# 英語の表示の「This sound isn't available in your country or region」なども
UNAVAILABLE_RE = re.compile(r"ご利用になれません|ご利用いただけません|isn['’]t available in your|not available in your|"
                            r"unavailable in your (?:country|region)", re.I)
PAGE_TEXT_JS = "return document.body ? (document.body.innerText || '').slice(0, 20000) : '';"
# 使えない楽曲ページで一覧の段が止まったときの文の頭（collector_app/jobs.retriable・proto_runner._acq_view もこの文で見分ける）
UNAVAILABLE_STOP = "この楽曲ページは日本の地域では使えません（TikTok の地域の制限）"


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


def music_urls_of(m: dict) -> list:
    """analysis.json の中身から、取得する楽曲ページ（1つ目が主。Run.music_urls と同じ。見込み（acquire/launch.py）にも使う）"""
    urls = [u for u in (m.get("music_urls") or []) if u]
    if m.get("music_url") and m["music_url"] not in urls:
        urls.insert(0, m["music_url"])
    return urls


# --- 台帳（一覧の段。2026-10-07。docs/LIST_CUT.md 第5・6章） ---
# 楽曲ページのグリッドは、スクロールのたびに /api/music/item_list/ を読む。その返事に、並ぶ動画の再生数・投稿日時・投稿者・説明文・
# 画面の文字まで入っている（ログインなしで、7ページ・9,846本で確かめた。来ないのは関連する検索の語・投稿の地域・TikTok のラベルだけ）。
# ページを開く前に fetch と XMLHttpRequest に仕掛けておき、届いた返事から要る項目だけを取っておく（全部だと1ページ約40MB）
LEDGER_HOOK_JS = r"""
(() => {
  if (window.__ledger) return;
  window.__ledger = [];
  const want = u => typeof u === 'string' && /\/api\/music\/item_list/.test(u);
  const pick = (o, ks) => { const r = {}; if (o) for (const k of ks) r[k] = o[k]; return r; };
  const take = t => {
    try {
      const j = JSON.parse(t);
      for (const it of (j.itemList || [])) {
        window.__ledger.push({
          id: it.id, createTime: it.createTime, desc: it.desc, isAd: it.isAd, photo: !!it.imagePost,
          stats: it.statsV2 || it.stats, author: pick(it.author, ['uniqueId', 'nickname', 'verified', 'signature']),
          authorStats: pick(it.authorStatsV2 || it.authorStats, ['followerCount', 'videoCount']),
          music: pick(it.music, ['id', 'title', 'authorName', 'original', 'duration']),
          duration: (it.video || {}).duration,
          hashtags: (it.textExtra || []).map(x => x.hashtagName).filter(x => x),
          challenges: (it.challenges || []).map(c => c.title).filter(x => x),
          stickers: (it.stickersOnItem || []).flatMap(s => s.stickerText || []),
        });
      }
    } catch (e) {}
  };
  const of = window.fetch;
  window.fetch = async function(input, init) {
    const u = typeof input === 'string' ? input : (input && input.url);
    const r = await of.apply(this, arguments);
    if (want(String(u))) { try { take(await r.clone().text()); } catch (e) {} }
    return r;
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__u = String(u); return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    if (want(this.__u)) this.addEventListener('load', () => { try { take(this.responseText); } catch (e) {} });
    return os.apply(this, arguments);
  };
})();
"""
LEDGER_READ_JS = "return window.__ledger || [];"


def hook_item_list(driver) -> bool:
    """これから開くページで、グリッドの一覧データを拾う仕掛けを入れる。入れられなければ False（台帳は再生数なしになり、全部読む）"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LEDGER_HOOK_JS})
        return True
    except Exception:
        return False


def _int(x):
    try:
        return int(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def ledger_row(vid: str, url, source: int, order: int, it=None) -> dict:
    """台帳の1行。it はグリッドの一覧データ（LEDGER_HOOK_JS で取っておいた形）。無ければ番号から投稿日時だけ。
    create_time は属性（enriched.jsonl）と同じ UTC の「YYYY-MM-DD HH:MM:SS」"""
    it = it or {}
    a, st, mu = it.get("author") or {}, it.get("stats") or {}, it.get("music") or {}
    t = _int(it.get("createTime")) or id_time(vid)
    kind = "Photo" if it.get("photo") or "/photo/" in str(url or "") else "Video"
    if not url:
        url = f"https://www.tiktok.com/@{a.get('uniqueId') or 'user'}/{kind.lower()}/{vid}"
    row = {"video_id": vid, "url": url, "type": kind, "source": source, "order": order, "collected_at": now(),
           "create_time": datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else None,
           "api": bool(it)}
    if it:
        row.update({"plays": _int(st.get("playCount")), "likes": _int(st.get("diggCount")),
                    "comments": _int(st.get("commentCount")), "shares": _int(st.get("shareCount")),
                    "saves": _int(st.get("collectCount")), "desc": it.get("desc") or "",
                    "hashtags": it.get("hashtags") or [], "challenges": it.get("challenges") or [],
                    "sticker_texts": it.get("stickers") or [], "duration": it.get("duration"),
                    "music_id": str(mu.get("id")) if mu.get("id") else None, "is_ad": bool(it.get("isAd")),
                    "author": {"unique_id": a.get("uniqueId"), "nickname": a.get("nickname"), "verified": bool(a.get("verified")),
                               "follower_count": _int((it.get("authorStats") or {}).get("followerCount"))}})
    return row


def ledger_scrolls(n_sounds: int, s: dict) -> int:
    """1音源あたりのスクロールの回数: 普段は ledger_scrolls。合計が ledger_scrolls_total を超えるなら、それを音源の数で割る（最低 ledger_scrolls_min）"""
    k = max(1, n_sounds)
    per = int(s["ledger_scrolls"])
    if per * k > int(s["ledger_scrolls_total"]):
        per = max(int(s["ledger_scrolls_min"]), int(s["ledger_scrolls_total"]) // k)
    return per


def read_quota(n_sounds: int, s: dict) -> int:
    """1音源あたりに詳しく読む本数（枠）: read_normal を音源の数で割る。最低 read_floor。ただし合計が read_cap を超えるなら read_cap を割る
    （1音源 500・2音源 250・5音源 100・10〜20音源 50・50音源 20・100音源 10。2026-10-07 ユーザー）"""
    k = max(1, n_sounds)
    q = max(int(s["read_floor"]), int(s["read_normal"]) // k)
    return q if q * k <= int(s["read_cap"]) else max(1, int(s["read_cap"]) // k)


def _week(create_time: str):
    d = datetime.date.fromisoformat(str(create_time)[:10])
    return d.isocalendar()[:2]


def choose_to_read(rows: list, urls: list, s: dict, artist: str) -> dict:
    """台帳の行から、詳しく読む動画 {video_id: 理由}。音源（楽曲ページ。行の source）ごとに枠（read_quota）を持ち、その中で
      ① 本人（"本人"。コメント選びと同じ見分け方 pool.artist_accounts）と、音源ができた日より後の最初期（"最初期"）を、
         それぞれ read_role 本。ただし枠の5分の1まで
      ② 再生 read_must_plays 以上（"50万以上"）を全部。枠を超えてもよい（2026-10-07 ユーザー「上限突破してでも全部読む」）
      ③ 週の一番（"週の一番"。その週に再生 read_week_plays 以上の動画があれば、一番再生の多い1本）。枠の5分の1まで。
         枠で足りないときは投稿の多い週から
      ④ 残りを再生の多い順（"再生順"）で枠まで
    合計が read_cap を超えたら（①が多いとき）、再生順 → 週の一番 → 50万以上 の順に、再生の少ないものから外す（本人・最初期は残す）。
    read_select 0、または再生数の無いページ（一覧データが拾えなかった）は、そのページを全部読む（"全部"）"""
    by_page = collections.defaultdict(list)
    for r in rows:
        by_page[int(r["source"])].append(r)
    if not int(s.get("read_select", 1)):
        return {r["video_id"]: "全部" for r in rows}
    q = read_quota(len(urls), s)
    role = min(int(s["read_role"]), max(1, q // 5))
    week_cap = max(role, q // 5)
    must, week_floor = int(s["read_must_plays"]), int(s["read_week_plays"])
    import pool as pool_mod
    arts = pool_mod.artist_accounts(
        [{"video_id": r["video_id"]} for r in rows if r.get("author")],
        {r["video_id"]: r for r in rows if r.get("author")}, artist) if artist else set()
    out = {}
    for k, rs in by_page.items():
        if not any(r.get("plays") is not None for r in rs):
            out.update({r["video_id"]: "全部" for r in rs})
            continue
        P = lambda r: r.get("plays") or 0   # noqa: E731
        by_play = sorted(rs, key=lambda r: (-P(r), r["order"]))
        born = id_time(music_id_of(urls[k - 1])) if k - 1 < len(urls) else None
        born_s = (datetime.datetime.fromtimestamp(born, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                  if born else "")
        mine = {}

        def add(r, why):
            if r["video_id"] not in mine and r["video_id"] not in out:
                mine[r["video_id"]] = why
        # 本人・最初期を先に印を付ける（50万以上と重なっても、合計の上限で外さないように）
        for r in [r for r in by_play if (r.get("author") or {}).get("unique_id") in arts][:role]:
            add(r, "本人")
        timed = [r for r in rs if r.get("create_time") and r["create_time"] >= born_s]
        for r in sorted(timed, key=lambda r: (r["create_time"], r["order"]))[:role]:
            add(r, "最初期")
        for r in by_play:
            if P(r) >= must:
                add(r, "50万以上" if must == 500000 else f"{must:,}再生以上")
        weeks, in_time = collections.defaultdict(list), {r["video_id"] for r in timed}
        for r in by_play:
            if P(r) >= week_floor and r["video_id"] in in_time:
                weeks[_week(r["create_time"])].append(r)
        n_posts = collections.Counter(_week(r["create_time"]) for r in timed)
        picked = sorted(weeks, key=lambda w: (-n_posts[w], w))[:week_cap]
        for w in sorted(picked):
            add(weeks[w][0], "週の一番")
        for r in by_play:
            if len(mine) >= q:
                break
            add(r, "再生順")
        out.update(mine)
    cap = int(s["read_cap"])
    if len(out) > cap:
        plays = {r["video_id"]: r.get("plays") or 0 for r in rows}
        rank = {"再生順": 0, "週の一番": 1}
        drop = sorted((v for v, w in out.items() if w not in ("本人", "最初期")),
                      key=lambda v: (rank.get(out[v], 2), plays[v]))
        for v in drop[:len(out) - cap]:
            del out[v]
    return out


def summary_files(d: Path) -> list:
    """コメント取得の要約（1ページ目は comments_summary.json、2ページ目からは comments_summary_p2.json …）"""
    return sorted((d / "fetch_log").glob("comments_summary*.json"))


def comment_rows(pool: Path) -> list:
    """プールの表のうち、コメントを取る動画の行（cap が1以上。cap 0 はラベルを付けるだけの動画）"""
    with open(pool, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f, delimiter="\t") if int(r.get("cap") or 0) > 0]


def subs_cap(s: dict) -> int:
    """グリッドで見つからない動画の代わりに取る動画の上限（"page" なら1ページ）"""
    return 20 if s.get("comment_plan", "page") == "page" else 40


def minutes_per_comment_video(s: dict) -> float:
    """コメントを取る動画1本の見込み（分）。見込み時間（acquire/launch.py）に使う"""
    return float(s["min_per_page_video"]) if s.get("comment_plan", "page") == "page" else float(s["min_per_video"])


def comments_ok(d: Path) -> int:
    """コメントが取れた動画の数。原本（raw/comments.jsonl）の status=ok の動画を重ねずに数える
    （要約はその走行の分だけで、続きから取り直すたびに上書きされる。原本は走行をまたいで足されていく）。
    原本がまだ無ければ、楽曲ページごとの要約を合わせる"""
    raw = d / "raw" / "comments.jsonl"
    if raw.exists():
        ok = set()
        with open(raw, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("status") == "ok":
                    ok.add(str(r.get("video_id")))
        return len(ok)
    n = 0
    for p in summary_files(d):
        n += sum(1 for r in (read_json(p, {}) or {}).get("rows", []) if r.get("status") == "ok")
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


def page_unavailable(driver) -> bool:
    """楽曲ページの本文に、日本の地域では使えないという TikTok の文が出ているか（UNAVAILABLE_RE）。読めなければ False"""
    try:
        text = driver.execute_script(PAGE_TEXT_JS)
    except Exception:
        return False
    return isinstance(text, str) and bool(UNAVAILABLE_RE.search(text))


def read_music_page(driver, wait: float = 15) -> dict:
    """楽曲ページの題・作者・UGC 数（表示の文字と数）。ページを開いたあとで呼ぶ（要求は増えない）。
    表示は読み込みのあとから出てくるので、UGC 数が出るまで最大 wait 秒待つ。
    題も UGC 数も読めないうちは本文も見て、日本の地域では使えないという文が出ていれば unavailable=True にして待たずに返す
    （その文が出ているときだけ。読み込みがたまたま遅いだけのページは今までどおり wait 秒まで待つ）"""
    info = {}
    t0 = time.time()
    while True:
        try:
            info = driver.execute_script(MUSIC_PAGE_JS) or {}
        except Exception as e:
            return {"error": f"{type(e).__name__}"}
        if info.get("video_count_text"):
            break
        if not info.get("title") and page_unavailable(driver):
            info["unavailable"] = True   # 題・UGC 数はこのあとも出ない
            break
        if time.time() - t0 > wait:
            break
        time.sleep(1)
    info["video_count"] = parse_count(info.get("video_count_text"))
    return info


def read_video_music(driver, url: str, wait: float = 4) -> dict:
    """動画のページ（ログインなし）から、使っている音源の id・題・作者・長さと、TikTok が照合した曲の番号（meta_song_ids）を読む
    （属性の段と同じ読み方）。同じ曲の音源を探す手掛かりに、動画の説明文と関連する検索の語も返す"""
    import enrich
    from selenium.webdriver.common.by import By
    driver.get(url)
    time.sleep(wait)
    tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
    it, _ = enrich.find_item_struct(json.loads(tag.get_attribute("innerHTML")))
    it = it or {}
    m = it.get("music") or {}
    return {"id": m.get("id"), "title": m.get("title"), "author": m.get("authorName"), "original": m.get("original"),
            "duration": m.get("duration"), "meta_song_ids": enrich.meta_song_ids(m), "desc": it.get("desc") or "",
            "suggested_words": [w for w in (it.get("suggestedWords") or []) if isinstance(w, str) and w.strip()]}


def music_id_of(url: str) -> str:
    """楽曲ページの URL の末尾の番号（音源の id）"""
    return (url or "").rstrip("/").split("?")[0].rsplit("-", 1)[-1]


def music_url_of(music_id, title=None) -> str:
    """音源の id から楽曲ページの URL（URL は末尾の id だけで決まる。見出しの部分は題にして読めるように）"""
    from urllib.parse import quote
    slug = quote(re.sub(r"\s+", "-", (title or "").strip()), safe="-") or "sound"
    return f"https://www.tiktok.com/music/{slug}-{music_id}"


def kana_key(s) -> str:
    """表記ゆれを見ない鍵: 全角半角・大文字小文字・カタカナとひらがな・濁点と半濁点・伸ばし棒・記号と空白の違いを消す
    （「キャワポッピンドゥー」「きゃわほっぴんどぅ」「きゃわぽっぴんどぅー」が同じになる）"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)
    s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    return re.sub(r"[\W_ー〜~]+", "", s)


def fits_song(song: str, artist: str, title, author) -> bool:
    """題が曲名に合い、作者がアーティスト名に合う（公式の音源。proto_runner._music_fits と同じ考え。表記ゆれは見ない）"""
    s, a, t, c = kana_key(song), kana_key(artist), kana_key(title), kana_key(author)
    return bool(s and t and s in t) and (not a or not c or a in c or c in a)


def discover_slug(word: str) -> str:
    from urllib.parse import quote
    return quote(re.sub(r"\s+", "-", (word or "").strip()), safe="-")


def discover_links(driver, word: str, limit: int = 16, wait: float = 8) -> list:
    """曲名などの discover のページ（ログインなしで見られる）に並ぶ人気の動画の URL（重ねずに全部）。並びは読み込みのあとから出てくるので、
    limit 本そろうか wait 秒たつまで待つ"""
    slug = discover_slug(word)
    if not slug:
        return []
    driver.get(f"https://www.tiktok.com/discover/{slug}")
    links, t0 = [], time.time()
    while time.time() - t0 < wait:
        links = driver.execute_script("return Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
                                      ".map(a => a.href.split('?')[0]);") or []
        if len(set(links)) >= limit:
            break
        time.sleep(1)
    return list(dict.fromkeys(links))


# discover のページは、スクロールのたびに /api/seo/kap/video_list/ を読む（1回16本）。その動画の音源（id・題・作者・長さ）が入っている
# （TikTok の照合した曲の番号は入っていない。2026-10-07 にきゃわで確かめた: 10回で60本・32秒）
DISCOVER_HOOK_JS = r"""
(() => {
  if (window.__disc) return;
  window.__disc = [];
  const want = u => typeof u === 'string' && /\/api\/seo\/kap\/video_list/.test(u);
  const take = t => {
    try {
      const j = JSON.parse(t);
      for (const it of (j.videoList || [])) {
        const m = it.music || {}, a = it.author || {};
        window.__disc.push({id: it.id, uid: a.uniqueId, desc: it.desc || '',
          hashtags: (it.textExtra || []).map(x => x.hashtagName).filter(x => x),
          music: {id: m.id, title: m.title, authorName: m.authorName, duration: m.duration}});
      }
    } catch (e) {}
  };
  const of = window.fetch;
  window.fetch = async function(input, init) {
    const u = typeof input === 'string' ? input : (input && input.url);
    const r = await of.apply(this, arguments);
    if (want(String(u))) { try { take(await r.clone().text()); } catch (e) {} }
    return r;
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__u = String(u); return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    if (want(this.__u)) this.addEventListener('load', () => { try { take(this.responseText); } catch (e) {} });
    return os.apply(this, arguments);
  };
})();
"""


def hook_discover(driver) -> bool:
    """これから開く discover のページで、一覧データの動画の音源を拾う仕掛けを入れる。入れられなければ False（discover_items は URL だけ返す）"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": DISCOVER_HOOK_JS})
        return True
    except Exception:
        return False


def discover_items(driver, word: str, scrolls: int = 10, limit: int = 16, wait: float = 6) -> list:
    """discover のページを scrolls 回スクロールして、並んだ動画を一覧データから: [{"video", "music", "desc", "hashtags"}]。
    一覧データが拾えなければ（仕掛けが無い・TikTok の形が変わった）、前と同じく並びの URL（文字列。開いて音源を読む）"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    links = discover_links(driver, word, limit, wait=8)
    if not links:
        return []
    for _ in range(max(0, scrolls)):
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
        except Exception:
            break
        time.sleep(2.5)
    try:
        got = driver.execute_script("return window.__disc || [];") or []
    except Exception:
        got = []
    out, seen = [], set()
    for x in got:
        if not isinstance(x, dict) or not x.get("id") or x["id"] in seen:
            continue
        seen.add(x["id"])
        out.append({"video": f"https://www.tiktok.com/@{x.get('uid') or 'user'}/video/{x['id']}", "music": x.get("music") or {},
                    "desc": x.get("desc") or "", "hashtags": x.get("hashtags") or []})
    return out or links


def related_words(song: str, counts, n, min_count: int = 1) -> list:
    """関連する検索の語・ハッシュタグ（counts: 語 → 出た動画の数）のうち、曲名の表記ゆれか曲名を含む語を、多い順に n 個（None は上限なし）。
    min_count 本より少ない動画にしか出ない語は除く。曲名そのものと、同じ discover のページになる語も除く"""
    import difflib
    k = kana_key(song)
    if not k or (n is not None and n <= 0):
        return []
    out, slugs = [], {discover_slug(song).lower()}
    for w, _c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        if _c < min_count:
            break
        kw = kana_key(w)
        if not kw or not (k in kw or difflib.SequenceMatcher(None, k, kw).ratio() >= 0.8):
            continue
        if discover_slug(w).lower() in slugs:
            continue
        slugs.add(discover_slug(w).lower())
        out.append(w)
        if n is not None and len(out) >= n:
            break
    return out


def search_fan_sounds(song: str, page_ids, links_of, music_of, words=None, page_links=None) -> dict:
    """同じ曲のほかの音源（ファンが上げた「オリジナル楽曲 - 〇〇」など）を探す。links_of(語) → discover の動画
    （discover_items の形 {"video", "music", ...}。音源が入っていれば開かない。URL の文字列なら開いて読む）、music_of(URL) → read_video_music の形。
    1. 曲名の discover のページの動画の音源を数える
    2. その動画の説明文・ハッシュタグ・関連する検索の語のうち、曲名の表記ゆれ・曲名を含む語（words 個。None は上限なしで2本以上に出た語）の
       ページも同じように数える（2026-10-06 きゃわぽっぴんどぅー: 39K のファンの音源は曲名のページには無く、「キャワポッピンドゥー」
       「きゃわほっぴんどぅ」「きゃわぽっぴんどぅー ダンス」のページに合わせて5回出た）
    3. 取るページ（page_ids）の音源の動画を開いて、TikTok が照合した曲の番号を知る（無ければ page_links() の動画を3本まで）
    4. 取るページでない音源ごとに動画を2本まで開いて曲の番号を読み、同じ番号の音源を、出た回数の多い順に返す
       （番号の無い音源は、同じ曲か分からないので返さない）
    戻り値 {"words", "videos"（音源が分かった動画の数）, "opened"（開いた動画の数）, "meta_song_ids",
           "candidates": [{"id", "title", "author", "duration", "hits", "videos"}]}"""
    page_ids = {str(x) for x in page_ids}
    seen, reads, opened = set(), [], {}
    counts = collections.Counter()

    def open_(u):
        if u not in opened:
            opened[u] = music_of(u) or {}
        return opened[u]

    def read(word):
        for x in links_of(word) or []:
            if isinstance(x, dict):
                u, mu = x.get("video") or x.get("url"), x.get("music") or {}
                if not u or u in seen:
                    continue
                seen.add(u)
                m = ({"id": str(mu["id"]), "title": mu.get("title"), "author": mu.get("authorName") or mu.get("author"),
                      "duration": mu.get("duration"), "desc": x.get("desc") or "", "hashtags": x.get("hashtags") or []}
                     if mu.get("id") else open_(u))
            else:
                u = x
                if u in seen:
                    continue
                seen.add(u)
                m = open_(u)
            if m.get("id"):
                reads.append({**m, "id": str(m["id"]), "video": u, "word": word})

    def words_of(m):
        return (set(m.get("suggested_words") or []) | set(m.get("hashtags") or [])
                | set(re.findall(r"#([^\s#]+)", m.get("desc") or "")))

    def meta_of(rs):
        """同じ音源の動画の、曲の番号（開いた動画に無ければ2本まで開く）"""
        got = {x for m in rs for x in (m.get("meta_song_ids") or [])}
        for m in rs[:2]:
            if got:
                break
            got |= set(open_(m["video"]).get("meta_song_ids") or [])
        return got

    def sounds():
        out = collections.defaultdict(list)
        for m in reads:
            out[m["id"]].append(m)
        return out

    read(song)
    # 曲名のページの音源ごとに、照合のための動画を先に開く（開いた動画の関連する検索の語も、表記ゆれの語を選ぶのに使う。
    # 一覧データには関連する検索の語が無い）
    for rs in sounds().values():
        meta_of(rs)
    for m in reads:
        counts.update(words_of(m))
    for u, m in opened.items():
        if u not in {r["video"] for r in reads if "suggested_words" in r}:
            counts.update(set(m.get("suggested_words") or []))
    ws = related_words(song, counts, words, min_count=1 if words else 2)
    for w in ws:
        read(w)
    by_sound = sounds()
    metas = set()
    for sid in page_ids:
        if by_sound.get(sid):
            metas |= meta_of(by_sound[sid])
    if not metas and page_links is not None:
        for u in (page_links() or [])[:3]:
            m = music_of(u) or {}
            if str(m.get("id")) in page_ids:
                metas |= set(m.get("meta_song_ids") or [])
            if metas:
                break
    cands = []
    if metas:
        for sid, rs in by_sound.items():
            if sid in page_ids or not metas & meta_of(rs):
                continue
            m = rs[0]
            cands.append({"id": sid, "title": m.get("title"), "author": m.get("author"), "duration": m.get("duration"),
                          "hits": len(rs), "videos": [x["video"] for x in rs]})
    return {"words": [song] + ws, "videos": len(reads), "opened": len(opened), "meta_song_ids": sorted(metas),
            "candidates": sorted(cands, key=lambda c: -c["hits"])}


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
