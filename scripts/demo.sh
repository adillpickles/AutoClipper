#!/usr/bin/env bash
# Set up and start AutoClipper (backend + web app) for a demo, from any clone of the repo.
#
# Run from anywhere:  bash scripts/demo.sh [--mock] [--skip-install] [--demo-vod URL]
#                                          [--api-port N] [--web-port N]
#
# It installs dependencies, fills in what .env and web/.env.local need, checks tools, starts
# `autoclipper serve` and the Next.js dev server, waits for both and opens the browser. Ctrl+C
# stops both servers. Re-running it is safe: finished steps are skipped.
#
# Without Twitch credentials in .env it starts the backend in mock mode (built-in sample jobs,
# no Twitch access), so the UI can always be shown.
#
# Works with the bash 3.2 that ships with macOS and with Linux; uses only POSIX sed/awk/grep.
# scripts/demo.ps1 is the Windows equivalent.

set -eu

MOCK=0
SKIP_INSTALL=0
DEMO_VOD="https://www.twitch.tv/videos/2941757172"
API_PORT=8000
WEB_PORT=3000

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --mock            Force mock mode: sample jobs instead of real analysis.
  --skip-install    Skip `uv sync`, `npm ci` and tool downloads (faster when already set up).
  --demo-vod URL    The VOD link to print (and copy) for the start page.
  --api-port N      Backend port (default 8000).
  --web-port N      Web app port (default 3000).
  -h, --help        Show this help.
EOF
}

need_value() {
    if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "ERROR: $1 needs a value." >&2
        exit 2
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mock) MOCK=1 ;;
        --skip-install) SKIP_INSTALL=1 ;;
        --demo-vod) need_value "$@"; DEMO_VOD="$2"; shift ;;
        --demo-vod=*) DEMO_VOD="${1#*=}" ;;
        --api-port) need_value "$@"; API_PORT="$2"; shift ;;
        --api-port=*) API_PORT="${1#*=}" ;;
        --web-port) need_value "$@"; WEB_PORT="$2"; shift ;;
        --web-port=*) WEB_PORT="${1#*=}" ;;
        -h | --help) usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

for port in "$API_PORT" "$WEB_PORT"; do
    case "$port" in
        '' | *[!0-9]*) echo "ERROR: port must be a number, got '$port'." >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -t 1 ]; then
    C_STEP=$(printf '\033[36m'); C_WARN=$(printf '\033[33m'); C_ERR=$(printf '\033[31m')
    C_OK=$(printf '\033[32m'); C_OFF=$(printf '\033[0m')
else
    C_STEP=""; C_WARN=""; C_ERR=""; C_OK=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
warn() { printf '%s%s%s\n' "$C_WARN" "$1" "$C_OFF"; }
fail() { printf '%sERROR: %s%s\n' "$C_ERR" "$1" "$C_OFF" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

OS_NAME="$(uname -s)"

# Reads NAME=value from an env file (first match), without surrounding quotes or spaces.
get_env_value() {
    [ -f "$1" ] || return 0
    grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" | head -n 1 | cut -d= -f2- |
        sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" |
        tr -d '\r'
}

# Sets NAME=value in an env file, replacing the first NAME= line or appending one.
set_env_value() {
    tmp="$1.tmp.$$"
    awk -v name="$2" -v value="$3" '
        BEGIN { pattern = "^[[:space:]]*" name "[[:space:]]*=" }
        { sub(/\r$/, "") }
        $0 ~ pattern && !done { print name "=" value; done = 1; next }
        { print }
        END { if (!done) print name "=" value }
    ' "$1" >"$tmp"
    mv "$tmp" "$1"
}

random_hex() {
    if have openssl && openssl rand -hex 32 2>/dev/null; then return 0; fi
    for py in python3 python; do
        if have "$py" && "$py" -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null; then
            return 0
        fi
    done
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    echo
}

# True when something accepts TCP connections on 127.0.0.1:PORT.
port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

url_ok() {
    if have curl; then
        curl -fsS -o /dev/null --max-time "$2" "$1" 2>/dev/null
    else
        port_open "$3"
    fi
}

open_browser() {
    case "$OS_NAME" in
        Darwin) open "$1" >/dev/null 2>&1 || true ;;
        MINGW* | MSYS* | CYGWIN*) cmd.exe //c start "" "$1" >/dev/null 2>&1 || true ;;
        *)
            if have xdg-open; then
                xdg-open "$1" >/dev/null 2>&1 || true
            else
                warn "Open $1 in your browser."
            fi
            ;;
    esac
}

