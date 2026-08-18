# SubSniper updater
#
# Right-click -> "Run with PowerShell", or:
#   powershell -ExecutionPolicy Bypass -File update.ps1
#   powershell -ExecutionPolicy Bypass -File update.ps1 -Check   (look, don't install)
#
# Downloads the latest version from GitHub and replaces the program files.
#
# YOUR SETTINGS ARE NEVER TOUCHED. The list below is preserved exactly as-is:
#   .env          your logins
#   config.yaml   your job preferences
#   state\        what it has already seen and accepted
#   logs\         history
# A full backup is taken before anything is replaced.

param([switch]$Check)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

$Repo    = "Chizance/subsniper"
$ZipUrl  = "https://github.com/$Repo/archive/refs/heads/main.zip"
$ApiUrl  = "https://api.github.com/repos/$Repo/commits/main"

# Never overwritten. Everything else in the folder is program code.
$Preserve = @(".env", "config.yaml", "state", "logs", ".venv",
              "STOP", "ARMED", "Start SubSniper.bat", "VERSION.txt")

function Say([string]$m)  { Write-Host $m -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  !   $m" -ForegroundColor Yellow }
function Die([string]$m) {
    Write-Host ""
    Write-Host "STOPPED: $m" -ForegroundColor Red
    Write-Host "Nothing was changed." -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Say "======================================"
Say "  SubSniper updater"
Say "======================================"
Write-Host ""

# --- What are we running now? -------------------------------------------------
$currentSha = "unknown"
if (Test-Path "VERSION.txt") {
    $line = Get-Content "VERSION.txt" -First 1
    if ($line -match "([0-9a-f]{7,40})") { $currentSha = $Matches[1] }
}
Write-Host "Installed version: $currentSha"

# --- What's the latest? -------------------------------------------------------
Say "Checking GitHub for a newer version..."
try {
    $latest = Invoke-RestMethod -Uri $ApiUrl -Headers @{ "User-Agent" = "SubSniper-Updater" } -TimeoutSec 30
    $latestSha  = $latest.sha
    $latestWhen = $latest.commit.author.date
    $latestMsg  = ($latest.commit.message -split "`n")[0]
} catch {
    Die "Couldn't reach GitHub. Check your internet connection and try again.`n$($_.Exception.Message)"
}

Write-Host "Latest version:    $($latestSha.Substring(0,7))  ($latestWhen)"
Write-Host "                   $latestMsg"
Write-Host ""

if ($currentSha -ne "unknown" -and $latestSha.StartsWith($currentSha)) {
    Good "You're already up to date. Nothing to do."
    Write-Host ""
    exit 0
}

if ($Check) {
    Warn "An update is available. Run update.ps1 without -Check to install it."
    Write-Host ""
    exit 0
}

# --- Is it running right now? -------------------------------------------------
$lock = Join-Path $ProjectDir "state\running.lock"
if (Test-Path $lock) {
    $age = (New-TimeSpan -Start (Get-Item $lock).LastWriteTime -End (Get-Date)).TotalSeconds
    if ($age -lt 90) {
        Die @"
SubSniper is currently running.

Close the SubSniper window first (the black window that says it's polling),
then run this updater again.

If you installed it as a scheduled task, run this first:
    Stop-ScheduledTask -TaskName SubSniper
"@
    }
}

# --- Back up ------------------------------------------------------------------
$stamp     = Get-Date -Format "yyyy-MM-dd_HHmmss"
$backupDir = Join-Path $ProjectDir "backups\$stamp"
Say "Backing up your current copy..."
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
Get-ChildItem -Path $ProjectDir -Force |
    Where-Object { $_.Name -notin @(".venv", "backups") } |
    Copy-Item -Destination $backupDir -Recurse -Force -ErrorAction SilentlyContinue
Good "Backup saved to backups\$stamp"

# --- Download -----------------------------------------------------------------
Say "Downloading the new version..."
$tmp = Join-Path $env:TEMP "subsniper-update-$stamp"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$zip = Join-Path $tmp "source.zip"
try {
    Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -TimeoutSec 120
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
} catch {
    Die "Download failed. $($_.Exception.Message)"
}

# GitHub wraps everything in a folder like "subsniper-main"
$src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
if (-not $src) { Die "The download didn't contain what we expected." }
Good "Downloaded"

# --- Replace program files, keeping settings ---------------------------------
Say "Installing (your settings are being left alone)..."
$replaced = 0
Get-ChildItem -Path $src.FullName -Force | ForEach-Object {
    if ($Preserve -contains $_.Name) {
        Write-Host "      keeping your $($_.Name)"
    } else {
        Copy-Item -Path $_.FullName -Destination $ProjectDir -Recurse -Force
        $replaced++
    }
}
Good "$replaced item(s) updated"

"$latestSha  installed $(Get-Date -Format 'yyyy-MM-dd HH:mm')  $latestMsg" |
    Out-File -Encoding utf8 -FilePath (Join-Path $ProjectDir "VERSION.txt")

# --- Refresh dependencies -----------------------------------------------------
$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Say "Updating the pieces it needs..."
    & $venvPython -m pip install -e . --quiet
    if ($LASTEXITCODE -ne 0) { Warn "Dependency update had a problem - try running setup.ps1 if it misbehaves." }
    else { Good "Dependencies current" }
} else {
    Warn "No .venv found. Run setup.ps1 first."
}

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

Write-Host ""
Say "======================================"
Say "  Updated to $($latestSha.Substring(0,7))"
Say "======================================"
Write-Host ""
Write-Host "Start it again by double-clicking 'Start SubSniper.bat'."
Write-Host ""
Write-Host "If something's wrong, your previous copy is in backups\$stamp" -ForegroundColor Yellow
Write-Host ""
