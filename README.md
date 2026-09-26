# StoryFlow

**Turn YouTube videos into brand-new stories and narrated audio - locally, restartably and almost hands-free.**

English | [Tiếng Việt](README.vi.md)

StoryFlow is a local workflow engine for AI-assisted story production. Give it a YouTube video, playlist or a whole
channel and it will fetch the subtitles, analyse the story (characters, relationships, events), have an AI write a
**new alternate-branch story**, optionally review and correct it, adapt it for speech and read it aloud with a local
text-to-speech model - ending with **one complete audio file per story**.

It runs entirely on your own machine (bound to `127.0.0.1`), keeps every step in a durable SQLite database, and picks up
where it stopped after a crash or restart. You can drive it from the web UI, from its HTTP API, or simply by asking an
AI assistant (for example Claude Code) to call that API for you.

```text
 YouTube video / playlist / channel   (or a subtitle file you drop in a folder)
                 |
                 v   subtitles (any language; Vietnamese in our own runs)
          source transcript ---> canon analysis  (characters, relations, events)
                 |
                 v   Claude writes a NEW story, at least as long as the source
               story ---> [optional] review + corrected revision
                 |
                 v   text adapted for speech and split into chunks
        local TTS (VieNeu-TTS) reads every chunk  --->  ONE final .wav per story
```

> **Current status** - Phases 0-9 (local MVP) and the batch roadmap P0-P5 are complete.
> Migration head `0009_feed_min_duration` | backend tests `1933 passed, 7 skipped` | frontend `144 passed`, typecheck and
> build clean | release smoke `10/10` executable steps pass (an offline channel run end to end).
> Next: multi-agent analysis teams and parallel audio workers (they need more than one model runner / a GPU).

## Highlights

- **One-click setup.** `setup.bat` installs everything and asks the few questions it needs (Claude, voice, channels...).
- **Whole channels, not just one video.** Paste a channel, playlist or video links; every video is processed **once**
  (ledger), new uploads are picked up with a re-scan, videos without subtitles are skipped and the batch keeps going.
- **Durable and restartable.** All progress lives in SQLite; retries with backoff, crash recovery, pause / resume /
  retry, safe migrations with automatic backups.
- **Quality presets.** `fast` (default), `balanced` (an editor records a verdict + issues) and `quality` (the editor
  also returns a corrected story, up to 2 rounds).
- **Real providers or an offline demo.** Claude Code CLI writes, VieNeu-TTS speaks, yt-dlp lists channels; every one has a
  deterministic fake so the whole app runs offline with `start-app.bat --fake`.
- **Private by design.** Loopback only, no telemetry, no API keys stored or logged; sub-processes run with a scrubbed
  environment.
- **Well tested.** ~1,900 backend tests, ~140 frontend tests, and a release smoke that starts real server processes.

## Requirements

