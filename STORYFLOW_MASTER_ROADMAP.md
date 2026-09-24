# StoryFlow — Master Roadmap & Session Handoff

> Mục đích của file này:
> - Là **source of truth** cho toàn bộ roadmap StoryFlow.
> - Nếu mất/reset session AI, chỉ cần đưa file này cho session mới.
> - Session mới phải đọc file này trước khi tiếp tục phát triển.
> - Không tự ý nhảy phase nếu phase hiện tại chưa hoàn tất và chưa có report xác nhận.

---

## 1. Mục tiêu sản phẩm

StoryFlow là workflow automation cho người dùng muốn giảm tối đa thao tác thủ công trong chuỗi:

**Nguồn / YouTube / subtitle -> phân tích nội dung -> tạo truyện mới -> review -> chuẩn hóa TTS -> gen audio -> xuất artifact**

Mục tiêu UX cuối cùng:

1. User cấu hình nguồn.
2. User cấu hình các AI agent được phép tham gia.
3. User chọn preset / workflow.
4. Bấm Start.
5. StoryFlow tự chạy đến khi có story + audio hoàn chỉnh.
6. Khi một item lỗi, hệ thống cố retry/fallback/skip theo policy thay vì làm chết toàn batch.
7. Khi một agent hết quota, hệ thống tự chuyển sang agent khác **trong tập agent được cấu hình cho workflow/session**.
8. Có thể chạy hàng loạt nhiều video / nhiều channel và tiếp tục tự động.

StoryFlow không nên bắt user:
- tự tải subtitle;
- tự copy/paste source;
- tự gọi từng AI;
- tự chia role cho agent;
- tự đổi AI khi hết quota;
- tự chuyển story sang TTS;
- tự gen từng chunk audio;
- tự chuyển channel khi channel hiện tại hết source.

---

## 2. Kiến trúc tổng thể

StoryFlow chịu trách nhiệm về:

- business workflow;
- dependency giữa các bước;
- state machine;
- retry/fallback;
- batch processing;
- checkpoint/resume;
- lựa chọn role;
- phân phối task vào runner pool;
- artifact/version tracking;
- orchestration nhiều story/channel.

Aion chịu trách nhiệm về execution layer:

- connect CLI agents;
- detect runner;
- health;
- start task;
- poll/wait;
- cancel;
- trả result/error.

StoryFlow **không phụ thuộc trực tiếp vào việc Aion có khái niệm role hay không**.

Role nằm ở StoryFlow.

Ví dụ:

```text
StoryFlow
   |
   +-- role: canon_analyzer
   +-- role: story_writer
   +-- role: reviewer
   +-- role: tts_adapter
   |
   v
Aion
   |
   +-- Claude Code
   +-- Codex
   +-- OpenCode
   +-- Antigravity
```

Nguyên tắc quan trọng:

**role != runner_type**

Ví dụ:

```text
story_writer -> Claude / Codex / OpenCode
reviewer     -> Claude / Codex / Antigravity
tts_adapter  -> Claude / Codex
```

Không hard-code kiểu:

```text
Claude = writer
Codex = reviewer
```

---

# 3. Roadmap 5 Phase

---

# Phase 1 — Durable Job Infrastructure

## Mục tiêu

Xây nền persistence và job queue đủ an toàn để toàn hệ thống có thể chạy dài hạn, crash/restart mà không mất trạng thái.

## Nội dung chính

- SQLite / database foundation.
- Alembic migrations.
- PipelineJob / job persistence.
- Job states.
- Atomic job claim.
- Lease / lease expiration.
- Concurrent worker safety.
- Crash recovery.
- Requeue abandoned jobs.
- Idempotency cơ bản.
- Attempt tracking.
- Deduplication.
- Transaction boundaries.
- Tests cho concurrency/recovery.

## Kết quả Phase 1

Có một queue/job core mà Phase 2+ có thể dùng, không phụ thuộc business story.

---

# Phase 2 — Multi-Agent Runner Scheduler

## Mục tiêu

Xây execution infrastructure để StoryFlow có thể phân task cho nhiều loại AI CLI agent khác nhau.

