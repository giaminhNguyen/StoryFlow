"""Regression tests for the review findings on the batch failure engine (R1) + related items:

finalize-time failures obey the policy, systemic errors never burn the batch (+ circuit breaker), a partial
failure_policy keeps the recommended backoff, operator retries get fresh source attempts, a workflow never finishes
over a late project, one raising project cannot stop the round, config shapes are validated, cancel sweeps the review
step, a per-workflow lock serialises ticks and operator commands, the 0006 downgrade works with data, and
UI-created workflows feed the ledger.
"""

import threading
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import storyflow.config
from storyflow.artifacts import ArtifactStore
from storyflow.database import make_engine
from storyflow.errors import ValidationFailed
from storyflow.models import (
    CanonAnalysis, ChannelWorkflow, PipelineJob, SourceSnapshot, StoryProject, StoryReview,
)
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import PipelineContext
from storyflow.policy import BREAKER_LIMIT, RECOMMENDED
from storyflow.protocol import ResultCode, RunnerResult
from storyflow.services import WorkflowService
from storyflow.source_service import SourceService
from storyflow.story_steps import SourceStep
from storyflow.subtitles import BlockedByProvider, FakeSubtitleClient
from test_batch import IDS, TRACK, BatchStack, make_stack  # noqa: F401  (make_stack is a fixture)

CONTINUE = {"on_no_subtitle": "skip", "on_permanent_error": "continue"}


def fresh(db, model, pk):
    return db.get(model, pk, populate_existing=True)


def project_rows(s):
    with s.app.session_factory() as db:
        return {p.id: p for p in db.scalars(select(StoryProject).where(StoryProject.channel_workflow_id == s.wf),
                                            execution_options={"populate_existing": True}).all()}


def fail_canon_row(s, pid, code):
    """Mark the project's canon domain row FAILED (as finalize does for a bad output)."""
    with s.app.session_factory() as db:
        row = db.scalar(select(CanonAnalysis).join(SourceSnapshot, SourceSnapshot.id == CanonAnalysis.source_snapshot_id)
                        .where(SourceSnapshot.story_project_id == pid))
        assert row is not None, "the canon row must exist after the first tick"
        row.status, row.error_code = "failed", code
        db.commit()


# --- R1#1: a step that is already FAILED (finalize found a bad output) still obeys the policy -----------------


def test_a_failed_domain_row_ends_only_that_project_under_continue(make_stack):
    s = make_stack(IDS[:3], policy=CONTINUE)
    s.orch.tick(s.wf)                                   # snapshots + canon rows + queued jobs for the 3 projects
    fail_canon_row(s, s.pids[0], "missing_output")
    res = s.orch.tick(s.wf)
    assert res.ended == [(s.pids[0], "canon", "missing_output")] and res.failed == []
    assert res.workflow_status == "active"
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["needs_attention", "completed", "completed"]
    rows = project_rows(s)
    assert rows[s.pids[0]].status_reason == "missing_output"
    assert rows[s.pids[0]].status_detail == {"step": "canon", "error_code": "missing_output"}


def test_a_failed_domain_row_still_pauses_the_workflow_under_the_legacy_policy(make_stack):
    s = make_stack(IDS[:3], policy={"on_permanent_error": "pause"})
    s.orch.tick(s.wf)
    fail_canon_row(s, s.pids[0], "missing_output")
    res = s.orch.tick(s.wf)
    assert res.workflow_status == "paused" and res.ended == []
    with s.app.session_factory() as db:
        detail = fresh(db, ChannelWorkflow, s.wf).status_detail
    assert detail["project_id"] == s.pids[0] and detail["error_code"] == "missing_output"


# --- R1#2: systemic errors pause the workflow, a circuit breaker stops a repeating cause --------------------------


