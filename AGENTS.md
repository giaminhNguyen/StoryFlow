# AGENTS.md - read this first (for coding agents)

This file tells an AI coding agent what StoryFlow is, how it is built, how to run and test it, and the traps that
have already cost time. Humans: read `README.md` (English) or `README.vi.md`. Operator guide: `docs/OPERATIONS.md`.

## 1. What this project is

StoryFlow is a **local workflow engine that turns YouTube videos into new stories and narrated audio**:

```text
YouTube video / playlist / channel (or a subtitle file in runtime/inbox)
  -> subtitles (source transcript)          [inline step, sub-process]
  -> canon analysis (characters, events)    [Claude CLI job]
  -> NEW alternate-branch story             [Claude CLI job; default length >= source]
  -> optional review + revision             [Claude CLI job; presets balanced / quality]
  -> speech text + chunk plan               [story-tts-adapter skill]
  -> VieNeu-TTS audio chunks -> ONE final.wav per story   [local TTS sub-process]
```

- It runs **only on the user's machine** (FastAPI bound to `127.0.0.1:8765`, no auth, no telemetry) with a durable
  **SQLite** database, so every step is restartable. A React UI is served by the same process.
- The owner works mostly in **Vietnamese** and operates StoryFlow *through an AI assistant calling the local HTTP API*
  (not by typing commands). Stories/subtitles in real runs are Vietnamese; code, comments and docs are English.
  Reply to the user in Vietnamese unless asked otherwise.
- One **workflow** = many **projects** (one per video); each project walks the **steps**
  `source -> canon -> story -> [review] -> tts -> audio`.

## 2. Stack and layout

Python 3.13 (>=3.11), FastAPI, SQLAlchemy 2, Alembic, SQLite (WAL); React 19 + Vite + TypeScript; pytest, Vitest.

```text
backend/storyflow/
  models.py database.py          ORM models (naive LOCAL time, see gotchas), engine
  queue.py dispatcher.py agents.py runners.py protocol.py roles.py   durable job queue, runner selection, quota failover
  orchestrator.py pipeline.py    restartable state machine over DB state; StepHandler / InlineStepHandler contracts
  story_steps.py                 SourceStep (inline), CanonStep, StoryStep, validators, FakeStoryPipelineRunner
  review_steps.py                ReviewStep + FakeReviewRunner
  tts_steps.py                   TTSStep, AudioStep, assemble_final_audio (streams final.wav)
  policy.py presets.py           failure_policy / batch window / preset+review settings (pure, strict-parse + lenient-read)
  sources.py source_service.py   URL parsing, yt-dlp lister, inbox files; add_sources / sync_feeds, ledger
  services.py                    WorkflowService / RunnerService = THE mutation boundary of the API
  readmodels.py                  query side (snapshots dataclasses -> JSON), never mutates
  providers.py                   STORYFLOW_* config, provider stack assembly, readiness
  integrations/                  claude_cli.py, vieneu.py(+worker), subtitle_subprocess.py(+worker) - all sub-process based
  api/                           app.py routes.py schemas.py errors.py artifacts.py (safe file serving)
  runtime/                       app.py (build_runtime: wires chain + router + validators), loop.py, supervisor.py
  ops/                           doctor, backup, restore, migrate
backend/alembic/versions/        0001 ... 0009 (head: 0009_feed_min_duration)
backend/tests/                   ~66 files, ~1,900 tests; fakes for every external system
frontend/src/                    pages/ components/ api/types.ts (mirrors readmodels dataclasses) test/fixtures.ts
scripts/                         setup wizard (configure_providers.py), backup/restore .bat, release_smoke.py (13 steps)
docs/                            OPERATIONS.md (operator guide), ENGINEERING_HISTORY.md (historical phase notes)
runtime/ external/ skills/       GENERATED, git-ignored: data / pinned clones / skill copies (never edit, never commit)
```

External projects are **pinned by commit** in `sources.lock.json` and fetched by `bootstrap.py`: Subtitle_supperVip
(subtitle worker upstream) and Skills-Import (`story-branch-writer`, `story-tts-adapter`). Do not edit `external/` or `skills/`.

## 3. Commands (Windows 11; use `backend/.venv/Scripts/python.exe`)

