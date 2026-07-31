#!/usr/bin/env bash
#
# Self-contained bash tests for `foreman.sh merge` (references/architecture.md,
# "Integration: the merge workflow"). No network, no models. Builds throwaway
# git repos under mktemp, wires up worker worktrees directly with `git
# worktree` + `.branch` state files (never calls `spawn worker`, which would
# launch a real runner process), and exercises the CLI end to end.
#
# Run: bash tests/test_merge.sh

set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$TESTS_DIR")"
FOREMAN="$REPO_DIR/scripts/foreman.sh"

FAILURES=0
LAST_CONFLICT_REPO=""

# All throwaway repos live under one parent dir, created directly (not via a
# command substitution — appending to a variable inside `$(...)` runs in a
# subshell and never reaches the parent, so per-repo tracking silently loses
# entries). Removing the single parent at exit covers everything.
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/foreman-merge-test.XXXXXX")"

cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

pass() { echo "PASS: $1"; }
fail() {
  echo "FAIL: $1"
  FAILURES=$((FAILURES + 1))
}

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    pass "$desc"
  else
    fail "$desc (expected [$expected], got [$actual])"
  fi
}

assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) pass "$desc" ;;
    *)
      fail "$desc (did not find: $needle)"
      echo "--- output ---"
      printf '%s\n' "$haystack"
      echo "--------------"
      ;;
  esac
}

assert_success() {
  local desc="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then
    pass "$desc"
  else
    fail "$desc (exit $rc)"
  fi
}

assert_failure() {
  local desc="$1" rc="$2"
  if [ "$rc" -ne 0 ]; then
    pass "$desc"
  else
    fail "$desc (expected nonzero exit, got 0)"
  fi
}

# --- fixtures ---------------------------------------------------------------

new_repo() {
  local dir
  dir="$(mktemp -d "$TEST_ROOT/repo.XXXXXX")"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email "foreman-test@example.com"
  git -C "$dir" config user.name "Foreman Test"
  mkdir -p "$dir/.foreman/worktrees"
  mkdir -p "$dir/.git/info"
  echo ".foreman/" >> "$dir/.git/info/exclude"
  echo "root" > "$dir/README.md"
  git -C "$dir" add README.md
  git -C "$dir" commit -q -m "initial commit"
  printf '%s\n' "$dir"
}

# make_worker <repo> <n> <suffix> — mirrors what `spawn worker <n>` does
# (worktree + branch + .branch state file) without launching a runner.
make_worker() {
  local repo="$1" n="$2" suffix="$3"
  local branch="foreman-worker-$n-$suffix"
  local wtdir="$repo/.foreman/worktrees/worker-$n"
  git -C "$repo" worktree add -q -b "$branch" "$wtdir" HEAD
  printf '%s\n' "$branch" > "$repo/.foreman/worktrees/worker-$n.branch"
  printf '%s\n' "$branch"
}

worker_commit() {
  local wtdir="$1" file="$2" content="$3" msg="$4"
  printf '%s\n' "$content" > "$wtdir/$file"
  git -C "$wtdir" add "$file"
  git -C "$wtdir" commit -q -m "$msg"
}

# run_merge <repo> [merge-args...] — sets MERGE_OUT / MERGE_RC.
run_merge() {
  local repo="$1"
  shift
  MERGE_OUT="$(cd "$repo" && "$FOREMAN" merge "$@" 2>&1)"
  MERGE_RC=$?
}

# --- scenarios ----------------------------------------------------------------