def test_a_systemic_failure_pauses_the_workflow_instead_of_burning_the_batch(make_stack):
    s = make_stack(IDS[:5], policy=CONTINUE, batch={"max_active": 2})
    s.router.execute = lambda packet: RunnerResult(code=ResultCode.TIMEOUT, error_code="timeout",
                                                    error_message="the runner timed out")
    snap = s.run(lambda snap: snap.status == "paused", rounds=200)
    assert snap.status_reason == "step_failed"
    assert snap.status_detail["error_code"].lower() == "infra_exhausted"
    assert snap.counts.needs_attention == 0                 # nothing was ended: the cause is not the video
    assert all(p.status == "active" for p in snap.projects)


def test_the_breaker_pauses_after_the_same_reason_ended_several_projects(make_stack):
    s = make_stack(IDS[:6], policy=CONTINUE)
    for pid in s.pids:
        s.router.poison[pid] = {"story"}
    snap = s.run(lambda snap: snap.status == "paused", rounds=300)
    ended = [p for p in snap.projects if p.state == "needs_attention"]
    assert len(ended) == BREAKER_LIMIT and {p.status_reason for p in ended} == {"poisoned"}
    assert snap.status_reason == "step_failed" and snap.status_detail["error_code"] == "poisoned"


def test_the_breaker_only_counts_the_same_reason(make_stack):
    s = make_stack(IDS[:4], policy=CONTINUE)
    s.router.poison[s.pids[0]] = {"canon"}
    s.router.poison[s.pids[1]] = {"story"}
    s.router.poison[s.pids[2]] = {"tts"}
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["needs_attention"] * 3 + ["completed"]


class Unit:
    """An orchestrator over plain rows (no runners) to call the policy decision directly."""

    def __init__(self, db, session_factory, tmp_path, policy):
        self.db = db
        self.ctx = PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "a"),
                                   subtitle_client=None, clock=lambda: datetime(2026, 6, 1))
        self.orch = Orchestrator(self.ctx, dispatcher=None, source=SourceStep(), steps=[])
        self.wf = ChannelWorkflow(name="w", mode="auto", status="active",
                                  config={"failure_policy": policy} if policy is not None else {})
        db.add(self.wf)
        db.commit()

    def project(self, status="active", reason=None):
        p = StoryProject(title="p", channel_workflow_id=self.wf.id, status=status, status_reason=reason)
        self.db.add(p)
        self.db.commit()
        return p

    def end(self, project, code, step="story"):
        return self.orch._end_by_policy(self.db, project.id, self.wf, step, code)


@pytest.fixture
def unit(db, session_factory, tmp_path):
    return lambda policy=CONTINUE: Unit(db, session_factory, tmp_path, policy)


def test_only_item_errors_end_a_project(unit, db):
    u = unit()
    for code in ("task_failed", "invalid_output", "invalid_story", "missing_output", "poisoned", "internal_error"):
        p = u.project()
        assert u.end(p, code) is True
        row = fresh(db, StoryProject, p.id)
        assert (row.status, row.status_reason) == ("needs_attention", code)
        assert row.status_detail == {"step": "story", "error_code": code}


@pytest.mark.parametrize("code", ["INFRA_EXHAUSTED", "LEASE_EXPIRED", "runner_crashed", "timeout", "rate_limited",
                                  "quota_exhausted", "auth_error", "ALL_AGENTS_UNAVAILABLE", "cancelled",
                                  "voice_not_found", "cli_not_found", "provider_unavailable"])
def test_systemic_errors_never_end_a_project(unit, db, code):
    u = unit()
    p = u.project()
    assert u.end(p, code) is False
    assert fresh(db, StoryProject, p.id).status == "active"


def test_the_legacy_policy_never_ends_a_project(unit, db):
    u = unit({"on_permanent_error": "pause"})
    p = u.project()
    assert u.end(p, "task_failed") is False and fresh(db, StoryProject, p.id).status == "active"


def test_the_breaker_counts_needs_attention_with_the_same_reason_only(unit, db):
    u = unit()
    for _ in range(BREAKER_LIMIT - 1):
        u.project("needs_attention", "poisoned")
    u.project("skipped", "poisoned")                      # skipped projects are not counted
    u.project("needs_attention", "other_reason")          # other reasons are not counted
    assert u.end(u.project(), "poisoned") is True          # the limit-th one may still end
    assert u.end(u.project(), "poisoned") is False         # ... the next one trips the breaker
    assert u.end(u.project(), "another_code") is True      # a different reason is still an item error


