"""Phase 5 hardening regressions found by the two-runtime scenario."""

from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow import queue
from storyflow.agents import FakeRunner, RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.models import PipelineJob, RunnerInstance, WorkflowSession

NOW = datetime(2026, 1, 2, 12, 0, 0)


# --- ArtifactStore under concurrent writers (Windows os.replace PermissionError) ------------


def _busy_replace(*_args, **_kwargs):
    raise PermissionError("[WinError 5] Access is denied")


def test_identical_concurrent_write_is_success_and_leaves_no_tmp(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    store.write("a/b.txt", b"same")
    monkeypatch.setattr("storyflow.artifacts.os.replace", _busy_replace)
    assert store.write("a/b.txt", b"same") == "a/b.txt"
    assert store.read("a/b.txt") == b"same"
    assert not list(tmp_path.rglob(".tmp-*"))


def test_conflicting_write_is_not_hidden_and_keeps_old_content(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    store.write("a/b.txt", b"old")
    monkeypatch.setattr("storyflow.artifacts.os.replace", _busy_replace)
    monkeypatch.setattr("storyflow.artifacts.time.sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        store.write("a/b.txt", b"new")
    assert store.read("a/b.txt") == b"old"
    assert not list(tmp_path.rglob(".tmp-*"))


def test_transient_busy_then_success_promotes_new_content(tmp_path, monkeypatch):
    import os

    store = ArtifactStore(tmp_path)
    store.write("a/b.txt", b"old")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("busy")
        return real(src, dst)

    monkeypatch.setattr("storyflow.artifacts.os.replace", flaky)
    monkeypatch.setattr("storyflow.artifacts.time.sleep", lambda _s: None)
    store.write("a/b.txt", b"new")
    assert store.read("a/b.txt") == b"new" and len(calls) == 3


# --- Dispatcher: lost claim race is not a failure ------------------------------------------


@pytest.mark.parametrize("exc", [queue.RunnerAtCapacity("full"), queue.RunnerUnavailable("gone")])
def test_claim_race_is_lost_race_not_failure(db, monkeypatch, exc):
    session = WorkflowSession(mode="auto", status="active", role_preferences={})
    db.add(session)
    db.commit()
    runner = RunnerInstance(workflow_session_id=session.id, runner_type="fake", max_concurrency=1,
                            supported_roles=["general_worker"])
    db.add(runner)
    db.commit()
    registry = RunnerRegistry()
    agent = FakeRunner("fake")
    registry.register(runner.id, agent)
    job = queue.enqueue_job(db, kind="chunk", session_id=session.id, now=NOW)

    def race(*_a, **_k):
        raise exc

    monkeypatch.setattr(queue, "claim_next_job", race)
    outcome, job_id = Dispatcher(registry).run_round(db, session, now=NOW)
    assert outcome is DispatchOutcome.LOST_RACE and job_id == job.id
    assert agent.invocations == []
    fresh = db.scalar(select(PipelineJob).where(PipelineJob.id == job.id), execution_options={"populate_existing": True})
    assert fresh.status == "queued" and fresh.attempts == 0 and fresh.infrastructure_failures == 0
    assert fresh.execution_count == 0
