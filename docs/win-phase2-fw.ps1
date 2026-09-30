# ============================================================
#  TikTok UGC Analyzer - Windows Phase 2b / SSH が Mac から届かないときの診断と修正
#
#  前提: win-phase1-ssh.ps1 が完了していて、sshd は Running・22番で Listen している。
#        それでも Mac からポート22がタイムアウトする状態。
#
#  やること:
#    [1] ファイアウォールの状態を全部出す（ルールのプロファイル・ネットワークの種類・他の22番ルール）
#    [2] OpenSSH のルールを「有効 / 全プロファイル / LocalSubnet 限定」に揃える
#    [3] Mac の公開鍵を administrators_authorized_keys に登録して ACL を締める（無人操作のため）
#    [4] sshd を再起動して再確認
#    [5] ログを Mac に送る
#
#  管理者 PowerShell で:
#    powershell -ExecutionPolicy Bypass -File .\win-phase2-fw.ps1
# ============================================================

$MacHost = "192.168.0.52"
$MacPort = 8765
$ErrorActionPreference = "Continue"

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host " 管理者 PowerShell で実行してください（Win+X → ターミナル(管理者)）。" -ForegroundColor Yellow
    exit 1
}

$logPath = Join-Path $env:USERPROFILE "win-phase2-fw.log"
try { Start-Transcript -Path $logPath -Force | Out-Null } catch {}

function Show-Rule {
    param([string]$Label)
    Write-Host ("--- {0} ---" -f $Label) -ForegroundColor Cyan
    $r = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
    if (-not $r) { Write-Host "  ルール OpenSSH-Server-In-TCP が存在しない" -ForegroundColor Red; return }
    Write-Host ("  Enabled   : {0}" -f $r.Enabled)
    Write-Host ("  Profile   : {0}" -f $r.Profile)
    Write-Host ("  Direction : {0} / Action : {1}" -f $r.Direction, $r.Action)
    $pf = $r | Get-NetFirewallPortFilter
    Write-Host ("  Protocol  : {0} / LocalPort : {1}" -f $pf.Protocol, $pf.LocalPort)
    $af = $r | Get-NetFirewallAddressFilter
    Write-Host ("  RemoteAddress : {0}" -f ($af.RemoteAddress -join ", "))
}

Write-Host ""
Write-Host "=== [1] 診断 ===" -ForegroundColor Cyan
Show-Rule "OpenSSH のルール（修正前）"

Write-Host "--- ネットワークの種類（Public だと Private 限定のルールは効かない） ---" -ForegroundColor Cyan
Get-NetConnectionProfile | Format-Table InterfaceAlias, NetworkCategory, IPv4Connectivity -AutoSize

Write-Host "--- ファイアウォールのプロファイル ---" -ForegroundColor Cyan
Get-NetFirewallProfile | Format-Table Name, Enabled, DefaultInboundAction -AutoSize

Write-Host "--- 22番に関わる他のルール（Block があればそれが原因） ---" -ForegroundColor Cyan
Get-NetFirewallPortFilter | Where-Object { $_.LocalPort -eq 22 -or $_.LocalPort -eq "22" } |
    Get-NetFirewallRule | Format-Table Name, Enabled, Profile, Direction, Action -AutoSize

Write-Host "--- サードパーティのファイアウォール（Windows 以外が握っていないか） ---" -ForegroundColor Cyan
try {
    Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct -ErrorAction Stop |
        Format-Table displayName, productState -AutoSize
} catch { Write-Host "  (取得できず: $_)" }

Write-Host "--- 22番の待ち受け ---" -ForegroundColor Cyan
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue |
    Format-Table LocalAddress, LocalPort, State -AutoSize

