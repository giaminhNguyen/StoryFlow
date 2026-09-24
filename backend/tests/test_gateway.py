"""Gateway abstraction + FakeAionGateway tests.

Covers the Phase 3 surface: detect/health, submit->poll->wait, cancel, error
classification, the AgentRunner bridge, and integration with StoryFlow's
TaskPacket/RunnerResult/ResultCode and the Phase 2 dispatcher/allow-list.
Deterministic: FakeAionGateway completes tasks after a scripted poll count, no sleeps.
"""

from datetime import datetime

import pytest

from storyflow import queue
from storyflow.agents import RunnerRegistry
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.gateway import (
    DetectedRunner,
    FakeAionGateway,
    GatewayAgentRunner,
    GatewayError,
    TaskGateway,
    TaskHandle,
    gateway_error_code_to_result,
)
from storyflow.models import RunnerInstance, WorkflowSession
from storyflow.protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket


def packet(task_id="t1", role="general_worker"):
    return TaskPacket(task_id=task_id, job_id="j1", role=role, inputs={"chapter": 1})


BASE = datetime(2026, 1, 2, 12, 0, 0)


def make_session(db):
    s = WorkflowSession(mode="auto", status="active",
                        all_agents_unavailable_policy="pause_auto_resume")
    db.add(s)
    db.commit()
    return db.get(WorkflowSession, s.id)


def add_runner(db, session, runner_type="fake", *, roles=None):
    r = RunnerInstance(workflow_session_id=session.id, runner_type=runner_type,
                       enabled=True, max_concurrency=2, state="ready",
                       supported_roles=roles or ["general_worker"])
    db.add(r)
    db.commit()
    return db.get(RunnerInstance, r.id)


# --- detect / health -------------------------------------------------------


def test_detect_lists_runners_with_ids_and_health():
    gw = FakeAionGateway({"alpha": True, "beta": False})
    found = gw.detect()
    assert [d.runner_id for d in found] == ["alpha", "beta"]
    assert [d.health.runner_type for d in found] == ["fake", "fake"]
    assert {d.health.state for d in found} == {"ready", "offline"}
    assert all(isinstance(d, DetectedRunner) for d in found)
    assert all(isinstance(d.health, RunnerHealth) for d in found)


def test_health_unknown_runner_is_not_ok_not_raise():
    gw = FakeAionGateway({"alpha": True})
    h = gw.health("nope")
    assert h.ok is False
    assert h.state == "offline"
    assert "unknown" in (h.error_message or "")


# --- submit / poll ---------------------------------------------------------


def test_submit_returns_handle_and_echoes_packet():
    gw = FakeAionGateway()
    handle = gw.submit(packet())
    assert handle.task_id
    assert gw.submitted == [packet()]
    status = gw.poll(handle)
    assert status.done is True
    assert status.result.code == ResultCode.SUCCESS


def test_scripted_result_codes():
    gw = FakeAionGateway(results=[ResultCode.QUOTA_EXHAUSTED, ResultCode.TASK_FAILED])
    for expected in (ResultCode.QUOTA_EXHAUSTED, ResultCode.TASK_FAILED):
        assert gw.wait(gw.submit(packet()), timeout=1.0).code == expected
    # empty queue defaults to SUCCESS
    assert gw.wait(gw.submit(packet()), timeout=1.0).code == ResultCode.SUCCESS


def test_runner_result_shapes_pass_through():
    want = RunnerResult(code=ResultCode.SUCCESS, artifacts={"file": "x.md"},
                        metrics={"invocations": 42})
    gw = FakeAionGateway(results=[want])
    assert gw.wait(gw.submit(packet()), timeout=1.0) == want


def test_poll_requires_latency_polls_before_done():
    gw = FakeAionGateway(latency_polls=2)
    handle = gw.submit(packet())
    assert gw.poll(handle).done is False  # 2 left
    assert gw.poll(handle).done is False  # 1 left
    status = gw.poll(handle)              # 0 left -> done, task forgotten
    assert status.done is True
    assert status.result.code == ResultCode.SUCCESS
    with pytest.raises(GatewayError):
        gw.poll(handle)


def test_wait_times_out_on_stuck_task():
    gw = FakeAionGateway(latency_polls=None)  # never completes -> always the timeout path
    result = gw.wait(gw.submit(packet()), timeout=0.02)
    assert result.code == ResultCode.TIMEOUT
    assert "exceeded" in (result.error_message or "")


# --- cancel ----------------------------------------------------------------


