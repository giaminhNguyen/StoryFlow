"""Runner roles. Roles are independent of runner types: any runner may declare support
for any role; the dispatcher never assumes "claude = story_writer"."""

import enum


class Role(str, enum.Enum):
    STORY_WRITER = "story_writer"
    TTS_ADAPTER = "tts_adapter"
    REVIEWER = "reviewer"
    GENERAL_WORKER = "general_worker"


DEFAULT_ROLE = Role.GENERAL_WORKER.value

# A new runner instance implicitly supports this set unless overridden.
DEFAULT_SUPPORTED_ROLES = [Role.GENERAL_WORKER.value]