Write-Host ""
Write-Host "=== [2] OpenSSH のルールを 有効 / 全プロファイル / LocalSubnet に揃える ===" -ForegroundColor Cyan
$r = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if ($r) {
    Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Enabled True -Profile Any -RemoteAddress LocalSubnet
    Write-Host "  設定しました。"
} else {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 `
        -Profile Any -RemoteAddress LocalSubnet | Out-Null
    Write-Host "  ルールが無かったので作りました。"
}
Show-Rule "OpenSSH のルール（修正後）"

Write-Host ""
Write-Host "=== [3] Mac の公開鍵を登録（管理者アカウント用の場所） ===" -ForegroundColor Cyan
$keysPath = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"
try {
    $resp = Invoke-WebRequest -Uri ("http://{0}:{1}/id_ed25519.pub" -f $MacHost, $MacPort) -UseBasicParsing -TimeoutSec 10
    # サーバーが .pub を application/octet-stream で返すので Content が byte[] になる。文字列に戻す
    $pub = $resp.Content
    if ($pub -is [byte[]]) { $pub = [Text.Encoding]::ASCII.GetString($pub) }
    $pub = ([string]$pub).Trim()
    if ($pub -notmatch '^ssh-ed25519 ') { throw "公開鍵の形式が想定外: $pub" }
    Write-Host ("  受け取った鍵: {0}" -f $pub)

    $existing = @()
    if (Test-Path $keysPath) { $existing = Get-Content $keysPath -ErrorAction SilentlyContinue }
    if ($existing -contains $pub) {
        Write-Host "  既に登録済み。"
    } else {
        $all = @($existing | Where-Object { $_ -and $_.Trim() }) + $pub
        # BOM 無し・改行 LF で書く（sshd が読むファイルなので）
        [IO.File]::WriteAllText($keysPath, (($all -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
        Write-Host "  追記しました。"
    }
    # ACL: 継承を切り、SYSTEM と Administrators だけにする（これが緩いと sshd が鍵を無視する）
    # グループ名は日本語環境でも SID 指定なら確実
    icacls.exe $keysPath /inheritance:r /grant "*S-1-5-18:F" /grant "*S-1-5-32-544:F" | Out-Null
    Write-Host "  ACL:"
    icacls.exe $keysPath
} catch {
    Write-Host ("  [NG] 鍵の登録に失敗: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "       パスワード認証は引き続き使えるので、ここは後回しにできる" -ForegroundColor Yellow
}

Write-Host "--- sshd_config の管理者用鍵の設定（既定で有効なはず） ---" -ForegroundColor Cyan
$cfg = Join-Path $env:ProgramData "ssh\sshd_config"
if (Test-Path $cfg) {
    Select-String -Path $cfg -Pattern "administrators_authorized_keys|^\s*PubkeyAuthentication|^\s*PasswordAuthentication|^\s*Match Group" |
        ForEach-Object { Write-Host ("  {0}: {1}" -f $_.LineNumber, $_.Line) }
} else {
    Write-Host "  sshd_config が無い（sshd を一度も起動していない?）" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== [4] sshd を再起動して再確認 ===" -ForegroundColor Cyan
Restart-Service sshd
Start-Sleep -Seconds 2
Get-Service sshd | Format-Table Name, Status, StartType -AutoSize
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue |
    Format-Table LocalAddress, LocalPort, State -AutoSize

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 完了。Mac 側で ssh の疎通を確認します。" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

try { Stop-Transcript | Out-Null } catch {}

# ------------------------------------------------------------
#  [5] ログを Mac に送る
# ------------------------------------------------------------
try {
    $r = Invoke-RestMethod -Uri ("http://{0}:{1}/upload/win-phase2-fw.log" -f $MacHost, $MacPort) `
            -Method Post -InFile $logPath -ContentType "application/octet-stream" -TimeoutSec 30
    Write-Host (" [送信OK] {0}" -f (($r | Out-String).Trim())) -ForegroundColor Green
} catch {
    Write-Host (" [送信NG] {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    Write-Host ("          ログ: {0}" -f $logPath) -ForegroundColor Yellow
}
