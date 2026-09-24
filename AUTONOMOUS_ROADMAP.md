# StoryFlow — Autonomous Roadmap (Phase 5 → Final MVP)

> Master execution file for continuing StoryFlow without waiting for manual confirmation between phases.
>
> Workspace: `E:\OTHER\StoryFlow`
>
> Baseline before this roadmap: Phase 0–4 complete, Phase 4 closure reported `198 passed, 1 skipped ×3`, migration head `0004_pipeline_dedupe`.

---

# 0. Mission

Continue StoryFlow from Phase 5 through the final local MVP in strict phase order.

For every phase:

1. Read the current repository, README, migrations, tests, and the previous phase report.
2. Implement only the current phase.
3. Run the current phase completion gate.
4. If the gate FAILS, fix only issues that belong to the current phase and run the gate again.
5. Do **not** commit or push a failed phase.
6. When the gate PASSES:
   - update `README.md` so repository documentation matches reality;
   - update this roadmap's progress section if this file is stored in the repository;
   - review the full diff;
   - verify `skills/` and `external/` are unchanged unless the phase explicitly authorizes a pinned dependency update;
   - commit the complete phase;
   - push to `origin/master`;
   - verify remote HEAD matches the local commit;
   - automatically continue to the next phase.
7. Do not ask for confirmation merely because a phase passed.
8. Stop only on a real blocker defined in **Stop Conditions**.

Do not skip phases.

---

# 1. Repository Is Authoritative

The working repository is authoritative for what has actually been implemented.

Before each phase:

```bat
cd /d E:\OTHER\StoryFlow
git status
git branch --show-current
git fetch origin
git log -5 --oneline
python bootstrap.py --check
```

Required conditions before modifying a new phase:

- branch is `master`;
- working tree is clean, except the roadmap file itself if intentionally being updated;
- local branch is not behind/diverged from `origin/master`;
- bootstrap pins are healthy;
- previous phase commit exists locally.

If remote has unrelated new commits and the branch diverged, STOP. Do not force-push, reset, or overwrite remote history.

Never use:

```text
git push --force
git reset --hard
```

unless explicitly instructed by the human owner in a later conversation.

---

# 2. Global Architecture Invariants

Preserve all invariants established in Phase 1–4.

## Durable state

SQLite/SQLAlchemy state is the source of truth.

Do not introduce required in-memory workflow state for:

- current step;
- retry state;
- workflow ownership;
- runner ownership;
- completion state.

A new process must be able to reconstruct state from persisted data.

## Queue

`PipelineJob` remains the durable work queue.

Do not create a second queue as the source of truth.

## Runner execution

AI/TTS execution remains:

```text
TaskPacket
→ Dispatcher
→ AgentRunner
→ RunnerResult
```

or:

```text
TaskPacket
→ Dispatcher
→ GatewayAgentRunner
→ TaskGateway
→ RunnerResult
```

Application services, API handlers, runtime code, and frontend code must not bypass the dispatcher to submit runner work directly.

## Session isolation

Preserve the workflow-session runner allow-list.

A runner assigned to session B must never execute work belonging to session A.

## Transactions

Use the established pattern:

```text
short DB transaction
→ external work / filesystem / network / idle
→ short guarded DB transaction
```

Do not hold SQLite write transactions while waiting for external services or sleeping.

## Artifact safety

Use the Phase 3 ArtifactStore semantics:

- temporary write;
- atomic promotion;
- relative DB paths;
- traversal/root escape protection;
- no arbitrary absolute paths returned to callers.

## External repositories

Do not edit generated or pinned upstream code in:

```text
skills/
external/
```

unless a later phase explicitly requires changing `sources.lock.json` and the change is justified in the phase report.

Prefer StoryFlow-side adapters.

---

# 3. Autonomous Commit / Push Policy

A phase may be committed only after its completion gate passes.

Before commit:

```bat
git status
git diff --check
git diff --stat
```

Run the entire required test suite for that phase before commit.

Update `README.md` with:

- completed phase;
- migration head;
- latest full-suite result;
- new major modules;
- remaining documented external gaps;
- next phase.

