# StoryFlow

**Biến video YouTube thành những câu chuyện hoàn toàn mới và audio đọc truyện - chạy cục bộ, có thể khởi động lại giữa chừng và gần như không cần thao tác tay.**

[English](README.md) | Tiếng Việt

StoryFlow là một workflow engine chạy cục bộ để sản xuất truyện có sự hỗ trợ của AI. Bạn đưa cho nó một video YouTube,
một playlist hoặc cả một kênh; nó sẽ lấy phụ đề, phân tích câu chuyện (nhân vật, quan hệ, sự kiện), nhờ AI viết một
**câu chuyện rẽ nhánh mới (alternate-branch)**, có thể tự động review và sửa lại, chuyển văn bản sang dạng phù hợp để đọc thành tiếng,
rồi đọc bằng một mô hình text-to-speech chạy trên máy bạn - kết quả cuối cùng là **một file audio hoàn chỉnh cho mỗi truyện**.

Toàn bộ chạy trên máy của bạn (chỉ lắng nghe ở `127.0.0.1`), mọi bước đều được lưu bền vững trong cơ sở dữ liệu SQLite, và
sẽ tiếp tục từ chỗ đã dừng sau khi bị crash hoặc khởi động lại. Bạn có thể điều khiển nó qua giao diện web, qua HTTP API,
hoặc đơn giản là nhờ một trợ lý AI (ví dụ Claude Code) gọi API đó thay bạn.

```text
 Video / playlist / kênh YouTube   (hoặc file phụ đề bạn tự bỏ vào một thư mục)
                 |
                 v   phụ đề (mọi ngôn ngữ; trong các lần chạy của chúng tôi là tiếng Việt)
          transcript nguồn ---> phân tích canon  (nhân vật, quan hệ, sự kiện)
                 |
                 v   Claude viết truyện MỚI, dài ít nhất bằng bản gốc
               truyện ---> [tùy chọn] review + bản sửa lại
                 |
                 v   văn bản được chuyển sang dạng dễ đọc thành tiếng và chia thành các chunk
        TTS cục bộ (VieNeu-TTS) đọc từng chunk  --->  MỘT file .wav cuối cùng cho mỗi truyện
```

> **Trạng thái hiện tại** - Các phase 0-9 (MVP chạy cục bộ) và lộ trình batch P0-P5 đã hoàn thành.
> Migration head `0009_feed_min_duration` | backend tests `1933 passed, 7 skipped` | frontend `144 passed`, typecheck và
> build đều sạch | release smoke `10/10` bước thực thi được đều pass (chạy trọn vẹn một kênh ở chế độ offline).
> Tiếp theo: các team phân tích nhiều agent và các audio worker chạy song song (cần nhiều hơn một model runner / một GPU).

## Điểm nổi bật

- **Cài đặt một chạm.** `setup.bat` cài mọi thứ và chỉ hỏi vài câu cần thiết (Claude, giọng đọc, kênh...).
- **Xử lý cả kênh, không chỉ một video.** Dán link kênh, playlist hoặc video; mỗi video chỉ được xử lý **một lần**
  (sổ ledger), video mới đăng sẽ được nhận khi quét lại (re-scan), video không có phụ đề sẽ bị bỏ qua và batch vẫn chạy tiếp.
- **Bền vững và khởi động lại được.** Mọi tiến trình nằm trong SQLite; có retry kèm backoff, phục hồi sau crash, tạm dừng /
  tiếp tục / thử lại, migration an toàn với bản sao lưu tự động.
- **Các preset chất lượng.** `fast` (mặc định), `balanced` (một editor ghi lại nhận xét + các vấn đề) và `quality` (editor
  còn trả về một bản truyện đã sửa, tối đa 2 vòng).
- **Provider thật hoặc bản demo offline.** Claude Code CLI viết truyện, VieNeu-TTS đọc, yt-dlp liệt kê kênh; mỗi thứ đều có
  một bản giả lập (fake) có tính xác định, nên cả ứng dụng chạy được offline với `start-app.bat --fake`.
