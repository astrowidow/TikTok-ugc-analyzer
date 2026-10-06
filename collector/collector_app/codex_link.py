"""ChatGPT（Mac アプリの Work の画面）にこのアプリの道具をつなぐ（メニュー「ChatGPT につなぐ」）。docs/CHATGPT_HANDOVER.md。

ChatGPT の Mac アプリは、Codex の設定ファイル（~/.codex/config.toml）の [mcp_servers.<名前>] にある道具を起こして使う。
ここに道具の節を1つ足す。書くのは3つ（どれも 2026-10-04 に運営の Mac の ChatGPT で確かめた形）:
  - 道具の起こし方（このアプリを --mcp で）と待ち時間（start_analysis は30〜40秒かかる）
  - 道具ごとの許可（approval_mode = "approve"。ChatGPT で「常に許可」を選んだときに ChatGPT 自身が書く形と同じ）。
    書いておかないと、書き込む道具（9つ）を初めて使うたびに許可を聞かれる
  - スキル（~/.agents/skills/ugc-analyzer/SKILL.md）。ChatGPT は道具の案内（MCP の instructions）を会話に入れず、
    道具の名前も最初は見せない。そのままだと「〇〇の分析を続けて」を会話の続きと受け取り、道具を使わずに答える。
    スキルは名前と説明が毎回の会話に入るので、曲の分析を頼まれたら道具を使うことに気づける
ほかの設定には触らない。書き換える前に、同じフォルダに控え（config.toml.bak-ugc-<日時>）を残す。
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import config

CODEX_HOME = Path(os.environ.get("UGC_CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CONFIG = CODEX_HOME / "config.toml"
SKILL_DIR = Path(os.environ.get("UGC_CODEX_SKILLS") or Path.home() / ".agents" / "skills") / "ugc-analyzer"
SERVER_NAME = "ugc-analyzer"
CHATGPT_APPS = [Path("/Applications/ChatGPT.app"), Path.home() / "Applications" / "ChatGPT.app"]
# 道具（mcp_proto.py。tests/check_local_mcp.py で、道具の一覧と同じかを確かめる）
TOOLS = ("start_analysis", "status", "next_task", "submit", "read", "revise", "deepen", "recut", "settings", "prompts",
         "update_knowledge", "cancel_analysis", "restart_analysis")
STARTUP_TIMEOUT_SEC = 60    # 入れた直後の初回は、macOS の検査で起動が遅いことがある
TOOL_TIMEOUT_SEC = 300      # start_analysis（30〜40秒）と、仕事の合間のサービスの工程（組み立て・Excel 用 ZIP）
MARK = "# UGC Analyzer の道具（メニュー「ChatGPT につなぐ」が書いた。この節は UGC Analyzer が書き直す）"

# いつ使うかは道具の案内（mcp_proto.USE_RULE）と同じ。ChatGPT は道具の案内を会話に入れないので、ここにも書く
SKILL_MD = """---
name: ugc-analyzer
description: UGC Analyzer（この Mac のアプリ。TikTok の楽曲の UGC を集めて分析レポートを作る）の道具を使う。使うのは、利用者が「UGC Analyzer で〇〇／△△を分析して」と名指しして新しい分析を頼んだときと、この Mac にすでにある分析のこと（「〇〇の分析を続けて」「〇〇の取得をやめて」「〇〇はどうなってる？」「〇〇のレポートの3章に〜を足して」）を頼んだとき。名指しの無い「〇〇を分析して」には使わない。
---

# UGC Analyzer

道具は MCP サーバー `ugc-analyzer`。道具の一覧（ALL_TOOLS など）で `ugc_analyzer` を探す: start_analysis・status・next_task・submit・read・revise・deepen・recut・settings・prompts・update_knowledge・cancel_analysis・restart_analysis。

