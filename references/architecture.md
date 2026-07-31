# Foreman Headless Architecture

Foreman v2 runs every crew member as a headless background process. No terminal
windows are spawned. The only interactive session is the Orchestrator, which the
owner talks to directly.

## Components

```
foreman/
├── foreman.config.json          # Role → backend/model mapping (user-editable)
├── scripts/
│   ├── foreman.sh               # Lifecycle CLI: start | spawn | stop | status | logs | clean
│   └── foreman-runner.py        # Generic agent runner (one process per crew member)
└── references/
    ├── protocol.md              # Shared communication norms
    ├── architecture.md          # This file
    └── roles/*.md               # Role instructions (fed to models as system context)
```

Per-project runtime state lives in `<project>/.foreman/`:

```
.foreman/
├── logs/<session-name>.log      # stdout+stderr of each runner
├── pids/<session-name>.pid      # PID files for lifecycle management
├── worktrees/worker-<n>/        # Isolated git worktrees for Workers
└── config.json                  # Optional per-project config override
```

`foreman.sh` automatically appends `.foreman/` to the project's
`.git/info/exclude` so runtime state never shows up in `git status`.

## Model Agnosticism

Every role's model is configured in `foreman.config.json`. No model is
hardcoded anywhere else — not in scripts, not in role files. Users swap models
by editing the config.

### Config schema

```json
{
  "defaults": { "backend": "claude-cli", "model": "sonnet" },
  "roles": {
    "orchestrator":    { "backend": "claude-interactive", "model": "opus" },
    "architect":       { "backend": "claude-cli", "model": "sonnet",
                         "allowed_tools": ["Read", "Glob", "Grep", "Write", "Bash(git diff:*)", "Bash(git log:*)"] },
    "dissenter":       { "backend": "claude-cli", "model": "sonnet",
                         "allowed_tools": ["Read", "Glob", "Grep"] },
    "inspector":       { "backend": "claude-cli", "model": "opus",
                         "allowed_tools": ["Read", "Glob", "Grep", "Bash(git diff:*)", "Bash(git log:*)"] },
    "worker":          { "backend": "claude-cli", "model": "sonnet" },
    "cleaner":         { "backend": "claude-cli", "model": "haiku" },
    "circuit-breaker": { "backend": "claude-cli", "model": "haiku",
                         "allowed_tools": [],
                         "arbiter": { "backend": "openai-compatible",
                                      "base_url": "http://127.0.0.1:11434/v1",
                                      "model": "SET-ME", "api_key_env": "" } },
    "muse":            { "backend": "claude-cli", "model": "haiku",
                         "allowed_tools": [] }
  }
}
```

Role entries are merged over `defaults`. Unknown keys are ignored (so users may
annotate with `"//"` comment keys). A `<project>/.foreman/config.json`, if
present, is merged over the skill-level config per role.

### Backends

| Backend | What it runs | Key options |
|---|---|---|
| `claude-interactive` | Interactive `claude` session (Orchestrator only; launched by `foreman.sh`, never by the runner) | `model` |
| `claude-cli` | Headless `claude -p` per task, with `--resume` for session continuity | `model`, `allowed_tools`, `extra_args` |
| `codex-cli` | `codex exec` per task | `model`, `reasoning_effort`, `bin` (default: `codex` on `$PATH`, override via `FOREMAN_CODEX_BIN`) |
| `openai-compatible` | POST to `{base_url}/chat/completions`; conversation history kept in-process | `base_url`, `model`, `api_key_env`, `temperature`, `max_tokens` |

`openai-compatible` is the universal adapter: it covers Ollama
(`http://127.0.0.1:11434/v1`), OpenAI, OpenRouter, LM Studio, vLLM, and any
other provider exposing the OpenAI chat-completions API. `api_key_env` names an
environment variable holding the key (empty/absent = no auth header, as for
local Ollama). API keys are never stored in the config file.

Example — a Dissenter on a local Ollama model:

```json
"dissenter": {
  "backend": "openai-compatible",
  "base_url": "http://127.0.0.1:11434/v1",
  "model": "qwen3.5:latest",
  "api_key_env": ""
}
```

## The Runner

`foreman-runner.py` is one process per crew member. It owns the relay
connection and the message loop — the model never has to remember to keep
listening, which was the primary failure mode of the terminal-based design
(a one-shot `claude -p` turn ends whenever the model decides it is done).