def test_the_breaker_is_per_workflow(unit, db):
    a, b = unit(), unit()
    for _ in range(BREAKER_LIMIT):
        a.project("needs_attention", "poisoned")
    assert a.end(a.project(), "poisoned") is False
    assert b.end(b.project(), "poisoned") is True


def test_an_already_ended_project_stays_ended(unit, db):
    u = unit()
    p = u.project("skipped", "subtitles_unavailable")
    assert u.end(p, "task_failed") is True
    assert fresh(db, StoryProject, p.id).status == "skipped"           # not overwritten


# --- R1#4: a partial failure_policy keeps the recommended retry limit / backoff ----------------------------------


class Scripted(FakeSubtitleClient):
    """Always blocked; counts the provider calls."""

    def __init__(self, store):
        super().__init__(store)
        self.calls = 0

    def fetch(self, video_id, *args, **kwargs):
        self.calls += 1
        raise BlockedByProvider("429")


def test_a_partial_policy_still_backs_off_and_limits_retries(make_stack):
    s = make_stack(IDS[:1], policy={"on_no_subtitle": "skip", "on_permanent_error": "continue"},
                   subtitles=Scripted({}))
    with s.app.session_factory() as db:
        stored = fresh(db, ChannelWorkflow, s.wf).config["failure_policy"]
    assert stored["subtitle_retries"] == 5 and stored["retry_base_seconds"] == 30       # merged over the recommended
    s.orch.tick(s.wf)
    assert s.subtitles.calls == 1
    s.orch.tick(s.wf)
    s.orch.tick(s.wf)
    assert s.subtitles.calls == 1                                    # backing off: the provider is not hit again
    p = project_rows(s)[s.pids[0]]
    assert p.source_attempts == 1 and p.next_attempt_at is not None


# --- R1#5: an operator retry / resume gives a fresh set of source attempts --------------------------------------


def _exhaust(s):
    """Run until the source retries are used up (legacy pause policy) -> the workflow pauses."""
    return s.run(lambda snap: snap.status == "paused", rounds=50)


def test_retry_after_exhausted_source_retries_gets_fresh_attempts(make_stack):
    s = make_stack(IDS[:1], policy={"subtitle_retries": 2, "retry_base_seconds": 0, "retry_max_seconds": 0,
                                    "on_permanent_error": "pause"}, subtitles=Scripted({}))
    snap = _exhaust(s)
    assert snap.status_detail["error_code"] == "subtitle_retries_exhausted"
    assert project_rows(s)[s.pids[0]].source_attempts == 2
    calls = s.subtitles.calls
    s.workflows.retry(s.wf)
    p = project_rows(s)[s.pids[0]]
    assert p.source_attempts == 0 and p.next_attempt_at is None and not (p.status_detail or {}).get("attempts")
    res = s.orch.tick(s.wf)
    assert s.subtitles.calls == calls + 1 and res.workflow_status == "active"     # one real attempt, not re-paused


def test_resume_resets_the_source_retry_state_but_not_after_a_snapshot(make_stack):
    s = make_stack(IDS[:2], policy={"subtitle_retries": 3, "retry_base_seconds": 30, "on_permanent_error": "pause"},
                   subtitles=Scripted({}), start=True)
    s.orch.tick(s.wf)
    with s.app.session_factory() as db:                      # project 1 already has a snapshot (attempts are history)
        db.add(SourceSnapshot(story_project_id=s.pids[1], snapshot_number=1, title="t", content="c"))
        p1 = db.get(StoryProject, s.pids[1])
        p1.source_attempts = 2
        db.commit()
    s.workflows.pause(s.wf)
    s.orch.resume(s.wf)
    rows = project_rows(s)
    assert rows[s.pids[0]].source_attempts == 0 and rows[s.pids[0]].next_attempt_at is None
    assert rows[s.pids[1]].source_attempts == 2              # untouched: it already has a snapshot


