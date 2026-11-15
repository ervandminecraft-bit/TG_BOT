# Adds/removes a small launcher in the Windows "Startup" folder,
# so the bot starts (minimized) every time you log in to Windows.
# A .cmd launcher is used instead of a .lnk shortcut because shortcuts
# created by WScript.Shell fail on non-Latin folder names.
param(
    [switch]$Remove,
    [string]$StartupDir = [Environment]::GetFolderPath('Startup')
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $StartupDir 'SubscriptionBot.cmd'

if ($Remove) {
    if (Test-Path -LiteralPath $launcher) { Remove-Item -LiteralPath $launcher -Force }
    Write-Output "Autostart removed / Автозапуск выключен: $launcher"
    exit 0
}

$runBat = Join-Path $projectDir 'run.bat'
$content = "@echo off`r`nchcp 65001 >nul`r`nstart `"Subscription bot`" /min `"$runBat`"`r`n"
[System.IO.File]::WriteAllText($launcher, $content, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Autostart enabled / Автозапуск включён: $launcher"
