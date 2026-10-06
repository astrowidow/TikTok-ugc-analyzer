#!/usr/bin/env python3
"""通し試験（docs/FULL_TEST_PROMPT.md）の AI 役が使う、UGC Analyzer の道具の呼び出し口。

運営の Mac に入っているアプリ（/Applications/UGC Analyzer.app）を、Claude デスクトップと同じ形（`UGC Analyzer --mcp`、標準入出力）で起こし、
道具を1回呼ぶ。呼び出しと返事・利用者と AI の発言はすべて、今いるフォルダ（走行のフォルダ）の log.jsonl に残す。
output/case_kyawa_20261006/ugc.py（事例づくり）を、走行のフォルダを選べるようにしたもの。

  python ugc.py tools                         … AI に見える案内（instructions）と道具の説明
  python ugc.py call <道具> '<引数の JSON>'     … 道具を1回呼ぶ。返事の文を表示（長いと calls/ に書いて場所だけ）
  python ugc.py submit <task_id> <ファイル>     … 道具 submit を、出力をファイルから読んで呼ぶ
  python ugc.py say <user|ai> <ファイル>       … 会話の発言を記録に足す（利用者の頼み・AI の返事）

走行のフォルダに HOME_DIR というファイルがあれば、その中に書いた置き場（運営の置き場の写し）でアプリを起こす。
そのときメニューバーのアプリは起こさない（TikTok に触らない）。設定・指示書・界隈の確認を省く試しに使う。
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

RUN = Path.cwd()
LOG = RUN / "log.jsonl"
OUT = RUN / "calls"
EXE = "/Applications/UGC Analyzer.app/Contents/MacOS/UGC Analyzer"
PRINT_MAX = 25_000


def sandbox_home() -> str | None:
    p = RUN / "HOME_DIR"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def log(row: dict) -> None:
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}
    if sandbox_home():
        row["home"] = sandbox_home()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def params() -> StdioServerParameters:
    home = sandbox_home()
    if home:   # 写しの置き場で、入っているアプリをそのまま起こす（メニューバーのアプリは起こさない）
        env = {"UGC_COLLECTOR_HOME": home, "UGC_COLLECTOR_NO_APP_LAUNCH": "1",
               "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        return StdioServerParameters(command=EXE, args=["--mcp"], env=env)
    return StdioServerParameters(command=EXE, args=["--mcp"])


async def cmd_tools() -> None:
    async with Client(params(), read_timeout_seconds=300) as c:
        print("## instructions\n")
        print(c.instructions or "")
        for t in (await c.list_tools()).tools:
            print(f"\n## {t.name}\n")
            print(t.description or "")
            print("引数:", json.dumps(t.input_schema.get("properties", {}), ensure_ascii=False))


async def cmd_call(tool: str, args: dict, logged_args: dict | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    n = sum(1 for _ in LOG.open(encoding="utf-8")) if LOG.exists() else 0
    t0 = time.time()
    async with Client(params(), read_timeout_seconds=600) as c:
        r = await c.call_tool(tool, args)
    sec = round(time.time() - t0, 1)
    args = logged_args or args
    texts, images = [], []
    for i, item in enumerate(r.content or []):
        if getattr(item, "type", "") == "text":
            texts.append(item.text)
        elif getattr(item, "type", "") == "image":
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(item.mime_type, "png")
            p = OUT / f"{n:04d}-{tool}-img{i}.{ext}"
            p.write_bytes(base64.b64decode(item.data))
            images.append(str(p))
    text = "\n".join(texts)
    log({"kind": "tool", "tool": tool, "args": args, "seconds": sec, "is_error": bool(r.is_error),
         "text": text, "images": images})
    head = f"[{tool}] {sec}秒" + ("（エラー）" if r.is_error else "")
    if images:
        head += "\n画像（Read で見られる）: " + " ".join(images)
    if len(text) <= PRINT_MAX:
        print(head)
        print(text)
    else:
        p = OUT / f"{n:04d}-{tool}.md"
        p.write_text(text, encoding="utf-8")
        print(head)
        print(f"（返事が {len(text)} 字あるので表示しない。全文は file={p} を Read で全部読むこと）")


def main():
    a = sys.argv[1:]
    if a[:1] == ["tools"]:
        asyncio.run(cmd_tools())
    elif a[:1] == ["call"] and len(a) in (2, 3):
        args = json.loads(a[2]) if len(a) == 3 else {}
        asyncio.run(cmd_call(a[1], args))
    elif a[:1] == ["submit"] and len(a) == 3:
        out = Path(a[2]).read_text(encoding="utf-8")
        asyncio.run(cmd_call("submit", {"task_id": a[1], "output": out},
                             {"task_id": a[1], "output_file": str(Path(a[2]).resolve()), "chars": len(out)}))
    elif a[:1] == ["say"] and len(a) == 3 and a[1] in ("user", "ai"):
        log({"kind": "say", "who": a[1], "text": Path(a[2]).read_text(encoding="utf-8")})
        print("記録しました")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