```bat
setup.bat                      REM one-click setup (interactive provider wizard); scripts\setup.bat /check = dry run
start-app.bat [--fake] [--port N] [--no-open]     REM API + runtime + UI; --fake = offline demo with fake providers
cd backend && .venv\Scripts\python.exe -m storyflow.api            REM start the server without the .bat wrapper
backend\.venv\Scripts\python.exe -m storyflow doctor|migrate|backup|restore     REM ops CLI (exit 0 ok / 1 problem / 2 usage)

cd backend  && .venv\Scripts\python.exe -m pytest -q tests -p no:cacheprovider    REM ~6 min, ~1,933 tests
cd frontend && npm run typecheck && npm test && npm run build                      REM 144 tests
python scripts\release_smoke.py --skip-backend-tests [--skip-frontend] [--only batch]   REM real servers on a temp workspace
```

Run single test files while iterating; run the **full** suite once before committing.

## 4. Architecture rules you must not break

1. **Nothing about progress lives in memory.** `Orchestrator.tick` re-derives "what is next" from the DB every time.
   Every step is idempotent and crash-safe: a domain row (durable intent) + a `PipelineJob` (execution) guarded by
   partial unique indexes and `dedupe_key`. Never hold a DB transaction across a runner / subprocess / network call.
2. **Step contract** (`pipeline.StepHandler`): `enabled / status / begin / link_job / finalize / mark_failed`;
   inline steps (`InlineStepHandler`, e.g. source) implement `status / run`. `enabled()` False = the step does not exist for
   that workflow (e.g. `review` under the `fast` preset): orchestrator and read models skip it. The chain is built in
   `runtime/app.py`. **Runner output is validated inside the dispatcher path** (`OutputValidatingRunner`), so `finalize` only
   sees valid output.
3. **Mutations only through `services.py`**, reads only through `readmodels.py`; the API layer never touches ORM rows.
4. **Config is validated at workflow creation** (`services.create_workflow`): `failure_policy`, `batch`, `preset`/`review`,
   `source`/`story`/`tts` types. It *records* the defaults that apply. Runtime reads are lenient (invalid stored config
   degrades to legacy behaviour, never crashes the loop). Keep that strict-write / lenient-read split.
5. **Failure handling** (`policy.py`, `orchestrator.py`): transient source errors back off durably
   (`story_projects.next_attempt_at`); `on_no_subtitle: skip` / `on_permanent_error: continue` end only ONE project
   (`skipped` / `needs_attention`); systemic errors (quota, auth, no runner, timeouts, missing CLI/voice) ALWAYS pause the
   workflow and a circuit breaker (3 identical ended projects) pauses it too. Project `status`: `active | completed |
   skipped | needs_attention` (`completed` is recorded by the orchestrator so finished projects cost O(1)).
6. **Artifacts**: files live under `runtime/artifacts/projects/<project_id>/...`; the DB and API only ever hold RELATIVE
   store paths (`ArtifactStore.resolve` guards escapes). No absolute paths, secrets or transcript text in logs / API errors
   (use the scrubbers in `claude_cli.py` / `readmodels.py`). Inbox files: plain names only, resolved inside the inbox.
7. **Time**: `models.utcnow()` returns naive LOCAL time. Always use the injected `ctx.clock()` (tests freeze it); never
   compare with `datetime.utcnow()` / aware datetimes.
8. **Sub-processes** (Claude CLI, VieNeu worker, subtitle worker, yt-dlp): isolated interpreter, hard timeout, process-tree
   kill, scrubbed environment (`STORYFLOW_*` stripped), JSON over stdin/stdout, generic path-free error messages.

## 5. Recipes

- **New pipeline step**: handler in a `*_steps.py` (+ `enabled()` if optional) -> add to the chain in `runtime/app.py` ->
  route the step name in `PipelineRouter` and add a fake runner -> register its validator (`wrap_runner_for_pipeline`
  registry) -> real runner support in `integrations/claude_cli.py` if it is an AI step -> `readmodels._STEP_DEDUPE_PREFIX`,
  snapshot info, `frontend/src/api/types.ts` (`StepName`) and UI -> tests (Stack pattern) -> docs.
- **New workflow config option**: strict parse + default in `policy.py` / `presets.py`, validate & record in
  `services.create_workflow`, lenient read where used, docs (`docs/OPERATIONS.md`), tests for parse / create / behaviour.
