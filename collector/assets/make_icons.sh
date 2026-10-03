#!/bin/bash
# アイコンを SVG から作る（Mac の Google Chrome で描いて、sips と iconutil で .icns にする）。TikTok には触らない
set -euo pipefail
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

render() {  # <svg> <描く大きさ> <出力 png>
  cat > "$TMP/p.html" <<EOF
<!doctype html><html><head><style>html,body{margin:0;background:transparent}img{display:block;width:${2}px;height:${2}px}</style></head>
<body><img src="file://$PWD/$1"></body></html>
EOF
  rm -f "$PWD/$3"
  # 書き出しても Chrome が終わらないことがあるので、ファイルができたら自分で起動したものだけ止める
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$TMP/prof-$2" \
    --default-background-color=00000000 --force-device-scale-factor=1 --window-size="$2,$2" \
    --screenshot="$PWD/$3" "file://$TMP/p.html" >/dev/null 2>&1 &
  local pid=$!
  for _ in $(seq 1 60); do
    [ -s "$PWD/$3" ] && sleep 1 && break
    sleep 0.5
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  [ -s "$PWD/$3" ] || { echo "描けませんでした: $1"; exit 1; }
}

render icon.svg 1024 icon-1024.png
rm -rf AppIcon.iconset && mkdir AppIcon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s icon-1024.png --out AppIcon.iconset/icon_${s}x${s}.png >/dev/null
  d=$((s * 2))
  sips -z $d $d icon-1024.png --out AppIcon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns AppIcon.iconset -o AppIcon.icns
rm -rf AppIcon.iconset

render menubar.svg 512 menubar-512.png
sips -z 44 44 menubar-512.png --out menubar@2x.png >/dev/null
sips -z 22 22 menubar-512.png --out menubar.png >/dev/null
rm -f menubar-512.png
ls -la AppIcon.icns icon-1024.png menubar.png menubar@2x.png
