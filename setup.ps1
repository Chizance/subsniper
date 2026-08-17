# SubSniper setup for Windows
#
# Right-click this file and choose "Run with PowerShell", or from a terminal:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# It installs everything SubSniper needs and creates your two settings files.
# Safe to run more than once - it won't overwrite settings you've already saved.

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

function Say([string]$msg)  { Write-Host $msg -ForegroundColor Cyan }
function Good([string]$msg) { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "  !   $msg" -ForegroundColor Yellow }
function Die([string]$msg) {
    Write-Host ""
    Write-Host "STOPPED: $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Say "======================================"
Say "  SubSniper setup"
Say "======================================"
Write-Host ""

# --- Step 1: find Python ------------------------------------------------------
Say "[1/5] Looking for Python..."

$python = $null
foreach ($candidate in @("py", "python", "python3")) {
    try {
        $version = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $version -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 10) {
                $python = $candidate
                Good "Found $version"
                break
            } else {
                Warn "$version is too old (need 3.10 or newer)"
            }
        }
    } catch { }
}

if (-not $python) {
    Die @"
Python isn't installed, or it's an older version.

Download it from:  https://www.python.org/downloads/

IMPORTANT: on the first screen of the installer, tick the box that says
"Add python.exe to PATH" before clicking Install. Without that box ticked,
this script won't be able to find it.

Once Python is installed, close this window and run setup again.
"@
}

# --- Step 2: virtual environment ---------------------------------------------
Say "[2/5] Setting up a private Python folder for SubSniper..."

if (Test-Path ".venv") {
    Good "Already exists, reusing it"
} else {
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "Could not create the .venv folder." }
    Good "Created"
}

$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Die "Setup finished but $venvPython is missing." }

# --- Step 3: dependencies -----------------------------------------------------
Say "[3/5] Downloading the pieces SubSniper needs (a minute or two)..."

& $venvPython -m pip install --upgrade pip --quiet
# Installing the project itself (-e .) is what lets you run the short
# "python -m subsniper ..." commands from this folder.
& $venvPython -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) { Die "Downloading dependencies failed. Check your internet connection and try again." }
Good "Installed"

Say "      Downloading the browser it uses (this one is big, please wait)..."
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "The browser download failed. Check your internet connection and try again." }
Good "Browser ready"

# --- Step 4: settings files ---------------------------------------------------
Say "[4/5] Creating your settings files..."

if (Test-Path ".env") {
    Good "Your login file already exists - leaving it alone"
} else {
    Copy-Item ".env.example" ".env"
    Good "Created .env  (your logins go here)"
}

if (Test-Path "config.yaml") {
    Good "Your preferences file already exists - leaving it alone"
} else {
    Copy-Item "config.example.yaml" "config.yaml"
    Good "Created config.yaml  (your job preferences go here)"
}

# --- Step 5: a shortcut for later --------------------------------------------
Say "[5/5] Making a shortcut you can double-click..."

@"
@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m subsniper run
pause
"@ | Out-File -Encoding ascii -FilePath "Start SubSniper.bat"
Good "Created 'Start SubSniper.bat'"

# --- Done ---------------------------------------------------------------------
Write-Host ""
Say "======================================"
Say "  Setup finished"
Say "======================================"
Write-Host ""
Write-Host "Next: you need to type your logins into the file that's about to open."
Write-Host "Step 5 of the README explains exactly what goes in each blank."
Write-Host ""
Write-Host "Nothing will run until you do this - SubSniper doesn't know who you are yet."
Write-Host ""
Read-Host "Press Enter to open your login file in Notepad"

Start-Process notepad.exe ".env"

Write-Host ""
Write-Host "When you've saved it, come back and run this to check it works:" -ForegroundColor Cyan
Write-Host ""
Write-Host '   .venv\Scripts\python.exe -m subsniper test-notify' -ForegroundColor White
Write-Host ""
Read-Host "Press Enter to close"