Use one phase commit unless there is a strong reason to separate code and docs.

Recommended messages:

```text
phase 5: backend control plane and runtime
phase 6: local api
phase 7: frontend mvp
phase 8: production integrations
phase 9: release hardening
```

Then:

```bat
git add <explicit phase files> README.md AUTONOMOUS_ROADMAP.md
git commit -m "phase N: ..."
git push origin master
git fetch origin
git rev-parse HEAD
git rev-parse origin/master
```

The last two hashes must match.

Do not use `git add .` if unrelated files exist.

After successful push, automatically begin the next phase.

---

# 4. Stop Conditions

Do not wait for routine confirmation. Continue autonomously when work is local, reversible, testable, and inside the phase scope.

STOP and leave a clear report without committing the incomplete phase if any of these occurs:

1. Git history diverged from remote and cannot be fast-forwarded safely.
2. A destructive migration would delete or irreversibly rewrite user data without an explicit compatibility plan.
3. A required external provider needs credentials, payment, license acceptance, interactive login, or a product choice that is not already configured.
4. A stable external API required by the phase does not exist and no honest supported fallback satisfies the completion gate.
5. A new architecture choice has two materially different product behaviors and repository/user context does not resolve which one is intended.
6. Tests expose a pre-existing corruption that cannot be repaired safely inside the current phase.
7. A push would require force-push or rewriting shared history.
8. Secrets or credentials are discovered in tracked files. Stop and report; do not commit them.

When stopped:

- keep the worktree inspectable;
- do not claim PASS;
- do not advance to the next phase;
- do not commit a knowingly incomplete phase merely to save progress.

---

# 5. Phase 5 — Backend Control Plane & Operational Runtime

## Goal

Turn the Phase 4 durable pipeline into an operational backend that future API/UI code can control without touching SQLAlchemy models or orchestrator internals directly.

Phase 5 owns:

1. application service / command boundary;
2. workflow lifecycle semantics;
3. stable read models;
4. runtime loop and runner supervision.

It does **not** implement HTTP or frontend.

## Workstream A — Application Service & Lifecycle

Create a clean application-level service layer for commands such as:

```text
create workflow
add project
start
pause
resume
retry failed step
cancel
assign runner to workflow session
remove/disable runner assignment
```

Requirements:

- future API/UI must not write DB rows directly;
- future API/UI must not enqueue PipelineJob directly;
- commands are idempotent where repetition is expected;
- ownership/session validation is explicit;
- empty workflow cannot start;
- pause prevents new scheduling without rewriting completed history;
- resume continues from persisted state;
- retry does not overwrite immutable StoryVersion/audio history;
- cancel is terminal at workflow level;
- queued/waiting jobs belonging to a cancelled workflow must not later advance the workflow;
- late runner completion after cancellation must not resurrect the workflow;
- no hard delete of workflow history.

Concurrency tests must cover repeated/concurrent start, pause, resume, retry, and cancel.

## Workstream B — Read Models / Operational Status

Create DTO/dataclass/typed read models so future API/UI does not consume ORM entities directly.

Workflow snapshot should expose machine-readable information including:

```text
workflow id/status
project count
completed/failed/blocked counts
current step for each project
latest source/canon/story/version/tts/audio records
job state
waiting/blocked reason
business failure vs infrastructure failure information
runner/capacity status
artifact metadata using relative paths only
```

Requirements:

- current step is derived from DB state;
- no in-memory current-step tracker;
- query path is read-only;
- query path does not call runners;
- no credentials, claim tokens, or storage-root absolute paths;
- restart produces the same snapshot from the same DB.

The Phase 4 `chunks_missing` situation must be visible as blocked/inconsistent rather than silently complete.

## Workstream C — Runtime & Runner Supervision

Create an operational runtime around the existing orchestrator.

Required modes:

```text
run_once()
run_forever(...)
```

Requirements:

