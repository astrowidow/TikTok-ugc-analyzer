import base64
import binascii
import logging
import os
import queue
import secrets
import threading
import uuid

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from log_setup import LOG_FILE, get_logger
from scraper import (
    jobs, init_job, update_job, run_job, job_log, _search_backends,
    PHASES, PHASE_LABELS, estimate_phase_seconds, remaining_seconds,
)

logger = get_logger("web")

# ---------------------------------------------------------------------------
# Basic認証（ngrokで公開したときのパスワードロック）
# 環境変数 UGC_USER / UGC_PASS で上書きできる
# ---------------------------------------------------------------------------
BASIC_USER = os.environ.get("UGC_USER", "ymafia")
BASIC_PASS = os.environ.get("UGC_PASS", "ymafia")

app = FastAPI(title="UGCAnalyzer Web")

templates = Jinja2Templates(directory="templates")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _is_authorized(header: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return False
    # タイミング攻撃対策のため compare_digest を使う
    user_ok = secrets.compare_digest(username, BASIC_USER)
    pass_ok = secrets.compare_digest(password, BASIC_PASS)
    return user_ok and pass_ok


def _client_ip(request: Request) -> str:
    """ngrok経由だと実IPはヘッダに入るので、あればそちらを使う"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not _is_authorized(request.headers.get("Authorization", "")):
        header = request.headers.get("Authorization", "")
        # 認証情報を送ってきたのに弾かれた場合だけ記録する
        # （初回アクセスは必ずヘッダ無しで来るので、それは記録しない）
        if header:
            logger.warning("認証に失敗しました: ip=%s path=%s", _client_ip(request), request.url.path)
        return Response(
            content="401 Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="UGCAnalyzer"'},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# 順番待ちキュー（Chromeを同時に何個も起動させないため、常に1件ずつ処理する）
# ---------------------------------------------------------------------------
job_queue: "queue.Queue[dict]" = queue.Queue()
_queue_lock = threading.Lock()
_pending_ids: list = []   # 実行待ちのjob_id（投入順）
_running_id = None        # 現在実行中のjob_id


def _worker():
    global _running_id
    while True:
        payload = job_queue.get()
        job_id = payload["job_id"]
        with _queue_lock:
            if job_id in _pending_ids:
                _pending_ids.remove(job_id)
            _running_id = job_id
            waiting = len(_pending_ids)
        job_log(job_id, f"キューから取り出して実行を開始します（残りの待ち: {waiting}件）")
        try:
            run_job(**payload)
        except Exception as e:
            update_job(job_id, status="error", error=str(e), message="エラーが発生しました。")
            job_log(job_id, f"ワーカーで例外が発生しました: {e}", logging.ERROR, exc_info=True)
        finally:
            with _queue_lock:
                _running_id = None
            job_queue.task_done()


threading.Thread(target=_worker, name="ugc-worker", daemon=True).start()


def _queue_snapshot():
    """実行中と待機列を一貫した状態で取り出す。以降の計算はロック外で行う"""
    with _queue_lock:
        return _running_id, list(_pending_ids)


def _jobs_ahead(job_id: str) -> int:
    """自分の前に何件残っているか（実行中の1件を含む）"""
    running_id, pending = _queue_snapshot()
    if running_id == job_id:
        return 0
    ahead = pending.index(job_id) if job_id in pending else 0
    if running_id is not None:
        ahead += 1
    return ahead


def _queue_eta(job_id: str) -> int:
    """
    順番待ちのジョブが開始されるまでの推定秒数。

    「実行中の人に出ている残り時間」＋「自分より前に並んでいる人の推定所要時間」。
    前に並んでいる人はまだ動いていないので、投入時の条件（スクロール数・セット数）
    から推定する。
    """
    running_id, pending = _queue_snapshot()
    if running_id == job_id:
        return 0

    eta = 0
    if running_id is not None:
        running = jobs.get(running_id)
        if running:
            eta += remaining_seconds(running)

    for pending_id in pending:
        if pending_id == job_id:
            break
        params = (jobs.get(pending_id) or {}).get("params") or {}
        eta += int(sum(estimate_phase_seconds(**params).values()))

    return eta


def _asset_version(relative_path: str) -> str:
    """静的ファイルの更新時刻。URLに付けてブラウザのキャッシュを確実に破棄する"""
    try:
        return str(int(os.path.getmtime(relative_path)))
    except OSError:
        return "0"


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"css_version": _asset_version("static/style.css")},
    )


@app.post("/api/analyze")
async def analyze_url(
    request: Request,
    mode: str = Form("url"),
    base_url: str = Form(""),
    project_name: str = Form(""),
    artist_name: str = Form(""),
    song_name: str = Form(""),
    scroll_num: int = Form(30),
    set_num: int = Form(3),
    is_headless_main: bool = Form(True),
):
    mode = "auto" if mode == "auto" else "url"
    client = _client_ip(request)

    if mode == "auto":
        if not artist_name.strip() or not song_name.strip():
            logger.warning("入力不備で受付を拒否しました: ip=%s mode=auto (アーティスト名/楽曲名が空)", client)
            return JSONResponse(
                content={"error": "アーティスト名と楽曲名を入力してください。"},
                status_code=400,
            )
    else:
        if not base_url.strip() or not project_name.strip():
            logger.warning("入力不備で受付を拒否しました: ip=%s mode=url (URL/プロジェクト名が空)", client)
            return JSONResponse(
                content={"error": "解析対象URLとプロジェクト名を入力してください。"},
                status_code=400,
            )

    job_id = str(uuid.uuid4())
    # 順番待ちの人のETA計算に使うので、実行条件は受付の時点で持たせておく
    init_job(job_id, mode=mode, scroll_num=scroll_num, set_num=set_num)

    payload = {
        "job_id": job_id,
        "mode": mode,
        "base_url": base_url.strip(),
        "project_name": project_name.strip(),
        "artist_name": artist_name.strip(),
        "song_name": song_name.strip(),
        "scroll_num": scroll_num,
        "set_num": set_num,
        "is_headless_main": is_headless_main,
    }

    with _queue_lock:
        _pending_ids.append(job_id)
    job_queue.put(payload)

    ahead = _jobs_ahead(job_id)
    eta = _queue_eta(job_id)
    job_log(job_id, f"受付: ip={client} mode={mode} 前に{ahead}件待ち "
                    f"開始まで約{eta // 60}分 / 入力={payload}")

    return JSONResponse(content={"job_id": job_id, "queue_ahead": ahead, "eta_seconds": eta})


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)

    data = dict(jobs[job_id])

    # 画面のフェーズ表示（①検索 →②収集 →③抽出 →④CSV作成）を駆動する情報
    phase = data.get("phase") if data.get("phase") in PHASES else PHASES[0]
    data["phases"] = PHASES
    data["phase_labels"] = PHASE_LABELS
    data["phase_index"] = PHASES.index(phase)

    if data.get("status") == "queued":
        ahead = _jobs_ahead(job_id)
        data["queue_ahead"] = ahead
        data["eta_seconds"] = _queue_eta(job_id)
        data["message"] = (
            "まもなく開始します..." if ahead == 0
            else f"順番待ち中です... (あなたの前に {ahead} 件)"
        )

    return JSONResponse(content=data)


@app.get("/api/download/{job_id}")
async def download_csv(request: Request, job_id: str):
    if job_id not in jobs:
        logger.warning("ダウンロード要求のジョブが見つかりません: ip=%s job=%s", _client_ip(request), job_id[:8])
        return JSONResponse(content={"error": "Job not found"}, status_code=404)

    job = jobs[job_id]
    if job["status"] != "completed" or not job.get("csv_path"):
        job_log(job_id, f"未完了のジョブにダウンロード要求がありました (status={job['status']})", logging.WARNING)
        return JSONResponse(content={"error": "Job not completed or file not found"}, status_code=400)

    csv_path = job["csv_path"]
    if not os.path.exists(csv_path):
        job_log(job_id, f"CSVファイルが見つかりません: {csv_path}", logging.ERROR)
        return JSONResponse(content={"error": "File not found on server"}, status_code=404)

    filename = os.path.basename(csv_path)
    job_log(job_id, f"CSVをダウンロードしました: ip={_client_ip(request)} file={filename}")
    return FileResponse(path=csv_path, filename=filename, media_type='text/csv')


def log_startup_info(host: str, port: int):
    """起動時の設定を記録する。「動かない」の原因切り分けはここから始まる"""
    backends = [name for name, _ in _search_backends()]
    logger.info("=" * 70)
    logger.info("UGCAnalyzer を起動します")
    logger.info("  待ち受け      : http://%s:%s", host, port)
    logger.info("  ログイン       : ユーザー名=%s / パスワードは環境変数 UGC_PASS で変更可", BASIC_USER)
    logger.info("  検索の優先順位  : %s", " → ".join(backends))
    if not any("API" in name for name in backends):
        logger.warning("  検索APIキーが未設定です。DuckDuckGoはBot判定で失敗することがあります "
                       "(TAVILY_API_KEY を設定すると安定します)")
    logger.info("  ログファイル    : %s", LOG_FILE)
    logger.info("=" * 70)


if __name__ == "__main__":
    host, port = "0.0.0.0", 8000
    log_startup_info(host, port)
    # log_config=None にすると uvicorn のアクセスログもこのロガー（＝ログファイル）に流れる
    uvicorn.run(app, host=host, port=port, log_config=None)
