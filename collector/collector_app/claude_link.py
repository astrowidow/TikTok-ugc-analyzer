"""Claude デスクトップにこのアプリの道具をつなぐ（メニュー「Claude につなぐ」）。docs/ALL_IN_APP_PLAN.md 段2。

Claude デスクトップの設定ファイル（claude_desktop_config.json）の mcpServers に1行足すだけ。
ほかの設定には触らない。書き換える前に、同じフォルダに控え（claude_desktop_config.json.bak-ugc-<日時>）を残す。
秘密の URL も番号も要らない（道具はこの Mac の中で動き、この Mac の利用者1人のもの）。
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config, system

CONFIG = Path(os.environ.get("UGC_CLAUDE_CONFIG")
              or Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
SERVER_NAME = "ugc-analyzer"
CLAUDE_APPS = [Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"]


class LinkError(Exception):
    pass


def claude_installed() -> bool:
    return any(p.exists() for p in CLAUDE_APPS)


def entry() -> dict:
    """設定ファイルに書く1行（このアプリを --mcp で起こす）"""
    if config.FROZEN:
        return {"command": str(Path(sys.executable).resolve()), "args": ["--mcp"]}
    return {"command": sys.executable, "args": ["-m", "collector_app", "--mcp"],
            "env": {"PYTHONPATH": str(config.REPO_ROOT / "collector")}}


def _load() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8") or "{}")
    except ValueError as e:
        raise LinkError(f"Claude の設定ファイルが読めません（壊れている可能性）: {e}") from e
    if not isinstance(data, dict):
        raise LinkError("Claude の設定ファイルの形が想定と違います")
    return data


def status() -> str:
    """connected（つながっている）/ outdated（前の場所のアプリを指している）/ none"""
    try:
        cur = (_load().get("mcpServers") or {}).get(SERVER_NAME)
    except LinkError:
        return "none"
    if not cur:
        return "none"
    return "connected" if cur.get("command") == entry()["command"] and cur.get("args") == entry()["args"] else "outdated"


def connect() -> str:
    """設定ファイルに道具を足す（控えを取ってから）。控えの場所を返す（元の設定ファイルが無ければ空）"""
    data = _load()
    backup = ""
    if CONFIG.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        b = CONFIG.with_name(f"{CONFIG.name}.bak-ugc-{stamp}")
        b.write_bytes(CONFIG.read_bytes())
        backup = str(b)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = entry()
    data["mcpServers"] = servers
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(CONFIG.name + ".tmp-ugc")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)
    return backup


def disconnect() -> None:
    data = _load()
    if (data.get("mcpServers") or {}).pop(SERVER_NAME, None) is not None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        CONFIG.with_name(f"{CONFIG.name}.bak-ugc-{stamp}").write_bytes(CONFIG.read_bytes())
        tmp = CONFIG.with_name(CONFIG.name + ".tmp-ugc")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG)


def claude_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-x", "Claude"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def restart_claude(on_asking=None) -> bool:
    """Claude デスクトップを終了して開き直す（設定ファイルは起動したときにだけ読まれるため）。
    作業中なら Claude が終了の確認を出すので、答えてもらうまで見張る（system.relaunch_app）"""
    return system.relaunch_app("Claude", claude_running, on_asking)
