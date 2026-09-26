# StoryFlow - engineering history

> These notes were written phase by phase while StoryFlow was built (Phase 0-9 and the later batch roadmap P0-P5).
> They are kept as a **historical record**: some module lists and file trees describe how things looked at that
> moment. For the current overview read the [README](../README.md), for running and configuring it read
> [OPERATIONS.md](OPERATIONS.md). The planning documents are [STORYFLOW_MASTER_ROADMAP.md](../STORYFLOW_MASTER_ROADMAP.md)
> and [AUTONOMOUS_ROADMAP.md](../AUTONOMOUS_ROADMAP.md).

## Current Status

**Phase 0–9 COMPLETE.**

**PHASE 9: PASS — STORYFLOW LOCAL MVP COMPLETE.**

Current Status: Phase 0–9 COMPLETE plus the post-MVP batch roadmap P0–P5 (below) · Migration head `0009_feed_min_duration` · Backend tests `1933 passed, 7 skipped` (the skips are the opt-in real smokes and a Windows symlink test) · Frontend: typecheck clean, `144 passed` unit tests, `vite build` OK · `python scripts/release_smoke.py`: 10/10 executable steps PASS, including the new step 13 (a whole offline channel: dedupe, skip, window, review + revision, one joined `final.wav`, retry, sync); real-provider step 12 is opt-in.

Supported real providers: local `claude` CLI (story/canon), local VieNeu-TTS (synthesis), pinned Subtitle_supperVip via subprocess (needs `requirements-subtitle.txt`). Known external limitations: YouTube blocks subtitle fetches from some IPs; no authentication (loopback-only by design); Ctrl+C in `start-app.bat` shows cmd's "Terminate batch job (Y/N)?" prompt.

Current migration head:

```text
0001_storyflow_initial
→ 0002_runner_dispatch
→ 0003_story_domain
→ 0004_pipeline_dedupe
→ 0005_control_plane
→ 0006_source_policy
→ 0007_source_feeds
→ 0008_story_reviews
→ 0009_feed_min_duration
```

### Post-MVP roadmap progress (batch operation)

| Step | Status |
|---|---|
| P0 story length >= source, single `final.wav`, one-click setup | done |
| P1 failure policy: subtitle backoff/retry limit, `skipped` / `needs_attention` projects, batch keeps going (migration `0006`) | done - see `docs/OPERATIONS.md` "Failure policy" |
| P2 channel / playlist / video-list / local-file input, processed-video ledger, sync of new videos, subtitle inbox (migration `0007`, `POST /api/workflows/{id}/sources`, `/sync`, `GET .../feeds`) | done - see `docs/OPERATIONS.md` "Adding videos, playlists and channels" |
| P3 batch orchestration: `batch.max_active` window (overlap without flooding YouTube), a failed AI/TTS step ends only that project (`on_permanent_error: continue`), `POST /api/projects/{id}/retry` | done - see `docs/OPERATIONS.md` "How many projects run at once" |
| P4 review + revision and presets: `"preset": "fast"` (default) / `"balanced"` (an editor call records verdict + issues) / `"quality"` (the editor also returns a corrected story, up to 2 rounds) (migration `0008`) | done - see `docs/OPERATIONS.md` "Presets" |
| P5 hardening: three independent code reviews found real bugs, all fixed - a `continue` policy that finalize-time failures bypassed, systemic failures (quota, login, runner down) that could burn a whole batch (now they pause + a circuit breaker), unavailable videos that paused a batch, partial `failure_policy` losing backoff, `final.wav` over 256 MB refused by the API (audio cap now 4 GiB) and built in memory (now streamed), revisions that could shrink or truncate a story, stranded projects on finish/cancel races, unbounded source ingestion; orchestrator ~3x faster with a durable `completed` project status; `final_path` in the API/UI; migrations `0009` | done |

Phase 9 validation: backend `684 passed, 6 skipped` on three independent runs (Phase 8: 541, Phase 7: 422, Phase 6: 420, Phase 5: 333, Phase 4: 198). Skips are symlink tests on Windows without symlink privilege.

Phase 3 closure validation:

```text
104 passed, 1 skipped
104 passed, 1 skipped
104 passed, 1 skipped
```

The skipped test is the symlink-path test on Windows when symlink privileges are unavailable.