- **Riêng tư từ thiết kế.** Chỉ loopback, không telemetry, không lưu hay ghi log API key; các sub-process chạy với môi trường
  đã được làm sạch.
- **Được kiểm thử kỹ.** ~1.900 backend test, ~140 frontend test, và một release smoke khởi động các server process thật.

## Yêu cầu

| Cần có | Phiên bản / ghi chú |
|---|---|
| Windows 11 | các script setup và start là file `.bat` và mọi thứ đã được kiểm thử trên Windows 11 |
| Python | 3.11 trở lên (đã thử với 3.13) |
| Node.js | 20 trở lên (đã thử với 24) - dùng để build giao diện web |
| Git | tải các dự án phụ trợ đã được ghim phiên bản |
| [Claude Code CLI](https://claude.com/claude-code) | tùy chọn, phải **đã đăng nhập** (chạy `claude` một lần); viết truyện / canon / review bằng lượng dùng Claude **của bạn** |
| Bản checkout của [VieNeu-TTS](https://github.com/pnnbao97/VieNeu-TTS) | tùy chọn; tổng hợp giọng nói tiếng Việt chạy cục bộ (CPU là đủ, GPU nhanh hơn) |
| yt-dlp | tùy chọn; chỉ cần cho link kênh / playlist (do `setup.bat` cài, không cần API key) |

Nếu thiếu các thành phần tùy chọn, StoryFlow vẫn khởi động được: dùng `--fake` để chạy demo offline đầy đủ.

## Bắt đầu nhanh

```bat
git clone https://github.com/giaminhNguyen/StoryFlow.git
cd StoryFlow
setup.bat          REM chạy một lần: tải các nguồn đã ghim phiên bản, tạo Python venv, build frontend, cài đặt provider có hướng dẫn, kiểm tra tình trạng hệ thống
start-app.bat      REM khởi động API + runtime + giao diện web và mở http://127.0.0.1:8765
```

* `setup.bat` có thể chạy lại an toàn. Nó phát hiện `claude` và VieNeu-TTS, liệt kê các giọng đọc có sẵn, cài `yt-dlp` nếu bạn
  muốn dùng link kênh và ghi ra `backend\.env` (các key tùy chỉnh sẵn có của bạn được giữ nguyên, file cũ được sao lưu).
* `start-app.bat --fake` chạy một bản demo offline có tính xác định (không cần model, không cần mạng). Dừng bằng `Ctrl+C`.
* `start-app.bat --port 9000 --no-open` đổi cổng / không tự mở trình duyệt.
* Kiểm tra mọi thứ mà không thay đổi gì: `setup.bat /check`, `start-app.bat /check`,
  `backend\.venv\Scripts\python -m storyflow doctor`.

## Sử dụng StoryFlow

### Giao diện web (`http://127.0.0.1:8765`)

Giao diện "web cục bộ" là một ứng dụng React nhỏ do chính tiến trình chạy API phục vụ - chỉ cần mở địa chỉ đó trong trình duyệt.

| Trang | Bạn có thể làm gì |
|---|---|
| **Workflows** | xem danh sách mọi workflow cùng tiến độ, trạng thái và tình trạng của các provider; tạo workflow cho **một** video (id, ngôn ngữ, nhánh truyện tùy chọn, giọng đọc) |
| **Workflow detail** | start / pause / resume / retry / cancel; xem pipeline của từng project (source, canon, story, [review], tts, audio), các project bị bỏ qua / cần chú ý kèm lý do, thêm project, gán runner và xem sức chứa (capacity) |
| **Project detail** | đọc bản nguồn và truyện mới, nhận xét và các vấn đề của review, nghe từng chunk audio và file **audio đầy đủ** (có link tải xuống) |

Batch (kênh / playlist), preset và failure policy được cấu hình qua API - xem phần tiếp theo.
Vì API chỉ là một HTTP API cục bộ thông thường, một trợ lý AI như Claude Code có thể vận hành toàn bộ hệ thống giúp bạn.

### Xử lý cả một kênh (API)

```bash
API=http://127.0.0.1:8765/api

# 1. tạo workflow: phụ đề tiếng Việt, preset 'fast', video lỗi thì bỏ qua, tối đa 2 project chạy cùng lúc
curl -s -X POST $API/workflows -H "Content-Type: application/json" -d '{
  "name": "my-channel",
  "config": {"source": {"languages": ["vi"]}, "preset": "fast", "batch": {"max_active": 2},
             "failure_policy": {"on_no_subtitle": "skip", "on_permanent_error": "continue"}}}'

# 2. lấy 3 video mới nhất của một kênh (playlist, link video và "inbox:file.txt" cũng dùng được)
curl -s -X POST $API/workflows/<workflow-id>/sources -H "Content-Type: application/json" -d '{
  "sources": ["https://www.youtube.com/@some-channel"], "limit": 3, "languages": ["vi"]}'

# 3. gán runner cho workflow (Claude cho văn bản, VieNeu cho audio), 4. chạy, 5. theo dõi
curl -s $API/runners                                            # ghi lại các runner id
curl -s -X POST $API/runners/<runner-id>/assign -H "Content-Type: application/json" -d '{"workflow_id": "<workflow-id>"}'
curl -s -X POST $API/workflows/<workflow-id>/start
curl -s $API/workflows/<workflow-id>                            # số lượng, trạng thái từng project, capacity
```

> Trên Windows, đừng gửi JSON chứa ký tự không phải ASCII (ví dụ tên giọng đọc tiếng Việt) bằng `curl`: văn bản sẽ bị lỗi font.
> Hãy dùng một script Python / PowerShell nhỏ với UTF-8, hoặc đặt giọng đọc một lần trong `backend\.env`.

| Endpoint | Mục đích |
|---|---|
| `GET /api/health`, `GET /api/providers` | revision của database, trạng thái runtime, mức sẵn sàng của các provider phụ đề / truyện / TTS |
| `POST /api/workflows`, `.../{id}/start`, `pause`, `resume`, `retry`, `cancel` | tạo và điều khiển một workflow |
| `POST /api/workflows/{id}/projects` | thêm một project thủ công |
| `POST /api/workflows/{id}/sources`, `POST .../sync`, `GET .../feeds` | thêm video / playlist / kênh, quét lại để tìm video mới đăng, liệt kê các feed |
| `POST /api/projects/{id}/retry` | đưa một project bị bỏ qua / cần chú ý quay lại chạy |
| `GET /api/workflows/{id}`, `GET /api/projects/{id}` | toàn bộ trạng thái, artifact, review, audio (`audio.final_path`) |
| `GET /api/runners`, `POST /api/runners/{id}/assign` / `unassign` / `enable` / `disable` | runner pool |
| `GET /api/artifacts/{relative-path}` | stream một file đã lưu (truyện, chunk, `final.wav`) |

### Preset, failure policy và concurrency

| Thiết lập (workflow `config`) | Tác dụng |
|---|---|
| `"preset": "fast" \| "balanced" \| "quality"` | `fast` = không review; `balanced` = một lần gọi editor ghi lại nhận xét + các vấn đề; `quality` = editor còn sửa lại truyện (tối đa 2 vòng) |
| `"failure_policy"` | retry kèm backoff khi yêu cầu lấy phụ đề bị chặn; `on_no_subtitle: skip` và `on_permanent_error: continue` chỉ kết thúc project bị ảnh hưởng; các sự cố mang tính hệ thống (hết quota, đăng nhập, runner ngừng hoạt động) luôn làm workflow tạm dừng, và một circuit breaker sẽ dừng batch nếu nó thất bại theo cùng một kiểu 3 lần |
| `"batch": {"max_active": 2}` | chỉ N project chưa xong đầu tiên được chạy canon / story / review / tts / audio, để các giai đoạn chồng lên nhau; phụ đề vẫn được tải trước cho mọi project đang chờ, lần lượt từng cái trong một tick của runtime (không song song), nên batch rất lớn có thể làm tick đầu kéo dài hoặc khiến provider backoff |
| `"story": {"target_length": 9000}` | đơn vị là từ; mặc định là "ít nhất dài bằng bản gốc" (tối đa 15.000) |

Chi tiết, đầy đủ các trường và mã lỗi nằm trong [docs/OPERATIONS.md](docs/OPERATIONS.md) (tiếng Anh).

### Bạn nhận được gì

Với mỗi video, trong `runtime\artifacts\projects\<project-id>\`:

| Artifact | Đường dẫn |
|---|---|
| Transcript nguồn | `source\0001\source.txt` |
| Phân tích canon | `canon\<id>\canon.json` |
| Truyện mới (có đánh phiên bản) | `story\<id>\story.md` (các bản sửa từ preset `quality`: `review\<id>\story_revised.md`) |
| Văn bản đọc + kế hoạch chia chunk | `tts\<id>\` |
| Các chunk audio và **file đã ghép** | `audio\<id>\run-001\0001.wav ...` và `final.wav` |

Phụ đề của riêng bạn cũng dùng được: đặt `<video_id>.txt` (hoặc `.srt` / `.vtt`) vào `runtime\inbox\` và nó sẽ được dùng thay vì
hỏi YouTube - tiện khi YouTube chặn IP của bạn.

## Cấu hình

`setup.bat` sẽ ghi `backend\.env` (đã được git bỏ qua) giúp bạn; bạn có thể sửa tay bất cứ lúc nào. Biến môi trường thật sẽ được ưu tiên hơn.

| Thiết lập | Mặc định | Ý nghĩa |
|---|---|---|
| `STORYFLOW_SUBTITLE_PROVIDER` | `external` | `external` (sub-process Subtitle_supperVip đã ghim phiên bản), `fake`, `none` |
| `STORYFLOW_STORY_RUNNER` | `none` | `claude-cli`, `fake`, `none` (không có văn bản nào được gửi đi đâu trừ khi bạn chọn một runner) |
| `STORYFLOW_CLAUDE_CLI`, `STORYFLOW_CLAUDE_MODEL`, `STORYFLOW_STORY_TIMEOUT` | PATH / mặc định / `900` (setup dùng `1800`) | file thực thi Claude, model, số giây cho mỗi truyện |
| `STORYFLOW_TTS_ENGINE` | `none` | `vieneu`, `fake`, `none` |
| `STORYFLOW_VIENEU_ROOT`, `_VOICE`, `_PRECISION`, `_THREADS` | - / `Ngọc Huyền` / `fp32` / `6` | bản checkout VieNeu-TTS của bạn, giọng đọc có sẵn, `fp32` hoặc `int8` nhanh hơn, số luồng CPU |
| `STORYFLOW_YTDLP_PYTHON`, `STORYFLOW_LISTER_TIMEOUT`, `STORYFLOW_INBOX_DIR` | venv / `120` / `runtime\inbox` | trình thông dịch và timeout khi liệt kê kênh, thư mục chứa file phụ đề của riêng bạn |
| `STORYFLOW_AUDIO_MAX_MB`, `STORYFLOW_ARTIFACT_MAX_MB` | `4096` / `256` | dung lượng lớn nhất của file audio / văn bản mà API phục vụ |
| `STORYFLOW_LOG_DIR`, `STORYFLOW_LOG_LEVEL` | `runtime\logs` / `info` | ghi log |

Thông tin đăng nhập thuộc về các công cụ bên ngoài (phiên đăng nhập `claude`); StoryFlow không bao giờ đọc, lưu hay ghi log chúng.
Danh sách đầy đủ được ghi ở đầu file [`backend/storyflow/providers.py`](backend/storyflow/providers.py).

## Vận hành hằng ngày

```bat
scripts\backup.bat                                   REM sao lưu đã xác minh cho database + artifact (an toàn khi đang chạy)
scripts\restore.bat --from runtime\backups\<dir>     REM hãy dừng ứng dụng trước; thêm --force để thay thế dữ liệu hiện có
backend\.venv\Scripts\python -m storyflow doctor     REM kiểm tra tình trạng môi trường / provider
backend\.venv\Scripts\python -m storyflow migrate    REM áp dụng các database migration (một bản sao lưu đã xác minh được tạo trước)
```

Mọi thứ có thể thay đổi đều nằm trong `runtime\` (database, artifact, bản sao lưu, log, inbox) và được git bỏ qua. Cách xử lý sự cố,
chi tiết sao lưu / khôi phục và chính sách migration nằm trong [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Cách hoạt động

```text
 Giao diện trình duyệt (React) --+
 Trợ lý AI / curl -------------+--> FastAPI (127.0.0.1:8765) --> services --> SQLite (WAL)
                                                                                ^
 runtime loop --> orchestrator (máy trạng thái khởi động lại được, dựa trên trạng thái bền vững)
                    |-- inline steps:  subtitle worker (sub-process), yt-dlp lister
                    `-- job steps ---> job queue --> dispatcher (danh sách runner được phép, chuyển dự phòng khi hết quota)
                                                       |-- Claude CLI runner  (canon, story, review)
                                                       `-- VieNeu-TTS runner  (văn bản đọc, audio, final.wav)
```

* Một **workflow** chứa nhiều **project** (mỗi video một project). Mỗi project đi qua các bước `source -> canon -> story ->
  [review] -> tts -> audio`; mỗi bước là một dòng dữ liệu bền vững cộng thêm (với công việc của AI) một job trong hàng đợi,
  nên không có gì phụ thuộc vào bộ nhớ.
* **Orchestrator** tính lại "bước tiếp theo là gì" từ database ở mỗi tick; **dispatcher** chỉ giao job cho
  các runner của workflow đó, xử lý quota / rate limit và retry; các lỗi được phân loại (tạm thời, nghiệp vụ,
  hệ thống) và được xử lý theo failure policy của workflow.
* **Skills** lo phần sáng tạo: `story-branch-writer` (truyện rẽ nhánh) và `story-tts-adapter` (văn bản đọc
  và chia chunk) từ [Skills-Import](https://github.com/giaminhNguyen/Skills-Import); phụ đề đến từ
  [Subtitle_supperVip](https://github.com/giaminhNguyen/Subtitle_supperVip). Cả hai được ghim theo commit trong
  `sources.lock.json` và được `bootstrap.py` tải về.

Công nghệ sử dụng: Python 3.13, FastAPI, SQLAlchemy 2, Alembic, SQLite; React 19, Vite, TypeScript; pytest, Vitest.

### Cấu trúc repository

```text
setup.bat  start-app.bat        cài đặt / khởi động một chạm
bootstrap.py, sources.lock.json các nguồn ngoài đã ghim phiên bản (tải vào external/ và skills/)
backend/storyflow/              phần engine
    api/                          ứng dụng FastAPI, các route, phục vụ artifact
    orchestrator.py pipeline.py   máy trạng thái khởi động lại được và các hợp đồng của từng bước
    story_steps.py review_steps.py tts_steps.py     các bước source / canon / story / review / tts / audio
    sources.py source_service.py  nạp kênh / playlist / video, ledger, inbox
    policy.py presets.py          failure policy, cửa sổ batch, preset
    dispatcher.py queue.py agents.py   job queue, chọn runner, failover
    integrations/                 adapter cho Claude CLI, VieNeu-TTS, sub-process phụ đề và yt-dlp
    readmodels.py services.py     phía truy vấn và ranh giới thay đổi dữ liệu duy nhất của API
    ops/                          doctor, backup, restore, migrate
backend/alembic/versions/       các schema migration (0001 ... 0009)
backend/tests/                  ~1.900 test
frontend/src/                   giao diện web (trang, component, test)
scripts/                        trình hướng dẫn setup, backup / restore, release smoke test
docs/                           OPERATIONS.md (hướng dẫn vận hành), ENGINEERING_HISTORY.md
runtime/  external/  skills/    được tạo ra trên máy của bạn (git bỏ qua)
```

## Phát triển và kiểm thử

```bat
cd backend  && .venv\Scripts\python -m pytest -q                        REM bộ test backend (khoảng 6 phút)
cd frontend && npm run typecheck && npm test && npm run build          REM frontend
python scripts\release_smoke.py --skip-backend-tests                    REM khởi động server thật trên một workspace tạm thời
```

`release_smoke.py` chạy 13 bước (bootstrap, migration, các luồng end-to-end, pause / retry / cancel, phục hồi sau khi khởi động lại,
cô lập dữ liệu, an toàn đường dẫn artifact, backup / restore, tắt máy êm (graceful shutdown) và một batch kênh offline); bước dùng
provider thật là tùy chọn, bật bằng `STORYFLOW_RUN_REAL_SMOKE=1`.

## Quyền riêng tư, an toàn và các giới hạn đã biết

- **Chỉ loopback, không có xác thực.** Không bao giờ mở cổng này ra mạng. Không có telemetry; bí mật (secret) không bao giờ được
  lưu hay ghi log; phần phục vụ artifact từ chối mọi thứ nằm ngoài cây thư mục artifact.
- **Nó tiêu tốn tài nguyên của bạn.** Các lần chạy tạo truyện thật dùng lượng sử dụng Claude của bạn (một video 30 phút cho ra
  khoảng một truyện 9.000 từ), và việc tổng hợp giọng nói bằng CPU mất hàng chục phút cho mỗi truyện.
- **YouTube có thể chặn yêu cầu lấy phụ đề từ một số IP.** StoryFlow sẽ lùi lại (backoff) và thử lại; thư mục inbox cho phép bạn tự cung cấp
  phụ đề.
- **Tôn trọng nguồn gốc.** Các truyện là những nhánh rẽ mới do AI viết từ một transcript; hãy bảo đảm bạn có
  quyền sử dụng các video bạn đưa vào và quyền công bố những gì bạn tạo ra.
- **Ưu tiên Windows.** Các script nhắm tới Windows 11 và đó là nền tảng duy nhất đã được kiểm thử; phần backend và giao diện là Python / Node thông thường.
- **Chưa được xây dựng:** phân tích bằng nhiều agent song song, một pool audio worker song song và các agent khác ngoài Claude
  (xem các roadmap).
- **Giấy phép:** chưa có file license nào được thêm - hãy thêm một file trước khi bạn tái sử dụng hoặc phân phối lại mã nguồn.

## Tài liệu khác

- [docs/OPERATIONS.md](docs/OPERATIONS.md) - cài đặt, cấu hình, chạy, sao lưu, migration, xử lý sự cố,
  preset / failure policy / batch chuyên sâu.
- [docs/ENGINEERING_HISTORY.md](docs/ENGINEERING_HISTORY.md) - mỗi phase đã được thiết kế và kiểm chứng như thế nào (tiếng Anh).
- [STORYFLOW_MASTER_ROADMAP.md](STORYFLOW_MASTER_ROADMAP.md), [AUTONOMOUS_ROADMAP.md](AUTONOMOUS_ROADMAP.md) - các kế hoạch mà dự án
  này được xây dựng dựa trên (tiếng Anh).