Các loại agent hiện tại có thể gồm:

- Claude Code
- Codex
- OpenCode
- Antigravity
- các runner khác trong tương lai

## Khái niệm chính

### TaskPacket / RunnerResult

Task business phải được đóng gói độc lập với runner.

Ví dụ:

```text
TaskPacket
- role
- skill
- inputs
- outputs
- workspace
- constraints
- config
```

Agent không được tự thao tác DB business trực tiếp nếu không có thiết kế rõ ràng.

### Workflow/session allow-list

Mỗi workflow/session chỉ được dùng những runner đã được config tham gia.

Ví dụ user config:

```yaml
claude:
  enabled: true
  instances: 2

codex:
  enabled: true
  instances: 3

opencode:
  enabled: false

antigravity:
  enabled: true
  instances: 1
```

Thì chỉ pool này được phép nhận task:

```text
Claude #1
Claude #2
Codex #1
Codex #2
Codex #3
Antigravity #1
```

OpenCode dù đang cài trên máy cũng không được sử dụng.

### Runner capability

Runner nên có metadata kiểu:

- runner_type
- enabled
- supported_roles
- max_concurrency
- active_count
- health/state
- cooldown_until
- quota_reset_at

### Role preference

Có thể ưu tiên runner theo role nhưng không khóa cứng.

Ví dụ:

```text
story_writer:
  prefer: claude, codex

reviewer:
  prefer: codex, claude, antigravity
```

### Quota/rate-limit failover

Nếu một agent hết quota:

```text
Claude -> QUOTA_EXHAUSTED
        -> release slot
        -> requeue
        -> thử runner khác trong SAME SESSION
        -> Codex / OpenCode / ...
```

Quota/rate-limit không nên tính như business failure thông thường.

Nếu tất cả runner được config đều unavailable:

```text
WAITING_CAPACITY
```

Sau đó resume khi có runner usable trở lại.

### Không được tự ý dùng agent ngoài config

Failover chỉ được thực hiện trong allow-list của workflow/session.

## Kết quả Phase 2

StoryFlow có scheduler/dispatcher độc lập với business story và hỗ trợ:

- multi-runner;
- role matching;
- concurrency;
- capacity;
- quota;
- cooldown;
- failover;
- waiting/resume;
- session allow-list.

---

# Phase 3 — Story Domain + Subtitle Integration + Aion Gateway

## Trạng thái

**Đây là phase hiện tại tại thời điểm tạo file này.**

Không bắt đầu Phase 4 cho đến khi Phase 3 được hoàn tất, test và có report tổng hợp.

## Workstream 1 — Story Domain

Implement domain models:

- ChannelWorkflow
- StoryProject
- SourceSnapshot
- CanonAnalysis
- StoryGeneration
- StoryVersion
- TTSGeneration
- AudioGeneration
- AudioChunk

Ngoài ra:

- Alembic migration.
- Versioning / unique indexes.
- Artifact store.
- Temp dir.
- Atomic promote.
- Relative paths.
- Path traversal protection.
- Tests.

Không làm Subtitle adapter trong workstream này.
Không làm Aion gateway trong workstream này.

## Workstream 2 — Subtitle Integration

Audit:

`external/subtitle_suppervip`

Nguyên tắc:

- source/migrations/tests của Subtitle upstream là authoritative;
- tạo `SubtitleClient` abstraction;
- ưu tiên API public hiện có;
- không đọc SQLite trực tiếp nếu API phù hợp;
- deterministic fake client;
- tests;
- verify modernization hiện có;
- báo gap nếu API thiếu capability;
- không sửa upstream Subtitle nếu chưa cần/không được phép.

## Workstream 3 — AionUI Gateway

Tạo clean StoryFlow gateway abstraction cho:

- detect runners
- health
- start task
- poll/wait
- cancel
- error classification

Integrate với:

- TaskPacket
- RunnerResult
- scheduler infra từ Phase 2

Không implement story/TTS business logic trong gateway.

Nếu Aion chưa có stable API phù hợp:

- tạo abstraction;
- FakeAionGateway;
- document gap rõ ràng;
- tests.

