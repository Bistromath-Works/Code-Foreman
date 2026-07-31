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
#   foreman.sh merge [--abort | --skip <n> | <n> ...]
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
  foreman.sh merge [<n> ...]       Merge worker branches (default: all
                                    discovered workers) into
                                    foreman-integration, sequentially.
  foreman.sh merge --abort         Restore the pre-merge branch;
                                    foreman-integration is kept for
                                    inspection.
  foreman.sh merge --skip <n>      Clear a merge block recorded against
                                    worker <n> without merging it — the
                                    worker must be handled manually.
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
    # Branch name must survive worktree removal — `merge` reads it from here.
    # See architecture.md "Integration: the merge workflow", rule 1.
    printf '%s\n' "$branch" > "$worktrees_dir/worker-$n.branch"
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

# --- merge --------------------------------------------------------------------
#
# Implements references/architecture.md, "Integration: the merge workflow".
# Every check here exists because an adversarial review found a concrete
# failure mode (see the numbered rules in that section) — do not simplify.

# Guard: refuse unless cwd is the main project root, not a worker worktree.
merge_validate_cwd() {
  case "$(pwd)" in
    */.foreman/worktrees/*)
      echo "Error: 'foreman.sh merge' must be run from the main project root, not from inside a worker worktree." >&2
      exit 1
      ;;
  esac

  local toplevel here
  if ! toplevel="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "Error: not inside a git repository." >&2
    exit 1
  fi
  here="$(pwd -P)"
  if [ "$toplevel" != "$here" ]; then
    echo "Error: 'foreman.sh merge' must be run from the main project root ($toplevel), not from $here." >&2
    exit 1
  fi
}

# Guard: mkdir-based lock so concurrent invocations fail fast instead of
# corrupting the index. Removed on exit via trap (covers success, error exit,
# and the hard `exit 1` calls sprinkled through the merge helpers below).
merge_acquire_lock() {
  local lockdir="$1"
  if ! mkdir "$lockdir" 2>/dev/null; then
    local holder=""
    [ -f "$lockdir/holder" ] && holder="$(cat "$lockdir/holder" 2>/dev/null || true)"
    echo "Error: another 'foreman.sh merge' is already in progress (lock: $lockdir)." >&2
    if [ -n "$holder" ]; then
      echo "  held by: $holder" >&2
    fi
    exit 1
  fi
  printf 'pid %s on host %s at %s\n' "$$" "$(hostname 2>/dev/null || echo unknown)" "$(date)" \
    > "$lockdir/holder" 2>/dev/null || true
  # MERGE_LOCKDIR is intentionally script-global (not local): the EXIT trap
  # fires after this function's own locals have gone out of scope.
  MERGE_LOCKDIR="$lockdir"
  trap 'rm -rf "$MERGE_LOCKDIR"' EXIT
}

# Resolves the set of worker numbers to operate on: explicit args if given,
# else every worker discoverable from .branch state files or live worktree
# dirs. Prints space-separated ascending unique numbers.
merge_resolve_worker_set() {
  local foreman_dir="$1" explicit="$2"
  local nums=""

  if [ -n "$(printf '%s' "$explicit" | tr -d '[:space:]')" ]; then
    nums="$explicit"
  else
    local f d base num
    for f in "$foreman_dir"/worktrees/worker-*.branch; do
      [ -e "$f" ] || continue
      base="$(basename "$f" .branch)"
      num="${base#worker-}"
      nums="$nums $num"
    done
    for d in "$foreman_dir"/worktrees/worker-*/; do
      [ -e "$d" ] || continue
      d="${d%/}"
      base="$(basename "$d")"
      num="${base#worker-}"
      nums="$nums $num"
    done
  fi

  printf '%s\n' "$nums" | tr ' ' '\n' | grep -v '^$' | sort -n -u | tr '\n' ' ' | sed 's/ *$//'
}