# --- R1#6 + R2#9: a workflow never finishes over a project added / re-armed meanwhile ------------------------------


def test_a_project_added_between_the_scan_and_the_finish_is_not_stranded(make_stack):
    s = make_stack(IDS[:2])
    orch, real, state = s.orch, s.orch._set_workflow_status, {"done": False}

    def racing(db, workflow_id, new, **kw):
        if new.value == "finished" and not state["done"]:
            state["done"] = True
            SourceService(s.app.ctx).add_sources(s.wf, [IDS[5]])     # writer saw ACTIVE: does not re-open
        return real(db, workflow_id, new, **kw)

    orch._set_workflow_status = racing
    snap = s.run(lambda snap: state["done"])
    assert snap.status == "active" and snap.finished_at is None      # re-opened right after the CAS won
    orch._set_workflow_status = real
    final = s.run(s.finished)
    assert len(final.projects) == 3 and [p.state for p in final.projects] == ["completed"] * 3


def test_a_project_added_during_the_tick_blocks_the_finish_decision(make_stack):
    s = make_stack(IDS[:2])
    orch, real, state = s.orch, s.orch._advance_project, {"added": False}
    real_status, transitions = orch._set_workflow_status, []

    def adding(db, pid, wf, now, res):
        real(db, pid, wf, now, res)
        # only once every existing project is closed, i.e. in the very tick that is about to finish the workflow
        if not state["added"] and pid == s.pids[-1] and all(orch._position(db, p).step is None for p in s.pids):
            state["added"] = True
            SourceService(s.app.ctx).add_sources(s.wf, [IDS[5]])

    def spy(db, workflow_id, new, **kw):
        transitions.append(new.value)
        return real_status(db, workflow_id, new, **kw)

    orch._advance_project, orch._set_workflow_status = adding, spy
    snap = s.run(lambda snap: state["added"])
    assert snap.status == "active"                                    # not finished over the unseen project
    assert "finished" not in transitions                              # ... it was never even attempted (pre-check)
    orch._advance_project, orch._set_workflow_status = real, real_status
    assert len(s.run(s.finished).projects) == 3


# --- R1#8: one raising project cannot stop the round ---------------------------------------------------------------


def _explode_for(monkeypatch, bad_pid, exc=ValueError("boom")):
    real = SourceStep.run

    def run(self, ctx, project_id):
        if project_id == bad_pid:
            raise exc
        return real(self, ctx, project_id)

    monkeypatch.setattr(SourceStep, "run", run)


def test_a_raising_project_is_ended_under_continue_and_the_others_finish(make_stack, monkeypatch):
    s = make_stack(IDS[:3], policy=CONTINUE)
    _explode_for(monkeypatch, s.pids[1])
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["completed", "needs_attention", "completed"]
    bad = project_rows(s)[s.pids[1]]
    assert bad.status_reason == "internal_error" and bad.status_detail == {"step": "orchestrator",
                                                                          "error_code": "internal_error"}


def test_a_raising_project_pauses_visibly_under_the_default_policy_without_blocking_the_others(make_stack, monkeypatch):
    s = make_stack(IDS[:3], policy={"on_permanent_error": "pause"})
    _explode_for(monkeypatch, s.pids[1])
    res = s.orch.tick(s.wf)
    assert res.workflow_status == "paused" and res.failed == [(s.pids[1], "orchestrator", "internal_error")]
    with s.app.session_factory() as db:
        detail = fresh(db, ChannelWorkflow, s.wf).status_detail
        snaps = db.scalars(select(SourceSnapshot.story_project_id)).all()
    assert detail == {"project_id": s.pids[1], "step": "orchestrator", "error_code": "internal_error"}
    assert set(snaps) == {s.pids[0], s.pids[2]}                         # the others were still advanced this tick


