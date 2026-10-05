#!/bin/bash
# 作った UGC Analyzer（collector/dist の .app）を、この Mac の /Applications に入れ替える。
# docs/STAGE4_LOG.md の入れ替えの手順（0.5.0〜）を1本にしたもの（2026-10-06 ユーザー「自動でできるようにして」）。
#   collector/install.sh            … 入れ替える
#   collector/install.sh --dry-run  … 確かめだけ（何も変えない）
# 確かめること: 作った .app がある・取得中/順番待ちの分析が無い（本線の取得・掘り下げや切り直しの取り足し）・TikTok のロックが無い
# 入れ替え: 今の .app を output/app-backup/UGC Analyzer-<版>.app に移す → メニューバーのアプリだけ止める
#           （Claude デスクトップの道具のプロセス --mcp は止めない。開き直すと新しい版になる）→ 新しい .app を写す → 起動 → 確かめる
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
import json, pathlib, sys
out = []
for p in pathlib.Path(sys.argv[1]).glob("*/analysis.json"):
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    a = (m.get("acquisition") or {}).get("status")
    d = (m.get("deepen") or {}).get("status")
    if a in ("queued", "running") or d in ("queued", "running"):
        out.append(f"{p.parent.name} {m.get('title', '')}（取得 {a}・取り足し {d}）")
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
if [ "$DRY" = 1 ]; then
  echo "確かめだけ: 入れ替えられます（取得中なし・TikTok のロックなし）"
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
echo "入れ替えました: $(ver "$APP")（前の版の控え: ${BK}）。Claude デスクトップの道具は、開き直すと新しい版になります"
