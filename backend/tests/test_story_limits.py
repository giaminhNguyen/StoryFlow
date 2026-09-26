"""Story size / sanity limits (review R3#5, R3#13): a bounded DEFAULT target length, whole-number coercion of an
explicit one, a repetition guard and a wider absolute-path deny-list in ``check_story_text``."""

from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.models import CanonAnalysis, ChannelWorkflow, DomainStatus, SourceSnapshot, StoryProject
from storyflow.pipeline import PipelineContext
from storyflow.story_steps import (
    MAX_DEFAULT_TARGET, REPEAT_MIN_DISTINCT, REPEAT_MIN_LINES, SourceStep, StoryStep, check_story_text,
    coerce_target_length,
)
from storyflow.subtitles import FakeSubtitleClient

NOW = datetime(2026, 7, 1, 9, 0, 0)
INTRO = "Một câu chuyện đủ dài để qua kiểm tra số từ tối thiểu."


def story(*lines):
    return "\n".join([INTRO, *lines])


# --- coerce_target_length ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (8000, 8000), (1, 1), ("8000", 8000), (" 8000 ", 8000), (5000.0, 5000), (0, None), (-5, None), ("0", None),
    (12.5, None), ("12.5", None), ("abc", None), ("1e3", None), ("", None), ("-3", None), (None, None), ([], None),
    ({}, None), (True, None), (False, None), (float("inf"), None), (float("nan"), None), (1e12, None),
    ("9" * 12, None),      # absurd sizes are "not set" rather than a value nobody could ever satisfy
])
def test_coerce_target_length(value, expected):
    assert coerce_target_length(value) == expected


# --- the default target is bounded ---------------------------------------------------------------------------------