# Resolves worker <n>'s branch name: the .branch state file first (survives
# worktree removal), falling back to the live worktree's checked-out branch.
merge_resolve_branch_for_worker() {
  local foreman_dir="$1" n="$2"
  local branch_file="$foreman_dir/worktrees/worker-$n.branch"
  local worktree_dir="$foreman_dir/worktrees/worker-$n"
  local branch=""

  if [ -f "$branch_file" ]; then
    branch="$(sed -n '1p' "$branch_file" | tr -d '\r\n')"
  fi
  if [ -z "$branch" ] && [ -d "$worktree_dir" ]; then
    branch="$(git -C "$worktree_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  fi
  if [ -z "$branch" ]; then
    echo "Error: cannot resolve a branch name for worker $n (no $branch_file and no live worktree at $worktree_dir)." >&2
    exit 1
  fi
  printf '%s\n' "$branch"
}

# Merges worker <n>'s branch into the currently-checked-out foreman-integration.
# Sets MERGE_WORKER_RESULT to "merged" or "skipped" on success; exits the
# script directly on any hard failure (rules 1, 4, 5, 6).
merge_one_worker() {
  local foreman_dir="$1" n="$2"
  local worktree_dir="$foreman_dir/worktrees/worker-$n"
  MERGE_WORKER_RESULT=""

  local branch
  branch="$(merge_resolve_branch_for_worker "$foreman_dir" "$n")"

  # Rule 1: hard error (not warn-and-continue) on a name that doesn't match
  # the expected foreman-worker-<n>-* pattern.
  case "$branch" in
    foreman-worker-"$n"-*) ;;
    *)
      echo "Error: worker $n's branch '$branch' does not match the expected pattern 'foreman-worker-$n-*'." >&2
      exit 1
      ;;
  esac

  if ! git rev-parse --verify -q "$branch" >/dev/null 2>&1; then
    echo "Error: worker $n's branch '$branch' does not exist." >&2
    exit 1
  fi

  # Rule 4: committed work only.
  if [ -d "$worktree_dir" ]; then
    local wstatus
    wstatus="$(git -C "$worktree_dir" status --porcelain 2>&1 || true)"
    if [ -n "$wstatus" ]; then
      echo "Error: worker $n has uncommitted changes — it must commit before merging." >&2
      printf '%s\n' "$wstatus" | sed 's/^/    /' >&2
      exit 1
    fi
  fi

  local ahead
  ahead="$(git rev-list --count foreman-integration.."$branch")"
  if [ "$ahead" = "0" ]; then
    echo "WARNING: worker $n: 0 commits since fork — possible failed worker. Skipping (nothing to merge)." >&2
    MERGE_WORKER_RESULT="skipped"
    return 0
  fi

  echo "Merging worker $n ($branch)..."
  local merge_out
  if merge_out="$(git merge --no-ff "$branch" -m "merge: worker $n ($branch)" 2>&1)"; then
    printf '%s\n' "$merge_out"
    echo "Worker $n merged cleanly."
    MERGE_WORKER_RESULT="merged"
    return 0
  fi

  # Rule 5 vs rule 6: unmerged paths means a real content conflict; an empty
  # `git ls-files -u` with a nonzero exit means the merge itself failed for
  # some other reason (hook, tooling) and must not be reported as a conflict.
  local unmerged
  unmerged="$(git ls-files -u)"
  if [ -n "$unmerged" ]; then
    local files blocked_sha
    files="$(git diff --name-only --diff-filter=U | sort -u)"
    blocked_sha="$(git rev-parse HEAD)"
    git merge --abort >/dev/null 2>&1 || true
    {
      echo "$n"
      echo "$blocked_sha"
    } > "$foreman_dir/merge-blocked"

    {
      echo ""
      echo "CONFLICT: worker $n ($branch) could not be merged cleanly."
      echo "Conflicted files:"
      printf '%s\n' "$files" | sed 's/^/    /'
      echo ""
      echo "foreman-integration is blocked at $blocked_sha."
      echo "Recovery: worker $n merges foreman-integration into its own worktree (the one"
      echo "sanctioned exception to \"workers never merge\"), resolves against $blocked_sha,"
      echo "commits, and reports back. Then rerun: foreman.sh merge $n"
    } >&2
    exit 1
  else
    {
      echo ""
      echo "ERROR: merge of worker $n ($branch) failed with no conflicted files — this is a"
      echo "hook or tooling failure, not a content conflict. git output:"
      printf '%s\n' "$merge_out" | sed 's/^/    /'
    } >&2
    if git rev-parse --verify -q MERGE_HEAD >/dev/null 2>&1; then
      git merge --abort >/dev/null 2>&1 || true
    fi
    exit 1
  fi
}

