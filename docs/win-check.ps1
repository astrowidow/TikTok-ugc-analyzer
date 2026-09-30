# ============================================================
#  TikTok UGC Analyzer - Windows 事前チェック
#
#  管理者 PowerShell で実行してください（開き方は手順書 Phase 0）。
#    powershell -ExecutionPolicy Bypass -File .\win-check.ps1
#
#  やること: 必要な条件を全部調べて [OK]/[NG] で判定し、
#            結果を Mac に自動送信する。TikTok には一切アクセスしない。
# ============================================================

$MacHost = "192.168.0.52"
$MacPort = 8765

$ErrorActionPreference = "Continue"
$script:NG   = @()
$script:WARN = @()

# ------------------------------------------------------------
function Show-Result {
    param([string]$Mark, [string]$Title, [string]$Value, [string]$Why)
    $color = "Gray"
    if ($Mark -eq "OK") { $color = "Green" }
    elseif ($Mark -eq "NG") { $color = "Red" }
    elseif ($Mark -eq "warn") { $color = "Yellow" }
    Write-Host ("[{0,-4}] {1}" -f $Mark, $Title) -ForegroundColor $color
    if ($Value) { Write-Host ("        実際の値 : {0}" -f $Value) }
    if ($Why)   { Write-Host ("        意味     : {0}" -f $Why) -ForegroundColor DarkGray }
    Write-Host ""
}

# ------------------------------------------------------------
#  Mac への送信は PowerShell 標準の Invoke-RestMethod を使う。
#  curl の --data-binary の @ファイル指定は、パスに空白が含まれる等で
#  「error encountered when reading a file」になりやすく、原因も分かりにくい。
# ------------------------------------------------------------
function Send-TextToMac {
    param([string]$Text, [string]$Name)
    $u = "http://{0}:{1}/upload/{2}" -f $MacHost, $MacPort, $Name
    try {
        $r = Invoke-RestMethod -Uri $u -Method Post -Body $Text `
                -ContentType "text/plain; charset=utf-8" -TimeoutSec 10
        return @{ ok = $true; msg = ($r | Out-String).Trim() }
    } catch {
        return @{ ok = $false; msg = $_.Exception.Message }
    }
}

function Send-FileToMac {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ ok = $false; msg = ("送るファイルが存在しません: " + $Path) }
    }
    $u = "http://{0}:{1}/upload/{2}" -f $MacHost, $MacPort, $Name
    try {
        $r = Invoke-RestMethod -Uri $u -Method Post -InFile $Path `
                -ContentType "application/octet-stream" -TimeoutSec 30
        return @{ ok = $true; msg = ($r | Out-String).Trim() }
    } catch {
        return @{ ok = $false; msg = $_.Exception.Message }
    }
}

function Add-NG {
    param([string]$m)
    $script:NG += $m
}
function Add-Warn {
    param([string]$m)
    $script:WARN += $m
}