## Rules Phase 3

- đọc code Phase 1–2 trước khi sửa;
- không sửa `skills/`;
- không sửa `external/subtitle_suppervip` nếu requirement không cho phép;
- tránh teammate sửa cùng file;
- mỗi teammate tự chạy tests;
- cuối Phase leader chạy toàn backend test suite ít nhất 3 lần;
- leader không tự viết lại phần teammate nếu không cần;
- không commit nếu task yêu cầu không commit.

## Output Phase 3

Report tổng hợp:

- report Story Domain;
- report Subtitle Integration;
- report Aion Gateway;
- files changed;
- migrations;
- test results;
- conflicts;
- remaining issues.

---

# Phase 4 — Batch Pipeline Orchestration & Parallel AI Workflow

## Mục tiêu

Ghép hạ tầng Phase 1–3 thành workflow end-to-end thực sự chạy được:

```text
Source / Channel / Video
        |
        v
Subtitle acquisition
        |
        v
SourceSnapshot
        |
        v
Parallel Analysis
        |
        v
Story Generation
        |
        v
Parallel Review
        |
        v
TTS Adaptation
        |
        v
Audio Chunk Planning
        |
        v
Parallel Audio Generation
        |
        v
Audio Assembly
        |
        v
Completed Artifact
```

Đây là phase xây **bộ não tự vận hành** của StoryFlow.

---

## 4.1 Multi-channel source ingestion

Hỗ trợ:

- 1 channel;
- nhiều channel;
- danh sách video;
- file/subtitle local;
- các source khác trong tương lai.

Ví dụ:

```text
Channel A
  |- Video 001
  |- Video 002
  |- Video 003

Channel B
  |- Video 001
  |- Video 002
```

StoryFlow phải biết source nào đã xử lý.

Cần cursor/state kiểu:

- last_seen_video
- last_processed_video
- source ID
- source hash
- subtitle status
- processed_at

Không xử lý lại source đã hoàn thành trừ khi user yêu cầu regenerate/reprocess.

Khi channel A hết source mới:

```text
Channel A exhausted
        |
        v
Channel B
        |
        v
Channel C
```

---

## 4.2 Subtitle retry / fallback / skip policy

Subtitle acquisition không được làm chết toàn batch khi một video lỗi.

Ví dụ state:

```text
DOWNLOAD_SUBTITLE
      |
      +-- success -> continue
      |
      +-- failure
             |
             v
           retry
             |
      +------+- ------+
      |             |
   success        still fail
      |             |
   continue      classify error
                    |
             +------+------+
             |             |
          fallback        skip
```

Phân loại lỗi tối thiểu:

- network/temporary;
- rate limit;
- authentication;
- no subtitle;
- private/deleted/unavailable source;
- corrupt subtitle;
- parse error;
- permanent unsupported case.

Policy:

### Temporary

- retry;
- exponential backoff;
- giới hạn số lần.

### Rate limit

- cooldown;
- retry sau;
- không làm chết batch.

### Không có subtitle

Nếu có fallback được config thì dùng fallback.

Nếu không:

- mark `needs_attention` hoặc `skipped`;
- tiếp tục item tiếp theo.

### Permanent failure

- ghi error rõ ràng;
- skip item;
- không block toàn workflow.

---

## 4.3 DAG / dependency-aware scheduler

Không phải bước nào cũng chạy song song được.

Dependency cơ bản:

```text
subtitle
  ->
source snapshot
  ->
analysis
  ->
story
  ->
review
  ->
TTS adaptation
  ->
audio
```

Nhưng các story/video độc lập có thể overlap:

```text
A: subtitle -> analysis -> story -> TTS -> audio
B:      subtitle -> analysis -> story -> TTS -> audio
C:           subtitle -> analysis -> story -> TTS -> audio
```

Mục tiêu là tận dụng máy và quota tối đa nhưng không phá dependency.

---

## 4.4 Parallel AI team

Một task lớn có thể fan-out thành nhiều agent.

Ví dụ analysis:

```text
SourceSnapshot
    |
    +-- Agent A: canon / character
    +-- Agent B: timeline
    +-- Agent C: plot structure
    +-- Agent D: style
    |
    v
Aggregate
```

Ví dụ review:

```text
Story Version
    |
    +-- reviewer canon
    +-- reviewer logic
    +-- reviewer style
    |
    v
Merge review
    |
    v
Revision / approve
```

Các agent này chỉ được lấy từ runner pool được config cho workflow/session.

---

## 4.5 Story generation

Story generation dùng skill phù hợp, ví dụ:

`story-branch-writer`

Use case điển hình:

- giữ tên nhân vật;
- giữ thân phận;
- giữ quan hệ;
- giữ bối cảnh;
- giữ các dữ kiện nền tảng;
- nhân vật trọng sinh giữ ký ức truyện gốc;
- được tạo timeline/cao trào/kết cục mới;
- hành vi phải hợp canon hoặc có nguyên nhân giải thích.

Story output phải được version hóa.

---

## 4.6 TTS adaptation

Sau khi story được approve:

```text
StoryVersion
   |
   v
story-tts-adapter
   |
   v
TTSGeneration
```

TTS adaptation không thay plot/canon/story meaning.

Mục đích:

- chuẩn hóa cách đọc;
- chia chunk;
- tối ưu punctuation;
- xử lý quote/dialogue;
- chuẩn bị manifest cho synthesis.

---

## 4.7 Bulk audio generation

Audio phải hỗ trợ sản xuất hàng loạt.

Không chạy vô hạn process.

Dùng worker pool / bounded concurrency.

Ví dụ:

```text
1000 chunks
    |
    v
TTS Queue
    |
 +--+--+--+--+
 |  |  |  |  |
W1 W2 W3 W4 W5
```

Các giới hạn cần xét:

- global concurrency;
- per TTS engine concurrency;
- per API/account concurrency;
- CPU/GPU/RAM;
- rate limit;
- disk IO.

Một story:

```text
chunk 1 --\
chunk 2 ---\
chunk 3 ----> assemble -> final audio
chunk 4 ---/
```

Nhiều story có thể gen chunk song song nếu resource cho phép.

---

## 4.8 Failure handling

Mỗi stage phải có:

- retry policy;
- max attempts;
- timeout;
- cancel;
- error classification;
- resume;
- checkpoint.

Một item lỗi không được làm chết toàn batch nếu policy cho phép skip.

---

## 4.9 Resume / crash recovery

StoryFlow phải restart được.

Ví dụ máy reset lúc:

```text
Story #127
  audio chunk 08/20
```

Khởi động lại không được chạy từ story #1.

Phải resume từ checkpoint phù hợp.

---

## 4.10 Idempotency

Không tạo duplicate nếu cùng job bị retry.

Cần bảo vệ:

- StoryVersion duplicate;
- TTSGeneration duplicate;
- AudioGeneration duplicate;
- AudioChunk duplicate;
- source duplicate.

---

## 4.11 Phase 4 tests

Bắt buộc test:

- 1 source happy path;
- nhiều source;
- nhiều channel;
- subtitle temporary failure;
- subtitle permanent failure;
- agent quota exhaustion;
- agent failover;
- all agents unavailable;
- resume after restart;
- duplicate/retry;
- partial audio generation;
- batch continuation;
- cancellation;
- FakeAionGateway;
- fake SubtitleClient.

## Output Phase 4

Có một headless end-to-end workflow chạy được hoàn chỉnh.

---

# Phase 5 — One-Click App, Presets, Monitoring & Production Hardening

## Mục tiêu

Biến engine Phase 4 thành sản phẩm người dùng bình thường có thể sử dụng mà gần như không cần hiểu agent orchestration.

Triết lý:

```text
Configure once
    ->
Press Start
    ->
StoryFlow runs automatically
```

---

## 5.1 User configuration

UI/config cho:

### Sources

- 1 hoặc nhiều YouTube channel;
- URL/video list;
- local subtitle/source;
- thứ tự source/channel.

### Agents

Ví dụ:

```text
[x] Claude Code      instances: 2
[x] Codex            instances: 3
[ ] OpenCode
[x] Antigravity      instances: 1
```

