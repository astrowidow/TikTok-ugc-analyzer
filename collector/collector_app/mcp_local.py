"""Claude に出す道具（MCP、この Mac の中で動く）。docs/ALL_IN_APP_PLAN.md 段1。

Claude デスクトップが、設定ファイル（claude_link.py が書く）にしたがって次のように起動し、標準入出力で話す:

  /Applications/UGC Analyzer.app/Contents/MacOS/UGC Analyzer --mcp

道具の中身は本線と同じ（mcp_proto.py の道具・proto_runner.py の仕事の列・flow_w1.py の W1）。違いは3つだけ:
  - 利用者はこの Mac の1人で固定（秘密の URL は要らない）
  - 取得は Windows 機の係でなく、メニューバーの取得アプリが拾う（LocalHooks）
  - 「指示書を見る・直す」「知識ベースを新しくする」の道具を足す
"""
import datetime
import fcntl
import importlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import VERSION, config, system, worker_entry

# 道具の処理が、仕事の途中で初めて読む部品（関数の中の import と、その先の部品）。起動のときに先に読んでおく（2026-10-12）。
# PyInstaller で固めたアプリは、部品の多くを実行ファイルに埋め込んだ束（PYZ）から、読むときに実行ファイルを開き直して取り出す。
# .app を置き換えると、前の版の道具の処理は新しい実行ファイルの中を前の版の位置で読み、zlib.error・SystemExit で落ちる
# （「サービス側のエラー、呼び直して」が続く。docs/PLAN_0_8.md 3-4）。先に読んでおけば、版のずれを知らせる返事（version_check）を
# 確実に返せて、置き換えの最中に走っていた道具の仕事も、部品が読めずに途中で落ちることが無くなる。
# 道具の仕事の .py（proto_runner など）は DATA_DIR/code/<版>/ に写したものを読むので、置き換えでは壊れない（config.code_dir）。
# 洗い方: proto_runner・flow_w1・kb_update・store・acquire/*・analysis/*・mcp_local の関数の中の import と、その部品の先頭の import。
# 外したもの:
#   - analysis/build_llm_input.py・prep_comments.py・prep_sample.py: 読むと引数を読んで動き出すスクリプト。道具は別のプロセス
#     （sys.executable）で走らせるので、この処理には読まれない
#   - PIL.Image.init()（画像の形式の部品を全部読む。約0.7秒）: シートの画像は PNG なので、PNG・JPEG などを読む preinit() だけにする
#   - mcp.cli（読むと終わる。UGCCollector.spec でも外している）
PRELOAD = (
    # 道具の本体（proto_runner・mcp_proto は build() でも読む）
    "proto_runner", "mcp_proto", "flow_w1", "kb_update", "prompt_store", "user_settings", "store", "tiktok_lock", "log_setup", "analysis_info",
    "acquire.pipeline", "acquire.launch", "acquire.worker", "acquire.deepen", "acquire.recut", "acquire.spatest",
    "acquire.notify",
    "scraper",                     # pandas・requests・selenium（楽曲ページを開く・音源を探す）
    "pool", "attr_cluster", "enrich",   # acquire.pipeline・acquire.recut が関数の中で読む（numpy）
    "selenium.webdriver", "selenium.webdriver.common.by", "selenium.webdriver.common.keys",
    "selenium.webdriver.chrome.options", "selenium.webdriver.chrome.service", "selenium.webdriver.chrome.webdriver",
    "requests", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    # 標準の部品で、関数の中で読むもの・ふつうの関数が裏で読むもの
    "difflib", "unicodedata", "traceback", "secrets", "io", "zipfile", "shutil", "urllib.parse", "argparse",
    "importlib.util", "ctypes", "csv", "statistics", "sqlite3", "smtplib", "email.message", "html", "uuid", "tempfile",
    "plistlib",
    "_strptime",                   # datetime.strptime が初めて呼ばれたときに読む
    "encodings.idna",              # ネットにつなぐとき（socket.getaddrinfo）に読む
    "encodings.utf_8_sig", "encodings.ascii", "encodings.latin_1",
    # 道具の返事の組み立て（MCP の SDK）と、その下の非同期の部品
    "mcp.server.mcpserver", "mcp.server.mcpserver.exceptions", "mcp.types", "anyio._backends._asyncio",
    "_suggestions",                # 例外の文の「もしかして」（NameError などを記録に書くときに読む）
    # このアプリの部品で、関数の中で読むもの（worker_entry.login_waiting → jobs）
    "collector_app.jobs",
)


