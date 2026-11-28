<#
.SYNOPSIS
    Set up and start AutoClipper (backend + web app) for a demo, from any clone of the repo.

.DESCRIPTION
    Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
    (scripts/demo.sh is the macOS/Linux equivalent.)
    It installs dependencies, fills in what .env and web\.env.local need, checks tools,
    starts `autoclipper serve` and the Next.js dev server in their own windows, waits for
    both, and opens the browser. Re-running it is safe: finished steps are skipped.

    Without Twitch credentials in .env it starts the backend in mock mode (built-in sample
    jobs, no Twitch access), so the UI can always be shown.

.PARAMETER Mock
    Force mock mode: sample jobs instead of real analysis.
.PARAMETER SkipInstall
    Skip `uv sync`, `npm ci` and tool downloads (faster when already set up).
.PARAMETER DemoVod
    The VOD link to copy to the clipboard for the start page.
.PARAMETER ApiPort
    Backend port (default 8000).
.PARAMETER WebPort
    Web app port (default 3000).
#>
[CmdletBinding()]
param(
    [switch]$Mock,
    [switch]$SkipInstall,
    [string]$DemoVod = "https://www.twitch.tv/videos/2941757172",
    [int]$ApiPort = 8000,
    [int]$WebPort = 3000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Fail([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

# Single-quote a value for a -Command string (paths may contain apostrophes).
function ConvertTo-Quoted([string]$Value) { "'" + ($Value -replace "'", "''") + "'" }

function Test-Port([int]$Port) {
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

# Body of a URL, or $null when nothing answers.
function Get-UrlBody([string]$Url, [int]$TimeoutSec) {
    try { (Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec $TimeoutSec).Content } catch { $null }
}

# True when web\node_modules is missing or older than web\package-lock.json (e.g. after a pull).
function Test-WebDepsStale {
    $installed = Join-Path $Root "web\node_modules\.package-lock.json"
    if (-not (Test-Path $installed)) { return $true }
    (Get-Item (Join-Path $Root "web\package-lock.json")).LastWriteTimeUtc -gt (Get-Item $installed).LastWriteTimeUtc
}

# Reads NAME=value like python-dotenv: quotes and spaces are dropped, and an unquoted value's
# " # comment" is ignored (so "NAME= # x" counts as empty).
function Get-EnvValue([string]$Path, [string]$Name) {
    if (-not (Test-Path $Path)) { return "" }
    $line = Get-Content -Encoding UTF8 $Path | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
    if ($null -eq $line) { return "" }
    $value = ($line -split "=", 2)[1].Trim()
    if ($value -match '^"([^"]*)"') { return $Matches[1] }
    if ($value -match "^'([^']*)'") { return $Matches[1] }
    if ($value.StartsWith("#")) { return "" }
    return ($value -replace '\s+#.*$', '').Trim()
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content -Encoding UTF8 $Path)
    $found = $false
    # @() keeps a one-line result an array, so += below appends a line instead of concatenating.
    $lines = @($lines | ForEach-Object {
        if (-not $found -and $_ -match "^\s*$Name\s*=") { $found = $true; "$Name=$Value" } else { $_ }
    })
    if (-not $found) { $lines += "$Name=$Value" }
    # UTF-8 without BOM, LF endings, so python-dotenv reads it the same everywhere.
    [IO.File]::WriteAllText($Path, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
}

# 1. Prerequisites -----------------------------------------------------------
Write-Step "Checking prerequisites"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv is not installed. Install it: winget install astral-sh.uv  (then open a new terminal)"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail "Node.js is not installed. Install it: winget install OpenJS.NodeJS.LTS  (then open a new terminal)"
}
Write-Host "uv and Node.js found."

# 2. .env --------------------------------------------------------------------
Write-Step "Preparing .env"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "Created .env from .env.example."
}
if (-not (Get-EnvValue $EnvFile "AUTOCLIPPER_CHAT_HASH_SALT")) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    Set-EnvValue $EnvFile "AUTOCLIPPER_CHAT_HASH_SALT" (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
    Write-Host "Generated AUTOCLIPPER_CHAT_HASH_SALT (keeps chat and viewer clips enabled)."
}
$HasTwitch = (Get-EnvValue $EnvFile "TWITCH_CLIENT_ID") -and (Get-EnvValue $EnvFile "TWITCH_CLIENT_SECRET")
if (-not $HasTwitch -and -not $Mock) {
    Write-Host "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are empty in .env." -ForegroundColor Yellow
    Write-Host "Add the team's shared Twitch app credentials (or your own from https://dev.twitch.tv/console) for real analysis. Starting in MOCK mode for now." -ForegroundColor Yellow
    $Mock = $true
}
$Jobs = if ($Mock) { "mock" } else { "pipeline" }

# 3. Dependencies and tools --------------------------------------------------
if (-not $SkipInstall) {
    Write-Step "Installing Python dependencies (uv sync --extra captions)"
    uv sync --extra captions
    if ($LASTEXITCODE -ne 0) {
        # The captions extra (faster-whisper/onnxruntime) has no wheel for some platforms, e.g.
        # Windows on ARM. Everything else works without it.
        Write-Host "Captions unavailable on this platform; installing without them (exports work with captions off)." -ForegroundColor Yellow
        uv sync
        if ($LASTEXITCODE -ne 0) { Fail "uv sync failed." }
    }

    # Mock mode still needs FFmpeg (the backend renders sample videos); pipeline mode also
    # needs TwitchDownloaderCLI. Both are skipped quickly when already installed.
    if ($Mock) {
        Write-Step "Installing pinned FFmpeg if missing (autoclipper setup-tools --tool ffmpeg)"
        uv run autoclipper setup-tools --tool ffmpeg
    } else {
        Write-Step "Installing pinned FFmpeg and TwitchDownloaderCLI if missing (autoclipper setup-tools)"
        uv run autoclipper setup-tools
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "setup-tools reported a problem; see the doctor output below." -ForegroundColor Yellow }

    Write-Step "Installing web dependencies (npm ci)"
    if (-not (Test-WebDepsStale)) {
        Write-Host "web\node_modules is up to date; skipping."
    } else {
        Push-Location (Join-Path $Root "web")
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
        } finally { Pop-Location }
    }
} elseif (-not (Test-Path (Join-Path $Root ".venv")) -or -not (Test-Path (Join-Path $Root "web\node_modules"))) {
    Fail "-SkipInstall needs an existing .venv and web\node_modules. Run once without it."
} elseif (Test-WebDepsStale) {
    Write-Host "web\package-lock.json changed since the last npm ci; run once without -SkipInstall if the web app fails." -ForegroundColor Yellow
}

$WebEnv = Join-Path $Root "web\.env.local"
$ApiOrigin = "http://127.0.0.1:$ApiPort"
if (-not (Test-Path $WebEnv)) { Set-Content -Path $WebEnv -Value "" -Encoding ascii }
Set-EnvValue $WebEnv "NEXT_PUBLIC_API_ORIGIN" $ApiOrigin

# 4. Health check ------------------------------------------------------------
Write-Step "Checking setup (autoclipper doctor)"
uv run autoclipper doctor --jobs $Jobs
if ($LASTEXITCODE -ne 0) { Write-Host "doctor reported problems (see above); continuing." -ForegroundColor Yellow }

# 5. Start -------------------------------------------------------------------
Write-Step "Starting the app"
$ChildPs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command")
# An open port must belong to AutoClipper; otherwise stop now instead of waiting 3 minutes.
# Both ports are checked before anything starts.
$ApiRunning = Test-Port $ApiPort
$WebRunning = Test-Port $WebPort
if ($ApiRunning) {
    $health = $null
    try { $health = Get-UrlBody "$ApiOrigin/api/v1/health" 30 | ConvertFrom-Json } catch { }
    if ($null -eq $health -or $null -eq $health.job_runner) {
        Fail "Port $ApiPort is in use by another program (it does not answer as the AutoClipper backend). Stop that program or pick another port with -ApiPort N."
    }
    if ($health.job_runner -ne $Jobs) {
        Write-Host "The backend already on port $ApiPort runs $($health.job_runner) jobs, not $Jobs; stop it to switch." -ForegroundColor Yellow
    }
    Write-Host "Port $ApiPort already has an AutoClipper backend; using it." -ForegroundColor Yellow
}
if ($WebRunning) {
    $page = Get-UrlBody "http://localhost:$WebPort" 30
    if ($null -eq $page -or -not $page.Contains("AutoClipper")) {
        Fail "Port $WebPort is in use by another program (it does not answer as the AutoClipper web app). Stop that program or pick another port with -WebPort N."
    }
    Write-Host "Port $WebPort already has the AutoClipper web app; using it." -ForegroundColor Yellow
}
if (-not $ApiRunning) {
    # The backend's default CORS origins only cover port 3000.
    $Cors = ""
    if ($WebPort -ne 3000 -and -not $env:AUTOCLIPPER_CORS_ORIGINS) {
        $Cors = "`$env:AUTOCLIPPER_CORS_ORIGINS = 'http://localhost:$WebPort,http://127.0.0.1:$WebPort'; "
    }
    Start-Process powershell -ArgumentList ($ChildPs + @(
        "`$Host.UI.RawUI.WindowTitle = 'AutoClipper backend'; Set-Location $(ConvertTo-Quoted $Root); ${Cors}uv run autoclipper serve --port $ApiPort --jobs $Jobs"))
}
if (-not $WebRunning) {
    # npx rather than `npm run dev -- --port`: Windows PowerShell 5.1 drops the `--` for npm.
    Start-Process powershell -ArgumentList ($ChildPs + @(
        "`$Host.UI.RawUI.WindowTitle = 'AutoClipper web'; Set-Location $(ConvertTo-Quoted (Join-Path $Root 'web')); npx --no-install next dev --port $WebPort"))
}

Write-Host "Waiting for the backend and web app (first start can take a minute)..."
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing "$ApiOrigin/api/v1/health" -TimeoutSec 2 | Out-Null
        Invoke-WebRequest -UseBasicParsing "http://localhost:$WebPort" -TimeoutSec 10 | Out-Null
        $ready = $true
        break
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { Fail "The app did not come up in 3 minutes. Check the two new windows for errors." }

Start-Process "http://localhost:$WebPort"
$Copied = $true
try { Set-Clipboard $DemoVod } catch { $Copied = $false }
Write-Host "`nAutoClipper is running ($Jobs mode)." -ForegroundColor Green
Write-Host "  Web app:  http://localhost:$WebPort"
Write-Host "  API docs: $ApiOrigin/docs"
if ($Copied) {
    Write-Host "  Demo VOD: $DemoVod  (copied to clipboard; paste it on the start page)"
} else {
    Write-Host "  Demo VOD: $DemoVod  (paste it on the start page)"
}
Write-Host "Close the two AutoClipper windows to stop it."