Chỉ agent được chọn mới tham gia.

### Concurrency

Ví dụ:

- concurrent stories;
- analysis workers;
- review workers;
- TTS workers;
- audio workers.

### Failure policy

Ví dụ:

- subtitle retries = 3;
- skip when no subtitle;
- pause or continue on permanent error;
- pause when all agents unavailable;
- auto resume when quota/capacity returns.

---

## 5.2 Workflow presets

Preset giúp user không phải hiểu orchestration.

### Fast

```text
Subtitle
 -> Analysis x1
 -> Writer x1
 -> TTS
 -> Audio
```

### Balanced

```text
Subtitle
 -> Analysis team
 -> Writer
 -> Review x2
 -> TTS
 -> Audio
```

### Quality

```text
Subtitle
 -> Analysis xN
 -> Writer x2
 -> Judge / merge
 -> Review xN
 -> Revision
 -> TTS review
 -> Audio
```

Preset phải map thành DAG/config chứ không hard-code business sâu trong UI.

---

## 5.3 Monitoring UI

Hiển thị:

- tổng số source;
- completed;
- running;
- skipped;
- failed;
- needs attention;
- progress từng stage.

Ví dụ:

```text
Overall: 37 / 500

Subtitle    73%
Story       49%
TTS         31%
Audio       25%
```

Agent status:

```text
Claude #1      BUSY
Claude #2      QUOTA
Codex #1       BUSY
Codex #2       READY
Antigravity #1 READY
```

---

## 5.4 Advanced view

Cho power user xem:

- DAG;
- current jobs;
- assigned runner;
- retries;
- cooldown;
- quota;
- errors;
- artifacts;
- logs;
- manual retry;
- cancel;
- resume.

User bình thường không cần mở view này.

---

## 5.5 Startup integration

Hoàn thiện app startup.

Ví dụ:

```text
start-app
  ->
bootstrap check
  ->
database migrations
  ->
backend
  ->
Aion detection
  ->
frontend
  ->
open StoryFlow
```

---

## 5.6 Production hardening

Test các case:

- Windows restart;
- app crash;
- backend crash;
- database migration;
- Aion unavailable;
- Subtitle service unavailable;
- agent process crash;
- quota exhaustion;
- disk full / low disk;
- artifact corrupt;
- path issue;
- duplicate start;
- partial batch;
- long-running batch;
- hundreds/thousands of sources.

---

## 5.7 Final release requirements

- E2E tests.
- Smoke tests.
- Stable config format.
- Logs/diagnostics.
- README.
- Install/start instructions.
- Backup/recovery guidance.
- Clear error messages.
- No hidden dependency on a specific AI CLI.
- No hidden dependency on every installed agent.
- Workflow strictly respects configured runner allow-list.

---

# 4. Global Technical Principles

## 4.1 StoryFlow owns business logic

Không nhét business logic vào:

- Aion gateway;
- runner implementation;
- Subtitle adapter;
- individual CLI wrappers.

Business logic nằm ở StoryFlow orchestration/application layer.

---

## 4.2 External systems behind abstractions

Ví dụ:

```text
SubtitleClient
AionGateway
Runner
ArtifactStore
TTS provider abstraction
```

Luôn có fake/deterministic implementation cho tests khi hợp lý.

---

## 4.3 Configured runner pool only

Không bao giờ tự động sử dụng agent chỉ vì nó đang cài trên máy.

Chỉ dùng agent thuộc workflow/session config.

---

## 4.4 Quota is capacity, not business failure

Quota/rate-limit:

- không làm hỏng story;
- không nên tăng business attempt như lỗi nội dung;
- thử runner khác;
- nếu hết runner thì waiting;
- resume sau.

---

## 4.5 Parallelism must respect dependencies

Tăng hiệu suất bằng:

- nhiều source song song;
- fan-out analysis;
- fan-out review;
- audio chunk workers;
- bounded concurrency.

Không chạy bước sau trước khi dependency hoàn tất.

---

## 4.6 Batch must continue through poisoned items

Một video/source lỗi không được chặn hàng trăm item khác.

