"""Gateway abstraction + FakeAionGateway tests.

Covers the Phase 3 surface: detect/health, submit->poll->wait, cancel, error
classification, and integration with StoryFlow's TaskPacket/RunnerResult/ResultCode.
Deterministic: FakeAionGateway completes tasks after a scripted poll count, no sleeps.
"""

import pytest

from storyflow.gateway import (
    FakeAionGateway,
    GatewayError,
    TaskGateway,
    TaskHandle,
    gateway_error_code_to_result,
)
from storyflow.protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket


def packet(task_id="t1", role="general_worker"):
    return TaskPacket(task_id=task_id, job_id="j1", role=role, inputs={"chapter": 1})


# --- detect / health -------------------------------------------------------


def test_detect_lists_healthy_runners():
    gw = FakeAionGateway({"alpha": True, "beta": False})
    found = gw.detect()
    assert [h.runner_type for h in found] == ["fake", "fake"]
    by_id = {h.state for h in found}
    assert {"ready", "offline"} == by_id
    assert all(isinstance(h, RunnerHealth) for h in found)


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
    assert all(isinstance(h, RunnerHealth) for h in gw.detect())