def test_a_busy_database_is_not_turned_into_a_failed_project(make_stack, monkeypatch):
    s = make_stack(IDS[:2], policy=CONTINUE)
    _explode_for(monkeypatch, s.pids[0], OperationalError("stmt", {}, Exception("database is locked")))
    with pytest.raises(OperationalError):
        s.orch.tick(s.wf)
    assert all(p.status == "active" for p in project_rows(s).values())


# --- create_workflow: config shapes and policy edge cases ---------------------------------------------------------


@pytest.fixture
def workflows(session_factory, tmp_path):
    ctx = PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "w"), subtitle_client=None,
                          clock=lambda: datetime(2026, 6, 1))
    return WorkflowService(ctx, Orchestrator(ctx, dispatcher=None, source=SourceStep(), steps=[]))


BAD_CONFIGS = [
    {"source": "abc"}, {"source": ["x"]}, {"story": 5}, {"tts": "narrator"},
    {"source": {"video_id": 5}}, {"source": {"video_id": ""}}, {"source": {"video_id": "x" * 65}},
    {"story": {"target_length": "8000"}}, {"story": {"target_length": 5000.0}}, {"story": {"target_length": 0}},
    {"story": {"target_length": 50001}}, {"story": {"target_length": True}}, {"story": {"branch": 3}},
    {"story": {"direction": ["dark"]}},
]


@pytest.mark.parametrize("bad", BAD_CONFIGS)
def test_create_workflow_rejects_wrongly_typed_blocks(workflows, db, bad):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config=bad)
    assert exc.value.details["reason"] == "invalid_config" and exc.value.details["field"]
    text = exc.value.message + str(exc.value.details)
    for leaked in ("abc", "narrator", "8000", "dark"):                 # a request value is never echoed back
        assert leaked not in text
    assert db.scalars(select(ChannelWorkflow)).all() == []


@pytest.mark.parametrize("good", [
    {}, {"source": None}, {"source": {"video_id": "abc"}}, {"source": {"languages": ["en"]}},
    {"story": {"target_length": 8000, "branch": "what if", "direction": "dark"}}, {"story": {"target_length": None}},
    {"tts": {"voice": "narrator"}}, {"story": None, "tts": None},
])
def test_create_workflow_accepts_valid_blocks(workflows, good):
    assert workflows.create_workflow("w", config=good).changed is True


def test_create_workflow_turns_an_infinite_policy_number_into_a_validation_error(workflows):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config={"failure_policy": {"subtitle_retries": float("inf")}})
    assert exc.value.details["reason"] == "invalid_failure_policy"


def test_create_workflow_rejects_max_below_base(workflows):
    with pytest.raises(ValidationFailed):
        workflows.create_workflow("w", config={"failure_policy": {"retry_base_seconds": 100, "retry_max_seconds": 5}})


def test_create_workflow_merges_a_partial_policy_and_keeps_explicit_nulls(workflows, db):
    wf = workflows.create_workflow("w", config={"failure_policy": {}}).workflow_id
    assert fresh(db, ChannelWorkflow, wf).config["failure_policy"] == RECOMMENDED
    wf2 = workflows.create_workflow("w2", config={"failure_policy": {"on_no_subtitle": "skip",
                                                                     "subtitle_retries": None}}).workflow_id
    stored = fresh(db, ChannelWorkflow, wf2).config["failure_policy"]
    assert stored == {**RECOMMENDED, "on_no_subtitle": "skip", "subtitle_retries": None}
    wf3 = workflows.create_workflow("w3", config={"failure_policy": None}).workflow_id
    assert fresh(db, ChannelWorkflow, wf3).config["failure_policy"] is None      # legacy opt-out stays


# --- R1#9: the 0006 downgrade works on a database that has child rows ---------------------------------------------