def test_cancel_preempts_scripted_result():
    gw = FakeAionGateway(results=[ResultCode.TASK_FAILED], latency_polls=1)
    handle = gw.submit(packet())
    assert gw.cancel(handle) is True
    result = gw.wait(handle, timeout=1.0)
    assert result.code == ResultCode.CANCELLED
    assert handle.task_id in gw.cancelled


def test_cancel_unknown_handle_returns_false():
    gw = FakeAionGateway()
    assert gw.cancel(TaskHandle(task_id="ghost", gateway_id="fake")) is False


# --- error classification --------------------------------------------------


def test_classify_gateway_error_maps_to_infra_codes():
    gw = FakeAionGateway()
    assert gw.classify_error(GatewayError("transport_unavailable")) == ResultCode.TRANSIENT_FAILURE
    assert gw.classify_error(TimeoutError("took too long")) == ResultCode.TIMEOUT
    assert gw.classify_error(RuntimeError("boom")) == ResultCode.TRANSIENT_FAILURE
    # never a business code
    for exc in (GatewayError("boom"), RuntimeError("boom"), TimeoutError("t")):
        assert gw.classify_error(exc) not in (ResultCode.TASK_FAILED, ResultCode.INVALID_OUTPUT)


def test_gateway_error_code_mapping():
    assert gateway_error_code_to_result("runtime_auth_failed") == ResultCode.AUTH_ERROR
    assert gateway_error_code_to_result("assistant_not_found") == ResultCode.TRANSIENT_FAILURE
    assert gateway_error_code_to_result("transport_unavailable") == ResultCode.TRANSIENT_FAILURE
    assert gateway_error_code_to_result("schema_validation_failed") == ResultCode.TASK_FAILED
    assert gateway_error_code_to_result("") == ResultCode.TRANSIENT_FAILURE


# --- protocol integration --------------------------------------------------


def test_gateway_speaks_phase2_types():
    assert issubclass(FakeAionGateway, TaskGateway)
    gw = FakeAionGateway()
    result = gw.wait(gw.submit(packet()), timeout=1.0)
    assert isinstance(result, RunnerResult)
    assert isinstance(result.code, ResultCode)
    assert all(isinstance(d, DetectedRunner) for d in gw.detect())


# --- AgentRunner bridge (Phase 2 dispatcher/allow-list integration) ---------


def test_agent_runner_health_and_cancel_delegate_to_gateway():
    gw = FakeAionGateway({"up": True, "down": False})
    ar = GatewayAgentRunner(gw, runner_id="down")
    assert ar.health().ok is False
    assert ar.health().state == "offline"
    # cancel only makes sense for a task the gateway is actually holding
    handle = gw.submit(packet())
    assert ar.cancel(handle.task_id) is True
    assert handle.task_id in gw.cancelled


def test_agent_runner_autopicks_first_healthy_detected():
    assert GatewayAgentRunner(FakeAionGateway({"up": True})).runner_id == "up"
    with pytest.raises(ValueError):
        GatewayAgentRunner(FakeAionGateway({"down": False}))


def test_dispatcher_routes_through_gateway_without_bypassing_allow_list(db):
    a_session = make_session(db)
    b_session = make_session(db)
    ra = add_runner(db, a_session, "fake")
    rb = add_runner(db, b_session, "fake")
    ga = FakeAionGateway()
    gb = FakeAionGateway()
    reg = RunnerRegistry()
    reg.register(ra.id, GatewayAgentRunner(ga))
    reg.register(rb.id, GatewayAgentRunner(gb))
    disp = Dispatcher(reg)

    queue.enqueue_job(db, kind="chunk", payload={}, role="general_worker",
                      session_id=a_session.id, now=BASE)
    outcome, _ = disp.run_round(db, session=a_session, now=BASE)

    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert len(ga.submitted) == 1
    assert gb.submitted == [], "other-session gateway-backed runner must never be reached"


def test_dispatcher_keeps_storyflow_role_through_gateway(db):
    session = make_session(db)
    r = add_runner(db, session, "fake", roles=["story_writer"])
    gw = FakeAionGateway()
    reg = RunnerRegistry()
    reg.register(r.id, GatewayAgentRunner(gw))
    disp = Dispatcher(reg)

    queue.enqueue_job(db, kind="chunk", payload={}, role="story_writer",
                      session_id=session.id, now=BASE)
    outcome, _ = disp.run_round(db, session=session, now=BASE)

    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert gw.submitted[0].role == "story_writer", "role must stay StoryFlow's, not Aion-choosen"