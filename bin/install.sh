#!/bin/bash
# Orchestrator one-time setup.
#   - Create ~/.orchestrator/ + subdirs
#   - Install notify_complete.sh into ~/.orchestrator/bin/
#   - Merge Stop hook into ~/.claude/settings.json (preserves existing hooks)
#   - Register this directory with forge (~/Documents/forge/projects.txt + .auto_push_dirs)
#
# Idempotent — safe to re-run.

set -e

ORCH_HOME="$HOME/.orchestrator"
ORCH_BIN="$ORCH_HOME/bin"
NOTIFY_DEST="$ORCH_BIN/notify_complete.sh"
NOTIFY_SRC="$(cd "$(dirname "$0")" && pwd)/notify_complete.sh"
TOOLUSE_DEST="$ORCH_BIN/notify_tool_use.sh"
TOOLUSE_SRC="$(cd "$(dirname "$0")" && pwd)/notify_tool_use.sh"
TOOLRESULT_DEST="$ORCH_BIN/notify_tool_result.sh"
TOOLRESULT_SRC="$(cd "$(dirname "$0")" && pwd)/notify_tool_result.sh"
SETTINGS="$HOME/.claude/settings.json"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Orchestrator install"
echo "  Project: $PROJECT_DIR"
echo "  Data dir: $ORCH_HOME"
echo ""

# 1. Dirs ----------------------------------------------------------------
mkdir -p "$ORCH_HOME" "$ORCH_BIN" "$ORCH_HOME/transcripts" \
         "$ORCH_HOME/tasks" "$ORCH_HOME/pids"
echo "✓ Created $ORCH_HOME and subdirs"

# 2. Hook payload --------------------------------------------------------
cp "$NOTIFY_SRC" "$NOTIFY_DEST"
chmod +x "$NOTIFY_DEST"
echo "✓ Installed notify_complete.sh"

cp "$TOOLUSE_SRC" "$TOOLUSE_DEST"
chmod +x "$TOOLUSE_DEST"
echo "✓ Installed notify_tool_use.sh"

cp "$TOOLRESULT_SRC" "$TOOLRESULT_DEST"
chmod +x "$TOOLRESULT_DEST"
echo "✓ Installed notify_tool_result.sh"

# 3. Merge Stop hook into ~/.claude/settings.json -----------------------
if ! command -v jq >/dev/null 2>&1; then
    echo "WARNING: jq not found — cannot merge Stop hook automatically."
    echo "         Install jq (brew install jq) and re-run, or manually add:"
    echo ""
    echo '         {"hooks":[{"type":"command","command":"'$NOTIFY_DEST'"}]}'
    echo ""
    echo "         to the .hooks.Stop array in $SETTINGS"
else
    mkdir -p "$(dirname "$SETTINGS")"
    if [ ! -f "$SETTINGS" ]; then
        echo '{"hooks":{"Stop":[]}}' > "$SETTINGS"
    fi

    # Stop hook
    if jq -e --arg cmd "$NOTIFY_DEST" \
        '(.hooks.Stop // []) | map(.hooks // []) | flatten | any(.command == $cmd)' \
        "$SETTINGS" >/dev/null 2>&1; then
        echo "✓ Stop hook already installed in $SETTINGS"
    else
        TMP=$(mktemp)
        jq --arg cmd "$NOTIFY_DEST" \
            '.hooks.Stop = ((.hooks.Stop // []) + [{"hooks":[{"type":"command","command":$cmd}]}])' \
            "$SETTINGS" > "$TMP"
        if jq -e '.hooks.Stop' "$TMP" >/dev/null 2>&1; then
            mv "$TMP" "$SETTINGS"
            echo "✓ Merged orchestrator Stop hook into $SETTINGS (existing hooks preserved)"
        else
            rm -f "$TMP"
            echo "ERROR: failed to write settings.json — aborting hook install"
            exit 1
        fi
    fi

    # PreToolUse hook (loop watchdog + live activity feed)
    if jq -e --arg cmd "$TOOLUSE_DEST" \
        '(.hooks.PreToolUse // []) | map(.hooks // []) | flatten | any(.command == $cmd)' \
        "$SETTINGS" >/dev/null 2>&1; then
        echo "✓ PreToolUse hook already installed in $SETTINGS"
    else
        TMP=$(mktemp)
        jq --arg cmd "$TOOLUSE_DEST" \
            '.hooks.PreToolUse = ((.hooks.PreToolUse // []) + [{"hooks":[{"type":"command","command":$cmd}]}])' \
            "$SETTINGS" > "$TMP"
        if jq -e '.hooks.PreToolUse' "$TMP" >/dev/null 2>&1; then
            mv "$TMP" "$SETTINGS"
            echo "✓ Merged orchestrator PreToolUse hook into $SETTINGS (existing hooks preserved)"
        else
            rm -f "$TMP"
            echo "ERROR: failed to write settings.json — aborting hook install"
            exit 1
        fi
    fi

    # PostToolUse hook (live activity feed)
    if jq -e --arg cmd "$TOOLRESULT_DEST" \
        '(.hooks.PostToolUse // []) | map(.hooks // []) | flatten | any(.command == $cmd)' \
        "$SETTINGS" >/dev/null 2>&1; then
        echo "✓ PostToolUse hook already installed in $SETTINGS"
    else
        TMP=$(mktemp)
        jq --arg cmd "$TOOLRESULT_DEST" \
            '.hooks.PostToolUse = ((.hooks.PostToolUse // []) + [{"hooks":[{"type":"command","command":$cmd}]}])' \
            "$SETTINGS" > "$TMP"
        if jq -e '.hooks.PostToolUse' "$TMP" >/dev/null 2>&1; then
            mv "$TMP" "$SETTINGS"
            echo "✓ Merged orchestrator PostToolUse hook into $SETTINGS (existing hooks preserved)"
        else
            rm -f "$TMP"
            echo "ERROR: failed to write settings.json — aborting hook install"
            exit 1
        fi
    fi
fi

# 4. Register with forge -------------------------------------------------
AUTO_PUSH="$HOME/Documents/.auto_push_dirs"
if [ -f "$AUTO_PUSH" ]; then
    if ! grep -qxF "$PROJECT_DIR" "$AUTO_PUSH"; then
        echo "$PROJECT_DIR" >> "$AUTO_PUSH"
        echo "✓ Registered with auto-push"
    else
        echo "✓ Already registered with auto-push"
    fi
fi

FORGE_REG="$HOME/Documents/forge/projects.txt"
if [ -f "$FORGE_REG" ]; then
    if ! grep -qxF "$PROJECT_DIR" "$FORGE_REG"; then
        echo "$PROJECT_DIR" >> "$FORGE_REG"
        echo "✓ Registered with forge projects.txt"
    else
        echo "✓ Already in forge projects.txt"
    fi
fi

# 5. venv + deps ----------------------------------------------------------
VENV="$PROJECT_DIR/.venv"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    echo "✓ Created venv at .venv"
fi
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
echo "✓ Installed Python deps"

# 6. iTerm2 sanity --------------------------------------------------------
if [ ! -d /Applications/iTerm.app ] && [ ! -d /Applications/iTerm2.app ]; then
    echo ""
    echo "WARNING: iTerm2 not installed. Dispatches will fail until you run:"
    echo "         brew install --cask iterm2"
fi

echo ""
echo "Install complete. Next:"
echo "  source .venv/bin/activate"
echo "  python -m orchestrator"
echo "  → http://127.0.0.1:7878"
