"""Stage graph per job kind. A stage becomes runnable when every dependency succeeded; a stage
whose dependency failed or was skipped is skipped, except join stages (publish) which run once
all their dependencies are terminal, whatever the outcome (speech/visual branches are independent)."""

GRAPHS = {
    "video": {
        "prepare": (),
        "asr": ("prepare",),
        "transcript_index": ("asr",),
        "vision": ("prepare",),
        "publish": ("transcript_index", "vision"),
    },
    "transcript": {
        "prepare": (),
        "asr": ("prepare",),
        "transcript_index": ("asr",),
        "publish": ("transcript_index",),
    },
}
JOIN_STAGES = {"publish"}
TERMINAL = {"succeeded", "failed", "skipped"}
# Separate queues so a long ASR or vision run never starves the short control/publish work.
QUEUES = {"prepare": "media", "vision": "media", "asr": "asr", "transcript_index": "embed", "publish": "control"}
ALL_QUEUES = ("media", "asr", "embed", "control")