---

## Phase 0 — Bootstrap

Phase 0 manages pinned external dependencies.

`bootstrap.py` is responsible for cloning/fetching the revisions declared in:

```text
sources.lock.json
```

Generated directories:

```text
external/
skills/
runtime/
```

are not StoryFlow source and are gitignored.

Bootstrap commands:

```bat
python bootstrap.py
python bootstrap.py --check
start-app.bat
```

Current pinned integrations include:

* `Subtitle_supperVip`
* `story-branch-writer`
* `story-tts-adapter`

Bootstrap is idempotent:

* correct revisions are not downloaded again;
* incorrect revisions are reset to the pinned revision;
* `--check` performs verification without downloading;
* failures are reported explicitly.

---

## Phase 1 — Durable Backend

Phase 1 introduced the persistent workflow/job layer.

Core capabilities:

* SQLite + SQLAlchemy;
* Alembic migrations;
* workflow sessions;
* pipeline jobs;
* atomic job claim;
* worker ownership;
* claim tokens;
* leases;
* stale-job recovery;
* active-job deduplication;
* deterministic time support for tests.

### Concurrency model

Ownership-changing queue operations use a single-writer transaction.

A job may only be finalized by the exact:

```text
(worker_id, claim_token)
```

that claimed it.

A partial unique index guarantees that only one active job exists for a given `dedupe_key`.

Stale recovery only recovers processing jobs whose lease has actually expired.

`waiting_capacity` is not treated as a business failure.

---

## Phase 2 — Runner Abstraction and Dispatcher

Phase 2 introduced the execution protocol between StoryFlow and execution agents.

Core abstractions:

```text
AgentRunner
TaskPacket
RunnerResult
ResultCode
RunnerHealth
RunnerRegistry
```

Supported StoryFlow roles currently include:

```text
story_writer
tts_adapter
reviewer
general_worker
```

Roles belong to StoryFlow and are independent from the underlying runner implementation.

### Runner allow-list

The dispatcher may only use runners belonging to the job's workflow session.

A candidate runner must satisfy:

```text
workflow-session membership
∩ supported role
∩ enabled
∩ registered
∩ currently eligible state
∩ available concurrency
```

Candidate ranking is deterministic:

```text
role preference
→ normalized load
→ least recently used
→ runner type/id
```

### Capacity semantics

Claiming a job with a runner creates an OPEN `RunnerAttempt`.

The runner's `active_count` is incremented when execution begins.

Capacity is released by idempotently closing the corresponding attempt.

`active_count` is never allowed to become negative.

### Failure counters

`PipelineJob.attempts` counts business failures only:

```text
task_failed
invalid_output
```

`execution_count` records every dispatch attempt for observability.

Infrastructure failures are tracked separately:

```text
crash
timeout
transient error
lease expiry
```

Quota, rate-limit and authentication outcomes do not increment business-failure counters.

### Quota and rate-limit handling

Quota exhaustion places the runner into:

```text
quota_exhausted
```

with a reset time.

Rate limiting places the runner into cooldown using `cooldown_until` / `retry_after`.

The runner slot is released and the job may fail over to another eligible runner.

### No eligible runner

Workflow policy controls the behavior.

`pause_auto_resume`:

```text
job → waiting_capacity
```

The job can resume when capacity becomes available.

`require_attention`:

```text
job → ALL_AGENTS_UNAVAILABLE
```

### Fake runner

Phase 2 includes a deterministic `FakeRunner` for tests.

It can simulate:

* success;
* quota exhaustion;
* rate limiting;
* crash;
* timeout;
* invalid output.

No Claude/Codex/OpenCode business implementation is embedded in the runner protocol.

---

## Phase 3 — Story Pipeline Foundations

Phase 3 added the domain and integration boundaries required for higher-level story orchestration.

It contains three workstreams:

1. Story Domain
2. Subtitle Integration
3. AionUI Gateway

---

## Phase 3.1 — Story Domain

StoryFlow now contains persistent domain models for the story production lifecycle:

```text
ChannelWorkflow
StoryProject
SourceSnapshot
CanonAnalysis
StoryGeneration
StoryVersion
TTSGeneration
AudioGeneration
AudioChunk
```

Relevant domain enums and status fields are defined alongside the models.

