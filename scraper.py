import logging
import os
import re
import time
import datetime
import json
import urllib.parse

import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from log_setup import get_logger, brief

logger = get_logger("scraper")

# 進捗状況を保持するためのグローバル辞書（簡易的な実装）
# 本番環境ではRedisやDBを使うのが推奨されますが、ローカル用途ならこれで十分です
jobs = {}


# ---------------------------------------------------------------------------
# 進捗フェーズと所要時間の推定
#
# 1件の解析は4フェーズに分かれる。UI側はこれを「1本の通しプログレスバー」で
# 表示するので、収集が100%になってから抽出が0%で仕切り直し、にはならない。
#
# 重み配分（どのフェーズが全体の何割か）はジョブ開始時に推定所要秒数から計算し、
# 実行中は絶対に変更しない。実行中に配分を変えるとプログレスバーが逆走するため、
# 実測ペースは ETA の計算にだけ反映し、進捗率には反映しない。
# ---------------------------------------------------------------------------

# scraper本体が実際に待つ秒数。推定式と実処理が食い違わないよう、ここで一元管理する
SCROLL_PAUSE_TIME = 3     # 収集フェーズ: スクロール1回ごとの待ち
PAGE_LOAD_TIME = 5        # 収集フェーズ: ページを開いた後の待ち
HTML_LOAD_TIME = 2        # 抽出フェーズ: ページを開いた後の待ち

# 以下は実測値。TikTokの楽曲ページに対してヘッドレスChromeを実際に走らせて計測した。
# sleepの秒数そのものではなく「sleep + Seleniumと通信のオーバーヘッド」で決めている。
# 回線やマシンを変えたら測り直すこと（特に DEFAULT_SECONDS_PER_URL は回線速度に効く）。
SECONDS_PER_SCROLL = 3.02      # 実測3.018秒。リンク932件までDOMが育っても劣化しなかった
SECONDS_PER_PAGE_LOAD = 6.0    # 実測5.69〜6.31秒（sleep 5秒 + 実際の読み込み）
DEFAULT_SECONDS_PER_URL = 3.2  # 実測3.158秒 + WebDriver再起動1.12秒を30件で按分
# 検索は1発で当たれば実測0.77秒だが、全部Bot判定されて動画ページからの逆引きに
# 落ちると20〜40秒かかる。その中間を取っている
SEARCH_ESTIMATE_SECONDS = 10
# いいね補間 + to_csv は900件でも実測0.011秒。ほぼ driver.quit() の時間
FINALIZE_ESTIMATE_SECONDS = 1

# 収集フェーズが終わるまで取得件数は分からないので、スクロール数から見積もる。
# 注意: 1つの動画カードに <a> は2本あるので、要素数ではなく重複除去後で数えること。
# 実測は12スクロールで「要素758件 / ユニーク379件」= 1スクロールあたり約31件。
# セット数は同じページを頭から読み直すだけなので、ユニーク件数はほとんど増えない。
LINKS_PER_SCROLL = 31
# 無限スクロールはいずれ打ち止めになる。実運用の全量取得は880〜1300件だった
MAX_EXPECTED_URLS = 900

PHASES = ["search", "collect", "extract", "finalize"]
PHASE_LABELS = {
    "search": "楽曲ページを検索",
    "collect": "URLを収集",
    "extract": "詳細データを抽出",
    "finalize": "CSVを作成",
}

# 直近の完了ジョブの実績（秒/件）。次のジョブの推定精度を上げるために覚えておく
_measured_seconds_per_url = None


def seconds_per_url() -> float:
    """詳細抽出1件あたりの秒数。実績があればそれを使う"""
    return _measured_seconds_per_url or DEFAULT_SECONDS_PER_URL


def record_pace(seconds: float):
    """完了したジョブの実測ペースを次回のために記録する"""
    global _measured_seconds_per_url
    if seconds and seconds > 0:
        _measured_seconds_per_url = seconds


def estimate_url_count(scroll_num: int, set_num: int = 1) -> int:
    """収集フェーズで何件のURLが集まりそうかの見積もり"""
    return max(min(int(scroll_num * LINKS_PER_SCROLL), MAX_EXPECTED_URLS), 1)


