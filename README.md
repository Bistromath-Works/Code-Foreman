# Foreman

### Or: How to Get Six Artificial Minds to Build Something Without Arguing About It Forever

It is a well-established fact that a single AI coding agent, left to its own devices, will produce code that works. It will also produce code that is structured in a way that makes perfect sense to it and absolutely no sense to anyone who has to maintain it six months later, including, somewhat ironically, itself.

It is a less well-established but equally true fact that if you give *two* AI coding agents the ability to talk to each other, they will immediately begin disagreeing about architecture and never stop.

Foreman solves this by doing something remarkably similar to what humans have done on construction sites for thousands of years: putting one person in charge and giving everyone else a job title that sounds important but mostly just tells them to stay in their lane.

## What Is This

Foreman is a skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that turns multiple Claude Code sessions into a collaborative coding team. It is built on top of [Claude Relay](https://github.com/innestic/claude-relay), which handles the part where the agents actually talk to each other. Foreman handles the considerably more difficult part where they talk to each other *productively*.

You give the Orchestrator a goal. It plans the work, runs the plan past a Dissenter whose entire job is to poke holes in things, staffs up the right number of Workers, and manages the whole operation through to completion. A Cleaner tidies up continuously, like a very diligent Roomba with opinions about import ordering. A Circuit Breaker watches for the inevitable moment when two agents start going in circles, and politely but firmly tells them to stop.

And then there is the Muse, who runs on an entirely different model than the rest of the crew, does not appear to do any actual work, and yet somehow makes everyone else better at theirs. Every job site has one.

## The Crew

| Role | Model | What They Do | What They Emphatically Do Not Do |
|------|-------|-------------|----------------------------------|
| **Orchestrator** | Opus | Plans, delegates, tracks, approves | Write code, ever, under any circumstances |
| **Dissenter** | Model of your choice | Challenges plans before and after execution | Touch the filesystem or look at actual code |
| **Worker** | Sonnet | Builds things | Argue about architecture (that ship has sailed) |
| **Cleaner** | Haiku | Linting, formatting, dead code removal | Modify application logic |
| **Circuit Breaker** | Haiku | Detects and resolves conversational loops | Take sides until forced to |
| **Muse** | Model of your choice (must differ from other crew models) | Reframes problems sideways | Anything resembling real work |

## Prerequisites

You will need:

1. [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (2.1.80 or later, though frankly the version number is changing so fast that by the time you read this sentence it may already be wrong)
2. [Claude Relay](https://github.com/innestic/claude-relay) installed as a plugin
3. A local model runner (e.g. [Ollama](https://ollama.com)) or a separate API provider if you want the Muse on a non-Claude model (optional but recommended, in the way that coffee is technically optional)

## Quick Start

The quickest way to understand Foreman is to watch it work.

**Step 1.** Install Claude Relay if you haven't:
```bash
# From any Claude Code session
/plugin marketplace add innestic/claude-relay
/plugin install relay@claude-relay
```

**Step 2.** Install the Foreman skill. (Place the `foreman/` directory in your Claude Code skills path.)

**Step 3.** Configure your models. Copy `.env.local.example` to `.env.local` and fill in the API key for whichever provider you choose for the Dissenter. Edit `scripts/foreman-bootstrap.sh` to set your Dissenter and Muse model names.

**Step 4.** Open Claude Code in your project directory with the Relay channel flag:
```bash
claude --dangerously-load-development-channels plugin:relay@claude-relay
```

**Step 5.** Say something like:
> "Spin up Foreman. Build me a REST API for user authentication with JWT tokens, bcrypt password hashing, and refresh token rotation."

**Step 6.** Watch in mild astonishment as terminal windows begin appearing, agents begin talking to each other, and code begins materializing in your project directory as if by magic, except that it is not magic, it is just several language models being very organized about it.

## How It Works

The workflow is, in principle, simple. In practice it is also simple, which is what makes it work.

1. **You give the Orchestrator a goal.** This is the only agent you talk to. Chain of command exists for a reason.
2. **The Orchestrator plans.** It breaks your goal into tasks and decides how many Workers it needs.
3. **The Dissenter reviews the plan.** Before a single line of code is written, the Dissenter stress-tests the reasoning. Not the code. The *reasoning*. This is an important distinction that most review processes get wrong.
4. **Workers build.** They make their own implementation decisions without checking in on every variable name. They are, after all, competent.
5. **The Cleaner cleans.** Continuously. Like the tide, but for dead code.
6. **The Dissenter reviews the results.** A second pass after the work is done, before anything is committed.
7. **The Orchestrator approves.** You get your code.

The Circuit Breaker watches all of this passively and intervenes only when two agents have gone back and forth three times on the same point without progress. At four round-trips, it forces a decision. Unless the Orchestrator is one of the looping parties, in which case it escalates to you, because even on a construction site, sometimes the foreman needs the owner to make a call.

The Muse sits off to the side and offers a completely different perspective when asked. It runs on a model distinct from the Claude agents, which means it literally thinks differently. This is not a metaphor. The weights are different. The latent space is different. It will say things none of the Claude agents would think of, and occasionally those things will be exactly what was needed.

## The Bootstrap Script

The Orchestrator spawns crew members using `scripts/foreman-bootstrap.sh`. Each invocation opens a new terminal session with the correct model, role instructions, and Relay connection.

```bash
# The Orchestrator handles this automatically, but if you are curious:
./scripts/foreman-bootstrap.sh orchestrator
./scripts/foreman-bootstrap.sh dissenter
./scripts/foreman-bootstrap.sh worker 1
./scripts/foreman-bootstrap.sh worker 2
./scripts/foreman-bootstrap.sh cleaner
./scripts/foreman-bootstrap.sh circuit-breaker
./scripts/foreman-bootstrap.sh muse
```

## File Structure

```
foreman/
├── SKILL.md                          # Main skill trigger and protocol
├── scripts/
│   ├── foreman-bootstrap.sh          # Spawns crew sessions
│   ├── foreman-dissenter-bridge.py   # Connects Dissenter to relay hub via external API
│   └── foreman-muse-bridge.py        # Connects Muse to relay hub via local model runner
└── references/
    ├── protocol.md                   # Shared communication norms (all agents)
    ├── relay-setup.md                # Relay installation guide
    └── roles/
        ├── orchestrator.md           # The foreman
        ├── dissenter.md              # The professional skeptic
        ├── worker.md                 # The builders
        ├── cleaner.md                # The invisible hand of lint
        ├── circuit-breaker.md        # The conversation referee
        └── muse.md                   # The one making coffee
```

## Model Configuration

The Orchestrator, Workers, Cleaner, and Circuit Breaker all run as standard Claude Code sessions. Their models are set via the `--model` flag in `foreman-bootstrap.sh`.

The Dissenter and Muse are different:

**Dissenter** runs via `foreman-dissenter-bridge.py`, a lightweight Python script that connects directly to the relay hub and forwards questions to a model API of your choice. The default implementation uses the Google Gemini API (`google-genai` SDK), but the bridge is short enough to adapt to any provider.

**Muse** runs via `foreman-muse-bridge.py`, which connects to a locally-running model server (default: Ollama). The key requirement is that the Muse uses a model with different weights than the Claude crew — the value of the Muse comes from genuinely different reasoning, not a Claude model wearing a different hat.

To configure:
1. Set your model names in the `dissenter` and `muse` cases in `foreman-bootstrap.sh`
2. Copy `.env.local.example` to `.env.local` and add your API key
3. Install the relevant Python SDK: `pip install google-genai` (or your chosen provider's equivalent)

## Philosophy

The central insight of Foreman is not that AI agents can talk to each other. Claude Relay already proved that. The insight is that *talking is not the same as collaborating*, and collaboration requires structure: clear roles, a chain of command, defined communication norms, and someone whose job it is to say "actually, have you considered that you might be building the wrong thing?"

Most multi-agent coding setups are either a pipeline (agent A generates, agent B reviews, repeat until heat death) or a free-for-all (everyone talks to everyone and nothing gets decided). Foreman is neither. It is a job site. There is a foreman. There are workers. There is a plan. There is someone whose literal job is to disagree with the plan before anyone picks up a hammer.

And there is someone making coffee.

This may seem like a small thing, but Douglas Adams once noted that the problem with the future is that it keeps turning into the present. The same is true of software architecture. The Muse exists because sometimes the most valuable contribution is not a better algorithm but the observation that you are solving the wrong problem.

## v1 Limitations

In the spirit of honesty, which is a trait undervalued in README files:

- **Single repo only.** All agents work in the same project directory. Cross-repo coordination is a v2 problem.
- **No persistence.** When sessions close, the crew is gone. Each job is a fresh start.
- **Same host only.** Relay uses Unix sockets. Your agents all live on one machine.
- **The bootstrap script may need tweaking.** CLI flags for Claude Code and model runners evolve quickly. If a session fails to spawn, check the launch command first.

## Credits

Foreman is built on [Claude Relay](https://github.com/innestic/claude-relay) by [Innestic](https://github.com/innestic). Without Relay, these agents would be very organized and completely unable to speak to each other, which, come to think of it, describes most software teams already.

## License

MIT. Do with it what you will. If you build something wonderful with it, that is its own reward. If you build something terrible, we would prefer not to know, but we acknowledge your right to do so.

---

*"The major difference between a thing that might go wrong and a thing that cannot possibly go wrong is that when a thing that cannot possibly go wrong goes wrong it usually turns out to be impossible to get at or repair."*

Keep your agents talking. Keep your Dissenter dissenting. Keep your Muse caffeinated.