def preload(log=None) -> dict:
    """PRELOAD の部品を先に読む。読めなかった部品は記録だけして続ける（その部品を使う道具だけが、使うときに理由を返す）"""
    t0 = time.time()
    failed = []
    for name in PRELOAD:
        try:
            importlib.import_module(name)
        except BaseException as e:   # noqa: BLE001 部品の読み込みの SystemExit でも、道具の処理は止めない
            failed.append(f"{name}（{type(e).__name__}）")
    try:
        import PIL.Image
        PIL.Image.preinit()   # PNG・JPEG・GIF・BMP・PPM の部品（シートの画像を書く・読む）
    except BaseException as e:   # noqa: BLE001
        failed.append(f"PIL.Image.preinit（{type(e).__name__}）")
    res = {"seconds": round(time.time() - t0, 2), "modules": len(sys.modules), "failed": failed}
    if log:
        log.info("道具が途中で読む部品を先に読みました（%s秒・%s個%s）", res["seconds"], res["modules"],
                 "・読めなかったもの " + "、".join(failed) if failed else "")
    return res


def _stat_mark(exe):
    """実行ファイルの印（入れ替えると変わる）。無ければ None"""
    try:
        s = os.stat(exe)
    except (OSError, TypeError):
        return None
    return (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size)


def stale_text(old: str, new: str | None, ai: str = "Claude（ChatGPT）") -> str:
    """版のずれを知らせる返事（道具の仕事はしない）"""
    if new is None:
        return (f"UGC Analyzer がアプリケーションフォルダに見つかりません（入れ替えの途中か、ゴミ箱に入れた）。"
                f"UGC Analyzer を入れ終えたら、{ai}を一度終了（⌘Q）して開き直してください。会話と分析は消えません。")
    ver = f"{old}→{new}" if new != old else new
    return (f"UGC Analyzer が新しくなりました（{ver}）。この会話の道具は前の版のままなので、"
            f"{ai}を一度終了（⌘Q）して開き直してください。会話と分析は消えません。")


# Mac の通知についての決まった説明（info の about=app）。設定の名前は macOS 26 の日本語の表記
# （システム設定の「通知」・アプリの一覧の「スクリプトエディタ」・スイッチの「通知を許可」。2026-10-12 に運営の Mac で確かめた）
NOTIFICATIONS_TEXT = (
    "UGC Analyzer は署名していないので、Mac の通知センターは使えない（ログの「Notifications are not allowed」は想定どおりで、直すことは無い）。"
    "通知は「スクリプトエディタ」の名前で出る。届かないときは、システム設定 →「通知」→ アプリの一覧の「スクリプトエディタ」→「通知を許可」をオン"
    "（一覧に UGC Analyzer は出ないし、出す方法も無い）")