```
foreman-runner.py --role <role> --project <abs-path>
                  [--name <session-name>]   # default: foreman-<role>; workers: foreman-worker-<n>
                  [--cwd <workdir>]         # working dir for the backend (worker worktrees)
                  [--config <path>]         # config file override
```

Loop:

1. Connect to the relay hub socket (candidates: `$RELAY_HUB_SOCKET`,
   `$CLAUDE_PLUGIN_DATA/hub.sock`,
   `~/.claude/plugins/data/relay-claude-relay/hub.sock`, `~/.claude-relay/hub.sock`).
2. `register` with the session name (line-delimited JSON, protocol version 2).
3. Announce readiness to `foreman-orchestrator`.
4. `inbox_wait` forever. On `inbox_deliver`: dispatch the message to the
   backend, then `reply` with the backend's final text on the delivered `ask_id`.
5. On hub disconnect: retry with backoff; log and exit after repeated failure.

System context for the backend is `protocol.md` + the role's `roles/<role>.md`,
assembled by the runner at startup.

### Outbound asks: the `@ask` directive

Headless backends have no relay MCP tools. Instead, a backend response may
contain directive lines:

```
@ask foreman-architect: What shape is the auth token object in the plan?
```

The runner parses these, performs the relay ask, and feeds the answer back to
the backend as a follow-up turn. This repeats (max 5 hops per incoming message)
until the backend produces a response with no directives — that text becomes
the relay reply. Role files document this syntax for crew members.

The outbound-ask wire format mirrors the inbound message types used by the
former bridge scripts and is isolated in the runner's `RelayClient` class; if
the live hub rejects it, the error is logged and surfaced to the asking backend
rather than crashing the runner.

## Traffic Ledger and the Circuit Breaker

Relay delivers directed messages, so no peer can passively observe all
traffic. Instead, the runners record it: every message that touches a
headless crew member passes through a runner, and since every conversation
involves at least one headless member, coverage is complete.

Each runner appends one JSON line per message to
`<project>/.foreman/traffic.jsonl` (`{ts, from, to, kind: ask|reply,
content}`; content truncated; O_APPEND writes, so no locking). The
duplicate-free logging rule per runner:

