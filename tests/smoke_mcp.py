#!/usr/bin/env python3
"""AI の接続口（mcp_proto.py）の試験クライアント。公式 SDK（mcp 2.x）で Streamable HTTP につなぐ。

引き継ぎ書 4-8 の確かめ:
  1. 初期化 → 道具の一覧 → instructions が返る
  2. next_task → 1の仕事が返る。read でシートの画像と表のページが取れる
  3. わざと形の悪い出力を submit → 理由つきで差し戻される
  4. 秘密の文字列を変えると 404。Basic 認証の既存画面は今までどおり
  --full を付けると、E2E の結果（正解の形）を出して13個の仕事を最後まで流す（検査と状態の一周の確認）

秘密の URL は画面に出さない。環境変数 UGC_MCP_URL か、--cred のファイルの UGC_MCP_URL= の行から読む。
  python tests/smoke_mcp.py [--full] [--cred ~/.tiktok-ugc-win-credentials] [--basic URL]
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "output" / "trial_silhouette"


def load_url(cred: str | None) -> str:
    url = os.environ.get("UGC_MCP_URL")
    if not url and cred:
        for line in Path(cred).expanduser().read_text(encoding="utf-8").splitlines():
            if line.startswith("UGC_MCP_URL="):
                url = line.split("=", 1)[1].strip().strip('"')
    if not url:
        sys.exit("UGC_MCP_URL が見つかりません")
    return url


def masked(url: str) -> str:
    return re.sub(r"/mcp/[^/]+", "/mcp/***", url)


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")


def ok(cond: bool, what: str):
    print(("  OK  " if cond else "  NG  ") + what)
    if not cond:
        ok.failed += 1


ok.failed = 0


def _ssl_context():
    """python.org 版の Python は証明書を持たないので、OS の証明書を使う（truststore は mcp の依存で入っている）"""
    try:
        import ssl
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> tuple:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def basic_checks(url: str, basic_root: str | None):
    print("[4] 知らない URL と既存画面")
    bad = re.sub(r"/mcp/[^/]+", "/mcp/" + "x" * 43, url)
    code, _ = http("POST", bad, {"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream"}, b"{}")
    ok(code == 404, f"秘密の文字列を変えると 404（{code}）")
    if basic_root:
        code, _ = http("GET", basic_root)
        ok(code == 401, f"既存画面 / は認証なしで 401（{code}）")
        user, pw = os.environ.get("UGC_BASIC", "ymafia:ymafia").split(":", 1)
        auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
        code, _ = http("GET", basic_root, auth)
        ok(code == 200, f"既存画面 / は認証つきで 200（{code}）")
        code, text = http("GET", basic_root.rstrip("/") + "/api/status/nonexistent", auth)
        ok(code == 404 and "Job not found" in text, f"既存の /api/status が今までどおり応答（{code}）")


def e2e_answers():
    """--full 用: E2E の結果を「正解の形」として出す"""
    tax = json.loads((E2E / "clean" / "taxonomy.json").read_text(encoding="utf-8"))
    tax = {k: v for k, v in tax.items() if not k.startswith("_")}
    labels = {}
    rows = (E2E / "clean" / "labels.tsv").read_text(encoding="utf-8").splitlines()
    for r in rows[1:]:
        labels[int(r.split("\t")[0])] = r
    va = {}
    for line in (E2E / "e2e" / "report" / "video_analysis.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            va[int(o["seq"])] = o
    return tax, rows[0], labels, va


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred")
    ap.add_argument("--basic", help="既存画面のルート URL（例 https://…ngrok-free.dev/）")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    url = load_url(args.cred)
    print("接続先:", masked(url))

    async with Client(url) as c:
        print("[1] 初期化・道具・instructions")
        tools = await c.list_tools()
        names = sorted(t.name for t in tools.tools)
        ok(names == ["next_task", "read", "status", "submit"], f"道具 {names}")
        ins = c.instructions or ""
        ok("next_task" in ins and "ask_user" in ins, f"instructions が返る（{len(ins)}文字）")
        print("      版:", c.protocol_version, c.server_info.name if c.server_info else None)

        r = await c.call_tool("status", {})
        ok(not r.is_error and "silhouette-proto" in text_of(r), "status に試作の分析が出る")
        print("      " + text_of(r).splitlines()[0])

        print("[2] next_task と read")
        r = await c.call_tool("next_task", {"analysis_id": "シルエット"})
        t = text_of(r)
        m = re.search(r"task_id: `([^`]+)`", t)
        ok(not r.is_error and m is not None, "曲名で next_task が引ける")
        task_id = m.group(1) if m else ""
        print(f"      {task_id} / {len(t)}文字 / 先頭: {t.splitlines()[0]}")
        ok(t.rstrip().endswith("（利用枠を節約するため）。"), "末尾に繰り返し方がある")

        r = await c.call_tool("read", {"analysis_id": "silhouette-proto", "name": "taxonomy_sample", "page": 1})
        rt = text_of(r)
        ok(not r.is_error and "page 1/" in rt, f"表のページが取れる（{len(rt)}文字、{rt.splitlines()[0]}）")
        pages = int(re.search(r"page 1/(\d+)", rt).group(1)) if "page 1/" in rt else 0
        sizes = []
        for p in range(1, pages + 1):
            rr = await c.call_tool("read", {"name": "taxonomy_sample", "page": p})
            sizes.append(len(text_of(rr).encode("utf-8")))
        print(f"      taxonomy_sample {pages}ページ、バイト {sizes}")
        r = await c.call_tool("read", {"name": "sheet:03"})
        imgs = [x for x in r.content if getattr(x, "type", "") == "image"]
        ok(not r.is_error and len(imgs) == 1 and imgs[0].mime_type == "image/jpeg",
           f"シートの画像が取れる（{len(base64.b64decode(imgs[0].data)) if imgs else 0}バイト）")
        r = await c.call_tool("read", {"name": "../analysis.json"})
        ok(r.is_error, f"目録に無い名前は断る（{text_of(r)[:60]}）")

        print("[3] 形の悪い出力を submit")
        r = await c.call_tool("submit", {"task_id": task_id, "output": "{\"community\": {\"a\": \"x\"}}"})
        st = text_of(r)
        ok('"ok": false' in st and "unknown" in st, "理由つきで差し戻される")
        print("      " + " / ".join(st.splitlines()[1:4]))
        r = await c.call_tool("submit", {"task_id": task_id, "output": "これは JSON ではない"})
        ok('"ok": false' in text_of(r), "JSON でないものも差し戻される")

        if args.full:
            print("[5] 13個を最後まで（E2E の結果を出す）")
            tax, head, labels, va = e2e_answers()
            for _ in range(20):
                r = await c.call_tool("next_task", {})
                t = text_of(r)
                tid = re.search(r"task_id: `([^`]+)`", t).group(1)
                kind = re.search(r"kind: `([^`]+)`", t).group(1)
                if kind == "done":
                    ok("完了" in t, f"{tid}: done（{len(t)}文字）")
                    break
                if kind == "ask_user":
                    ok("OK" in t and "界隈の案" in t, f"{tid}: ask_user に利用者に見せる内容がある")
                    out = json.dumps({"user_answer": "OK"}, ensure_ascii=False)
                elif tid.endswith("propose_axes"):
                    out = "```json\n" + json.dumps(tax, ensure_ascii=False) + "\n```"
                elif tid.endswith("label"):
                    seqs = [int(x) for x in re.search(r"（\d+ 本: seq ([0-9, ]+)）", t).group(1).split(", ")]
                    out = head + "\n" + "\n".join(labels[s] for s in seqs)
                else:
                    seq = int(re.search(r"seq (\d+)）", t).group(1))
                    o = dict(va[seq])
                    o["community"] = o.get("community") if o.get("community") in tax["community"] else "unknown"
                    out = json.dumps(o, ensure_ascii=False)
                r = await c.call_tool("submit", {"task_id": tid, "output": out})
                st = text_of(r)
                ok('"ok": true' in st, f"{tid}: {len(t)}文字を受け取り {len(out)}文字を提出 → {st.splitlines()[0]}")
                if '"ok": false' in st:
                    print(st[:1500])
                    break
            r = await c.call_tool("submit", {"task_id": tid, "output": "x"})
            r2 = await c.call_tool("status", {})
            print("      " + text_of(r2).splitlines()[0])

    basic_checks(url, args.basic)
    print("失敗", ok.failed, "件")
    sys.exit(1 if ok.failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