def estimate_phase_seconds(mode: str = "url", scroll_num: int = 30, set_num: int = 3,
                           url_count: int = None) -> dict:
    """各フェーズの推定所要秒数。重み配分にもETAにも、この1つの推定を使う"""
    urls = url_count or estimate_url_count(scroll_num, set_num)
    return {
        "search": float(SEARCH_ESTIMATE_SECONDS) if mode == "auto" else 0.0,
        "collect": float(set_num * (SECONDS_PER_PAGE_LOAD + scroll_num * SECONDS_PER_SCROLL)),
        "extract": float(urls * seconds_per_url()),
        "finalize": float(FINALIZE_ESTIMATE_SECONDS),
    }


def _clamp_ratio(value) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _overall_progress(job: dict) -> int:
    """フェーズ内の進捗から、全体の進捗率(0〜100)を出す"""
    weights = job.get("phase_seconds") or estimate_phase_seconds()
    total = sum(weights.values())
    if total <= 0:
        return 0
    phase = job.get("phase") if job.get("phase") in PHASES else PHASES[0]
    index = PHASES.index(phase)
    done = sum(weights.get(p, 0.0) for p in PHASES[:index])
    done += weights.get(phase, 0.0) * _clamp_ratio(job.get("phase_progress"))
    return int(done / total * 100)


def remaining_seconds(job: dict) -> int:
    """
    完了までの残り秒数。

    重みは進捗率と共用するが、抽出フェーズだけは実測ペースで上書きして精度を上げる。
    上書きするのはETAだけで、進捗率には反映しない（反映すると全体%が逆走するため）。
    """
    weights = dict(job.get("phase_seconds") or estimate_phase_seconds())

    total_urls = job.get("total_urls") or 0
    pace = job.get("measured_seconds_per_url")
    if total_urls and pace:
        weights["extract"] = total_urls * pace

    phase = job.get("phase") if job.get("phase") in PHASES else PHASES[0]
    index = PHASES.index(phase)
    remain = weights.get(phase, 0.0) * (1 - _clamp_ratio(job.get("phase_progress")))
    remain += sum(weights.get(p, 0.0) for p in PHASES[index + 1:])
    return max(int(remain), 0)


def _refresh_progress(job_id: str, message: str = None):
    job = jobs.setdefault(job_id, {})
    # 進捗率は必ず単調増加させる。バーが戻るとユーザーはやり直しだと思ってしまう
    job["progress"] = max(job.get("progress") or 0, _overall_progress(job))
    job["eta_seconds"] = remaining_seconds(job)
    if message is not None:
        job["message"] = message


def set_phase(job_id: str, phase: str, message: str = None):
    """次のフェーズへ進む。進捗率はフェーズ境界の値に張り付く（戻らない）"""
    job = jobs.setdefault(job_id, {})
    job["phase"] = phase
    job["phase_progress"] = 0.0
    _refresh_progress(job_id, message)


def set_phase_progress(job_id: str, ratio: float, message: str = None):
    """フェーズ内の進捗(0.0〜1.0)を更新する"""
    job = jobs.setdefault(job_id, {})
    job["phase_progress"] = _clamp_ratio(ratio)
    _refresh_progress(job_id, message)


def finish_job(job_id: str, status: str, message: str, error: str = None):
    """終了状態をまとめて書き込む。バーは必ず100%まで到達させる"""
    job = jobs.setdefault(job_id, {})
    job["phase"] = PHASES[-1]
    job["phase_progress"] = 1.0
    job["progress"] = 100
    job["eta_seconds"] = 0
    job["status"] = status
    job["message"] = message
    if error is not None:
        job["error"] = error


def job_log(job_id: str, message: str, level: int = logging.INFO, exc_info: bool = False):
    """ジョブIDの先頭8文字を付けて記録する。1件の解析を後から追跡できる"""
    logger.log(level, "[%s] %s", (job_id or "--------")[:8], message, exc_info=exc_info)


