---
name: foreman
description: Spin up and manage a collaborative coding team using Claude Relay. Foreman turns multiple Claude Code sessions into a coordinated crew with defined roles (Orchestrator, Dissenter, Workers, Cleaner, Circuit Breaker) that communicate via natural language to build software right the first time. Triggers include "spin up foreman," "launch the crew," "start a coding team," "foreman build," "staff the job site," or any request to coordinate multiple Claude Code agents for a coding task. Also use when the user says "ask the dissenter," "broadcast to the team," "check on the workers," or references Foreman roles by name. Use this skill whenever multi-agent coding collaboration is needed, even if the user doesn't say "foreman" explicitly.
---

# Foreman

A collaborative coding team protocol built on Claude Relay. You are the owner of a construction company. The Orchestrator is your foreman. You hand it a blueprint (goal), it staffs the job site (spins up sessions), and manages the crew through completion.

## Prerequisites

Claude Relay must be installed as a Claude Code plugin. See `references/relay-setup.md` for installation steps if not already configured.

## Roles

Five distinct roles, each running as a separate Claude Code session connected via Relay.

| Role | Model | Count | Purpose |
|------|-------|-------|---------|
| Orchestrator | Opus | 1 | Plans, delegates, tracks, approves. The foreman. |
| Dissenter | Gemini 3.1 | 1 | Challenges plans before execution and results before commit. |
| Worker | Sonnet | 1+ | Builds. Scaled by the Orchestrator based on job scope. |
| Cleaner | Haiku | 1 | Continuous tidying plus final sweep at job completion. |
| Circuit Breaker | Haiku | 1 | Monitors all relay traffic for loops and forces resolution. |
| Muse | Gemma 4 (Ollama) | 0-1 | Reframes problems, offers lateral perspective. Optional. |

Load role-specific instructions from `references/roles/` when bootstrapping each session.

## How It Works

### 1. You Give the Goal

Tell the Orchestrator what to build. One sentence or a full spec. The Orchestrator is the only agent you talk to directly. You have read-only status access to any agent, but all directives go through the Orchestrator.

### 2. Orchestrator Plans

The Orchestrator breaks the goal into discrete tasks, determines how many workers are needed, and drafts an implementation plan.

### 3. Dissenter Reviews the Plan

Before any code is written, the Orchestrator sends the plan summary to the Dissenter. The Dissenter challenges the reasoning: architectural decisions, dependency choices, interface designs, potential failure modes. The Dissenter works from summaries only and never accesses the filesystem directly.

The Orchestrator tags tasks as **substantive** or **minor**. Only substantive tasks go through dissent review. Trivial work (renaming, config updates, formatting) skips dissent.

### 4. Orchestrator Staffs the Job Site

After the plan survives dissent, the Orchestrator spawns worker sessions using the bootstrap script. Each worker launches in the current project directory with its role-specific CLAUDE.md loaded.

### 5. Workers Build

Workers execute assigned tasks independently. They do not consult the Orchestrator for mid-implementation decisions. Sonnet is capable of making reasonable choices on its own. Workers can ask each other questions via Relay when their tasks have dependencies.

### 6. Cleaner Runs Continuously

The Cleaner operates in the background throughout the build: removing dead code, organizing imports, fixing lint errors, stripping debug artifacts. Think of it as the crew member keeping the job site clear so workers can focus. A final deep sweep runs when the Orchestrator declares the goal complete.

### 7. Dissenter Reviews Results

Before commit, the Orchestrator sends completed work summaries back to the Dissenter for a second review. Same rules: logic and reasoning review only, no filesystem access, summaries only.

### 8. Orchestrator Approves and Reports

The Orchestrator gives final approval, coordinates the commit, and reports results back to you.

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

**When the Orchestrator should spawn the Muse:**
- Before a major architectural decision where the obvious choices feel limiting
- When the crew has been stuck on the same problem for multiple cycles
- When the Dissenter and Orchestrator are approaching a loop and a fresh perspective might prevent it
- When the owner asks for it

The Muse is the only role that is not always on the job site. It shows up when needed and goes back to its coffee.

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
- `foreman-orchestrator`
- `foreman-dissenter`
- `foreman-worker-1`, `foreman-worker-2`, ...
- `foreman-cleaner`
- `foreman-circuit-breaker`
- `foreman-muse` (when spawned)

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
