# 本番の Web サービス（start.bat → uvicorn＋ngrok）を PID で止めて schtasks で起こし直す。
# 止めるのは「start.bat の cmd」「ngrok の cmd と ngrok.exe」「コマンドラインが main.py の python」だけ。
# 取得の係（acquire.worker）・取得の python（tiktok-overnight / spatest）・Chrome（9222）・chromedriver には触らない。
# Mac から: docs/mac-winrun.sh docs/win-restart-web.ps1
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$p='C:\Users\astrowidow\workspace\TikTok-ugc-analyzer'
$all = Get-CimInstance Win32_Process
$root = $all | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like "*$p\start.bat*" }
$ngcmd = $all | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*ngrok http*' }
$ngrok = $all | Where-Object { $_.Name -eq 'ngrok.exe' }
$web = $all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'main\.py"?\s*$' -and $_.CommandLine -notmatch 'acquire' }
$targets = @($root) + @($ngcmd) + @($ngrok) + @($web) | Where-Object { $_ }
$targets | Select-Object ProcessId,Name,CommandLine | Format-Table -AutoSize -Wrap
foreach ($t in $targets) { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
"8000 after stop: $((Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue).Count)"
schtasks /Run /TN tiktok-ugc-web