copy_to_clipboard() {
    if have pbcopy; then
        printf '%s' "$1" | pbcopy
    elif have wl-copy && [ -n "${WAYLAND_DISPLAY:-}" ]; then
        printf '%s' "$1" | wl-copy
    elif have xclip && [ -n "${DISPLAY:-}" ]; then
        printf '%s' "$1" | xclip -selection clipboard
    elif have clip.exe; then
        printf '%s' "$1" | clip.exe
    else
        return 1
    fi
}

# 1. Prerequisites -----------------------------------------------------------
step "Checking prerequisites"
if ! have uv; then
    fail "uv is not installed. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh  (then open a new terminal)"
fi
if ! have npm || ! have npx; then
    if [ "$OS_NAME" = "Darwin" ]; then
        fail "Node.js is not installed. Install Node.js 24 LTS from https://nodejs.org/ (or: brew install node@24), then open a new terminal."
    fi
    fail "Node.js is not installed. Install Node.js 24 LTS from https://nodejs.org/ or your package manager (nvm works too), then open a new terminal."
fi
echo "uv and Node.js found."

# 2. .env --------------------------------------------------------------------
step "Preparing .env"
ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$ROOT/.env.example" "$ENV_FILE"
    echo "Created .env from .env.example."
fi
if [ -z "$(get_env_value "$ENV_FILE" AUTOCLIPPER_CHAT_HASH_SALT)" ]; then
    set_env_value "$ENV_FILE" AUTOCLIPPER_CHAT_HASH_SALT "$(random_hex)"
    echo "Generated AUTOCLIPPER_CHAT_HASH_SALT (keeps chat and viewer clips enabled)."
fi
if [ -z "$(get_env_value "$ENV_FILE" TWITCH_CLIENT_ID)" ] ||
    [ -z "$(get_env_value "$ENV_FILE" TWITCH_CLIENT_SECRET)" ]; then
    if [ "$MOCK" -eq 0 ]; then
        warn "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are empty in .env."
        warn "Add the team's shared Twitch app credentials (or your own from https://dev.twitch.tv/console) for real analysis. Starting in MOCK mode for now."
        MOCK=1
    fi
fi
if [ "$MOCK" -eq 1 ]; then JOBS=mock; else JOBS=pipeline; fi

# 3. Dependencies and tools --------------------------------------------------
if [ "$SKIP_INSTALL" -eq 0 ]; then
    step "Installing Python dependencies (uv sync --extra captions)"
    uv sync --extra captions || fail "uv sync failed."

    # Mock mode still needs FFmpeg (the backend renders sample videos); pipeline mode also
    # needs TwitchDownloaderCLI. Both are skipped quickly when already installed.
    if [ "$MOCK" -eq 1 ]; then
        step "Installing pinned FFmpeg if missing (autoclipper setup-tools --tool ffmpeg)"
        tools_ok=1
        uv run autoclipper setup-tools --tool ffmpeg || tools_ok=0
    else
        step "Installing pinned FFmpeg and TwitchDownloaderCLI if missing (autoclipper setup-tools)"
        tools_ok=1
        uv run autoclipper setup-tools || tools_ok=0
    fi
    [ "$tools_ok" -eq 1 ] || warn "setup-tools reported a problem; see the doctor output below."

    step "Installing web dependencies (npm ci)"
    if [ -d "$ROOT/web/node_modules" ]; then
        echo "web/node_modules exists; skipping."
    else
        (cd "$ROOT/web" && npm ci) || fail "npm ci failed."
    fi
