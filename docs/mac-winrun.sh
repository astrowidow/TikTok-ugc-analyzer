#!/bin/sh
# Mac から Windows 機で PowerShell スクリプトを1本実行する（ssh 越しの引用符崩れを避けるため、ファイルを送って -File で実行）。
# 使い方: docs/mac-winrun.sh <ローカルの .ps1>   → C:\Users\astrowidow\ugc-work\ps\ に写して実行、出力は UTF-8
# PowerShell 5.1 は BOM の無い .ps1 を ANSI（CP932）で読んで日本語で壊れるので、無ければ BOM を付けて送る。
f="$1"; b=$(basename "$f")
tmp=$(mktemp -t winrun)
if [ "$(head -c 3 "$f" | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
  cp "$f" "$tmp"
else
  printf '\357\273\277' > "$tmp"; cat "$f" >> "$tmp"
fi
ssh -o BatchMode=yes win 'New-Item -ItemType Directory -Force C:\Users\astrowidow\ugc-work\ps | Out-Null' >/dev/null 2>&1
scp -q -o BatchMode=yes "$tmp" "win:C:/Users/astrowidow/ugc-work/ps/$b" || { rm -f "$tmp"; exit 1; }
rm -f "$tmp"
exec ssh -o BatchMode=yes win "[Console]::OutputEncoding=[Text.Encoding]::UTF8; \$ProgressPreference='SilentlyContinue'; & powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\astrowidow\\ugc-work\\ps\\$b"