# Normal run: merges the requested (or discovered) worker set into
# foreman-integration, sequentially in ascending order.
merge_do_run() {
  local foreman_dir="$1" explicit_workers="$2"

  # Rule 2: no detached HEAD, unless we're effectively already on
  # foreman-integration (its tip commit is checked out).
  local current_branch=""
  if git symbolic-ref -q HEAD >/dev/null 2>&1; then
    current_branch="$(git symbolic-ref --short HEAD)"
  elif git rev-parse --verify -q refs/heads/foreman-integration >/dev/null 2>&1 \
      && [ "$(git rev-parse HEAD)" = "$(git rev-parse foreman-integration)" ]; then
    current_branch="foreman-integration"
  else
    echo "Error: 'foreman.sh merge' cannot run on a detached HEAD. Check out a branch and retry." >&2
    exit 1
  fi

  # Rule 2: main tree must be clean.
  local dirty
  dirty="$(git status --porcelain)"
  if [ -n "$dirty" ]; then
    echo "Error: the main working tree is not clean. Commit or stash changes before merging." >&2
    printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    exit 1
  fi

  local requested_workers
  requested_workers="$(merge_resolve_worker_set "$foreman_dir" "$explicit_workers")"
  if [ -z "$requested_workers" ]; then
    echo "Error: no workers found (no .foreman/worktrees/worker-*.branch files and no live worktrees)." >&2
    exit 1
  fi

  # Rule 5: the conflict gate. Any run must match the blocked worker exactly.
  local blocked_file="$foreman_dir/merge-blocked"
  if [ -f "$blocked_file" ]; then
    local blocked_n blocked_sha
    blocked_n="$(sed -n '1p' "$blocked_file")"
    blocked_sha="$(sed -n '2p' "$blocked_file")"
    if [ "$requested_workers" != "$blocked_n" ]; then
      echo "Error: merge is blocked on worker $blocked_n (foreman-integration was at $blocked_sha)." >&2
      echo "Resolve worker $blocked_n first (rerun 'foreman.sh merge $blocked_n' once it has merged" >&2
      echo "foreman-integration into its own worktree and resolved), or run 'foreman.sh merge --skip $blocked_n'." >&2
      exit 1
    fi
  fi

  # Rule 3: first run creates foreman-integration; later runs require it.
  if ! git rev-parse --verify -q refs/heads/foreman-integration >/dev/null 2>&1; then
    printf '%s\n' "$current_branch" > "$foreman_dir/pre-merge-branch"
    git checkout -b foreman-integration
    echo "Recorded pre-merge branch: $current_branch"
    echo "Created and checked out foreman-integration."
  elif [ "$current_branch" != "foreman-integration" ]; then
    echo "Error: foreman-integration already exists but is not checked out (currently on '$current_branch')." >&2
    echo "Run 'git checkout foreman-integration' to continue the merge, or 'foreman.sh merge --abort' to abandon it." >&2
    exit 1
  fi

  local pre_merge_sha
  pre_merge_sha="$(git rev-parse HEAD)"

  local merged_list="" skipped_list="" n
  for n in $requested_workers; do
    merge_one_worker "$foreman_dir" "$n"
    case "$MERGE_WORKER_RESULT" in
      merged) merged_list="$merged_list $n" ;;
      skipped) skipped_list="$skipped_list $n" ;;
    esac
  done

  rm -f "$blocked_file"

  echo ""
  if [ -n "$merged_list" ]; then
    echo "Merged workers:$merged_list"
  fi
  if [ -n "$skipped_list" ]; then
    echo "Skipped (0 commits since fork):$skipped_list"
  fi
  echo ""
  echo "Diff stat vs pre-merge ($pre_merge_sha):"
  git diff --stat "$pre_merge_sha"..HEAD
  echo ""
  echo "foreman-integration is ready. Run the TypeScript review, Architect conformance"
  echo "review, and Inspector audit against this tree."
}