def init_job(job_id: str, message: str = "順番待ち中...", mode: str = "url",
             scroll_num: int = 30, set_num: int = 3):
    """ジョブをキューに積んだ時点の初期状態を作る"""
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": message,
        "total_urls": 0,
        "current_url_index": 0,
        "csv_path": None,
        "error": None,
        "resolved_url": None,
        "project_name": None,
        # 進捗フェーズ。重み配分はここで確定させ、実行中は変更しない
        "phase": "search" if mode == "auto" else "collect",
        "phase_progress": 0.0,
        "phase_seconds": estimate_phase_seconds(mode, scroll_num, set_num),
        "eta_seconds": None,
        "measured_seconds_per_url": None,
        # 順番待ちの人のETAを計算するとき、他のジョブからこの条件を参照する
        "params": {"mode": mode, "scroll_num": scroll_num, "set_num": set_num},
    }
    jobs[job_id]["eta_seconds"] = remaining_seconds(jobs[job_id])


def update_job(job_id: str, **kwargs):
    job = jobs.setdefault(job_id, {})
    job.update(kwargs)


def safe_filename(name: str) -> str:
    """CSVのファイル名として使えない文字を置き換える"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "").strip()
    return cleaned or "project"


def create_visible_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--mute-audio")
    return webdriver.Chrome(options=options)


def create_headless_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument("--mute-audio")
    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# 検索エンジンから TikTok の楽曲ページURLを探す
# ---------------------------------------------------------------------------

# ブラウザを一切使わず、HTTPリクエストだけで検索する。
# こうすることで画面（GUIセッション）に依存しなくなり、モニタを繋いでいない
# サーバーや画面ロック中のPCでも動く。
#
# 優先順位は「APIキーが設定されている検索API」→「DuckDuckGoのスクレイピング」。
# DuckDuckGoは無料で使えるが、ブラウザ以外のクライアントをBot判定して
# HTTP 202 を返し始めることがあり、一度踏むと数十分単位で復旧しない。
# 無人運用では検索APIのキーを設定しておくこと。
#
#   TAVILY_API_KEY  … 1,000クレジット/月まで無料、カード登録不要（推奨）
#   BRAVE_API_KEY   … 月$5クレジット。カード登録が必要で、超過分は課金される
#   GOOGLE_API_KEY + GOOGLE_CSE_ID … 2027年1月終了予定。新規登録は不可
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "").strip()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "").strip()

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SEARCH_TIMEOUT = 20


def _extract_music_url(href: str):
    """リンクのhrefが https://www.tiktok.com/music/... ならそのURLを返す"""
    if not href:
        return None

    # 検索エンジンのリダイレクト形式 (Google: /url?q=... / DuckDuckGo: /l/?uddg=...)
    if "/url?" in href or "uddg=" in href:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        href = (params.get("q") or params.get("url") or params.get("uddg") or [""])[0]

    if href.startswith("https://www.tiktok.com/music/"):
        # 余計なクエリパラメータは落とす
        return href.split("?")[0].split("#")[0]
    return None


def _collect_music_urls(links):
    """リンクの一覧から楽曲ページURLを重複なく集める"""
    urls = []
    for href in links or []:
        music_url = _extract_music_url(href)
        if music_url and music_url not in urls:
            urls.append(music_url)
    return urls


def _links_in_html(html_text: str):
    """HTMLから href の値を全部取り出す"""
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', html_text or "")]


def _pick_best_music_url(urls):
    """末尾に楽曲IDが付いた公式サウンドページを優先する"""
    for url in urls:
        if re.search(r"-\d{15,}$", url):
            return url
    return urls[0] if urls else None


def _search_tavily(query: str):
    """
    Tavily Search API。無料枠は 1,000クレジット/月・カード登録不要。

    site: 演算子ではなく include_domains でドメインを絞る仕様。
    （site: を外す処理は呼び出し側の supports_site_operator で行っている）
    """
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "query": query,
            "max_results": 20,
            "search_depth": "basic",        # basic は1クレジット、advanced は2クレジット
            "include_domains": ["tiktok.com"],
        },
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
        timeout=SEARCH_TIMEOUT,
    )
    # 原因がログだけで分かるように、よくある失敗は個別のメッセージにする
    if response.status_code == 401:
        raise RuntimeError("TAVILY_API_KEY が正しくないようです (401)")
    if response.status_code in (429, 432):
        raise RuntimeError(f"Tavilyの無料枠を使い切った可能性があります ({response.status_code})")

    response.raise_for_status()
    return [item.get("url", "") for item in response.json().get("results", [])]


# Tavilyは site: 演算子を解釈しないので、クエリから取り除いてもらう
_search_tavily.supports_site_operator = False


