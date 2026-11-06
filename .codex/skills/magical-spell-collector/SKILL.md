---
name: magical-spell-collector
description: Operate the local Magical Spell Collector for bounded Arctic Reddit-archive collection, evidence-based ritual analysis, resume, statistics, rights review, and safe exports. Use for this collector; not for live Reddit scraping or publishing unapproved source text.
---

# Magical Spell Collector

Start with:

```powershell
Get-Command spell-collector -ErrorAction SilentlyContinue
spell-collector --json doctor
spell-collector --json stats
```

If the installed command is unavailable, run it from the repository with
`uv run --python 3.11 spell-collector`.

For a first or diagnostic collection, preview and keep the hard 100-record limit:

```powershell
spell-collector --json pilot --limit 100 --dry-run
spell-collector --json pilot --limit 100 --source auto
spell-collector --json verify
```

For a larger task, inspect `config/default.yaml`, free space, dates, and the planned
subreddits before removing `--dry-run`. Use explicit `--after` and `--before` dates.
Prefer the installed high-level commands; use `spell-collector arctic query` only for a
bounded read from already imported local data.

Rules:

- Never use Arctic `pull`, `publish`, bulk torrents, proxy rotation, or access bypasses.
- Keep the configured storage limit and free-space reserve active.
- Run `resume` after an interruption; do not duplicate or delete raw records.
- Use `analyze --reanalyze review --use-ai --model <local-model>` for optional local
  inference. AI output without literal source evidence stays `REVIEW`.
- Run `export` and then `verify` before reporting results.
- Treat `rights_status=UNKNOWN` as no publication permission. Do not publish full Reddit
  text. Use `rights approve` only after real human rights review.
- Do not mark a ritual complete to meet a target count. Report actual counts from `stats`.

The raw escape hatch is deliberately read-only and bounded:

```powershell
spell-collector --json arctic info
spell-collector --json arctic query Spells --limit 20 --contains ritual
```
