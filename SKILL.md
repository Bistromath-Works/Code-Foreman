---
name: foreman
description: Spin up and manage a collaborative coding team using Claude Relay. Foreman turns multiple Claude Code sessions into a coordinated crew with defined roles (Orchestrator, Dissenter, Workers, Cleaner, Circuit Breaker) that communicate via natural language to build software right the first time. Triggers include "spin up foreman," "launch the crew," "start a coding team," "foreman build," "staff the job site," or any request to coordinate multiple Claude Code agents for a coding task. Also use when the user says "ask the dissenter," "broadcast to the team," "check on the workers," or references Foreman roles by name. Use this skill whenever multi-agent coding collaboration is needed, even if the user doesn't say "foreman" explicitly.
---

# Foreman

A collaborative coding team protocol built on Claude Relay. You are the owner of a construction company. The Orchestrator is your foreman. You hand it a blueprint (goal), it staffs the job site (spins up sessions), and manages the crew through completion.

## Prerequisites

Claude Relay must be installed as a Claude Code plugin. See `references/relay-setup.md` for installation steps if not already configured.

## Roles

Eight roles, each running as a headless background process connected via Relay.

| Role | Model | Count | Purpose |
|------|-------|-------|---------|
| Orchestrator | Opus (configurable) | 1 | Approves plans, delegates, tracks, reports. The foreman. |
| Architect | Sonnet (configurable) | 1 | Reads codebase, writes CURRENT_PLAN.md. |
| Dissenter | Sonnet (configurable) | 1 | Challenges plans (First Principles first) and results. |
| Inspector | Opus (configurable) | 1 | Full code audit (correctness, security, conformance). Blocks commit. |
| Worker | Sonnet (configurable) | 1+ | Builds in isolated git worktrees. Scaled by Orchestrator. |
| Cleaner | Haiku (configurable) | 1 | Tidies after Inspector clears. Final sweep only. |
| Circuit Breaker | Haiku (configurable) | 1 | Reads traffic ledger; detects loops mechanically; judges via confirm/arbiter models. |
| Muse | Haiku (configurable) | 1 | Reframes. Invoked on disagreements. Pre-spawned. |

Models and backends are configured in `foreman.config.json` (see references/architecture.md for details). Role-specific instructions are loaded from `references/roles/` when each crew member starts.

## How It Works

### 1. You Give the Goal
Tell the Orchestrator what to build. You only talk to the Orchestrator.

### 2. Architect Writes the Plan
The Orchestrator hands the goal to the Architect. The Architect reads the codebase (read-only) and writes `CURRENT_PLAN.md`.

### 3. Plan Approval Loop
The Orchestrator sends the plan to the Dissenter. The Dissenter challenges premise first (First Principles), then approach.

If the Orchestrator and Dissenter cannot resolve a disagreement after one round, the Orchestrator invokes the Muse for a lateral perspective before making a final call. The Orchestrator holds final authority. The Circuit Breaker monitors this loop with the same escalation ladder as all other relay traffic.

### 4. Orchestrator Staffs the Job Site
After plan approval, the Orchestrator spawns Workers via the `foreman.sh spawn worker <n>` command. Each Worker gets an isolated git worktree. The Architect, Dissenter, Inspector, Cleaner, Circuit Breaker, and Muse are pre-spawned at startup via `foreman.sh start`.

### 5. Workers Build
Workers execute assigned tasks in their worktrees. They coordinate laterally with each other and can ping the Architect directly for plan clarification. The Orchestrator stays out of implementation decisions.

### 6. Cleaner Runs Continuously
The Cleaner keeps the job site tidy throughout the build. Its final deep sweep runs *after* the Inspector clears.

### 7. Architect Conformance Review
When Workers complete, the Architect checks whether the implementation matches `CURRENT_PLAN.md`. It reads the actual changed files.

### 8. Inspector Audit
The Inspector reads everything: the plan, all changed files, affected existing code. Audit covers correctness, security, and plan conformance. A BLOCK finding halts the commit until fixed. Nothing bypasses the Inspector without an explicit Orchestrator override recorded in `DECISIONS.md`.

### 9. Cleaner Final Sweep
After Inspector clearance, the Cleaner runs its final sweep: lint, dead code, imports, formatting.

### 10. Orchestrator Reports
The Orchestrator reports completion to you and signals readiness for PR. You run `/dev-go` when you are ready to open it. The crew does not open PRs automatically.

## Circuit Breaker Protocol

The Circuit Breaker reads the traffic ledger (`.foreman/traffic.jsonl`) and detects loops mechanically: a sliding 15-minute window per agent pair trips at ≥6 messages with ≥3 in each direction. When a trip is detected, a confirm model (default Haiku) is invoked once to verify it is a real loop (false positives are suppressed). If the loop continues, an arbiter model issues a binding forced resolution. If the Orchestrator is a party to the loop or the arbiter is unconfigured/unreachable, the Circuit Breaker escalates to you (the owner) for a decision instead.

**Escalation ladder:**

- **Confirmed trip** (after confirm model call): Flag message sent to both agents directing them to resolve it.
- **4+ messages after flag**: Arbiter model forces a binding decision by selecting the position with stronger justification.
- **Exception**: If the Orchestrator is one of the looping agents, or if the arbiter is unconfigured/unreachable, the Circuit Breaker escalates to the owner via the Orchestrator. You make the call.

