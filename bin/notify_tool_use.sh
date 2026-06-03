#!/bin/bash
# Orchestrator PreToolUse hook — runs before every Claude tool call.
# No-op unless ORCHESTRATOR_RUN_ID is set (manual claude sessions unaffected).
#
# Reads Claude's PreToolUse JSON from stdin and POSTs a fingerprint
# {run_id, tool_name, input_hash} to the orchestrator. Orchestrator's
# loop watchdog uses this to detect when claude is stuck repeating the
# same tool call and kill the session.
#
# MUST exit 0 even on error — never block a tool call.

set +e

if [ -z "$ORCHESTRATOR_RUN_ID" ]; then
    exit 0
fi

PAYLOAD=$(cat)

if command -v jq >/dev/null 2>&1; then
    TOOL_NAME=$(echo "$PAYLOAD" | jq -r '.tool_name // ""' 2>/dev/null)
    # Hash the tool input (any structure) → stable fingerprint. md5 keeps
    # the payload small and is plenty for "is this the same call again?".
    INPUT_HASH=$(echo "$PAYLOAD" | jq -c '.tool_input // {}' 2>/dev/null | md5 -q 2>/dev/null || \
                 echo "$PAYLOAD" | jq -c '.tool_input // {}' 2>/dev/null | md5sum 2>/dev/null | awk '{print $1}')
    BODY=$(jq -n \
        --arg run_id "$ORCHESTRATOR_RUN_ID" \
        --arg tool_name "$TOOL_NAME" \
        --arg input_hash "$INPUT_HASH" \
        '{run_id:$run_id, tool_name:$tool_name, input_hash:$input_hash}')
else
    BODY="{\"run_id\":\"$ORCHESTRATOR_RUN_ID\",\"tool_name\":\"unknown\",\"input_hash\":\"\"}"
fi

PORT="${ORCHESTRATOR_PORT:-7878}"
curl -sS --max-time 2 -X POST "http://127.0.0.1:${PORT}/api/tool_use" \
    -H 'Content-Type: application/json' \
    -d "$BODY" >/dev/null 2>&1

exit 0
