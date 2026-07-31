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

`.foreman/` should be added to the project's `.gitignore`.

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
                         "allowed_tools": [] },
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

## Lifecycle CLI

```
foreman.sh start              # spawn core crew headless, then exec the interactive Orchestrator
foreman.sh spawn worker <n>   # create .foreman/worktrees/worker-<n> + launch a Worker runner
foreman.sh stop               # SIGTERM all PIDs in .foreman/pids/ (worktrees are left intact)
foreman.sh status             # liveness of each crew member (PID check) + last log line
foreman.sh logs <role> [-f]   # print/follow a crew member's log
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

## Known Limitations

- **Circuit Breaker visibility.** Relay delivers directed messages; a peer only
  sees traffic addressed to it. The Circuit Breaker therefore cannot passively
  observe all conversations. Crew members are instructed to CC it on
  contentious exchanges, and the Orchestrator involves it when loops are
  suspected. True passive monitoring needs a hub-level tap (upstream Relay
  feature).
- **Outbound ask wire format** is best-effort against Relay protocol v2 and has
  not been verified against a live hub from this repo. It is isolated in
  `RelayClient` for easy correction.