def _search_brave(query: str):
    """Brave Search API。2026年2月に無料枠が廃止され、カード登録と従量課金が必要"""
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 20},
        headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
        timeout=SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])
    return [item.get("url", "") for item in results]


def _search_google_cse(query: str):
    """Google Custom Search JSON API。無料枠は 100クエリ/日"""
    response = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query, "num": 10},
        timeout=SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    return [item.get("link", "") for item in response.json().get("items", [])]


class SearchBlocked(RuntimeError):
    """検索サイトにBot判定されたときに投げる"""


def _make_ddg_search(endpoint: str):
    """DuckDuckGoのHTMLをスクレイピングする（キー不要だがBot判定を受けることがある）"""
    def search(query: str):
        # GETは202（Bot対策のチャレンジ）を返すのでPOSTでなければならない
        response = requests.post(
            endpoint,
            data={"q": query},
            headers={"User-Agent": BROWSER_UA},
            timeout=SEARCH_TIMEOUT,
        )
        # 202はエラー扱いされない成功ステータスだが、中身はCAPTCHAのチャレンジ画面。
        # 「検索結果0件」と区別できるよう、ここで明示的に弾く
        if response.status_code != 200:
            raise SearchBlocked(f"HTTP {response.status_code} / Bot判定のチャレンジ画面が返りました")
        if "bots use duckduckgo" in response.text.lower():
            raise SearchBlocked("CAPTCHAのチャレンジ画面が返りました")

        response.raise_for_status()
        return _links_in_html(response.text)
    return search


def _search_backends():
    """使える検索手段を優先順に並べる。APIキーがあるものを先に使う"""
    backends = []
    if TAVILY_API_KEY:
        backends.append(("Tavily", _search_tavily))
    if BRAVE_API_KEY:
        backends.append(("Brave Search API", _search_brave))
    if GOOGLE_API_KEY and GOOGLE_CSE_ID:
        backends.append(("Google Custom Search API", _search_google_cse))
    backends.append(("DuckDuckGo", _make_ddg_search("https://html.duckduckgo.com/html/")))
    backends.append(("DuckDuckGo(lite)", _make_ddg_search("https://lite.duckduckgo.com/lite/")))
    return backends


# 検索が動画ページしか返さなかったとき、何本まで開いて楽曲IDを調べるか
MAX_VIDEO_PROBES = 6

_VIDEO_URL_PATTERN = re.compile(r"^https://www\.tiktok\.com/@[^/]+/(?:video|photo)/\d+")


def _normalize_name(text: str) -> str:
    """曲名・アーティスト名を突き合わせるために、記号や空白を落として小文字化する"""
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]", "", (text or "").lower())


def _music_slug(title: str) -> str:
    """楽曲ページURLの見出し部分。実際はIDだけで一意に決まるので体裁の問題"""
    return re.sub(r"[^0-9A-Za-z]+", "-", title or "").strip("-") or "sound"