- 「UGC Analyzer で〇〇／△△を分析して」→ `start_analysis`（曲名とアーティスト名。30〜40秒かかる）
- 名指しの無い「〇〇を分析して」→ 道具を使わず、ふつうに答える
- すでにある分析の頼み（「〇〇の分析を続けて」など）→ 分析があるか分からなければ、まず `status`（読むだけ）で、その曲の分析がこの Mac にあるかを確かめる。無ければ道具を使わず、ふつうに答える
- 分析の状態（集めたデータ・進み具合・成果物）はアプリが持っている。「続けて」は会話の続きではない。分析の状態を会話の履歴や前のスレッド、ウェブで探さない（指示書が「ウェブで調べる」と言う仕事では、ウェブ検索で調べる）
- 「〇〇の分析を続けて」→ `next_task`（analysis_id に曲名）。返ってきた指示書どおりに作業して `submit` し、また `next_task`。kind が done か wait になるまで、利用者に確認せずに繰り返す。kind が ask_user のときだけ、その内容を利用者に見せて答えを待つ
- 今後ずっと使う設定・指示書・知識ベースの頼みは、「UGC Analyzer の〜」と名指しがあるか、分析の会話の途中のときだけ
- 「界隈の確認はいらない」「確認なしで最後まで書いて」と言われたら、そのとき呼ぶ `start_analysis` か `next_task` に skip_confirm=true を付ける（界隈の案で止まらず、案のまま最後まで書く。使った界隈は完了の知らせで伝わる）
- 完成したレポートの「△△界隈のところが浅い・もっとコメントを取って掘り下げて」→ `deepen`（界隈と、利用者の言葉そのまま）。Mac がコメントを取り足すので止まる。終わったあと利用者が「〇〇の分析を続けて」と言ったら `next_task` で書き直しを片付ける
- 完成したレポートの「界隈を分けて・まとめて・切り直して」→ `recut`（利用者の言葉そのまま）。そのまま `next_task` を続ける。コメントが足りなければ kind が wait で止まるので、返ってきた一文を伝える。終わったあと利用者が「〇〇の分析を続けて」と言ったら `next_task` で続ける
- そのほか（進み具合・取得をやめる・やり直す・レポートの直し）は、道具の説明に従う。道具の説明と返事に書いてある決まりが、ここより優先する
"""

OPENAI_YAML = """interface:
  display_name: "UGC Analyzer"
  short_description: "TikTok の楽曲の UGC 分析（この Mac の UGC Analyzer）"
