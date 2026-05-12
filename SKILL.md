---
name: foreman
description: Spin up and manage a collaborative coding team using Claude Relay. Foreman turns multiple Claude Code sessions into a coordinated crew with defined roles (Orchestrator, Dissenter, Workers, Cleaner, Circuit Breaker) that communicate via natural language to build software right the first time. Triggers include "spin up foreman," "launch the crew," "start a coding team," "foreman build," "staff the job site," or any request to coordinate multiple Claude Code agents for a coding task. Also use when the user says "ask the dissenter," "broadcast to the team," "check on the workers," or references Foreman roles by name. Use this skill whenever multi-agent coding collaboration is needed, even if the user doesn't say "foreman" explicitly.
---

# Foreman

A collaborative coding team protocol built on Claude Relay. You are the owner of a construction company. The Orchestrator is your foreman. You hand it a blueprint (goal), it staffs the job site (spins up sessions), and manages the crew through completion.

## Prerequisites

Claude Relay must be installed as a Claude Code plugin. See `references/relay-setup.md` for installation steps if not already configured.

## Roles

Eight roles, each running as a separate session connected via Relay.

| Role | Model | Count | Purpose |
|------|-------|-------|---------|
| Orchestrator | Opus 4.6 | 1 | Approves plans, delegates, tracks, reports. The foreman. |
| Architect | Qwen3.5 (Ollama) | 1 | Reads codebase, writes CURRENT_PLAN.md. Python bridge. |
| Dissenter | Gemini 3.1 Pro | 1 | Challenges plans (First Principles first) and results. Python bridge. |
| Inspector | Opus 4.7 | 1 | Full code audit (correctness, security, conformance). Blocks commit. |
| Worker | Sonnet | 1+ | Builds in isolated git worktrees. Scaled by Orchestrator. |
| Cleaner | Haiku | 1 | Tidies after Inspector clears. Final sweep only. |
| Circuit Breaker | Haiku | 1 | Monitors all relay traffic for loops, including plan approval. |
| Muse | Gemma 4 (Ollama) | 1 | Reframes. Invoked on disagreements. Pre-spawned. Python bridge. |

Load role-specific instructions from `references/roles/` when bootstrapping each session.

## How It Works

### 1. You Give the Goal
Tell the Orchestrator what to build. You only talk to the Orchestrator.

### 2. Architect Writes the Plan
The Orchestrator hands the goal to the Architect. The Architect reads the codebase (read-only) and writes `CURRENT_PLAN.md`.

### 3. Plan Approval Loop
The Orchestrator sends the plan to the Dissenter. The Dissenter challenges premise first (First Principles), then approach.

If the Orchestrator and Dissenter cannot resolve a disagreement after one round, the Orchestrator invokes the Muse for a lateral perspective before making a final call. The Orchestrator holds final authority. The Circuit Breaker monitors this loop with the same escalation ladder as all other relay traffic.

### 4. Orchestrator Staffs the Job Site
After plan approval, the Orchestrator spawns Workers via the bootstrap script. Each Worker gets an isolated git worktree. The Architect, Dissenter, Muse, and Circuit Breaker are pre-spawned at startup.

### 5. Workers Build
Workers execute assigned tasks in their worktrees. They coordinate laterally with each other and can ping the Architect directly for plan clarification. The Orchestrator stays out of implementation decisions.

### 6. Cleaner Runs Continuously
The Cleaner keeps the job site tidy throughout the build. Its final deep sweep runs *after* the Inspector clears.

### 7. Architect Conformance Review
When Workers complete, the Architect checks whether the implementation matches `CURRENT_PLAN.md`. It reads the actual changed files.

### 8. Inspector Audit
The Inspector (Opus 4.7) reads everything: the plan, all changed files, affected existing code. Audit covers correctness, security, and plan conformance. A BLOCK finding halts the commit until fixed. Nothing bypasses the Inspector without an explicit Orchestrator override recorded in `DECISIONS.md`.