# ------------------------------------------------------------
#  管理者チェック（最初に1回だけ。以降ずっと同じウィンドウで作業する）
# ------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host " 管理者 PowerShell で開き直す必要があります。" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "   1. Win+X を押す（スタートボタン右クリックでも同じ）"
    Write-Host "   2.「ターミナル (管理者)」または「Windows PowerShell (管理者)」"
    Write-Host "   3. UAC の確認に「はい」"
    Write-Host "   4. cd $env:USERPROFILE\Downloads"
    Write-Host "   5. powershell -ExecutionPolicy Bypass -File .\win-check.ps1"
    Write-Host ""
    $ans = Read-Host " いま自動で開き直しますか？ (y/N)"
    if ($ans -eq "y" -or $ans -eq "Y") {
        Start-Process powershell.exe -Verb RunAs -ArgumentList @(
            "-NoExit","-ExecutionPolicy","Bypass","-File","`"$($MyInvocation.MyCommand.Path)`"")
        Write-Host " 新しいウィンドウを開きました。以降はそちらで作業してください。" -ForegroundColor Green
    }
    exit 1
}

$logPath = Join-Path $env:USERPROFILE "win-check.log"
try { Start-Transcript -Path $logPath -Force | Out-Null } catch {}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Windows 事前チェック（TikTok には一切アクセスしません）" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
#  [1] Mac との疎通（これが通らないと以降の送信が全部無駄になる）
# ============================================================
Write-Host "--- [1] Mac との疎通 ---" -ForegroundColor Cyan
$probeText = "conn test / {0} / {1}" -f $env:COMPUTERNAME, (Get-Date -Format s)
$res = Send-TextToMac -Text $probeText -Name "_conn_test.txt"

if ($res.ok) {
    Show-Result "OK" "Mac に届いた" $res.msg `
        "Mac 側のサーバー画面にも [受信] と出ているはず。以降 結果は自動で送れる"
    $script:CanSend = $true
} else {
    Show-Result "NG" "Mac に届かない" $res.msg `
        ("次を順に確認: (a) Mac で python3 <リポジトリ>/docs/mac-share-server.py が動いているか " +
         "(b) Mac の IP が {0} で合っているか (c) 初回の macOS ファイアウォール確認で「許可」を押したか" -f $MacHost)
    Add-NG "Mac との疎通ができない（結果は手で伝える必要あり）"
    $script:CanSend = $false
}

# ============================================================
#  [2] Windows のバージョン
# ============================================================
Write-Host "--- [2] Windows のバージョン ---" -ForegroundColor Cyan
$os    = Get-CimInstance Win32_OperatingSystem
$build = [System.Environment]::OSVersion.Version.Build
# OpenSSH サーバーを Add-WindowsCapability で入れられるのは 1809 (build 17763) 以降
if ($build -ge 17763) {
    Show-Result "OK" "OpenSSH を標準機能として導入できるビルド" `
        ("{0} / build {1}" -f $os.Caption, $build) `
        "build 17763 (Windows 10 1809) 以降なら Add-WindowsCapability が使える"
} else {
    Show-Result "NG" "ビルドが古い" ("{0} / build {1}" -f $os.Caption, $build) `
        "build 17763 未満だと OpenSSH を標準機能として入れられない。別の方法の判断が要るので報告すること"
    Add-NG "Windows のビルドが古い (build $build)"
}

# ============================================================
#  [3] デスクトップセッション ★コメント取得の成否を決める
# ============================================================
Write-Host "--- [3] デスクトップセッション（最重要） ---" -ForegroundColor Cyan
$qs = (query session) 2>&1 | Out-String
Write-Host "        query session の生出力:" -ForegroundColor DarkGray
$qs.TrimEnd().Split("`n") | ForEach-Object { Write-Host ("          " + $_.TrimEnd()) -ForegroundColor DarkGray }
Write-Host ""
$consoleLine = $qs.Split("`n") | Where-Object { $_ -match "console" }
$isActive    = $consoleLine -match "Active|アクティブ"
if ($isActive) {
    Show-Result "OK" "console セッションが Active" (($consoleLine | Out-String).Trim()) `
        "画面が無くてもデスクトップが生きている。Chrome が実ウィンドウで描画できる"
} else {
    Show-Result "NG" "console セッションが Active でない" (($qs).Trim()) `
        "この状態で収集すると、ブロックもエラーも出ないまま全動画 20 件で終わる（引き継ぎ書 2-9）。自動ログインの設定か、RDP で切断してロックされていないかを確認する"
    Add-NG "console セッションが Active でない（最重要）"
}

# ============================================================
#  [4] ネットワーク
# ============================================================
Write-Host "--- [4] ネットワーク ---" -ForegroundColor Cyan
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }
$ipList = ($ips | ForEach-Object { $_.IPAddress }) -join ", "
$isPrivate = $false
foreach ($ip in $ips) {
    if ($ip.IPAddress -match "^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)") { $isPrivate = $true }
}
if ($isPrivate) {
    Show-Result "OK" "NAT の内側にいる" $ipList `
        "プライベートIP。ルーターがポート転送していなければ、SSH を有効にしても外からは届かない"
} else {
    Show-Result "NG" "プライベートIPが見つからない" $ipList `
        "グローバルIPが直接付いている可能性。SSH を開ける前に必ず相談すること"
    Add-NG "NAT の内側にいるか確認できない"
}
Write-Host ("        >>> この IP を Mac 側に伝える: {0}" -f $ipList) -ForegroundColor Cyan
Write-Host ("        >>> ユーザー名も伝える       : {0}" -f (whoami)) -ForegroundColor Cyan
Write-Host ""

# ============================================================
#  [5] Chrome
# ============================================================
Write-Host "--- [5] Chrome ---" -ForegroundColor Cyan
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
    Show-Result "OK" "Chrome がある" $chrome "コメント取得は Chrome 本体が必須"
} else {
    Show-Result "NG" "Chrome が見つからない" (($chromePaths -join " / ")) `
        "3 箇所とも無い。Chrome を入れる必要がある"
    Add-NG "Chrome が入っていない"
}

# ============================================================
#  [6] Python
# ============================================================
Write-Host "--- [6] Python ---" -ForegroundColor Cyan
$pyVer = $null
foreach ($cmd in @("py -3 --version", "python --version")) {
    $out = (cmd /c "$cmd 2>&1") | Out-String
    if ($LASTEXITCODE -eq 0 -and $out -match "Python (\d+)\.(\d+)") { $pyVer = $out; break }
}
if ($pyVer -and $pyVer -match "Python (\d+)\.(\d+)") {
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
    if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 9)) {
        Show-Result "OK" "Python 3.9 以上" $pyVer.Trim() "スクレイパーと収集スクリプトの実行に必要"
    } else {
        Show-Result "NG" "Python が古い" $pyVer.Trim() "3.9 以上が必要"
        Add-NG "Python が 3.9 未満"
    }
} else {
    Show-Result "NG" "Python が見つからない" "(py -3 も python も応答なし)" `
        "python.org から 3.11 か 3.12 を入れる。インストール時に「Add python.exe to PATH」に必ずチェック"
    Add-NG "Python が入っていない"
}