def test_0006_downgrade_works_with_child_rows(monkeypatch, tmp_path):
    from pathlib import Path
    url = f"sqlite:///{(tmp_path / 'm6.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    ts = "'2026-01-01 00:00:00.000000'"

    command.upgrade(cfg, "0006_source_policy")
    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(f"INSERT INTO story_projects (id, title, status, created_at, updated_at, source_attempts) "
                            f"VALUES ('p1','t','active',{ts},{ts},3)")
        con.exec_driver_sql(f"INSERT INTO source_snapshots (id, story_project_id, snapshot_number, title, content, "
                            f"meta, status, created_at) VALUES ('s1','p1',1,'t','c','{{}}','active',{ts})")
    engine.dispose()

    command.downgrade(cfg, "0005_control_plane")              # used to fail: FOREIGN KEY constraint failed
    engine = make_engine(url)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("story_projects")}
        assert not {"source_attempts", "next_attempt_at", "status_reason", "status_detail"} & cols
        with engine.connect() as con:                          # the data survived
            assert con.exec_driver_sql("SELECT id, title FROM story_projects").fetchall() == [("p1", "t")]
            assert con.exec_driver_sql("SELECT story_project_id FROM source_snapshots").fetchall() == [("p1",)]
    finally:
        engine.dispose()
    command.upgrade(cfg, "0006_source_policy")                # and it can go up again
    engine = make_engine(url)
    try:
        assert "source_attempts" in {c["name"] for c in inspect(engine).get_columns("story_projects")}
    finally:
        engine.dispose()


# --- R1#11: a per-workflow lock serialises ticks and operator commands ---------------------------------------------


def test_an_operator_command_waits_for_a_running_tick_of_the_same_workflow(make_stack):
    s = make_stack(IDS[:2])
    entered, release = threading.Event(), threading.Event()
    real = s.orch._advance_project

    def slow(db, pid, wf, now, res):
        entered.set()
        assert release.wait(10)
        return real(db, pid, wf, now, res)

    s.orch._advance_project = slow
    done = {}
    tick = threading.Thread(target=lambda: done.setdefault("tick", s.orch.tick(s.wf)))
    tick.start()
    assert entered.wait(10)
    resume = threading.Thread(target=lambda: done.setdefault("resume", s.orch.resume(s.wf)))
    resume.start()
    resume.join(0.4)
    assert resume.is_alive() and "resume" not in done                 # blocked behind the tick
    release.set()
    tick.join(10)
    resume.join(10)
    assert "tick" in done and "resume" in done and not resume.is_alive()


def test_ticks_of_different_workflows_do_not_block_each_other(make_stack):
    a = make_stack(IDS[:1])
    other = a.workflows.create_workflow("second", config={"source": {"languages": ["en"]}}).workflow_id
    SourceService(a.app.ctx).add_sources(other, [IDS[1]])
    a.workflows.start(other)
    entered, release = threading.Event(), threading.Event()
    real = a.orch._advance_project

    def slow(db, pid, wf, now, res):
        if wf.id == a.wf:                                   # only the first workflow's tick is held
            entered.set()
            assert release.wait(10)
        return real(db, pid, wf, now, res)

    a.orch._advance_project = slow
    t = threading.Thread(target=lambda: a.orch.tick(a.wf))
    t.start()
    assert entered.wait(10)
    res = a.orch.tick(other)                                # a different workflow: not blocked by the held lock
    assert res.workflow_status in ("active", "finished") and res.projects
    release.set()
    t.join(10)
    assert not t.is_alive()


# --- R3#3: cancel sweeps the review step ---------------------------------------------------------------------------


def test_cancel_sweeps_a_queued_review(tmp_path):
    s = BatchStack(tmp_path, IDS[:1], start=False)
    try:
        with s.app.session_factory() as db:                     # quality preset: review + revision
            wf = db.get(ChannelWorkflow, s.wf)
            wf.config = {**wf.config, "preset": "quality", "review": {"enabled": True, "revise": True, "max_rounds": 2}}
            db.commit()
        s.workflows.start(s.wf)
        s.assign_discovered_runner(s.wf)
        for _ in range(60):
            s.orch.run_round(s.wf)
            with s.app.session_factory() as db:
                review = db.scalar(select(StoryReview).where(StoryReview.story_project_id == s.pids[0]))
                if review is not None and review.status == "queued":
                    break
        else:
            raise AssertionError("the review never reached the queued state")
        job_id = review.pipeline_job_id
        assert job_id is not None
        result = s.workflows.cancel(s.wf)
        assert result.status == "cancelled"
        with s.app.session_factory() as db:
            assert fresh(db, StoryReview, review.id).status == "cancelled"
            assert fresh(db, PipelineJob, job_id).status == "cancelled"
        snap = s.read.get_project(s.pids[0])
        assert snap.review is not None and snap.review.status == "cancelled"
    finally:
        s.app.close()


