"""Claude の Code タブ（中身は Claude Code）から UGC Analyzer を使うための作業フォルダ（docs/CODE_TAB_ONE_SITTING.md）。

アプリが起動のたびに整える。利用者は Code タブでこのフォルダを選んで頼むだけ。置くもの（ほかのファイルには触らない）:
  .mcp.json                    道具（このアプリを --mcp で起こす）
  .claude/settings.local.json  道具と待つ命令だけを許可し、.mcp.json の道具を使う（中身は足すだけ。利用者や Claude Code が足した許可は消さない）
  CLAUDE.md                    手順（取得中は ./ugc-wait を裏で走らせ、終わったら続ける）
  ugc-wait                     待つ命令（このアプリを --wait で起こす。collector_app/waiter.py）
Code タブは「裏で走らせたコマンドが終わると Claude を起こし直す」ので、取得を待ってから完成まで1回で進む。
"""
import json
import os
import shlex
from pathlib import Path

from . import claude_link

FOLDER = Path(os.environ.get("UGC_CODE_FOLDER") or Path.home() / "UGC Analyzer")
SERVER = "ugc-analyzer"
ALLOW = [f"mcp__{SERVER}", "Bash(./ugc-wait:*)", "Bash(./ugc-wait *)"]

CLAUDE_MD = """# UGC Analyzer（Claude の Code タブ用の作業フォルダ）

このフォルダは、この Mac の UGC Analyzer（TikTok の楽曲の UGC を集めて分析レポートを作るアプリ）を、Claude の Code タブから使うためのもの。
UGC Analyzer が自動で作り、アプリを新しくすると書き直す。

- 道具は MCP サーバー `ugc-analyzer`。使い方は道具の説明（instructions）のとおり。利用者への返事は日本語で、短く
- このフォルダのファイルを読んだり書き換えたりしない。Bash で使ってよいのは `./ugc-wait` だけ。レポートは UGC Analyzer が Mac の「レポート」フォルダに置く

## 待つところで止まらずに続ける（Code タブだけの使い方）

道具の返事が待ちになったら（next_task の kind が wait のとき、start_analysis・deepen・restart_analysis の返事に「その間 AI は待てないため」の一文があるとき）:

0. 取得をやめた分析の返事と、「./ugc-wait は走らせない」とある返事（ログイン待ち・アプリが動いていない・取得が止まった。利用者が何かするまで進まない）は、
   1〜3 をせずに、その返事のとおり利用者に伝えて止まる
1. 利用者には「集め終わったら、このまま自動で続けます（終わる見込みの時刻）。Mac と Claude は開いたままにしてください」と1〜2文で伝える。
   「〇〇の分析を続けて」と頼む一文は伝えない（利用者が頼まなくても続けるため）。どの楽曲ページで進めるかなど、ほかに伝えることは道具の説明のとおり
2. Bash で `./ugc-wait <分析 ID>` を **裏で（run_in_background）** 走らせて、そこで止まる。待っている間に status や next_task を何度も呼ばない
3. 終わった知らせが来たら、出力の1行を見る:
   - 「終わりました」→ next_task から続ける（利用者が「〇〇の分析を続けて」と言ったのと同じ。done か、また待ちになるまで）
   - 「ログイン待ち」「止まりました」「やめてあります」→ その文のとおり利用者に伝えて止まる
   - 「まだ終わっていません」→ もう一度 `./ugc-wait <分析 ID>` を裏で走らせる
4. 界隈の確認（kind が ask_user）は、利用者が「界隈の確認はいらない」と言っていなければ、今までどおり案を見せて答えを待つ。
   言っていれば next_task に skip_confirm=true を付けて、止まらずに完成まで進める
"""


def _wait_script() -> str:
    e = claude_link.entry()
    args = [a for a in e.get("args", []) if a != "--mcp"]
    cmd = " ".join(shlex.quote(x) for x in [e["command"], *args, "--wait"])
    env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (e.get("env") or {}).items())
    return ("#!/bin/sh\n"
            "# UGC Analyzer の取得（掘り下げ・切り直しの取り足しを含む）が終わるまで待つ。Claude の Code タブで、Claude が裏で走らせる\n"
            f"exec {'env ' + env + ' ' if env else ''}{cmd} \"$@\"\n")


def _mcp_json() -> str:
    e = claude_link.entry()
    return json.dumps({"mcpServers": {SERVER: {"type": "stdio", **e}}}, ensure_ascii=False, indent=2) + "\n"


def _files() -> dict:
    """置くファイル（相対パス → (中身, 権限)）。settings.local.json は別（足すだけ）"""
    return {".mcp.json": (_mcp_json(), 0o644), "CLAUDE.md": (CLAUDE_MD, 0o644), "ugc-wait": (_wait_script(), 0o755)}


def _settings_path() -> Path:
    return FOLDER / ".claude" / "settings.local.json"


def _settings() -> dict:
    """今の settings.local.json。無い・読めない・辞書でない（[] など）ときは空から作り直す
    （そのままだとアプリの起動のたびに落ちる。Claude Code も辞書でない設定は読めない）"""
    try:
        s = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def _list(v) -> list:
    """許可・道具の一覧。形が違う（文字列・辞書など）ときは空とみなす"""
    return list(v) if isinstance(v, list) else []


def _perm(s: dict) -> dict:
    p = s.get("permissions")
    return p if isinstance(p, dict) else {}


def _settings_current(s: dict) -> bool:
    allow = _list(_perm(s).get("allow"))
    return all(a in allow for a in ALLOW) and SERVER in _list(s.get("enabledMcpjsonServers"))


def status() -> str:
    """connected（全部いまのアプリに合っている）／ outdated（無い・古い）"""
    for rel, (text, _) in _files().items():
        p = FOLDER / rel
        try:
            if p.read_text(encoding="utf-8") != text:
                return "outdated"
        except (OSError, ValueError):   # 無い・UTF-8 でない（利用者が別の文字コードで保存した）
            return "outdated"
    return "connected" if _settings_current(_settings()) else "outdated"


def ensure() -> list:
    """作業フォルダを、いまのアプリに合わせて整える。書き換えたファイルの一覧を返す"""
    FOLDER.mkdir(parents=True, exist_ok=True)
    changed = []
    for rel, (text, mode) in _files().items():
        p = FOLDER / rel
        try:
            same = p.read_text(encoding="utf-8") == text
        except (OSError, ValueError):   # 無い・UTF-8 でない（書き直す。そのままだとアプリの起動のたびに落ちる）
            same = False
        if not same:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, mode)
            os.replace(tmp, p)
            changed.append(rel)
    s = _settings()
    if not _settings_current(s):
        perm = s["permissions"] = _perm(s)
        allow = _list(perm.get("allow"))
        perm["allow"] = allow + [a for a in ALLOW if a not in allow]
        servers = _list(s.get("enabledMcpjsonServers"))
        s["enabledMcpjsonServers"] = servers + ([SERVER] if SERVER not in servers else [])
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        changed.append(".claude/settings.local.json")
    return changed
