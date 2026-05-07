# Claude Relay Setup

Foreman requires Claude Relay to be installed as a Claude Code plugin. Follow these steps if you haven't set it up yet.

## Installation

### 1. Add the marketplace

From any Claude Code session:

```
/plugin marketplace add innestic/claude-relay
```

### 2. Install the plugin

```
/plugin install relay@claude-relay
```

This registers the MCP server and slash commands.

### 3. Launch sessions with the channel capability

Every session that sends or receives messages must be launched with:

```bash
claude --dangerously-load-development-channels plugin:relay@claude-relay
```

The `dangerously-` prefix is required until Anthropic promotes the channels capability to general availability.

## Verifying the Installation

In any session launched with the channel flag, try:
- "what sessions are active?" (should invoke `relay_peers`)
- `/relay-rename test-session` (should rename successfully)

If these work, Relay is ready for Foreman.

## Troubleshooting

Runtime data lives under `$CLAUDE_PLUGIN_DATA` (`~/.claude/plugins/data/relay-claude-relay/`).

```bash
DATA=~/.claude/plugins/data/relay-claude-relay

# View today's logs
tail -f "$DATA/logs/relay-$(date +%Y-%m-%d).log" | jq

# Check if hub is running
pgrep -f hub-daemon.ts

# Force reset (kills hub and removes socket)
pkill -f hub-daemon.ts && rm -f "$DATA/hub.sock"
```

Per-session MCP stderr lives under `~/Library/Caches/claude-cli-nodejs/<project-slug>/mcp-logs-*/`.
