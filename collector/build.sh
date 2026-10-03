#!/bin/bash
# UGC Collector の .app と .dmg を作る（Apple シリコンの Mac で）。
#   collector/build.sh            → collector/dist/UGC Collector.app と collector/dist/UGC-Collector-<版>.dmg
# 署名はしない（Apple の開発者登録をしない。2026-10-01 ユーザー判断）。受け取った人は最初に
# 「システム設定 → プライバシーとセキュリティ → このまま開く」が要る（docs/COLLECTOR_TRIAL.md）
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f assets/AppIcon.icns ] || assets/make_icons.sh

VERSION=$("$PY" -c 'import re;print(re.search(r"VERSION = \"([^\"]+)\"", open("collector_app/__init__.py").read()).group(1))')
rm -rf build "dist/UGC Collector.app" "dist/UGC Collector"
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build UGCCollector.spec >build.log 2>&1 \
  || { tail -30 build.log; exit 1; }
rm -rf "dist/UGC Collector"   # .app の中身と同じもの（onedir の残り）

# 自己点検（固めたもので同梱物が読めるか。TikTok には触らない）
UGC_COLLECTOR_HOME="$(mktemp -d)" "dist/UGC Collector.app/Contents/MacOS/UGC Collector" --selftest >dist/selftest.json \
  || { cat dist/selftest.json; exit 1; }

# .dmg（アプリと「アプリケーション」へのリンク。ドラッグして入れる形）
STAGE=$(mktemp -d)
cp -R "dist/UGC Collector.app" "$STAGE/"
ln -s /Applications "$STAGE/アプリケーション"
DMG="dist/UGC-Collector-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "UGC Collector" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
du -sh "dist/UGC Collector.app" "$DMG"