# 最近の問題に入れない、想定どおりの行
EXPECTED_LOG = ("Notifications are not allowed", "通知センターは使えない", "通知の許可")
_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (WARNING|ERROR|CRITICAL)\s+(?:\[\d+\] )?(.*)$")


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

    def __init__(self, exe=None, ai: str = "Claude（ChatGPT）"):
        # この処理を起こした実行ファイル（.app の Contents/MacOS/UGC Analyzer）の印と版を覚える。
        # 凍結されていない（試験・開発）なら見ない。試験では exe を渡して確かめる
        self.exe = Path(exe) if exe is not None else (Path(sys.executable) if config.FROZEN else None)
        self.mark = _stat_mark(self.exe) if self.exe is not None else None
        self.version = VERSION
        self.ai = ai                 # 返事で開き直してもらう AI の名前（main() が呼び元から決める）
        self._stale = None           # 一度ずれたら、この処理が終わるまでずっと同じ返事
        self._stale_logged = False

    # --- 版のずれ（2026-10-12。docs/PLAN_0_8.md 3-4） ---
    def version_check(self) -> str | None:
        """アプリを新しくしたのに、この道具の処理が前の版のまま動いていれば、その知らせの文（道具の仕事はしない）。
        ずれていなければ None。mcp_proto._call が道具を呼ぶたびに、仕事の前に呼ぶ。
        ここで使うのは os.stat・open・正規表現だけ（.app を置き換えたあとは、まだ読んでいない部品を読めないため）"""
        if self.mark is None:
            return None
        if self._stale:
            return self._stale
        cur = _stat_mark(self.exe)
        if cur is None:   # 入れ替えの途中・ゴミ箱に入れた。戻ってくることもあるので、覚えずに毎回見る
            return stale_text(self.version, None, self.ai)
        if cur == self.mark:
            # 実行ファイルはそのままでも、新しい版のメニューのアプリが開かれていれば（「両方とも残す」でできた「UGC Analyzer 2.app」など。
            # 新しい版は開いたときに state.json の app_version を書き、Claude・ChatGPT の設定を自分に書き直す）、この処理は前の版
            try:
                newer = config.load_state().get("app_version")
            except Exception:
                newer = None
            if not newer or system.version_key(newer) <= system.version_key(self.version):
                return None
            new = str(newer)
            app = system.bundle_of(self.exe)
            # 開き直しても古い方の .app が起こされるとき（つなぎを書き直せなかったなど）に、輪にならないように
            extra = (f"開き直してもこの知らせが続くときは、アプリケーションフォルダの古い「{app.stem}」（{self.version}）を"
                     "ゴミ箱に入れてから、もう一度開き直してください。") if app else ""
        else:
            app = system.bundle_of(self.exe)
            new = (system.bundle_version(app) if app else None) or "?"
            extra = ""
        self._stale = stale_text(self.version, new, self.ai) + extra
        if not self._stale_logged:
            self._stale_logged = True
            import logging
            logging.getLogger("collector-mcp").warning(
                "アプリが入れ替わりました（%s→%s）。Claude（ChatGPT）を開き直すまで、道具は仕事をせずに開き直しを頼みます",
                self.version, new)
        return self._stale

    def disk_free_bytes(self) -> int:
        """データの置き場（分析・レポート）のボリュームの空き（バイト）。1つの分析で 144〜443MB 使う"""
        p = config.DATA_DIR
        while not p.exists() and p != p.parent:   # まだ作っていなければ、ある親で測る
            p = p.parent
        return int(shutil.disk_usage(p).free)

    def app_info(self) -> dict:
        """info 道具（about=app）の中身。どれも、調べられなければ None（落ちない）"""
        out = {}

        def put(key, fn):
            try:
                out[key] = fn()
            except Exception:
                out[key] = None

        exe = self.exe
        app = system.bundle_of(exe) if exe is not None else None
        put("app_version_disk", lambda: system.bundle_version(app) if app else None)
        out["tool_version"] = self.version
        put("app_path", lambda: str(app) if app else None)
        try:
            stale = self.version_check()
        except Exception:
            stale = None
        disk = out.get("app_version_disk")
        out["mismatch"] = bool(stale) or bool(disk and disk != self.version)
        out["mismatch_text"] = stale or (stale_text(self.version, disk, self.ai) if out["mismatch"] else None)
        st = {}
        try:
            st = config.load_state()
        except Exception:
            pass
        put("menu_app_running", system.app_running)
        out["logged_in_at"] = st.get("logged_in_at")
        put("login_wanted", lambda: bool(worker_entry.login_waiting() or not st.get("logged_in_at")))
        put("chrome_port_open", lambda: _port_open(config.CHROME_PORT))
        put("recent_problems", recent_problems)
        out["notifications"] = NOTIFICATIONS_TEXT
        put("duplicate_apps", _duplicate_apps)
        put("disk_free_gb", lambda: round(self.disk_free_bytes() / 1e9, 1))
        put("edited_prompts", _edited_prompts)
        put("knowledge", _knowledge)
        return out

    def acquisition_settings(self) -> dict:
        s = {"chrome_port": str(config.CHROME_PORT)}
        if config.load_state().get("ai_trial"):   # 運営の試し用（メニューの運営向け）
            s.update(config.TRIAL_SETTINGS)
        return s

    def app_state(self) -> dict:
        st = config.load_state()
        return {"running": app_running(),   # 係がもういない古いログイン待ちの印は見ない（login_waiting が消す）
                "login_wanted": worker_entry.login_waiting() or not st.get("logged_in_at")}

    def inspect_many(self, urls: list) -> list:
        """楽曲ページを順に開いて、題・作者・UGC 数を読む（1つの Chrome で。1本あたり数秒）。
        日本の地域で使えないページ（「この楽曲はご利用になれません」）は unavailable=True（pipeline.read_music_page）"""
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない。UGC 数は UGC_TEST_COUNTS（{id: 数}）から
            import json as _json
            counts = _json.loads(os.environ.get("UGC_TEST_COUNTS") or "{}")
            gone = set(_json.loads(os.environ.get("UGC_TEST_UNAVAILABLE") or "[]"))   # 日本の地域で使えないページの id
            out = []
            for u in urls:
                if u.rsplit("-", 1)[-1] in gone:
                    out.append({"title": None, "creator": None, "video_count_text": None, "video_count": None,
                                "unavailable": True})
                    continue
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
        """楽曲ページを1回だけ開いて、題・作者・UGC 数を読む（ヘッドレス・ログインなし。取得の一覧の段と同じ開き方）。
        日本の地域で使えないページは unavailable=True（pipeline.read_music_page）"""
        if os.environ.get("UGC_COLLECTOR_NO_INSPECT"):   # 試験用: TikTok に触らない
            import json as _json
            if url.rsplit("-", 1)[-1] in set(_json.loads(os.environ.get("UGC_TEST_UNAVAILABLE") or "[]")):
                return {"title": None, "creator": None, "video_count_text": None, "video_count": None, "unavailable": True}
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
                    limit: int = 8, given_max: int = 3, artist: str = "") -> dict:
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
        import scraper
        from acquire import pipeline
        d = scraper.create_headless_driver()
        links = []
        try:
            if url:
                # 動画の並びは読み込みのあとから出てくる（limit 本そろうか8秒まで待つ）。曲名だけのページが無い曲（トップページに飛ばされる）は、
                # アーティスト名を足した語で（pipeline.discover_words。2026-10-07「愛くださいませ」「倍倍FIGHT」）
                for w in pipeline.discover_words(song, artist):
                    try:
                        links = pipeline.discover_links(d, w, limit)
                    except Exception:
                        links = []
                    if links:
                        url = f"https://www.tiktok.com/discover/{pipeline.discover_slug(w)}"
                        break
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
                bundle = Path(sys.executable).resolve().parents[2]   # …/UGC Analyzer.app
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
    """メニューバーのアプリが動いているか（app.pid の錠と、番号のプロセスが本当にアプリか。番号が生きているかだけでは見ない）"""
    return system.app_running()