- bounded work per iteration;
- no busy-loop;
- bounded idle wait/backoff in long-running mode;
- graceful shutdown;
- existing stale-lease recovery reused rather than duplicated;
- one bad workflow should not silently kill all runtime processing;
- unexpected exceptions are observable;
- no broad silent exception swallowing;
- runner discovery does not automatically grant workflow-session permission;
- explicit session assignment remains required;
- health refresh preserves quota/cooldown rules;
- offline runners are not dispatched.

Provide a minimal standard-library CLI if consistent with the project, e.g.:

```text
python -m storyflow.runtime --once
python -m storyflow.runtime --run
```

Do not add a CLI framework dependency only for convenience.

## Required Phase 5 integration scenarios

### Lifecycle

```text
create
→ add project
→ start
→ pause
→ inspect
→ resume
→ failure
→ inspect
→ retry
→ completed
```

### Cancellation race

```text
start
→ runner processing
→ cancel
→ runner returns late
```

Expected: workflow remains cancelled and no new downstream step is scheduled.

### Two independent runtime instances

Use two independent service/runtime objects against the same SQLite DB with deterministic barriers/events.

Expected: no duplicate PipelineJob, StoryGeneration, TTSGeneration, AudioGeneration, or AudioChunk.

## Migration

Do not create a migration unless required for durable lifecycle/assignment semantics.

If needed:

```text
0005_...
down_revision = 0004_pipeline_dedupe
```

Never edit historical migrations.

## Phase 5 completion gate

PASS only if:

- application service is the working mutation boundary;
- empty workflow start is rejected;
- start/pause/resume/retry/cancel semantics are deterministic;
- late results cannot resurrect cancelled workflows;
- read models reconstruct completely from DB;
- runtime supports one-shot and long-running modes;
- graceful shutdown works;
- stale recovery remains correct;
- detected runners are not implicitly assigned;
- session isolation remains enforced;
- two-runtime concurrency does not duplicate work;
- Phase 4 end-to-end tests still pass;
- full backend suite passes 3 independent runs.

Verdict:

```text
PHASE 5: PASS — READY FOR PHASE 6
```

On PASS: update README, commit, push, verify remote, continue automatically to Phase 6.

---

# 6. Phase 6 — Local HTTP API

## Goal

Expose the Phase 5 application service and read models through a stable local API for the frontend.

The API is an adapter around the application layer, not a second business layer.

## Framework decision

First audit the backend dependency/config conventions.

If a suitable HTTP framework already exists, use it.

If none exists, prefer a small mainstream Python ASGI stack such as FastAPI + Uvicorn, pin/add dependencies using the repository's existing dependency-management convention, and document the decision.

Do not add multiple competing HTTP frameworks.

## Binding / security default

Default bind must be loopback only:

```text
127.0.0.1
```

Do not expose `0.0.0.0` by default.

Do not implement accounts/authentication in this phase.

Do not return secrets, DB URLs containing credentials, claim tokens, or unrestricted filesystem paths.

## Required API surface

Exact paths may follow local conventions, but capabilities must include:

### Health

```text
GET health
```

Returns server/runtime/db readiness without leaking secrets.

### Workflows

```text
list workflows
create workflow
get workflow snapshot
start
pause
resume
retry
cancel
```

Mutation endpoints must call Phase 5 application services only.

### Projects

```text
add project
get project/pipeline state
```

### Runners

```text
list detected/registered runners
inspect health/capacity
assign runner to workflow session
remove/disable assignment
```

Assignment must preserve Phase 2 session allow-list semantics.

### Artifacts

Provide a safe way for the future UI to read generated story/audio artifacts.

Requirements:

- caller does not submit arbitrary absolute paths;
- resolve through ArtifactStore safety rules;
- prevent `..` traversal/root escape;
- correct content type where practical;
- 404/validation errors do not leak storage root.

## Error contract

Define a consistent machine-readable error shape for:

```text
validation
not_found
conflict
not_retryable
invalid_state
capacity/unavailable
internal error
```

Do not expose raw tracebacks to API clients by default.

Map expected domain errors to stable status codes.

## Idempotency / concurrency

Repeated lifecycle requests must preserve Phase 5 idempotency.

