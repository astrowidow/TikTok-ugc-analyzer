#!/bin/bash
# UGC Analyzer の .app と .dmg を作る（Apple シリコンの Mac で）。
#   collector/build.sh            → collector/dist/UGC Analyzer.app と collector/dist/UGC-Analyzer-<版>.dmg
# 友達に配るときは、コミットして push してから collector/release.sh（GitHub のリリースに載せる。友達のリンクは版によらず同じ）
# 署名はしない（Apple の開発者登録をしない。2026-10-01 ユーザー判断）。受け取った人は最初に
# 「システム設定 → プライバシーとセキュリティ → このまま開く」が要る（docs/FRIEND_GUIDE.md）
# 最初の知識ベース（output/notes_corpus/。リポジトリには入れていない）を同梱するので、それがある Mac で作る。
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f assets/AppIcon.icns ] || assets/make_icons.sh

VERSION=$("$PY" -c 'import re;print(re.search(r"VERSION = \"([^\"]+)\"", open("collector_app/__init__.py").read()).group(1))')
rm -rf build "dist/UGC Analyzer.app" "dist/UGC Analyzer" "dist/UGC Collector.app"
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build UGCCollector.spec >build.log 2>&1 \
  || { tail -30 build.log; exit 1; }
rm -rf "dist/UGC Analyzer"   # .app の中身と同じもの（onedir の残り）

# 自己点検1: 同梱物が読めるか（TikTok にも Claude にも触らない）
UGC_COLLECTOR_HOME="$(mktemp -d)" "dist/UGC Analyzer.app/Contents/MacOS/UGC Analyzer" --selftest >dist/selftest.json \
  || { cat dist/selftest.json; exit 1; }
# 自己点検2: Claude に出す道具を、Claude と同じ形（標準入出力）で起こして確かめる（TikTok・note には触らない）
"$PY" ../tests/check_local_mcp.py --app "dist/UGC Analyzer.app" --no-network >dist/mcp_check.txt 2>&1 \
  || { cat dist/mcp_check.txt; exit 1; }
tail -1 dist/mcp_check.txt

# .dmg（アプリと「アプリケーション」へのリンク。ドラッグして入れる形）
STAGE=$(mktemp -d)
cp -R "dist/UGC Analyzer.app" "$STAGE/"
ln -s /Applications "$STAGE/アプリケーション"
DMG="dist/UGC-Analyzer-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "UGC Analyzer" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
du -sh "dist/UGC Analyzer.app" "$DMG"