# --- info（about=app）の材料（2026-10-12。AI に推測で答えさせない。docs/PLAN_0_8.md 3-1） ---
def _port_open(port: int) -> bool:
    """取得用の Chrome（つなぐ口 config.CHROME_PORT）が開いているか"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _tail_lines(p: Path, limit: int = 600_000) -> list:
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > limit else lines   # 途中から読んだ最初の1行は切れている


def recent_problems(hours: float = 24, limit: int = 5, now: float | None = None) -> list:
    """メニューのアプリ（collector.log）と AI の道具（mcp.log）の、最近の WARNING・ERROR（想定どおりの行は除く）。
    新しい順に最大 limit 件: [{"at": 時刻, "level", "where": どこの記録か, "text": 短い文, "times": 同じ文の回数}]。
    ERROR の後ろに続く例外の詳しい行（Traceback …）は入れない（1件目の行だけ）"""
    now = time.time() if now is None else now
    since = now - hours * 3600
    seen = {}
    for name, where in (("collector", "メニューのアプリ"), ("mcp", "AI の道具")):
        for p in (config.LOG_DIR / f"{name}.log.1", config.LOG_DIR / f"{name}.log"):
            for ln in _tail_lines(p):
                m = _LOG_LINE_RE.match(ln)
                if not m or any(k in ln for k in EXPECTED_LOG):
                    continue
                try:
                    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
                if t < since:
                    continue
                text = m.group(3).strip()
                text = text if len(text) <= 160 else text[:159] + "…"
                key = (where, re.sub(r"\d+", "N", text))   # 数だけ違う同じ文はまとめる
                cur = seen.get(key)
                seen[key] = {"at": m.group(1), "t": t, "level": m.group(2), "where": where, "text": text,
                             "times": (cur["times"] + 1) if cur else 1}
    rows = sorted(seen.values(), key=lambda r: r["t"], reverse=True)[:limit]
    for r in rows:
        r.pop("t")
    return rows


def _duplicate_apps() -> list:
    """ほかの UGC Analyzer の .app（「両方とも残す」でできた「UGC Analyzer 2.app」など）。1つしか無ければ空"""
    apps = system.find_app_bundles()
    if len(apps) < 2:
        return []
    return [{"path": a["path"], "version": a["version"]} for a in apps if not a["running"]]


def _edited_prompts() -> list:
    """利用者が直した指示書（直したあとにアプリの版で初期の指示書が変わったかも）。prompt_store.edited_prompts"""
    import prompt_store
    return prompt_store.edited_prompts()


def _knowledge() -> dict:
    """知識ベース: 記事の数・カードの数・最後に新着を見た日・取り込み待ち・アプリが運営の同梱物を届けた記録"""
    import json as _json
    import kb_update
    if not kb_update.ready():
        return {"ready": False, "summary": kb_update.summary()}
    s = kb_update.state()
    cards = kb_update.kb_dir() / "distilled" / "cards.jsonl"
    try:
        shipped = _json.loads((kb_update.kb_dir() / config.SHIPPED_MARK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shipped = None
    return {"ready": True, "articles": len(kb_update.index()),
            "cards": sum(1 for x in cards.read_text(encoding="utf-8").splitlines() if x.strip()) if cards.exists() else 0,
            "last_check": s.get("last_check"), "pending": len(s["pending"]) + len(s["merge_pending"]),
            "ingested_here": len(s.get("ingested") or []), "summary": kb_update.summary(),
            "shipped": shipped}


def build(hooks: "LocalHooks | None" = None):
    """道具をそろえたサーバー（試験でも使う）。serverInfo の版は、この処理の版（2026-10-12。前は "app-1" のままだった）"""
    import mcp_proto
    import proto_runner
    proto_runner.LOCAL = hooks or LocalHooks()
    proto_runner._lock = FileRLock(config.LOCK_DIR / "runner.lock")   # noqa: SLF001
    return mcp_proto._build_server(user_of=lambda ctx: config.OWNER, local=True, version=VERSION)   # noqa: SLF001


def main() -> int:
    hooks = LocalHooks()   # 実行ファイルの印は、起動してすぐ覚える（版のずれの見分け）
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
    system.register_tool("mcp")   # 新しい版のメニューのアプリが「前の版の道具が動いているか」を見分ける印
    config.seed_data(log)
    caller = _caller()
    if caller in ("Claude", "ChatGPT"):
        hooks.ai = caller
    log.info("=== AI の道具を開きます（%s %s、データ %s、呼び元 %s）", config.APP_NAME, VERSION, config.DATA_DIR, caller)
    try:
        server = build(hooks)
        preload(log)
        server.run("stdio")
    except Exception:
        log.exception("AI の道具が止まりました")
        raise
    finally:
        log.info("=== AI の道具を閉じました")
    return 0


def _caller() -> str:
    """道具を起こしたアプリ（Claude は Claude.app の中の disclaimer、ChatGPT は codex）。記録で見分けるため"""
    try:
        r = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=5)
        name = r.stdout.strip()
        return "ChatGPT" if "/ChatGPT.app/" in name or name.endswith("/codex") else \
               "Claude" if "/Claude.app/" in name else (Path(name).name or "?")
    except Exception:
        return "?"
