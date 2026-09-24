"""Opt-in REAL smoke: one tiny canon + story through the operator's own logged-in ``claude`` CLI.

Skipped unless STORYFLOW_RUN_REAL_SMOKE=1 and STORYFLOW_STORY_RUNNER=claude-cli. Sends a <=200 word
source to the model (uses the operator's subscription/quota). Auth/quota/rate-limit failures skip
with the classified reason instead of failing.
"""

import json
import os
import time

import pytest

from storyflow.artifacts import ArtifactStore
from storyflow.integrations.claude_cli import ClaudeCliRunner
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.providers import load_provider_config
from storyflow.story_steps import STORY_VALIDATORS

pytestmark = pytest.mark.skipif(
    os.environ.get("STORYFLOW_RUN_REAL_SMOKE") != "1" or os.environ.get("STORYFLOW_STORY_RUNNER") != "claude-cli",
    reason="set STORYFLOW_RUN_REAL_SMOKE=1 and STORYFLOW_STORY_RUNNER=claude-cli to call the real claude CLI")

SOURCE = (
    "Lena, a young lighthouse keeper, guards a remote island with her stubborn uncle Tomas. Each night she "
    "lights the lamp; each night Tomas insists a ship called the Marigold will return for him. One stormy "
    "evening a stranger, Captain Voss, washes ashore and claims the Marigold sank twenty years ago with "
    "Tomas's brother aboard. Tomas refuses to believe it and locks Voss in the cellar. Lena finds the "
    "captain's logbook, which shows Tomas himself deliberately extinguished the lamp that night. Confronted, "
    "Tomas admits his guilt, lights the lamp for the first time in years and rows out into the storm."
)


def _run(runner, packet):
    started = time.monotonic()
    res = runner.execute(packet)
    if res.code in (ResultCode.AUTH_ERROR, ResultCode.QUOTA_EXHAUSTED, ResultCode.RATE_LIMITED):
        pytest.skip(f"real claude CLI unavailable: {res.code.value} ({res.error_message})")
    assert res.code is ResultCode.SUCCESS, (res.code, res.error_code, res.error_message)
    return time.monotonic() - started


def test_real_canon_and_story(tmp_path):
    config = load_provider_config(env=dict(os.environ), env_files=[])
    config = type(config)(**{**config.__dict__, "story_runner": "claude-cli",
                             "story_timeout": min(config.story_timeout, 600.0)})
    store = ArtifactStore(tmp_path / "artifacts")
    runner = ClaudeCliRunner(config, store)
    if runner.health().ok is False:
        pytest.skip("claude CLI not found")
    pid = "smoke"
    src = f"projects/{pid}/source/0001/source.txt"
    store.write(src, SOURCE.encode())
    canon_rel = f"projects/{pid}/canon/A1/canon.json"
    canon = TaskPacket(task_id="smoke-canon", inputs={"source_artifact": src, "snapshot_id": "s", "project_id": pid},
                       outputs=[canon_rel], task_config={"step": "canon"})
    t_canon = _run(runner, canon)
    assert STORY_VALIDATORS["canon"](canon, store) is None
    json.loads(store.read(canon_rel))
    story_rel = f"projects/{pid}/story/G1/story.md"
    story = TaskPacket(task_id="smoke-story", inputs={"source_artifact": src, "canon_artifact": canon_rel,
                                                      "project_id": pid, "target_length": 120},
                       outputs=[story_rel], task_config={"step": "story"})
    t_story = _run(runner, story)
    assert STORY_VALIDATORS["story"](story, store) is None
    words = len(store.read(story_rel).decode().split())
    print(f"\nREAL SMOKE ok: canon {t_canon:.1f}s, story {t_story:.1f}s, {words} words")
    assert words >= 40