# --- R2#1: UI-created workflows (workflow-level source + add_project) feed the ledger ------------------------------


def test_add_project_stamps_the_video_id_of_the_workflow_source(workflows, db):
    wf = workflows.create_workflow("ui", config={"source": {"video_id": "nap7Usq0lWE", "languages": ["vi"]}}).workflow_id
    pid = workflows.add_project(wf, "The story").detail["project_id"]
    assert fresh(db, StoryProject, pid).video_id == "nap7Usq0lWE"
    plain = workflows.create_workflow("plain").workflow_id
    assert fresh(db, StoryProject, workflows.add_project(plain, "P").detail["project_id"]).video_id is None
    odd = workflows.create_workflow("odd", config={"source": {"languages": ["en"]}}).workflow_id
    assert fresh(db, StoryProject, workflows.add_project(odd, "P").detail["project_id"]).video_id is None


def test_a_ui_created_project_is_reported_as_a_duplicate_by_add_sources(workflows, db):
    ui = workflows.create_workflow("ui", config={"source": {"video_id": "nap7Usq0lWE"}}).workflow_id
    workflows.add_project(ui, "The story")
    other = workflows.create_workflow("batch").workflow_id
    result = SourceService(workflows.ctx).add_sources(other, ["https://youtu.be/nap7Usq0lWE"])
    assert result.added == []
    assert [(d["video_id"], d["reason"]) for d in result.duplicates] == [("nap7Usq0lWE", "already_processed")]


# --- a step that tolerates its own failure (advisory review) does not fail the project or the workflow -----------------


def test_a_step_that_tolerates_its_failure_moves_the_chain_on(unit, db):
    from storyflow import queue
    from storyflow.models import JobStatus
    from storyflow.orchestrator import TickResult
    from storyflow.pipeline import StepHandler, StepStatus, StepView

    class Tolerant(StepHandler):
        step, job_kind, role = "tol", "tol_job", "story_writer"
        failed_marked = False

        def status(self, db, ctx, project):
            return StepView(StepStatus.COMPLETED if self.failed_marked else StepStatus.FAILED)

        def begin(self, db, ctx, project): return None
        def link_job(self, db, ctx, domain_id, job): pass
        def finalize(self, db, ctx, domain_id, job): pass

        def mark_failed(self, db, ctx, domain_id, job):
            self.failed_marked = True

    u = unit(CONTINUE)
    handler = Tolerant()
    u.orch = Orchestrator(u.ctx, dispatcher=None, source=SourceStep(), steps=[handler])
    p = u.project()
    job = queue.enqueue_job(db, kind="tol_job", payload={}, dedupe_key="tol:1", role="story_writer")
    job.status, job.last_error_code = JobStatus.FAILED.value, "task_failed"
    db.commit()
    res = TickResult(workflow_status="active")
    outcome = u.orch._reconcile(db, handler, p.id, "d1", job, res, u.wf)
    assert outcome == "again" and res.failed == [] and res.ended == []
    assert fresh(db, StoryProject, p.id).status == "active"                 # not ended, although the policy is continue

    strict = Tolerant()
    strict.mark_failed = lambda *a, **k: None                              # a normal step: still FAILED afterwards
    res2 = TickResult(workflow_status="active")
    assert u.orch._reconcile(db, strict, p.id, "d1", job, res2, u.wf) == "ended"
    assert res2.ended and fresh(db, StoryProject, p.id).status == "needs_attention"
