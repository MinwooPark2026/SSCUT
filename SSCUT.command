#!/usr/bin/env bash
# Double-click launcher for SSCUT on macOS.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LAST_PROJECT_FILE="$SCRIPT_DIR/.sscut_last_project"
PORT="${SSCUT_PORT:-8765}"
LANDING="${SSCUT_LANDING:-files}"

pause_and_exit() {
    local code="${1:-0}"
    echo
    if [ "$code" -eq 0 ]; then
        echo "[SSCUT] Server stopped."
    else
        echo "[SSCUT] Failed. Check the message above."
    fi
    echo
    read -r -p "Press Return to close this window..."
    exit "$code"
}

is_sscut_project() {
    local project_dir="$1"
    [ -d "$project_dir" ] && [ -f "$project_dir/SSCUT.json" ]
}

choose_folder_macos() {
    local start_dir="$1"
    /usr/bin/osascript - "$start_dir" <<'APPLESCRIPT'
on run argv
  set startPath to item 1 of argv
  try
    set chosenFolder to choose folder with prompt "SSCUT 프로젝트 폴더를 선택하세요" default location (POSIX file startPath)
    return POSIX path of chosenFolder
  on error number -128
    return ""
  end try
end run
APPLESCRIPT
}

ask_last_project_macos() {
    local last_project="$1"
    /usr/bin/osascript - "$last_project" <<'APPLESCRIPT'
on run argv
  set lastPath to item 1 of argv
  try
    set answer to display dialog ("최근 SSCUT 프로젝트를 열까요?" & return & return & lastPath) buttons {"폴더 선택", "최근 프로젝트"} default button "최근 프로젝트"
    return button returned of answer
  on error number -128
    return "취소"
  end try
end run
APPLESCRIPT
}

choose_project() {
    if [ "${1:-}" != "" ]; then
        printf '%s\n' "$1"
        return 0
    fi

    local last_project=""
    if [ -f "$LAST_PROJECT_FILE" ]; then
        last_project="$(cat "$LAST_PROJECT_FILE")"
    fi

    if command -v /usr/bin/osascript >/dev/null 2>&1; then
        if [ "$last_project" != "" ] && is_sscut_project "$last_project"; then
            local answer
            answer="$(ask_last_project_macos "$last_project")"
            if [ "$answer" = "최근 프로젝트" ]; then
                printf '%s\n' "$last_project"
                return 0
            fi
            if [ "$answer" = "취소" ]; then
                printf '\n'
                return 0
            fi
        fi
        choose_folder_macos "${last_project:-$HOME}"
        return 0
    fi

    echo "Enter SSCUT project folder path:"
    read -r project_dir
    printf '%s\n' "$project_dir"
}

PROJECT_DIR="$(choose_project "${1:-}")"
if [ "$PROJECT_DIR" = "" ]; then
    echo "[SSCUT] Canceled."
    pause_and_exit 0
fi

if ! is_sscut_project "$PROJECT_DIR"; then
    echo "[SSCUT] Not an SSCUT project folder:"
    echo "  $PROJECT_DIR"
    echo
    echo "A project folder must contain SSCUT.json."
    pause_and_exit 1
fi

printf '%s\n' "$PROJECT_DIR" > "$LAST_PROJECT_FILE"

echo "[SSCUT] Project: $PROJECT_DIR"
echo "[SSCUT] Starting local server. The browser opens automatically."
echo "[SSCUT] Close this window or press Ctrl+C to stop the server."
echo

if [ "${SSCUT_DRY_RUN:-0}" = "1" ]; then
    echo "[SSCUT] Dry run:"
    echo "  $SCRIPT_DIR/run.sh -m img_gen.editor $PROJECT_DIR --port $PORT --landing $LANDING"
    pause_and_exit 0
fi

"$SCRIPT_DIR/run.sh" -m img_gen.editor "$PROJECT_DIR" --port "$PORT" --landing "$LANDING"
pause_and_exit "$?"
