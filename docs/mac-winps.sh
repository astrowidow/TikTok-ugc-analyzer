#!/bin/sh
# Mac から Windows 機（~/.ssh/config の "win"）で PowerShell を1発実行する。
#   docs/mac-winps.sh 'Get-Service sshd | Format-Table -AutoSize'
# 前提: Windows 側の OpenSSH の既定シェルが PowerShell（2026-09-11 に設定済み）。
# 出力を UTF-8 にしないと日本語 Windows は CP932 で返してきて Mac 側で化ける。
exec ssh -o BatchMode=yes win "[Console]::OutputEncoding=[Text.Encoding]::UTF8; \$ProgressPreference='SilentlyContinue'; $*"
