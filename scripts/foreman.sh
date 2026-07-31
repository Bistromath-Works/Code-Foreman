#!/usr/bin/env bash
#
# foreman.sh — Foreman lifecycle CLI.
#
# Replaces foreman-bootstrap.sh's terminal-spawning model: crew members now
# run as detached headless background processes (scripts/foreman-runner.py),
# one process per crew member, talking to the Relay hub directly. Only the
# Orchestrator remains an interactive foreground session.
#
# Usage:
#   foreman.sh start
#   foreman.sh spawn worker <n>
#   foreman.sh stop
#   foreman.sh status
#   foreman.sh logs <role-or-session-name> [-f]
#   foreman.sh clean
#   foreman.sh help
#
# Compatible with macOS bash 3.2: no associative arrays, no ${var@Q}.
# Portable quoting uses printf '%q' (as foreman-bootstrap.sh did).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOREMAN_DIR="$(dirname "$SCRIPT_DIR")"

RUNNER="$SCRIPT_DIR/foreman-runner.py"
PROTOCOL_FILE="$FOREMAN_DIR/references/protocol.md"
ORCHESTRATOR_ROLE_FILE="$FOREMAN_DIR/references/roles/orchestrator.md"
SKILL_CONFIG="$FOREMAN_DIR/foreman.config.json"

# Core crew spawned headless by `start`. Workers are spawned on demand via
# `spawn worker <n>`.
CORE_ROLES=(architect dissenter inspector cleaner circuit-breaker muse)

# Embedded stdlib-only JSON reader: resolves a role's "model" by merging
# "defaults" < role entry, first against the skill-level config, then again
# against the project-level override (if present) layered on top per role.
# No model is ever hardcoded in this script — see architecture.md "Model
# Agnosticism".
PY_RESOLVE_MODEL='
import json, sys

def load(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def resolve(cfg, role):
    if not isinstance(cfg, dict):
        return {}
    defaults = cfg.get("defaults") or {}
    roles = cfg.get("roles") or {}
    role_cfg = roles.get(role) or {}
    merged = dict(defaults)
    merged.update(role_cfg)
    return merged

role = sys.argv[1]
skill_cfg_path = sys.argv[2]
project_cfg_path = sys.argv[3] if len(sys.argv) > 3 else ""

merged = resolve(load(skill_cfg_path), role)
if project_cfg_path:
    merged.update(resolve(load(project_cfg_path), role))

print(merged.get("model", ""))
'

# Pretty-printer for the traffic ledger (.foreman/traffic.jsonl).
PY_PRINT_TRAFFIC='
import json, sys, time

path = sys.argv[1]
try:
    f = open(path, encoding="utf-8")
except OSError:
    sys.exit("No traffic ledger at " + path + " (has the crew exchanged any messages?)")
with f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("ts", 0)))
        content = str(m.get("content", "")).replace("\n", " ")
        if len(content) > 160:
            content = content[:157] + "..."
        print("%s  %-24s -> %-24s [%s] %s" % (
            ts, m.get("from", "?"), m.get("to", "?"), m.get("kind", "?"), content))
'

# Reports "unset" if the circuit-breaker arbiter model is missing or SET-ME,
# "ok" otherwise (project config layered over skill config).
PY_CHECK_ARBITER='
import json, sys

