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

# 1b. Fusion config registry (optional multi-model brain; default-off) ---
# Writes the config.json template ONLY IF ABSENT — re-running never clobbers
# keys you've pasted. The block is self-contained (depends only on $ORCH_HOME)
# so tests/test_fusion_config.py can extract and run it in isolation.
# >>> FUSION_CONFIG_BLOCK
CONFIG_DEST="$ORCH_HOME/config.json"
if [ ! -f "$CONFIG_DEST" ]; then
    cat > "$CONFIG_DEST" <<'JSON'
{
  "fusion": {
    "preset": "budget",
    "timeout_s": 300,
    "providers": {
      "deepseek": { "script": "providers/deepseek.py", "key_env": "DEEPSEEK_API_KEY", "api_key": "", "model": "deepseek-chat",    "price_in": 0.44, "price_out": 0.87 },
      "xai":      { "script": "providers/xai.py",       "key_env": "XAI_API_KEY",       "api_key": "", "model": "grok-4",           "price_in": 1.25, "price_out": 2.50 },
      "gemini":   { "script": "providers/gemini.py",    "key_env": "GEMINI_API_KEY",    "api_key": "", "model": "gemini-2.5-flash", "price_in": 0.30, "price_out": 1.50 },
      "minimax":  { "script": "providers/minimax.py",   "key_env": "MINIMAX_API_KEY",   "api_key": "", "model": "MiniMax-Text-01",  "price_in": 0.30, "price_out": 1.20 },
      "glm":      { "script": "providers/glm.py",       "key_env": "ZAI_API_KEY",       "api_key": "", "model": "glm-4.6",          "price_in": 1.40, "price_out": 4.40 },
      "qwen":     { "script": "providers/qwen.py",      "key_env": "DASHSCOPE_API_KEY", "api_key": "", "model": "qwen-max",         "price_in": 1.25, "price_out": 3.75 },
      "kimi":     { "script": "providers/kimi.py",      "key_env": "MOONSHOT_API_KEY",  "api_key": "", "model": "kimi-k3",          "price_in": 3.00, "price_out": 15.00 }
    },
    "presets": {
      "budget":   ["deepseek", "minimax", "gemini"],
      "balanced": ["deepseek", "xai", "qwen"],
      "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"]
    }
  }
}
JSON
    chmod 600 "$CONFIG_DEST"
    echo "✓ Wrote Fusion config template → $CONFIG_DEST (chmod 600)"
else
    echo "✓ config.json already exists — leaving it (and any pasted keys) untouched"
fi
echo "  Fusion is OPT-IN and default-off; enable it by adding >= 2 provider keys."
echo "  Paste each key into $CONFIG_DEST (the \"api_key\" field), or export its"
echo "  env var (which takes precedence). Where to get each key:"
echo "    deepseek → DEEPSEEK_API_KEY    https://platform.deepseek.com/api_keys"
echo "    xai      → XAI_API_KEY         https://console.x.ai"
echo "    gemini   → GEMINI_API_KEY      https://aistudio.google.com/apikey"
echo "    minimax  → MINIMAX_API_KEY     https://www.minimax.io/platform"
echo "    glm      → ZAI_API_KEY         https://z.ai"
echo "    qwen     → DASHSCOPE_API_KEY   https://modelstudio.console.alibabacloud.com"
echo "    kimi     → MOONSHOT_API_KEY    https://platform.moonshot.ai/console/api-keys"
# <<< FUSION_CONFIG_BLOCK

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
