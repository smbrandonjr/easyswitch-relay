<#
  Installs (or updates) EasySwitch Relay for the current Windows user.

      powershell -ExecutionPolicy Bypass -File install.ps1

  No admin rights needed. It:
    1. copies the app to %LOCALAPPDATA%\Programs\EasySwitchRelay
    2. sets the relay to start when you sign in (Task Scheduler, restarts if it crashes)
    3. adds "EasySwitch Relay" to the Start menu (opens the settings window)
    4. checks your keyboard and mouse and prints the result
    5. starts the relay and opens the settings window
  Your settings are kept when you update.

  -Update      used by the app's own updater: no device check, runs without a window
  -NoSettings  don't open the settings window at the end (unattended updates)
#>
param([switch]$Update, [switch]$NoSettings)
$ErrorActionPreference = 'Stop'
$Src      = $PSScriptRoot
$Dest     = Join-Path $env:LOCALAPPDATA 'Programs\EasySwitchRelay'
$Exe      = Join-Path $Dest 'easyswitch-relay.exe'
$TaskName = 'EasySwitch Relay'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LogPath  = Join-Path $env:LOCALAPPDATA 'EasySwitchRelay\relay.log'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'EasySwitch Relay.lnk'

if ($Update) {
    New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
    Start-Transcript -Path (Join-Path (Split-Path $LogPath) 'update.log') -Append | Out-Null
}
if (-not (Test-Path (Join-Path $Src 'easyswitch-relay.exe'))) {
    throw "easyswitch-relay.exe not found next to this script. Run install.ps1 from the built folder (dist\EasySwitchRelay-windows-x64)."
}
if ((Resolve-Path $Src).Path.TrimEnd('\') -ieq $Dest.TrimEnd('\')) {
    throw "Run install.ps1 from the built or unzipped folder, not from the installed copy."
}

# Stop a running copy so its files can be replaced.
$running = Get-Process -Name 'easyswitch-relay' -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    $running | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue   # files stay locked until exit
}
Start-Sleep -Milliseconds 300

# Copy the app (everything except the scripts).
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Get-ChildItem -Path $Src |
    Where-Object { $_.Name -notin @('install.ps1', 'uninstall.ps1') } |
    Copy-Item -Destination $Dest -Recurse -Force
# Files copied from a downloaded zip carry a "came from the internet" mark that
# makes Windows warn on every start. You chose to install this, so clear it.
Get-ChildItem -Path $Dest -Recurse -File | Unblock-File

# Start at sign-in. Task Scheduler is preferred: it restarts the relay if it
# crashes and keeps it running on battery. Fall back to the Run key if the
# task can't be created on this PC.
$user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$useTask = $true
try {
    $action    = New-ScheduledTaskAction -Execute $Exe -Argument '--run' -WorkingDirectory $Dest
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
                    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
                    -StartWhenAvailable -Priority 4
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Remove-ItemProperty -Path $RunKey -Name $TaskName -ErrorAction SilentlyContinue
    Write-Host "Set to start at sign-in (Task Scheduler: '$TaskName')."
} catch {
    $useTask = $false
    Set-ItemProperty -Path $RunKey -Name $TaskName -Value "`"$Exe`" --run"
    Write-Host "Set to start at sign-in (Startup apps). Task Scheduler wasn't available: $($_.Exception.Message)"
}

# Start menu entry: opens the settings window.
try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Shortcut)
    $lnk.TargetPath = $Exe
    $lnk.WorkingDirectory = $Dest
    $lnk.Description = 'EasySwitch Relay settings'
    $lnk.Save()
    Write-Host "Added 'EasySwitch Relay' to the Start menu (opens the settings)."
} catch {
    Write-Host "Couldn't add the Start menu entry: $($_.Exception.Message)"
}

# Check the devices. They must be connected to THIS PC right now.
if (-not $Update) {
Write-Host ""
Write-Host "Checking your keyboard and mouse (they need to be connected to this PC)."
Write-Host "Move the mouse and press a key now so both are awake (this takes up to 10 seconds)..."
$before = if (Test-Path $LogPath) { (Get-Content $LogPath).Count } else { 0 }
$env:EASYSWITCH_QUIET = '1'      # the relay writes only to its log; we print it below
Start-Process -FilePath $Exe -ArgumentList '--probe' -Wait -WindowStyle Hidden
Remove-Item Env:\EASYSWITCH_QUIET
if (Test-Path $LogPath) {
    Get-Content $LogPath | Select-Object -Skip $before | ForEach-Object { Write-Host "  $_" }
}
}

# Start the relay now, then open the settings window.
if ($useTask) { Start-ScheduledTask -TaskName $TaskName } else { Start-Process -FilePath $Exe -ArgumentList '--run' }
Start-Sleep -Seconds 1
if (-not $NoSettings) { Start-Process -FilePath $Exe }
if ($Update) { Stop-Transcript | Out-Null }
Write-Host ""
Write-Host "EasySwitch Relay is installed and running. The settings window is open;"
Write-Host "you can reopen it any time from the Start menu (EasySwitch Relay)."
Write-Host "Log: $LogPath"
