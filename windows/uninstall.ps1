<#
  Removes EasySwitch Relay for the current Windows user.

      powershell -ExecutionPolicy Bypass -File uninstall.ps1             # keep settings and log
      powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveLogs # remove those too

  After this, the Easy-Switch keys go back to switching only the keyboard
  (immediately, or the next time the keyboard reconnects).
#>
param([switch]$RemoveLogs)
$ErrorActionPreference = 'Stop'
$Dest     = Join-Path $env:LOCALAPPDATA 'Programs\EasySwitchRelay'
$DataDir  = Join-Path $env:LOCALAPPDATA 'EasySwitchRelay'
$TaskName = 'EasySwitch Relay'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'EasySwitch Relay.lnk'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Remove-ItemProperty -Path $RunKey -Name $TaskName -ErrorAction SilentlyContinue
Get-Process -Name 'easyswitch-relay' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
if (Test-Path $Shortcut) { Remove-Item -Force $Shortcut }
if ($RemoveLogs -and (Test-Path $DataDir)) { Remove-Item -Recurse -Force $DataDir }

Write-Host "EasySwitch Relay removed."
if (-not $RemoveLogs) { Write-Host "Settings and log kept in $DataDir (use -RemoveLogs to delete them)." }
