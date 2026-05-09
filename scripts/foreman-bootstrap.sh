#!/usr/bin/env bash
#
# foreman-bootstrap.sh — spawn a Foreman crew member session.
#
# Usage:
#   ./foreman-bootstrap.sh <role> [worker-number]

set -euo pipefail

ROLE="${1:?Usage: foreman-bootstrap.sh <role> [worker-number]}"
WORKER_NUM="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOREMAN_DIR="$(dirname "$SCRIPT_DIR")"

case "$ROLE" in
  orchestrator)
    MODEL="opus"
    SESSION_NAME="foreman-orchestrator"
    ROLE_FILE="$FOREMAN_DIR/references/roles/orchestrator.md"
    ;;
  dissenter)
    MODEL="gemini-3.1-pro"
    SESSION_NAME="foreman-dissenter"
    ROLE_FILE="$FOREMAN_DIR/references/roles/dissenter.md"
    ;;
  worker)
    MODEL="sonnet"
    [ -z "$WORKER_NUM" ] && { echo "Error: worker requires a number"; exit 1; }
    SESSION_NAME="foreman-worker-$WORKER_NUM"
    ROLE_FILE="$FOREMAN_DIR/references/roles/worker.md"
    ;;
  cleaner)
    MODEL="haiku"
    SESSION_NAME="foreman-cleaner"
    ROLE_FILE="$FOREMAN_DIR/references/roles/cleaner.md"
    ;;
  circuit-breaker)
    MODEL="haiku"
    SESSION_NAME="foreman-circuit-breaker"
    ROLE_FILE="$FOREMAN_DIR/references/roles/circuit-breaker.md"
    ;;
  muse)
    MODEL="gemma4"
    SESSION_NAME="foreman-muse"
    ROLE_FILE="$FOREMAN_DIR/references/roles/muse.md"
    USE_OLLAMA=true
    ;;
  *)
    echo "Error: Unknown role '$ROLE'"
    echo "Valid roles: orchestrator, dissenter, worker, cleaner, circuit-breaker, muse"
    exit 1
    ;;
esac

PROTOCOL_FILE="$FOREMAN_DIR/references/protocol.md"
USE_OLLAMA="${USE_OLLAMA:-false}"

for f in "$ROLE_FILE" "$PROTOCOL_FILE"; do
  [ -f "$f" ] || { echo "Error: Missing file: $f"; exit 1; }
done

spawn_session() {
  local tmpdir tmpscript script_cwd
  tmpdir="$(mktemp -d /tmp/foreman-XXXXXX)"
  tmpscript="$tmpdir/launch.sh"
  script_cwd="$(pwd)"

  # Use printf '%q' for portable quoting (works on bash 3.2+, unlike ${var@Q})
  local q_cwd q_tmpdir q_model
  q_cwd="$(printf '%q' "$script_cwd")"
  q_tmpdir="$(printf '%q' "$tmpdir")"
  q_model="$(printf '%q' "$MODEL")"

  if [ "$ROLE" = "orchestrator" ]; then
    # Interactive — the human talks to this session directly.
    # No -p, no permission bypass: the user is in the loop here.
    cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
exec claude --model $q_model
SCRIPT

  else
    # Autonomous crew member — runs non-interactively.
    # Build the full init message (protocol + role + startup instructions).
    {
      cat "$PROTOCOL_FILE"
      echo ""
      echo "---"
      echo ""
      cat "$ROLE_FILE"
      echo ""
      echo "---"
      echo ""
      echo "STARTUP SEQUENCE — execute immediately, in order:"
      echo "1. relay_rename new_name=\"$SESSION_NAME\""
      echo "2. relay_ask to=\"foreman-orchestrator\" question=\"$SESSION_NAME is online and ready\""
      echo "3. relay_listen(timeout_ms=300000) — block until a message arrives"
      echo "4. Handle the message per your role, then relay_reply with your result"
      echo "5. Return to step 3. Continue this loop until you receive a shutdown signal."
    } > "$tmpdir/init_msg.txt"

    if [ "$ROLE" = "muse" ]; then
      # Muse runs as a Python bridge talking directly to the relay hub + Ollama API.
      # No Hermes, no MCP — just JSON over Unix socket and one HTTP call per question.
      local q_bridge
      q_bridge="$(printf '%q' "$FOREMAN_DIR/scripts/foreman-muse-bridge.py")"
      cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
echo "[muse] Starting Gemma4 bridge..."
exec python3 $q_bridge --model $q_model
SCRIPT
    elif [ "$ROLE" = "dissenter" ]; then
      # Dissenter runs as a Python bridge talking directly to the relay hub + Google Gemini API.
      # Requires: pip install google-generativeai and GOOGLE_API_KEY or GEMINI_API_KEY set.
      local q_bridge
      q_bridge="$(printf '%q' "$FOREMAN_DIR/scripts/foreman-dissenter-bridge.py")"
      cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
echo "[dissenter] Starting Gemini bridge..."
exec python3 $q_bridge --model $q_model
SCRIPT
    else
      # All other autonomous roles use Claude with full permission bypass.
      # The project .claude/settings.json pre-approves all tools so nothing prompts.
      cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
_MSG=\$(cat $q_tmpdir/init_msg.txt)
claude --model $q_model --dangerously-skip-permissions -p "\$_MSG"
exec zsh
SCRIPT
    fi
  fi

  chmod +x "$tmpscript"

  if [[ "$OSTYPE" == "darwin"* ]]; then
    osascript -e "tell application \"Terminal\" to do script \"$tmpscript\""
    osascript -e "tell application \"Terminal\" to activate"
  elif command -v gnome-terminal &> /dev/null; then
    gnome-terminal -- zsh "$tmpscript"
  elif command -v tmux &> /dev/null; then
    tmux new-window -n "$SESSION_NAME" "zsh $tmpscript"
  else
    echo "Error: No supported terminal emulator found"
    exit 1
  fi
}

echo "Spawning $SESSION_NAME ($MODEL model)..."
spawn_session
echo "$SESSION_NAME launched."
