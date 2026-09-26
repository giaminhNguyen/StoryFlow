# CLAUDE.md

@AGENTS.md

## Claude Code notes

- The project overview, architecture rules, commands, recipes and gotchas are in `AGENTS.md` (imported above). Keep that
  file as the single source of truth and update it when the architecture or commands change.
- Answer the owner in Vietnamese. Use the Write tool (not shell heredocs) for any file content that contains backslashes.
- Do not run the real Claude / VieNeu pipeline for testing; the owner's server on port 8765 may be mid-batch.
