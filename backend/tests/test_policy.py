"""storyflow.policy: failure policy parsing, backoff maths and error classification (pure functions)."""

import pytest

from storyflow.models import ProjectStatus
from storyflow.policy import (
    NO_SUBTITLE, OPERATOR, PERMANENT, RECOMMENDED, RECOMMENDED_BATCH, TRANSIENT, FailurePolicy, PolicyError,
    classify_source_error, parse_failure_policy, policy_from_config, terminal_status_for,
)


def test_engine_default_is_the_legacy_behaviour():
    p = parse_failure_policy(None)
    assert p == FailurePolicy()
    assert p.subtitle_retries is None and p.retry_base_seconds == 0
    assert p.on_no_subtitle == "pause" and p.on_permanent_error == "pause"
    assert p.backoff_seconds(3) == 0 and not p.exhausted(10_000)


def test_recommended_presets_parse_and_differ_only_in_batch_outcomes():
    single, batch = parse_failure_policy(RECOMMENDED), parse_failure_policy(RECOMMENDED_BATCH)
    assert single.subtitle_retries == 5 and single.retry_base_seconds == 30
    assert (single.on_no_subtitle, single.on_permanent_error) == ("pause", "pause")
    assert (batch.on_no_subtitle, batch.on_permanent_error) == ("skip", "continue")
    assert single.subtitle_retries == batch.subtitle_retries


def test_backoff_is_exponential_and_capped():
    p = FailurePolicy(retry_base_seconds=30, retry_max_seconds=200)
    assert [p.backoff_seconds(n) for n in (1, 2, 3, 4, 5)] == [30, 60, 120, 200, 200]
    assert p.backoff_seconds(0) == 0 and p.backoff_seconds(10_000) == 200  # huge attempt counts do not overflow


def test_exhausted_counts_attempts():
    p = FailurePolicy(subtitle_retries=3)
    assert [p.exhausted(n) for n in (1, 2, 3, 4)] == [False, False, True, True]


@pytest.mark.parametrize("raw", [
    "skip", ["a"], {"nope": 1}, {"on_no_subtitle": "explode"}, {"on_permanent_error": "skip"},
    {"subtitle_retries": 0}, {"subtitle_retries": 1.5}, {"subtitle_retries": True}, {"subtitle_retries": "3"},
    {"subtitle_retries": 1000}, {"retry_base_seconds": -1}, {"retry_base_seconds": float("nan")},
    {"retry_max_seconds": 10 ** 9},
])
def test_invalid_policies_are_rejected(raw):
    with pytest.raises(PolicyError):
        parse_failure_policy(raw)


def test_unknown_key_message_does_not_echo_long_input():
    with pytest.raises(PolicyError) as exc:
        parse_failure_policy({"x" * 500: 1})
    assert len(str(exc.value)) < 200


def test_none_retries_means_unlimited():
    assert parse_failure_policy({"subtitle_retries": None}).subtitle_retries is None


def test_policy_from_config_is_lenient_at_runtime():
    assert policy_from_config({"failure_policy": {"on_no_subtitle": "skip"}}).on_no_subtitle == "skip"
    assert policy_from_config({"failure_policy": {"on_no_subtitle": "garbage"}}) == FailurePolicy()
    assert policy_from_config({}) == FailurePolicy() and policy_from_config(None) == FailurePolicy()


@pytest.mark.parametrize("code,category", [
    ("provider_blocked", TRANSIENT), ("provider_timeout", TRANSIENT),
    ("subtitles_unavailable", NO_SUBTITLE), ("language_unavailable", NO_SUBTITLE), ("empty_source", NO_SUBTITLE),
    ("provider_unavailable", OPERATOR),
    ("subtitle_retries_exhausted", PERMANENT), ("source_not_configured", PERMANENT), ("anything_else", PERMANENT),
    (None, PERMANENT),
])
def test_classify_source_error(code, category):
    assert classify_source_error(code) == category


def test_terminal_status_for_follows_the_policy():
    skip_all = FailurePolicy(on_no_subtitle="skip", on_permanent_error="continue")
    legacy = FailurePolicy()
    assert terminal_status_for(skip_all, NO_SUBTITLE) == ProjectStatus.SKIPPED.value
    assert terminal_status_for(skip_all, PERMANENT) == ProjectStatus.NEEDS_ATTENTION.value
    assert terminal_status_for(legacy, NO_SUBTITLE) is None and terminal_status_for(legacy, PERMANENT) is None
    # operator errors and transient errors never end a project, whatever the policy says
    assert terminal_status_for(skip_all, OPERATOR) is None and terminal_status_for(skip_all, TRANSIENT) is None
