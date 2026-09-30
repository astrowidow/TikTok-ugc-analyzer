# ============================================================
#  TikTok UGC Analyzer - Windows Phase 1 / SSH を有効にする
#
#  実行方法（管理者 PowerShell が必要）:
#    Win+X → 「ターミナル (管理者)」 を開いてから
#      cd $env:USERPROFILE\Downloads
#      powershell -ExecutionPolicy Bypass -File .\win-phase1-ssh.ps1
#
#  管理者でなければ、昇格するかどうかを聞いたうえで自動で開き直します。
# ============================================================

$ErrorActionPreference = "Continue"

# ------------------------------------------------------------
#  管理者かどうかを最初に判定する（Add-WindowsCapability に必要）
# ------------------------------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host " 管理者権限がありません。" -ForegroundColor Yellow
    Write-Host "" -ForegroundColor Yellow
    Write-Host " Add-WindowsCapability（OpenSSH サーバーの導入）には管理者権限が要ります。" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host " 手動でやる場合:"
    Write-Host "   1. Win+X →「ターミナル (管理者)」または「Windows PowerShell (管理者)」"
    Write-Host "   2. UAC の確認に「はい」"
    Write-Host "   3. cd $env:USERPROFILE\Downloads"
    Write-Host "   4. powershell -ExecutionPolicy Bypass -File .\win-phase1-ssh.ps1"
    Write-Host ""
    $ans = Read-Host " いま自動で管理者ウィンドウを開き直しますか？ (y/N)"
    if ($ans -eq "y" -or $ans -eq "Y") {
        $me = $MyInvocation.MyCommand.Path
        # -NoExit を付けないと、終了と同時にウィンドウが閉じて出力が読めなくなる
        Start-Process powershell.exe -Verb RunAs -ArgumentList @(
            "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$me`""
        )
        Write-Host " 新しいウィンドウを開きました。そちらの出力を見てください。" -ForegroundColor Green
    } else {
        Write-Host " 中止しました。管理者で開き直してから再実行してください。" -ForegroundColor Yellow
    }
    exit 1
}

# ------------------------------------------------------------
#  出力をログにも残す（あとで Mac 側から読めるようにするため）
# ------------------------------------------------------------
$logPath = Join-Path $env:USERPROFILE "win-phase1-ssh.log"
try { Start-Transcript -Path $logPath -Force | Out-Null } catch {}
Write-Host "ログ: $logPath" -ForegroundColor DarkGray

Write-Host ""
Write-Host "=== [1/5] 現状の確認 ===" -ForegroundColor Cyan
Write-Host "--- OSバージョン ---"
[System.Environment]::OSVersion.Version
Write-Host "--- ログオン中のユーザー ---"
whoami
Write-Host "--- セッション一覧（console が Active でないと描画が止まる）---"
query session
Write-Host "--- IPアドレス ---"
ipconfig | Select-String "IPv4"

Write-Host ""
Write-Host "=== [2/5] OpenSSH サーバーを導入 ===" -ForegroundColor Cyan

$capName = "OpenSSH.Server~~~~0.0.1.0"
$cap = Get-WindowsCapability -Online -Name $capName -ErrorAction SilentlyContinue
Write-Host ("現在の状態: " + $(if ($cap) { $cap.State } else { "(取得できず)" }))

if ($cap -and $cap.State -eq "Installed") {
    Write-Host "機能としては導入済みです。"
} else {
    try {
        $r = Add-WindowsCapability -Online -Name $capName -ErrorAction Stop
        Write-Host "導入コマンドは成功しました。"
        if ($r.RestartNeeded) {
            Write-Host ""
            Write-Host "[重要] 再起動が必要と報告されました。" -ForegroundColor Yellow
            Write-Host "  再起動してから、このスクリプトをもう一度実行してください。" -ForegroundColor Yellow
            try { Stop-Transcript | Out-Null } catch {}
            exit 2
        }
    } catch {
        Write-Host ""
        Write-Host "[エラー] OpenSSH サーバーの導入に失敗しました:" -ForegroundColor Red
        Write-Host "  $_" -ForegroundColor Red
        Write-Host ""
        Write-Host "  0x800f0954 の場合は WSUS/グループポリシーで Windows Update が制限されています。" -ForegroundColor Yellow
        Write-Host "  この機械がそういう管理下にあるなら勝手に回避してよいものではないので、" -ForegroundColor Yellow
        Write-Host "  このメッセージごと報告して判断を仰いでください。" -ForegroundColor Yellow
        try { Stop-Transcript | Out-Null } catch {}
        exit 1
    }
}

