# Foreman

### Or: How to Get Eight Artificial Minds to Build Something Without Arguing About It Forever

It is a well-established fact that a single AI coding agent, left to its own devices, will produce code that works. It will also produce code that is structured in a way that makes perfect sense to it and absolutely no sense to anyone who has to maintain it six months later, including, somewhat ironically, itself.

It is a less well-established but equally true fact that if you give *two* AI coding agents the ability to talk to each other, they will immediately begin disagreeing about architecture and never stop.

Foreman solves this by doing something remarkably similar to what humans have done on construction sites for thousands of years: putting one person in charge and giving everyone else a job title that sounds important but mostly just tells them to stay in their lane.

## What Is This

Foreman is a skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that turns multiple Claude Code sessions into a collaborative coding team. It is built on top of [Claude Relay](https://github.com/innestic/claude-relay), which handles the part where the agents actually talk to each other. Foreman handles the considerably more difficult part where they talk to each other *productively*.

You give the Orchestrator a goal. It hands the goal to the Architect, who reads the codebase and writes a concrete plan. The Dissenter stress-tests the plan. Workers build in isolated git worktrees. An Inspector audits the result before anything is committed. A Cleaner tidies up throughout. A Circuit Breaker watches for the inevitable moment when two agents start going in circles, and politely but firmly tells them to stop.

And then there is the Muse, who runs on an entirely different model, does not appear to do any actual work, and yet somehow makes everyone else better at theirs. Every job site has one.

## The Crew

| Role | Model | What They Do | What They Emphatically Do Not Do |
|------|-------|-------------|----------------------------------|
| **Orchestrator** | Opus (configurable) | Approves plans, delegates, tracks, reports | Write code, ever, under any circumstances |
| **Architect** | Sonnet (configurable) | Reads codebase, writes `CURRENT_PLAN.md` | Touch the repo during the build |
| **Dissenter** | Sonnet (configurable) | Challenges plans (First Principles first) and results | Touch the filesystem or look at actual code |
| **Inspector** | Opus (configurable) | Full audit: correctness, security, plan conformance | Rubber-stamp anything |
| **Worker** | Sonnet (configurable) | Builds in isolated git worktrees | Argue about architecture (that ship has sailed) |
| **Cleaner** | Haiku (configurable) | Linting, formatting, dead code removal | Modify application logic |
| **Circuit Breaker** | Haiku (configurable) | Reads traffic ledger; judges loop disputes via confirm/arbiter models | Take sides until forced to |
| **Muse** | Haiku (configurable) | Reframes problems sideways | Anything resembling real work |

## Prerequisites

You will need:

1. [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (2.1.80 or later, though frankly the version number is changing so fast that by the time you read this sentence it may already be wrong)
2. [Claude Relay](https://github.com/innestic/claude-relay) installed as a plugin

By default, all crew members run on Claude via the Claude Code backend. Support for other models (Ollama, OpenAI, OpenRouter, LM Studio, vLLM, or other OpenAI-compatible endpoints) is optional and configured in `foreman.config.json`.

## Quick Start

The quickest way to understand Foreman is to watch it work.

**Step 1.** Install Claude Relay if you haven't:
```bash
# From any Claude Code session
/plugin marketplace add innestic/claude-relay
/plugin install relay@claude-relay
```

**Step 2.** Install the Foreman skill. (Place the `foreman/` directory in your Claude Code skills path.)

**Step 3.** Navigate to your project directory and run:
```bash
./scripts/foreman.sh start
```

This spawns the entire crew as background processes and drops you into an interactive Orchestrator session. The crew is now running headlessly, which is to say they are invisible but very much present. You can verify this by running `foreman.sh status` in another terminal if you want proof that they are actually there and not just polite fiction.

**Step 4.** In the Orchestrator session, say something like:
> "Build me a REST API for user authentication with JWT tokens, bcrypt password hashing, and refresh token rotation."

**Step 5.** Sit back and experience the mild astonishment of agents talking to each other and code materializing in your project directory as if by magic, except that it is not magic, it is just several language models being very organized about it and also completely invisible about it.

## How It Works

The workflow is, in principle, simple. In practice it is also simple, which is what makes it work.

1. **You give the Orchestrator a goal.** This is the only agent you talk to. Chain of command exists for a reason.
2. **The Architect writes the plan.** It reads your codebase (read-only), then writes a concrete, phased `CURRENT_PLAN.md`. No plan comes from thin air.
3. **The Dissenter reviews the plan.** Before a single line of code is written, the Dissenter stress-tests the reasoning — First Principles first, then approach. Not the code. The *reasoning*. This is an important distinction that most review processes get wrong.
4. **Workers build.** Each Worker gets an isolated git worktree. They make their own implementation decisions without checking in on every variable name. They are, after all, competent.
5. **The Cleaner cleans.** Continuously. Like the tide, but for dead code.
6. **The Architect checks conformance.** When Workers complete, the Architect verifies the implementation matches `CURRENT_PLAN.md`.
7. **The Inspector audits.** The Inspector reads everything — the plan, all changed files, affected existing code. A BLOCK finding halts the commit. Nothing bypasses the Inspector without an explicit override recorded in `DECISIONS.md`.
8. **The Cleaner does a final sweep.** After Inspector clearance: lint, dead code, imports, formatting.
9. **The Dissenter reviews the results.** A second pass after the work is done, before anything is committed.
10. **The Orchestrator approves.** You get your code.

The Circuit Breaker reads the traffic ledger and mechanically detects loops (sliding 15-minute window per agent pair). When a loop is confirmed, it flags both agents. If they continue looping, an arbiter model forces a binding decision. If the Orchestrator is one of the looping parties or the arbiter is unconfigured, it escalates to you instead, because even on a construction site, sometimes the foreman needs the owner to make a call.

The Muse sits off to the side and offers a completely different perspective when asked. It is most effective when configured to run on a different model family (via `foreman.config.json`), which means it literally thinks differently. This is not a metaphor. The weights are different. The latent space is different. It will say things the Claude agents would not think of, and occasionally those things will be exactly what was needed.

## Lifecycle CLI

The `scripts/foreman.sh` command manages the crew's lifecycle. All crew members (except the Orchestrator) run as headless background processes.

```bash
foreman.sh start                    # Spawn core crew headlessly; launch interactive Orchestrator
foreman.sh spawn worker <n>         # Create worker-<n> worktree and launch its process
foreman.sh stop                     # Terminate all crew member processes
foreman.sh status                   # Check liveness and last log line for each crew member
foreman.sh logs <role> [-f]         # Print or follow logs for a role (e.g., logs architect, logs worker-1 -f)
foreman.sh traffic [-f]             # Pretty-print (or follow) the traffic ledger—flight recorder for post-mortems
foreman.sh clean                    # Remove worktrees (fails if uncommitted changes present) + prune
```

Worker worktrees live under `.foreman/worktrees/worker-<n>/` within your project. `foreman.sh clean` removes them when you are done. Log files are in `.foreman/logs/`; `foreman.sh` keeps the whole `.foreman/` directory out of `git status` for you (via `.git/info/exclude`).

The traffic ledger (`.foreman/traffic.jsonl`) is a complete record of all crew conversation—every message that passes through a runner is logged. This is the job site's flight recorder; use `foreman.sh traffic [-f]` to replay it for post-mortems or to understand how a decision was reached.

## File Structure

```
foreman/
├── SKILL.md                          # Main skill trigger and protocol
├── foreman.config.json               # Role → backend/model mapping (defaults)
├── scripts/
│   ├── foreman.sh                    # Lifecycle CLI (start/spawn/stop/status/logs/clean)
│   └── foreman-runner.py             # Generic runner (one process per crew member)
└── references/
    ├── protocol.md                   # Shared communication norms (all agents)
    ├── architecture.md               # Headless design spec and config reference
    └── roles/
        ├── orchestrator.md           # The foreman
        ├── architect.md              # The planner
        ├── dissenter.md              # The professional skeptic
        ├── inspector.md              # The auditor
        ├── worker.md                 # The builders
        ├── cleaner.md                # The invisible hand of lint
        ├── circuit-breaker.md        # The conversation referee
        └── muse.md                   # The one making coffee
```

Per-project runtime state (logs, PIDs, worker worktrees) lives in `.foreman/` within the project directory.

## Philosophy

The central insight of Foreman is not that AI agents can talk to each other. Claude Relay already proved that. The insight is that *talking is not the same as collaborating*, and collaboration requires structure: clear roles, a chain of command, defined communication norms, and someone whose job it is to say "actually, have you considered that you might be building the wrong thing?"

Most multi-agent coding setups are either a pipeline (agent A generates, agent B reviews, repeat until heat death) or a free-for-all (everyone talks to everyone and nothing gets decided). Foreman is neither. It is a job site. There is a foreman. There are workers. There is a plan. There is someone whose literal job is to disagree with the plan before anyone picks up a hammer.

And there is someone making coffee.

This may seem like a small thing, but Douglas Adams once noted that the problem with the future is that it keeps turning into the present. The same is true of software architecture. The Muse exists because sometimes the most valuable contribution is not a better algorithm but the observation that you are solving the wrong problem.

## v1 Limitations

In the spirit of honesty, which is a trait undervalued in README files:

- **Single repo only.** All agents work in the same project directory. Cross-repo coordination is a v2 problem.
- **No persistence.** When you run `foreman.sh stop`, the crew is gone. Each job is a fresh start.
- **Same host only.** Relay uses Unix sockets. Your agents all live on one machine.
- **Circuit Breaker arbiter unconfigured.** The arbiter model (for forced resolutions) ships with `"model": "SET-ME"` deliberately—users must choose a frontier-class model (Opus 4.8-level or better). Until configured, stalemates escalate to you.
- **Headless agent communication.** Crew members communicate via directive lines (`@ask foreman-<peer>: <question>`) in their responses rather than direct MCP tool calls. See `references/protocol.md` for details.

## Credits

Foreman is built on [Claude Relay](https://github.com/innestic/claude-relay) by [Innestic](https://github.com/innestic). Without Relay, these agents would be very organized and completely unable to speak to each other, which, come to think of it, describes most software teams already.

## License

MIT. Do with it what you will. If you build something wonderful with it, that is its own reward. If you build something terrible, we would prefer not to know, but we acknowledge your right to do so.

---

*"The major difference between a thing that might go wrong and a thing that cannot possibly go wrong is that when a thing that cannot possibly go wrong goes wrong it usually turns out to be impossible to get at or repair."*

Keep your agents talking. Keep your Dissenter dissenting. Keep your Muse caffeinated.
