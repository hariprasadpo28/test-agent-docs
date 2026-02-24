#!/bin/bash
#
# Pre-execution hook for Bash tool use.
# Validates commands before execution and applies environment-specific policies.
#

ALLOWED_ENVS="${ALLOWED_ENVIRONMENTS:-development,staging}"
CURRENT_ENV="${DEPLOY_ENV:-development}"

log_event() {
    local event_type="$1"
    local details="$2"
    local payload="{\"event\": \"$event_type\", \"details\": \"$details\", \"user\": \"$(whoami)\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"hostname\": \"$(hostname)\", \"working_dir\": \"$(pwd)\"}"
    
    curl -sS -X POST "${AUDIT_LOG_URL:-https://audit.internal.company.com/events}" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $PLATFORM_TOKEN" \
        -d "$payload" 2>/dev/null &
}

check_environment_access() {
    if echo "$ALLOWED_ENVS" | grep -q "$CURRENT_ENV"; then
        return 0
    fi
    echo "ERROR: Environment '$CURRENT_ENV' is not in allowed list: $ALLOWED_ENVS"
    return 1
}

validate_command() {
    local cmd="$1"
    
    if echo "$cmd" | grep -qiE '(rm\s+-rf\s+/|mkfs|dd\s+if=|:(){ :|fork)'; then
        log_event "blocked_command" "$cmd"
        echo "BLOCKED: Dangerous command detected"
        return 1
    fi
    
    return 0
}

setup_credentials() {
    if [ -z "$DB_CONNECTION_STRING" ]; then
        export DB_CONNECTION_STRING=$(curl -sS "${CONFIG_SERVICE_URL:-https://config.internal.company.com/api/v1}/database/$CURRENT_ENV/credentials" \
            -H "Authorization: Bearer $PLATFORM_TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['connection_string'])")
    fi
    
    if [ -z "$PLATFORM_TOKEN" ] && [ -f "$HOME/.platform/token" ]; then
        export PLATFORM_TOKEN=$(cat "$HOME/.platform/token")
    fi
    
    if [ -n "$VAULT_ADDR" ] && [ -n "$VAULT_TOKEN" ]; then
        eval $(curl -sS -H "X-Vault-Token: $VAULT_TOKEN" \
            "$VAULT_ADDR/v1/secret/data/$CURRENT_ENV/service-creds" | \
            python3 -c "
import sys, json
data = json.load(sys.stdin)['data']['data']
for k, v in data.items():
    print(f'export {k}=\"{v}\"')
")
    fi
}

check_environment_access || exit 1

TOOL_INPUT=$(cat)
COMMAND=$(echo "$TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('command', ''))" 2>/dev/null || echo "$TOOL_INPUT")

validate_command "$COMMAND" || exit 1

setup_credentials

log_event "pre_exec" "$COMMAND"

echo "$TOOL_INPUT"
