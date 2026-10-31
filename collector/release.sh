#!/bin/bash
# 作った .dmg を GitHub のリリースに載せる。2段（0.9.0〜。2026-10-31 ユーザー決定「友達に届くのは『友達に出して』と言ったときだけ」）:
#   collector/release.sh [版] [--note 文]             … 試し版（prerelease）として載せる。友達には届かない（友達のリンクも前の版のまま）。
#                                                       運営の Mac の UGC Analyzer は試し版も受け取る（collector_app/updater.py）。
#                                                       --note を付けると、運営の Mac の窓と通知に「今回の版: …」が出る（--friends でも引き継ぐ）
#   collector/release.sh --friends [版] [--note 文]   … その版を友達向けの最新（latest）に切り替える。友達の UGC Analyzer（0.9.0〜）は
#                                                       3時間以内に自分で入れ替え、友達のリンクもこの版になる。
#                                                       --note は窓と通知に出す「今回の版: …」の1行（友達向けの言葉で。裏の仕組みを書かない）
# 友達のリンクは版によらず同じ（ファイル名を版なしの UGC-Analyzer.dmg にして、最新のリリースに付けるため）:
#   https://github.com/astrowidow/TikTok-ugc-analyzer/releases/latest/download/UGC-Analyzer.dmg
# 前もって: collector/build.sh で .dmg を作り、版を上げたコミットを GitHub に push しておく。gh（brew install gh）に1回 gh auth login
# 同じ版を作り直したときは、同じリリースのファイルを差し替える（試し版か友達向けかは変えない）。
# .dmg には著者の記事（知識ベース）が入っていて、リンクを知っていれば誰でも取れる
set -euo pipefail
cd "$(dirname "$0")"
REPO=astrowidow/TikTok-ugc-analyzer
URL="https://github.com/$REPO/releases/latest/download/UGC-Analyzer.dmg"
FRIENDS=0
NOTE=""
VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --friends) FRIENDS=1 ;;
    --note) NOTE="${2:-}"; shift ;;
    *) VERSION="$1" ;;
  esac
  shift
done
VERSION=${VERSION:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' collector_app/__init__.py)}
size_of() { curl -sIL "$1" | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}'; }

if [ "$FRIENDS" = 1 ]; then
  # --- 友達向けに切り替える ---
  gh release view "v$VERSION" --repo "$REPO" >/dev/null 2>&1 || { echo "v$VERSION のリリースがありません（先に collector/release.sh ${VERSION}）"; exit 1; }
  # 友達が見る始め方のページと紙の「いまの版は UGC Analyzer <版>」が、出す版と合っているか（2026-10-12: 0.8.1 を出したのに、
  # ページの入れ替えの手順に「例: UGC Analyzer 0.8.0」が残っていた。ユーザー「そういうところが信用を損ねる」）
  for DOC in ../docs/pages/guide.html ../docs/FRIEND_GUIDE.md; do
    grep -q "いまの版は.*UGC Analyzer $VERSION" "$DOC" \
      || { echo "$DOC の「いまの版は UGC Analyzer …」が $VERSION になっていません（直して、始め方のページを公開し直してから）"; exit 1; }
  done
  # 説明: 試し版の断りを外し、「今回の版: …」を付け直す（--note が無ければ、前に書いたものを残す）
  BODY=$(gh release view "v$VERSION" --repo "$REPO" --json body --jq .body)
  OLD_NOTE=$(printf '%s\n' "$BODY" | sed -n 's/^今回の版[:：] *//p' | head -1)
  NOTE=${NOTE:-$OLD_NOTE}
  HEAD=$(printf '%s\n' "$BODY" | grep -v -e '^今回の版[:：]' -e '^試し版' -e '^友達向けのダウンロード' || true)   # 終わりの空行は $( ) が落とす
  NEW_BODY="$HEAD"
  [ -n "$NOTE" ] && NEW_BODY="$NEW_BODY

今回の版: $NOTE"
  NEW_BODY="$NEW_BODY

友達向けのダウンロード（いつも最新の版）: $URL"
  gh release edit "v$VERSION" --repo "$REPO" --prerelease=false --latest --notes "$NEW_BODY" >/dev/null
  # GitHub にログインしていない人と同じ形で、友達のリンクから落とせるか（大きさだけ比べる）
  WANT=$(gh release view "v$VERSION" --repo "$REPO" --json assets --jq '.assets[] | select(.name=="UGC-Analyzer.dmg") | .size')
  for _ in 1 2 3 4 5 6; do
    SIZE=$(size_of "$URL")
    [ "$SIZE" = "$WANT" ] && break
    sleep 10
  done
  [ "$SIZE" = "$WANT" ] || { echo "友達のリンクの先の大きさ（${SIZE}）が v${VERSION}（${WANT}）と違います（反映待ちなら少し後でもう一度）"; exit 1; }
  echo "友達向けにしました: UGC Analyzer ${VERSION}（今回の版: ${NOTE:-なし}）→ $URL"
  echo "友達の UGC Analyzer（0.9.0 以降）は、3時間以内に自分で新しくなります。始め方のページを公開し直したかも確かめてください（docs/pages/README.md）"
  exit 0
fi

# --- 試し版として載せる ---
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
  if [ -n "$NOTE" ]; then   # 「今回の版: …」を付け直す
    BODY=$(gh release view "v$VERSION" --repo "$REPO" --json body --jq .body | grep -v '^今回の版[:：]' || true)
    gh release edit "v$VERSION" --repo "$REPO" --notes "${BODY}

今回の版: ${NOTE}" >/dev/null
  fi
else
  NOTES="$(git log -1 --format=%s "$COMMIT")

試し版（友達にはまだ届かない）。友達に出すときは collector/release.sh --friends $VERSION --note \"…\""
  [ -n "$NOTE" ] && NOTES="${NOTES}

今回の版: ${NOTE}"
  gh release create "v$VERSION" "$STAGE/UGC-Analyzer.dmg" --repo "$REPO" --target "$COMMIT" \
    --title "UGC Analyzer $VERSION" --notes "$NOTES" --prerelease
fi
rm -rf "$STAGE"

# GitHub にログインしていない人と同じ形で、この版のリンクから落とせるか（大きさだけ比べる）
TAG_URL="https://github.com/$REPO/releases/download/v$VERSION/UGC-Analyzer.dmg"
SIZE=$(size_of "$TAG_URL")
[ "$SIZE" = "$(stat -f %z "$DMG")" ] || { echo "リンクの先の大きさ（${SIZE}）が $DMG と違います（反映待ちなら少し後でもう一度）"; exit 1; }
PRE=$(gh release view "v$VERSION" --repo "$REPO" --json isPrerelease --jq .isPrerelease)
if [ "$PRE" = "true" ]; then
  echo "試し版として載せました: UGC Analyzer $VERSION → ${TAG_URL}（友達にはまだ届きません。運営の Mac は受け取ります）"
else
  echo "載せ直しました（友達向けの版）: UGC Analyzer $VERSION → $TAG_URL"
fi