### Versioning and uniqueness

Partial unique indexes enforce active-version semantics.

Examples include:

* snapshot number per story project;
* story version number per story project;
* canon analysis per source snapshot;
* audio generation run number;
* audio chunk index.

Superseded or abandoned records release their active uniqueness slot where appropriate.

Projects remain isolated from one another.

### Artifact store

StoryFlow provides an artifact-store abstraction with:

* temporary writes;
* filesystem flush before promotion;
* atomic promotion using `os.replace`;
* relative artifact paths stored in the database;
* absolute-path rejection;
* `..` traversal rejection;
* root-escape protection;
* symlink escape protection where supported by the platform.

The database does not depend on machine-specific absolute artifact paths.

### Migration

Phase 3 introduced:

```text
0003_story_domain
```

The migration chain is:

```text
0001_storyflow_initial
→ 0002_runner_dispatch
→ 0003_story_domain
```

Clean-database validation has verified:

```text
upgrade
→ downgrade to base
→ upgrade again
```

---

## Phase 3.2 — Subtitle Integration

StoryFlow integrates with the pinned `external/subtitle_suppervip` project through a clean abstraction.

Main interfaces:

```text
SubtitleClient
FakeSubtitleClient
ExternalSubtitleClient
```

The external repository remains authoritative for its own:

* source;
* migrations;
* tests;
* subtitle-provider behavior.

StoryFlow does not read the external project's SQLite database directly when an appropriate public API exists.

The real integration uses the upstream in-process public subtitle API where possible.

The fake implementation is deterministic and is used for StoryFlow tests.

### Known external subtitle gaps

The current external subtitle API does not provide every capability StoryFlow may eventually need.

Documented gaps include:

* no REST endpoint returning subtitle snippets directly;
* no API for selecting an exact track purely by requested track code;
* no API for reading previously stored subtitle content;
* subtitle preference persistence is not available at every desired scope;
* StoryFlow's backend environment does not yet automatically wire all external subtitle dependencies.

These are external/integration gaps and do not block the Phase 3 completion gate.

---

## Phase 3.3 — AionUI Gateway

StoryFlow provides a gateway boundary between its runner infrastructure and AionUI-style execution backends.

Core abstractions include:

```text
TaskGateway
TaskHandle
TaskStatus
GatewayError
DetectedRunner
GatewayAgentRunner
FakeAionGateway
```

The gateway supports the StoryFlow-side contract for:

```text
detect runners
health
submit/start task
poll
wait
cancel
error classification
```

### Phase 2 integration

Gateway execution uses the existing Phase 2 protocol:

```text
TaskPacket
→ gateway
→ RunnerResult
→ ResultCode
```

`GatewayAgentRunner` implements the `AgentRunner` seam required by the dispatcher.

This means gateway-backed runners still pass through StoryFlow's normal:

* workflow/session allow-list;
* role filtering;
* runner eligibility;
* capacity handling;
* dispatcher behavior.

The gateway does not bypass the Phase 2 scheduler.

Roles remain owned by StoryFlow.

AionUI does not decide StoryFlow's story/TTS roles.

### Error mapping

Gateway errors are classified into StoryFlow result types for conditions such as:

```text
authentication
quota
rate limit
timeout
unavailable
schema/protocol error
```

### Fake gateway

`FakeAionGateway` provides deterministic task execution for tests.

It allows Phase 4 orchestration to be developed without depending on a stable real AionUI task API.

### Known AionUI gap

The currently audited AionUI surface does not expose a stable task-run contract equivalent to:

```text
submit
→ task id
→ poll
→ final result
```

with the required cancellation semantics.

Therefore the real gateway remains behind the StoryFlow abstraction until the external API exposes the required capabilities.

This external gap does not block Phase 4 development because the gateway interface, runner bridge and deterministic fake implementation are already available.

---

## Backend Structure

```text
backend/
  storyflow/
    database.py
    models.py
    queue.py
    roles.py
    protocol.py
    agents.py
    dispatcher.py
    runners.py
    config.py
    artifacts.py
    subtitles.py
    gateway.py

  alembic/
    versions/
      0001_storyflow_initial.py
      0002_runner_dispatch.py
      0003_story_domain.py

  tests/
    ...
```

Important responsibilities:

```text
database.py
  database engine/session configuration

models.py
  workflow, runner, job and story-domain persistence

queue.py
  enqueue / claim / renew / complete / fail / recover

roles.py
  StoryFlow role definitions

protocol.py
  TaskPacket / RunnerResult / ResultCode / RunnerHealth

agents.py
  AgentRunner abstraction and registry

dispatcher.py
  runner selection, allow-list, failover and capacity behavior

runners.py
  runner-state helpers

artifacts.py
  artifact staging, promotion and safe path resolution

subtitles.py
  subtitle integration boundary

gateway.py
  task gateway and AgentRunner bridge

config.py
  StoryFlow backend settings
```

---

## Repository Layout

```text
backend/                  StoryFlow backend
frontend/                 frontend project placeholder
config/                   configuration placeholder
scripts/                  bootstrap/tooling/smoke tests
runtime/                  generated runtime state (gitignored)
bootstrap.py              pinned dependency bootstrap
sources.lock.json         upstream revision source of truth
start-app.bat             bootstrap/start entry point
external/                 generated external clones (gitignored)
skills/                   generated skill copies (gitignored)
```

`external/`, `skills/` and `runtime/` are generated resources and are not StoryFlow source.

---

## Running Backend Tests

Windows:

```bat
cd backend
.venv\Scripts\python.exe -m pytest tests\ -q
```

Run migrations:

```bat
cd backend
.venv\Scripts\alembic.exe upgrade head
```

Current migration head:

```text
0003_story_domain
```

---

## Bootstrap Smoke Tests

```bat
python scripts\test_bootstrap.py
```

The bootstrap smoke tests use a temporary root and cover:

* fresh bootstrap;
* idempotent rerun;
* `--check`;
* missing Git;
* invalid pinned revision.

---

## Pinned Revisions

`sources.lock.json` is the source of truth for external revisions.

To update an upstream dependency:

1. update its revision in `sources.lock.json`;
2. run `bootstrap.py`;
3. verify with:

```bat
python bootstrap.py --check
```

Do not edit generated external or skill copies as if they were StoryFlow source.

---

## Development Baseline

Before starting a new phase:

1. read the current README and repository state;
2. inspect the latest migrations;
3. run the complete backend test suite;
4. preserve workflow/session runner allow-list semantics;
5. keep StoryFlow roles independent from external runner implementations;
6. do not bypass public integration boundaries without an explicit architectural decision;
7. document external capability gaps instead of hiding them;
8. update this README when a phase passes its completion gate.

Current baseline:

```text
Phase 0  COMPLETE
Phase 1  COMPLETE
Phase 2  COMPLETE
Phase 3  COMPLETE

NEXT: Phase 4
```


---

## Phase 4 — End-to-End Workflow Orchestration

```text
ChannelWorkflow → StoryProject → source (SubtitleClient) → SourceSnapshot
→ canon → CanonAnalysis → story → StoryGeneration/StoryVersion
→ tts → TTSGeneration → audio → AudioGeneration/AudioChunk[] → workflow finished
```

* `storyflow/pipeline.py` — shared step contract (`StepHandler`, `InlineStepHandler`, `JobSpec`, `OutputValidatingRunner`).
* `storyflow/orchestrator.py` — derives workflow position only from DB state, enqueues `PipelineJob`s (stable dedupe keys), drives the Phase 2 `Dispatcher`, finalizes finished jobs. Restart-safe; never calls runners directly.
* `storyflow/story_steps.py`, `storyflow/tts_steps.py` — step handlers, deterministic fake runners, output validators.
* Invalid runner output is caught inside the dispatcher path (`INVALID_OUTPUT` = business failure). Quota/rate/crash/timeout/waiting_capacity keep Phase 2 semantics.
* Migration `0004_pipeline_dedupe` adds partial unique indexes on `story_generations` and `tts_generations` (one live generation per input) as the concurrency backstop.

Documented external gaps: no production AI runner (AionUI has no stable task-run API, see `gateway.py`), no real TTS synthesis backend, `ExternalSubtitleClient` needs the upstream backend deps. The fakes are the executable contract. `AudioGeneration` has no `pipeline_job_id`; its job is found by dedupe key `audio:<id>`. A failed step never auto-retries: use `Orchestrator.resume` / `retry_failed_step`.


