@echo off
REM ============================================================
REM  コメント取得用の Chrome を起動する
REM
REM  重要: このファイルを SSH から直接叩いてはいけない。
REM  Windows はセッションを分離するので、SSH セッションから起動した
REM  GUI アプリはコンソールのデスクトップを持たない。すると Chrome は
REM  描画を止め、コメントの仮想リストがスケルトンのままになり、
REM  ブロックもエラーも出ないまま全動画が 20 件で終わる（引き継ぎ書 2-9）。
REM
REM  必ず schtasks の /IT でログオン中のセッションに起動すること:
REM    schtasks /Create /TN "tiktok-chrome" /TR "C:\tiktok\win-launch-chrome.bat" /SC ONCE /ST 00:00 /IT /F
REM    schtasks /Run /TN "tiktok-chrome"
REM ============================================================

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

REM  プロファイル置き場。利用者ごとに分けるならここを変える。
REM  認証情報そのものなのでリポジトリには絶対に入れないこと。
set "PROFILE=C:\tiktok-profiles\main"
set "PORT=9222"

if not exist "%PROFILE%" mkdir "%PROFILE%"

start "" "%CHROME%" ^
  --user-data-dir="%PROFILE%" ^
  --profile-directory=Default ^
  --remote-debugging-port=%PORT% ^
  --no-first-run ^
  --no-default-browser-check ^
  --mute-audio ^
  "about:blank"
