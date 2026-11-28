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

# Reads NAME=value from an env file (first match), like python-dotenv: surrounding quotes and
# spaces are dropped, and an unquoted value's " # comment" is ignored (so "NAME= # x" is empty).
get_env_value() {
    [ -f "$1" ] || return 0
    grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" | head -n 1 | tr -d '\r' | cut -d= -f2- |
        sed -e 's/^[[:space:]]*//' \
            -e 's/^#.*$//' \
            -e "/^[\"']/!s/[[:space:]][[:space:]]*#.*\$//" \
            -e 's/^"\([^"]*\)".*$/\1/' \
            -e "s/^'\\([^']*\\)'.*\$/\\1/" \
            -e 's/[[:space:]]*$//'
}

# Sets NAME=value in an env file, replacing the first NAME= line or appending one. Rewrites the
# file in place, so its permissions and any symlink are kept.
set_env_value() {
    tmp="$1.tmp.$$"
    awk -v name="$2" -v value="$3" '
        BEGIN { pattern = "^[[:space:]]*" name "[[:space:]]*=" }
        { sub(/\r$/, "") }
        $0 ~ pattern && !done { print name "=" value; done = 1; next }
        { print }
        END { if (!done) print name "=" value }
    ' "$1" >"$tmp"
    cat "$tmp" >"$1"
    rm -f "$tmp"
}

# True when web/node_modules is missing or older than web/package-lock.json (e.g. after a pull).
web_deps_stale() {
    installed="$ROOT/web/node_modules/.package-lock.json"
    [ -f "$installed" ] || return 0
    [ "$ROOT/web/package-lock.json" -nt "$installed" ]
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

# True when something accepts TCP connections on 127.0.0.1:PORT or [::1]:PORT, the two
# addresses `localhost` can mean. On macOS Node may listen on ::1 only. Not every bash build
# opens /dev/tcp to ::1, so curl and then nc are tried for it too.
port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    (exec 3<>"/dev/tcp/::1/$1") 2>/dev/null && return 0
    if have curl; then
        # The port is taken when curl got any reply (0), a non-HTTP or cut-off one (8, 18, 52,
        # 56), or connected and then timed out waiting (a nonzero connect time). A timeout
        # alone isn't enough: Windows retries a refused connect until curl gives up.
        rc=0
        connect_s=$(curl -s -o /dev/null --max-time 2 -w '%{time_connect}' \
            "http://[::1]:$1/" 2>/dev/null) || rc=$?
        case "$rc" in
            0 | 8 | 18 | 52 | 56) return 0 ;;
        esac
        case "$connect_s" in
            *[1-9]*) return 0 ;; # "0.000000" (or empty) means it never connected
        esac
    fi
    if have nc; then
        nc -z -w 2 ::1 "$1" >/dev/null 2>&1 && return 0
    fi
    return 1
}

url_ok() {
    if have curl; then
        curl -fsS -o /dev/null --max-time "$2" "$1" 2>/dev/null
    else
        port_open "$3"
    fi
}

# Prints the body of URL, or nothing (curl missing or no answer).
url_body() {
    have curl || return 0
    curl -fsS --max-time "$2" "$1" 2>/dev/null || true
}