### 9. Cleaner Final Sweep
After Inspector clearance, the Cleaner runs its final sweep: lint, dead code, imports, formatting.

### 10. Orchestrator Reports
The Orchestrator reports completion to you and signals readiness for PR. You run `/dev-go` when you are ready to open it. The crew does not open PRs automatically.

## Circuit Breaker Protocol

The Circuit Breaker is a passive monitor on all Relay traffic. It watches for repetitive exchanges between any two agents on the same topic.

**Escalation ladder:**

- **3 round-trips** on the same topic between the same agents: Circuit Breaker sends a flag message summarizing both positions and directing the agents to resolve it.
- **4 round-trips**: Circuit Breaker forces a decision by selecting the position with the strongest justification and instructing both agents to accept it and move on.
- **Exception**: If the Orchestrator is one of the looping agents, the Circuit Breaker escalates to you (the owner) at round-trip 4 instead of forcing a decision. You make the call.

The Circuit Breaker notifies the Orchestrator of every intervention so the Orchestrator maintains a record of forced resolutions.

## The Muse

The Muse is optional. The Orchestrator spawns it when the job feels like it could benefit from lateral thinking, or when the crew has been grinding on a hard problem and needs a different angle.

The Muse runs Gemma 4 via Ollama, not Claude. It thinks differently at the weights level. That is the point. It is not smarter than the crew. It sees sideways.

**How agents use the Muse:** Any agent can ping `foreman-muse` via `relay_ask` when they want a reframe. The Muse responds with one short observation, question, or metaphor, then goes quiet. It does not initiate conversations, write code, or make decisions.

The Muse is pre-spawned at crew startup alongside the Orchestrator, Architect, Dissenter, and Circuit Breaker. It is always available.

**Structured trigger:** When the Orchestrator and Dissenter cannot resolve a plan disagreement after one round, the Orchestrator invokes the Muse before making a final call. This is the primary structural use.

**Any-time use:** Any agent can ping `foreman-muse` via `relay_ask` when stuck. The Muse responds with one short observation, question, or metaphor, then goes quiet.

## Communication Norms

These norms are loaded into every session via the shared protocol file (`references/protocol.md`).

- **Ask vs. Broadcast**: Use `relay_ask` for directed questions between specific agents. Use `relay_broadcast` only for status requests or announcements that genuinely need all-team visibility.
- **Incoming asks get priority**: When an agent receives an incoming ask, it answers before continuing its current work. Responsiveness keeps the job site moving.
- **Status checks are free**: Any agent can be asked for status by anyone, including you. This is read-only and does not require going through the Orchestrator.
- **Workers talk laterally**: Workers with dependent tasks should coordinate directly with each other via Relay, not route everything through the Orchestrator.
- **The Orchestrator delegates, not implements**: The Orchestrator never writes code or edits files. It plans, assigns, reviews, and approves.

## Bootstrapping

The Orchestrator uses the bootstrap script at `scripts/foreman-bootstrap.sh` to spawn sessions. The script accepts a role name and launches a Claude Code session with the correct model flag, the shared protocol, and the role-specific CLAUDE.md.

Run `cat scripts/foreman-bootstrap.sh` to review the bootstrap script before first use.

### Session Naming Convention

Sessions auto-register with Relay using these names:

| Session name | Role |
|---|---|
| `foreman-orchestrator` | Orchestrator |
| `foreman-architect` | Architect |
| `foreman-dissenter` | Dissenter |
| `foreman-inspector` | Inspector |
| `foreman-worker-1`, `foreman-worker-2`, ... | Workers |
| `foreman-cleaner` | Cleaner |
| `foreman-circuit-breaker` | Circuit Breaker |
| `foreman-muse` | Muse |

Use `relay_peers` to verify the crew is connected.

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
- Not persistent. When sessions close, the crew is gone. Spin up fresh for each job.
- Not cross-machine. All sessions run on the same host via Relay's Unix socket.