scenario_1_happy_path() {
  echo ""
  echo "== Scenario 1: happy path (two workers, disjoint changes) =="
  local repo
  repo="$(new_repo)"
  make_worker "$repo" 1 "a$$" >/dev/null
  make_worker "$repo" 2 "b$$" >/dev/null
  worker_commit "$repo/.foreman/worktrees/worker-1" "a.txt" "worker one output" "worker 1: add a.txt"
  worker_commit "$repo/.foreman/worktrees/worker-2" "b.txt" "worker two output" "worker 2: add b.txt"

  run_merge "$repo"
  assert_success "merge succeeds with two disjoint workers" "$MERGE_RC"
  assert_contains "output lists both merged workers" "$MERGE_OUT" "Merged workers: 1 2"
  assert_contains "diffstat mentions a.txt" "$MERGE_OUT" "a.txt"
  assert_contains "diffstat mentions b.txt" "$MERGE_OUT" "b.txt"

  local cur
  cur="$(git -C "$repo" symbolic-ref --short HEAD)"
  assert_eq "foreman-integration is checked out" "foreman-integration" "$cur"

  if [ -f "$repo/a.txt" ] && [ -f "$repo/b.txt" ]; then
    pass "both files present on foreman-integration"
  else
    fail "both files present on foreman-integration"
  fi

  local pmb
  pmb="$(cat "$repo/.foreman/pre-merge-branch" 2>/dev/null || echo "")"
  assert_eq "pre-merge-branch recorded as main" "main" "$pmb"

  run_merge "$repo" --abort
  assert_success "merge --abort succeeds" "$MERGE_RC"

  cur="$(git -C "$repo" symbolic-ref --short HEAD)"
  assert_eq "abort restores the original branch" "main" "$cur"

  if git -C "$repo" show-ref --verify -q refs/heads/foreman-integration; then
    pass "foreman-integration still exists after abort"
  else
    fail "foreman-integration still exists after abort"
  fi

  if [ -f "$repo/.foreman/pre-merge-branch" ]; then
    fail "pre-merge-branch removed after abort"
  else
    pass "pre-merge-branch removed after abort"
  fi
}

scenario_2_conflict() {
  echo ""
  echo "== Scenario 2: conflict path (worker 2 blocked) =="
  local repo
  repo="$(new_repo)"
  printf 'original line\n' > "$repo/conflict.txt"
  git -C "$repo" add conflict.txt
  git -C "$repo" commit -q -m "add conflict.txt"

  make_worker "$repo" 1 "c1$$" >/dev/null
  make_worker "$repo" 2 "c2$$" >/dev/null

  worker_commit "$repo/.foreman/worktrees/worker-1" "conflict.txt" "worker one change" "worker 1: edit conflict.txt"
  worker_commit "$repo/.foreman/worktrees/worker-2" "conflict.txt" "worker two change" "worker 2: edit conflict.txt"

  run_merge "$repo"
  assert_failure "merge exits nonzero on conflict" "$MERGE_RC"
  assert_contains "conflict report names worker 2" "$MERGE_OUT" "worker 2"
  assert_contains "conflict report names the conflicted file" "$MERGE_OUT" "conflict.txt"
  assert_contains "conflict report gives recovery instructions" "$MERGE_OUT" "foreman-integration into its own worktree"

  if [ -f "$repo/.foreman/merge-blocked" ]; then
    pass "merge-blocked recorded"
    local bn bsha
    bn="$(sed -n '1p' "$repo/.foreman/merge-blocked")"
    bsha="$(sed -n '2p' "$repo/.foreman/merge-blocked")"
    assert_eq "merge-blocked names worker 2" "2" "$bn"
    if [[ "$bsha" =~ ^[0-9a-f]{7,40}$ ]]; then
      pass "merge-blocked records a SHA"
    else
      fail "merge-blocked records a SHA (got: $bsha)"
    fi
  else
    fail "merge-blocked recorded"
  fi

  local log
  log="$(git -C "$repo" log --oneline foreman-integration)"
  assert_contains "integration has worker 1's merge" "$log" "worker 1"
  case "$log" in
    *"worker 2"*) fail "integration does NOT have worker 2's merge" ;;
    *) pass "integration does NOT have worker 2's merge" ;;
  esac

  if git -C "$repo" rev-parse --verify -q MERGE_HEAD >/dev/null 2>&1; then
    fail "no MERGE_HEAD left behind"
  else
    pass "no MERGE_HEAD left behind"
  fi

  LAST_CONFLICT_REPO="$repo"
}