- every **inbound delivery** (the sender's runner does not log it), plus the
  runner's own **reply** to it;
- an **outbound ask** only when addressed to `foreman-orchestrator`, and the
  **answer received** only when it came from `foreman-orchestrator` — the
  Orchestrator's interactive session is the one place with no runner to log
  the other end.

Ledger writes are best-effort and never crash a runner. `foreman.sh traffic`
pretty-prints the ledger — a flight recorder for post-mortems.

### Circuit Breaker: mechanical detection, LLM judgment

The `circuit-breaker` role no longer runs the generic message loop. Its
runner tails the traffic ledger and detects loops *mechanically* (sliding
15-minute window per agent pair; a pair trips at ≥6 messages with ≥3 in
each direction). LLMs are invoked only at the moment of judgment:

1. **Trip → confirm (the role's `model`, cheap).** One call: is this a real
   loop, and what are the two positions? False positive → suppress and keep
   watching. Real → send the flag message directing both agents to resolve.
2. **Four more messages after the flag → arbitrate (the `arbiter` config
   block).** The breaker assembles an evidence packet — the pair's
   transcript, `CURRENT_PLAN.md`, `DECISIONS.md`, and any project files
   named in the disputed messages (capped) — and the arbiter issues the
   binding ruling, delivered to both agents and recorded with the
   Orchestrator.
3. **Escalation instead of ruling** when the Orchestrator is a party to the
   loop, or when the arbiter is unconfigured/unreachable: the breaker asks
   the Orchestrator to put the decision to the owner. An unconfigured
   arbiter never silently downgrades to a weaker judge.

The breaker still registers on the relay and answers status asks (with a
mechanical summary — no LLM call).

**Arbiter quality bar:** forced rulings overrule two strong agents and are
binding, so the arbiter must be a frontier-class model — as capable as
Claude Opus 4.8 or better (e.g. GLM 5.2 or Kimi 2.6 cloud via
Ollama/OpenRouter). The shipped config deliberately ships `"model":
"SET-ME"`: users must choose their own arbiter. Until they do, stalemates
escalate to the owner, and `foreman.sh start` prints a notice.

## Lifecycle CLI

```
foreman.sh start              # spawn core crew headless, then exec the interactive Orchestrator
foreman.sh spawn worker <n>   # create .foreman/worktrees/worker-<n> + launch a Worker runner
foreman.sh stop               # SIGTERM all PIDs in .foreman/pids/ (worktrees are left intact)
foreman.sh status             # liveness of each crew member (PID check) + last log line
foreman.sh logs <role> [-f]   # print/follow a crew member's log
foreman.sh traffic [-f]       # pretty-print (or follow) the traffic ledger
foreman.sh clean              # remove worktrees (refuses if a worktree has uncommitted changes) + prune
```

Core crew spawned by `start`: architect, dissenter, inspector, cleaner,
circuit-breaker, muse. Each is launched detached (`setsid`, stdout+stderr to
its log file, PID recorded). Workers are spawned on demand — by the
Orchestrator running `foreman.sh spawn worker <n>`, exactly as it previously
ran the bootstrap script.

The Orchestrator remains an interactive `claude` session launched in the
foreground of the owner's terminal with the relay plugin channel flag and
protocol + role context appended as system prompt, exactly as before. It is the
only crew member with real relay MCP tools.

## Integration: the merge workflow

Workers build on isolated branches in isolated worktrees; `foreman.sh merge`
is what lands their work. Without it, the conformance review and Inspector
audit (which run in the main project directory) would inspect a tree the
work never reached.

### Commands

```
foreman.sh merge [<n> ...]    # merge worker branches (default: all) into foreman-integration
foreman.sh merge --abort      # restore the pre-merge branch; foreman-integration is kept for inspection
```

### Rules (each exists because a specific failure mode was found in review)

1. **Branch names survive worktree removal.** `spawn worker <n>` records the
   worker's branch in `.foreman/worktrees/worker-<n>.branch`; `merge` reads
   branch names from these state files (falling back to the live worktree),
   and sanity-checks them against the `foreman-worker-<n>-*` naming pattern.
2. **cwd validation.** `merge` refuses to run unless the current directory is
   the main project root (`git rev-parse --show-toplevel` equals `pwd`, and
   the path is not inside `.foreman/worktrees/`). It also refuses on a
   detached HEAD and on a dirty main working tree.
3. **First run:** records the current branch in `.foreman/pre-merge-branch`,
   then creates and checks out `foreman-integration` off HEAD. Later runs
   require `foreman-integration` to be the checked-out branch.
4. **Committed work only.** Before merging worker `<n>`, `merge` hard-errors
   if that worker's worktree has uncommitted changes — an unenforced "please
   commit" rule silently drops work. A branch with zero commits since its
   fork point is merged but loudly flagged as a possible failed worker.
5. **Sequential, with a conflict gate.** Branches merge in worker-number
   order. On conflict: `git merge --abort`, record the blocked worker and the
   integration SHA in `.foreman/merge-blocked`, and refuse to merge ANY other
   worker until the blocked one succeeds or is explicitly skipped
   (`foreman.sh merge --skip <n>`). Recovery: the blocked Worker merges
   `foreman-integration` into its own worktree (the one sanctioned exception
   to "workers never merge"), resolves against the recorded SHA, commits,
   and reports; then `merge <n>` is rerun.
6. **Hook failures are not conflicts.** A merge that fails with no unmerged
   files (`git ls-files -u` empty) is reported as a hook/tooling failure
   with the underlying git output, not as a content conflict.
7. **Locking.** `merge` holds `.foreman/merge.lock` (mkdir-based) for its
   duration; concurrent invocations fail fast instead of corrupting the
   index.
8. **`merge --abort`** checks out the recorded pre-merge branch and leaves
   `foreman-integration` in place for inspection (deleting it is printed as
   a manual follow-up). This is the documented way out of an abandoned job.

### Process changes

- Workers MUST commit their work in their worktree before reporting
  completion.
- The Cleaner does not touch the main project directory during the build; it
  works in worker worktrees on request. Its final sweep runs on
  `foreman-integration` after the Inspector clears, and the Cleaner COMMITS
  that sweep itself.
- Orchestrator flow: Workers complete → `foreman.sh merge` (+ conflict loop)
  → TypeScript review (on the integrated tree) → Architect conformance →
  Inspector audit → Cleaner final sweep (committed) → report. The owner's PR
  is opened from `foreman-integration`.

## Known Limitations

- **Outbound ask wire format** is best-effort against Relay protocol v2 and has
  not been verified against a live hub from this repo. It is isolated in
  `RelayClient` for easy correction.