The Circuit Breaker notifies the Orchestrator of every intervention so the Orchestrator maintains a record of forced resolutions.

## The Muse

The Muse is pre-spawned as part of the core crew. The Orchestrator (or any agent) can ask it for a lateral perspective when the crew feels stuck or needs a different angle.

The Muse is most effective when configured to run on a model family different from Claude (e.g., a local Ollama model). Running it on different weights creates genuinely different thinking patterns. That is the point. It is not smarter than the crew. It sees sideways.

**How agents use the Muse:** Any agent can ask `foreman-muse` via `relay_ask` when they want a reframe. The Muse responds with one short observation, question, or metaphor, then goes quiet. It does not initiate conversations, write code, or make decisions.

**Structured trigger:** When the Orchestrator and Dissenter cannot resolve a plan disagreement after one round, the Orchestrator invokes the Muse before making a final call. This is the primary structural use.

**Any-time use:** Any agent can ask the Muse when stuck on a problem. The Muse responds with one short thought, then goes quiet.

## Communication Norms

These norms are loaded into every session via the shared protocol file (`references/protocol.md`).

- **Ask vs. Broadcast**: Use `relay_ask` for directed questions between specific agents. Use `relay_broadcast` only for status requests or announcements that genuinely need all-team visibility.
- **Incoming asks get priority**: When an agent receives an incoming ask, it answers before continuing its current work. Responsiveness keeps the job site moving.
- **Status checks are free**: Any agent can be asked for status by anyone, including you. This is read-only and does not require going through the Orchestrator.
- **Workers talk laterally**: Workers with dependent tasks should coordinate directly with each other via Relay, not route everything through the Orchestrator.
- **The Orchestrator delegates, not implements**: The Orchestrator never writes code or edits files. It plans, assigns, reviews, and approves.

## Bootstrapping and Lifecycle

The `scripts/foreman.sh` CLI manages the crew's lifecycle. All crew members run as headless background processes under a generic runner (`scripts/foreman-runner.py`), with the exception of the Orchestrator, which remains an interactive Claude Code session.

### Lifecycle Commands

| Command | What it does |
|---------|-------------|
| `foreman.sh start` | Spawn the core crew (architect, dissenter, inspector, cleaner, circuit-breaker, muse) as headless processes, then launch the interactive Orchestrator session. |
| `foreman.sh spawn worker <n>` | Create a worker-specific git worktree and launch a Worker runner. Workers are spawned on demand by the Orchestrator. |
| `foreman.sh stop` | Terminate all running crew member processes. Worktrees are left intact. |
| `foreman.sh status` | Check liveness of each crew member (PID check) and print the last log line for each. |
| `foreman.sh logs <role> [-f]` | Print or follow the log for a specific crew member (e.g., `logs architect`, `logs worker-1 -f`). |
| `foreman.sh clean` | Remove all worker worktrees (refuses if any have uncommitted changes) and prune Relay state. |

### Session Naming Convention

Crew members auto-register with Relay using these names:

| Session name | Role |
|---|---|
| `foreman-orchestrator` | Orchestrator (interactive session — the only one you interact with directly) |
| `foreman-architect` | Architect |
| `foreman-dissenter` | Dissenter |
| `foreman-inspector` | Inspector |
| `foreman-worker-1`, `foreman-worker-2`, ... | Workers |
| `foreman-cleaner` | Cleaner |
| `foreman-circuit-breaker` | Circuit Breaker |
| `foreman-muse` | Muse |

## Configuration

Every crew member's model and backend are configured in `foreman.config.json`. This file defines defaults for all roles and allows per-role overrides.

**Supported backends:** `claude-cli` (headless Claude), `claude-interactive` (Orchestrator only), `codex-cli`, `openai-compatible` (Ollama, OpenAI, OpenRouter, LM Studio, vLLM, etc.). See `references/architecture.md` for full config schema and examples.

**Circuit Breaker arbiter:** The circuit-breaker role includes an `arbiter` config block for the model that issues binding forced resolutions; it must be set to a frontier-class model (Opus 4.8-level or better, e.g., GLM 5.2 or Kimi 2.6 cloud via Ollama/OpenRouter). The config ships with `"model": "SET-ME"` on purpose—users must choose their own arbiter.

**Per-project overrides:** Place a `.foreman/config.json` in your project directory to override specific roles without editing the skill-level config.

## Scope Control

All sessions spawn in the same project directory where the Orchestrator was launched. Workers see only the project they are in. The Orchestrator sees only the project it is in. No cross-repo coordination in v1. If you need multi-repo support, launch separate Foreman crews per repo.

## Trigger Phrases

- "Spin up Foreman"
- "Launch the crew"
- "Staff the job site"
- "Foreman, build [goal]"
- "Start a coding team for [task]"
- "Get the crew on [feature]"
- Any request for multi-agent coding collaboration

## What Foreman Is Not

- Not a CI/CD pipeline. It does not deploy.
- Not a testing framework. Workers write tests as part of their tasks, but Foreman does not run test suites independently.
- Not persistent. When processes are stopped via `foreman.sh stop`, the crew is gone. Spin up fresh for each job.
- Not cross-machine. All sessions run on the same host via Relay's Unix socket.