---

## Phase 5 — Control Plane & Operational Runtime

Backend-only (no HTTP/UI yet). Future API/UI must go through these boundaries and never touch ORM rows or enqueue `PipelineJob`s directly.

* `storyflow/services.py` — `WorkflowService` (create / add_project / start / pause / resume / retry / cancel) and `RunnerService` (assign / unassign / enable). Every status change is a guarded compare-and-set; commands are idempotent (`changed=False` no-op) and concurrency-safe. `client_key` gives DB-enforced create idempotency. Cancel is terminal: pending jobs are cancelled, open domain rows marked `cancelled`, history is kept, and a late runner result cannot resurrect the workflow. Errors are `storyflow.errors` types with stable `code`.
* `storyflow/readmodels.py` — read-only typed snapshots (`WorkflowSnapshot`, `ProjectSnapshot`, `RunnerSnapshot`, `to_jsonable`). Current step/state is derived from the DB; relative artifact paths only; no claim tokens/paths/secrets. Business vs infrastructure vs capacity vs provider failures are separated; `chunks_missing` surfaces as `blocked`.
* `storyflow/runtime/` — `Runtime.run_once()` / `run_forever()` (bounded work, backoff, graceful stop, per-workflow error isolation), `RunnerSupervisor` (discovery upserts runners **unassigned**; health refresh preserves quota/cooldown rules), `build_runtime()` wiring, and a stdlib CLI: `python -m storyflow.runtime --once|--run [--fake]`.
* Migration `0005_control_plane`: `channel_workflows.status_reason/status_detail/client_key`, `runner_instances.external_id` (all additive/nullable). New workflow statuses `draft`, `cancelled`.
* Fixes found by the two-runtime scenario: dispatcher treats a lost claim race as `LOST_RACE` (not an exception); `ArtifactStore.write` tolerates concurrent identical writers on Windows; `.gitignore` generated-dir patterns anchored to the repo root so `backend/storyflow/runtime/` is tracked.

Remaining documented gaps: no production AI runner / TTS engine (Phase 8), `list_workflows` display_state is an aggregate approximation of `get_workflow`, runtime fairness counter is in-memory (ordering only, not correctness).


---

## Phase 6 — Local HTTP API

FastAPI + Uvicorn (pinned in `backend/requirements.txt`; `httpx` in `requirements-dev.txt` for the test client). The API is a thin adapter over the Phase 5 boundary: handlers call only `WorkflowService` / `RunnerService` / `ReadModels` (no ORM, queue, dispatcher or runner access) and share **one** orchestrator with the operational runtime.

```bat
cd backend
.venv\Scripts\python -m storyflow.api --fake            REM loopback 127.0.0.1:8765, embedded runtime, deterministic fakes
.venv\Scripts\python -m storyflow.api --no-runtime      REM API only (run `python -m storyflow.runtime --run` separately)
```

* Default bind is `127.0.0.1`; a non-loopback host is refused (exit 2) unless `--allow-non-loopback` (logs a no-auth warning). No accounts/auth in this phase. Host-header guard (DNS-rebinding), CORS limited to `http://localhost|127.0.0.1[:port]`, `nosniff` everywhere, request bodies capped at 1 MiB (413), `/docs` disabled.
* Endpoints (`/api`): `GET health`; `GET|POST workflows`, `GET workflows/{id}`, `POST workflows/{id}/start|pause|resume|retry|cancel`, `POST workflows/{id}/projects`, `GET projects/{id}`; `GET runners[/{id}]`, `POST runners/{id}/assign|unassign|enable|disable`; `GET|HEAD artifacts/{relative path}`.
* Error contract: `{"error": {"code", "message", "details"}}` with codes `validation` 422, `not_found` 404, `conflict` 409, `invalid_state` 409, `not_retryable` 409, `capacity_unavailable` 503, `internal` 500 (no tracebacks/paths/input echo).
* Idempotency is durable, never an in-memory cache: `POST /workflows` uses `client_key` / `Idempotency-Key` (DB unique index), projects use `slug`, lifecycle commands are idempotent no-ops.
* Artifacts: only store-relative paths under `projects/`, strict segment grammar + extension allow-list, resolved through `ArtifactStore`; every refusal is the same 404 (layout cannot be probed); fixed content types, ETag/304, Range (audio), size cap.
* Secret-looking config keys (`secret|token|password|api_key|credential`) are rejected so credentials can never be persisted through the API.
* Module map: `storyflow/api/{app,routes,schemas,host,errors,artifacts,__main__}.py`.

