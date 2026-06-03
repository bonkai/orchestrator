#!/bin/bash
# Orchestrator PostToolUse hook — runs after every Claude tool call returns.
# No-op unless ORCHESTRATOR_RUN_ID is set (manual claude sessions unaffected).
#
# Posts a brief tool-result fingerprint to the orchestrator so the live
# activity timeline can show "Read returned 1.2KB" / "Bash exit 0" etc.
# MUST exit 0 — never block.

set +e

if [ -z "$ORCHESTRATOR_RUN_ID" ]; then
    exit 0
fi

PAYLOAD=$(cat)

if command -v jq >/dev/null 2>&1; then
    TOOL_NAME=$(echo "$PAYLOAD" | jq -r '.tool_name // ""' 2>/dev/null)
    # Tool response can be huge (a 50KB file Read) — never send the full body
    RESPONSE_PREVIEW=$(echo "$PAYLOAD" | jq -r '
        if (.tool_response | type) == "string" then .tool_response
        elif (.tool_response | type) == "object" then (.tool_response | tostring)
        else "" end' 2>/dev/null | head -c 400)
    RESPONSE_BYTES=$(echo "$PAYLOAD" | jq -r '
        if (.tool_response | type) == "string" then (.tool_response | length)
        else 0 end' 2>/dev/null)
    BODY=$(jq -n \
        --arg run_id "$ORCHESTRATOR_RUN_ID" \
        --arg tool_name "$TOOL_NAME" \
        --arg preview "$RESPONSE_PREVIEW" \
        --arg bytes "$RESPONSE_BYTES" \
        '{run_id:$run_id, tool_name:$tool_name, response_preview:$preview, response_bytes:($bytes|tonumber? // 0)}')
else
    BODY="{\"run_id\":\"$ORCHESTRATOR_RUN_ID\",\"tool_name\":\"unknown\"}"
fi

PORT="${ORCHESTRATOR_PORT:-7878}"
curl -sS --max-time 2 -X POST "http://127.0.0.1:${PORT}/api/tool_result" \
    -H 'Content-Type: application/json' \
    -d "$BODY" >/dev/null 2>&1

exit 0