def _resolve_music_url_from_videos(job_id: str, video_urls, artist_name: str, song_name: str):
    """
    検索が楽曲ページを返さず、その曲を使った動画ページしか返さなかったときの逃げ道。

    動画ページのJSONには使用楽曲のIDが入っているので、そこから楽曲ページURLを組み立てる。
    URLは末尾のIDだけで決まり、見出し部分（スラッグ）は何でもよいことを確認済み。
    """
    want_song = _normalize_name(song_name)
    want_artist = _normalize_name(artist_name)

    update_job(job_id, message="検索結果の動画から楽曲ページを特定しています...")
    job_log(job_id, f"楽曲ページが直接見つからないため、動画ページから楽曲IDを逆引きします（最大{MAX_VIDEO_PROBES}件）")

    fallback = None
    driver = None
    try:
        driver = create_headless_driver()
        for url in video_urls[:MAX_VIDEO_PROBES]:
            try:
                driver.get(url)
                time.sleep(2)
                script_tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
                data = json.loads(script_tag.get_attribute("innerHTML"))
                music = (data.get("__DEFAULT_SCOPE__", {})
                             .get("webapp.video-detail", {})
                             .get("itemInfo", {})
                             .get("itemStruct", {})
                             .get("music", {}))
            except Exception as e:
                job_log(job_id, f"動画ページの解析に失敗: {url} -> {brief(e)}", logging.DEBUG)
                continue

            music_id = music.get("id")
            title = music.get("title", "")
            author = music.get("authorName", "")
            if not music_id:
                continue

            candidate = f"https://www.tiktok.com/music/{_music_slug(title)}-{music_id}"
            song_ok = bool(want_song) and want_song in _normalize_name(title)
            artist_ok = (want_artist in _normalize_name(author)) if want_artist else True

            if song_ok and artist_ok:
                job_log(job_id, f"楽曲ページを特定しました: '{author} - {title}' -> {candidate}")
                return candidate
            if song_ok and fallback is None:
                fallback = (candidate, title, author)

        if fallback:
            candidate, title, author = fallback
            job_log(
                job_id,
                f"アーティスト名は一致しませんでしたが、曲名が一致する楽曲を採用します: "
                f"'{author} - {title}' -> {candidate}",
                logging.WARNING,
            )
            return candidate

        job_log(job_id, "動画ページからも楽曲ページを特定できませんでした", logging.WARNING)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def find_tiktok_music_url(job_id: str, artist_name: str, song_name: str):
    """
    「アーティスト名 楽曲名」で検索し、
    https://www.tiktok.com/music/ から始まるURLを返す。見つからなければ None。

    ブラウザを使わずHTTPリクエストだけで完結するので、
    画面ロック中やモニタ未接続のサーバーでも動作する。
    """
    keywords = " ".join(part for part in [artist_name, song_name] if part).strip()
    # サイト指定のほうが楽曲ページに当たりやすいので先に試す
    queries = [
        f"site:tiktok.com/music {keywords}",
        f"{keywords} tiktok",
    ]

    backends = _search_backends()
    job_log(job_id, f"検索開始: キーワード='{keywords}' 使用可能な検索手段={[n for n, _ in backends]}")

    blocked_engines = []
    video_candidates = []   # 楽曲ページが無かったときに楽曲IDを逆引きする材料

    # 「検索手段 × クエリ」の総当たり回数を分母にして、検索フェーズの進捗を出す
    total_attempts = max(len(backends) * len(queries), 1)
    attempts = 0

    for engine_name, search in backends:
        supports_site = getattr(search, "supports_site_operator", True)
        for i, raw_query in enumerate(queries):
            query = raw_query if supports_site else raw_query.replace("site:tiktok.com/music", "").strip()
            attempts += 1
            set_phase_progress(job_id, attempts / total_attempts,
                               f"{engine_name}で「{query}」を検索しています...")
            if i > 0:
                time.sleep(1)  # Brave無料枠の 1クエリ/秒 制限に合わせる
            try:
                links = search(query)
            except SearchBlocked as e:
                blocked_engines.append(engine_name)
                job_log(job_id, f"検索がブロックされました: {engine_name} -> {e}", logging.WARNING)
                break  # このバックエンドは諦めて次へ
            except Exception as e:
                job_log(job_id, f"検索に失敗: {engine_name} / '{query}' -> {brief(e)}", logging.WARNING)
                break  # このバックエンドは諦めて次へ
            candidates = _collect_music_urls(links)
            found = _pick_best_music_url(candidates)
            job_log(
                job_id,
                f"検索結果: {engine_name} / '{query}' -> リンク{len(links)}件中 楽曲ページ{len(candidates)}件"
                + (f" 採用={found}" if found else " （該当なし）"),
            )
            if found:
                return found

            # 楽曲ページは無くても、その曲を使った動画ページなら拾えることが多い
            for link in links:
                if _VIDEO_URL_PATTERN.match(link or "") and link not in video_candidates:
                    video_candidates.append(link)

    # どの検索先も楽曲ページを返さなかった場合、動画ページから楽曲IDを逆引きする
    if video_candidates:
        found = _resolve_music_url_from_videos(job_id, video_candidates, artist_name, song_name)
        if found:
            return found

    if blocked_engines:
        update_job(job_id, search_blocked=True)
        job_log(
            job_id,
            f"検索できませんでした。{'・'.join(dict.fromkeys(blocked_engines))} にBot判定でブロックされています。"
            "同じ回線からの連続アクセスが原因で、復旧まで時間がかかります。"
            "TAVILY_API_KEY を設定すればこの問題は起きません。",
            logging.ERROR,
        )
    else:
        job_log(job_id, "すべての検索手段で楽曲ページが見つかりませんでした（結果は返っていますが該当なし）", logging.ERROR)
    return None


