#!/bin/bash
# Orchestrator Stop hook.
# No-op unless ORCHESTRATOR_RUN_ID is set in the env of the claude process
# that spawned this hook (orchestrator sets it; manual sessions don't).
#
# Reads Claude's Stop-hook JSON from stdin and POSTs to localhost.
# MUST exit 0 even on error — never block the Stop event.

set +e

if [ -z "$ORCHESTRATOR_RUN_ID" ]; then
    exit 0
fi

PAYLOAD=$(cat)

# Best-effort parse with jq. If jq isn't present, fall back to a minimal payload.
if command -v jq >/dev/null 2>&1; then
    SESSION_ID=$(echo "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null)
    TRANSCRIPT=$(echo "$PAYLOAD" | jq -r '.transcript_path // ""' 2>/dev/null)
    CWD=$(echo "$PAYLOAD" | jq -r '.cwd // ""' 2>/dev/null)
    # hook_event_name is "Stop"; stop_hook_active is a bool meaning Claude
    # was re-prompted by a previous Stop hook. We surface that as a reason
    # string so downstream sees "stop_continued" instead of just "Stop".
    EXIT_REASON=$(echo "$PAYLOAD" | jq -r '
        if (.stop_hook_active // false) then "stop_continued"
        else (.hook_event_name // "stop")
        end' 2>/dev/null)
    BODY=$(jq -n \
        --arg run_id "$ORCHESTRATOR_RUN_ID" \
        --arg session_id "$SESSION_ID" \
        --arg transcript_path "$TRANSCRIPT" \
        --arg cwd "$CWD" \
        --arg exit_reason "$EXIT_REASON" \
        '{run_id:$run_id, session_id:$session_id, transcript_path:$transcript_path, cwd:$cwd, exit_reason:$exit_reason}')
else
    BODY="{\"run_id\":\"$ORCHESTRATOR_RUN_ID\",\"exit_reason\":\"stop\"}"
fi

PORT="${ORCHESTRATOR_PORT:-7878}"
curl -sS --max-time 5 -X POST "http://127.0.0.1:${PORT}/api/complete" \
    -H 'Content-Type: application/json' \
    -d "$BODY" >/dev/null 2>&1

exit 0