Write-Host ""
Write-Host "=== [2b/5] サービスが登録されているか確認 ===" -ForegroundColor Cyan
# 「機能は導入済みなのに sshd サービスが存在しない」ことが実際に起きる。
# 導入コマンドが例外を投げなかったことを成功と見なしてはいけない。
$svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "sshd サービスは登録されています。"
} else {
    Write-Host "sshd サービスがありません。登録スクリプトを探します。" -ForegroundColor Yellow
    $exe       = Join-Path $env:SystemRoot "System32\OpenSSH\sshd.exe"
    $installer = Join-Path $env:SystemRoot "System32\OpenSSH\install-sshd.ps1"
    Write-Host ("  sshd.exe        : " + (Test-Path $exe))
    Write-Host ("  install-sshd.ps1: " + (Test-Path $installer))

    if (-not (Test-Path $exe)) {
        Write-Host ""
        Write-Host "[中断] sshd.exe が存在しません。導入が完了していないか、再起動待ちです。" -ForegroundColor Red
        Write-Host "  一度再起動してから、このスクリプトを再実行してください。" -ForegroundColor Yellow
        Write-Host "  再起動しても変わらなければ、上の出力ごと報告してください。" -ForegroundColor Yellow
        try { Stop-Transcript | Out-Null } catch {}
        exit 1
    }

    if (Test-Path $installer) {
        # OpenSSH に同梱されている公式の登録スクリプト。sshd と ssh-agent を登録する
        & $installer
        $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
    }

    if (-not $svc) {
        Write-Host ""
        Write-Host "[中断] サービスを登録できませんでした。上の出力ごと報告してください。" -ForegroundColor Red
        try { Stop-Transcript | Out-Null } catch {}
        exit 1
    }
    Write-Host "登録しました。" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== [3/5] サービスを自動起動にして開始 ===" -ForegroundColor Cyan
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-Service sshd | Format-Table Name,Status,StartType -AutoSize

Write-Host ""
Write-Host "=== [4/5] ファイアウォールをローカルネットだけに絞る ===" -ForegroundColor Cyan
# これをやらないと「到達できる範囲」が既定で Any のままになる。
# ルーターにポート転送が無ければ外からは届かないが、二重に塞いでおく。
try {
    Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -RemoteAddress LocalSubnet -ErrorAction Stop
    Write-Host "OpenSSH-Server-In-TCP を LocalSubnet に限定しました。"
} catch {
    Write-Host "[警告] ルールが見つかりませんでした。手動で確認してください: $_" -ForegroundColor Yellow
}
Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue |
    Get-NetFirewallAddressFilter

Write-Host ""
Write-Host "=== [5/5] 待ち受け確認 ===" -ForegroundColor Cyan
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue |
    Format-Table LocalAddress,LocalPort,State -AutoSize

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 完了。次の2つを Mac 側に伝えてください:" -ForegroundColor Green
Write-Host "   IPアドレス : 上の IPv4 の値" -ForegroundColor Green
Write-Host "   ユーザー名 : $(whoami)" -ForegroundColor Green
Write-Host ""
Write-Host " このウィンドウの内容は次にも残っています: $logPath" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

try { Stop-Transcript | Out-Null } catch {}

# ------------------------------------------------------------
#  結果を Mac に送る（win-check.ps1 と同じ経路。書き写し不要にするため）
# ------------------------------------------------------------
$MacHost = "192.168.0.52"
$MacPort = 8765
try {
    $r = Invoke-RestMethod -Uri ("http://{0}:{1}/upload/win-phase1-ssh.log" -f $MacHost, $MacPort) `
            -Method Post -InFile $logPath -ContentType "application/octet-stream" -TimeoutSec 30
    Write-Host (" [送信OK] このログを Mac に送りました: {0}" -f (($r | Out-String).Trim())) -ForegroundColor Green
} catch {
    Write-Host (" [送信NG] Mac に送れませんでした: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    Write-Host ("          ログはこの機械に残っています: {0}" -f $logPath) -ForegroundColor Yellow
}
