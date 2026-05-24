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

USE_BRIDGE=false
USE_WORKTREE=false
BRIDGE_SCRIPT=""
WORKTREE_PATH=""
WORKTREE_BRANCH=""

case "$ROLE" in
  orchestrator)
    MODEL="claude-opus-4-6"
    SESSION_NAME="foreman-orchestrator"
    ROLE_FILE="$FOREMAN_DIR/references/roles/orchestrator.md"
    ;;
  architect)
    MODEL="qwen3.5:latest"
    SESSION_NAME="foreman-architect"
    ROLE_FILE="$FOREMAN_DIR/references/roles/architect.md"
    USE_BRIDGE=true
    BRIDGE_SCRIPT="$FOREMAN_DIR/scripts/foreman-architect-bridge.py"
    ;;
  dissenter)
    MODEL="gemini-3.1-pro"
    SESSION_NAME="foreman-dissenter"
    ROLE_FILE="$FOREMAN_DIR/references/roles/dissenter.md"
    USE_BRIDGE=true
    BRIDGE_SCRIPT="$FOREMAN_DIR/scripts/foreman-dissenter-bridge.py"
    ;;
  inspector)
    MODEL="claude-opus-4-7"
    SESSION_NAME="foreman-inspector"
    ROLE_FILE="$FOREMAN_DIR/references/roles/inspector.md"
    ;;
  worker)
    MODEL="sonnet"
    [ -z "$WORKER_NUM" ] && { echo "Error: worker requires a number"; exit 1; }
    [[ "$WORKER_NUM" =~ ^[0-9]+$ ]] || { echo "Error: worker number must be numeric, got: '$WORKER_NUM'"; exit 1; }
    SESSION_NAME="foreman-worker-$WORKER_NUM"
    ROLE_FILE="$FOREMAN_DIR/references/roles/worker.md"
    USE_WORKTREE=true
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
    MODEL="gemma4:latest"
    SESSION_NAME="foreman-muse"
    ROLE_FILE="$FOREMAN_DIR/references/roles/muse.md"
    USE_BRIDGE=true
    BRIDGE_SCRIPT="$FOREMAN_DIR/scripts/foreman-muse-bridge.py"
    ;;
  *)
    echo "Error: Unknown role '$ROLE'"
    echo "Valid roles: orchestrator, architect, dissenter, inspector, worker, cleaner, circuit-breaker, muse"
    exit 1
    ;;
esac

PROTOCOL_FILE="$FOREMAN_DIR/references/protocol.md"

for f in "$ROLE_FILE" "$PROTOCOL_FILE"; do
  [ -f "$f" ] || { echo "Error: Missing file: $f"; exit 1; }
done

if [ "$USE_BRIDGE" = "true" ] && [ -n "$BRIDGE_SCRIPT" ]; then
  [ -f "$BRIDGE_SCRIPT" ] || { echo "Error: Missing bridge script: $BRIDGE_SCRIPT"; exit 1; }
fi

# Create an isolated git worktree for this Worker if the project is a git repo.
if [ "$USE_WORKTREE" = "true" ]; then
  if git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
    WORKTREE_BRANCH="foreman-worker-$WORKER_NUM-$(date +%s)-$$"
    WORKTREE_PATH="/tmp/foreman-worker-$WORKER_NUM-$$"
    git worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" HEAD
    echo "Worker worktree created: $WORKTREE_PATH (branch: $WORKTREE_BRANCH)"
  else
    echo "Warning: Not a git repo — Worker $WORKER_NUM will share the main directory."
    WORKTREE_PATH="$(pwd)"
  fi
fi

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
    # Inject protocol + role as system context so Claude knows it's the Orchestrator
    # and has relay tools available from the start.
    {
      cat "$PROTOCOL_FILE"
      echo ""
      echo "---"
      echo ""
      cat "$ROLE_FILE"
      echo ""
      echo "---"
      echo ""
      echo "STARTUP: When the user gives you a goal, begin the Foreman workflow immediately:"
      echo "1. relay_rename new_name=\"foreman-orchestrator\""
      echo "2. Proceed with Step 1 of your role (commission the plan via relay_ask to the Architect)."
    } > "$tmpdir/orchestrator_ctx.txt"
    local q_ctx
    q_ctx="$(printf '%q' "$tmpdir/orchestrator_ctx.txt")"
    cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
exec claude --model $q_model --append-system-prompt-file $q_ctx
SCRIPT

  else
    # Autonomous crew member — runs non-interactively.
    if [ "$USE_BRIDGE" = "true" ]; then
      # Bridge roles (Architect, Dissenter, Muse) run as Python processes talking
      # directly to the relay hub. No Claude Code session needed.
      local q_bridge
      q_bridge="$(printf '%q' "$BRIDGE_SCRIPT")"
      cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_cwd
echo "[${SESSION_NAME}] Starting bridge..."
exec python3 $q_bridge --model $q_model
SCRIPT
    else
      # All other autonomous roles use Claude with full permission bypass.
      # The project .claude/settings.json pre-approves all tools so nothing prompts.
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
        echo "3. relay_listen(timeout_ms=5000) — await your first task assignment from the Orchestrator"
        echo "4. Handle the message per your role, then relay_reply with your result"
        echo "5. Continue working. After each task chunk or tool-call sequence, call relay_listen() to drain pending messages. When you receive a notifications/claude/channel push, call relay_listen() immediately to handle it."
        if [ "$USE_WORKTREE" = "true" ] && [ -n "$WORKTREE_PATH" ]; then
          echo ""
          echo "Your worktree path: $WORKTREE_PATH — cd here before doing any work."
          echo "Your branch: $WORKTREE_BRANCH"
        fi
      } > "$tmpdir/init_msg.txt"

      local q_work_cwd
      if [ "$USE_WORKTREE" = "true" ] && [ -n "$WORKTREE_PATH" ]; then
        q_work_cwd="$(printf '%q' "$WORKTREE_PATH")"
      else
        q_work_cwd="$q_cwd"
      fi
      cat > "$tmpscript" <<SCRIPT
#!/usr/bin/env zsh
cd $q_work_cwd
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
