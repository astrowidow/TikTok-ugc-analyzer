"""Claude に出す道具（MCP、この Mac の中で動く）。docs/ALL_IN_APP_PLAN.md 段1。

Claude デスクトップが、設定ファイル（claude_link.py が書く）にしたがって次のように起動し、標準入出力で話す:

  /Applications/UGC Collector.app/Contents/MacOS/UGC Collector --mcp

道具の中身は本線と同じ（mcp_proto.py の道具・proto_runner.py の仕事の列・flow_w1.py の W1）。違いは3つだけ:
  - 利用者はこの Mac の1人で固定（秘密の URL は要らない）
  - 取得は Windows 機の係でなく、メニューバーの取得アプリが拾う（LocalHooks）
  - 「指示書を見る・直す」「知識ベースを新しくする」の道具を足す
"""
import fcntl
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from . import config, system, worker_entry


class FileRLock:
    """プロセスをまたぐ再入可能なロック。Claude が道具のプロセスを2つ起こしても（チャットと Cowork など）、
    仕事の状態（tasks.json）を同時に書き換えないようにする"""

    def __init__(self, path: Path):
        self.path = path
        self._t = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._t.acquire()
        if self._depth == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._t.release()
        return False


class LocalHooks:
    """proto_runner.LOCAL に差し込むもの（取得アプリの形）"""

    def acquisition_settings(self) -> dict:
        s = {"chrome_port": str(config.CHROME_PORT)}
        if config.load_state().get("ai_trial"):   # 運営の試し用（メニューの運営向け）
            s.update(config.TRIAL_SETTINGS)
        return s

    def app_state(self) -> dict:
        st = config.load_state()
        return {"running": app_running(),
                "login_wanted": worker_entry.need_login_flag().exists() or not st.get("logged_in_at")}

    def inspect_many(self, urls: list) -> list:
        """楽曲ページを順に開いて、題・作者・UGC 数を読む（1つの Chrome で。1本あたり数秒）"""
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない。UGC 数は UGC_TEST_COUNTS（{id: 数}）から
            import json as _json
            counts = _json.loads(os.environ.get("UGC_TEST_COUNTS") or "{}")
            out = []
            for u in urls:
                n = counts.get(u.rsplit("-", 1)[-1])
                out.append({"title": os.environ.get("UGC_TEST_MUSIC_TITLE"), "creator": None,
                            "video_count_text": f"{n} 動画" if n else None, "video_count": n})
            return out
        import scraper
        from acquire import pipeline
        d = scraper.create_headless_driver()
        out = []
        try:
            for u in urls:
                try:
                    d.get(u)
                    out.append(pipeline.read_music_page(d, wait=12))   # UGC 数が出るまで待つ（決まった待ちはしない）
                except Exception as e:
                    out.append({"error": type(e).__name__})
        finally:
            try:
                d.quit()
            except Exception:
                pass
        return out

    def inspect_music(self, url: str) -> dict:
        """楽曲ページを1回だけ開いて、題・作者・UGC 数を読む（ヘッドレス・ログインなし。取得の一覧の段と同じ開き方）"""
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない
            return {"title": None, "creator": None, "video_count_text": None, "video_count": None, "skipped": True}
        import time
        import scraper
        from acquire import pipeline
        d = scraper.create_headless_driver()
        try:
            d.get(url)
            time.sleep(scraper.PAGE_LOAD_TIME)
            return pipeline.read_music_page(d)
        finally:
            try:
                d.quit()
            except Exception:
                pass

    def find_sounds(self, song: str, video_urls: list | None = None, discover: bool = True,
                    limit: int = 8, given_max: int = 3) -> dict:
        """その曲の音源（楽曲ページ）を TikTok で探す。曲名の「discover」のページ（人気の動画が並ぶ。ログインなしで見られる）から
        動画を集め、渡された動画と合わせて、動画ごとに使っている音源を読む（1つの Chrome で。1本数秒）。
        （2026-10-04: AI のウェブ検索に出る動画は古いものが多く、先行版や個人の音源ばかりだった。discover の人気の動画8本では、
        本命の 31.2K のページが3本・17.7K のページが1本）
        戻り値 {"discover": URL, "found": discover で見つけた動画の数, "reads": [{id, title, author, music_url, video}]}"""
        from urllib.parse import quote
        slug = quote(re.sub(r"\s+", "-", (song or "").strip()), safe="-")
        url = f"https://www.tiktok.com/discover/{slug}" if discover and slug else None
        given = list(dict.fromkeys(video_urls or []))
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない。UGC_TEST_DISCOVER（{曲名: [[音源 id, 題, 作者], …]}）から
            import json as _json
            found = (_json.loads(os.environ.get("UGC_TEST_DISCOVER") or "{}").get(song) or []) if discover else []
            reads = [_with_music_url({"id": x[0], "title": x[1], "author": x[2], "video": f"discover-{i}"})
                     for i, x in enumerate(found[:limit])]
            g = given[:max(given_max, limit + given_max - len(found[:limit]))]
            reads += [{**m, "video": v} for v, m in zip(g, self.music_from_videos(g))]
            return {"discover": url, "found": len(found), "reads": reads}
        import time
        import scraper
        d = scraper.create_headless_driver()
        links = []
        try:
            if url:
                try:
                    d.get(url)
                    t0 = time.time()
                    while time.time() - t0 < 8:   # 動画の並びは読み込みのあとから出てくる
                        links = d.execute_script("return Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
                                                 ".map(a => a.href.split('?')[0]);") or []
                        if len(set(links)) >= limit:
                            break
                        time.sleep(1)
                except Exception:
                    links = []
                links = list(dict.fromkeys(links))
            # 読むのは合わせて limit + given_max 本まで（道具の返事が遅くなりすぎないように）。discover で足りなければ、渡された動画で埋める
            targets = list(dict.fromkeys(given[:max(given_max, limit + given_max - len(links[:limit]))] + links[:limit]))
            reads = [{**self._read_music(d, v), "video": v} for v in targets]
        finally:
            try:
                d.quit()
            except Exception:
                pass
        return {"discover": url, "found": len(links), "reads": reads}

    @staticmethod
    def _read_music(d, url: str) -> dict:
        import time
        from acquire import pipeline
        m = {}
        for wait in (1.5, 4):   # 動画のデータはページの最初の HTML に入っている。読めなければ1回だけ待ち直す
            try:
                m = pipeline.read_video_music(d, url, wait=wait)
            except Exception as e:
                m = {"error": type(e).__name__}
            if m.get("id"):
                break
            time.sleep(1)
        return _with_music_url(m)

    def music_from_videos(self, urls: list) -> list:
        """その曲を使った動画のページを順に開いて、音源の id・題・作者と、楽曲ページの URL を読む（1つの Chrome で。1本あたり数秒）"""
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない。UGC_TEST_VIDEO_MUSIC（{動画 id: [音源 id, 題, 作者]}）から
            import json as _json
            table = _json.loads(os.environ.get("UGC_TEST_VIDEO_MUSIC") or "{}")
            out = []
            for u in urls:
                v = table.get(u.rstrip("/").rsplit("/", 1)[-1])
                m = ({"id": v[0], "title": v[1], "author": v[2]} if v else
                     {"id": "7000000000000000009", "title": os.environ.get("UGC_TEST_VIDEO_MUSIC_TITLE", "てすと"), "author": "だれか"})
                out.append(_with_music_url(m))
            return out
        import scraper
        d = scraper.create_headless_driver()
        try:
            return [self._read_music(d, u) for u in urls]
        finally:
            try:
                d.quit()
            except Exception:
                pass

    def music_from_video(self, url: str) -> dict:
        """その曲を使った動画のページを1回だけ開いて、音源の id・題・作者と、楽曲ページの URL を返す"""
        return self.music_from_videos([url])[0]

    def request_stop(self, analysis_id: str) -> None:
        """この分析の取得をやめる印を置く（メニューバーのアプリが5秒おきに見て、係を止める）"""
        config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        (config.LOCK_DIR / f"cancel-{analysis_id}").write_text(str(os.getpid()), encoding="utf-8")

    def ensure_app(self) -> str:
        """メニューバーの取得アプリが動いていなければ起こす（仕事は、取得アプリが5秒おきに見て拾う）"""
        if app_running():
            return "running"
        if os.environ.get("UGC_COLLECTOR_NO_APP_LAUNCH"):   # 試験用: メニューバーのアプリを起こさない（TikTok に触らない）
            return "disabled"
        try:
            if config.FROZEN:
                bundle = Path(sys.executable).resolve().parents[2]   # …/UGC Collector.app
                subprocess.Popen(["/usr/bin/open", "-g", str(bundle)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                env = {**os.environ, "PYTHONPATH": str(config.REPO_ROOT / "collector")}
                subprocess.Popen([sys.executable, "-m", "collector_app"], cwd=str(config.REPO_ROOT), env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            return "started"
        except Exception as e:
            return f"failed: {e}"


def _with_music_url(m: dict) -> dict:
    """音源の id から楽曲ページの URL を作る（URL は末尾の id だけで決まる。見出しの部分は曲名にして読めるように）"""
    if m.get("id"):
        from urllib.parse import quote
        slug = quote(re.sub(r"\s+", "-", (m.get("title") or "").strip()) or "sound", safe="-")
        m["music_url"] = f"https://www.tiktok.com/music/{slug}-{m['id']}"
    return m


def app_running() -> bool:
    try:
        pid = int(config.PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    return system._alive(pid)   # noqa: SLF001


def build():
    """道具をそろえたサーバー（試験でも使う）"""
    import mcp_proto
    import proto_runner
    proto_runner.LOCAL = LocalHooks()
    proto_runner._lock = FileRLock(config.LOCK_DIR / "runner.lock")   # noqa: SLF001
    return mcp_proto._build_server(user_of=lambda ctx: config.OWNER, local=True)   # noqa: SLF001


def main() -> int:
    config.setup_env()
    import logging
    from logging.handlers import RotatingFileHandler
    try:   # 部品（scraper）の記録の設定は画面（標準出力）にも出す。Claude との通話を汚さないよう、ここでは使わせない
        import log_setup
        log_setup._configured = True   # noqa: SLF001
    except Exception:
        pass
    # 記録は全部 logs/mcp.log へ（mcp_proto・SDK の記録も。標準出力は Claude との通話に使う）。
    # ハンドラは根元にだけ付ける（名前つきのロガーにも付けると、伝わって2行ずつ出る）
    root = logging.getLogger()
    if not root.handlers:
        h = RotatingFileHandler(config.LOG_DIR / "mcp.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(process)d] %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    log = logging.getLogger("collector-mcp")
    config.seed_data(log)
    log.info("=== Claude の道具を開きます（%s %s、データ %s）", config.APP_NAME, __import__("collector_app").VERSION,
             config.DATA_DIR)
    try:
        server = build()
        server.run("stdio")
    except Exception:
        log.exception("Claude の道具が止まりました")
        raise
    finally:
        log.info("=== Claude の道具を閉じました")
    return 0