Tùy policy:

```text
retry
 -> fallback
 -> needs_attention / skipped
 -> continue batch
```

---

## 4.7 Durable checkpoints

Mọi stage tốn thời gian phải có trạng thái persist đủ để resume sau crash/restart.

---

# 5. Current Handoff State

Tại thời điểm file này được tạo:

```text
Phase 1: DONE / đã có nền job infrastructure
Phase 2: DONE / đã có runner scheduler concepts
Phase 3: IN PROGRESS
Phase 4: NOT STARTED
Phase 5: NOT STARTED
```

Current workspace:

```text
E:\OTHER\StoryFlow
```

Current Phase 3 được chia 3 workstream:

```text
1. Story Domain
2. Subtitle Integration
3. AionUI Gateway
```

Session mới phải:

1. đọc file này;
2. đọc code hiện tại;
3. đọc migrations/tests;
4. kiểm tra Phase 3 thực tế đã hoàn thành đến đâu;
5. không giả định report cũ chính xác hơn repository;
6. tiếp tục phần Phase 3 còn thiếu;
7. chạy tests;
8. tạo report;
9. chỉ khi Phase 3 hoàn tất mới chuẩn bị Phase 4.

---

# 6. Instructions for a New AI Session

Khi file này được đưa vào một session AI mới, hãy thực hiện:

```text
You are continuing development of StoryFlow.

Treat STORYFLOW_MASTER_ROADMAP.md as the project roadmap/source of truth,
but treat the current repository code, migrations and tests as authoritative
for what has actually been implemented.

First:
1. Read this roadmap completely.
2. Audit the current repository state.
3. Determine the current phase and unfinished work.
4. Do NOT restart completed phases.
5. Do NOT jump to a later phase.
6. Preserve existing architecture unless there is a concrete reason to change it.
7. Use parallel teammates/agents when workstreams are independent.
8. Avoid multiple teammates editing the same files.
9. Run tests before declaring a phase complete.
10. Return a structured completion report.

Current expected phase at the time this file was written:
Phase 3 — Story Domain + Subtitle Integration + Aion Gateway.
```

---

# 7. Definition of Done for Entire StoryFlow

StoryFlow được coi là đạt mục tiêu ban đầu khi:

1. User có thể truyền một hoặc nhiều nguồn/channel.
2. StoryFlow tự tìm/lấy subtitle hoặc xử lý failure theo policy.
3. Không tải/xử lý lại source đã hoàn thành nếu không cần.
4. Có thể tự chuyển sang channel tiếp theo khi channel hiện tại hết source.
5. AI analysis có thể chia agent song song.
6. Story generation tự động.
7. Review có thể chia agent song song.
8. Chỉ các AI CLI được user config mới tham gia.
9. Agent hết quota được failover sang runner khác trong allow-list.
10. Nếu toàn bộ runner unavailable thì workflow pause/wait và resume được.
11. Story được version hóa.
12. TTS adaptation tự động.
13. Audio được chia chunk và gen hàng loạt với bounded concurrency.
14. Có thể chạy nhiều story song song theo giới hạn tài nguyên.
15. Crash/restart không làm mất toàn bộ tiến độ.
16. Một item lỗi không làm chết toàn batch.
17. User có UI/config đơn giản kiểu “configure -> Start -> nhận output”.
18. Có monitoring/logging đủ để debug.
19. Có E2E tests.
20. Không bị khóa cứng vào một AI CLI/provider duy nhất.

---

# 8. Short Summary

```text
PHASE 1
Durable jobs / DB / queue / recovery

        ↓

PHASE 2
Multi-agent runners / scheduler / quota / failover / allow-list

        ↓

PHASE 3
Story Domain + SubtitleClient + AionGateway

        ↓

PHASE 4
Batch DAG workflow:
Source -> Subtitle -> Analysis -> Story -> Review -> TTS -> Audio
+ multi-channel
+ retry/fallback
+ parallel agents
+ bulk audio
+ resume

        ↓

PHASE 5
One-click app:
config + presets + monitoring + startup + production hardening
```

---

End of StoryFlow Master Roadmap.
