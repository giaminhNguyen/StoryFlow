# StoryFlow Operations Guide

Operator documentation for running StoryFlow locally on Windows 11. Everything here matches the
implemented command-line flags (`--help` of each tool is the source of truth).

Contents: [Requirements](#1-requirements) | [First run](#2-first-run) | [Configuration and providers](#3-configuration-and-providers) |
[Data layout](#4-data-layout) | [Logs](#5-logs) | [Backup and restore](#6-backup-and-restore) |
[Safe migration policy](#7-safe-migration-policy) | [Stopping](#8-stopping-the-app) | [Troubleshooting](#9-troubleshooting) |
[Security](#10-security-notes) | [Upgrading](#11-upgrading) | [Release verification](#12-release-verification-checklist)

## 1. Requirements

| Need | Version | Notes |
|---|---|---|
| Windows | 11 | POSIX works for the Python tooling; the `.bat` scripts are Windows only |
| Python | 3.11+ (tested: 3.13.x) | must be on `PATH` for the setup; the app itself runs from `backend\.venv` |
| Node.js | 20+ (tested: 24) | only needed to build/test the UI (`npm ci`, `npm run build`) |
| Git | any recent | `bootstrap.py` clones the pinned external sources |
| `claude` CLI (optional) | logged in | real story/canon generation (`STORYFLOW_STORY_RUNNER=claude-cli`) |
| VieNeu-TTS checkout (optional) | v3 turbo, own venv | real speech synthesis (`STORYFLOW_VIENEU_ROOT`) |

No account, credential or API key is needed to run StoryFlow. The optional providers use their own login
(the `claude` CLI session); StoryFlow never reads or stores it.

## 2. First run

```bat
git clone <repo-url> StoryFlow
cd StoryFlow
scripts\setup.bat          REM one time (safe to repeat): prerequisites, pinned sources, venv, npm ci + build, doctor
start-app.bat              REM starts API + runtime + UI and opens http://127.0.0.1:8765
```

* `scripts\setup.bat /check` only validates prerequisites and prints what is present; it changes nothing.
* `start-app.bat` runs, in order: `bootstrap.py --check` (pins) -> venv present -> `python -m storyflow doctor --quick`
  (errors stop the start, warnings continue) -> `python -m storyflow migrate` (safe migration, see section 7) ->
  `python -m storyflow.api --open-browser`. Extra arguments are passed to `storyflow.api`
  (`start-app.bat --port 9000`). `start-app.bat /check` runs every pre-flight step (migration as a dry run) without starting the server.
* `start-app.bat --fake` starts a deterministic offline demo (fake subtitle source `demo-video`, fake story/TTS runners).
  Nothing is sent to any model; UI labels these providers "Demo (fake)".
* First workflow in the UI: create it (the form is prefilled with the demo video id in `--fake` mode), add a project,
  **assign a detected runner** (detected runners stay unused until you assign them), press Start.

URLs (loopback only): UI `http://127.0.0.1:8765/`, API `http://127.0.0.1:8765/api/` (`/api/health`, `/api/providers`).
Default port 8765; change with `--port`. For UI development only: `cd frontend && npm run dev` (Vite on 5173, proxies `/api`).

Optional real subtitle dependencies (only for `STORYFLOW_SUBTITLE_PROVIDER=external`, the default):

```bat
backend\.venv\Scripts\python -m pip install -r backend\requirements-subtitle.txt
```

## 3. Configuration and providers

Configuration is environment variables (`STORYFLOW_*`) or a git-ignored `backend\.env` (copy `backend\.env.example`).
Real environment variables win over the file. There are no secrets in this file by design.

| Capability | Default | Select | Readiness |
|---|---|---|---|
| Subtitles | `external` (pinned Subtitle_supperVip in an isolated subprocess) | `STORYFLOW_SUBTITLE_PROVIDER=external\|fake\|none` | needs `requirements-subtitle.txt` |
| Story / canon | `none` (no text goes to any model) | `STORYFLOW_STORY_RUNNER=claude-cli` | `claude` on `PATH` (or `STORYFLOW_CLAUDE_CLI`) and logged in |
| TTS | `none` | `STORYFLOW_TTS_ENGINE=vieneu` + `STORYFLOW_VIENEU_ROOT=<checkout>` | VieNeu-TTS venv present |

Provider states: `ready`, `unavailable` (a dependency is missing; the message says what to install),
`misconfigured` (a setting is wrong), `disabled` (`none`), `fake`. Check them in the UI header/Providers panel,
`GET /api/providers`, the startup log, or `python -m storyflow doctor`. Real `claude` runs consume your own Claude usage.

Other variables: `STORYFLOW_LOG_LEVEL` (default `info`), `STORYFLOW_LOG_DIR`.

### Adding videos, playlists and channels (batches)

One workflow can hold many videos. Send sources to `POST /api/workflows/{id}/sources`:

```json
{"sources": ["https://www.youtube.com/@channel", "https://youtu.be/VIDEO_ID", "inbox:my-story.txt"],
 "limit": 10, "languages": ["vi"], "reprocess": false, "min_duration_seconds": 300}
```

| Source | Result |
|---|---|
| video link / bare 11-char id (`watch?v=`, `youtu.be/`, `/shorts/`, `/live/`, `/embed/`) | one project |
| playlist (`playlist?list=`) or channel (`/@handle`, `/channel/UC...`, `/c/name`, `/user/name`) | up to `limit` videos (1..1000, default 10; `null` is refused); a channel lists the **newest** first, a playlist lists its **first N in playlist order**; the feed is remembered |
| `inbox:file.txt` | one project read from a subtitle file you put in `runtime\inbox\` (`.txt`, `.srt`, `.vtt`) |

* **Every video is processed once (ledger).** A video that already has a project in this workflow, or in any
  workflow that was not cancelled (including workflows created from the UI with `source.video_id`), is reported under
  `duplicates` (`in_workflow`, `already_processed`, `repeated`) and not added again. Send `"reprocess": true` to add it anyway.
* **Size limits.** One call creates at most 1000 projects; the rest is reported (`truncated: true`, `not_added`) and the next
  call picks it up through the ledger. The response carries a small `workflow` summary (`id`, `status`, `project_count`,
  `counts`); read `GET /api/workflows/{id}` for the full snapshot. Listing is bounded (300 s per call, at most 2 yt-dlp
  processes at a time).
* `POST /api/workflows/{id}/sync` re-scans the stored channels / playlists and adds only videos not seen before
  (`known_count` and `last_scanned_at` are the cursor; the feed's `limit` and `min_duration_seconds` are remembered and
  a wider window than the first add is scanned so videos published in between are not lost).
  `GET /api/workflows/{id}/feeds` lists the feeds. Every feed report says `window_full` (the listing returned as many
  videos as asked: older videos may exist), `partial` (yt-dlp failed part-way; also a `partial_listing` entry in `warnings`)
  and `order`. Adding to a finished workflow re-opens it. A feed that cannot be listed is marked `error` and the others still sync.
* Channel / playlist links need `yt-dlp` (`backend\requirements-channel.txt`, installed by `setup.bat`; no API key).
  Video links and inbox files work without it. Settings: `STORYFLOW_YTDLP_PYTHON`, `STORYFLOW_LISTER_TIMEOUT`
  (an invalid value falls back to 120 s and is reported by `doctor`).
* **Inbox** (`STORYFLOW_INBOX_DIR`, default `runtime\inbox`, created automatically): a file named `<video_id>.txt|srt|vtt`
  is used instead of asking YouTube, even while a retry is waiting (handy when YouTube blocks your IP or a video has no
  subtitles); `inbox:name.txt` adds a project from a file that has no video at all (checked when you add it; a missing or
  unreadable file is a `422`). Files may be UTF-8, UTF-16 (BOM) or Windows cp1252. Only plain file names inside that folder are read.
* Videos are picked up in listing order. For a batch use `"failure_policy": {"on_no_subtitle": "skip",
  "on_permanent_error": "continue"}` (below) so one bad video does not stop the rest.

### Presets: Fast / Balanced / Quality (review and revision)

`"preset"` in the workflow config chooses how much quality control runs after the story is written (recorded as
`preset` + `review` when the workflow is created; default **fast**):

| Preset | Pipeline | Extra cost |
|---|---|---|
| `fast` | source, canon, story, tts, audio | none |
| `balanced` | ... story, **review**, tts, audio: an editor call checks canon, logic, style and length and records the verdict + issues; the story is not changed; a failed review is skipped (advisory) | one more model call per story |
| `quality` | ... story, **review**, tts, audio: the editor also returns a **corrected story** (a new story version that TTS reads); up to 2 rounds (the corrected story is reviewed again); a failed review blocks the project | up to 2 more calls, each with a full story in and out |

An explicit `"review": {...}` block is laid **over** the preset (`enabled`, `revise`, `max_rounds` 1..3, `on_failure`
`"block"|"skip"`); `"review": null` is refused. The review is ONE call that covers all four aspects (parallel reviewer
"teams" would need more than one model runner). A corrected story must be at least 95% of the story it corrects and at
least 85% of the original target, must end like a finished text and must not contain the format marker, so repeated
revisions cannot shrink or truncate the story. The verdict, the issues, the number of rounds and the revision count are
shown in the project detail (`review`, `revision_count` in the API).

### Failure policy (subtitles and batches)

`failure_policy` in the workflow config decides what happens when ONE video fails. New workflows get
`{"subtitle_retries": 5, "retry_base_seconds": 30, "retry_max_seconds": 900, "on_no_subtitle": "pause",
"on_permanent_error": "pause"}`; a policy you give is merged **over** these values (`{}` = the recommended policy; an
explicit `null` switches that setting back to "unlimited / immediate").

| Situation | What StoryFlow does |
|---|---|
| YouTube blocks / times out (`provider_blocked`, `provider_timeout`) | Retry after 30 s, 60 s, 120 s ... (capped at 15 min). The next-attempt time is stored, so a restart never hammers the provider. After `subtitle_retries` attempts the error becomes `subtitle_retries_exhausted`. An operator `retry` / `resume` starts again with fresh attempts. |
| No subtitle, wrong language, empty, private / removed / age-restricted video (`subtitles_unavailable`, `language_unavailable`, `empty_source`) | `on_no_subtitle: "pause"` (default) pauses the workflow; `"skip"` marks only that project **skipped** and the rest of the batch continues. |
| Anything else that goes wrong for one video (`subtitle_failed`), retries used up, source not configured | `on_permanent_error: "pause"` (default) pauses; `"continue"` marks only that project **needs_attention**. |
| Provider not installed / misconfigured (`provider_unavailable`) | Always pauses (it would fail every video the same way). |

Skipped / needs-attention projects are shown with their reason (UI: project card; API: `state`, `status_reason`,
`status_detail.step`) and are counted in the workflow `counts`. A workflow finishes when every project is completed,
skipped or needs-attention. Values are validated when the workflow is created (`422` with
`reason: invalid_failure_policy`); the `source`, `story` and `tts` blocks are type-checked too (`invalid_config`).

With `on_permanent_error: "continue"` this also covers the AI / TTS steps (including a step whose output failed
validation when it was collected): if `canon`, `story`, `review`, `tts` or `audio` fails for good only that project
becomes **needs_attention**; without it the whole workflow pauses. **Systemic problems always pause the workflow** even
under `continue` (runner crashed / timed out, quota or login problem, no runner available, missing CLI or voice), and so
does a **circuit breaker**: when 3 projects already ended with the same reason, the next one pauses the workflow instead
(the cause is not the video). An unexpected internal error in one project is recorded as `internal_error` for that
project and never stops the round for the others.

**Bringing a project back:** `POST /api/projects/{project_id}/retry` (skipped or needs-attention only; anything else is a
`409 not_retryable`). A failed AI / TTS step gets a fresh attempt, the subtitle step simply runs again, and a workflow
that had already finished is re-opened.

### How many projects run at once (`batch`)

`"batch": {"max_active": 2}` in the workflow config (default for new workflows: 2; `null` = all at once). Only the first
N unfinished projects, in creation order (= newest video first), run the job-backed steps (canon / story / review /
tts / audio). When one completes, is skipped or needs attention, the next one starts immediately, so stages overlap (one
story is being written while another is read aloud). Channels are simply worked through in the order they were added.

The window does **not** hold back subtitles: the `source` step is inline (no job, no runner slot), so every waiting
project outside the window also fetches its subtitles ahead of time. These fetches run **one after another inside the
runtime tick** (not as parallel subtitle workers), with the usual failure policy (transient errors back off durably,
`on_no_subtitle: skip` ends only that project). A very large batch can therefore make the first tick long (the runtime
iteration counter does not move while it runs) and can trigger provider rate limits / backoff.
A finished project is recorded as `completed` (project `status`), so a batch of hundreds costs one row read per finished
project instead of a walk through every step (60 projects: about 3x faster per runtime iteration than before).

### Story length

Unless the workflow config sets `story.target_length` (an integer, words), the story is asked to be **at least as long as
the source transcript** (whitespace-separated word count, capped at 15,000 words because one model call cannot write more).
A story shorter than 85% of the target, one that repeats the same lines over and over, or one that contains an absolute
file path is rejected and rewritten by the normal retry logic. Long stories take longer: `scripts\setup.bat` sets
`STORYFLOW_STORY_TIMEOUT=1800`.

### The final audio file

Every completed project ends with ONE joined audio file, `audio\<generation>\run-001\final.wav` (the chunks in order with
0.3 s between them, written as a stream so long stories do not need much memory). The API exposes it as
`audio.final_path` and the project page has a "Full audio" player and a Download link. Audio files may be up to 4 GiB
(text artifacts 256 MiB; raise with `STORYFLOW_AUDIO_MAX_MB` / `STORYFLOW_ARTIFACT_MAX_MB`).

## 4. Data layout

Everything mutable lives under `runtime\` (git-ignored) unless you override it:

| Path | Content |
|---|---|
| `runtime\storyflow.db` | SQLite database (WAL mode; `-wal`/`-shm` files appear while running) |
| `runtime\artifacts\` | append-only artifact tree: `projects\<id>\...` (sources, canon, story versions, audio chunks) |
| `runtime\backups\` | backups (`backup-<timestamp>\`) and pre-migration backups (`pre-migrate-*`) |
| `runtime\logs\storyflow.log` | rotating application log |

The database references artifacts by relative path, so **the database and the artifact tree belong together**:
back up and restore them as a pair (the tools do).

## 5. Logs

* Console (stderr) plus `runtime\logs\storyflow.log`, UTF-8, rotating: 5 MB per file, 5 rotated files kept
  (`storyflow.log.1` ...). Move with `--log-dir` or `STORYFLOW_LOG_DIR`; `--no-log-file` = console only;
  `--log-level debug|info|warning|error`.
* Access lines contain method and path only (no query strings).
* Never logged: story text or transcript content, prompts, secrets. Keys/tokens/passwords/bearer values are masked,
  absolute file paths become `<path>`, tracebacks are redacted, messages are length-capped. There is no switch that
  turns content logging on.
* A clean stop ends with the line `shutdown complete`.

## 6. Backup and restore

### Backup (safe while the app runs)

```bat
scripts\backup.bat                          REM -> runtime\backups\backup-<timestamp>\
scripts\backup.bat --to D:\safe\storyflow-2026-09-25 --label before-upgrade
scripts\backup.bat --no-artifacts           REM database only (NOT sufficient to recover audio/story files)
```

Equivalent: `backend\.venv\Scripts\python -m storyflow backup [--to DIR] [--label TXT] [--no-artifacts] [--json]`
(`--db`, `--artifact-root`, `--backup-dir` override the defaults).

Semantics: the database is snapshotted first with the SQLite online-backup API (consistent even during writes),
integrity-checked, then the artifact tree is copied. Artifacts are written before the database rows that reference
them, so the snapshot never references a missing file; files created after the snapshot are merely extra. The backup
directory is staged and renamed into place last (a crash never leaves a plausible half backup) and an existing
destination is never overwritten. `manifest.json` records SHA-256 hashes and sizes of everything.

### Restore (stop the app first)

```bat
scripts\restore.bat --from runtime\backups\backup-20260925-101500
scripts\restore.bat --from D:\safe\storyflow-2026-09-25 --force
```

Equivalent: `python -m storyflow restore --from DIR [--db PATH] [--artifact-root DIR] [--force] [--json]`.

1. The backup is fully verified first (manifest, every hash, database integrity, known schema revision). If that
   fails nothing is touched (exit 1, or 2 for a newer/unknown schema).
2. A database that is in use (app running) is refused, and so is a non-empty target, unless `--force`.
3. With `--force` the existing database (+ WAL files) and artifact directory are **moved** to
   `<target>.pre-restore-<timestamp>` - never deleted. A failure during the swap moves everything back.
4. Database and artifacts are restored together; afterwards integrity and artifact references are re-checked.
5. Restore never migrates. If the backup is from an older schema, `start-app.bat` (or `python -m storyflow migrate`) upgrades it safely.

Exit codes for all `python -m storyflow` commands: `0` ok, `1` problem found/failed, `2` usage error or refused.

To verify a backup end to end without touching live data, restore it into a scratch location:
`python -m storyflow restore --from <backup> --db C:\scratch\t.db --artifact-root C:\scratch\artifacts`, then
`python -m storyflow doctor --db C:\scratch\t.db --artifact-root C:\scratch\artifacts`.

## 7. Safe migration policy

`start-app.bat` runs `python -m storyflow migrate` before starting (also usable by hand: `--dry-run`, `--json`).

| Database state | Action |
|---|---|
| missing / empty | created at the current head |
| at head | nothing to do |
| older known revision | verified pre-migration backup (`runtime\backups\pre-migrate-<from>-to-<to>-<ts>`), then `alembic upgrade head`, then integrity + revision checks |
| newer or unknown revision | refused (exit 2): this code is older than the data; update StoryFlow instead |
| tables but no `alembic_version` | refused (exit 2): not a StoryFlow-managed database; it is never touched |

If the backup cannot be taken or verified the database is left untouched. If the upgrade itself fails, the backup is
kept and the exact `restore` command is printed. Manual recovery: stop the app, run
`scripts\restore.bat --from <pre-migrate backup> --force`, then investigate with `python -m storyflow doctor`.
Migrations only ever add (no data is dropped) and there is no downgrade.

## 8. Stopping the app

Press **Ctrl+C** in the console window. Uvicorn stops, the embedded runtime finishes its current iteration and
joins, the database connections are closed and `shutdown complete` is logged; the exit code is 0. cmd may ask
"Terminate batch job (Y/N)?" - answer either way, the server is already stopped. Work in progress is not lost: pending
jobs stay durable in the database and continue after the next start. Do not close the window with the X or kill the
process unless it hangs (a killed process is still recovered on the next start through lease expiry, but a graceful
stop is cleaner). Headless: `CTRL_BREAK_EVENT` to a process started in a new process group (Windows) or SIGINT/SIGTERM (POSIX).

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `start-app.bat` says pinned sources missing/differ | run `scripts\setup.bat` (fetches the exact pins from `sources.lock.json`; local changes in `external\` are never reset). `python bootstrap.py --check` shows which dependency |
| "backend virtual environment is missing" | run `scripts\setup.bat`; if Python was upgraded delete `backend\.venv` and re-run it |
| Port 8765 already in use (`storyflow.api` exits 2 with one line) | another StoryFlow or program uses it: stop it or `start-app.bat --port 9001` |
| Browser shows a "frontend not built" hint / doctor WARN `frontend` | `cd frontend && npm ci && npm run build` (or `scripts\setup.bat`); the API works without it |
| Provider `unavailable` | read the message: e.g. `pip install -r backend\requirements-subtitle.txt`; `claude` not found or not logged in (run `claude` once interactively); VieNeu root missing |
| Provider `misconfigured` | fix the `STORYFLOW_*` value in `backend\.env` or the environment, restart |
| Workflow pauses with `provider_blocked` / subtitles fail with a YouTube block | the subtitle provider rate-limits/blocks some IPs; wait, change network, or use a video with accessible captions. A missing/unavailable subtitle fails the step permanently (`subtitles_unavailable`); use Retry after fixing |
| Workflow `paused` "waiting capacity" | no ready runner: assign a detected runner to the workflow, or wait for a quota/cooldown window to end |
| Migration refused: database newer/unknown | the data was written by a newer StoryFlow: run the newer version, or restore an older backup |
| Migration refused: no `alembic_version` | the file is not a StoryFlow database; point `--db` at the right one |
| Restore refused: "in use" | stop the app first |
| Restore refused: target not empty | inspect it, then use `--force` (old data is kept as `.pre-restore-*`) |
| Restore: "backup is corrupt" | hash mismatch: use another backup; the source directory was modified or truncated |
| doctor FAIL `database_integrity` | restore the latest good backup with `--force` |
| Symlink tests are skipped (`6 skipped`) | Windows without symlink privilege (enable Developer Mode to run them); harmless |
| `Terminate batch job (Y/N)?` after Ctrl+C | normal cmd behaviour; the server has already stopped |

Collect for a bug report: `python -m storyflow doctor --json` and the tail of `runtime\logs\storyflow.log`
(both are already redacted; still skim them before sharing).

## 10. Security notes

* Loopback only: the server binds `127.0.0.1` and refuses any other `--host` (exit 2) unless `--allow-non-loopback`, which
  logs a warning. **The API has no authentication**; anyone who can reach the port controls your workflows. Do not expose it.
* Host-header and CORS guards limit browser-based attacks (DNS rebinding, foreign origins); request bodies are capped.
* Secrets are never stored: API bodies with secret-looking keys are rejected, the `.env` file holds no credentials, logs are redacted.
* The story runner (`claude -p`) runs with tools disabled in an empty temporary directory and without `STORYFLOW_*` variables.
* Artifact URLs only serve files under the artifact root; every refusal is the same 404.
* Backups contain your story text and audio: treat them like the data itself.

## 11. Upgrading

1. Stop the app; `scripts\backup.bat --label before-upgrade`.
2. `git pull` (never `git reset --hard`; `bootstrap.py` will not overwrite local changes in `external\`).
3. `scripts\setup.bat` (re-syncs pins, requirements, frontend build).
4. `start-app.bat` (safe migration takes its own verified backup for older databases).

## 12. Release verification checklist

```bat
backend\.venv\Scripts\python scripts\release_smoke.py                       REM full matrix, ~10 min incl. backend tests
backend\.venv\Scripts\python scripts\release_smoke.py --skip-backend-tests  REM matrix without the full pytest run
backend\.venv\Scripts\python scripts\release_smoke.py --only 5,6,shutdown   REM selected steps (see --list)
```

The script uses temporary databases/artifacts/logs only (never `runtime\`), picks free ports, and always cleans up its
child processes. Steps: 1 bootstrap pins, 2 migration (empty -> head, dry run), 3 backend tests, 4 frontend build+unit tests,
5 fake end-to-end through the real server (UI at `/`, story + audio chunk), 6 pause/resume/retry/cancel, 7 restart mid-pipeline,
8 two-runtime/session isolation tests, 9 artifact path safety, 10 online backup + restore + second server, 11 graceful shutdown
(exit 0 + `shutdown complete`), 12 real providers (only with `STORYFLOW_RUN_REAL_SMOKE=1`). Exit 0 means every executed step passed.

Also: `cd backend && .venv\Scripts\python -m pytest -q`, `cd frontend && npm run typecheck && npm test && npm run build`.