# ---------------------------------------------------------------------------
# ジョブ本体
# ---------------------------------------------------------------------------

def run_job(job_id: str, mode: str = "url", base_url: str = "", project_name: str = "",
            artist_name: str = "", song_name: str = "", scroll_num: int = 30,
            set_num: int = 3, is_headless_main: bool = True):
    """
    キューのワーカーから呼ばれる入口。
    mode="auto" なら先に検索でURLを特定してから解析する。
    """
    started_at = time.time()
    job_log(
        job_id,
        f"ジョブ開始: mode={mode} artist='{artist_name}' song='{song_name}' "
        f"project='{project_name}' url='{base_url}' "
        f"scroll={scroll_num} set={set_num} headless={is_headless_main}",
    )

    try:
        if mode == "auto":
            project_name = project_name or " ".join(
                part for part in [artist_name, song_name] if part
            ).strip()
            update_job(job_id, status="running", project_name=project_name)
            set_phase(job_id, "search", "楽曲ページを検索しています...")

            found_url = find_tiktok_music_url(job_id, artist_name, song_name)
            if not found_url:
                if jobs.get(job_id, {}).get("search_blocked"):
                    reason = ("検索サイトからBot判定を受け、楽曲ページを検索できませんでした。"
                              "しばらく時間をおくか、「直接URLモード」でTikTokの楽曲ページURLを"
                              "直接貼り付けてお試しください。")
                else:
                    reason = ("検索してもTikTokの楽曲ページ (https://www.tiktok.com/music/...) が"
                              "見つかりませんでした。アーティスト名・楽曲名を見直すか、"
                              "直接URLモードをお使いください。")
                update_job(job_id, status="error", error=reason,
                           message="エラーが発生しました。", eta_seconds=0)
                return

            base_url = found_url
            update_job(job_id, resolved_url=base_url,
                       message=f"楽曲ページを検出しました: {base_url}")

        run_scraper(
            job_id=job_id,
            base_url=base_url,
            project_name=project_name,
            scroll_num=scroll_num,
            set_num=set_num,
            is_headless_main=is_headless_main,
        )
    except Exception as e:
        update_job(job_id, status="error", error=str(e),
                   message="エラーが発生しました。", eta_seconds=0)
        job_log(job_id, f"ジョブが例外で停止しました: {brief(e)}", logging.ERROR, exc_info=True)
    finally:
        final = jobs.get(job_id, {})
        job_log(
            job_id,
            f"ジョブ終了: status={final.get('status')} 所要={int(time.time() - started_at)}秒"
            + (f" error={final.get('error')}" if final.get("error") else ""),
        )


_VIDEO_LINK_XPATH = '//a[contains(@href, "/video/") or contains(@href, "/photo/")]'


# 件数の集計はブラウザ側で1回だけ実行する。
# Seleniumの get_attribute() は要素ごとに通信が発生するため、同じ集計に
# 実測0.53秒（12スクロール目で0.92秒、件数に比例して増加）かかってしまう。
# このJSなら実測0.002秒で、結果は同一であることを確認済み。
_COUNT_LINKS_JS = (
    "return new Set(Array.from(document.querySelectorAll("
    "'a[href*=\"/video/\"], a[href*=\"/photo/\"]')).map(a => a.href)).size;"
)


def _count_collected(driver, video_urls) -> int:
    """
    画面に出す「収集済み件数」。表示専用なので、失敗しても収集そのものは止めない。
    各セットは同じページを頭から読み直すので、累計と今の表示件数の多いほうを採る。
    """
    try:
        return max(len(video_urls), int(driver.execute_script(_COUNT_LINKS_JS)))
    except Exception:
        return len(video_urls)