- **New migration**: new file in `backend/alembic/versions/`; SQLite cannot `ALTER` in a foreign key and recreating
  `story_projects` fights child FKs -> add plain nullable columns (soft references) or `batch_alter_table(..., recreate="never")`
  for drops; keep upgrades re-runnable. Then bump the head in `tests/test_migration_0004.py`, `tests/test_ops_migrate.py`,
  `frontend/src/test/fixtures.ts` (`schema_revision`) and the README migration list; add `tests/test_migration_000N.py`
  (clean cycle + data preserved). Existing DBs migrate via `python -m storyflow migrate` (verified backup first).
- **New API endpoint**: `api/schemas.py` (bounded pydantic, `extra="forbid"`) -> `api/routes.py` -> `services.py` /
  `readmodels.py` -> error contract in `api/errors.py` -> tests (`tests/test_api*.py`) -> `README.md` table + docs.

## 6. Testing conventions

- Deterministic: injected clock, no `sleep`, tmp_path stores, fakes for every external system
  (`FakeSubtitleClient`, `FakeStoryPipelineRunner`, `FakeReviewRunner`, `FakeTTSAdapterRunner`, `FakeAudioRunner`,
  `FakeVideoLister`, `tests/fake_claude_cli.py` that emulates `claude -p`). End-to-end pipeline tests use the `Stack`
  helper in `tests/test_phase5_integration.py` (real alembic-migrated SQLite + fake runners).
- Fixtures in `tests/conftest.py`: `db`, `session_factory`, `engine`. Tests must not write into the real `runtime/`.
- Prefer a regression test per bug and **mutation-check important ones** (break the code, confirm a test fails, restore).
- After changing behaviour, grep the tests for assertions on the old behaviour (e.g. stored workflow config, project status).
- Frontend: Vitest + Testing Library; keep accessibility roles/labels; API types in `frontend/src/api/types.ts` must match
  `readmodels.py` dataclasses.

## 7. Gotchas (each one already cost a debugging round)

- **Windows + Git Bash**: in heredocs the harness collapses `\\` to `\` (a `"\\x00"` became a real NUL byte in a source
  file). For any text containing backslashes write a script with the file-Write tool, then run it. Python does not
  understand Git-Bash `/tmp` (it becomes `E:\tmp`); use Windows paths or `cygpath -m`.
- **Never send Vietnamese JSON with `curl`** on this machine (arrives as `?`); call the API from Python with UTF-8.
- `*.bat` files use CRLF (`.gitattributes`); LF->CRLF warnings on other files are harmless.
- Long test runs: run in the background and wait for completion; do not poll in a loop.
- Windows symlink test is skipped without privilege (expected: 7 skipped tests in total).
- `runtime-server.log`, `runtime/`, `.env`, `*.wav`, `*.db` are git-ignored on purpose.

## 8. Working agreements with the owner

- **Real runs cost money/time**: story/canon/review use the owner's Claude usage; TTS on CPU is ~1 h per 45-minute story.
  Never start real story/TTS runs (or re-run finished ones) without being asked. Use `--fake` or the fakes for testing.
- **Do not restart the server while a batch is running** (`http://127.0.0.1:8765`, DB `runtime/storyflow.db`); after code
  changes run `python -m storyflow migrate`, then restart when idle.
- Work in **phases**; at the end of each phase: full tests green -> update `README.md` (+ `README.vi.md`) and docs ->
  commit -> push. Commit trailer: `Co-Authored-By: Claude ... <noreply@anthropic.com>` as the environment specifies.
- Parallel agents are welcome **only with strictly disjoint file ownership** (source AND test files); forks must not commit,
  push or run the full suite; one agent integrates, runs the full suite and commits.
- Independent read-only code reviews before a release found real bugs every time - repeat them for big changes.

## 9. Known limits / not built

Multi-agent parallel analysis, parallel audio worker pool and non-Claude agents (need more runners / GPU);
`get_workflow` is ~10 ms per project (heavy for hundreds); audio chunk progress is only registered when the audio step
finishes (files appear earlier in the run directory); no authentication (loopback only); Windows 11 is the only tested
platform; no LICENSE file yet.