HTTP handler retries must not create duplicate workflows/projects/jobs when an idempotent command is retried according to the chosen command contract.

Do not solve idempotency only with in-memory request caches.

## Runtime integration

Support a local server mode in which API and operational runtime can coexist safely.

Avoid two unrelated scheduler loops.

Define startup/shutdown ownership clearly.

API shutdown must stop runtime gracefully.

## Tests

Required:

- health;
- create/list/get workflow;
- add project;
- start empty rejected;
- start/pause/resume/retry/cancel;
- cancelled late result remains cancelled;
- runner list/assignment/session isolation;
- artifact happy path;
- traversal attack rejected;
- invalid IDs/state;
- API never exposes claim token or secret config;
- concurrent repeated requests do not duplicate work;
- startup/shutdown runtime integration.

Use in-process test client; tests must not require a real external AI provider.

## Phase 6 scope exclusions

Do not implement:

- frontend;
- authentication/accounts;
- cloud deployment;
- WebSocket unless there is a proven requirement that polling cannot satisfy;
- public internet binding by default;
- production provider credentials UI.

## Phase 6 completion gate

PASS only if:

- all required operations are exposed through the application/read-model boundary;
- no API handler directly mutates ORM state outside approved services;
- error contract is stable and tested;
- artifacts are safely served;
- local server startup/shutdown is deterministic;
- default bind is loopback;
- Phase 5 and Phase 4 tests remain green;
- full backend suite passes 3 independent runs.

Verdict:

```text
PHASE 6: PASS — READY FOR PHASE 7
```

On PASS: update README, commit, push, verify remote, continue automatically to Phase 7.

---

# 7. Phase 7 — Frontend MVP

## Goal

Replace the frontend placeholder with a usable local StoryFlow UI backed only by the Phase 6 API.

The frontend must not access SQLite or backend filesystem paths directly.

## Technology

Audit `frontend/` first.

If an existing frontend stack exists, preserve it.

If it is truly only a placeholder, use a small TypeScript + React + Vite setup unless repository context strongly indicates another stack.

Do not introduce a heavy component framework solely for convenience.

Prefer simple CSS/local components for the MVP.

## Required screens

### Workflow list

Show:

- workflow identity/name;
- lifecycle status;
- project count/progress;
- blocked/failed indicator;
- create workflow action.

### Workflow detail

Show:

- workflow lifecycle controls;
- project list;
- per-project current pipeline step;
- source/canon/story/TTS/audio progress;
- waiting/blocked/failure reason;
- assigned runners and health/capacity summary.

Controls:

```text
add project
start
pause
resume
retry
cancel
assign/remove runner
```

Disable or hide invalid actions according to API state; backend remains authoritative.

### Project detail

Show readable outputs where available:

- source/subtitle metadata;
- canon result;
- generated story version;
- TTS generation status;
- audio chunks.

Do not expose raw database internals unnecessarily.

### Artifacts

Allow:

- viewing story text;
- accessing generated audio through safe API artifact URLs;
- basic audio playback if browser-supported audio exists.

No video editor in this phase.

## Refresh model

Use simple polling first unless Phase 6 implemented an established event stream.

Requirements:

- bounded polling interval;
- no request storm;
- stop polling when page/component is gone;
- tolerate backend restart and show recoverable connection state.

Do not add WebSocket merely for aesthetics.

## UX requirements

MVP, not visual-polish phase.

Must clearly distinguish:

```text
draft
active
paused
waiting capacity
failed
blocked
cancelled
completed
```

Show business failure separately from infrastructure/capacity problems when the API provides them.

Destructive/terminal actions such as cancel should require a clear confirmation in UI.

## Frontend tests

Use the stack's normal testing approach.

Required coverage:

- API client mapping;
- workflow list rendering;
- detail state rendering;
- lifecycle controls;
- invalid controls disabled;
- failure/blocked states;
- runner assignment;
- artifact/story display;
- backend unavailable state.

## Full application E2E

Add at least one deterministic local E2E/smoke path using fake backend dependencies:

