"""Review step of the real Claude CLI runner (roadmap 4.4): prompt, answer parser and the runner over the FAKE
claude CLI (tests/fake_claude_cli.py). The real binary and the network are never touched."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from storyflow.artifacts import ArtifactStore
from storyflow.integrations import claude_cli as cc
from storyflow.integrations.claude_cli import (
    MAX_REVIEW_ISSUES, REVISED_DELIMITER, ClaudeCliRunner, ReviewParseError, build_review_prompt, parse_review_output,
)
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.providers import ProviderConfig

FAKE = Path(__file__).with_name("fake_claude_cli.py")
PID = "P1"
SOURCE = "Mira finds the secret. Oren hides it."
STORY = "# Cánh cửa khác\n\nMira mở cánh cửa khác và mọi thứ thay đổi. " * 4


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def make_runner(store, tmp_path, monkeypatch, mode="success", *, timeout=30.0):
    def popen(argv, **kw):
        kw["env"] = {**kw["env"], "FAKE_CLAUDE_MODE": mode}
        return subprocess.Popen([sys.executable, str(FAKE)] + list(argv[1:]), **kw)

    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_CLAUDE_PIDFILE", str(tmp_path / "pids.txt"))
    cfg = ProviderConfig(story_runner="claude-cli", claude_cli="claude", story_timeout=timeout)
    return ClaudeCliRunner(cfg, store, popen=popen, which=lambda name: "claude")


def record(tmp_path):
    return json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))


def canon_json():
    sys.path.insert(0, str(FAKE.parent))
    import fake_claude_cli
    return json.dumps(fake_claude_cli.CANON)


def review_packet(store, *, revise=False, project_id=PID, review_id="R1", target_length=None, with_source=True,
                  outputs=None, config_revise="same", inputs_extra=None, canon=None, story=STORY, write_story=True):
    inputs = {"project_id": project_id, "canon_artifact": f"projects/{project_id}/canon/A1/canon.json",
              "story_artifact": f"projects/{project_id}/story/G1/story.md", "story_version_id": "V1",
              "review_id": review_id, "round_number": 1, "revise": revise, "target_length": target_length}
    store.write(inputs["canon_artifact"], (canon if canon is not None else canon_json()).encode())
    if write_story:
        store.write(inputs["story_artifact"], story.encode("utf-8"))
    if with_source:
        inputs["source_artifact"] = f"projects/{project_id}/source/0001/source.txt"
        store.write(inputs["source_artifact"], SOURCE.encode())
    inputs.update(inputs_extra or {})
    base = f"projects/{project_id}/review/{review_id}"
    if outputs is None:
        outputs = [f"{base}/review.json"] + ([f"{base}/story_revised.md"] if revise else [])
    task_config = {"step": "review", "review_id": review_id, "revise": revise if config_revise == "same" else config_revise,
                   "target_length": target_length}
    return TaskPacket(task_id="t-review", role="story_writer", inputs=inputs, outputs=outputs, task_config=task_config)


def words(text):
    return len(text.split())


# --- prompt ------------------------------------------------------------------------------------------


def test_prompt_carries_the_three_texts_in_delimited_blocks():
    p = build_review_prompt("SRC-TEXT", '{"canon": 1}', "STORY-TEXT", revise=False)
    assert "TASK: review" in p and "TASK: analyse" not in p
    assert "=== CANON (JSON) ===\n{\"canon\": 1}\n=== END CANON ===" in p
    assert "=== REFERENCE STORY ===\nSRC-TEXT\n=== END REFERENCE STORY ===" in p
    assert "=== STORY UNDER REVIEW ===\nSTORY-TEXT\n=== END STORY UNDER REVIEW ===" in p
    assert p.index("=== CANON") < p.index("=== REFERENCE STORY") < p.index("=== STORY UNDER REVIEW")


def test_prompt_names_the_four_aspects_and_the_schema():
    p = build_review_prompt("s", "{}", "t", revise=False)
    for word in ("canon", "logic", "style", "length", "approve", "revise", "verdict", "summary", "issues", "severity"):
        assert word in p
    assert '"version": 1' in p and "approve | revise" in p
    assert "at most 50 issues" in p


def test_prompt_without_revise_forbids_a_rewrite():
    p = build_review_prompt("s", "{}", "t", revise=False)
    assert "no revised story" in p and "Do not rewrite the story" in p
    assert REVISED_DELIMITER not in p


def test_prompt_with_revise_defines_the_delimiter_and_the_corrected_story():
    p = build_review_prompt("s", "{}", "t", revise=True)
    assert f"on its own line write exactly {REVISED_DELIMITER}" in p
    assert "COMPLETE corrected story" in p and "same language" in p and "at least as long" in p
    assert "NO revised story" in p          # ...but only for the verdict "revise"
    assert "no code fence" in p and "never mention file paths" in p


def test_prompt_length_line():
    with_target = build_review_prompt("s", "{}", "t", revise=False, target_length=1234)
    assert "AT LEAST 1234 words" in with_target
    without = build_review_prompt("s", "{}", "t", revise=False)
    assert "AT LEAST" not in without and "proportionate" in without


def test_prompt_without_a_reference_story_omits_that_block():
    p = build_review_prompt("", "{}", "t", revise=False)
    assert "REFERENCE STORY" not in p and "STORY UNDER REVIEW" in p


def test_prompt_is_deterministic_and_keeps_vietnamese_text():
    a = build_review_prompt("nguồn", "{}", "Truyện tiếng Việt: Cánh cửa", revise=True, target_length=10)
    assert a == build_review_prompt("nguồn", "{}", "Truyện tiếng Việt: Cánh cửa", revise=True, target_length=10)
    assert "Truyện tiếng Việt: Cánh cửa" in a


# --- parser ------------------------------------------------------------------------------------------

APPROVE = {"version": 1, "verdict": "approve", "summary": "Good.", "issues": []}
REVISE = {"version": 1, "verdict": "revise", "summary": "Fix it.",
          "issues": [{"aspect": "canon", "severity": "high", "note": "Name changes."}]}


def test_parse_plain_approve():
    review, revised = parse_review_output(json.dumps(APPROVE), revise=False)
    assert review == APPROVE and revised is None


def test_parse_tolerates_fences_prose_and_whitespace():
    fenced = "  \n```json\n" + json.dumps(APPROVE) + "\n```\n "
    assert parse_review_output(fenced, revise=False)[0]["verdict"] == "approve"
    prose = "Here is my review:\n" + json.dumps(APPROVE) + "\nHope it helps!"
    assert parse_review_output(prose, revise=True)[0]["verdict"] == "approve"


def test_parse_verdict_is_case_and_space_insensitive():
    review, _ = parse_review_output(json.dumps({**APPROVE, "verdict": "  Approve "}), revise=False)
    assert review["verdict"] == "approve"


def test_parse_revision_after_the_delimiter():
    text = json.dumps(REVISE) + f"\n{REVISED_DELIMITER}\n# Title\n\nThe corrected story.\n"
    review, revised = parse_review_output(text, revise=True)
    assert review["verdict"] == "revise" and revised == "# Title\n\nThe corrected story.\n"


def test_parse_revision_with_fenced_json_and_a_fence_around_the_whole_answer():
    separate = "```json\n" + json.dumps(REVISE) + f"\n```\n{REVISED_DELIMITER}\nStory line.\n"
    assert parse_review_output(separate, revise=True)[1] == "Story line.\n"
    wrapped = "```\n" + json.dumps(REVISE) + f"\n{REVISED_DELIMITER}\nStory line.\n```"
    assert parse_review_output(wrapped, revise=True)[1] == "Story line.\n"
    fenced_story = json.dumps(REVISE) + f"\n{REVISED_DELIMITER}\n```markdown\nStory line.\n```\n"
    assert parse_review_output(fenced_story, revise=True)[1] == "Story line.\n"


def test_parse_keeps_a_code_block_that_belongs_to_the_story():
    story = "Intro.\n\n```\nnot a wrapper\n```\n\nOutro."
    _, revised = parse_review_output(json.dumps(REVISE) + f"\n{REVISED_DELIMITER}\n{story}", revise=True)
    assert revised == story + "\n"


def test_parse_accepts_crlf_around_the_delimiter():
    text = json.dumps(REVISE) + f"\r\n{REVISED_DELIMITER}\r\nStory.\r\n"
    assert parse_review_output(text, revise=True)[1].strip() == "Story."


def test_parse_ignores_a_story_the_model_volunteers():
    text = json.dumps(REVISE) + f"\n{REVISED_DELIMITER}\nSurprise rewrite."
    assert parse_review_output(text, revise=False)[1] is None          # revision not requested
    approve = json.dumps(APPROVE) + f"\n{REVISED_DELIMITER}\nSurprise rewrite."
    assert parse_review_output(approve, revise=True)[1] is None        # verdict approve: nothing to revise


def test_parse_delimiter_must_be_on_its_own_line():
    inline = json.dumps(REVISE) + f" then {REVISED_DELIMITER} inline story"
    with pytest.raises(ReviewParseError) as exc:
        parse_review_output(inline, revise=True)
    assert exc.value.code == "no_revised_story"


@pytest.mark.parametrize("tail", ["", "\n", "   \n\t\n", "```\n```"])
def test_parse_missing_or_empty_revised_story_is_a_clean_failure(tail):
    with pytest.raises(ReviewParseError) as exc:
        parse_review_output(json.dumps(REVISE) + f"\n{REVISED_DELIMITER}{tail}", revise=True)
    assert exc.value.code == "no_revised_story"


def test_parse_revise_verdict_without_delimiter_fails_only_when_revising():
    with pytest.raises(ReviewParseError) as exc:
        parse_review_output(json.dumps(REVISE), revise=True)
    assert exc.value.code == "no_revised_story"
    review, revised = parse_review_output(json.dumps(REVISE), revise=False)     # balanced preset: judge only
    assert review["verdict"] == "revise" and revised is None


@pytest.mark.parametrize("text,code", [
    ("Great story!", "no_json"), ("", "no_json"), ("[1, 2, 3]", "no_json"), ("{not json}", "bad_json"),
    ('{"verdict": approve}', "bad_json"), ('{"a": 1}', "bad_verdict"),
    (json.dumps({**APPROVE, "verdict": "maybe"}), "bad_verdict"), (json.dumps({**APPROVE, "verdict": None}), "bad_verdict"),
    (json.dumps({"summary": "x"}), "bad_verdict"), (json.dumps({**APPROVE, "verdict": ["approve"]}), "bad_verdict"),
])
def test_parse_bad_answers_are_clean_failures(text, code):
    with pytest.raises(ReviewParseError) as exc:
        parse_review_output(text, revise=False)
    assert exc.value.code == code and exc.value.message


def test_parse_summary_default_and_scrubbing():
    review, _ = parse_review_output(json.dumps({"verdict": "approve"}), revise=False)
    assert review["summary"] == "Verdict: approve." and review["issues"] == []
    review, _ = parse_review_output(json.dumps({"verdict": "approve", "summary": "  "}), revise=False)
    assert review["summary"] == "Verdict: approve."
    leaky = json.dumps({**APPROVE, "summary": "Saved in C:\\Users\\bob\\secret.txt and /home/bob/x sk-abcdefghij"})
    summary = parse_review_output(leaky, revise=False)[0]["summary"]
    assert "bob" not in summary and "sk-abcdefghij" not in summary and "[path]" in summary
    long = parse_review_output(json.dumps({**APPROVE, "summary": "x " * 1000}), revise=False)[0]["summary"]
    assert len(long) <= 500


def test_parse_normalises_issues():
    raw = {**APPROVE, "issues": [
        {"aspect": "STYLE", "severity": "HIGH", "note": "  Repetitive.  "},
        {"aspect": "plot", "severity": "urgent", "note": "Unknown aspect and severity."},
        {"note": "No aspect, no severity."},
        {"aspect": "logic", "severity": "low", "note": ""},
        {"aspect": "logic", "severity": "low"},
        {"aspect": "logic", "severity": "low", "note": 42},
        "A bare string issue",
        None, 7, ["x"],
        {"aspect": "canon", "severity": "medium", "note": "Đổi tên nhân vật ở cảnh hai."},
    ]}
    issues = parse_review_output(json.dumps(raw), revise=False)[0]["issues"]
    assert issues == [
        {"aspect": "style", "severity": "high", "note": "Repetitive."},
        {"aspect": "other", "severity": "medium", "note": "Unknown aspect and severity."},
        {"aspect": "other", "severity": "medium", "note": "No aspect, no severity."},
        {"aspect": "other", "severity": "medium", "note": "A bare string issue"},
        {"aspect": "canon", "severity": "medium", "note": "Đổi tên nhân vật ở cảnh hai."},
    ]


def test_parse_caps_issues_and_note_length_and_scrubs_notes():
    many = [{"aspect": "style", "severity": "low", "note": f"issue {i}"} for i in range(200)]
    issues = parse_review_output(json.dumps({**APPROVE, "issues": many}), revise=False)[0]["issues"]
    assert len(issues) == MAX_REVIEW_ISSUES == 50 and issues[0]["note"] == "issue 0"
    big = {"aspect": "style", "severity": "low", "note": "word " * 500}
    assert len(parse_review_output(json.dumps({**APPROVE, "issues": [big]}), revise=False)[0]["issues"][0]["note"]) <= 500
    leak = {"aspect": "other", "severity": "low", "note": "see /Users/bob/x.txt and Bearer abcdefghijklmnop"}
    note = parse_review_output(json.dumps({**APPROVE, "issues": [leak]}), revise=False)[0]["issues"][0]["note"]
    assert "bob" not in note and "abcdefghijklmnop" not in note


def test_parse_issues_not_a_list_is_treated_as_empty():
    for value in (None, "many", {"a": 1}, 3):
        assert parse_review_output(json.dumps({**APPROVE, "issues": value}), revise=False)[0]["issues"] == []


def test_parse_output_matches_the_review_json_contract():
    review, _ = parse_review_output(json.dumps(REVISE), revise=False)
    assert set(review) == {"version", "verdict", "summary", "issues"} and review["version"] == 1
    assert isinstance(review["summary"], str) and review["summary"]
    for issue in review["issues"]:
        assert set(issue) == {"aspect", "severity", "note"}
        assert issue["aspect"] in cc.REVIEW_ASPECTS and issue["severity"] in cc.REVIEW_SEVERITIES and issue["note"]


# --- runner over the fake CLI --------------------------------------------------------------------


def test_default_answer_is_an_approving_review_and_only_review_json_is_written(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.artifacts == {"outputs": [packet.outputs[0]]}
    review = json.loads(store.read(packet.outputs[0]).decode("utf-8"))
    assert review["verdict"] == "approve" and review["version"] == 1 and review["summary"]
    assert review["issues"][0] == {"aspect": "style", "severity": "low", "note": "Pacing dips slightly in the middle."}
    assert not store.exists(f"projects/{PID}/review/R1/story_revised.md")
    assert res.metrics["duration_ms"] >= 0 and res.metrics["total_cost_usd"] == 0.0123


def test_review_json_is_pretty_utf8_json_with_a_trailing_newline(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store)
    runner.execute(packet)
    raw = store.read(packet.outputs[0]).decode("utf-8")
    assert raw.endswith("}\n") and raw.startswith("{\n  ")


def test_the_prompt_reaches_the_cli_with_all_three_texts_and_the_usual_invocation(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    runner.execute(review_packet(store, target_length=77))
    rec = record(tmp_path)
    assert SOURCE in rec["stdin"] and STORY in rec["stdin"] and '"central_conflict"' in rec["stdin"]
    assert "TASK: review" in rec["stdin"] and "AT LEAST 77 words" in rec["stdin"]
    assert rec["argv"] == ["-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
                           "--append-system-prompt", cc.SYSTEM_PROMPT]
    assert not any(SOURCE in a or STORY in a for a in rec["argv"])           # long texts go through stdin only


def test_revision_writes_both_files_and_the_story_is_long_enough(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_revise")
    packet = review_packet(store, revise=True)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.artifacts == {"outputs": packet.outputs}
    review = json.loads(store.read(packet.outputs[0]).decode("utf-8"))
    revised = store.read(packet.outputs[1]).decode("utf-8")
    assert review["verdict"] == "revise" and [i["aspect"] for i in review["issues"]] == ["canon", "logic"]
    assert words(revised) >= words(STORY) and revised.endswith("\n") and REVISED_DELIMITER not in revised
    assert "STORY UNDER REVIEW" not in revised


def test_revise_requested_but_verdict_approve_writes_review_only(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_approve")
    packet = review_packet(store, revise=True)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.artifacts == {"outputs": [packet.outputs[0]]}
    assert not store.exists(packet.outputs[1])


def test_revision_not_requested_ignores_the_volunteered_story(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_revise")
    packet = review_packet(store, revise=False)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.artifacts == {"outputs": [packet.outputs[0]]}
    assert json.loads(store.read(packet.outputs[0]).decode())["verdict"] == "revise"
    assert not store.exists(f"projects/{PID}/review/R1/story_revised.md")
    assert "NO revised story" not in record(tmp_path)["stdin"] and REVISED_DELIMITER not in record(tmp_path)["stdin"]


def test_fenced_answer_is_accepted(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_fenced")
    packet = review_packet(store)
    assert runner.execute(packet).code is ResultCode.SUCCESS
    assert json.loads(store.read(packet.outputs[0]).decode())["verdict"] == "approve"


@pytest.mark.parametrize("mode,revise,code", [
    ("review_bad_json", False, "no_json"), ("review_bad_json", True, "no_json"),
    ("review_bad_verdict", False, "bad_verdict"), ("review_no_delimiter", True, "no_revised_story"),
])
def test_unusable_answers_fail_cleanly_and_write_nothing(store, tmp_path, monkeypatch, mode, revise, code):
    runner = make_runner(store, tmp_path, monkeypatch, mode)
    packet = review_packet(store, revise=revise)
    res = runner.execute(packet)
    assert (res.code, res.error_code) == (ResultCode.INVALID_OUTPUT, code)
    assert res.error_message and "Users" not in res.error_message
    assert not any(store.exists(p) for p in packet.outputs)


def test_revise_verdict_without_a_story_is_fine_when_not_revising(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_no_delimiter")
    packet = review_packet(store, revise=False)
    assert runner.execute(packet).code is ResultCode.SUCCESS


def test_reference_story_is_optional(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    assert runner.execute(review_packet(store, with_source=False)).code is ResultCode.SUCCESS
    stdin = record(tmp_path)["stdin"]
    assert "REFERENCE STORY" not in stdin and STORY in stdin


def test_target_length_comes_from_inputs_then_task_config(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store, target_length=None)
    packet.task_config["target_length"] = 55
    runner.execute(packet)
    assert "AT LEAST 55 words" in record(tmp_path)["stdin"]
    runner.execute(review_packet(store, target_length=90))
    assert "AT LEAST 90 words" in record(tmp_path)["stdin"]


def test_revise_flag_task_config_wins_and_only_true_counts(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "review_revise")
    # task_config says revise, inputs say no: two outputs are declared and a story is produced
    packet = review_packet(store, revise=True, inputs_extra={"revise": False})
    assert runner.execute(packet).artifacts == {"outputs": packet.outputs}
    # a truthy string is not "true": no revision, so two declared outputs are rejected
    bad = review_packet(store, revise=True, config_revise="true")
    res = runner.execute(bad)
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "bad_path")


def test_the_step_is_supported_and_other_steps_are_unchanged(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    res = runner.execute(TaskPacket(task_id="x", inputs={"project_id": PID}, outputs=["a"], task_config={"step": "tts"}))
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "unsupported_step")
    canon = TaskPacket(task_id="t-canon", inputs={"source_artifact": f"projects/{PID}/source/0001/source.txt",
                                                  "project_id": PID},
                       outputs=[f"projects/{PID}/canon/A1/canon.json"], task_config={"step": "canon"})
    store.write(canon.inputs["source_artifact"], b"A source that even mentions TASK: review is still a canon job.")
    assert runner.execute(canon).code is ResultCode.SUCCESS
    assert json.loads(store.read(canon.outputs[0]).decode())["version"] == 1


# --- output paths ------------------------------------------------------------------------------------


@pytest.mark.parametrize("outputs,revise", [
    ([], False), (["projects/P1/review/R1/review.json", "projects/P1/review/R1/story_revised.md"], False),
    (["projects/P1/review/R1/review.json"], True),
    (["projects/P1/review/R1/story_revised.md"], False), (["projects/P1/review/R1/review.json.bak"], False),
    (["projects/P1/review/R1/story.md"], False), (["review.json"], False), (["projects/review.json"], False),
    (["projects/P2/review/R1/review.json"], False), (["other/P1/review/R1/review.json"], False),
    (["projects/P1/../P2/review/R1/review.json"], False), (["../secret/review.json"], False),
    (["C:\\Windows\\review.json"], False), (["/etc/review.json"], False), ([None], False), ([5], False),
    (["projects/P1/review/R1/review.json", "projects/P1/review/R2/story_revised.md"], True),
    (["projects/P1/review/R1/review.json", "projects/P1/review/R1/story.md"], True),
    (["projects/P1/review/R1/story_revised.md", "projects/P1/review/R1/review.json"], True),
    (["projects/P1/review/R1/review.json", "projects/P2/review/R1/story_revised.md"], True),
])
def test_unsafe_or_wrong_output_paths_are_rejected_before_the_cli_runs(store, tmp_path, monkeypatch, outputs, revise):
    runner = make_runner(store, tmp_path, monkeypatch)
    res = runner.execute(review_packet(store, revise=revise, outputs=outputs))
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "bad_path")
    assert not (tmp_path / "record.json").exists()                      # the CLI was never started
    assert store.read(f"projects/{PID}/story/G1/story.md")              # nothing else was touched


def test_missing_project_id_is_rejected(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store)
    del packet.inputs["project_id"]
    assert runner.execute(packet).error_code == "bad_path"


def test_input_paths_must_stay_inside_the_project(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    for key in ("story_artifact", "canon_artifact", "source_artifact"):
        packet = review_packet(store)
        store.write("projects/P2/story/G1/story.md", b"someone else's story")
        packet.inputs[key] = "projects/P2/story/G1/story.md"
        res = runner.execute(packet)
        assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "bad_path"), key
        packet.inputs[key] = "../outside.txt"
        assert runner.execute(packet).error_code == "bad_path", key


# --- inputs -----------------------------------------------------------------------------------------


def test_missing_input_artifacts_fail_cleanly(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store)
    packet.inputs["story_artifact"] = f"projects/{PID}/story/G9/story.md"          # not on disk
    assert (runner.execute(packet).error_code) == "missing_input"
    packet = review_packet(store)
    packet.inputs["canon_artifact"] = f"projects/{PID}/canon/A9/canon.json"
    assert runner.execute(packet).error_code == "missing_input"
    packet = review_packet(store)
    packet.inputs["source_artifact"] = f"projects/{PID}/source/0009/source.txt"
    assert runner.execute(packet).error_code == "missing_input"
    assert not (tmp_path / "record.json").exists()


def test_story_artifact_is_required(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = review_packet(store)
    del packet.inputs["story_artifact"]
    res = runner.execute(packet)
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "bad_path")


def test_unreadable_inputs_are_bad_input(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    assert runner.execute(review_packet(store, canon="{ this is not json")).error_code == "bad_input"
    packet = review_packet(store)
    store.write(packet.inputs["story_artifact"], b"\xff\xfe\xfa not utf-8")
    assert runner.execute(packet).error_code == "bad_input"
    assert not (tmp_path / "record.json").exists()


# --- failures of the CLI itself map exactly like canon / story -----------------------------------------


@pytest.mark.parametrize("mode,code,error", [
    ("quota", ResultCode.QUOTA_EXHAUSTED, "quota_exhausted"), ("auth", ResultCode.AUTH_ERROR, "auth_error"),
    ("rate_limit", ResultCode.RATE_LIMITED, "rate_limited"), ("crash", ResultCode.TRANSIENT_FAILURE, "cli_failed"),
    ("invalid_json", ResultCode.INVALID_OUTPUT, "invalid_envelope"), ("empty", ResultCode.INVALID_OUTPUT, "empty_result"),
    ("non_utf8", ResultCode.INVALID_OUTPUT, "bad_encoding"),
])
def test_cli_failures_are_mapped_and_nothing_is_written(store, tmp_path, monkeypatch, mode, code, error):
    runner = make_runner(store, tmp_path, monkeypatch, mode)
    packet = review_packet(store, revise=True)
    res = runner.execute(packet)
    assert (res.code, res.error_code) == (code, error)
    assert not any(store.exists(p) for p in packet.outputs)
    assert "Users" not in (res.error_message or "") and "sk-" not in (res.error_message or "")


def test_the_same_failure_maps_identically_for_canon_and_review(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "quota")
    canon = TaskPacket(task_id="c", inputs={"source_artifact": f"projects/{PID}/source/0001/source.txt",
                                            "project_id": PID},
                       outputs=[f"projects/{PID}/canon/A1/canon.json"], task_config={"step": "canon"})
    store.write(canon.inputs["source_artifact"], SOURCE.encode())
    a, b = runner.execute(canon), runner.execute(review_packet(store))
    assert (a.code, a.error_code, a.quota_reset_at) == (b.code, b.error_code, b.quota_reset_at)


def test_timeout_is_a_timeout_and_the_process_is_forgotten(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "slow", timeout=3.0)
    started = time.monotonic()
    res = runner.execute(review_packet(store))
    assert (res.code, res.error_code) == (ResultCode.TIMEOUT, "timeout")
    assert time.monotonic() - started < 30 and runner._procs == {}


def test_cli_missing_is_reported(store, tmp_path, monkeypatch):
    cfg = ProviderConfig(story_runner="claude-cli", claude_cli="claude")
    runner = ClaudeCliRunner(cfg, store, which=lambda name: None)
    res = runner.execute(review_packet(store))
    assert (res.code, res.error_code) == (ResultCode.RUNNER_CRASHED, "cli_not_found")