def track(words_per_snippet, snippets=3):
    return {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
            "snippets": [{"text": " ".join(["word"] * words_per_snippet), "start": float(i)} for i in range(snippets)]}


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def begin_story(db, session_factory, store, *, source_words, story_cfg):
    """Snapshot of a source with ``source_words`` words, a completed canon, then StoryStep.begin -> job payload."""
    client = FakeSubtitleClient({"vid": {"tracks": [track(source_words // 3)]}})
    ctx = PipelineContext(session_factory=session_factory, store=store, subtitle_client=client, clock=lambda: NOW)
    wf = ChannelWorkflow(name="c", mode="auto", status="active",
                         config={"source": {"video_id": "vid", "languages": ["en"]}, "story": story_cfg})
    db.add(wf)
    db.commit()
    project = StoryProject(title="t", channel_workflow_id=wf.id)
    db.add(project)
    db.commit()
    assert SourceStep().run(ctx, project.id).status.value == "completed"
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project.id))
    db.add(CanonAnalysis(source_snapshot_id=snap.id, status=DomainStatus.COMPLETED.value, canon={"x": 1}))
    db.commit()
    gen_id, spec = StoryStep().begin(db, ctx, project)
    return spec.payload["inputs"]["target_length"], spec.payload["task_config"]["target_length"], gen_id


def test_a_huge_source_gets_a_bounded_default_target(db, session_factory, store):
    inputs_target, task_target, _ = begin_story(db, session_factory, store, source_words=30_000, story_cfg={})
    assert MAX_DEFAULT_TARGET == 15_000
    assert inputs_target == task_target == MAX_DEFAULT_TARGET


def test_a_normal_source_keeps_its_own_word_count_as_the_default(db, session_factory, store):
    inputs_target, task_target, _ = begin_story(db, session_factory, store, source_words=9_000, story_cfg={})
    assert inputs_target == task_target == 9_000


def test_an_explicit_target_is_honoured_as_given_even_above_the_default_cap(db, session_factory, store):
    inputs_target, _, _ = begin_story(db, session_factory, store, source_words=300,
                                      story_cfg={"target_length": 40_000})
    assert inputs_target == 40_000


@pytest.mark.parametrize("given,expected", [("8000", 8000), (5000.0, 5000), (" 7500 ", 7500)])
def test_a_whole_number_in_another_type_is_coerced_and_therefore_enforced(db, session_factory, store, given, expected):
    inputs_target, task_target, gen_id = begin_story(db, session_factory, store, source_words=300,
                                                     story_cfg={"target_length": given})
    assert inputs_target == task_target == expected
    from storyflow.models import StoryGeneration
    assert db.get(StoryGeneration, gen_id, populate_existing=True).config["target_length"] == expected


@pytest.mark.parametrize("junk", ["lots", 12.5, -3, 0, True, [], {}])
def test_junk_targets_fall_back_to_the_default(db, session_factory, store, junk):
    inputs_target, _, _ = begin_story(db, session_factory, store, source_words=600, story_cfg={"target_length": junk})
    assert inputs_target == 600


# --- repetition guard --------------------------------------------------------------------------------------------


def test_a_repetition_loop_is_rejected():
    text = story(*["The rival answers in kind."] * (REPEAT_MIN_LINES + 50))
    assert check_story_text(text) == "story is repetitive (the same lines over and over)"


def test_a_long_story_with_varied_lines_passes():
    text = story(*[f"Scene {i}: another consequence unfolds." for i in range(REPEAT_MIN_LINES * 3)])
    assert check_story_text(text) is None


def test_repetition_is_only_judged_on_long_stories():
    short_loop = story(*["Same line again."] * (REPEAT_MIN_LINES - 5))     # fewer non-empty lines than the minimum
    assert check_story_text(short_loop) is None


def test_the_distinct_share_boundary():
    n = REPEAT_MIN_LINES * 2
    distinct = int(REPEAT_MIN_DISTINCT * (n + 1))          # + the intro line, which is itself distinct
    ok = story(*[f"unique {i}" for i in range(distinct)], *["filler"] * (n - distinct))
    assert check_story_text(ok) is None                     # exactly at / above the share: accepted
    bad = story(*[f"unique {i}" for i in range(distinct // 2)], *["filler"] * (n - distinct // 2))
    assert "repetitive" in check_story_text(bad)


def test_blank_lines_and_indentation_do_not_hide_or_fake_repetition():
    padded = story(*["   The rival answers in kind.   ", "", "\t"] * (REPEAT_MIN_LINES + 10))
    assert "repetitive" in check_story_text(padded)


def test_fake_runner_padding_is_not_repetitive(store):
    """The deterministic fake writer honours a big target with numbered scenes: every line is distinct."""
    import json
    from storyflow.protocol import TaskPacket
    from storyflow.story_steps import FakeStoryPipelineRunner
    canon = {"characters": [{"id": "c1", "name": "Mira", "function": "protagonist"}], "central_conflict": "a secret"}
    rel = store.write("projects/p/canon/a/canon.json", json.dumps(canon).encode("utf-8"))
    packet = TaskPacket(task_id="t", role="story_writer", inputs={"canon_artifact": rel, "target_length": 20_000},
                        outputs=[], task_config={"step": "story"})
    text = FakeStoryPipelineRunner(store)._story(packet).decode("utf-8")
    assert len(text.split()) >= 20_000 and check_story_text(text, target_length=20_000) is None


# --- absolute paths ---------------------------------------------------------------------------------------------

REJECTED = [
    r"The file is at C:\Users\Someone\story.txt today",
    "The file is at C:/Users/Someone/story.txt today",
    r"Look in \\fileserver\share\folder for it",
    "(see \\\\nas\\media\\x.wav)",
    "Open ~/notes/story.md next",
    "Read ~/.config/thing now",
    r"Copy it to %USERPROFILE%\Desktop please",
    r"Then %appdata%\StoryFlow\x and %LOCALAPPDATA%\y",
    "Saved under /Volumes/Backup/story.txt yesterday",
    "The service lives in /srv/app/current now",
    "Check (/data/exports/x.txt) later",
    "Nothing in /proc/1/environ matters",
    "It ran from /home/user/app today",
]


@pytest.mark.parametrize("line", REJECTED)
def test_absolute_paths_are_rejected(line):
    assert check_story_text(story(line)) == "story contains an absolute path"


ACCEPTED = [
    "Read https://example.com/data/export.csv for the numbers",
    "Watch https://www.youtube.com/watch?v=abcdefghijk tonight",
    "Visit http://host/home/page and http://host/srv/x",
    "He said and/or she said, whichever came first",
    "The tilde ~ is just a symbol, and so is ~5 minutes",
    "About 50% of the time, or 100 % of it",
    "The word data/proc/srv alone is fine: data proc srv",
    "A backslash \\ on its own and a\\b are fine",
    "Anh ấy nói: 'Về nhà đi', rồi đi qua /con phố/ vắng",
]


@pytest.mark.parametrize("line", ACCEPTED)
def test_ordinary_prose_and_urls_are_not_mistaken_for_paths(line):
    assert check_story_text(story(line)) is None


def test_the_store_root_is_still_rejected():
    assert check_story_text(story("stored in /opt-elsewhere/store here"), store_root="/opt-elsewhere/store") == \
        "story contains an absolute path"