def load(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

skill = load(sys.argv[1])
project = load(sys.argv[2] if len(sys.argv) > 2 else "")
role = dict((skill.get("roles") or {}).get("circuit-breaker") or {})
role.update((project.get("roles") or {}).get("circuit-breaker") or {})
model = (role.get("arbiter") or {}).get("model", "")
print("unset" if not model or model == "SET-ME" else "ok")
'

usage() {
  cat <<'USAGE'
Foreman lifecycle CLI.

Usage:
  foreman.sh start                 Spawn the core crew headless, then launch
                                    the interactive Orchestrator in the
                                    foreground.
  foreman.sh spawn worker <n>      Create a git worktree and launch Worker <n>.
  foreman.sh stop                  SIGTERM (then SIGKILL after 5s) every
                                    tracked crew member. Worktrees are left
                                    intact.
  foreman.sh status                Show liveness and last log line for each
                                    tracked crew member.
  foreman.sh logs <role> [-f]      Print (or follow with -f) a crew member's
                                    log. <role> may be e.g. "architect",
                                    "worker-2", or the full session name
                                    "foreman-worker-2".
  foreman.sh traffic [-f]          Pretty-print the crew traffic ledger
                                    (.foreman/traffic.jsonl) — the job
                                    site's flight recorder. -f follows the
                                    raw ledger.
  foreman.sh clean                 Remove worker worktrees with no
                                    uncommitted changes, prune git worktree
                                    metadata, and drop stale pid files.
  foreman.sh help                  Show this message.

Core crew (spawned by 'start'): architect, dissenter, inspector, cleaner,
circuit-breaker, muse. Workers are spawned on demand with 'spawn worker <n>'.

State lives under <project>/.foreman/ (logs/, pids/, worktrees/).
USAGE
}

# --- helpers ----------------------------------------------------------------

resolve_model() {
  local role="$1"
  local project_cfg=""
  if [ -n "${PROJECT:-}" ] && [ -f "$PROJECT/.foreman/config.json" ]; then
    project_cfg="$PROJECT/.foreman/config.json"
  fi
  python3 -c "$PY_RESOLVE_MODEL" "$role" "$SKILL_CONFIG" "$project_cfg"
}

pidfile_is_live() {
  local pidfile="$1"
  [ -f "$pidfile" ] || return 1
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# Keep .foreman/ runtime state out of the project's git status without
# touching the user's .gitignore: append it to .git/info/exclude once.
ensure_git_exclude() {
  git -C "$PROJECT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  local git_dir exclude
  git_dir="$(git -C "$PROJECT" rev-parse --git-dir)"
  case "$git_dir" in
    /*) ;;
    *) git_dir="$PROJECT/$git_dir" ;;
  esac
  exclude="$git_dir/info/exclude"
  mkdir -p "$(dirname "$exclude")"
  grep -qxF '.foreman/' "$exclude" 2>/dev/null || echo '.foreman/' >> "$exclude"
}

# launch_detached <session-name> <logfile> <pidfile> <command...>
# Idempotent: skips the launch if the pidfile already points at a live process.
launch_detached() {
  local name="$1" logfile="$2" pidfile="$3"
  shift 3

  if pidfile_is_live "$pidfile"; then
    echo "  $name: already running (pid $(cat "$pidfile")), skipping"
    return 0
  fi

  touch "$logfile"

  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" >>"$logfile" 2>&1 </dev/null &
  else
    # macOS has no setsid by default — nohup keeps the process alive after
    # this script exits, which is the property we actually need.
    nohup "$@" >>"$logfile" 2>&1 </dev/null &
  fi
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$pidfile"
  echo "  $name: started (pid $pid)"
}

# --- subcommands --------------------------------------------------------------

cmd_start() {
  PROJECT="$(pwd)"
  local foreman_dir="$PROJECT/.foreman"
  local logs_dir="$foreman_dir/logs"
  local pids_dir="$foreman_dir/pids"
  mkdir -p "$logs_dir" "$pids_dir"
  ensure_git_exclude

  local f
  for f in "$PROTOCOL_FILE" "$ORCHESTRATOR_ROLE_FILE"; do
    [ -f "$f" ] || { echo "Error: missing required file: $f" >&2; exit 1; }
  done

  echo "Starting Foreman crew in $PROJECT ..."
  local role name
  for role in "${CORE_ROLES[@]}"; do
    name="foreman-$role"
    launch_detached "$name" "$logs_dir/$name.log" "$pids_dir/$name.pid" \
      python3 "$RUNNER" --role "$role" --project "$PROJECT"
  done

  local project_cfg=""
  [ -f "$PROJECT/.foreman/config.json" ] && project_cfg="$PROJECT/.foreman/config.json"
  if [ "$(python3 -c "$PY_CHECK_ARBITER" "$SKILL_CONFIG" "$project_cfg")" = "unset" ]; then
    echo ""
    echo "NOTICE: the Circuit Breaker's arbiter model is not configured (model: SET-ME)."
    echo "Binding loop rulings need a frontier-class model of your choosing — Claude Opus"
    echo "4.8-level or better (e.g. GLM 5.2 or Kimi 2.6 cloud via Ollama/OpenRouter)."
    echo "Set roles.circuit-breaker.arbiter in foreman.config.json. Until then, crew"
    echo "stalemates escalate to you instead of being ruled on."
    echo ""
  fi

  local orch_model
  orch_model="$(resolve_model orchestrator)"
  if [ -z "$orch_model" ]; then
    echo "Error: no model configured for role 'orchestrator' in $SKILL_CONFIG" >&2
    exit 1
  fi

  # Assemble the Orchestrator's system-prompt context exactly like
  # foreman-bootstrap.sh's orchestrator branch did: protocol.md + role file +
  # a STARTUP section. The old STARTUP text never spawned workers itself, but
  # the role file's Step 3 references "the bootstrap script" — that no longer
  # exists, so we append a correction pointing at the new lifecycle command.
  local ctx_dir ctx_file
  ctx_dir="$(mktemp -d "/tmp/foreman-orchestrator-XXXXXX")"
  ctx_file="$ctx_dir/orchestrator_ctx.txt"
  {
    cat "$PROTOCOL_FILE"
    echo ""
    echo "---"
    echo ""
    cat "$ORCHESTRATOR_ROLE_FILE"
    echo ""
    echo "---"
    echo ""
    echo "STARTUP: When the user gives you a goal, begin the Foreman workflow immediately:"
    echo "1. relay_rename new_name=\"foreman-orchestrator\""
    echo "2. Proceed with Step 1 of your role (commission the plan via relay_ask to the Architect)."
    echo ""
    echo "Note on staffing: spawn Workers by running \`$SCRIPT_DIR/foreman.sh spawn worker <n>\` via your"
    echo "Bash tool from the project directory. Each call creates an isolated git worktree and launches"
    echo "a headless Worker runner."
  } > "$ctx_file"

  local q_model q_ctx
  q_model="$(printf '%q' "$orch_model")"
  q_ctx="$(printf '%q' "$ctx_file")"
  echo "Launching interactive Orchestrator (model: $orch_model)..."
  echo "  exec claude --model $q_model --dangerously-load-development-channels plugin:relay@claude-relay --append-system-prompt-file $q_ctx"
  exec claude --model "$orch_model" --dangerously-load-development-channels plugin:relay@claude-relay --append-system-prompt-file "$ctx_file"
}

cmd_spawn_worker() {
  local n="${1:-}"
  if [ -z "$n" ]; then
    echo "Usage: foreman.sh spawn worker <n>" >&2
    exit 1
  fi
  if ! [[ "$n" =~ ^[0-9]+$ ]]; then
    echo "Error: worker number must be numeric, got: '$n'" >&2
    exit 1
  fi

  PROJECT="$(pwd)"
  local foreman_dir="$PROJECT/.foreman"
  local logs_dir="$foreman_dir/logs"
  local pids_dir="$foreman_dir/pids"
  local worktrees_dir="$foreman_dir/worktrees"
  mkdir -p "$logs_dir" "$pids_dir"
  ensure_git_exclude

  local name="foreman-worker-$n"
  local worktree_path="$worktrees_dir/worker-$n"

  if git -C "$PROJECT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -e "$worktree_path" ]; then
      echo "Error: worktree path already exists: $worktree_path" >&2
      echo "Remove it manually, or run 'foreman.sh clean' if it belongs to a stopped worker." >&2
      exit 1
    fi
    mkdir -p "$worktrees_dir"
    local branch
    branch="foreman-worker-$n-$(date +%s)-$$"
    git -C "$PROJECT" worktree add -b "$branch" "$worktree_path" HEAD
    echo "Worker worktree created: $worktree_path (branch: $branch)"
  else
    echo "Warning: not a git repo — Worker $n will share the main project directory." >&2
    worktree_path="$PROJECT"
  fi

  launch_detached "$name" "$logs_dir/$name.log" "$pids_dir/$name.pid" \
    python3 "$RUNNER" --role worker --project "$PROJECT" --name "$name" --cwd "$worktree_path"
}

cmd_stop() {
  PROJECT="$(pwd)"
  local pids_dir="$PROJECT/.foreman/pids"
  if [ ! -d "$pids_dir" ]; then
    echo "Foreman crew is not started in this project (no .foreman directory)."
    return 0
  fi

  local pidfile name pid i any=false
  for pidfile in "$pids_dir"/*.pid; do
    [ -e "$pidfile" ] || continue
    any=true
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile" 2>/dev/null || true)"

    if [ -z "$pid" ]; then
      echo "$name: no pid recorded, removing stale pid file"
      rm -f "$pidfile"
      continue
    fi

    if kill -0 "$pid" 2>/dev/null; then
      echo "$name: stopping (pid $pid)..."
      kill -TERM "$pid" 2>/dev/null || true
      i=0
      while [ "$i" -lt 5 ] && kill -0 "$pid" 2>/dev/null; do
        sleep 1
        i=$((i + 1))
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "$name: still running after SIGTERM, sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
      fi
      echo "$name: stopped"
    else
      echo "$name: not running (stale pid $pid)"
    fi

    rm -f "$pidfile"
  done

  if [ "$any" = false ]; then
    echo "No crew members recorded (no pid files under .foreman/pids)."
  else
    echo "Worktrees left intact. Run 'foreman.sh clean' to remove them."
  fi
}

cmd_status() {
  PROJECT="$(pwd)"
  local foreman_dir="$PROJECT/.foreman"
  if [ ! -d "$foreman_dir" ]; then
    echo "Foreman crew is not started in this project (no .foreman directory)."
    return 0
  fi

  local pids_dir="$foreman_dir/pids"
  local logs_dir="$foreman_dir/logs"
  local pidfile name pid logfile last_line found=false

  if [ -d "$pids_dir" ]; then
    for pidfile in "$pids_dir"/*.pid; do
      [ -e "$pidfile" ] || continue
      found=true
      name="$(basename "$pidfile" .pid)"
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      logfile="$logs_dir/$name.log"
      last_line=""
      if [ -f "$logfile" ]; then
        last_line="$(tail -n 1 "$logfile" 2>/dev/null || true)"
      fi

      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        printf '%-28s RUNNING (pid %s)\n' "$name" "$pid"
      else
        printf '%-28s DEAD\n' "$name"
      fi
      if [ -n "$last_line" ]; then
        printf '    last log: %s\n' "$last_line"
      fi
    done
  fi

  if [ "$found" = false ]; then
    echo "No crew members recorded (no pid files under .foreman/pids)."
  fi
}

cmd_logs() {
  local follow=false target=""
  local a
  for a in "$@"; do
    case "$a" in
      -f) follow=true ;;
      *) target="$a" ;;
    esac
  done

  if [ -z "$target" ]; then
    echo "Usage: foreman.sh logs <role-or-session-name> [-f]" >&2
    exit 1
  fi

  PROJECT="$(pwd)"
  local logs_dir="$PROJECT/.foreman/logs"

  local session_name
  case "$target" in
    foreman-*) session_name="$target" ;;
    *) session_name="foreman-$target" ;;
  esac

  local logfile="$logs_dir/$session_name.log"
  if [ ! -f "$logfile" ]; then
    echo "Error: no log file for '$target' (looked for $logfile)" >&2
    exit 1
  fi

  if [ "$follow" = true ]; then
    exec tail -f "$logfile"
  else
    cat "$logfile"
  fi
}

cmd_traffic() {
  local follow=false a
  for a in "$@"; do
    case "$a" in
      -f) follow=true ;;
      *) echo "Usage: foreman.sh traffic [-f]" >&2; exit 1 ;;
    esac
  done

  PROJECT="$(pwd)"
  local ledger="$PROJECT/.foreman/traffic.jsonl"

  if [ "$follow" = true ]; then
    [ -f "$ledger" ] || { echo "No traffic ledger at $ledger" >&2; exit 1; }
    exec tail -f "$ledger"
  fi
  python3 -c "$PY_PRINT_TRAFFIC" "$ledger"
}

cmd_clean() {
  PROJECT="$(pwd)"
  local foreman_dir="$PROJECT/.foreman"
  if [ ! -d "$foreman_dir" ]; then
    echo "Foreman crew is not started in this project (no .foreman directory)."
    return 0
  fi

  local worktrees_dir="$foreman_dir/worktrees"
  local wt status_out any_dirty=false

  if [ -d "$worktrees_dir" ]; then
    for wt in "$worktrees_dir"/*/; do
      [ -e "$wt" ] || continue
      wt="${wt%/}"

      if ! git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Warning: $wt does not look like a git worktree, skipping" >&2
        continue
      fi

      status_out="$(git -C "$wt" status --porcelain 2>&1 || true)"
      if [ -n "$status_out" ]; then
        any_dirty=true
        echo "Refusing to remove $wt: uncommitted changes present:" >&2
        printf '%s\n' "$status_out" | sed 's/^/    /' >&2
        continue
      fi

      echo "Removing worktree: $wt"
      git -C "$PROJECT" worktree remove "$wt"
    done
  fi

  git -C "$PROJECT" worktree prune >/dev/null 2>&1 || true

  local pids_dir="$foreman_dir/pids"
  local pidfile pid
  if [ -d "$pids_dir" ]; then
    for pidfile in "$pids_dir"/*.pid; do
      [ -e "$pidfile" ] || continue
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "Removing stale pid file: $(basename "$pidfile")"
        rm -f "$pidfile"
      fi
    done
  fi

  if [ "$any_dirty" = true ]; then
    echo "Some worktrees were left in place due to uncommitted changes." >&2
    return 1
  fi
  return 0
}

# --- dispatch -----------------------------------------------------------------

main() {
  if [ "$#" -eq 0 ]; then
    usage
    return 0
  fi

  local sub="$1"
  shift

  case "$sub" in
    start)
      cmd_start
      ;;
    spawn)
      if [ "${1:-}" != "worker" ]; then
        echo "Usage: foreman.sh spawn worker <n>" >&2
        exit 1
      fi
      shift
      cmd_spawn_worker "${1:-}"
      ;;
    stop)
      cmd_stop
      ;;
    status)
      cmd_status
      ;;
    logs)
      cmd_logs "$@"
      ;;
    traffic)
      cmd_traffic "$@"
      ;;
    clean)
      cmd_clean
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      echo "Error: unknown command '$sub'" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