```text
start backend/runtime
→ open frontend
→ create workflow
→ add project
→ assign fake runners
→ start
→ wait/poll until completed
→ inspect story
→ inspect audio artifact/chunks
```

If browser automation framework is not already present, a lightweight integration/smoke approach is acceptable; do not add a large test framework solely for one test without justification.

## Phase 7 scope exclusions

Do not implement:

- authentication;
- multi-user collaboration;
- cloud hosting;
- video editing;
- timeline editor;
- publishing;
- extensive design system;
- real-provider credential entry unless already safely designed.

## Phase 7 completion gate

PASS only if:

- frontend no longer placeholder;
- normal workflow lifecycle can be operated without direct DB/CLI manipulation;
- frontend uses Phase 6 API only;
- all key pipeline states are visible;
- story/audio outputs are inspectable;
- fake end-to-end UI path completes;
- frontend build succeeds;
- frontend tests pass;
- full backend suite still passes 3 independent runs.

Verdict:

```text
PHASE 7: PASS — READY FOR PHASE 8
```

On PASS: update README, commit, push, verify remote, continue automatically to Phase 8.

---

# 8. Phase 8 — Production Integration Readiness

## Goal

Replace as many fake-only execution boundaries as can be honestly replaced with stable, supported real integrations, while preserving deterministic fake tests.

This phase must **not invent external APIs that do not exist**.

Real-provider code must remain behind the Phase 2–3 abstractions.

## Workstream A — Subtitle production wiring

Make `ExternalSubtitleClient` usable from the normal StoryFlow runtime without requiring developers to manually alter Python import paths.

Audit the pinned subtitle project and choose the safest integration model supported by reality, for example:

- same interpreter with explicitly installed upstream dependencies; or
- isolated subprocess/venv boundary if dependency isolation is necessary.

Requirements:

- no direct SQLite bypass if public upstream API suffices;
- no edits to upstream source;
- clear dependency/startup diagnostics;
- timeout/error classification;
- test fake remains deterministic;
- real smoke may be optional when network/provider conditions are unavailable, but wiring must be testable without silently faking success.

## Workstream B — Real story runner

Audit the currently available runner/provider surfaces at execution time.

Preference order:

1. stable AionUI task-run API if it now exists and satisfies submit → task id → poll/result → cancel semantics;
2. an already-configured local supported runner interface present in the project/environment with a stable noninteractive contract;
3. otherwise STOP Phase 8 with a documented external blocker rather than pretending the fake gateway is production.

Possible local runner families may include Claude/Codex/OpenCode only if actually available and automatable through a stable documented local interface.

Requirements:

- adapter implements existing `AgentRunner`/`TaskGateway` boundary;
- dispatcher/session allow-list preserved;
- credentials are never persisted into story artifacts or API responses;
- stdout/stderr/error mapping is bounded and classified;
- timeout/cancel behavior is explicit;
- task output passes existing validators before persistence.

Do not hardcode personal absolute executable paths when discoverable/configurable paths can be used.

## Workstream C — Real TTS synthesis

First distinguish the pinned `story-tts-adapter` responsibility from actual audio synthesis.

If an actual TTS synthesis engine/provider is already configured in the repository/environment, integrate it behind the existing runner/adapter boundary.

If no synthesis engine/provider has been selected/configured, this is a legitimate STOP condition for Phase 8 because choosing one changes product/dependency behavior.

Do not silently choose a paid cloud provider or upload text/audio to an external service without explicit existing configuration.

Requirements for a real TTS adapter when available:

- deterministic request metadata persisted where appropriate;
- output written through ArtifactStore;
- chunk ordering/retry semantics preserved;
- credentials excluded from DB/artifacts/logs;
- provider error/timeout/rate-limit classification;
- fake tests remain the primary deterministic test path.

## Configuration

Add a clear runtime configuration model for choosing providers without putting secrets in tracked files.

Accept environment variables or local ignored config according to repository conventions.

Provide `.env.example` or equivalent only with placeholder names, never secrets.

Frontend may display provider readiness/status but should not become a secret store unless a secure local secret design already exists.