Remaining documented gaps: no authentication (loopback-only by design), no WebSocket (polling is enough for the MVP), real AI/TTS providers are Phase 8.


---

## Phase 7 — Frontend MVP

React 19 + TypeScript + Vite + Vitest/Testing Library in `frontend/` (versions pinned exactly, `package-lock.json` committed). The UI talks **only** to the Phase 6 API (`src/api/client.ts`); it never reads SQLite or backend paths. Story text is rendered as plain text, never HTML.

```bat
REM terminal 1 - API + embedded runtime with deterministic fakes (demo video id "demo-video")
cd backend
.venv\Scripts\python -m storyflow.api --fake --database-url sqlite:///runtime/demo.db --artifact-root runtime/demo-artifacts

REM terminal 2 - dev UI at http://127.0.0.1:5173 (Vite proxies /api to 127.0.0.1:8765; override with STORYFLOW_API_URL)
cd frontend
npm install
npm run dev
```

* Screens: workflow list (state badge, `completed/total` progress, needs-attention flag, idempotent create form prefilled from `/api/health` demo), workflow detail (lifecycle controls computed by `availableActions`, invalid actions disabled with reasons, inline cancel confirmation, add project, per-project pipeline steps, failure vs capacity vs provider vs blocked panels, runner panel where detected runners stay unused until explicitly assigned), project detail (source/canon/story/TTS/audio, story viewer, per-chunk `<audio>` via safe artifact URLs).
* All 8 states are distinguishable by text and colour: draft, active, paused, waiting capacity, failed, blocked, cancelled, completed.
* Refresh model: bounded polling (`usePolling`): one request at a time, backoff up to 15 s while the backend is unreachable, paused while the tab is hidden, aborted on unmount; a recoverable "backend unreachable" banner keeps the last good data visible.
* Scripts: `npm run typecheck`, `npm test` (unit, jsdom), `npm run build`, `npm run test:e2e` (spawns the real `python -m storyflow.api --fake` from the backend venv, then drives the real `<App>`: create → add project → assign runner → start → completed → story text → audio chunk `audio/wav` with RIFF header; traversal → 404).
* Backend additions in this phase: `python -m storyflow.api --fake` wires an offline demo subtitle source and `/api/health` reports `demo.video_id`; `WorkflowSummary.completed_projects` for list progress.

Remaining documented gaps: no browser-automation E2E (jsdom + real backend instead), no auth, real providers are Phase 8, the production build is not yet served by the backend (Phase 9).


---

## Phase 8 — Production Integration Readiness

Real providers sit behind the existing abstractions (`SubtitleClient`, `AgentRunner`/`RunnerProvider`), are **opt-in** and are dispatched through the normal `Dispatcher` with the session allow-list. Deterministic fakes stay the default test path.

| Capability | Real backend | Boundary | Select with |
|---|---|---|---|
| Subtitles | pinned `Subtitle_supperVip` public API (`available_transcripts` / `fetch_selected`) | subprocess with hard timeout (`integrations/subtitle_subprocess.py`, `subtitle_worker.py`); upstream never imported in-process, no upstream DB access, no writes into `external/` | `STORYFLOW_SUBTITLE_PROVIDER=external` (default) |
| Story / canon | local `claude` CLI, non-interactive (`claude -p --output-format json --no-session-persistence --tools ""`), empty temp cwd, no `STORYFLOW_*` env, prompt on stdin | `integrations/claude_cli.py` (`ClaudeCliRunner`, `ClaudeCliProvider`) | `STORYFLOW_STORY_RUNNER=claude-cli` |
| TTS synthesis | locally installed VieNeu-TTS v3 turbo (own venv) | subprocess worker (`integrations/vieneu.py`, `vieneu_worker.py`); adaptation is rule-based per the pinned profile | `STORYFLOW_TTS_ENGINE=vieneu` + `STORYFLOW_VIENEU_ROOT` |

