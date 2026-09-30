"""
成果物のダウンロードの口（docs/IMPLEMENTATION_HANDOVER.md 6-3、F3）。

  https://<公開ドメイン>/dl/<分析ごとの鍵>/<名前>

- 鍵は分析ごとの乱数（analysis.json の download_key）。接続の秘密の URL とは別なので、メールに載せても接続は漏れない
- 返すのは決まった名前のファイルだけ（下の FILES）。それ以外・知らない鍵は 404
- Basic 認証の外（main.py で /dl/ を除外）。一番外側の ASGI ミドルウェアで受ける
- 読み込みに失敗しても既存の Web サービスは動く（main.py 側で try/except）
"""
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("download")

BASE_DIR = Path(__file__).resolve().parent
ANALYSES_DIR = Path(os.environ.get("UGC_ANALYSES_DIR", BASE_DIR / "output" / "analyses"))
PREFIX = "/dl/"
# 名前 → 分析フォルダの中の場所
FILES = {
    "REPORT.md": "outputs/REPORT.md",
    "NOTE_BODY.md": "outputs/NOTE_BODY.md",
    "EDITOR_NOTES.md": "outputs/EDITOR_NOTES.md",
    "data.zip": "outputs/data.zip",
    "videos_review.csv": "derived/review/videos_review.csv",
}


def find(key: str):
    """鍵 → 分析フォルダ（知らなければ None）"""
    if not re.match(r"^[A-Za-z0-9_-]{16,}$", key or ""):
        return None
    if not ANALYSES_DIR.exists():
        return None
    for d in ANALYSES_DIR.iterdir():
        try:
            meta = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, NotADirectoryError):
            continue
        if meta.get("download_key") == key:
            return d, meta
    return None


async def _plain(send, status: int, body: bytes):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class DownloadGate:
    """/dl/<鍵>/<名前> だけを受ける ASGI ミドルウェア。それ以外は素通し"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(PREFIX):
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in ("GET", "HEAD"):
            await _plain(send, 405, b"405 Method Not Allowed")
            return
        parts = scope["path"][len(PREFIX):].split("/")
        hit = find(parts[0]) if len(parts) == 2 else None
        rel = FILES.get(parts[1]) if len(parts) == 2 else None
        path = (hit[0] / rel) if hit and rel else None
        if not path or not path.is_file():
            logger.warning("ダウンロード: 見つからない要求を 404 で返しました hint=%s name=%s",
                           (parts[0] or "")[:4], parts[1] if len(parts) == 2 else "?")
            await _plain(send, 404, b"404 Not Found")
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(parts[1])[0] or "application/octet-stream"
        if parts[1].endswith(".md"):
            ctype = "text/markdown"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        title = re.sub(r'[\\/:*?"<>|\s]+', "_", hit[1].get("title") or hit[1].get("analysis_id") or "analysis")
        fname = f"{title}_{parts[1]}"
        logger.info("ダウンロード: %s %s（%d バイト）", hit[1].get("analysis_id"), parts[1], len(data))
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(data)).encode()),
                                (b"content-disposition", f"attachment; filename*=UTF-8''{quote(fname)}".encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else data})


class _MaskKeyFilter(logging.Filter):
    """アクセスログの /dl/<鍵> を伏せる"""
    _re = re.compile(r"/dl/[^/\s\"?]+")

    def filter(self, record):
        try:
            if record.args and isinstance(record.args, tuple):
                record.args = tuple(self._re.sub("/dl/***", a) if isinstance(a, str) else a for a in record.args)
        except Exception:
            pass
        return True


def attach(app) -> None:
    app.add_middleware(DownloadGate)
    logging.getLogger("uvicorn.access").addFilter(_MaskKeyFilter())
