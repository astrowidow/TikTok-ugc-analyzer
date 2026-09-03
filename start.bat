@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  設定
REM ============================================================
REM  ngrokの固定URL。環境変数 NGROK_DOMAIN があればそちらを優先します。
if not defined NGROK_DOMAIN set "NGROK_DOMAIN=giblet-squishy-icing.ngrok-free.dev"
set "PORT=8000"

REM  「start.bat local」と実行すると、ngrokを使わずこのPC内だけで動かします
set "SKIP_NGROK="
if /i "%~1"=="local" set "SKIP_NGROK=1"

echo ============================================================
echo   UGCAnalyzer
echo ============================================================
echo.

REM ------------------------------------------------------------
REM  Python を探す
REM ------------------------------------------------------------
set "PYCMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [エラー] Python が見つかりません。
    echo.
    echo   https://www.python.org/downloads/windows/ から Python 3.11 か 3.12 を
    echo   インストールしてください。
    echo   インストール時に "Add python.exe to PATH" のチェックを必ず入れてください。
    echo.
    pause
    exit /b 1
)

%PYCMD% -c "import sys; sys.exit(0 if min(sys.version_info[:2], (3, 9)) == (3, 9) else 1)"
if errorlevel 1 (
    echo [エラー] Python 3.9 以上が必要です。
    %PYCMD% --version
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM  [1/4] 仮想環境 .venv を用意する
REM ------------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo [1/4] 仮想環境 .venv は作成済みです。
) else (
    echo [1/4] 仮想環境 .venv を作成しています... ^(初回のみ数十秒かかります^)
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [エラー] 仮想環境の作成に失敗しました。
        echo   フォルダの書き込み権限を確認してください。
        pause
        exit /b 1
    )
    echo       作成しました。
)

set "VENV_PY=.venv\Scripts\python.exe"

REM ------------------------------------------------------------
REM  [2/4] 必要なパッケージを入れる
REM ------------------------------------------------------------
echo [2/4] 必要なパッケージを確認しています... ^(初回は数分かかります^)
"%VENV_PY%" -m pip install -r requirements.txt --disable-pip-version-check --quiet
if errorlevel 1 (
    echo.
    echo [警告] パッケージのインストールに失敗しました。
    echo        ネットワークが繋がっていない可能性があります。
    echo        すでに入っているパッケージで起動できるか確認します...
    "%VENV_PY%" -c "import fastapi, uvicorn, selenium, pandas, requests, jinja2" 2>nul
    if errorlevel 1 (
        echo.
        echo [エラー] 必要なパッケージが揃っていません。
        echo   インターネット接続を確認してから、もう一度実行してください。
        pause
        exit /b 1
    )
    echo        既存の環境で続行します。
) else (
    echo       準備できました。
)

REM ------------------------------------------------------------
REM  起動前の確認
REM ------------------------------------------------------------
set "CHROME_FOUND="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if not defined CHROME_FOUND (
    echo.
    echo [警告] Google Chrome が見つかりませんでした。
    echo        解析には Chrome 本体が必要です。入っていないと解析が失敗します。
)

if not defined TAVILY_API_KEY (
    if not defined BRAVE_API_KEY (
        echo.
        echo [情報] 検索APIキーが未設定です。自動検索モードは DuckDuckGo を使います。
        echo        Bot判定で検索が失敗することがあります。安定させるには
        echo        https://tavily.com でキーを取得し、次を一度だけ実行してください:
        echo          setx TAVILY_API_KEY "取得したキー"
        echo        そのあと、このウィンドウを閉じて開き直してください。
    )
)

REM ------------------------------------------------------------
REM  [3/4] ngrok で固定URLに公開する
REM ------------------------------------------------------------
set "NGROK_STARTED="
if defined SKIP_NGROK (
    echo.
    echo [3/4] ngrok は起動しません ^(local 指定^)。このPC内からのみ使えます。
    goto :run_server
)

where ngrok >nul 2>&1
if errorlevel 1 (
    echo.
    echo [警告] ngrok が見つかりません。このPC内からのみ使える状態で起動します。
    echo        外部に公開するには https://ngrok.com/download からインストールし、
    echo          ngrok config add-authtoken 取得したトークン
    echo        を一度実行してください。
    goto :run_server
)

if not exist "%LocalAppData%\ngrok\ngrok.yml" (
    echo.
    echo [警告] ngrok の認証トークンが未設定のようです。次を実行してください:
    echo          ngrok config add-authtoken 取得したトークン
)

REM  新しい ngrok は --url、古い版は --domain を使うので自動判定する
set "NGROK_FLAG=--url"
ngrok http --help 2>nul | findstr /C:"--url" >nul
if errorlevel 1 set "NGROK_FLAG=--domain"

set "NGROK_TARGET=%NGROK_DOMAIN%"
if "%NGROK_FLAG%"=="--url" set "NGROK_TARGET=https://%NGROK_DOMAIN%"

echo.
echo [3/4] ngrok を別ウィンドウで起動します ^(%NGROK_FLAG% を使用^)...
start "ngrok - UGCAnalyzer" cmd /k ngrok http %PORT% %NGROK_FLAG% %NGROK_TARGET%
set "NGROK_STARTED=1"

:run_server
REM ------------------------------------------------------------
REM  [4/4] サーバー起動
REM ------------------------------------------------------------
echo.
echo [4/4] サーバーを起動します。
echo.
echo   このPCから      : http://localhost:%PORT%
if defined NGROK_STARTED echo   外部から        : https://%NGROK_DOMAIN%
echo   ログイン        : ユーザー名 ymafia / パスワード ymafia
echo   ログファイル     : logs\ugc-analyzer.log
echo.
echo   停止するには、このウィンドウで Ctrl+C を押してください。
echo ------------------------------------------------------------
echo.

"%VENV_PY%" main.py

REM ------------------------------------------------------------
REM  後片付け
REM ------------------------------------------------------------
if defined NGROK_STARTED (
    echo.
    echo ngrok も終了します...
    taskkill /IM ngrok.exe /F >nul 2>&1
)

echo.
echo ============================================================
echo   サーバーが停止しました。
echo   エラーで落ちた場合は logs\ugc-analyzer.log を確認してください。
echo ============================================================
pause
