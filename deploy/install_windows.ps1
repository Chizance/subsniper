# Registers SubSniper as a Windows scheduled task that starts at boot and
# restarts itself if it dies.
#
# Run this ONCE from an elevated PowerShell prompt, from the project folder:
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
#
# Manage it afterwards from Task Scheduler, or:
#   Start-ScheduledTask  -TaskName SubSniper
#   Stop-ScheduledTask   -TaskName SubSniper
#   Get-ScheduledTaskInfo -TaskName SubSniper

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path "$PSScriptRoot\..").Path
$Python     = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "No virtualenv found at $Python. Run the setup steps in README.md first."
}

Write-Host "Project:  $ProjectDir"
Write-Host "Python:   $Python"

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m subsniper run" `
    -WorkingDirectory $ProjectDir

# Start at boot AND at logon, so it survives a reboot without anyone logging in.
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn)
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = run indefinitely

# Run as the current user so the saved browser session is readable.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "SubSniper" `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Polls Frontline Absence Management for substitute jobs and pushes alerts." `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered. Starting it now..." -ForegroundColor Green
Start-ScheduledTask -TaskName "SubSniper"
Start-Sleep -Seconds 3
Get-ScheduledTaskInfo -TaskName "SubSniper" | Format-List TaskName, LastRunTime, LastTaskResult

Write-Host ""
Write-Host "IMPORTANT: Windows must not sleep, or polling stops." -ForegroundColor Yellow
Write-Host "Run this to keep it awake while plugged in:" -ForegroundColor Yellow
Write-Host "  powercfg /change standby-timeout-ac 0" -ForegroundColor Yellow