elif [ ! -d "$ROOT/.venv" ] || [ ! -d "$ROOT/web/node_modules" ]; then
    fail "--skip-install needs an existing .venv and web/node_modules. Run once without it."
fi

API_ORIGIN="http://127.0.0.1:$API_PORT"
WEB_URL="http://localhost:$WEB_PORT"
WEB_ENV="$ROOT/web/.env.local"
[ -f "$WEB_ENV" ] || : >"$WEB_ENV"
set_env_value "$WEB_ENV" NEXT_PUBLIC_API_ORIGIN "$API_ORIGIN"

# 4. Health check ------------------------------------------------------------
step "Checking setup (autoclipper doctor)"
uv run autoclipper doctor --jobs "$JOBS" || warn "doctor reported problems (see above); continuing."

# 5. Start -------------------------------------------------------------------
step "Starting the app"
LOG_DIR="$ROOT/artifacts/demo-logs"
mkdir -p "$LOG_DIR"
API_LOG="$LOG_DIR/backend.log"
WEB_LOG="$LOG_DIR/web.log"
PIDS=""

cleanup() {
    trap - INT TERM EXIT
    if [ -n "$PIDS" ]; then
        echo
        echo "Stopping AutoClipper..."
        for pid in $PIDS; do
            # Each server runs in its own process group (set -m), so this stops its children too.
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        done
        wait 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set -m  # own process group per background job, so Ctrl+C and cleanup reach the servers' children
if port_open "$API_PORT"; then
    warn "Something is already listening on port $API_PORT; assuming the backend is running."
else
    if [ "$WEB_PORT" != "3000" ] && [ -z "${AUTOCLIPPER_CORS_ORIGINS:-}" ]; then
        export AUTOCLIPPER_CORS_ORIGINS="http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT"
    fi
    uv run autoclipper serve --port "$API_PORT" --jobs "$JOBS" </dev/null >"$API_LOG" 2>&1 &
    PIDS="$PIDS $!"
fi
if port_open "$WEB_PORT"; then
    warn "Something is already listening on port $WEB_PORT; assuming the web app is running."
else
    (cd "$ROOT/web" && exec npx --no-install next dev --port "$WEB_PORT") </dev/null >"$WEB_LOG" 2>&1 &
    PIDS="$PIDS $!"
fi
set +m

echo "Waiting for the backend and web app (first start can take a minute)..."
ready=0
i=0
while [ "$i" -lt 90 ]; do
    if url_ok "$API_ORIGIN/api/v1/health" 2 "$API_PORT" && url_ok "$WEB_URL" 10 "$WEB_PORT"; then
        ready=1
        break
    fi
    for pid in $PIDS; do
        kill -0 "$pid" 2>/dev/null ||
            fail "A server exited during startup. See $API_LOG and $WEB_LOG."
    done
    sleep 2
    i=$((i + 1))
done
[ "$ready" -eq 1 ] || fail "The app did not come up in 3 minutes. See $API_LOG and $WEB_LOG."

open_browser "$WEB_URL"
printf '\n%sAutoClipper is running (%s mode).%s\n' "$C_OK" "$JOBS" "$C_OFF"
echo "  Web app:  $WEB_URL"
echo "  API docs: $API_ORIGIN/docs"
if copy_to_clipboard "$DEMO_VOD" 2>/dev/null; then
    echo "  Demo VOD: $DEMO_VOD  (copied to clipboard; paste it on the start page)"
else
    echo "  Demo VOD: $DEMO_VOD  (paste it on the start page)"
fi
echo "  Logs:     $API_LOG, $WEB_LOG"
if [ -z "$PIDS" ]; then
    echo "Both servers were already running; nothing to stop."
    trap - EXIT
    exit 0
fi
echo "Press Ctrl+C to stop."
wait
