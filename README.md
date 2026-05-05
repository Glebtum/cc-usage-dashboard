# Claude Code Usage Dashboard

Local dashboard that reads your Claude Code transcripts (`~/.claude/projects/*.jsonl`)
and visualizes spend by day, by skill, by subagent, and per-session breakdown.

100% local — nothing leaves your machine. No API keys, no auth, no telemetry.

## Requirements

- macOS or Linux
- Python 3.8+
- Claude Code installed and used at least once (so `~/.claude/projects/` exists)

## Install

1. Unzip this folder anywhere, e.g. `~/cc-usage-dashboard/`
2. Make the launcher executable:
   ```
   chmod +x open.sh
   ```

## Run

```
./open.sh           # last 30 days
./open.sh 7         # last 7 days
./open.sh 365       # last year
```

The script:
1. Scans `~/.claude/projects/` and writes `data.json`
2. Starts a local HTTP server on `http://localhost:8765`
3. Opens it in your browser

To stop the server: `kill $(lsof -ti:8765)` or just close your terminal.

## What you'll see

- **Total cost** for the period (computed from token counts × Anthropic published prices)
- **Daily cost bars** — click a bar to filter the table to that day
- **By skill** — which `/skill-name` skills consumed the most
- **By subagent** — which Task() subagents consumed the most
- **Sessions table** — every session with title, $, message count, skills/agents used

## Notes

- Pricing is hardcoded (Opus 4.7 / Sonnet 4.6 / Haiku 4.5). Update `PRICES` dict in
  `usage_breakdown.py` if Anthropic changes rates or you use other models.
- Cost is an estimate based on token counts in transcripts; it should match your
  Anthropic console within a few percent.
- Skills are detected from `/command-name` markers and skill paths in the first
  user messages of each session.
