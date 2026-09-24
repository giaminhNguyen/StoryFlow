# StoryFlow

StoryFlow is a local workflow orchestration system for building AI-assisted story production pipelines.

The project currently provides:

* durable workflow/job state backed by SQLite;
* concurrency-safe job claiming and recovery;
* runner abstraction and deterministic dispatch;
* workflow/session runner allow-lists;
* story-domain persistence and artifact versioning;
* subtitle integration abstraction;
* AionUI task gateway abstraction;
* deterministic fake implementations for integration testing.

## Current Status

**Phase 0–3 complete.**

**Phase 3 closure: PASS — READY FOR PHASE 4.**

Phase 4 has not started yet.

Current migration head:

```text
0001_storyflow_initial
→ 0002_runner_dispatch
→ 0003_story_domain
```

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
