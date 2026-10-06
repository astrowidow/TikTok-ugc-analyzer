#!/bin/bash
# 作った UGC Analyzer（collector/dist の .app）を、この Mac の /Applications に入れ替える。
# docs/STAGE4_LOG.md の入れ替えの手順（0.5.0〜）を1本にしたもの（2026-10-06 ユーザー「自動でできるようにして」）。
#   collector/install.sh            … 入れ替える
#   collector/install.sh --dry-run  … 確かめだけ（何も変えない）
# 確かめること: 作った .app がある・取得中/順番待ちの分析が無い（本線の取得・掘り下げや切り直しの取り足し。
#               止まって自動で取り直す予定のもの＝止まって30分以内で、取り直しの回数が残っているものも）・TikTok のロックが無い
# 入れ替え: メニューバーのアプリを止める → 前の版の AI の道具（--mcp）と待つ命令（--wait）を止める
#           （動いたまま .app を置き換えると、前の版のプロセスが部品を実行ファイルの場所から読み直して壊れる。PyInstaller）
#           → 今の .app を output/app-backup/UGC Analyzer-<版>.app に移す → 新しい .app を写す → 起動 → 確かめる
#           → 道具を止めたら「Claude（ChatGPT）を開き直してください」と出す（Claude は落とさない。会話を切らない）
set -euo pipefail
cd "$(dirname "$0")/.."
APP="/Applications/UGC Analyzer.app"
NEW="collector/dist/UGC Analyzer.app"
DATA="$HOME/Library/Application Support/UGC Analyzer"
EXE="$APP/Contents/MacOS/UGC Analyzer"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

ver() { /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$1/Contents/Info.plist" 2>/dev/null || echo "?"; }

[ -d "$NEW" ] || { echo "作った .app がありません（先に collector/build.sh）"; exit 1; }
NEWV=$(ver "$NEW")
CURV=$([ -d "$APP" ] && ver "$APP" || echo "なし")
echo "入れ替え: $CURV → $NEWV"

busy=$(/usr/bin/python3 - "$DATA/analyses" <<'EOF'
import datetime, json, pathlib, sys
NO_RETRY = ("見つかりませんでした", "URL を教えて", "1本も見つかりませんでした")   # jobs.retriable と同じ
now = datetime.datetime.now().astimezone()
out = []
for p in pathlib.Path(sys.argv[1]).glob("*/analysis.json"):
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    acq = m.get("acquisition") or {}
    a = acq.get("status")
    d = (m.get("deepen") or {}).get("status")
    retry = False   # 止まって、アプリが自動で取り直す予定（止まって30分以内・回数が残っている）
    if a == "failed" and not any(k in (acq.get("error") or "") for k in NO_RETRY) \
            and int((m.get("collector") or {}).get("retries") or 0) < 3:
        try:
            retry = now - datetime.datetime.fromisoformat(acq["failed_at"]) < datetime.timedelta(minutes=30)
        except Exception:
            retry = False
    if a in ("queued", "running") or d in ("queued", "running") or retry:
        out.append(f"{p.parent.name} {m.get('title', '')}（取得 {a}{'・自動で取り直す予定' if retry else ''}・取り足し {d}）")
print("\n".join(out))
EOF
)
if [ -n "$busy" ]; then
  echo "取得中・順番待ちの分析があるので入れ替えません:"
  echo "$busy"
  exit 1
fi
if [ -e "$DATA/locks/tiktok.lock" ]; then
  echo "TikTok のロック（locks/tiktok.lock）があるので入れ替えません（実走などが TikTok を使っている）"
  exit 1
fi
# 前の版の AI の道具（--mcp。Claude・ChatGPT が起こしたもの）と待つ命令（--wait。Code タブ）。名前（ps の command）で見る
# （実行ファイルの場所は、前に入れ替えたときの控えに移っていることがある）。Claude の disclaimer など、ほかのプロセスは見ない
AI_PROCS="^$EXE --(mcp|wait)( |\$)"
if [ "$DRY" = 1 ]; then
  echo "確かめだけ: 入れ替えられます（取得中なし・TikTok のロックなし。入れ替えると止める AI の道具・待つ命令: $(pgrep -f "$AI_PROCS" | wc -l | tr -d ' ')個）"
  exit 0
fi

mkdir -p output/app-backup
BK="output/app-backup/UGC Analyzer-$CURV.app"
[ ! -e "$BK" ] || BK="output/app-backup/UGC Analyzer-$CURV-$(date +%Y%m%d-%H%M%S).app"

# メニューバーのアプリだけを止める（引数なしで動いているもの。--mcp は止めない）
for pid in $(pgrep -fx "$EXE" || true); do
  kill -TERM "$pid"
done
for _ in $(seq 1 20); do
  pgrep -fx "$EXE" >/dev/null || break
  sleep 0.5
done
if pgrep -fx "$EXE" >/dev/null; then
  echo "メニューバーのアプリが止まりませんでした（入れ替えていません）"
  exit 1
fi

# 前の版の AI の道具・待つ命令を PID で止める。道具が仕事の状態を書いている最中（runner.lock を持っている）なら、
# 書き終わるのを待ってから止める（1分まで）。止めた数を出す
ai_stopped=$(/usr/bin/python3 - "$AI_PROCS" "$DATA/locks/runner.lock" <<'EOF'
import fcntl, os, re, signal, subprocess, sys, time
pat, lock = re.compile(sys.argv[1]), sys.argv[2]

def ours():
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for ln in out.splitlines():
        pid, _, cmd = ln.strip().partition(" ")
        if pat.match(cmd.strip()) and int(pid) != os.getpid():
            pids.append(int(pid))
    return pids

pids = ours()
if pids:
    fd = None
    try:
        fd = os.open(lock, os.O_RDWR)
        t0 = time.time()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - t0 > 60:
                    break
                time.sleep(0.5)
    except OSError:
        pass   # まだ道具を使ったことがない（runner.lock が無い）
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    t0 = time.time()
    while ours() and time.time() - t0 < 5:
        time.sleep(0.3)
    for p in ours():
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if fd is not None:
        os.close(fd)
print(len(pids))
EOF
)

if [ -d "$APP" ]; then
  mv "$APP" "$BK"
fi
cp -R "$NEW" "$APP"
open "$APP"
for _ in $(seq 1 30); do
  pgrep -fx "$EXE" >/dev/null && break
  sleep 0.5
done
if ! pgrep -fx "$EXE" >/dev/null; then
  echo "入れ替えたが、起動を確かめられませんでした（控え: $BK）"
  exit 1
fi
echo "入れ替えました: $(ver "$APP")（前の版の控え: ${BK}）"
if [ "${ai_stopped:-0}" != 0 ]; then
  echo "前の版の AI の道具・待つ命令を ${ai_stopped}個 止めました。Claude（ChatGPT）を開き直してください（⌘Q で終了して開く。話していた会話は消えません）。開き直すまで、UGC Analyzer の道具は使えません"
else
  echo "Claude（ChatGPT）は、次に開いたときから新しい版の道具を使います"
fi