# `merge --abort`: rule 8.
merge_do_abort() {
  local foreman_dir="$1"
  local pre_branch_file="$foreman_dir/pre-merge-branch"

  if [ ! -f "$pre_branch_file" ]; then
    echo "Error: no merge in progress (no $pre_branch_file recorded)." >&2
    exit 1
  fi
  local pre_branch
  pre_branch="$(cat "$pre_branch_file")"

  if git rev-parse --verify -q MERGE_HEAD >/dev/null 2>&1; then
    git merge --abort >/dev/null 2>&1 || true
  fi

  git checkout "$pre_branch"

  rm -f "$foreman_dir/merge-blocked" "$pre_branch_file"

  echo "Restored branch: $pre_branch"
  echo "foreman-integration is left in place for inspection."
  echo "Delete it when you are done with it: git branch -D foreman-integration"
}

# `merge --skip <n>`: only valid while worker <n> is the recorded block.
merge_do_skip() {
  local foreman_dir="$1" n="$2"
  local blocked_file="$foreman_dir/merge-blocked"

  if [ ! -f "$blocked_file" ]; then
    echo "Error: no merge is currently blocked; nothing to skip." >&2
    exit 1
  fi
  local blocked_n
  blocked_n="$(sed -n '1p' "$blocked_file")"
  if [ "$blocked_n" != "$n" ]; then
    echo "Error: the current block is on worker $blocked_n, not worker $n." >&2
    exit 1
  fi

  rm -f "$blocked_file"
  echo "Worker $n skipped. Its branch was NOT merged and must be handled manually."
}

cmd_merge() {
  local abort=false skip_n="" explicit_workers=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --abort)
        abort=true
        shift
        ;;
      --skip)
        shift
        skip_n="${1:-}"
        if [ -z "$skip_n" ] || ! [[ "$skip_n" =~ ^[0-9]+$ ]]; then
          echo "Usage: foreman.sh merge --skip <n>" >&2
          exit 1
        fi
        shift
        ;;
      -*)
        echo "Error: unknown 'merge' option '$1'" >&2
        exit 1
        ;;
      *)
        if ! [[ "$1" =~ ^[0-9]+$ ]]; then
          echo "Error: worker number must be numeric, got: '$1'" >&2
          exit 1
        fi
        explicit_workers="$explicit_workers $1"
        shift
        ;;
    esac
  done

  if [ "$abort" = true ] && [ -n "$skip_n" ]; then
    echo "Usage: foreman.sh merge [--abort | --skip <n> | <n> ...]" >&2
    exit 1
  fi
  if { [ "$abort" = true ] || [ -n "$skip_n" ]; } && [ -n "$explicit_workers" ]; then
    echo "Usage: foreman.sh merge [--abort | --skip <n> | <n> ...]" >&2
    exit 1
  fi

  PROJECT="$(pwd)"
  local foreman_dir="$PROJECT/.foreman"
  mkdir -p "$foreman_dir"

  merge_validate_cwd

  local lockdir="$foreman_dir/merge.lock"
  merge_acquire_lock "$lockdir"

  if [ "$abort" = true ]; then
    merge_do_abort "$foreman_dir"
    return 0
  fi

  if [ -n "$skip_n" ]; then
    merge_do_skip "$foreman_dir" "$skip_n"
    return 0
  fi

  merge_do_run "$foreman_dir" "$explicit_workers"
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
    merge)
      cmd_merge "$@"
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
