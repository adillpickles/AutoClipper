<#
.SYNOPSIS
    Set up and start AutoClipper (backend + web app) for a demo, from any clone of the repo.

.DESCRIPTION
    Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
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

function Test-Port([int]$Port) {
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-EnvValue([string]$Path, [string]$Name) {
    if (-not (Test-Path $Path)) { return "" }
    $line = Get-Content $Path | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
    if ($null -eq $line) { return "" }
    return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content $Path)
    $found = $false
    $lines = $lines | ForEach-Object {
        if ($_ -match "^\s*$Name\s*=") { $found = $true; "$Name=$Value" } else { $_ }
    }
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
    Write-Host "Ask the team lead for the shared Twitch app credentials. Starting in MOCK mode for now." -ForegroundColor Yellow
    $Mock = $true
}

# 3. Dependencies and tools --------------------------------------------------
if (-not $SkipInstall) {
    Write-Step "Installing Python dependencies (uv sync --extra captions)"
    uv sync --extra captions
    if ($LASTEXITCODE -ne 0) { Fail "uv sync failed." }

    if (-not $Mock) {
        Write-Step "Installing pinned FFmpeg and TwitchDownloaderCLI if missing (autoclipper setup-tools)"
        uv run autoclipper setup-tools
        if ($LASTEXITCODE -ne 0) { Write-Host "setup-tools reported a problem; see the doctor output below." -ForegroundColor Yellow }
    }

    Write-Step "Installing web dependencies (npm ci)"
    Push-Location (Join-Path $Root "web")
    try {
        if (-not (Test-Path "node_modules")) { npm ci } else { Write-Host "web\node_modules exists; skipping." }
        if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
    } finally { Pop-Location }
}

$WebEnv = Join-Path $Root "web\.env.local"
$ApiOrigin = "http://127.0.0.1:$ApiPort"
if (-not (Test-Path $WebEnv)) { Set-Content -Path $WebEnv -Value "" -Encoding ascii }
Set-EnvValue $WebEnv "NEXT_PUBLIC_API_ORIGIN" $ApiOrigin

# 4. Health check ------------------------------------------------------------
Write-Step "Checking setup (autoclipper doctor)"
uv run autoclipper doctor

# 5. Start -------------------------------------------------------------------
Write-Step "Starting the app"
$Jobs = if ($Mock) { "mock" } else { "pipeline" }
if (Test-Port $ApiPort) {
    Write-Host "Something is already listening on port $ApiPort; assuming the backend is running." -ForegroundColor Yellow
} else {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "`$Host.UI.RawUI.WindowTitle = 'AutoClipper backend'; Set-Location '$Root'; uv run autoclipper serve --port $ApiPort --jobs $Jobs"
}
if (Test-Port $WebPort) {
    Write-Host "Something is already listening on port $WebPort; assuming the web app is running." -ForegroundColor Yellow
} else {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "`$Host.UI.RawUI.WindowTitle = 'AutoClipper web'; Set-Location '$Root\web'; npx --no-install next dev --port $WebPort"
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
Set-Clipboard $DemoVod
Write-Host "`nAutoClipper is running ($Jobs mode)." -ForegroundColor Green
Write-Host "  Web app:  http://localhost:$WebPort"
Write-Host "  API docs: $ApiOrigin/docs"
Write-Host "  Demo VOD: $DemoVod  (copied to clipboard; paste it on the start page)"
Write-Host "Close the two AutoClipper windows to stop it."