# An open port must belong to AutoClipper; otherwise stop now instead of waiting 3 minutes.
# Without curl the check is skipped and the open port is trusted.
check_port_owner() { # PORT URL MARKER WHAT FLAG
    have curl || return 0
    case "$(url_body "$2" 30)" in
        *"$3"*) return 0 ;;
    esac
    fail "Port $1 is in use by another program (it does not answer as the AutoClipper $4). Stop that program or pick another port with $5 N."
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
    mkdir -p "$ROOT/artifacts/demo-logs"
    SYNC_LOG="$ROOT/artifacts/demo-logs/uv-sync.log"
    # tee keeps uv's output on screen; PIPESTATUS (bash 3.2 has it) is uv's own exit code.
    uv sync --extra captions 2>&1 | tee "$SYNC_LOG"
    sync_rc=${PIPESTATUS[0]}
    if [ "$sync_rc" -ne 0 ]; then
        # The captions extra (faster-whisper/onnxruntime) has no wheel for some platforms, e.g.
        # Intel Macs and macOS 13 or older. Everything else works without it. Any other error
        # (network, lockfile, disk) is shown as is, since it isn't about the platform.
        if grep -qiE "wheel for the current platform|only has wheels for|has no wheels|no matching wheel" "$SYNC_LOG"; then
            warn "Captions unavailable on this platform; installing without them (exports work with captions off)."
        else
            warn "uv sync --extra captions failed (exit $sync_rc):"
            sync_errors=$(grep -iE "^ *(error|caused by|hint)" "$SYNC_LOG" | tail -n 15 || true)
            if [ -n "$sync_errors" ]; then
                printf '%s\n' "$sync_errors" >&2
            else
                tail -n 15 "$SYNC_LOG" >&2
            fi
            warn "Full output: $SYNC_LOG. Trying again without captions (exports work with captions off)."
        fi
        uv sync || fail "uv sync failed."
    fi

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
    if web_deps_stale; then
        (cd "$ROOT/web" && npm ci) || fail "npm ci failed."
    else
        echo "web/node_modules is up to date; skipping."
    fi
elif [ ! -d "$ROOT/.venv" ] || [ ! -d "$ROOT/web/node_modules" ]; then
    fail "--skip-install needs an existing .venv and web/node_modules. Run once without it."
elif web_deps_stale; then
    warn "web/package-lock.json changed since the last npm ci; run once without --skip-install if the web app fails."
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

# Sends signal $1 to each server. Each runs in its own process group (set -m below), so this
# reaches its children (uvicorn under `uv run`, next under npx) too.
signal_servers() {
    for pid in $PIDS; do
        kill "-$1" -- "-$pid" 2>/dev/null || kill "-$1" "$pid" 2>/dev/null || true
    done
}

servers_alive() {
    for pid in $PIDS; do
        if kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null; then return 0; fi
    done
    return 1
}

cleanup() {
    set +e
    trap - EXIT
    # A second Ctrl+C (or a hangup) while stopping kills the servers outright.
    trap 'signal_servers KILL; exit 130' INT TERM HUP
    [ -n "$PIDS" ] || return 0
    signal_servers TERM
    printf '\nStopping AutoClipper (Ctrl+C again to force)...\n' 2>/dev/null
    n=0
    while [ "$n" -lt 10 ] && servers_alive; do
        sleep 1
        n=$((n + 1))
    done
    servers_alive && signal_servers KILL
    wait 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# An open port must belong to AutoClipper; both are checked before anything starts.
API_RUNNING=0
WEB_RUNNING=0
if port_open "$API_PORT"; then
    API_RUNNING=1
    check_port_owner "$API_PORT" "$API_ORIGIN/api/v1/health" '"job_runner"' backend --api-port
    case "$(url_body "$API_ORIGIN/api/v1/health" 5)" in
        *"\"job_runner\":\"$JOBS\""*) ;;
        *) warn "The backend already on port $API_PORT runs a different job mode than $JOBS; stop it to switch." ;;
    esac
    warn "Port $API_PORT already has an AutoClipper backend; using it."
fi
if port_open "$WEB_PORT"; then
    WEB_RUNNING=1
    check_port_owner "$WEB_PORT" "$WEB_URL" AutoClipper "web app" --web-port
    warn "Port $WEB_PORT already has the AutoClipper web app; using it."
fi

set -m  # own process group per background job, so Ctrl+C and cleanup reach the servers' children
if [ "$API_RUNNING" -eq 0 ]; then
    if [ "$WEB_PORT" != "3000" ] && [ -z "${AUTOCLIPPER_CORS_ORIGINS:-}" ]; then
        export AUTOCLIPPER_CORS_ORIGINS="http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT"
    fi
    uv run autoclipper serve --port "$API_PORT" --jobs "$JOBS" </dev/null >"$API_LOG" 2>&1 &
    PIDS="$PIDS $!"
fi
if [ "$WEB_RUNNING" -eq 0 ]; then
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
