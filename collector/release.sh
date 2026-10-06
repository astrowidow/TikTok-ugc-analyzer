#!/bin/bash
# 作った .dmg を GitHub のリリースに載せる。友達はリンクを押すだけで、GitHub の登録なしでダウンロードできる。
#   collector/release.sh          → いまの版（collector_app/__init__.py）の collector/dist/UGC-Analyzer-<版>.dmg
#   collector/release.sh 0.6.0    → 版を指定
# 友達に渡すリンクは版によらず同じ（ファイル名を版なしの UGC-Analyzer.dmg にして、最新のリリースに付けるため）:
#   https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg
# 前もって: collector/build.sh で .dmg を作り、版を上げたコミットを GitHub に push しておく。gh（brew install gh）に1回 gh auth login
# 同じ版を作り直したときは、同じリリースのファイルを差し替える。.dmg には著者の記事（知識ベース）が入っていて、リンクを知っていれば誰でも取れる
set -euo pipefail
cd "$(dirname "$0")"
REPO=astrowidow/TikTok-ugc-analyzer
URL="https://github.com/$REPO/releases/latest/download/UGC-Analyzer.dmg"
VERSION=${1:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' collector_app/__init__.py)}
DMG="dist/UGC-Analyzer-$VERSION.dmg"
[ -f "$DMG" ] || { echo "$DMG がありません（先に collector/build.sh）"; exit 1; }

# .dmg の中のアプリの版が合っているか
MNT=$(mktemp -d)
hdiutil attach -readonly -nobrowse -mountpoint "$MNT" "$DMG" >/dev/null
INNER=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$MNT/UGC Analyzer.app/Contents/Info.plist")
hdiutil detach "$MNT" >/dev/null
[ "$INNER" = "$VERSION" ] || { echo "$DMG の中のアプリは $INNER です（$VERSION ではない）"; exit 1; }

# 印（v<版>）は、その版に上げたコミットに付ける。GitHub に無いコミットには付けられない
COMMIT=$(git log --reverse --format=%H -S "VERSION = \"$VERSION\"" -- collector_app/__init__.py | sed -n 1p)
[ -n "$COMMIT" ] || { echo "版 $VERSION に上げたコミットが見つかりません（コミットしてから）"; exit 1; }
git fetch -q origin
[ -n "$(git branch -r --contains "$COMMIT")" ] || { echo "$(git log -1 --format='%h %s' "$COMMIT" | cut -c1-80) が GitHub にありません（push してから）"; exit 1; }

STAGE=$(mktemp -d)
cp "$DMG" "$STAGE/UGC-Analyzer.dmg"
if gh release view "v$VERSION" --repo "$REPO" >/dev/null 2>&1; then
  gh release upload "v$VERSION" "$STAGE/UGC-Analyzer.dmg" --repo "$REPO" --clobber
else
  NOTES="$(git log -1 --format=%s "$COMMIT")

友達向けのダウンロード（いつも最新の版）: $URL"
  gh release create "v$VERSION" "$STAGE/UGC-Analyzer.dmg" --repo "$REPO" --target "$COMMIT" \
    --title "UGC Analyzer $VERSION" --notes "$NOTES" --latest
fi
rm -rf "$STAGE"

# GitHub にログインしていない人と同じ形で、リンクから落とせるか（大きさだけ比べる）
SIZE=$(curl -sIL "$URL" | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}')
[ "$SIZE" = "$(stat -f %z "$DMG")" ] || { echo "リンクの先の大きさ（$SIZE）が $DMG と違います（反映待ちなら少し後でもう一度）"; exit 1; }
echo "載せました: UGC Analyzer $VERSION → $URL"