| Needed | Version / note |
|---|---|
| Windows 11 | the setup and start scripts are `.bat` files and everything has been tested on Windows 11 |
| Python | 3.11 or newer (tested with 3.13) |
| Node.js | 20 or newer (tested with 24) - builds the web UI |
| Git | fetches the pinned helper projects |
| [Claude Code CLI](https://claude.com/claude-code) | optional, **logged in** (`claude` once); writes story / canon / review with **your** Claude usage |
| [VieNeu-TTS](https://github.com/pnnbao97/VieNeu-TTS) checkout | optional; local Vietnamese speech synthesis (CPU is fine, GPU is faster) |
| yt-dlp | optional; only for channel / playlist links (installed by `setup.bat`, no API key) |

Without the optional pieces StoryFlow still starts: use `--fake` for a full offline demo.

## Quick start

```bat
git clone https://github.com/giaminhNguyen/StoryFlow.git
cd StoryFlow
setup.bat          REM one time: pinned sources, Python venv, frontend build, guided provider setup, health check
start-app.bat      REM starts the API + runtime + web UI and opens http://127.0.0.1:8765
```

* `setup.bat` is safe to re-run. It detects `claude` and VieNeu-TTS, lists the available voices, installs `yt-dlp` if you
  want channel links and writes `backend\.env` (your existing custom keys are kept, old files are backed up).
* `start-app.bat --fake` runs a deterministic offline demo (no model, no network). Stop with `Ctrl+C`.
* `start-app.bat --port 9000 --no-open` changes the port / does not open the browser.
* Check everything without changing anything: `setup.bat /check`, `start-app.bat /check`,
  `backend\.venv\Scripts\python -m storyflow doctor`.

## Using StoryFlow

### The web UI (`http://127.0.0.1:8765`)

The "local web" is a small React app served by the same process as the API - just open the address in a browser.

| Page | What you can do |
|---|---|
| **Workflows** | list every workflow with progress, status and health of the providers; create a workflow for **one** video (id, language, optional story branch, voice) |
| **Workflow detail** | start / pause / resume / retry / cancel; see each project's pipeline (source, canon, story, [review], tts, audio), skipped / needs-attention projects with the reason, add projects, assign runners and see capacity |
| **Project detail** | read the source and the new story, the review verdict and issues, play every audio chunk and the **full audio** file (with a download link) |

Batches (channels / playlists), presets and failure policies are configured through the API - see the next section.
Because the API is a plain local HTTP API, an AI assistant such as Claude Code can operate the whole thing for you.

### Process a whole channel (API)

```bash
API=http://127.0.0.1:8765/api

# 1. a workflow: Vietnamese subtitles, 'fast' preset, bad videos are skipped, at most 2 projects at a time
curl -s -X POST $API/workflows -H "Content-Type: application/json" -d '{
  "name": "my-channel",
  "config": {"source": {"languages": ["vi"]}, "preset": "fast", "batch": {"max_active": 2},
             "failure_policy": {"on_no_subtitle": "skip", "on_permanent_error": "continue"}}}'

# 2. the newest 3 videos of a channel (playlists, video links and "inbox:file.txt" work too)
curl -s -X POST $API/workflows/<workflow-id>/sources -H "Content-Type: application/json" -d '{
  "sources": ["https://www.youtube.com/@some-channel"], "limit": 3, "languages": ["vi"]}'

# 3. give the workflow its runners (Claude for text, VieNeu for audio), 4. start, 5. watch
curl -s $API/runners                                            # note the runner ids
curl -s -X POST $API/runners/<runner-id>/assign -H "Content-Type: application/json" -d '{"workflow_id": "<workflow-id>"}'
curl -s -X POST $API/workflows/<workflow-id>/start
curl -s $API/workflows/<workflow-id>                            # counts, per-project state, capacity
```

> On Windows, do not send non-ASCII JSON (for example a Vietnamese voice name) with `curl`: the text arrives mangled.
> Use a small Python / PowerShell script with UTF-8, or set the voice once in `backend\.env`.

| Endpoint | Purpose |
|---|---|
| `GET /api/health`, `GET /api/providers` | database revision, runtime state, readiness of subtitle / story / TTS providers |
| `POST /api/workflows`, `.../{id}/start`, `pause`, `resume`, `retry`, `cancel` | create and control a workflow |
| `POST /api/workflows/{id}/projects` | add one project by hand |
| `POST /api/workflows/{id}/sources`, `POST .../sync`, `GET .../feeds` | add videos / playlists / channels, re-scan for new uploads, list feeds |
| `POST /api/projects/{id}/retry` | bring a skipped / needs-attention project back |
| `GET /api/workflows/{id}`, `GET /api/projects/{id}` | full state, artifacts, review, audio (`audio.final_path`) |
| `GET /api/runners`, `POST /api/runners/{id}/assign` / `unassign` / `enable` / `disable` | runner pool |
| `GET /api/artifacts/{relative-path}` | stream a stored file (story, chunk, `final.wav`) |

### Presets, failure policy and concurrency

| Setting (workflow `config`) | What it does |
|---|---|
| `"preset": "fast" \| "balanced" \| "quality"` | `fast` = no review; `balanced` = one editor call records verdict + issues; `quality` = the editor also corrects the story (up to 2 rounds) |
| `"failure_policy"` | retries with backoff for blocked subtitle requests; `on_no_subtitle: skip` and `on_permanent_error: continue` end only the affected project; systemic problems (quota, login, runner down) always pause the workflow, and a circuit breaker stops a batch that fails the same way 3 times |
| `"batch": {"max_active": 2}` | only the first N unfinished projects run canon / story / review / tts / audio, so stages overlap; subtitles are still prefetched for every waiting project, one after another inside the runtime tick (not in parallel), so a very large batch can make the first tick long or trigger provider backoff |
| `"story": {"target_length": 9000}` | words; the default is "at least as long as the source" (capped at 15,000) |

Details, all fields and error codes are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

### What you get

For every video, under `runtime\artifacts\projects\<project-id>\`:

| Artifact | Path |
|---|---|
| Source transcript | `source\0001\source.txt` |
| Canon analysis | `canon\<id>\canon.json` |
| New story (versioned) | `story\<id>\story.md` (revisions from the `quality` preset: `review\<id>\story_revised.md`) |
| Speech text + chunk plan | `tts\<id>\` |
| Audio chunks and the **joined file** | `audio\<id>\run-001\0001.wav ...` and `final.wav` |

Your own subtitles work too: put `<video_id>.txt` (or `.srt` / `.vtt`) in `runtime\inbox\` and it is used instead of
asking YouTube - handy when YouTube blocks your IP.

## Configuration

`setup.bat` writes `backend\.env` (git-ignored) for you; edit it by hand any time. Real environment variables win.

| Setting | Default | Meaning |
|---|---|---|
| `STORYFLOW_SUBTITLE_PROVIDER` | `external` | `external` (pinned Subtitle_supperVip sub-process), `fake`, `none` |
| `STORYFLOW_STORY_RUNNER` | `none` | `claude-cli`, `fake`, `none` (no text is sent anywhere unless you choose one) |
| `STORYFLOW_CLAUDE_CLI`, `STORYFLOW_CLAUDE_MODEL`, `STORYFLOW_STORY_TIMEOUT` | PATH / default / `900` (setup uses `1800`) | Claude executable, model, seconds per story |
| `STORYFLOW_TTS_ENGINE` | `none` | `vieneu`, `fake`, `none` |
| `STORYFLOW_VIENEU_ROOT`, `_VOICE`, `_PRECISION`, `_THREADS` | - / `Ngọc Huyền` / `fp32` / `6` | your VieNeu-TTS checkout, preset voice, `fp32` or faster `int8`, CPU threads |
| `STORYFLOW_YTDLP_PYTHON`, `STORYFLOW_LISTER_TIMEOUT`, `STORYFLOW_INBOX_DIR` | venv / `120` / `runtime\inbox` | channel listing interpreter and timeout, folder for your own subtitle files |
| `STORYFLOW_AUDIO_MAX_MB`, `STORYFLOW_ARTIFACT_MAX_MB` | `4096` / `256` | largest audio / text file the API serves |
| `STORYFLOW_LOG_DIR`, `STORYFLOW_LOG_LEVEL` | `runtime\logs` / `info` | logging |

Credentials belong to the external tools (the `claude` login); StoryFlow never reads, stores or logs them.
The full list is documented at the top of [`backend/storyflow/providers.py`](backend/storyflow/providers.py).

## Everyday operations

```bat
scripts\backup.bat                                   REM verified backup of the database + artifacts (safe while running)
scripts\restore.bat --from runtime\backups\<dir>     REM stop the app first; add --force to replace existing data
backend\.venv\Scripts\python -m storyflow doctor     REM environment / provider health check
backend\.venv\Scripts\python -m storyflow migrate    REM apply database migrations (a verified backup is taken first)
```

Everything mutable lives in `runtime\` (database, artifacts, backups, logs, inbox) and is git-ignored. Troubleshooting,
backup / restore details and the migration policy are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## How it works

```text
 Browser UI (React) --+
 AI assistant / curl -+--> FastAPI (127.0.0.1:8765) --> services --> SQLite (WAL)
                                                                        ^
 runtime loop --> orchestrator (a restartable state machine over durable state)
                    |-- inline steps:  subtitle worker (sub-process), yt-dlp lister
                    `-- job steps ---> job queue --> dispatcher (runner allow-list, quota failover)
                                                       |-- Claude CLI runner  (canon, story, review)
                                                       `-- VieNeu-TTS runner  (speech text, audio, final.wav)
```

* A **workflow** holds many **projects** (one per video). Each project walks the steps `source -> canon -> story ->
  [review] -> tts -> audio`; every step is a durable row plus (for AI work) a queued job, so nothing depends on memory.
* The **orchestrator** re-derives "what is next" from the database on every tick; the **dispatcher** hands jobs to
  the runners of that workflow only, handles quota / rate limits and retries; failures are classified (transient,
  business, systemic) and handled by the workflow's failure policy.
* **Skills** do the creative work: `story-branch-writer` (alternate-branch story) and `story-tts-adapter` (speech
  text and chunking) from [Skills-Import](https://github.com/giaminhNguyen/Skills-Import); subtitles come from
  [Subtitle_supperVip](https://github.com/giaminhNguyen/Subtitle_supperVip). Both are pinned by commit in
  `sources.lock.json` and fetched by `bootstrap.py`.

Tech stack: Python 3.13, FastAPI, SQLAlchemy 2, Alembic, SQLite; React 19, Vite, TypeScript; pytest, Vitest.

### Repository layout

```text
setup.bat  start-app.bat        one-click setup / start
bootstrap.py, sources.lock.json pinned external sources (fetched into external/ and skills/)
backend/storyflow/              the engine
    api/                          FastAPI app, routes, artifact serving
    orchestrator.py pipeline.py   restartable state machine and step contracts
    story_steps.py review_steps.py tts_steps.py     source / canon / story / review / tts / audio steps
    sources.py source_service.py  channel / playlist / video ingestion, ledger, inbox
    policy.py presets.py          failure policy, batch window, presets
    dispatcher.py queue.py agents.py   job queue, runner selection, failover
    integrations/                 Claude CLI, VieNeu-TTS, subtitle and yt-dlp sub-process adapters
    readmodels.py services.py     query side and the only mutation boundary of the API
    ops/                          doctor, backup, restore, migrate
backend/alembic/versions/       schema migrations (0001 ... 0009)
backend/tests/                  ~1,900 tests
frontend/src/                   web UI (pages, components, tests)
scripts/                        setup wizard, backup / restore, release smoke test
docs/                           OPERATIONS.md (operator guide), ENGINEERING_HISTORY.md
runtime/  external/  skills/    generated on your machine (git-ignored)
```

## Development and tests

```bat
cd backend  && .venv\Scripts\python -m pytest -q                        REM backend suite (about 6 minutes)
cd frontend && npm run typecheck && npm test && npm run build          REM frontend
python scripts\release_smoke.py --skip-backend-tests                    REM starts real servers on a temporary workspace
```

`release_smoke.py` runs 13 steps (bootstrap, migrations, end-to-end flows, pause / retry / cancel, restart recovery,
isolation, artifact path safety, backup / restore, graceful shutdown and an offline channel batch); the real-provider
step is opt-in with `STORYFLOW_RUN_REAL_SMOKE=1`.

## Privacy, safety and known limits

- **Loopback only, no authentication.** Never expose the port to a network. There is no telemetry; secrets are never
  stored or logged; artifact serving refuses anything outside the artifact tree.
- **It spends your resources.** Real story runs use your Claude usage (a 30-minute video is roughly a 9,000-word
  story), and speech synthesis on CPU takes tens of minutes per story.
- **YouTube may block subtitle requests from some IPs.** StoryFlow backs off and retries; the inbox folder lets you supply
  subtitles yourself.
- **Respect the source.** The stories are new alternate branches written by an AI from a transcript; make sure you have
  the right to use the videos you feed in and to publish what you produce.
- **Windows-first.** The scripts target Windows 11 and that is the only platform it has been tested on; the backend and UI are ordinary Python / Node.
- **Not built yet:** analysis by several agents in parallel, a parallel audio worker pool and agents other than Claude
  (see the roadmaps).
- **License:** no license file has been added yet - add one before you reuse or redistribute the code.

## More documentation

- [docs/OPERATIONS.md](docs/OPERATIONS.md) - installing, configuring, running, backing up, migrating, troubleshooting,
  presets / failure policy / batches in depth.
- [docs/ENGINEERING_HISTORY.md](docs/ENGINEERING_HISTORY.md) - how each phase was designed and verified.
- [STORYFLOW_MASTER_ROADMAP.md](STORYFLOW_MASTER_ROADMAP.md), [AUTONOMOUS_ROADMAP.md](AUTONOMOUS_ROADMAP.md) - the plans this
  project was built from.
