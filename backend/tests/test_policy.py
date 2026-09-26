"""storyflow.policy: failure policy parsing, backoff maths and error classification (pure functions)."""

import pytest

from storyflow.models import ProjectStatus
from storyflow.policy import (
    BREAKER_LIMIT, NO_SUBTITLE, OPERATOR, PERMANENT, RECOMMENDED, SYSTEMIC_CODES, TRANSIENT, FailurePolicy,
    PolicyError, classify_source_error, is_systemic_error, parse_failure_policy, policy_from_config,
    terminal_status_for, with_recommended,
)


def test_engine_default_is_the_legacy_behaviour():
    p = parse_failure_policy(None)
    assert p == FailurePolicy()
    assert p.subtitle_retries is None and p.retry_base_seconds == 0
    assert p.on_no_subtitle == "pause" and p.on_permanent_error == "pause"
    assert p.backoff_seconds(3) == 0 and not p.exhausted(10_000)


def test_recommended_policy_parses_and_a_batch_policy_only_changes_the_outcomes():
    single = parse_failure_policy(RECOMMENDED)
    batch = parse_failure_policy({**RECOMMENDED, "on_no_subtitle": "skip", "on_permanent_error": "continue"})
    assert single.subtitle_retries == 5 and single.retry_base_seconds == 30
    assert (single.on_no_subtitle, single.on_permanent_error) == ("pause", "pause")
    assert (batch.on_no_subtitle, batch.on_permanent_error) == ("skip", "continue")
    assert single.subtitle_retries == batch.subtitle_retries


def test_with_recommended_merges_a_partial_policy_over_the_recommended_values():
    assert with_recommended({}) == RECOMMENDED
    partial = with_recommended({"on_no_subtitle": "skip", "on_permanent_error": "continue"})
    assert partial == {**RECOMMENDED, "on_no_subtitle": "skip", "on_permanent_error": "continue"}
    assert parse_failure_policy(partial).subtitle_retries == 5 and parse_failure_policy(partial).retry_base_seconds == 30
    assert with_recommended({"subtitle_retries": None})["subtitle_retries"] is None      # explicit null = legacy opt-out
    assert with_recommended({"retry_base_seconds": 5})["retry_base_seconds"] == 5         # given keys win
    assert with_recommended(None) is None and with_recommended("skip") == "skip"          # not a dict: unchanged
    given = {"on_no_subtitle": "skip"}
    with_recommended(given)
    assert given == {"on_no_subtitle": "skip"}                                             # the input is not mutated


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
    {"subtitle_retries": float("inf")}, {"retry_base_seconds": float("inf")}, {"retry_max_seconds": float("-inf")},
    {"retry_base_seconds": float("nan")}, {"retry_base_seconds": 10 ** 400},
    {"retry_base_seconds": 100, "retry_max_seconds": 50},
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


def test_backoff_is_never_capped_below_the_base_delay():
    # base larger than the default max: the delay is the base, not silently cut to 900 s
    assert FailurePolicy(retry_base_seconds=3600).backoff_seconds(1) == 3600
    assert FailurePolicy(retry_base_seconds=3600).backoff_seconds(4) == 3600
    assert FailurePolicy(retry_base_seconds=30, retry_max_seconds=10).backoff_seconds(3) == 30
    assert FailurePolicy(retry_base_seconds=30, retry_max_seconds=200).backoff_seconds(4) == 200   # normal cap unchanged


def test_max_may_equal_base_and_a_partial_pair_is_not_cross_checked():
    assert parse_failure_policy({"retry_base_seconds": 60, "retry_max_seconds": 60}).retry_max_seconds == 60
    assert parse_failure_policy({"retry_base_seconds": 5000}).retry_base_seconds == 5000       # default max 900 < base: fine


@pytest.mark.parametrize("code", ["INFRA_EXHAUSTED", "lease_expired", "runner_crashed", "timeout", "rate_limited",
                                  "quota_exhausted", "auth_error", "ALL_AGENTS_UNAVAILABLE", "cancelled",
                                  "voice_not_found", "cli_not_found", "provider_unavailable"])
def test_systemic_codes_are_recognised_case_insensitively(code):
    assert is_systemic_error(code) and code.lower() in SYSTEMIC_CODES


@pytest.mark.parametrize("code", ["task_failed", "invalid_output", "invalid_story", "missing_output", "poisoned",
                                  "internal_error", "chunks_missing", None, ""])
def test_item_errors_are_not_systemic(code):
    assert not is_systemic_error(code)


def test_breaker_limit_is_a_small_positive_number():
    assert isinstance(BREAKER_LIMIT, int) and 2 <= BREAKER_LIMIT <= 10