## Real smoke tests

Where credentials/provider access are available, add opt-in smoke tests separated from deterministic CI tests.

They must never run automatically in normal unit test suite.

Example convention:

```text
STORYFLOW_RUN_REAL_SMOKE=1
```

Exact naming may follow project conventions.

## Phase 8 completion gate

PASS only if:

- subtitle real wiring is operational or its upstream limitation is demonstrably outside StoryFlow and does not prevent configured use;
- at least one **real story execution backend** is usable through normal StoryFlow dispatcher infrastructure;
- at least one **real TTS synthesis backend** is usable through normal StoryFlow infrastructure;
- provider configuration does not leak secrets;
- deterministic fake suite still passes;
- optional real smoke path is documented and succeeds when configured;
- no session isolation regression;
- frontend can distinguish provider ready/unavailable states;
- full normal test suites pass.

If no real story runner or no real TTS engine can be selected without human/product input, verdict must be:

```text
PHASE 8: BLOCKED — INPUT REQUIRED
```

In that case:

- do not commit an incomplete Phase 8 as PASS;
- do not start Phase 9;
- leave a concise blocker report describing exactly what input/configuration is required.

On genuine PASS:

```text
PHASE 8: PASS — READY FOR PHASE 9
```

Update README, commit, push, verify remote, continue automatically to Phase 9.

---

# 9. Phase 9 — Release Hardening & Local MVP Completion

## Goal

Turn the completed application into a reproducible local MVP that can be started, stopped, diagnosed, and upgraded without developer-only manual steps.

No new product features unless required to make the existing system reliably operable.

## Startup / shutdown

Make `start-app.bat` a real entry point rather than a placeholder.

Expected responsibilities:

```text
validate bootstrap pins
validate/install guidance for dependencies
validate configuration
validate/migrate database according to explicit safe policy
start backend API + operational runtime
start/serve frontend
handle Ctrl+C / shutdown cleanly
```

Do not silently destructive-migrate a database.

If migrations are applied automatically, create a clear backup/safety policy first and test it.

## Reproducible setup

Document clean-machine setup for Windows 11, including required:

- Python version;
- Node version if frontend uses Node tooling;
- Git;
- bootstrap step;
- backend dependencies;
- frontend dependencies/build;
- provider configuration;
- database/runtime directories.

Prefer scripts that validate prerequisites and fail with actionable messages.

Do not bundle credentials.

## Logging / diagnostics

Provide useful local logs for:

- app startup;
- runtime lifecycle;
- workflow state transitions;
- runner/provider availability;
- unexpected failures.

Requirements:

- no secrets;
- no full sensitive story/source payloads by default unless intentionally configured;
- bounded/rotating log policy if logs persist;
- diagnostic command/page for basic readiness.

## Database safety

Add a simple backup/restore or documented safe backup mechanism appropriate for local SQLite.

At minimum:

- consistent database backup while app is stopped, or SQLite backup API if backing up live;
- artifact directory backup instructions paired with DB backup;
- restore procedure tested on a temporary workspace.

Do not pretend DB-only backup is sufficient if artifacts are external files.

## Packaging / serving

Choose the smallest practical local-MVP distribution model consistent with current architecture.

Acceptable outcomes include:

- production-built frontend served by local backend plus start script; or
- another already-adopted local packaging model.

Do not introduce Electron/Tauri solely to call the project "desktop" unless the existing roadmap/product explicitly requires it.

## Final regression matrix

Run on a clean temporary runtime/database:

1. bootstrap/check;
2. migration from base to head;
3. backend tests;
4. frontend tests/build;
5. fake-provider end-to-end workflow;
6. pause/resume/retry/cancel;
7. restart/resume;
8. two-runtime/session isolation regression;
9. artifact safety/traversal tests;
10. backup + restore smoke;
11. startup + graceful shutdown smoke;
12. real-provider smoke if Phase 8 configuration is available.

## README / operator docs

README must accurately state:

```text
Phase 0–9 COMPLETE
current migration head
current backend test result
frontend test/build result
supported real provider(s)
known external limitations
how to start app
how to stop app
how to backup/restore
```

