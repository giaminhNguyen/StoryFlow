"""retry_project: bring ONE skipped / needs_attention project back into its workflow (roadmap 4.6 / 5.1).

Real alembic-migrated SQLite + the deterministic fake runners (Stack from test_phase5_integration), a frozen clock
and no sleeps. The route (POST /api/projects/{id}/retry) is covered at the end.
"""

import threading

import pytest
from sqlalchemy import func, select, update

from storyflow.api.app import create_app
from storyflow.errors import InvalidState, NotFound, NotRetryable
from storyflow.models import ChannelWorkflow, PipelineJob, StoryGeneration, StoryProject
from storyflow.protocol import ResultCode, RunnerResult
from test_api import CONFIG as API_CONFIG
from test_api import ScanClient, assert_error, discover_and_assign, make_runtime
from test_api import drive as api_drive
from test_phase5_integration import CONFIG, TRACK, Stack

POLICY = {"on_no_subtitle": "skip", "on_permanent_error": "continue"}
BATCH_CONFIG = {**CONFIG, "failure_policy": POLICY}
_FRESH = {"execution_options": {"populate_existing": True}}


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.app.close()


# --- helpers ---------------------------------------------------------------------------------


def add_video_project(stack, wf, title, video_id):
    """A project with its OWN video (the SourceService does this for channel / playlist / link sources)."""
    project_id = stack.workflows.add_project(wf, title).detail["project_id"]
    with stack.app.session_factory() as db:
        db.execute(update(StoryProject).where(StoryProject.id == project_id).values(
            video_id=video_id, source_config={"kind": "video", "video_id": video_id}))
        db.commit()
    return project_id


def start(stack, wf):
    stack.workflows.start(wf)
    stack.assign_discovered_runner(wf)


def finished(snap):
    return snap.status == "finished"


def drive(stack, wf, until=finished):
    return stack.drive(wf, until, max_iterations=300)


def states(snap):
    return {p.id: p.state for p in snap.projects}


def project_row(stack, project_id):
    with stack.app.session_factory() as db:
        return db.scalar(select(StoryProject).where(StoryProject.id == project_id), **_FRESH)


def workflow_row(stack, wf):
    with stack.app.session_factory() as db:
        return db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == wf), **_FRESH)


def mark_ended(stack, project_id, status, reason, step):
    with stack.app.session_factory() as db:
        db.execute(update(StoryProject).where(StoryProject.id == project_id).values(
            status=status, status_reason=reason, status_detail={"step": step, "error_code": reason}))
        db.commit()


def add_subtitle(stack, video_id):
    stack.app.ctx.subtitle_client.store[video_id] = {"tracks": [TRACK]}


def break_story_for(stack, project_id):
    """Make the fake story runner fail (business failure, retried up to max_attempts) for ONE project."""
    story = stack.router.story
    original = story.execute
    state = {"broken": True}

    def execute(packet):
        if (state["broken"] and (packet.task_config or {}).get("step") == "story"
                and (packet.inputs or {}).get("project_id") == project_id):
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="task_failed", error_message="scripted failure")
        return original(packet)

    story.execute = execute
    return state


def story_jobs(stack):
    return stack.count(PipelineJob, PipelineJob.kind == "story_generation")


def generations(stack, project_id, status=None):
    where = [StoryGeneration.story_project_id == project_id]
    if status:
        where.append(StoryGeneration.status == status)
    return stack.count(StoryGeneration, *where)


# --- a skipped project (no subtitle) ---------------------------------------------------------