* Configuration: `storyflow/providers.py` reads `STORYFLOW_*` environment variables or a git-ignored `backend/.env` (see `backend/.env.example`, placeholders only). Story and TTS default to **none**: nothing is sent to any model and no model runs unless selected. Credentials belong to the external tools (the `claude` CLI login); StoryFlow never reads, stores or logs them.
* Readiness: `GET /api/providers` (ready / unavailable / misconfigured / disabled / fake, path-free messages) and startup logs; the UI shows a header chip + Providers panel (fakes are labelled "Demo (fake)", never "ready").
* Errors are classified into the Phase 2 semantics: quota / rate limit / auth / timeout / crash / transient network map to infrastructure results, invalid output to business failure; upstream network errors from the subtitle worker are transient.
* Install the subtitle dependencies once: `pip install -r backend/requirements-subtitle.txt` (in the interpreter named by `STORYFLOW_SUBTITLE_PYTHON`, default the backend venv); until then the subtitle provider reports `unavailable` with that hint.
* Opt-in real smoke tests (never run by the normal suite): `STORYFLOW_RUN_REAL_SMOKE=1` plus `STORYFLOW_STORY_RUNNER=claude-cli` / `STORYFLOW_TTS_ENGINE=vieneu STORYFLOW_VIENEU_ROOT=<path>` and `pytest backend/tests/smoke_real`.

Verified on this machine (2026-09-25): real `claude` story smoke passed (canon 15 s + story 7 s, validators OK); real VieNeu smoke passed (2 chunks, 48 kHz mono PCM16); full HTTP flow with `claude-cli` + `vieneu` + demo subtitles completed in 77 s (164-word story, 5 audio chunks served as `audio/wav`).

Known external limitations: the real YouTube subtitle fetch was **blocked by the provider from this machine's IP** (smoke skipped as "provider blocked"; wiring itself is verified against the real upstream import and a fake upstream); `claude` runs consume the operator's own Claude usage; very long sources are sent in one prompt (no chunking); TTS temperature/gap are constants; no live TTS progress channel.


---

## Phase 9 — Release Hardening & Local MVP

No new product features: the system is now reproducibly installable, startable, stoppable, diagnosable and recoverable. Operator guide: [docs/OPERATIONS.md](docs/OPERATIONS.md).

* **Entry points:** `scripts\setup.bat` (idempotent setup, `/check` mode), `start-app.bat` (bootstrap pin check → venv check → `doctor --quick` → safe `migrate` → `python -m storyflow.api --open-browser`; `/check` validates without starting; Ctrl+C stops cleanly), `scriptsackup.bat`, `scripts
estore.bat`.
* **Serving:** the production-built frontend (`frontend/dist`) is served by the backend on `http://127.0.0.1:8765/`; `/api/*` keeps the JSON error contract; hashed assets are immutable-cached; everything is `nosniff`.
* **Operations CLI** (`python -m storyflow`): `doctor` (prerequisites/DB/artifacts/providers/port/frontend, PASS/WARN/FAIL with fixes, `--json`), `backup` (SQLite online backup, then artifacts, manifest with sha256; never overwrites), `restore` (verifies hashes/integrity/revision, refuses in-use or non-empty targets, `--force` keeps `<target>.pre-restore-<ts>`, atomic staged swap), `migrate` (older DB ⇒ verified backup first, then upgrade; newer/unknown/foreign DBs refused; never destructive).
* **Logging:** rotating `runtime/logs/storyflow.log`; secrets/paths redacted, messages truncated, uvicorn query strings stripped; workflow/runner/transition events logged as ids only (no story/source payloads, no switch to enable them).
* **Fixes found by the final matrix:** `ArtifactStore.resolve` raised a spurious `PathTraversalError` on Windows when another runtime was creating/replacing the same artifact (containment is now checked component by component instead of `Path.resolve()` on the whole target; deterministic regression test added); `retry(project_id)` for an inline source failure now answers coherently (no 409 + silent reactivation); `alembic/env.py` disposes its engine.
* **Regression matrix:** `python scripts/release_smoke.py` runs 12 steps on a clean temporary workspace (bootstrap, migration, backend tests, frontend, fake end-to-end through the real server, pause/resume/retry/cancel, restart/resume, two-runtime isolation, artifact safety, online backup + restore into a second workspace, graceful shutdown via CTRL_BREAK, opt-in real providers).