def run_scraper(job_id: str, base_url: str, project_name: str, scroll_num: int, set_num: int = 3, is_headless_main: bool = True):
    """
    UGCのスクレイピングを実行し、結果をCSVに保存する。
    is_headless_main: 最初のURLリスト収集をバックグラウンド(非表示)で実行するかどうか
    """
    if job_id not in jobs:
        init_job(job_id)

    # progress はここでリセットしない。自動検索モードでは検索フェーズで既に
    # 進んでおり、0に戻すとプログレスバーが巻き戻ってしまう
    update_job(
        job_id,
        status="running",
        message="初期化中...",
        total_urls=0,
        current_url_index=0,
        csv_path=None,
        error=None,
        project_name=project_name,
    )

    driver = None
    try:
        # WebDriverを設定して起動
        if is_headless_main:
            driver = create_headless_driver()
        else:
            driver = create_visible_driver()

        video_urls = set()

        set_phase(job_id, "collect",
                  f"ページを巡回してURLを収集しています... (セット数: {set_num})")
        job_log(job_id, f"URL収集開始: {base_url} (セット{set_num} × スクロール{scroll_num}回, ヘッドレス={is_headless_main})")

        # スクロール1回ごとに進捗を出す。ここは数分〜数時間かかるので、
        # 更新しないと画面が固まったようにしか見えない
        total_scrolls = max(set_num * scroll_num, 1)

        for set_i in range(set_num):
            driver.get(base_url)
            time.sleep(PAGE_LOAD_TIME)

            for scroll_i in range(scroll_num):
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
                time.sleep(SCROLL_PAUSE_TIME)
                done_scrolls = set_i * scroll_num + scroll_i + 1
                set_phase_progress(
                    job_id,
                    done_scrolls / total_scrolls,
                    f"URLを収集しています... {set_i + 1}/{set_num}セット "
                    f"({scroll_i + 1}/{scroll_num}スクロール) "
                    f"収集済み{_count_collected(driver, video_urls)}件",
                )

            video_elements = driver.find_elements(By.XPATH, _VIDEO_LINK_XPATH)
            for element in video_elements:
                video_url = element.get_attribute('href')
                if video_url:
                    video_urls.add(video_url)
            job_log(job_id, f"URL収集 {set_i + 1}/{set_num}セット完了: このセット{len(video_elements)}件 / 累計ユニーク{len(video_urls)}件")

        driver.quit()
        driver = None

        final_video_urls = list(video_urls)
        total_videos = len(final_video_urls)

        if total_videos == 0:
            finish_job(job_id, "completed",
                       "URLが1件も見つかりませんでした。URLや条件を確認してください。")
            job_log(
                job_id,
                f"URLが1件も取得できませんでした。対象URLが正しいか、TikTok側の表示制限を確認してください: {base_url}",
                logging.WARNING,
            )
            return

        jobs[job_id]["total_urls"] = total_videos
        set_phase(job_id, "extract",
                  f"合計 {total_videos} 件の動画/写真URLを取得しました。詳細データの抽出を開始します...")
        job_log(job_id, f"詳細データ抽出開始: 対象{total_videos}件")

        video_data = []
        start_time = time.time()
        load_errors = 0
        extract_errors = 0

        driver = create_headless_driver()
        restart_threshold = 30

        for idx, url in enumerate(final_video_urls):
            jobs[job_id]["current_url_index"] = idx + 1

            if idx % restart_threshold == 0 and idx != 0:
                jobs[job_id]["message"] = "安定性のためWebDriverを再起動しています..."
                job_log(job_id, f"WebDriverを再起動します ({idx}/{total_videos}件処理済み)", logging.DEBUG)
                driver.quit()
                driver = create_headless_driver()

            try:
                driver.get(url)
            except Exception as e:
                load_errors += 1
                job_log(job_id, f"ページ読み込み失敗: {url} -> {brief(e)}", logging.DEBUG)
                continue

            time.sleep(HTML_LOAD_TIME)

            try:
                if "/photo/" in url:
                    video_id_str = url.split("/")[-1]
                    video_id = int(video_id_str)
                    timestamp_bits = video_id >> 32
                    create_time = datetime.datetime.fromtimestamp(timestamp_bits).strftime('%Y-%m-%d %H:%M:%S')

                    video_data.append({
                        'URL': url,
                        'Created Date': create_time,
                        'Description': '',
                        'Likes': None,
                        'Shares': '',
                        'Comments': '',
                        'Plays': '',
                        'Saves': '',
                        'Reposts': '',
                        'Type': 'Photo'
                    })
                else:
                    script_tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
                    json_text = script_tag.get_attribute('innerHTML')
                    data = json.loads(json_text)
                    video_info = data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct', {})

                    create_time = int(video_info.get('createTime', None))
                    desc = video_info.get('desc', '')
                    author_info = video_info.get('author', {})
                    unique_id = author_info.get('uniqueId', '')

                    stats = video_info.get('statsV2', {})
                    if create_time:
                        create_time = datetime.datetime.fromtimestamp(create_time, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                    digg_count = int(stats.get('diggCount', 0))
                    share_count = stats.get('shareCount', 0)
                    comment_count = stats.get('commentCount', 0)
                    play_count = stats.get('playCount', 0)
                    collect_count = stats.get('collectCount', 0)
                    repost_count = stats.get('repostCount', 0)

                    video_data.append({
                        'URL': url,
                        'Created Date': create_time,
                        'Description': desc,
                        'Likes': digg_count,
                        'Shares': share_count,
                        'Comments': comment_count,
                        'Plays': play_count,
                        'Saves': collect_count,
                        'Reposts': repost_count,
                        'Type': 'Video',
                        'Username': unique_id,
                    })

            except Exception as e:
                extract_errors += 1
                job_log(job_id, f"詳細データの抽出失敗: {url} -> {brief(e)}", logging.DEBUG)

            # 実測ペースを記録する。ETAはこれを使って精度を上げるが、
            # 進捗率の重み配分には反映しない（反映すると全体%が逆走するため）
            jobs[job_id]["measured_seconds_per_url"] = (time.time() - start_time) / (idx + 1)
            set_phase_progress(
                job_id,
                (idx + 1) / total_videos,
                f"詳細データを抽出しています... ({idx + 1}/{total_videos})",
            )

        failed = load_errors + extract_errors
        summary_level = logging.WARNING if failed > total_videos * 0.3 else logging.INFO
        job_log(
            job_id,
            f"詳細データ抽出完了: 成功{len(video_data)}件 / 読み込み失敗{load_errors}件 / "
            f"抽出失敗{extract_errors}件 (対象{total_videos}件, {int(time.time() - start_time)}秒)"
            + ("  ※失敗が多い場合は UGC_LOG_LEVEL=DEBUG で個別のURLを確認できます" if failed else ""),
            summary_level,
        )
        # 次のジョブの推定精度を上げるため、実測ペースを覚えておく
        record_pace(jobs[job_id].get("measured_seconds_per_url"))

        # いいね数の補間
        set_phase(job_id, "finalize", "データを整形し、CSVを作成しています...")
        for idx, data in enumerate(video_data):
            if data['Type'] == 'Photo' and data['Likes'] is None:
                previous_likes, next_likes = None, None
                previous_idx, next_idx = None, None

                for i in range(idx - 1, -1, -1):
                    if i >= 0 and video_data[i]['Likes'] is not None:
                        previous_likes = video_data[i]['Likes']
                        previous_idx = i
                        break

                for i in range(idx + 1, len(video_data)):
                    if i < len(video_data) and video_data[i]['Likes'] is not None:
                        next_likes = video_data[i]['Likes']
                        next_idx = i
                        break

                if previous_likes is not None and next_likes is not None:
                    data['Likes'] = previous_likes + ((next_likes - previous_likes) * (idx - previous_idx) // (next_idx - previous_idx))
                elif previous_likes is not None:
                    data['Likes'] = previous_likes
                elif next_likes is not None:
                    data['Likes'] = next_likes

        df = pd.DataFrame(video_data)
        if not df.empty:
            df.insert(0, 'Index', range(len(df)))
            df['Project Name'] = project_name

            output_dir = "output"
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"{safe_filename(project_name)}_{timestamp}.csv"
            csv_path = os.path.join(output_dir, file_name)

            df.to_csv(csv_path, index=False, encoding='utf-8-sig')

            jobs[job_id]["csv_path"] = csv_path
            finish_job(job_id, "completed", "処理が完了しました！")
            job_log(job_id, f"CSV書き出し完了: {csv_path} ({len(df)}行)")
        else:
            finish_job(job_id, "error", "エラーが発生しました。",
                       error="データが取得できませんでした")
            job_log(job_id, "URLは取得できましたが、詳細データが1件も抽出できませんでした", logging.ERROR)

        driver.quit()
        driver = None

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["message"] = "エラーが発生しました。"
        jobs[job_id]["eta_seconds"] = 0
        job_log(job_id, f"解析中に例外が発生しました: {brief(e)}", logging.ERROR, exc_info=True)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
