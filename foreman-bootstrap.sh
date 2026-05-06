#!/usr/bin/env bash
#
# foreman-bootstrap.sh
#
# Spawns a Claude Code session with the correct model, role instructions,
# and Relay channel capability for a Foreman crew member.
#
# Usage:
#   ./foreman-bootstrap.sh <role> [worker-number]
#
# Examples:
#   ./foreman-bootstrap.sh orchestrator
#   ./foreman-bootstrap.sh dissenter
#   ./foreman-bootstrap.sh worker 1
#   ./foreman-bootstrap.sh worker 2
#   ./foreman-bootstrap.sh cleaner
#   ./foreman-bootstrap.sh circuit-breaker
#
# The script opens a new terminal window/tab for each session.
# Requires: claude CLI, Claude Relay plugin installed.

set -euo pipefail

ROLE="${1:?Usage: foreman-bootstrap.sh <role> [worker-number]}"
WORKER_NUM="${2:-}"

# Resolve the Foreman skill directory (assumes this script is in foreman/scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOREMAN_DIR="$(dirname "$SCRIPT_DIR")"

# Determine model and session name based on role
case "$ROLE" in
  orchestrator)
    MODEL="opus"
    SESSION_NAME="foreman-orchestrator"
    ROLE_FILE="$FOREMAN_DIR/references/roles/orchestrator.md"
    ;;
  dissenter)
    MODEL="opus"
    SESSION_NAME="foreman-dissenter"
    ROLE_FILE="$FOREMAN_DIR/references/roles/dissenter.md"
    ;;
  worker)
    MODEL="sonnet"
    if [ -z "$WORKER_NUM" ]; then
      echo "Error: worker role requires a worker number (e.g., worker 1)"
      exit 1
    fi
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

# Verify files exist
for f in "$ROLE_FILE" "$PROTOCOL_FILE"; do
  if [ ! -f "$f" ]; then
    echo "Error: Missing file: $f"
    exit 1
  fi
done

# Build the system prompt by combining protocol + role instructions
SYSTEM_PROMPT="$(cat "$PROTOCOL_FILE")

---

$(cat "$ROLE_FILE")"

# Build the initial message that sets up the session
INIT_MESSAGE="You are $SESSION_NAME, a member of a Foreman coding crew. Your role is: $ROLE. Rename yourself to $SESSION_NAME using relay_rename and then report ready to the orchestrator. Read your role instructions carefully and follow the Foreman communication protocol."

# Build the launch command based on whether this is an Ollama or Claude session
if [ "$USE_OLLAMA" = true ]; then
  LAUNCH_CMD="ollama launch claude --model $MODEL --dangerously-load-development-channels plugin:relay@claude-relay --system-prompt \"$SYSTEM_PROMPT\" -p \"$INIT_MESSAGE\""
else
  LAUNCH_CMD="claude --model $MODEL --dangerously-load-development-channels plugin:relay@claude-relay --system-prompt \"$SYSTEM_PROMPT\" -p \"$INIT_MESSAGE\""
fi

# Detect OS for terminal spawning
spawn_session() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS: open a new Terminal tab
    osascript -e "
      tell application \"Terminal\"
        activate
        do script \"cd $(pwd) && $LAUNCH_CMD\"
      end tell
    "
  elif command -v gnome-terminal &> /dev/null; then
    # Linux with GNOME
    gnome-terminal -- bash -c "cd $(pwd) && $LAUNCH_CMD; exec bash"
  elif command -v tmux &> /dev/null; then
    # Fallback: tmux
    tmux new-window -n "$SESSION_NAME" "cd $(pwd) && $LAUNCH_CMD"
  else
    echo "Error: No supported terminal emulator found (tried Terminal.app, gnome-terminal, tmux)"
    exit 1
  fi
}

echo "Spawning $SESSION_NAME ($MODEL model)..."
spawn_session
echo "$SESSION_NAME launched."