# ============================================================
#  [7] 自動ログイン（再起動後もデスクトップが残るか）
# ============================================================
Write-Host "--- [7] 自動ログイン ---" -ForegroundColor Cyan
$wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
if ($wl.AutoAdminLogon -eq "1") {
    Show-Result "OK" "自動ログインが有効" ("DefaultUserName = " + $wl.DefaultUserName) `
        "再起動してもデスクトップセッションが復活する"
} else {
    Show-Result "warn" "自動ログインが無効" ("AutoAdminLogon = " + $wl.AutoAdminLogon) `
        "今日の作業には影響しないが、常時稼働させるなら再起動のたびに手でログインが必要になる"
    Add-Warn "自動ログインが無効（常時稼働させるなら要設定）"
}

# ============================================================
#  [8] OpenSSH の現状
# ============================================================
Write-Host "--- [8] OpenSSH の現状 ---" -ForegroundColor Cyan
$cap     = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue
$capState = if ($cap) { ($cap | ForEach-Object { "$($_.Name) = $($_.State)" }) -join " / " } else { "(取得できず)" }
$svc     = Get-Service -Name sshd -ErrorAction SilentlyContinue
$exePath = Join-Path $env:SystemRoot "System32\OpenSSH\sshd.exe"
$insPath = Join-Path $env:SystemRoot "System32\OpenSSH\install-sshd.ps1"
$hasExe  = Test-Path $exePath
$hasIns  = Test-Path $insPath

Write-Host ("        機能の状態       : {0}" -f $capState)
Write-Host ("        sshd サービス    : {0}" -f $(if ($svc) { $svc.Status } else { "存在しない" }))
Write-Host ("        sshd.exe         : {0}" -f $hasExe)
Write-Host ("        install-sshd.ps1 : {0}" -f $hasIns)
Write-Host ""

if ($svc) {
    Show-Result "OK" "sshd サービスが登録済み" ("Status = " + $svc.Status) `
        "あとは自動起動にして開始するだけ（win-phase1-ssh.ps1）"
} elseif ($hasExe -and $hasIns) {
    Show-Result "warn" "導入済みだがサービスが未登録" "sshd.exe はあるがサービスが無い" `
        "同梱の公式スクリプトで登録できる: & `"$insPath`"  ← これを実行してから win-phase1-ssh.ps1"
    Add-Warn "sshd サービスが未登録（install-sshd.ps1 で登録する）"
} elseif ($cap -and $capState -match "Installed" -and -not $hasExe) {
    Show-Result "warn" "導入済みなのに実体が無い" $capState `
        "再起動待ちの可能性が高い。再起動してからこのスクリプトをもう一度実行する"
    Add-Warn "OpenSSH が再起動待ちの可能性"
} else {
    Show-Result "warn" "OpenSSH 未導入" $capState `
        "これから win-phase1-ssh.ps1 で導入する（まだ実行しなくてよい）"
}

# ============================================================
#  総合判定
# ============================================================
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  総合判定" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
if ($script:NG.Count -eq 0) {
    Write-Host " [OK] 先に進める条件は揃っています。" -ForegroundColor Green
} else {
    Write-Host (" [NG] 解決が必要な項目が {0} 件あります:" -f $script:NG.Count) -ForegroundColor Red
    $script:NG | ForEach-Object { Write-Host ("        - " + $_) -ForegroundColor Red }
}
if ($script:WARN.Count -gt 0) {
    Write-Host ""
    Write-Host " 注意:" -ForegroundColor Yellow
    $script:WARN | ForEach-Object { Write-Host ("        - " + $_) -ForegroundColor Yellow }
}
Write-Host ""
Write-Host " 次にやること: この出力を Mac 側に見せて判断を仰いでください。" -ForegroundColor Cyan
Write-Host "               自分で先に進めなくて大丈夫です。" -ForegroundColor Cyan
Write-Host ""

try { Stop-Transcript | Out-Null } catch {}

# ------------------------------------------------------------
#  結果を Mac に送る
# ------------------------------------------------------------
if ($script:CanSend) {
    $res = Send-FileToMac -Path $logPath -Name "win-check.log"
    if ($res.ok) {
        Write-Host (" [送信OK] この結果を Mac に送りました: {0}" -f $res.msg) -ForegroundColor Green
        Write-Host "          Mac 側では ~/win-transfer/inbox/win-check.log で読めます。" -ForegroundColor Green
    } else {
        Write-Host (" [送信NG] 送信に失敗しました: {0}" -f $res.msg) -ForegroundColor Red
        Write-Host ("          ログはこの機械に残っています: {0}" -f $logPath) -ForegroundColor Yellow
    }
} else {
    Write-Host " [送信せず] Mac との疎通が取れていないので送っていません。" -ForegroundColor Yellow
    Write-Host ("            ログはこの機械に残っています: {0}" -f $logPath) -ForegroundColor Yellow
}
Write-Host ""