scenario_3_blocked_gate() {
  echo ""
  echo "== Scenario 3: blocked gate refuses other workers; --skip clears it =="
  local repo="$LAST_CONFLICT_REPO"
  if [ -z "$repo" ] || [ ! -d "$repo" ]; then
    fail "blocked gate scenario requires scenario 2's repo"
    return
  fi

  run_merge "$repo" 3
  assert_failure "merge 3 is refused while blocked on worker 2" "$MERGE_RC"
  assert_contains "refusal names the blocked worker" "$MERGE_OUT" "worker 2"

  run_merge "$repo" --skip 2
  assert_success "merge --skip 2 succeeds" "$MERGE_RC"
  assert_contains "skip output names worker 2" "$MERGE_OUT" "Worker 2"
  assert_contains "skip output says manual handling is required" "$MERGE_OUT" "manually"

  if [ -f "$repo/.foreman/merge-blocked" ]; then
    fail "merge-blocked cleared after --skip"
  else
    pass "merge-blocked cleared after --skip"
  fi
}

scenario_4_dirty_worktree() {
  echo ""
  echo "== Scenario 4: dirty worker worktree refuses before merging anything =="
  local repo
  repo="$(new_repo)"
  make_worker "$repo" 1 "d1$$" >/dev/null
  make_worker "$repo" 2 "d2$$" >/dev/null

  # worker 1: uncommitted change (dirty)
  printf 'uncommitted\n' > "$repo/.foreman/worktrees/worker-1/dirty.txt"

  # worker 2: clean, committed change
  worker_commit "$repo/.foreman/worktrees/worker-2" "clean.txt" "worker two clean" "worker 2: add clean.txt"

  run_merge "$repo"
  assert_failure "merge refuses with a dirty worker worktree" "$MERGE_RC"
  assert_contains "error names worker 1's uncommitted changes" "$MERGE_OUT" "worker 1 has uncommitted changes"

  local log
  log="$(git -C "$repo" log --oneline --all)"
  case "$log" in
    *"merge: worker"*) fail "nothing was merged before the dirty-worktree error" ;;
    *) pass "nothing was merged before the dirty-worktree error" ;;
  esac
}

scenario_5_cwd_guard() {
  echo ""
  echo "== Scenario 5: cwd guard rejects running merge from a worker worktree =="
  local repo
  repo="$(new_repo)"
  make_worker "$repo" 1 "e1$$" >/dev/null
  worker_commit "$repo/.foreman/worktrees/worker-1" "e.txt" "e" "worker 1: add e.txt"

  MERGE_OUT="$(cd "$repo/.foreman/worktrees/worker-1" && "$FOREMAN" merge 2>&1)"
  MERGE_RC=$?
  assert_failure "merge from inside a worker worktree fails" "$MERGE_RC"
  assert_contains "error explains the cwd validation" "$MERGE_OUT" "main project root"
}

scenario_6_zero_commit() {
  echo ""
  echo "== Scenario 6: zero-commit worker warns but overall merge succeeds =="
  local repo
  repo="$(new_repo)"
  make_worker "$repo" 1 "f1$$" >/dev/null
  # no additional commits on worker 1's branch — 0 commits since fork

  run_merge "$repo"
  assert_success "merge succeeds overall despite a zero-commit worker" "$MERGE_RC"
  assert_contains "zero-commit warning is printed" "$MERGE_OUT" "0 commits since fork"
}

# --- run ----------------------------------------------------------------------

echo "== syntax checks =="
if bash -n "$FOREMAN"; then
  pass "bash -n scripts/foreman.sh"
else
  fail "bash -n scripts/foreman.sh"
fi

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -s bash "$FOREMAN"; then
    pass "shellcheck scripts/foreman.sh"
  else
    fail "shellcheck scripts/foreman.sh"
  fi
else
  echo "shellcheck not available, skipping"
fi

scenario_1_happy_path
scenario_2_conflict
scenario_3_blocked_gate
scenario_4_dirty_worktree
scenario_5_cwd_guard
scenario_6_zero_commit

echo ""
if [ "$FAILURES" -gt 0 ]; then
  echo "RESULT: $FAILURES assertion(s) failed"
  exit 1
fi
echo "RESULT: all merge tests passed"