"""
SKILL_FILES = {"SKILL.md": SKILL_MD, "agents/openai.yaml": OPENAI_YAML}

# [mcp_servers.ugc-analyzer] と、その下の表（.env・.tools.<道具>）の見出し。ChatGPT が書き直すと名前が引用符つきになることもある
_OUR_HEADER = re.compile(r"""^\s*\[\s*mcp_servers\s*\.\s*(?:"ugc-analyzer"|'ugc-analyzer'|ugc-analyzer)\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$""")
_ANY_HEADER = re.compile(r"^\s*\[")
_OUR_MARK = re.compile(r"^\s*# UGC (?:Collector|Analyzer) の道具")   # 前の版（アプリ名が UGC Collector のころ）の目印も


# ウェブ検索を live に（2026-10-06〜。docs/WEB_RESEARCH.md）。Codex の既定（cached）は OpenAI の索引だけを見るので、YouTube の再生数のような
# 日々変わる数字が取れない。設定ファイルのいちばん上（最初の [ ] より前）に書く。利用者がすでに web_search を決めていれば触らない
WEB_LINE = 'web_search = "live"   # UGC Analyzer が足した（曲の情報・チャートを最新のページで調べるため）'
_OUR_WEB = re.compile(r'^\s*web_search\s*=\s*"live"\s*#\s*UGC Analyzer')


class LinkError(Exception):
    pass


def chatgpt_installed() -> bool:
    return any(p.exists() for p in CHATGPT_APPS)


def entry() -> dict:
    """設定ファイルに書く道具の起こし方（このアプリを --mcp で）"""
    if config.FROZEN:
        return {"command": str(Path(sys.executable).resolve()), "args": ["--mcp"]}
    return {"command": sys.executable, "args": ["-m", "collector_app", "--mcp"],
            "env": {"PYTHONPATH": str(config.REPO_ROOT / "collector")}}


def _s(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)   # TOML の基本文字列（JSON と同じ書き方）


def _block(disabled: bool = False) -> str:
    e = entry()
    lines = [MARK, f"[mcp_servers.{SERVER_NAME}]", f"command = {_s(e['command'])}",
             "args = [" + ", ".join(_s(a) for a in e["args"]) + "]",
             f"startup_timeout_sec = {STARTUP_TIMEOUT_SEC}", f"tool_timeout_sec = {TOOL_TIMEOUT_SEC}"]
    if disabled:   # 利用者が ChatGPT の設定で切ったもの（切ったまま書き直す）
        lines.append("enabled = false")
    if e.get("env"):
        lines += ["", f"[mcp_servers.{SERVER_NAME}.env]"] + [f"{k} = {_s(v)}" for k, v in e["env"].items()]
    for t in TOOLS:
        lines += ["", f"[mcp_servers.{SERVER_NAME}.tools.{t}]", 'approval_mode = "approve"']
    return "\n".join(lines) + "\n"


def _strip(text: str) -> str:
    """設定ファイルの文から、この道具の節（見出しから次の見出しの前まで）と目印の行を除く"""
    out, ours = [], False
    for line in text.splitlines():
        if _OUR_HEADER.match(line):
            ours = True
            continue
        if ours and _ANY_HEADER.match(line):
            ours = False
        if ours or _OUR_MARK.match(line) or _OUR_WEB.match(line):
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n")


def _with_web(text: str) -> str:
    """いちばん上の階層に web_search が無ければ、最初の表の見出しの前に足す"""
    try:
        if "web_search" in tomllib.loads(text):
            return text
    except tomllib.TOMLDecodeError:
        return text
    lines = text.splitlines()
    i = next((k for k, line in enumerate(lines) if _ANY_HEADER.match(line)), len(lines))
    head = lines[:i]
    while head and not head[-1].strip():   # 前に足して外した跡の空行をためない（何度つないでも同じ文になるように）
        head.pop()
    return "\n".join(head + ([""] if head else []) + [WEB_LINE, ""] + lines[i:]).rstrip("\n")


def _load() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        raise LinkError(f"ChatGPT（Codex）の設定ファイルが読めません（壊れている可能性）: {e}") from e


def _skill_current() -> bool:
    try:
        return all((SKILL_DIR / n).read_text(encoding="utf-8") == body for n, body in SKILL_FILES.items())
    except OSError:
        return False


def status() -> str:
    """connected（つながっている）/ outdated（前の場所のアプリ・古いスキル）/ disabled（ChatGPT の設定で切られている）/ none"""
    try:
        cur = (_load().get("mcp_servers") or {}).get(SERVER_NAME)
    except LinkError:
        return "none"
    if not isinstance(cur, dict):
        return "none"
    e = entry()
    if cur.get("command") != e["command"] or cur.get("args") != e["args"] or not _skill_current():
        return "outdated"
    if cur.get("enabled") is False:
        return "disabled"
    return "connected"


def _write(path: Path, text: str, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-ugc")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _backup() -> str:
    if not CONFIG.exists():
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    b = CONFIG.with_name(f"{CONFIG.name}.bak-ugc-{stamp}")
    shutil.copy2(CONFIG, b)
    return str(b)


def connect(enable: bool = False) -> str:
    """設定ファイルに道具の節を足し（前の節は置き換える）、スキルを置く。控えの場所を返す（元の設定ファイルが無ければ空）。
    前の節が ChatGPT の設定で切られていれば（enabled = false）、切ったまま書き直す（アプリを新しくしたときに勝手に戻さない）。
    enable はメニューの「ChatGPT につなぐ」を押したとき（利用者がつなぎ直したい）"""
    try:
        old = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    except (OSError, UnicodeDecodeError) as e:   # UTF-8 でない・読めない設定ファイルには書き足さない
        raise LinkError(f"ChatGPT（Codex）の設定ファイルが読めません（UTF-8 でない・壊れている可能性）: {e}") from e
    prev = (_load().get("mcp_servers") or {}).get(SERVER_NAME)   # 読めない設定ファイルには書き足さない
    disabled = not enable and isinstance(prev, dict) and prev.get("enabled") is False
    base = _with_web(_strip(old))   # ウェブ検索を live に（利用者が決めていれば触らない）
    new = base + "\n\n" + _block(disabled)
    try:
        cur = (tomllib.loads(new).get("mcp_servers") or {}).get(SERVER_NAME) or {}
    except tomllib.TOMLDecodeError as e:
        raise LinkError(f"書き足すと設定ファイルの形が崩れるため、やめました（{e}）") from e
    if cur.get("command") != entry()["command"]:
        raise LinkError("設定ファイルの中の ugc-analyzer の書き方が想定と違うため、書き足せませんでした")
    backup = _backup()
    mode = (CONFIG.stat().st_mode & 0o777) if CONFIG.exists() else 0o600
    _write(CONFIG, new, mode)
    for name, body in SKILL_FILES.items():
        _write(SKILL_DIR / name, body, 0o644)
    return backup


def disconnect() -> None:
    if CONFIG.exists():
        old = CONFIG.read_text(encoding="utf-8")
        new = _strip(old)
        if new != old.rstrip("\n"):
            _backup()
            _write(CONFIG, new + "\n" if new else "", CONFIG.stat().st_mode & 0o777)
    shutil.rmtree(SKILL_DIR, ignore_errors=True)


def chatgpt_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-x", "ChatGPT"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def restart_chatgpt() -> bool:
    """ChatGPT を終了して開き直す（道具の設定は起動したときに読まれるため）"""
    if chatgpt_running():
        subprocess.run(["/usr/bin/osascript", "-e", 'tell application "ChatGPT" to quit'], capture_output=True)
        t0 = time.time()
        while chatgpt_running() and time.time() - t0 < 30:
            time.sleep(0.5)
        if chatgpt_running():
            return False
        time.sleep(1.0)
    subprocess.run(["/usr/bin/open", "-a", "ChatGPT"], capture_output=True)
    return True
