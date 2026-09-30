#!/usr/bin/env python3
"""
Mac ⇄ Windows のファイル受け渡し用サーバー。

  GET  /            → 配信対象の一覧（Windows の Chrome で開く）
  GET  /<file>      → そのファイルを渡す
  POST /upload/<名前> → 本文を ~/win-transfer/inbox/<名前> に保存する

配信元は2箇所を順に探す。リポジトリの docs/ を直接配信するので、
手順書やスクリプトを直したら**コピーせずにそのまま反映される**。

  1. <リポジトリ>/docs      … 手順書・PowerShellスクリプト
  2. ~/win-transfer         … spatest.py など、リポジトリ外の配布物

Windows 側は PowerShell 標準のコマンドだけで使えるので、何も入れなくてよい
（curl は使わない。--data-binary の @ファイル指定が空白入りパスで失敗するため）。

使い方（Mac）:
    python3 <リポジトリ>/docs/mac-share-server.py
    # 止めるときは Ctrl+C

使い方（Windows / PowerShell）:
    Invoke-WebRequest -Uri "http://192.168.0.52:8765/windows-setup.html" -OutFile "windows-setup.html" -UseBasicParsing
    Invoke-RestMethod  -Uri "http://192.168.0.52:8765/upload/out.txt" -Method Post -InFile "C:\\path\\to\\out.txt"
"""

import html
import http.server
import io
import os
import pathlib
import re
import sys
import urllib.parse

DOCS = pathlib.Path(__file__).resolve().parent
TRANSFER = pathlib.Path.home() / "win-transfer"
ROOTS = [DOCS, TRANSFER]
INBOX = TRANSFER / "inbox"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
MAX_UPLOAD = 32 * 1024 * 1024   # 32MB。ログやjsonlを想定した上限


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def translate_path(self, path: str) -> str:
        """複数の配信元を順に探す。最初に見つかったものを返す"""
        rel = urllib.parse.urlparse(path).path.lstrip("/")
        rel = urllib.parse.unquote(rel)
        if rel in ("", "/"):
            return str(DOCS)
        # ディレクトリを抜け出す指定は受け付けない
        candidate = os.path.normpath(rel)
        if candidate.startswith("..") or os.path.isabs(candidate):
            return str(DOCS / "__notfound__")
        for root in ROOTS:
            p = root / candidate
            if p.exists():
                return str(p)
        return str(DOCS / candidate)

    def list_directory(self, path):
        """2つの配信元をまとめて一覧にする"""
        rows = []
        for root in ROOTS:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if entry.name.startswith(".") or entry.name == "inbox":
                    continue
                label = html.escape(entry.name)
                size = "" if entry.is_dir() else f"{entry.stat().st_size:,} バイト"
                rows.append(
                    f'<tr><td><a href="/{urllib.parse.quote(entry.name)}">{label}</a></td>'
                    f'<td>{size}</td><td>{html.escape(str(root))}</td></tr>'
                )
        body = (
            "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
            "<title>win-transfer</title>"
            "<style>body{font:14px -apple-system,sans-serif;margin:32px}"
            "table{border-collapse:collapse}td{border:1px solid #ddd;padding:5px 10px}"
            "a{text-decoration:none}</style></head><body>"
            "<h2>配信中のファイル</h2>"
            "<p><a href='/windows-setup.html'><b>→ 作業手順書を開く</b></a></p>"
            "<table>" + "".join(rows) + "</table></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def _safe_name(self, raw: str) -> str:
        """パス区切りや .. を落として、ファイル名だけにする"""
        name = os.path.basename(raw.strip("/"))
        name = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
        return name or "upload.bin"

    def _read_exact(self, length: int) -> bytes:
        buf = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            buf += chunk
            remaining -= len(chunk)
        return bytes(buf)

    def _read_chunked(self) -> bytes:
        buf = bytearray()
        while True:
            line = self.rfile.readline().strip()
            if not line:
                break
            size = int(line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()   # 末尾の空行
                break
            if len(buf) + size > MAX_UPLOAD:
                raise ValueError(f"too large (max {MAX_UPLOAD} bytes)")
            buf += self._read_exact(size)
            self.rfile.readline()       # チャンク後の CRLF
        return bytes(buf)

    def do_POST(self):
        if not self.path.startswith("/upload/"):
            self.send_error(404, "POST is only accepted at /upload/<name>")
            return

        name = self._safe_name(self.path[len("/upload/"):])
        INBOX.mkdir(parents=True, exist_ok=True)
        dest = INBOX / name

        # 送り手によって Content-Length だったり chunked だったりする。
        # PowerShell の Invoke-RestMethod は chunked を使うことがあるので両方受ける。
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in encoding:
                body = self._read_chunked()
            else:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    self.send_error(400, "empty body")
                    return
                if length > MAX_UPLOAD:
                    self.send_error(413, f"too large (max {MAX_UPLOAD} bytes)")
                    return
                body = self._read_exact(length)
        except ValueError as e:
            self.send_error(400, f"bad request: {e}")
            return

        if not body:
            self.send_error(400, "empty body")
            return

        with open(dest, "wb") as f:
            f.write(body)

        body = f"saved: inbox/{name} ({dest.stat().st_size} bytes)\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"  [受信] inbox/{name}  {dest.stat().st_size} バイト", flush=True)

    # PUT でも同じ扱いにしておく（curl -T を使いたくなる場合がある）
    do_PUT = do_POST

    def end_headers(self):
        # 手順書を更新したのに古いものが表示される事故を防ぐ
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")


def main():
    TRANSFER.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 60)
    print(f"  配信元 1     : {DOCS}   ← 手順書・スクリプト（編集が即反映）")
    print(f"  配信元 2     : {TRANSFER}")
    print(f"  受信フォルダ : {INBOX}")
    print(f"  Windows から : http://<このMacのIP>:{PORT}/")
    print(f"  止めるには   : Ctrl+C")
    print("=" * 60, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")


if __name__ == "__main__":
    main()