def test_skipped_project_is_retried_once_its_subtitle_exists(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    good = add_video_project(stack, wf, "Good", "vid")
    late = add_video_project(stack, wf, "Late", "latevid")
    start(stack, wf)

    snap = drive(stack, wf)
    assert states(snap) == {good: "completed", late: "skipped"}
    assert snap.counts.skipped == 1 and snap.counts.completed == 1
    assert project_row(stack, late).status == "skipped" and project_row(stack, late).status_reason == "subtitles_unavailable"

    add_subtitle(stack, "latevid")                       # the subtitle shows up later
    result = stack.workflows.retry_project(late)
    assert result.changed is True and result.detail == {"project_id": late, "step": "source"}
    row = project_row(stack, late)
    assert (row.status, row.status_reason, row.status_detail, row.source_attempts) == ("active", None, None, 0)

    done = drive(stack, wf)
    assert states(done) == {good: "completed", late: "completed"}
    late_snap = next(p for p in done.projects if p.id == late)
    assert late_snap.story_version is not None and late_snap.audio.chunk_count == len(late_snap.audio.chunks) > 0


def test_a_project_that_is_still_without_subtitles_is_simply_skipped_again(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    late = add_video_project(stack, wf, "Late", "latevid")
    start(stack, wf)
    assert states(drive(stack, wf)) == {late: "skipped"}
    stack.workflows.retry_project(late)                  # nothing changed at the provider
    snap = drive(stack, wf)
    assert states(snap) == {late: "skipped"} and snap.status == "finished"


# --- a project that needs attention because a job step failed for good -----------------------


def test_needs_attention_project_gets_a_fresh_job_on_retry_and_finishes(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    good = add_video_project(stack, wf, "Good", "vid")
    bad = add_video_project(stack, wf, "Bad", "vid")
    breaker = break_story_for(stack, bad)
    start(stack, wf)

    snap = drive(stack, wf)
    assert states(snap) == {good: "completed", bad: "needs_attention"}
    row = project_row(stack, bad)
    assert row.status == "needs_attention" and row.status_detail["step"] == "story"
    assert row.status_reason == row.status_detail["error_code"]
    assert generations(stack, bad, "failed") == 1 and generations(stack, bad) == 1
    jobs_before = story_jobs(stack)

    breaker["broken"] = False                            # the runner works again
    result = stack.workflows.retry_project(bad)
    assert result.detail == {"project_id": bad, "step": "story"}
    assert generations(stack, bad) == 2 and generations(stack, bad, "failed") == 1   # a FRESH domain row, not a reuse
    assert story_jobs(stack) == jobs_before + 1                                      # and exactly one new job
    assert stack.read.get_workflow(wf).status == "active"

    done = drive(stack, wf)
    assert states(done) == {good: "completed", bad: "completed"}
    assert generations(stack, bad, "completed") == 1 and generations(stack, bad, "failed") == 1
    assert story_jobs(stack) == jobs_before + 1


def test_double_retry_is_a_no_op_the_second_time(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    bad = add_video_project(stack, wf, "Bad", "vid")
    break_story_for(stack, bad)
    start(stack, wf)
    drive(stack, wf)
    jobs = story_jobs(stack)

    assert stack.workflows.retry_project(bad).changed is True
    after_first = (story_jobs(stack), generations(stack, bad), project_row(stack, bad).status)
    with pytest.raises(NotRetryable) as exc:
        stack.workflows.retry_project(bad)
    assert exc.value.details["reason"] == "project_not_ended"
    assert (story_jobs(stack), generations(stack, bad), project_row(stack, bad).status) == after_first
    assert after_first[0] == jobs + 1


def test_concurrent_retries_create_exactly_one_job(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    bad = add_video_project(stack, wf, "Bad", "vid")
    break_story_for(stack, bad)
    start(stack, wf)
    drive(stack, wf)
    jobs, gens = story_jobs(stack), generations(stack, bad)

    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        barrier.wait(timeout=10)
        try:
            outcomes.append(stack.workflows.retry_project(bad))
        except NotRetryable as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(outcomes) == 2
    winners = [o for o in outcomes if not isinstance(o, Exception)]
    losers = [o for o in outcomes if isinstance(o, NotRetryable)]
    assert len(winners) == 1 and len(losers) == 1 and losers[0].details["reason"] == "project_not_ended"
    assert story_jobs(stack) == jobs + 1 and generations(stack, bad) == gens + 1


# --- the workflow lifecycle -------------------------------------------------------------------


def test_a_workflow_that_finished_because_everything_ended_is_reopened_and_finishes_again(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    a = add_video_project(stack, wf, "A", "gone-a")
    b = add_video_project(stack, wf, "B", "gone-b")
    start(stack, wf)

    snap = drive(stack, wf)
    assert states(snap) == {a: "skipped", b: "skipped"} and snap.finished_at is not None
    assert snap.counts.skipped == 2 and snap.counts.completed == 0

    add_subtitle(stack, "gone-a")
    stack.workflows.retry_project(a)
    reopened = stack.read.get_workflow(wf)
    assert reopened.status == "active" and reopened.finished_at is None
    assert workflow_row(stack, wf).finished_at is None

    done = drive(stack, wf)
    assert states(done) == {a: "completed", b: "skipped"} and done.finished_at is not None


def test_retry_in_an_operator_paused_workflow_reactivates_the_project_but_keeps_the_pause(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    keep = add_video_project(stack, wf, "Keep", "vid")
    late = add_video_project(stack, wf, "Late", "latevid")
    start(stack, wf)
    stack.workflows.pause(wf)                             # before the first tick: nothing has run yet
    mark_ended(stack, late, "skipped", "subtitles_unavailable", "source")

    add_subtitle(stack, "latevid")
    result = stack.workflows.retry_project(late)
    assert result.status == "paused" and project_row(stack, late).status == "active"
    assert stack.read.get_workflow(wf).status == "paused"
    stack.workflows.resume(wf)
    assert states(drive(stack, wf)) == {keep: "completed", late: "completed"}


# --- refusals -----------------------------------------------------------------------------------


def test_a_project_that_has_not_ended_is_refused_without_any_state_change(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    ok = add_video_project(stack, wf, "Ok", "vid")
    pending = add_video_project(stack, wf, "Pending", "vid")
    before_draft = (stack.read.get_workflow(wf), stack.count(PipelineJob))
    with pytest.raises(NotRetryable) as exc:              # not started yet
        stack.workflows.retry_project(pending)
    assert exc.value.details["reason"] == "project_not_ended"
    assert exc.value.details["workflow_id"] == wf and exc.value.details["project_id"] == pending
    assert (stack.read.get_workflow(wf), stack.count(PipelineJob)) == before_draft

    start(stack, wf)
    assert states(drive(stack, wf)) == {ok: "completed", pending: "completed"}
    before_done = (stack.read.get_workflow(wf), stack.count(PipelineJob), stack.count(StoryGeneration))
    with pytest.raises(NotRetryable) as done_exc:         # completed
        stack.workflows.retry_project(ok)
    assert done_exc.value.details["reason"] == "project_not_ended"
    assert (stack.read.get_workflow(wf), stack.count(PipelineJob), stack.count(StoryGeneration)) == before_done


def test_unknown_project_is_not_found(stack):
    with pytest.raises(NotFound) as exc:
        stack.workflows.retry_project("no-such-project")
    assert exc.value.details["reason"] == "project_not_found"


def test_a_cancelled_workflow_refuses_the_retry(stack):
    wf = stack.workflows.create_workflow("batch", config=BATCH_CONFIG).workflow_id
    project = add_video_project(stack, wf, "P", "vid")
    mark_ended(stack, project, "skipped", "subtitles_unavailable", "source")
    stack.workflows.cancel(wf)
    with pytest.raises(InvalidState) as exc:
        stack.workflows.retry_project(project)
    assert exc.value.details["reason"] == "terminal" and exc.value.details["status"] == "cancelled"
    assert project_row(stack, project).status == "skipped"


def test_a_project_without_a_workflow_is_refused(stack):
    with stack.app.session_factory() as db:
        orphan = StoryProject(title="orphan", status="skipped")
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id
    with pytest.raises(InvalidState) as exc:
        stack.workflows.retry_project(orphan_id)
    assert exc.value.details["reason"] == "workflow_closed"


# --- HTTP route ---------------------------------------------------------------------------------

API_POLICY_CONFIG = {**API_CONFIG, "source": {"video_id": "missing", "languages": ["en"]}, "failure_policy": POLICY}


@pytest.fixture
def rt(tmp_path):
    app = make_runtime(tmp_path, sleep=lambda s: None)
    yield app
    app.close()


@pytest.fixture
def client(rt):
    return ScanClient(create_app(rt))


def skipped_project(rt, client):
    r = client.post("/api/workflows", json={"name": "chan", "config": API_POLICY_CONFIG})
    assert r.status_code == 201, r.text
    wf = r.json()["workflow"]["id"]
    project = client.post(f"/api/workflows/{wf}/projects", json={"title": "Tale"}).json()["project"]["id"]
    assert client.post(f"/api/workflows/{wf}/start").status_code == 200
    discover_and_assign(rt, client, wf)
    snap = api_drive(rt, client, wf, lambda s: s["status"] == "finished")
    assert [p["state"] for p in snap["projects"]] == ["skipped"]
    return wf, project


def test_route_brings_a_skipped_project_back_and_it_completes(rt, client):
    wf, project = skipped_project(rt, client)
    rt.ctx.subtitle_client.store["missing"] = {"tracks": [TRACK]}

    r = client.post(f"/api/projects/{project}/retry")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"result", "workflow", "project"}
    assert body["result"]["changed"] is True and body["result"]["workflow_id"] == wf
    assert body["result"]["detail"] == {"project_id": project, "step": "source"}
    assert body["workflow"]["status"] == "active" and body["workflow"]["finished_at"] is None
    assert body["project"]["id"] == project and body["project"]["status"] == "active"
    assert body["project"]["state"] == "not_started" and body["project"]["status_reason"] is None

    snap = api_drive(rt, client, wf, lambda s: s["status"] == "finished")
    assert [p["state"] for p in snap["projects"]] == ["completed"]


def test_route_errors_follow_the_error_contract(rt, client):
    assert_error(client.post("/api/projects/nope/retry"), 404, "not_found")
    assert client.post("/api/projects/bad id!/retry").status_code == 422

    wf, project = skipped_project(rt, client)
    fresh = client.post(f"/api/workflows/{wf}/projects", json={"title": "Second"})
    assert fresh.status_code == 409                       # a finished workflow does not take new projects
    completed_wf = client.post("/api/workflows", json={"name": "ok", "config": API_CONFIG}).json()["workflow"]["id"]
    running = client.post(f"/api/workflows/{completed_wf}/projects", json={"title": "Tale"}).json()["project"]["id"]
    err = assert_error(client.post(f"/api/projects/{running}/retry"), 409, "not_retryable")
    assert err["details"]["reason"] == "project_not_ended" and err["details"]["project_id"] == running

    # cancel a workflow that still has an ended project -> 409 invalid_state
    other = client.post("/api/workflows", json={"name": "c", "config": API_POLICY_CONFIG}).json()["workflow"]["id"]
    ended = client.post(f"/api/workflows/{other}/projects", json={"title": "E"}).json()["project"]["id"]
    with rt.session_factory() as db:
        db.execute(update(StoryProject).where(StoryProject.id == ended).values(
            status="needs_attention", status_reason="task_failed", status_detail={"step": "story"}))
        db.commit()
    assert client.post(f"/api/workflows/{other}/cancel").status_code == 200
    assert_error(client.post(f"/api/projects/{ended}/retry"), 409, "invalid_state")


def test_route_takes_no_body_and_repeat_is_a_409_not_a_second_run(rt, client):
    wf, project = skipped_project(rt, client)
    rt.ctx.subtitle_client.store["missing"] = {"tracks": [TRACK]}
    assert client.post(f"/api/projects/{project}/retry").status_code == 200
    err = assert_error(client.post(f"/api/projects/{project}/retry"), 409, "not_retryable")
    assert err["details"]["reason"] == "project_not_ended"
    with rt.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(PipelineJob)) == 0     # nothing was enqueued by the retries