Add a concise operator/troubleshooting document if README would become too large.

## Final completion gate

PASS only if:

- clean setup instructions are reproducible;
- `start-app.bat` launches the actual application stack;
- normal shutdown is clean;
- migration path works from clean DB;
- backend/frontend normal test suites pass;
- deterministic full-app E2E passes;
- configured real-provider smoke passes when Phase 8 requires it;
- backup/restore smoke passes;
- no secrets are tracked;
- `git diff --check` clean;
- `skills/` and upstream external repos remain clean;
- README matches repository reality.

Final verdict:

```text
PHASE 9: PASS — STORYFLOW LOCAL MVP COMPLETE
```

On PASS:

1. update README and this roadmap progress;
2. commit with:

```text
phase 9: release hardening
```

3. push `origin/master`;
4. verify local HEAD equals `origin/master`;
5. STOP. Do not invent Phase 10 automatically.

---

# 10. Per-Phase Closure Audit Template

Before declaring any phase PASS, perform a closure audit after implementation rather than trusting the implementation report alone.

Use this structure:

```text
PHASE N CLOSURE AUDIT

1. Requirement-by-requirement PASS/FAIL
2. Repository diff review
3. Migration/model consistency
4. Scope check
5. skills/ clean check
6. external/ clean check
7. bootstrap pin check
8. targeted phase tests
9. integration tests
10. concurrency/restart/session-isolation tests where applicable
11. full suite run #1
12. full suite run #2
13. full suite run #3
14. remaining issues classified as:
    - blocking current phase
    - external documented gap
    - future phase/non-blocking
15. final verdict
```

Do not mark PASS merely because tests pass if a required capability is missing.

Do not mark FAIL merely because a documented external gap belongs to a later phase and the current completion gate explicitly permits it.

---

# 11. Automatic README Update Rule

At the end of every successful phase, update a small canonical section near the top of `README.md`:

```text
Current Status

Phase 0 COMPLETE
...
Phase N COMPLETE

Migration head: <head>
Backend tests: <latest result>
Frontend tests/build: <latest result if applicable>
Next: Phase N+1
```

Also update architecture/module sections that became stale.

Never leave README claiming an older migration head/test count after a successful phase.

---

# 12. Progress Tracker

Update this table only after a phase closure audit passes and the phase is pushed successfully.

| Phase | Status | Commit | Notes |
|---|---|---|---|
| 0 | COMPLETE | existing | Bootstrap |
| 1 | COMPLETE | existing | Durable backend |
| 2 | COMPLETE | existing | Runner abstraction/dispatcher |
| 3 | COMPLETE | existing | Domain + subtitle + gateway foundations |
| 4 | COMPLETE | existing | End-to-end durable orchestration |
| 5 | COMPLETE | db31bc0 | Backend control plane/runtime; 333 passed ×3 |
| 6 | COMPLETE | ed02b93 | Local HTTP API; 420 passed ×3 |
| 7 | COMPLETE | see git log (`phase 7: frontend mvp`) | Frontend MVP; 105 unit + 2 e2e; backend 422 ×3 |
| 8 | PENDING |  | Production integration readiness |
| 9 | PENDING |  | Release hardening/local MVP |

---

# 13. Final Instruction

Start at the first PENDING phase.

Execute continuously.

For each phase:

```text
IMPLEMENT
→ TEST
→ CLOSURE AUDIT
→ FIX IF NEEDED
→ PASS
→ UPDATE README/ROADMAP
→ COMMIT
→ PUSH
→ VERIFY REMOTE
→ NEXT PHASE
```

Do not ask the human to approve a successfully completed phase.

Do not push a failed phase.

Do not fabricate real provider support.

If Phase 8 requires missing credentials/provider selection, stop honestly with `PHASE 8: BLOCKED — INPUT REQUIRED` and leave Phase 9 untouched.

If Phase 9 passes, push the final commit and stop with:

```text
PHASE 9: PASS — STORYFLOW LOCAL MVP COMPLETE
```
